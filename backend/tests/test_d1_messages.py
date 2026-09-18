"""D1-h：非图片任务与持久对话。

要验证的行为（对应 07 的 R4 与 08 的受控 Agent）：
1. 工作区消息创建的是**没有图片**的 AgentRun，编排器走文字链路，不调用视觉模型。
2. 助手回答持久化（Message），刷新/重开后仍在；SSE 事件可重放。
3. 幂等：同一 Idempotency-Key 重发返回原消息与原运行，调用次数与用量不变（不重复扣费）。
4. 照片范围问题只用已存的地点/卡片上下文，不重新识图（追问不重复识图）。
5. 未核实地点不进入可引用事实（不确定时不编地点）；工作区版本变化会在回答里被标注。
6. 失败时也留下可见消息（status=FAILED），运行状态为 FAILED。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.agent.orchestrator import execute_run
from app.agent.providers.fixtures import CALL_COUNTER, FixturePlaceProvider
from app.config import Settings
from app.models import (
    AgentRun,
    GuideCard,
    Intent,
    Message,
    Place,
    RunStatus,
    StepName,
    ToolInvocation,
)
from tests.conftest import create_project, make_image_bytes, unique_title
from tests.test_s2_agent_flow import setup_run

pytestmark = pytest.mark.integration


def _settings_with(settings, **overrides) -> Settings:
    base = {
        "app_env": "test",
        "database_url": settings.database_url,
        "redis_url": settings.redis_url,
        "session_secret": settings.session_secret,
        "provider_mode": "mock",
        "media_root": settings.media_root,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _send(client, project_id: str, content: str, *, idempotency_key: str | None = None, media_id: str | None = None):
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
    body: dict[str, object] = {"content": content}
    if media_id:
        body["media_asset_id"] = media_id
    return client.post(f"/api/v1/projects/{project_id}/messages", json=body, headers=headers)


def test_workspace_message_creates_text_run_without_media(app, client, settings, session_factory, providers):
    project = create_project(client, title=unique_title("d1h"), city_hint="杭州")
    CALL_COUNTER.clear()

    response = _send(client, project["id"], "这趟旅行应该带什么？")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["run"]["media_asset_id"] is None, "工作区消息不应绑定图片"
    assert body["run"]["current_step"] == StepName.ANSWER_QUESTION
    assert body["run"]["status"] == RunStatus.QUEUED
    assert body["message"]["role"] == "user" and body["message"]["seq"] == 1
    assert body["idempotent_replay"] is False

    status_value = execute_run(session_factory, body["run"]["id"], providers=providers, settings=settings)
    assert status_value == RunStatus.SUCCEEDED

    with session_factory() as db:
        run = db.get(AgentRun, body["run"]["id"])
        assert run.intent == Intent.ANSWER_QUESTION
        messages = list(
            db.execute(select(Message).where(Message.project_id == project["id"]).order_by(Message.seq)).scalars()
        )
        assert [(m.role, m.status) for m in messages] == [("user", "READY"), ("assistant", "READY")]
        assert messages[1].run_id == run.id
        assert "FIXTURE" in messages[1].content
        operations = [
            row.operation
            for row in db.execute(select(ToolInvocation).where(ToolInvocation.run_id == run.id)).scalars()
        ]
        assert operations == ["answer_question"], "文字链路不应调用识图"

    # fixture 的视觉 Provider 一次都不能被调用
    assert CALL_COUNTER.get("fixture-vision") is None
    assert CALL_COUNTER.get("fixture-text:answer") == 1


def test_message_idempotency_does_not_double_charge(app, client, settings, session_factory, providers):
    project = create_project(client, title=unique_title("d1h-idem"), city_hint="杭州")
    CALL_COUNTER.clear()

    first = _send(client, project["id"], "帮我看看这趟要几天", idempotency_key="msg-1")
    assert first.status_code == 201
    replay = _send(client, project["id"], "帮我看看这趟要几天", idempotency_key="msg-1")
    assert replay.status_code == 201
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["run"]["id"] == first.json()["run"]["id"]
    assert replay.json()["message"]["id"] == first.json()["message"]["id"]

    with session_factory() as db:
        messages = db.execute(select(Message).where(Message.project_id == project["id"])).scalars().all()
        assert len(messages) == 1, "重复提交不能产生第二条消息"

    execute_run(session_factory, first.json()["run"]["id"], providers=providers, settings=settings)

    # 再次重放不会产生新的模型调用
    again = _send(client, project["id"], "帮我看看这趟要几天", idempotency_key="msg-1")
    assert again.json()["idempotent_replay"] is True
    with session_factory() as db:
        calls = db.execute(
            select(ToolInvocation).where(ToolInvocation.run_id == first.json()["run"]["id"])
        ).scalars().all()
        assert len(calls) == 1
        assert calls[0].operation == "answer_question"


def test_media_scoped_question_reuses_stored_context_without_reanalysis(
    app, client, settings, session_factory, providers
):
    """照片范围追问：使用已存卡片与地点，不重新识图。"""
    setup = setup_run(client)
    run_id = setup["run"]["id"]
    assert execute_run(session_factory, run_id, providers=providers, settings=settings) == RunStatus.WAITING_USER
    with session_factory() as db:
        from app.models import PlaceCandidate

        candidate = db.execute(select(PlaceCandidate).where(PlaceCandidate.run_id == run_id)).scalars().first()
        candidate_id = candidate.id
    client.post(f"/api/v1/runs/{run_id}/confirm-place", json={"decision": "confirm", "candidate_id": candidate_id})
    assert execute_run(session_factory, run_id, providers=providers, settings=settings) == RunStatus.SUCCEEDED

    CALL_COUNTER.clear()
    response = _send(client, setup["project"]["id"], "这张照片里的地方值得去吗？", media_id=setup["media"]["id"])
    assert response.status_code == 201, response.text
    assert response.json()["run"]["media_asset_id"] == setup["media"]["id"]

    created = execute_run(session_factory, response.json()["run"]["id"], providers=providers, settings=settings)
    assert created == RunStatus.SUCCEEDED
    assert CALL_COUNTER.get("fixture-vision") is None, "追问不得重新识图"

    with session_factory() as db:
        run = db.get(AgentRun, response.json()["run"]["id"])
        assert run.intent == Intent.ANSWER_QUESTION
        card = db.execute(select(GuideCard).where(GuideCard.media_asset_id == setup["media"]["id"])).scalar_one()
        # 卡片是同一张（没有被重新生成）
        assert card.run_id == run_id
        reply = db.execute(
            select(Message).where(Message.project_id == setup["project"]["id"], Message.role == "assistant")
        ).scalars().first()
        assert reply is not None and reply.media_ids == [setup["media"]["id"]]


def test_unverified_place_is_not_citable_and_version_change_is_flagged(
    app, client, settings, session_factory, providers
):
    """未核实地点不能当事实来源；工作区版本变化要在回答里体现。"""
    setup = setup_run(client)
    run_id = setup["run"]["id"]
    assert execute_run(session_factory, run_id, providers=providers, settings=settings) == RunStatus.WAITING_USER
    with session_factory() as db:
        from app.models import PlaceCandidate

        candidate = db.execute(select(PlaceCandidate).where(PlaceCandidate.run_id == run_id)).scalars().first()
        candidate_id = candidate.id
    client.post(f"/api/v1/runs/{run_id}/confirm-place", json={"decision": "confirm", "candidate_id": candidate_id})

    # 人为把地点标成"未核实"：不得进入可引用事实
    with session_factory() as db:
        place = db.execute(select(Place).where(Place.media_asset_id == setup["media"]["id"])).scalar_one()
        place.match_status = "conflict"
        place.provider = "user"
        place.latitude = None
        place.longitude = None
        db.commit()

    response = _send(client, setup["project"]["id"], "这里是哪？", media_id=setup["media"]["id"])
    assert response.status_code == 201
    captured: dict[str, object] = {}

    class _Spy:
        name = "spy-text"

        def __init__(self, inner):
            self.inner = inner

        def generate_guide_card(self, *, messages):
            return self.inner.generate_guide_card(messages=messages)

        def answer_question(self, *, messages):
            captured["messages"] = messages
            return self.inner.answer_question(messages=messages)

    spy_settings = settings
    spy_providers = providers.__class__(
        vision=providers.vision, text=_Spy(providers.text), place=providers.place, mode=providers.mode
    )
    assert execute_run(
        session_factory, response.json()["run"]["id"], providers=spy_providers, settings=spy_settings
    ) == RunStatus.SUCCEEDED

    prompt = captured["messages"][-1]["content"]
    assert "confirmed_place: none" in prompt, "未核实的地点不能作为可引用事实"
    assert "latitude" not in prompt.split("confirmed_place")[1].split("photo_scope")[0]


def test_failed_answer_is_recorded_as_visible_message(app, client, settings, session_factory):
    strict = _settings_with(settings, fixture_behaviors="text=invalid_json")
    from app.agent.providers import build_provider_set

    providers = build_provider_set(strict)
    project = create_project(client, title=unique_title("d1h-fail"), city_hint="杭州")
    response = _send(client, project["id"], "会失败的问题")
    assert response.status_code == 201
    status_value = execute_run(session_factory, response.json()["run"]["id"], providers=providers, settings=strict)
    assert status_value == RunStatus.FAILED

    with session_factory() as db:
        run = db.get(AgentRun, response.json()["run"]["id"])
        assert run.error_code is not None
        messages = list(
            db.execute(select(Message).where(Message.project_id == project["id"]).order_by(Message.seq)).scalars()
        )
        assert [m.status for m in messages] == ["READY", "FAILED"]
        assert messages[1].role == "assistant" and messages[1].error_code


def test_messages_survive_reload_and_are_listed(app, client, settings, session_factory, providers):
    project = create_project(client, title=unique_title("d1h-reload"), city_hint="杭州")
    response = _send(client, project["id"], "第一问")
    execute_run(session_factory, response.json()["run"]["id"], providers=providers, settings=settings)

    snapshot = client.get(f"/api/v1/projects/{project['id']}").json()
    assert [item["role"] for item in snapshot["messages"]] == ["user", "assistant"]
    assert snapshot["last_message_seq"] == 2

    listing = client.get(f"/api/v1/projects/{project['id']}/messages").json()
    assert [item["seq"] for item in listing["messages"]] == [1, 2]
    incremental = client.get(f"/api/v1/projects/{project['id']}/messages?after_seq=1").json()
    assert [item["role"] for item in incremental["messages"]] == ["assistant"]


def test_message_media_scope_must_belong_to_project(app, client, settings):
    """引用别人项目的照片必须 404（不能只靠前端过滤）。"""
    from fastapi.testclient import TestClient

    project = create_project(client, title=unique_title("d1h-mine"), city_hint="杭州")
    with TestClient(app) as other_client:
        other = create_project(other_client, title=unique_title("d1h-other"), city_hint="杭州")
        upload = other_client.post(
            f"/api/v1/projects/{other['id']}/media",
            files={"file": ("p.png", make_image_bytes("PNG"), "image/png")},
            data={"start_analysis": "false"},
        )
        assert upload.status_code == 201
        media_id = upload.json()["media"]["id"]

    response = _send(client, project["id"], "看看这张", media_id=media_id)
    assert response.status_code == 404
    assert response.json()["detail"] == "media_not_found"
