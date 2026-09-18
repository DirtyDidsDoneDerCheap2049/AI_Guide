"""把 run_id 投递到 Redis 队列。

Redis 只承载队列：投递失败不会改变 AgentRun 状态，记录仍然留在 QUEUED，等待恢复入口重新投递。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("app.agent.queue")


def enqueue_run(run_id: str) -> bool:
    try:
        from app.worker.actors import advance_run

        advance_run.send(run_id)
        return True
    except Exception as exc:  # noqa: BLE001 - Redis 不可用不应让 API 失败
        logger.warning(
            "enqueue_failed",
            extra={"run_id": run_id, "error_type": type(exc).__name__, "note": "run 保持 QUEUED，等待恢复"},
        )
        return False
