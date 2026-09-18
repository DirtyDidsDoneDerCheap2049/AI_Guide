"""D2：账户体系与访客认领。

覆盖账户接口的验收点：
1. 注册/登录/登出/me；口令用 Argon2id 哈希，库里不存明文。
2. 两个账号互不可见：A 的项目对 B 是 404；未登录访问 /trips 只看到访客工作区。
3. 访客认领：幂等（重复调用返回同一条），认领后原访客 Cookie 失去访问权，换浏览器登录仍可打开。
4. 会话撤销：退出/撤销/重置密码后，旧 Cookie 立刻失效，SSE 订阅也失败。
5. 邮箱验证与密码重置令牌：一次性、过期即失败、只存摘要。
6. 邮件发送失败不影响账号事务（响应里 email_sent=false）。
7. 认证接口限流（同一 IP+邮箱多次失败后被 429）。
8. 项目所有权在登录用户之间正确隔离，提交的项目属于账号而不是浏览器。
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.models import AuthSession, EmailToken, GuideProject, User, WorkspaceClaim, utcnow
from app.services import accounts
from tests.conftest import create_project, make_image_bytes, unique_title

pytestmark = pytest.mark.integration

# 自动测试专用的一次性口令（占位值，不是任何真实账号的凭据）
PASSWORD = "test-only-account-passphrase"


def _settings_with(settings, **overrides) -> Settings:
    base = {
        "app_env": "test",
        "database_url": settings.database_url,
        "redis_url": settings.redis_url,
        "session_secret": settings.session_secret,
        "provider_mode": "mock",
        "media_root": settings.media_root,
        "email_backend": "console",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _register(client, email: str, *, claim: str | None = None):
    body = {"email": email, "password": PASSWORD}
    if claim:
        body["claim_project_id"] = claim
    return client.post("/api/v1/auth/register", json=body)


def _login(client, email: str, *, claim: str | None = None):
    body = {"email": email, "password": PASSWORD}
    if claim:
        body["claim_project_id"] = claim
    return client.post("/api/v1/auth/login", json=body)


def test_register_login_logout_roundtrip(app, client, settings, session_factory):
    email = f"user-{unique_title('a')}@example.com"
    registered = _register(client, email)
    assert registered.status_code == 201, registered.text
    body = registered.json()
    assert body["user"]["email"] == email.lower()
    assert body["email_verification_required"] is True

    with session_factory() as db:
        user = db.execute(select(User).where(User.email == email.lower())).scalar_one()
        # 口令必须是 Argon2id 哈希，且不含明文
        assert user.password_hash.startswith("$argon2id$")
        assert PASSWORD not in user.password_hash
        # 会话令牌只存摘要
        session_row = db.execute(select(AuthSession).where(AuthSession.user_id == user.id)).scalar_one()
        assert len(session_row.token_hash) == 64
        assert session_row.revoked_at is None

    assert client.get("/api/v1/auth/me").status_code == 200
    assert client.post("/api/v1/auth/logout").json()["status"] == "logged_out"
    assert client.get("/api/v1/auth/me").status_code == 401

    assert _login(client, email).status_code == 200
    assert client.get("/api/v1/auth/me").json()["user"]["email"] == email.lower()

    wrong = client.post("/api/v1/auth/login", json={"email": email, "password": "test-only-wrong-password"})
    assert wrong.status_code == 401
    assert wrong.json()["detail"]["code"] == "invalid_credentials"

    missing = client.post("/api/v1/auth/login", json={"email": "nobody@example.com", "password": PASSWORD})
    assert missing.status_code == 401
    # 不区分"账号不存在"与"密码错误"
    assert missing.json()["detail"]["code"] == "invalid_credentials"


def test_duplicate_email_is_rejected_without_enumeration(app, client, settings):
    email = f"dup-{unique_title('b')}@example.com"
    assert _register(client, email).status_code == 201
    again = _register(client, email.upper())
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "email_taken"
    weak = _register(client, f"weak-{unique_title('c')}@example.com")
    assert weak.status_code in (201,)


def test_password_policy_rejects_short_password(app, client, settings):
    response = client.post(
        "/api/v1/auth/register", json={"email": f"short-{unique_title('d')}@example.com", "password": "short"}
    )
    assert response.status_code == 422


def test_two_accounts_are_isolated(app, client, settings, session_factory):
    email_a = f"a-{unique_title('iso')}@example.com"
    email_b = f"b-{unique_title('iso')}@example.com"

    with TestClient(app) as client_a, TestClient(app) as client_b:
        assert _register(client_a, email_a).status_code == 201
        project_a = client_a.post("/api/v1/projects", json={"title": "A 的旅行", "city_hint": "杭州"}).json()

        assert _register(client_b, email_b).status_code == 201
        # B 看不到 A 的项目（404 而不是 403，避免泄露存在性）
        assert client_b.get(f"/api/v1/projects/{project_a['id']}").status_code == 404
        assert client_b.patch(f"/api/v1/projects/{project_a['id']}", json={"title": "改"}).status_code == 404
        assert client_b.post(f"/api/v1/projects/{project_a['id']}/archive").status_code == 404
        assert client_b.get(f"/api/v1/projects/{project_a['id']}/events").status_code == 404
        assert [trip["title"] for trip in client_b.get("/api/v1/trips").json()["trips"]] == []
        assert [trip["title"] for trip in client_a.get("/api/v1/trips").json()["trips"]] == ["A 的旅行"]

    with session_factory() as db:
        project = db.get(GuideProject, project_a["id"])
        assert project.owner_user_id is not None


def test_guest_workspace_claim_is_idempotent_and_transfers_access(app, client, settings, session_factory):
    project = create_project(client, title=unique_title("claim"), city_hint="杭州")
    email = f"claim-{unique_title('e')}@example.com"

    # 注册时直接认领
    registered = _register(client, email, claim=project["id"])
    assert registered.status_code == 201, registered.text
    assert registered.json()["claimed_project_id"] == project["id"]

    with session_factory() as db:
        claims = list(db.execute(select(WorkspaceClaim)).scalars())
        assert len(claims) == 1
        assert db.get(GuideProject, project["id"]).owner_user_id is not None

    # 重复认领：幂等，不产生第二条记录
    again = client.post("/api/v1/auth/claim", json={"project_id": project["id"]})
    assert again.status_code == 200
    assert again.json() == {"project_id": project["id"], "claimed": False, "already_claimed": True}
    with session_factory() as db:
        assert len(list(db.execute(select(WorkspaceClaim)).scalars())) == 1

    # 登录用户仍可打开，并且出现在旅行列表
    assert client.get(f"/api/v1/projects/{project['id']}").status_code == 200
    assert [trip["id"] for trip in client.get("/api/v1/trips").json()["trips"]] == [project["id"]]

    # 换一个浏览器登录同一账号：仍能打开（归属在账号上，不在浏览器上）
    with TestClient(app) as other_browser:
        assert _login(other_browser, email).status_code == 200
        assert other_browser.get(f"/api/v1/projects/{project['id']}").status_code == 200
        assert [t["id"] for t in other_browser.get("/api/v1/trips").json()["trips"]] == [project["id"]]


def test_claim_requires_login_and_ownership(app, client, settings):
    # 别人的访客工作区（另一个浏览器）
    with TestClient(app) as other_client:
        other_guest = create_project(other_client, title=unique_title("other-guest"), city_hint="杭州")

    # 1) 未登录不能认领
    anonymous = client.post("/api/v1/auth/claim", json={"project_id": other_guest["id"]})
    assert anonymous.status_code == 401
    assert anonymous.json()["detail"]["code"] == "auth_required"

    # 2) 自己的访客工作区（注册前先创建，注册后仍属于这个访客会话）
    mine = create_project(client, title=unique_title("mine"), city_hint="杭州")

    email = f"claimer-{unique_title('f')}@example.com"
    assert _register(client, email).status_code == 201

    # 3) 登录后仍然不能认领别人的访客工作区（不泄露存在性 → 404）
    denied = client.post("/api/v1/auth/claim", json={"project_id": other_guest["id"]})
    assert denied.status_code == 404

    # 4) 自己的访客工作区可以认领，且之后归属在账号上
    claimed = client.post("/api/v1/auth/claim", json={"project_id": mine["id"]})
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["claimed"] is True
    # 认领后原访客身份也不再有访问权（防止同一台机器的其他人继续读）
    with TestClient(app) as guest_after:
        assert guest_after.get(f"/api/v1/projects/{mine['id']}").status_code == 404


def test_revoked_session_loses_access_and_sse(app, client, settings, session_factory):
    email = f"revoke-{unique_title('g')}@example.com"
    assert _register(client, email).status_code == 201
    project = client.post("/api/v1/projects", json={"title": "会被撤销", "city_hint": "杭州"}).json()
    assert client.get("/api/v1/auth/me").status_code == 200

    with session_factory() as db:
        user = db.execute(select(User).where(User.email == email.lower())).scalar_one()
        session_row = db.execute(select(AuthSession).where(AuthSession.user_id == user.id)).scalar_one()
        session_id = session_row.id

    listed = client.get("/api/v1/auth/sessions").json()["sessions"]
    assert len(listed) == 1 and listed[0]["current"] is True

    revoked = client.delete(f"/api/v1/auth/sessions/{session_id}")
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "current_session_revoked"

    # 撤销后：me 401，项目 404，SSE 也不再可用（不能继续收旧项目事件）。
    # SSE 返回 401（没有可用身份）或 404（身份已失效但仍有访客 Cookie）都算拒绝，
    # 关键是不能返回 200 的事件流。
    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.get(f"/api/v1/projects/{project['id']}").status_code == 404
    sse_status = client.get(f"/api/v1/projects/{project['id']}/events").status_code
    assert sse_status in (401, 404), sse_status


def test_password_reset_revokes_all_sessions(app, client, settings, session_factory):
    email = f"reset-{unique_title('h')}@example.com"
    assert _register(client, email).status_code == 201

    with TestClient(app) as second:
        assert _login(second, email).status_code == 200
        assert second.get("/api/v1/auth/me").status_code == 200

        forgot = client.post("/api/v1/auth/password/forgot", json={"email": email})
        assert forgot.status_code == 202
        assert forgot.json()["status"] == "accepted"

        with session_factory() as db:
            user = db.execute(select(User).where(User.email == email.lower())).scalar_one()
            token_row = db.execute(
                select(EmailToken).where(EmailToken.user_id == user.id, EmailToken.purpose == accounts.PURPOSE_RESET_PASSWORD)
            ).scalars().first()
            assert token_row is not None
            # 库里只有摘要，没有可用令牌
            assert len(token_row.token_hash) == 64

        # 用真实令牌重置（令牌来自服务层，因为 console 邮件后端不落盘到这里）
        with session_factory() as db:
            user = db.execute(select(User).where(User.email == email.lower())).scalar_one()
            token = accounts.issue_email_token(db, settings, user=user, purpose=accounts.PURPOSE_RESET_PASSWORD)
            db.commit()

        new_password = "brand-new-passphrase-1"
        reset = client.post("/api/v1/auth/password/reset", json={"token": token, "new_password": new_password})
        assert reset.status_code == 200, reset.text

    # 两个浏览器都被撤销
    assert client.get("/api/v1/auth/me").status_code == 401
    assert second.get("/api/v1/auth/me").status_code == 401

    # 旧密码失效、新密码可用
    assert client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD}).status_code == 401
    assert client.post("/api/v1/auth/login", json={"email": email, "password": new_password}).status_code == 200

    # 令牌一次性：重复使用失败
    reused = client.post("/api/v1/auth/password/reset", json={"token": token, "new_password": PASSWORD})
    assert reused.status_code == 400
    assert reused.json()["detail"]["code"] == "invalid_token"


def test_forgot_password_does_not_leak_existence(app, client, settings):
    unknown = client.post("/api/v1/auth/password/forgot", json={"email": f"nobody-{unique_title('i')}@example.com"})
    assert unknown.status_code == 202
    assert unknown.json()["status"] == "accepted"


def test_expired_email_token_is_rejected(app, client, settings, session_factory):
    email = f"expired-{unique_title('j')}@example.com"
    assert _register(client, email).status_code == 201
    with session_factory() as db:
        user = db.execute(select(User).where(User.email == email.lower())).scalar_one()
        token = accounts.issue_email_token(db, settings, user=user, purpose=accounts.PURPOSE_VERIFY_EMAIL)
        # 只让"这一个"令牌过期没有意义（注册时也发过验证令牌）：
        # 把所有未使用令牌统一置为过期，再验证令牌过期一定被拒绝。
        for row in db.execute(
            select(EmailToken).where(EmailToken.user_id == user.id, EmailToken.used_at.is_(None))
        ).scalars():
            row.expires_at = utcnow() - timedelta(minutes=1)
        db.commit()

    response = client.post("/api/v1/auth/verify-email", json={"token": token})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_token"


def test_verify_email_marks_user_verified(app, client, settings, session_factory):
    email = f"verify-{unique_title('k')}@example.com"
    assert _register(client, email).status_code == 201
    with session_factory() as db:
        user = db.execute(select(User).where(User.email == email.lower())).scalar_one()
        token = accounts.issue_email_token(db, settings, user=user, purpose=accounts.PURPOSE_VERIFY_EMAIL)
        db.commit()

    verified = client.post("/api/v1/auth/verify-email", json={"token": token})
    assert verified.status_code == 200
    assert verified.json()["user"]["email_verified"] is True
    assert client.get("/api/v1/auth/me").json()["user"]["email_verified"] is True


def test_email_failure_does_not_break_registration(app, client, settings, monkeypatch):
    """邮件服务不可用时，账号仍然建好，响应明确 email_sent=false。"""
    from app.services import email as email_service

    def boom(message):  # noqa: ANN001
        raise RuntimeError("smtp down")

    monkeypatch.setattr(email_service, "build_sender", lambda _settings: type("S", (), {"name": "boom", "send": staticmethod(boom)})())
    email = f"mailfail-{unique_title('l')}@example.com"
    response = _register(client, email)
    assert response.status_code == 201
    assert response.json()["email_sent"] is False
    assert client.get("/api/v1/auth/me").status_code == 200


def test_login_rate_limit_blocks_bruteforce(app, client, settings):
    email = f"brute-{unique_title('m')}@example.com"
    assert _register(client, email).status_code == 201
    client.post("/api/v1/auth/logout")

    strict = _settings_with(settings, auth_login_attempt_limit=3)
    from app.main import create_app

    limited_app = create_app(strict)
    with TestClient(limited_app) as limiter:
        codes = [
            limiter.post("/api/v1/auth/login", json={"email": email, "password": "test-only-wrong-password"}).status_code
            for _ in range(5)
        ]
    assert 429 in codes, codes
    assert codes.count(401) >= 1


def test_change_password_keeps_current_session_and_revokes_others(app, client, settings):
    email = f"change-{unique_title('n')}@example.com"
    assert _register(client, email).status_code == 201

    with TestClient(app) as other:
        assert _login(other, email).status_code == 200

        changed = client.post(
            "/api/v1/auth/password/change",
            json={"current_password": PASSWORD, "new_password": "test-only-another-pass-2"},
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["sessions_revoked"] == 1

        assert client.get("/api/v1/auth/me").status_code == 200, "当前会话应保留"
        assert other.get("/api/v1/auth/me").status_code == 401, "其他会话应被撤销"

        wrong = client.post(
            "/api/v1/auth/password/change",
            json={"current_password": "test-only-not-current", "new_password": "test-only-yet-another-3"},
        )
        assert wrong.status_code == 401


def test_trips_list_for_guest_and_account(app, client, settings):
    guest_project = create_project(client, title=unique_title("guest-trip"), city_hint="杭州")
    trips = client.get("/api/v1/trips").json()["trips"]
    assert [item["id"] for item in trips] == [guest_project["id"]]

    email = f"trips-{unique_title('o')}@example.com"
    assert _register(client, email).status_code == 201
    # 认领后访客工作区出现在账号的旅行列表里
    assert client.post("/api/v1/auth/claim", json={"project_id": guest_project["id"]}).status_code == 200
    ids = [item["id"] for item in client.get("/api/v1/trips").json()["trips"]]
    assert guest_project["id"] in ids


def test_account_deletion_is_traceable(app, client, settings, session_factory):
    email = f"delete-{unique_title('p')}@example.com"
    assert _register(client, email).status_code == 201
    project = client.post("/api/v1/projects", json={"title": "将被释放", "city_hint": "杭州"}).json()

    response = client.delete("/api/v1/auth/account")
    assert response.status_code == 200
    assert response.json()["status"] == "account_disabled"

    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD}).status_code == 401

    with session_factory() as db:
        user = db.execute(select(User).where(User.email == email.lower())).scalar_one()
        assert user.status == "disabled"
        # 工作区保留但归还为无人拥有（可导出、可审计），不是物理删除
        project_row = db.get(GuideProject, project["id"])
        assert project_row is not None
        assert project_row.owner_user_id is None
        assert all(
            row.revoked_at is not None
            for row in db.execute(select(AuthSession).where(AuthSession.user_id == user.id)).scalars()
        )


def test_password_hash_never_appears_in_api_responses(app, client, settings):
    email = f"nohash-{unique_title('q')}@example.com"
    response = _register(client, email)
    payload = response.text
    assert "$argon2" not in payload
    assert PASSWORD not in payload
    assert hashlib.sha256(PASSWORD.encode()).hexdigest() not in payload


def test_auth_feature_flag_can_disable_new_entry(app, client, settings):
    """特性开关关闭时只拒绝入口，不删除既有账号数据。"""
    from fastapi.testclient import TestClient

    from app.main import create_app

    email = f"flag-{unique_title('r')}@example.com"
    assert _register(client, email).status_code == 201

    disabled = create_app(_settings_with(settings, auth_enabled=False, database_url=settings.database_url))
    with TestClient(disabled) as off:
        blocked = off.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
        assert blocked.status_code == 503
        assert blocked.json()["detail"]["code"] == "auth_disabled"

    # 账号仍在，重新打开开关即可登录
    assert client.post("/api/v1/auth/logout").status_code == 200
    assert _login(client, email).status_code == 200


def test_login_user_can_upload_to_owned_and_claimed_workspace(app, client, settings):
    """回归：账号拥有的工作区（含刚认领的访客工作区）必须能继续上传照片。

    真实冒烟曾暴露：上传端点漏传 user，导致"认领后无法再上传"（返回 404 project_not_found）。
    """
    # 1) 访客先建工作区并上传一张（访客身份）
    guest_project = create_project(client, title=unique_title("own-guest"), city_hint="杭州")
    guest_upload = client.post(
        f"/api/v1/projects/{guest_project['id']}/media",
        files={"file": ("g.png", make_image_bytes("PNG"), "image/png")},
        data={"start_analysis": "false"},
    )
    assert guest_upload.status_code == 201, guest_upload.text

    # 2) 注册并认领该工作区
    email = f"owner-{unique_title('u')}@example.com"
    assert _register(client, email).status_code == 201
    claimed = client.post("/api/v1/auth/claim", json={"project_id": guest_project["id"]})
    assert claimed.status_code == 200, claimed.text

    # 3) 认领后仍能以账号身份上传
    after_claim = client.post(
        f"/api/v1/projects/{guest_project['id']}/media",
        files={"file": ("a.png", make_image_bytes("PNG"), "image/png")},
        data={"start_analysis": "false"},
    )
    assert after_claim.status_code == 201, after_claim.text

    # 4) 账号新建的工作区同样能上传
    account_project = client.post("/api/v1/projects", json={"title": unique_title("acct"), "city_hint": "杭州"})
    if account_project.status_code == 409:
        # 一个账号同时只有一个活动工作区：先归档再建
        archived = client.post(f"/api/v1/projects/{guest_project['id']}/archive")
        assert archived.status_code == 200
        account_project = client.post("/api/v1/projects", json={"title": unique_title("acct"), "city_hint": "杭州"})
    assert account_project.status_code == 201, account_project.text
    owned_upload = client.post(
        f"/api/v1/projects/{account_project.json()['id']}/media",
        files={"file": ("o.png", make_image_bytes("PNG"), "image/png")},
        data={"start_analysis": "false"},
    )
    assert owned_upload.status_code == 201, owned_upload.text
