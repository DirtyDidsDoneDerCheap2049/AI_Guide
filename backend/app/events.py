"""WorkspaceEvent：供 SSE 推送与断线补发的持久事件（写入 MySQL）。

事件与业务变化在同一事务中写入，因此 MySQL 提交成功后事件一定存在。
心跳不写业务表（见 api/events.py）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Select, func, select, text, update
from sqlalchemy.orm import Session

from app.models import ProjectEventCounter, WorkspaceEvent


class EventType:
    PROJECT_CREATED = "project.created"
    MEDIA_UPLOADED = "media.uploaded"
    RUN_QUEUED = "run.queued"
    RUN_STARTED = "run.started"
    STEP_STARTED = "step.started"
    STEP_FINISHED = "step.finished"
    CANDIDATE_READY = "candidate.ready"
    RUN_WAITING_USER = "run.waiting_user"
    PLACE_CONFIRMED = "place.confirmed"
    PLACE_REJECTED = "place.rejected"
    # B4：供应商返回多条同名 POI 或城市冲突，需要用户在候选里消歧
    PLACE_DISAMBIGUATION_REQUIRED = "place.disambiguation_required"
    PLACE_MATCH_RESOLVED = "place.match_resolved"
    # B5/D4：项目与媒体生命周期、用户编辑
    PROJECT_UPDATED = "project.updated"
    PROJECT_CLAIMED = "project.claimed"
    MEDIA_DELETED = "media.deleted"
    MEDIA_RESTORED = "media.restored"
    MEDIA_UPDATED = "media.updated"
    PLACE_CHANGED = "place.changed"
    # D3-b：收藏与路线
    PLACE_SAVED = "place.saved"
    PLACE_UNSAVED = "place.unsaved"
    ROUTE_DRAFT_CREATED = "route.draft_created"
    ROUTE_DRAFT_UPDATED = "route.draft_updated"
    ROUTE_COMPUTED = "route.computed"
    # D1-h：持久对话（用户消息、助手回答、失败）
    MESSAGE_CREATED = "message.created"
    MESSAGE_READY = "message.ready"
    MESSAGE_FAILED = "message.failed"
    MESSAGE_UPDATED = "message.updated"
    MESSAGE_DELETED = "message.deleted"
    CARD_READY = "card.ready"
    RUN_PARTIAL = "run.partial"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"
    RUN_RETRY_QUEUED = "run.retry_queued"


REQUIRED_EVENT_TYPES = frozenset(
    {
        EventType.RUN_QUEUED,
        EventType.STEP_STARTED,
        EventType.CANDIDATE_READY,
        EventType.RUN_WAITING_USER,
        EventType.PLACE_CONFIRMED,
        EventType.CARD_READY,
        EventType.RUN_FAILED,
        EventType.RUN_CANCELLED,
    }
)


def allocate_event_seq(db: Session, project_id: str) -> int:
    """在**当前事务内**分配下一个项目事件序号（B7）。

    实现：对 ``project_event_counters`` 的行做 ``SELECT ... FOR UPDATE`` 再自增。
    行锁持有到事务提交/回滚，因此两个并发事务不会拿到相同或交叉的序号，
    并且较小序号的事务一定先提交——这正是"读者不会漏事件"的前提。

    第一次写入的项目用 ``INSERT ... ON DUPLICATE KEY UPDATE`` 建行（并发安全）；
    回滚会留下序号空洞，但不会出现乱序提交。
    """
    # 先插入计数行（幂等）；若并发同时插入，唯一键保证只有一行生效。
    db.execute(
        text(
            "INSERT INTO project_event_counters (project_id, last_seq, updated_at) "
            "VALUES (:pid, 0, NOW()) ON DUPLICATE KEY UPDATE project_id = project_id"
        ),
        {"pid": project_id},
    )
    current = db.execute(
        select(ProjectEventCounter.last_seq)
        .where(ProjectEventCounter.project_id == project_id)
        .with_for_update()
    ).scalar()
    nxt = int(current or 0) + 1
    db.execute(
        update(ProjectEventCounter)
        .where(ProjectEventCounter.project_id == project_id)
        .values(last_seq=nxt)
    )
    return nxt


def append_event(
    db: Session,
    *,
    project_id: str,
    type: str,
    payload: dict[str, Any] | None = None,
    run_id: str | None = None,
    media_asset_id: str | None = None,
    seq: int | None = None,
) -> WorkspaceEvent:
    """把事件加入当前事务（调用方负责提交）。

    序号在同一事务内分配，所以业务变更与事件要么一起提交、要么一起回滚；
    ``seq`` 显式传入只用于数据修复/迁移脚本。
    """
    if seq is None:
        seq = allocate_event_seq(db, project_id)
    event = WorkspaceEvent(
        project_id=project_id,
        run_id=run_id,
        media_asset_id=media_asset_id,
        type=type,
        payload=payload or {},
        seq=seq,
    )
    db.add(event)
    db.flush()
    return event


def events_since(db: Session, project_id: str, last_seq: int, limit: int = 200) -> list[WorkspaceEvent]:
    """按项目序号增量读取（SSE 补发用）。参数是游标 seq，不是全局自增 id。"""
    statement: Select[tuple[WorkspaceEvent]] = (
        select(WorkspaceEvent)
        .where(WorkspaceEvent.project_id == project_id, WorkspaceEvent.seq > last_seq)
        .order_by(WorkspaceEvent.seq)
        .limit(limit)
    )
    return list(db.execute(statement).scalars())


def latest_event_seq(db: Session, project_id: str) -> int:
    """快照的一致性边界：先读这个水位，再读业务行（同一事务、同一读视图）。"""
    value = db.execute(
        select(ProjectEventCounter.last_seq).where(ProjectEventCounter.project_id == project_id)
    ).scalar()
    if value is not None:
        return int(value)
    return int(
        db.execute(
            select(func.max(WorkspaceEvent.seq)).where(WorkspaceEvent.project_id == project_id)
        ).scalar()
        or 0
    )


def latest_event_id(db: Session, project_id: str) -> int:
    """项目内最新事件的全局自增 id（仅诊断用途，不能当游标）。"""
    value = db.execute(
        select(func.max(WorkspaceEvent.id)).where(WorkspaceEvent.project_id == project_id)
    ).scalar()
    return int(value or 0)
