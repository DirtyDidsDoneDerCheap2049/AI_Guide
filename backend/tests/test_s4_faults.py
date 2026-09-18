"""S4：故障与恢复测试。

覆盖：
1. Redis 不可达时 readiness 明确报 down（真实 TCP 失败）。
2. 投递失败（Redis 不可用）时 AgentRun 留在 QUEUED，恢复入口可以重新投递。
3. Worker 中止后租约过期的 RUNNING 可以被恢复并继续跑完。
4. 真实子进程：Worker 跑到一半被杀，进程死亡后由恢复入口接管（不是模拟状态，而是真的 kill 进程）。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest
import redis
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.agent.orchestrator import execute_run
from app.agent.providers import build_provider_set
from app.agent.recovery import recover_runs
from app.config import Settings
from app.models import AgentRun, GuideCard, MediaAsset, PlaceCandidate, RunStatus, RunStep, StepName, utcnow
from tests.conftest import BACKEND_DIR, create_project, make_image_bytes, unique_title

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


def _queue_depth(settings: Settings) -> int:
    client = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=1.0, socket_timeout=1.0)
    try:
        return int(client.llen(f"dramatiq:{settings.redis_queue_name}"))
    finally:
        client.close()


def _upload(client: TestClient, project_id: str, *, color=(10, 20, 30)) -> dict:
    response = client.post(
        f"/api/v1/projects/{project_id}/media",
        files={"file": ("fault.png", make_image_bytes("PNG", color=color), "image/png")},
        data={"start_analysis": "true"},
    )
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------- 1


def test_ready_reports_redis_down_when_unreachable(settings, engine):
    """真实连接一个没有监听的端口：readiness 必须报 redis=down 并返回 503。"""
    from app.main import create_app

    broken = _settings_with(settings, redis_url="redis://127.0.0.1:6399/0")
    app = create_app(broken)
    with TestClient(app) as client:
        response = client.get("/api/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["checks"]["redis"] == "down"
    assert body["checks"]["redis_error_type"]
    assert body["checks"]["mysql"] == "up"


# --------------------------------------------------------------------------- 2


def test_enqueue_failure_keeps_run_queued_then_recovery_requeues(app, client, settings, session_factory, monkeypatch):
    """Redis 不可用时 run 留在 QUEUED（不丢任务），恢复入口重新投递。"""
    import app.api.media as media_module
    import app.worker.actors as actors_module

    def broken_send(*args, **kwargs):
        raise ConnectionError("simulated redis outage")

    monkeypatch.setattr(actors_module.advance_run, "send", broken_send)
    monkeypatch.setattr(media_module, "enqueue_run", lambda run_id: False)

    project = create_project(client, title=unique_title("outage"))
    body = _upload(client, project["id"])
    run_id = body["run"]["id"]

    assert body["run"]["status"] == RunStatus.QUEUED
    assert _queue_depth(settings) == 0  # 队列里确实没有消息

    with app.state.session_factory() as db:
        run = db.get(AgentRun, run_id)
        assert run.status == RunStatus.QUEUED
        assert run.attempt == 0

    monkeypatch.undo()

    # 恢复入口：把 QUEUED 重新投递。
    result = recover_runs(session_factory, settings)
    assert result["queued"] >= 1
    assert result["enqueued"] >= 1
    assert _queue_depth(settings) >= 1

    # 恢复后可以正常跑完第一阶段。
    providers = build_provider_set(settings)
    assert execute_run(session_factory, run_id, providers=providers, settings=settings) == RunStatus.WAITING_USER


# --------------------------------------------------------------------------- 3


def test_recovery_reclaims_expired_lease_and_run_continues(app, client, settings, session_factory):
    """Worker 中止后遗留的 RUNNING（租约过期）可以被恢复并继续。"""
    project = create_project(client, title=unique_title("lease"))
    body = _upload(client, project["id"])
    run_id = body["run"]["id"]

    # 模拟"Worker 拿到任务后进程被杀"：状态 RUNNING + 过期租约 + 步骤停在 RUNNING。
    with app.state.session_factory() as db:
        run = db.get(AgentRun, run_id)
        run.status = RunStatus.RUNNING
        run.attempt = 1
        run.lease_owner = "dead-worker"
        run.lease_expires_at = utcnow() - timedelta(seconds=60)
        run.started_at = utcnow() - timedelta(seconds=120)
        step = RunStep(run_id=run_id, name=StepName.ANALYZE_IMAGE, sequence=1, status="RUNNING", attempt=1)
        db.add(step)
        db.commit()

    result = recover_runs(session_factory, settings)
    assert result["stale_running"] == 1
    assert result["enqueued"] == 1

    with app.state.session_factory() as db:
        run = db.get(AgentRun, run_id)
        assert run.lease_expires_at is None and run.lease_owner is None
        assert run.status == RunStatus.RUNNING  # 交给 Worker 认领，不擅自改成终态

    providers = build_provider_set(settings)
    assert execute_run(session_factory, run_id, providers=providers, settings=settings) == RunStatus.WAITING_USER

    with app.state.session_factory() as db:
        run = db.get(AgentRun, run_id)
        assert run.attempt == 2  # 接管后 attempts 递增
        step = db.execute(
            select(RunStep).where(RunStep.run_id == run_id, RunStep.name == StepName.ANALYZE_IMAGE)
        ).scalar_one()
        assert step.status == "SUCCEEDED"


def test_waiting_user_runs_are_not_touched_by_recovery(app, client, settings, session_factory):
    project = create_project(client, title=unique_title("waiting"))
    body = _upload(client, project["id"])
    run_id = body["run"]["id"]
    providers = build_provider_set(settings)
    assert execute_run(session_factory, run_id, providers=providers, settings=settings) == RunStatus.WAITING_USER

    result = recover_runs(session_factory, settings)
    assert result["queued"] == 0
    assert result["stale_running"] == 0

    with app.state.session_factory() as db:
        run = db.get(AgentRun, run_id)
        assert run.status == RunStatus.WAITING_USER


# --------------------------------------------------------------------------- 4


def test_worker_process_killed_mid_run_is_recovered(app, client, settings, session_factory):
    """真实子进程演练：Worker 跑到一半被杀，租约过期后由恢复入口接管并跑完。"""
    project = create_project(client, title=unique_title("kill"))
    body = _upload(client, project["id"])
    run_id = body["run"]["id"]
    media_id = body["media"]["id"]

    env = dict(os.environ)
    env.update(
        {
            "DATABASE_URL": settings.database_url,
            "REDIS_URL": settings.redis_url,
            "SESSION_SECRET": settings.session_secret,
            "PROVIDER_MODE": "mock",
            "APP_ENV": "test",
            "MEDIA_ROOT": str(settings.media_root),
            # 让视觉步骤长时间阻塞（fixture 的 slow 行为），以便在它执行中被杀。
            "PROVIDER_TIMEOUT_SECONDS": "30",
            "FIXTURE_BEHAVIORS": "vision=slow",
        }
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "app.worker.cli", "run", run_id],
        cwd=str(BACKEND_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        # 等到 Worker 已经把 run 认领成 RUNNING 并写下步骤记录。
        deadline = time.monotonic() + 30
        claimed = False
        while time.monotonic() < deadline:
            with app.state.session_factory() as db:
                run = db.get(AgentRun, run_id)
                steps = db.execute(select(RunStep).where(RunStep.run_id == run_id)).scalars().all()
                if run.status == RunStatus.RUNNING and steps:
                    claimed = True
                    break
            time.sleep(0.2)
        assert claimed, "worker 未在预期时间内认领任务"

        process.send_signal(signal.SIGTERM)
        process.wait(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()

    with app.state.session_factory() as db:
        run = db.get(AgentRun, run_id)
        assert run.status == RunStatus.RUNNING  # 进程死了，但状态与租约留在 MySQL 里
        assert run.lease_expires_at is not None
        # 让租约立即过期，模拟 Worker 长时间不再回来。
        run.lease_expires_at = utcnow() - timedelta(seconds=1)
        db.commit()

    result = recover_runs(session_factory, settings)
    assert result["stale_running"] == 1

    providers = build_provider_set(settings)
    assert execute_run(session_factory, run_id, providers=providers, settings=settings) == RunStatus.WAITING_USER

    with app.state.session_factory() as db:
        media = db.get(MediaAsset, media_id)
        assert media.status == "WAITING_USER"
        candidates = db.execute(select(PlaceCandidate).where(PlaceCandidate.run_id == run_id)).scalars().all()
        assert len(candidates) == 1

    # 确认后继续跑完，卡片只生成一张。
    candidate = candidates[0]
    confirm = client.post(
        f"/api/v1/runs/{run_id}/confirm-place", json={"decision": "confirm", "candidate_id": candidate.id}
    )
    assert confirm.status_code == 200
    assert execute_run(session_factory, run_id, providers=providers, settings=settings) == RunStatus.SUCCEEDED

    with app.state.session_factory() as db:
        cards = db.execute(select(GuideCard).where(GuideCard.media_asset_id == media_id)).scalars().all()
        assert len(cards) == 1


def test_media_file_missing_fails_run_cleanly(app, client, settings, session_factory):
    """图片文件被外部删除时，运行必须明确失败，而不是卡在 RUNNING。"""
    from app.storage import storage_path

    project = create_project(client, title=unique_title("missing"))
    body = _upload(client, project["id"])
    run_id = body["run"]["id"]

    with app.state.session_factory() as db:
        media = db.get(MediaAsset, body["media"]["id"])
        path = storage_path(Path(settings.media_root), media.storage_key)
    path.unlink()

    providers = build_provider_set(settings)
    status = execute_run(session_factory, run_id, providers=providers, settings=settings)
    assert status == RunStatus.FAILED

    with app.state.session_factory() as db:
        run = db.get(AgentRun, run_id)
        assert run.error_code == "media_file_missing"
        assert db.get(MediaAsset, body["media"]["id"]).status == "FAILED"
