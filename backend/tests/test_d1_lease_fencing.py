"""D1 / B2：过期持有者不能写回当前成果（lease fencing）。

07 号报告的复现路径：阻塞旧 Worker → 让租约过期 → 新 Worker 接管并推进到 WAITING_USER
→ 旧 Worker 返回后仍然写入了第二次调用、用量与候选处理。修正后：
- 旧持有者的**调用事实**仍然记账（钱确实花了，必须可解释）；
- 但候选、卡片、运行/照片状态、业务事件一律丢弃，运行保持接管者的结果。
"""

from __future__ import annotations

import threading
import time
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.agent.orchestrator import execute_run
from app.agent.providers import build_provider_set
from app.config import Settings
from app.models import (
    AgentRun,
    PlaceCandidate,
    RunStatus,
    RunStep,
    StepName,
    ToolInvocation,
    UsageLedger,
    utcnow,
)
from tests.conftest import create_project, make_image_bytes, unique_title

pytestmark = pytest.mark.integration


def _settings_with(settings: Settings, **overrides) -> Settings:
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


def _upload(client, project_id: str) -> dict:
    response = client.post(
        f"/api/v1/projects/{project_id}/media",
        files={"file": ("fence.png", make_image_bytes("PNG"), "image/png")},
        data={"start_analysis": "true"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_stale_worker_cannot_overwrite_after_takeover(app, client, settings, session_factory):
    """旧 Worker 阻塞 3 秒；期间租约过期、新 Worker 接管并进入 WAITING_USER。"""
    slow = _settings_with(settings, fixture_behaviors="vision=slow:3", provider_timeout_seconds=30)
    fast = _settings_with(settings)
    slow_providers = build_provider_set(slow)
    fast_providers = build_provider_set(fast)

    project = create_project(client, title=unique_title("fence"))
    body = _upload(client, project["id"])
    run_id = body["run"]["id"]

    results: dict[str, str] = {}

    def old_worker() -> None:
        results["old"] = execute_run(session_factory, run_id, providers=slow_providers, settings=slow, owner="old-worker")

    thread = threading.Thread(target=old_worker, daemon=True)
    thread.start()

    # 等旧 Worker 认领并进入 RUNNING
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        with app.state.session_factory() as db:
            run = db.get(AgentRun, run_id)
            steps = db.execute(select(RunStep).where(RunStep.run_id == run_id)).scalars().all()
            if run.status == RunStatus.RUNNING and steps:
                old_epoch = run.lease_epoch
                break
        time.sleep(0.1)
    else:  # pragma: no cover
        pytest.fail("旧 Worker 未在预期时间内认领")

    # 强制租约过期：模拟 Worker 卡住超过租约
    with app.state.session_factory() as db:
        run = db.get(AgentRun, run_id)
        run.lease_expires_at = utcnow() - timedelta(seconds=5)
        db.commit()

    # 新 Worker 接管（旧 Worker 仍在 slow provider 里阻塞）
    takeover = execute_run(session_factory, run_id, providers=fast_providers, settings=fast, owner="new-worker")
    assert takeover == RunStatus.WAITING_USER

    with app.state.session_factory() as db:
        run = db.get(AgentRun, run_id)
        new_epoch = run.lease_epoch
        assert new_epoch > old_epoch, "接管必须递增租约代次"
        candidates_after_takeover = db.execute(
            select(func.count(PlaceCandidate.id)).where(PlaceCandidate.run_id == run_id)
        ).scalar_one()

    # 旧 Worker 此刻才返回（3 秒阻塞结束）
    thread.join(timeout=30)
    assert results.get("old") == "stale_discarded", results

    with app.state.session_factory() as db:
        run = db.get(AgentRun, run_id)
        # 成果保持不变：仍是接管者的 WAITING_USER，候选没有被旧 Worker 再写一遍
        assert run.status == RunStatus.WAITING_USER
        assert run.lease_owner in (None, "new-worker")
        candidates_now = db.execute(
            select(func.count(PlaceCandidate.id)).where(PlaceCandidate.run_id == run_id)
        ).scalar_one()
        assert candidates_now == candidates_after_takeover

        # 调用事实仍然留痕：旧 Worker 的调用必须有 ToolInvocation 或用量记录可解释
        invocations = db.execute(
            select(ToolInvocation).where(ToolInvocation.run_id == run_id)
        ).scalars().all()
        assert len(invocations) >= 2, "旧持有者的调用也必须留下审计记录"
        usage_rows = db.execute(select(func.count(UsageLedger.id)).where(UsageLedger.run_id == run_id)).scalar_one()
        assert usage_rows >= 2


def test_cancelled_run_discards_late_worker_result(app, client, settings, session_factory):
    """运行被取消后，晚到的 Worker 结果不得把状态改回 RUNNING/WAITING_USER。"""
    slow = _settings_with(settings, fixture_behaviors="vision=slow:3", provider_timeout_seconds=30)
    slow_providers = build_provider_set(slow)

    project = create_project(client, title=unique_title("fence-cancel"))
    body = _upload(client, project["id"])
    run_id = body["run"]["id"]

    results: dict[str, str] = {}

    def worker() -> None:
        results["worker"] = execute_run(session_factory, run_id, providers=slow_providers, settings=slow, owner="slow-worker")

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        with app.state.session_factory() as db:
            run = db.get(AgentRun, run_id)
            if run.status == RunStatus.RUNNING:
                break
        time.sleep(0.1)

    cancelled = client.post(f"/api/v1/runs/{run_id}/cancel")
    assert cancelled.status_code == 200
    thread.join(timeout=30)

    with app.state.session_factory() as db:
        run = db.get(AgentRun, run_id)
        assert run.status == RunStatus.CANCELLED
        step = db.execute(
            select(RunStep).where(RunStep.run_id == run_id, RunStep.name == StepName.ANALYZE_IMAGE)
        ).scalar_one_or_none()
        assert step is None or step.status != "SUCCEEDED"
