"""S2：Redis 队列、受控 Agent 状态机、三个工具、WAITING_USER、取消、恢复与幂等。

自动回归使用 fixture Provider（输出带 [FIXTURE] 标记）；真实 Provider smoke 见 S4 证据。
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.agent.orchestrator import execute_run
from app.agent.providers import build_provider_set
from app.config import Settings
from app.models import (
    AgentRun,
    GuideCard,
    MediaAsset,
    Place,
    PlaceCandidate,
    RunStatus,
    RunStep,
    StepName,
    ToolInvocation,
    UsageLedger,
    WorkspaceEvent,
)
from tests.conftest import create_project, make_image_bytes, unique_title

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


def setup_run(client, *, start_analysis: bool = True, idempotency_key: str | None = None) -> dict:
    project = create_project(client, title=unique_title("agent"))
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
    response = client.post(
        f"/api/v1/projects/{project['id']}/media",
        files={"file": ("photo.png", make_image_bytes("PNG"), "image/png")},
        data={"start_analysis": "true" if start_analysis else "false"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return {"project": project, "media": body["media"], "run": body["run"]}


def test_upload_creates_queued_run_and_enqueues_message(client, settings, db):
    """POST 上传返回 201 + run(QUEUED)，并把消息投递到真实 Redis 队列。"""
    import redis

    setup = setup_run(client)
    assert setup["run"] is not None
    assert setup["run"]["status"] == RunStatus.QUEUED

    status = client.get(f"/api/v1/runs/{setup['run']['id']}")
    assert status.status_code == 200
    assert status.json()["status"] == RunStatus.QUEUED
    assert status.json()["current_step"] == StepName.ANALYZE_IMAGE

    client_redis = redis.Redis.from_url(settings.redis_url)
    depth = client_redis.llen(f"dramatiq:{settings.redis_queue_name}")
    client_redis.close()
    assert depth >= 1, "任务必须真实进入 Redis 队列，而不是留在 API 进程内"


def test_analyze_image_moves_run_to_waiting_user(app, client, settings, providers, session_factory):
    setup = setup_run(client)
    run_id = setup["run"]["id"]

    status = execute_run(session_factory, run_id, providers=providers, settings=settings)
    assert status == RunStatus.WAITING_USER

    with app.state.session_factory() as db:
        run = db.get(AgentRun, run_id)
        media = db.get(MediaAsset, setup["media"]["id"])
        assert run.status == RunStatus.WAITING_USER
        assert run.current_step == StepName.WAIT_FOR_PLACE_CONFIRMATION
        assert media.status == "WAITING_USER"

        candidates = db.execute(select(PlaceCandidate).where(PlaceCandidate.run_id == run_id)).scalars().all()
        assert len(candidates) == 1
        assert candidates[0].rationale  # 视觉依据必须落库
        assert "[FIXTURE]" in candidates[0].name or "[FIXTURE]" in candidates[0].rationale

        steps = {step.name: step for step in db.execute(select(RunStep).where(RunStep.run_id == run_id)).scalars()}
        assert steps[StepName.ANALYZE_IMAGE].status == "SUCCEEDED"
        assert steps[StepName.WAIT_FOR_PLACE_CONFIRMATION].status == "WAITING"

        invocations = db.execute(select(ToolInvocation).where(ToolInvocation.run_id == run_id)).scalars().all()
        assert len(invocations) == 1
        assert invocations[0].operation == "analyze_image"
        assert invocations[0].status == "SUCCEEDED"
        assert invocations[0].duration_ms >= 0

        event_types = [
            row.type
            for row in db.execute(
                select(WorkspaceEvent).where(WorkspaceEvent.run_id == run_id).order_by(WorkspaceEvent.id)
            ).scalars()
        ]
        assert "run.queued" in event_types
        assert "run.started" in event_types  # 认领成功即发送
        assert "step.started" in event_types
        assert "step.finished" in event_types  # analyze_image 完成后发送
        assert "candidate.ready" in event_types
        assert "run.waiting_user" in event_types


def test_confirm_place_resumes_same_run_and_builds_card(app, client, settings, providers, session_factory):
    setup = setup_run(client)
    run_id = setup["run"]["id"]
    assert execute_run(session_factory, run_id, providers=providers, settings=settings) == RunStatus.WAITING_USER

    with app.state.session_factory() as db:
        candidate = db.execute(select(PlaceCandidate).where(PlaceCandidate.run_id == run_id)).scalar_one()
        candidate_id = candidate.id

    confirm = client.post(
        f"/api/v1/runs/{run_id}/confirm-place",
        json={"decision": "confirm", "candidate_id": candidate_id},
    )
    assert confirm.status_code == 200, confirm.text
    assert confirm.json()["status"] == RunStatus.QUEUED
    assert confirm.json()["id"] == run_id  # 同一个 AgentRun 恢复执行
    assert confirm.json()["current_step"] == StepName.LOOKUP_PLACE

    assert execute_run(session_factory, run_id, providers=providers, settings=settings) == RunStatus.SUCCEEDED

    with app.state.session_factory() as db:
        run = db.get(AgentRun, run_id)
        assert run.status == RunStatus.SUCCEEDED
        assert run.finished_at is not None

        card = db.execute(select(GuideCard).where(GuideCard.media_asset_id == setup["media"]["id"])).scalar_one()
        assert card.title.startswith("[FIXTURE]")
        assert card.sections
        assert card.partial is False
        # 供应商事实与模型讲解分开保存。
        assert card.place_facts is not None
        assert card.place_facts["provider"] == "fixture-amap"
        assert "fixture" in (card.place_facts.get("payload") or {})

        place = db.execute(select(Place).where(Place.media_asset_id == setup["media"]["id"])).scalar_one()
        assert place.confirmed_by == "candidate"
        assert place.provider == "fixture-amap"
        assert place.query_at is not None
        assert place.latitude is not None and place.longitude is not None

        steps = {step.name: step.status for step in db.execute(select(RunStep).where(RunStep.run_id == run_id)).scalars()}
        assert steps == {
            StepName.ANALYZE_IMAGE: "SUCCEEDED",
            StepName.WAIT_FOR_PLACE_CONFIRMATION: "SUCCEEDED",
            StepName.LOOKUP_PLACE: "SUCCEEDED",
            StepName.GENERATE_GUIDE_CARD: "SUCCEEDED",
        }

        operations = [
            row.operation
            for row in db.execute(
                select(ToolInvocation).where(ToolInvocation.run_id == run_id).order_by(ToolInvocation.id)
            ).scalars()
        ]
        assert operations == ["analyze_image", "lookup_place", "generate_guide_card"]

        event_types = [
            row.type
            for row in db.execute(
                select(WorkspaceEvent).where(WorkspaceEvent.run_id == run_id).order_by(WorkspaceEvent.id)
            ).scalars()
        ]
        assert "place.confirmed" in event_types
        assert "card.ready" in event_types

    snapshot = client.get(f"/api/v1/projects/{setup['project']['id']}").json()
    media_snapshot = snapshot["media"][0]
    assert media_snapshot["card"]["title"].startswith("[FIXTURE]")
    assert media_snapshot["place"]["provider"] == "fixture-amap"
    assert media_snapshot["media"]["status"] == "CONFIRMED"


def test_user_corrected_place_is_saved_as_user_input(app, client, settings, providers, session_factory):
    setup = setup_run(client)
    run_id = setup["run"]["id"]
    execute_run(session_factory, run_id, providers=providers, settings=settings)

    corrected = client.post(
        f"/api/v1/runs/{run_id}/confirm-place",
        json={"decision": "correct", "name": "用户填写的地点", "region": "杭州"},
    )
    assert corrected.status_code == 200
    assert execute_run(session_factory, run_id, providers=providers, settings=settings) == RunStatus.SUCCEEDED

    with app.state.session_factory() as db:
        place = db.execute(select(Place).where(Place.media_asset_id == setup["media"]["id"])).scalar_one()
        assert place.name == "用户填写的地点"
        assert place.confirmed_by == "user_input"
        # 地点 Provider 用纠正后的名称查询，供应商事实仍然单独保存。
        assert place.provider == "fixture-amap"

        candidates = db.execute(select(PlaceCandidate).where(PlaceCandidate.run_id == run_id)).scalars().all()
        assert all(candidate.status == "REJECTED" for candidate in candidates)


def test_reject_place_cancels_run(app, client, settings, providers, session_factory):
    setup = setup_run(client)
    run_id = setup["run"]["id"]
    execute_run(session_factory, run_id, providers=providers, settings=settings)

    rejected = client.post(f"/api/v1/runs/{run_id}/confirm-place", json={"decision": "reject"})
    assert rejected.status_code == 200
    assert rejected.json()["status"] == RunStatus.CANCELLED
    assert rejected.json()["error_code"] == "place_rejected"

    with app.state.session_factory() as db:
        media = db.get(MediaAsset, setup["media"]["id"])
        assert media.status == "REJECTED"
        assert db.execute(select(GuideCard).where(GuideCard.media_asset_id == media.id)).scalar_one_or_none() is None


def test_cancel_before_worker_runs_prevents_tool_calls(app, client, settings, providers, session_factory):
    setup = setup_run(client)
    run_id = setup["run"]["id"]

    cancelled = client.post(f"/api/v1/runs/{run_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == RunStatus.CANCELLED

    status = execute_run(session_factory, run_id, providers=providers, settings=settings)
    assert status == RunStatus.CANCELLED

    with app.state.session_factory() as db:
        assert db.execute(select(ToolInvocation).where(ToolInvocation.run_id == run_id)).scalars().all() == []
        assert db.execute(select(RunStep).where(RunStep.run_id == run_id)).scalars().all() == []
        events = [row.type for row in db.execute(select(WorkspaceEvent).where(WorkspaceEvent.run_id == run_id)).scalars()]
        assert "run.cancelled" in events


def test_cancel_after_finish_is_conflict(client):
    setup = setup_run(client, start_analysis=False)
    assert setup["run"] is None
    project_id = setup["project"]["id"]
    response = client.post(
        f"/api/v1/projects/{project_id}/media",
        files={"file": ("b.png", make_image_bytes("PNG"), "image/png")},
        data={"start_analysis": "true"},
    )
    assert response.status_code == 201
    run_id = response.json()["run"]["id"]
    assert client.post(f"/api/v1/runs/{run_id}/cancel").status_code == 200
    second = client.post(f"/api/v1/runs/{run_id}/cancel")
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "run_already_finished"


def test_duplicate_worker_delivery_runs_tools_once(app, client, settings, providers, session_factory):
    """两个 Worker 同时拿到同一个 run_id 时，只有一个能执行工具。"""
    setup = setup_run(client)
    run_id = setup["run"]["id"]

    results: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        outcome = execute_run(session_factory, run_id, providers=providers, settings=settings)
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    # 只有一个 Worker 能认领成功；另一个要么没抢到租约，要么看到已经进入等待确认。
    assert len(results) == 2
    assert all(result in {"not_claimed", RunStatus.WAITING_USER} for result in results)
    assert RunStatus.WAITING_USER in results
    with app.state.session_factory() as db:
        invocations = db.execute(select(ToolInvocation).where(ToolInvocation.run_id == run_id)).scalars().all()
        assert len(invocations) == 1

        # 确认后再次推进：卡片只会有一张，用量账目只记一次。
        candidate = db.execute(select(PlaceCandidate).where(PlaceCandidate.run_id == run_id)).scalar_one()
    client.post(f"/api/v1/runs/{run_id}/confirm-place", json={"decision": "confirm", "candidate_id": candidate.id})

    assert execute_run(session_factory, run_id, providers=providers, settings=settings) == RunStatus.SUCCEEDED
    assert execute_run(session_factory, run_id, providers=providers, settings=settings) == RunStatus.SUCCEEDED

    with app.state.session_factory() as db:
        cards = db.execute(select(GuideCard).where(GuideCard.media_asset_id == setup["media"]["id"])).scalars().all()
        assert len(cards) == 1
        usage = db.execute(select(func.count(UsageLedger.id)).where(UsageLedger.run_id == run_id)).scalar_one()
        assert usage == 3  # analyze + lookup + card，各一次
        invocations = db.execute(select(func.count(ToolInvocation.id)).where(ToolInvocation.run_id == run_id)).scalar_one()
        assert invocations == 3


def test_invalid_model_json_fails_run_without_partial_data(app, client, settings, session_factory):
    strict = _settings_with(settings, fixture_behaviors="vision=invalid_json")
    bad_providers = build_provider_set(strict)

    setup = setup_run(client)
    run_id = setup["run"]["id"]

    status = execute_run(session_factory, run_id, providers=bad_providers, settings=settings)
    assert status == RunStatus.FAILED

    with app.state.session_factory() as db:
        run = db.get(AgentRun, run_id)
        assert run.error_code == "provider_invalid_response"
        assert db.execute(select(PlaceCandidate).where(PlaceCandidate.run_id == run_id)).scalars().all() == []
        assert db.execute(select(GuideCard).where(GuideCard.media_asset_id == setup["media"]["id"])).scalar_one_or_none() is None
        assert db.get(MediaAsset, setup["media"]["id"]).status == "FAILED"

        step = db.execute(
            select(RunStep).where(RunStep.run_id == run_id, RunStep.name == StepName.ANALYZE_IMAGE)
        ).scalar_one()
        assert step.status == "FAILED"
        assert step.attempt == settings.run_step_max_attempts  # 有限重试后明确失败

        event_types = [row.type for row in db.execute(select(WorkspaceEvent).where(WorkspaceEvent.run_id == run_id)).scalars()]
        assert "run.failed" in event_types


def test_place_provider_timeout_yields_partial_card_without_fake_facts(app, client, settings, session_factory):
    strict = _settings_with(settings, fixture_behaviors="place=timeout")
    flaky_providers = build_provider_set(strict)

    setup = setup_run(client)
    run_id = setup["run"]["id"]
    execute_run(session_factory, run_id, providers=flaky_providers, settings=settings)

    with app.state.session_factory() as db:
        candidate = db.execute(select(PlaceCandidate).where(PlaceCandidate.run_id == run_id)).scalar_one()
    client.post(f"/api/v1/runs/{run_id}/confirm-place", json={"decision": "confirm", "candidate_id": candidate.id})

    status = execute_run(session_factory, run_id, providers=flaky_providers, settings=settings)
    assert status == RunStatus.PARTIAL

    with app.state.session_factory() as db:
        run = db.get(AgentRun, run_id)
        assert run.error_code == "place_lookup_failed"
        card = db.execute(select(GuideCard).where(GuideCard.media_asset_id == setup["media"]["id"])).scalar_one()
        assert card.partial is True
        assert card.place_facts is None  # 不伪造供应商事实
        assert card.title.startswith("[FIXTURE]")

        place = db.execute(select(Place).where(Place.media_asset_id == setup["media"]["id"])).scalar_one()
        assert place.provider == "user"
        assert place.address is None and place.latitude is None

        lookup_step = db.execute(
            select(RunStep).where(RunStep.run_id == run_id, RunStep.name == StepName.LOOKUP_PLACE)
        ).scalar_one()
        assert lookup_step.status == "FAILED"
        assert lookup_step.error_code == "provider_timeout"
        assert lookup_step.attempt == settings.run_step_max_attempts


def test_budget_limit_stops_run(app, settings, session_factory):
    strict = _settings_with(settings, run_max_tool_calls=1, demo_session_run_limit=10, demo_daily_run_limit=100)
    from app.main import create_app

    strict_app = create_app(strict)
    with TestClient(strict_app) as client:
        setup = setup_run(client)
        run_id = setup["run"]["id"]
        strict_providers = build_provider_set(strict)
        assert execute_run(session_factory, run_id, providers=strict_providers, settings=strict) == RunStatus.WAITING_USER

        with strict_app.state.session_factory() as db:
            candidate = db.execute(select(PlaceCandidate).where(PlaceCandidate.run_id == run_id)).scalar_one()
        client.post(f"/api/v1/runs/{run_id}/confirm-place", json={"decision": "confirm", "candidate_id": candidate.id})

        status = execute_run(session_factory, run_id, providers=strict_providers, settings=strict)
        assert status == RunStatus.FAILED
        with strict_app.state.session_factory() as db:
            run = db.get(AgentRun, run_id)
            assert run.error_code == "budget_tool_calls_exceeded"
            invocations = db.execute(select(ToolInvocation).where(ToolInvocation.run_id == run_id)).scalars().all()
            assert len(invocations) == 1  # 预算耗尽后不再发起新的 Provider 调用


def test_retry_creates_new_run_idempotently(app, client, settings, session_factory):
    strict = _settings_with(settings, fixture_behaviors="vision=http_error")
    failing = build_provider_set(strict)

    setup = setup_run(client)
    run_id = setup["run"]["id"]
    assert execute_run(session_factory, run_id, providers=failing, settings=settings) == RunStatus.FAILED

    first = client.post(f"/api/v1/runs/{run_id}/retry")
    assert first.status_code == 202, first.text
    assert first.json()["idempotent_replay"] is False
    new_run_id = first.json()["run_id"]
    assert new_run_id != run_id

    with app.state.session_factory() as db:
        retry_events = [
            row.type
            for row in db.execute(
                select(WorkspaceEvent).where(WorkspaceEvent.run_id == new_run_id).order_by(WorkspaceEvent.id)
            ).scalars()
        ]
    assert "run.retry_queued" in retry_events

    replay = client.post(f"/api/v1/runs/{run_id}/retry")
    assert replay.status_code == 202
    assert replay.json()["run_id"] == new_run_id
    assert replay.json()["idempotent_replay"] is True


def test_confirm_requires_waiting_user_state(client):
    setup = setup_run(client)
    run_id = setup["run"]["id"]
    response = client.post(f"/api/v1/runs/{run_id}/confirm-place", json={"decision": "confirm", "name": "x"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "run_not_waiting_user"


def test_retry_after_cancel_creates_new_run(app, client, settings, session_factory):
    """B5：取消的运行必须能重做；旧运行保持 CANCELLED，新运行带 source_run_id。"""
    setup = setup_run(client)
    run_id = setup["run"]["id"]
    cancelled = client.post(f"/api/v1/runs/{run_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == RunStatus.CANCELLED

    retried = client.post(f"/api/v1/runs/{run_id}/retry")
    assert retried.status_code == 202, retried.text
    new_run_id = retried.json()["run_id"]
    assert new_run_id != run_id

    with app.state.session_factory() as db:
        old = db.get(AgentRun, run_id)
        new = db.get(AgentRun, new_run_id)
        assert old.status == RunStatus.CANCELLED
        assert new.source_run_id == run_id
        assert new.status == RunStatus.QUEUED


def test_retry_after_media_deleted_is_rejected(app, client, settings, session_factory):
    setup = setup_run(client)
    run_id = setup["run"]["id"]
    client.delete(f"/api/v1/media/{setup['media']['id']}")
    response = client.post(f"/api/v1/runs/{run_id}/retry")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "media_deleted"
