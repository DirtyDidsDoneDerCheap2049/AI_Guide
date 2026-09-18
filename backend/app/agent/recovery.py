"""恢复入口：重新投递 QUEUED 和租约过期的 RUNNING。

- WAITING_USER 的运行属于等待用户输入，不会被恢复逻辑改动。
- 终态运行不参与恢复。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.agent.queue import enqueue_run
from app.config import Settings
from app.db import session_scope
from app.models import AgentRun, RunStatus, utcnow

logger = logging.getLogger("app.agent.recovery")


def recover_runs(factory: sessionmaker[Session], settings: Settings, *, limit: int = 200) -> dict[str, Any]:
    now = utcnow()
    with session_scope(factory) as db:
        queued_ids = [
            row[0]
            for row in db.execute(
                select(AgentRun.id)
                .where(AgentRun.status == RunStatus.QUEUED)
                .order_by(AgentRun.created_at)
                .limit(limit)
            ).all()
        ]
        stale_ids = [
            row[0]
            for row in db.execute(
                select(AgentRun.id)
                .where(
                    AgentRun.status == RunStatus.RUNNING,
                    or_(AgentRun.lease_expires_at.is_(None), AgentRun.lease_expires_at < now),
                )
                .order_by(AgentRun.created_at)
                .limit(limit)
            ).all()
        ]
        if stale_ids:
            # 条件更新：只有仍然处于"租约已过期"的 RUNNING 才清理，
            # 避免扫描之后某个 Worker 刚好续租/接管而被误清（B2）。
            db.execute(
                update(AgentRun)
                .where(
                    AgentRun.id.in_(stale_ids),
                    AgentRun.status == RunStatus.RUNNING,
                    or_(AgentRun.lease_expires_at.is_(None), AgentRun.lease_expires_at < now),
                )
                .values(lease_owner=None, lease_expires_at=None, updated_at=now)
            )

    enqueued = 0
    for run_id in queued_ids + stale_ids:
        if enqueue_run(run_id):
            enqueued += 1

    result = {
        "queued": len(queued_ids),
        "stale_running": len(stale_ids),
        "enqueued": enqueued,
    }
    logger.info("recovery_done", extra=result)
    return result
