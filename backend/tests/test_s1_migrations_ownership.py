"""S1：真实 MySQL 迁移、表结构、匿名会话所有权。"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from tests.conftest import create_project, make_image_bytes, unique_title

BACKEND_DIR = Path(__file__).resolve().parents[1]

EXPECTED_TABLES = {
    "demo_sessions",
    "guide_projects",
    "media_assets",
    "place_candidates",
    "places",
    "agent_runs",
    "run_steps",
    "tool_invocations",
    "guide_cards",
    "workspace_events",
    "usage_ledger",
    "alembic_version",
}


def test_alembic_head_tables_exist(engine):
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE()")
        ).all()
    tables = {row[0] for row in rows}
    assert EXPECTED_TABLES <= tables


def test_alembic_version_matches_script_head(engine):
    from alembic.config import Config as AlembicConfig
    from alembic.script import ScriptDirectory

    config = AlembicConfig(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    head = ScriptDirectory.from_config(config).get_current_head()

    with engine.connect() as conn:
        current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert current == head


def test_unique_constraints_enforced(engine):
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT constraint_name FROM information_schema.table_constraints "
                "WHERE table_schema = DATABASE() AND constraint_type = 'UNIQUE'"
            )
        ).all()
    names = {row[0] for row in rows}
    assert {
        "uq_runs_project_idempotency",
        "uq_cards_media_asset",
        "uq_places_media_asset",
        "uq_media_project_position",
        "uq_projects_active_owner",
    } <= names


def test_ready_endpoint_reports_mysql_redis_and_migration(client):
    response = client.get("/api/health/ready")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["mysql"] == "up"
    assert body["checks"]["redis"] == "up"
    assert body["checks"]["migration_current"] == body["checks"]["migration_head"]


def test_session_cookie_is_signed_and_httponly(client):
    response = client.get("/api/v1/session")
    assert response.status_code == 200
    set_cookie = response.headers.get("set-cookie", "")
    assert "ai_guide_session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie.lower().replace("samesite=lax", "SameSite=lax")
    # 生产环境默认 Secure；测试环境未强制，这里只断言签名 Cookie 存在且不是明文会话 ID。
    body = response.json()
    assert body["id"] not in set_cookie


def test_project_creation_and_current_snapshot(client):
    created = create_project(client, title=unique_title("snapshot"))
    assert created["media_count"] == 0
    assert created["status"] == "active"

    current = client.get("/api/v1/projects/current")
    assert current.status_code == 200
    snapshot = current.json()
    assert snapshot["project"]["id"] == created["id"]
    assert snapshot["media"] == []
    assert snapshot["last_event_id"] >= 1  # project.created 已持久化

    second = client.post("/api/v1/projects", json={"title": unique_title("dup"), "city_hint": None})
    assert second.status_code == 201
    assert second.json()["id"] != created["id"]
    assert client.get(f"/api/v1/projects/{created['id']}").status_code == 200


def test_project_snapshot_404_for_other_session(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as owner:
        project = create_project(owner, title=unique_title("owned"))
        media = owner.post(
            f"/api/v1/projects/{project['id']}/media",
            files={"file": ("a.png", make_image_bytes("PNG"), "image/png")},
            data={"start_analysis": "false"},
        )
        assert media.status_code == 201
        media_id = media.json()["media"]["id"]

        owner_read = owner.get(f"/api/v1/projects/{project['id']}")
        assert owner_read.status_code == 200
        assert owner.get(f"/api/v1/media/{media_id}/content").status_code == 200

    # 另一个匿名会话（不同 Cookie）不能读取或推断该项目是否存在。
    with TestClient(app) as intruder:
        # 先取得自己的匿名会话（真实客户端同样先调用 /session）。
        assert intruder.get("/api/v1/session").status_code == 200
        assert intruder.get(f"/api/v1/projects/{project['id']}").status_code == 404
        assert intruder.get(f"/api/v1/media/{media_id}/content").status_code == 404
        assert intruder.get("/api/v1/projects/current").status_code == 404
        assert intruder.get(f"/api/v1/projects/{project['id']}/events").status_code == 404

    # 完全没有会话的客户端访问 SSE 时明确返回 401，而不是建立新会话。
    with TestClient(app) as anonymous:
        assert anonymous.get(f"/api/v1/projects/{project['id']}/events").status_code == 401


def test_project_archive_allows_new_active_project(client):
    first = create_project(client, title=unique_title("first"))
    archived = client.post(f"/api/v1/projects/{first['id']}/archive")
    assert archived.status_code == 200
    assert archived.json()["status"] == "archived"

    second = create_project(client, title=unique_title("second"))
    current = client.get("/api/v1/projects/current").json()
    assert current["project"]["id"] == second["id"]


def test_run_quota_is_enforced(app, settings):
    from fastapi.testclient import TestClient

    from app.config import Settings

    strict = Settings(
        _env_file=None,
        app_env="test",
        database_url=settings.database_url,
        redis_url=settings.redis_url,
        session_secret=settings.session_secret,
        provider_mode="mock",
        media_root=settings.media_root,
        demo_session_run_limit=1,
        demo_daily_run_limit=100,
    )

    from app.main import create_app

    strict_app = create_app(strict)
    with TestClient(strict_app) as strict_client:
        project = create_project(strict_client, title=unique_title("quota"))
        first = strict_client.post(
            f"/api/v1/projects/{project['id']}/media",
            files={"file": ("a.png", make_image_bytes("PNG"), "image/png")},
            data={"start_analysis": "true"},
        )
        assert first.status_code == 201
        second = strict_client.post(
            f"/api/v1/projects/{project['id']}/media",
            files={"file": ("b.png", make_image_bytes("PNG"), "image/png")},
            data={"start_analysis": "true"},
        )
        # 会话额度用尽：直接 429，不写文件、不留媒体记录。
        assert second.status_code == 429
        assert second.json()["detail"]["code"] == "quota_exceeded"

        snapshot = strict_client.get(f"/api/v1/projects/{project['id']}").json()
        assert len(snapshot["media"]) == 1

        # 关闭自动分析时不受运行额度限制，仍可保存媒体。
        third = strict_client.post(
            f"/api/v1/projects/{project['id']}/media",
            files={"file": ("c.png", make_image_bytes("PNG"), "image/png")},
            data={"start_analysis": "false"},
        )
        assert third.status_code == 201
        assert third.json()["run"] is None
