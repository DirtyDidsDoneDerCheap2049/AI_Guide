"""AgentRun 状态机：显式、可校验的迁移规则。"""

from __future__ import annotations

from datetime import datetime

from app.models import AgentRun, RunStatus, utcnow


class InvalidTransition(RuntimeError):
    def __init__(self, current: str, target: str) -> None:
        self.current = current
        self.target = target
        super().__init__(f"invalid_transition:{current}->{target}")


ALLOWED: dict[str, frozenset[str]] = {
    RunStatus.QUEUED: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED, RunStatus.FAILED}),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.WAITING_USER,
            RunStatus.SUCCEEDED,
            RunStatus.PARTIAL,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.QUEUED,
        }
    ),
    RunStatus.WAITING_USER: frozenset({RunStatus.QUEUED, RunStatus.CANCELLED, RunStatus.FAILED}),
    RunStatus.PARTIAL: frozenset(),
    RunStatus.SUCCEEDED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}


def can_transition(current: str, target: str) -> bool:
    return target == current or target in ALLOWED.get(current, frozenset())


def truncate(value: str | None, limit: int = 500) -> str | None:
    if value is None:
        return None
    # 只保留错误类型与简短说明，避免把请求头或响应体写进数据库。
    return value[:limit]


def transition(
    run: AgentRun,
    target: str,
    *,
    error_code: str | None = None,
    error_message: str | None = None,
    now: datetime | None = None,
) -> AgentRun:
    if target == run.status:
        if error_code is not None:
            run.error_code = error_code
            run.error_message = truncate(error_message)
        return run
    if not can_transition(run.status, target):
        raise InvalidTransition(run.status, target)

    moment = now or utcnow()
    run.status = target
    run.updated_at = moment
    if target == RunStatus.RUNNING and run.started_at is None:
        run.started_at = moment
    if target in RunStatus.TERMINAL:
        run.finished_at = moment
        run.lease_expires_at = None
        run.lease_owner = None
    run.error_code = error_code
    run.error_message = truncate(error_message)
    return run
