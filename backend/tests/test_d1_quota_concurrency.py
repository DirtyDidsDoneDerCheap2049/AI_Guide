"""D1 / B6：额度预留必须并发安全，且入口指纹不盲信 X-Forwarded-For。

覆盖 07 号报告 B6 的两点：
1. 旧实现是"先 count 再递增"，并发时会超额；这里用数据库额度桶条件扣减。
2. client_hash 存了但没用于 IP 限流，且 IP 解析信任任意 X-Forwarded-For。
"""

from __future__ import annotations

import threading
from datetime import date

import pytest
from sqlalchemy import delete, select

from app.config import Settings
from app.limits import QuotaExceeded, bucket_snapshot, reserve_run_quota
from app.models import DemoSession, QuotaBucket

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


def test_quota_reservation_is_concurrency_safe(session_factory, engine):
    """12 个线程并发抢 5 个名额：只能成功 5 次（旧实现会成功 12 次）。"""
    strict = _settings_with(
        Settings(_env_file=None, database_url=str(engine.url), session_secret="x", provider_mode="mock"),
        demo_session_run_limit=5,
        demo_daily_run_limit=1000,
        demo_ip_daily_limit=1000,
    )

    with session_factory() as db:
        db.execute(delete(QuotaBucket))
        session_row = DemoSession(client_hash="quota-test")
        db.add(session_row)
        db.commit()
        session_id = session_row.id

    successes: list[int] = []
    failures: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        with session_factory() as db:
            row = db.get(DemoSession, session_id)
            try:
                reserve_run_quota(db, strict, row, "quota-test-fingerprint")
                db.commit()
                with lock:
                    successes.append(1)
            except QuotaExceeded:
                db.rollback()
                with lock:
                    failures.append(1)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(successes) == 5, f"应恰好放行 5 个名额，实际 {len(successes)}"
    assert len(failures) == 7

    with session_factory() as db:
        used = db.execute(
            select(QuotaBucket.used).where(
                QuotaBucket.scope == "session",
                QuotaBucket.bucket_key == session_id,
                QuotaBucket.bucket_day == date.today(),
            )
        ).scalar()
    assert used == 5


def test_daily_bucket_blocks_further_runs(session_factory, engine):
    strict = _settings_with(
        Settings(_env_file=None, database_url=str(engine.url), session_secret="x", provider_mode="mock"),
        demo_session_run_limit=100,
        demo_daily_run_limit=2,
        demo_ip_daily_limit=100,
    )
    with session_factory() as db:
        db.execute(delete(QuotaBucket))
        session_row = DemoSession(client_hash="daily-test")
        db.add(session_row)
        db.commit()
        session_id = session_row.id

    for _ in range(2):
        with session_factory() as db:
            reserve_run_quota(db, strict, db.get(DemoSession, session_id), None)
            db.commit()

    with session_factory() as db:
        with pytest.raises(QuotaExceeded) as excinfo:
            reserve_run_quota(db, strict, db.get(DemoSession, session_id), None)
        assert excinfo.value.scope == "global"
        db.rollback()

    with session_factory() as db:
        snapshot = bucket_snapshot(db, session_id, None)
    assert snapshot == {"global": 2, "session": 2}


def test_forwarded_for_is_ignored_unless_peer_is_trusted(app):
    """未配置可信代理时，伪造 X-Forwarded-For 不会改变入口指纹。"""
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        first = client.get("/api/v1/session", headers={"X-Forwarded-For": "203.0.113.9"})
        second = client.get("/api/v1/session", headers={"X-Forwarded-For": "198.51.100.7"})
    assert first.status_code == 200 and second.status_code == 200
    # 两次请求来自同一 TCP 对端（TestClient），伪造头不应产生两个不同身份
    assert first.json()["id"] == second.json()["id"]
