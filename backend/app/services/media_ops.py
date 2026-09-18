"""媒体操作服务（B5 / D4）：对已保存照片新建分析、软删除与恢复、笔记、改地点、重新生成讲解。

设计原则（对应 07 的 B5 与 08 的第 3/9 节）：
- **终态 run 不复活**：任何"重做"都创建新的 AgentRun 并写 `source_run_id`。
- **取消与删除分开**：取消只终止任务，删除是媒体生命周期操作（软删除，可恢复）。
- **版本化**：改地点使 `Place.version + 1`，并把受影响的讲解标记为过期（`stale`），保留历史与笔记。
- **条件更新**：改地点请求可带 `expected_version`，版本不符返回冲突，避免两个标签页互相覆盖。
"""

from __future__ import annotations

import logging
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agent.queue import enqueue_run
from app.config import Settings
from app.events import EventType, append_event
from app.limits import reserve_run_quota
from app.models import (
    AgentRun,
    DemoSession,
    GuideCard,
    GuideProject,
    MediaAsset,
    MediaStatus,
    Place,
    RunStatus,
    StepName,
    utcnow,
)

logger = logging.getLogger("app.services.media_ops")


class MediaOperationError(RuntimeError):
    """媒体操作被拒绝（状态不允许或版本冲突）。code 会返回给前端。"""

    def __init__(self, code: str, *, status_code: int = 409) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


def _next_position(db: Session, project_id: str) -> int:
    current = db.execute(
        select(func.max(MediaAsset.position)).where(MediaAsset.project_id == project_id)
    ).scalar()
    return int(current or 0) + 1


def start_analysis_for_media(
    db: Session,
    settings: Settings,
    *,
    project: GuideProject,
    media: MediaAsset,
    session_row: DemoSession,
    idempotency_key: str | None = None,
    client_hash: str | None = None,
    trigger: str = "manual",
) -> tuple[AgentRun, bool]:
    """对已保存/已存在的照片新建分析运行（B5：上传时 start_analysis=false 的补入口）。"""
    if media.deleted_at is not None:
        raise MediaOperationError("media_deleted")

    # 幂等优先：同一个 Idempotency-Key 的重放必须返回原运行，
    # 而不是因为"任务正在跑"被判成 409。
    key = (idempotency_key or "").strip() or None
    if key:
        existing = db.execute(
            select(AgentRun).where(AgentRun.project_id == project.id, AgentRun.idempotency_key == key)
        ).scalar_one_or_none()
        if existing is not None:
            return existing, False

    if media.status in (MediaStatus.ANALYZING, MediaStatus.QUEUED):
        raise MediaOperationError("media_busy")

    active = db.execute(
        select(AgentRun).where(
            AgentRun.media_asset_id == media.id,
            AgentRun.status.in_((RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING_USER)),
        )
    ).scalar_one_or_none()
    if active is not None:
        raise MediaOperationError("run_in_progress")

    place = db.execute(select(Place).where(Place.media_asset_id == media.id)).scalar_one_or_none()
    start_step = StepName.LOOKUP_PLACE if place is not None else StepName.ANALYZE_IMAGE

    reserve_run_quota(db, settings, session_row, client_hash)

    run = AgentRun(
        project_id=project.id,
        media_asset_id=media.id,
        status=RunStatus.QUEUED,
        current_step=start_step,
        trigger=trigger,
        input_snapshot={
            "media_id": media.id,
            "position": media.position,
            "city_hint": project.city_hint,
            "mime_type": media.mime_type,
            "source": "media_ops",
            "input_version": project.version,
        },
        input_version=project.version,
        idempotency_key=key,
        budget_max_steps=settings.run_max_steps,
        budget_max_tool_calls=settings.run_max_tool_calls,
        budget_max_tokens=settings.run_max_tokens,
        budget_max_cost=settings.run_max_cost,
    )
    db.add(run)
    db.flush()
    media.status = MediaStatus.QUEUED
    media.active_run_id = run.id
    media.updated_at = utcnow()
    project.version += 1
    project.updated_at = utcnow()
    session_row.runs_started += 1
    append_event(
        db,
        project_id=project.id,
        type=EventType.RUN_QUEUED,
        payload={"trigger": trigger, "start_step": start_step, "media_asset_id": media.id},
        run_id=run.id,
        media_asset_id=media.id,
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if key:
            existing = db.execute(
                select(AgentRun).where(AgentRun.project_id == project.id, AgentRun.idempotency_key == key)
            ).scalar_one_or_none()
            if existing is not None:
                return existing, False
        raise
    return run, True


def soft_delete_media(
    db: Session, *, project: GuideProject, media: MediaAsset, session_row: DemoSession
) -> MediaAsset:
    """软删除：保留文件与历史，取消未完成任务，不再出现在相册里。"""
    if media.deleted_at is not None:
        return media
    now = utcnow()
    cancelled = 0
    for run in db.execute(
        select(AgentRun).where(
            AgentRun.media_asset_id == media.id,
            AgentRun.status.in_((RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING_USER)),
        )
    ).scalars():
        from app.agent.state import transition

        transition(run, RunStatus.CANCELLED, error_code="media_deleted")
        run.finished_at = now
        cancelled += 1
        append_event(
            db,
            project_id=project.id,
            type=EventType.RUN_CANCELLED,
            payload={"reason": "media_deleted", "run_id": run.id},
            run_id=run.id,
            media_asset_id=media.id,
        )
    media.deleted_at = now
    media.active_run_id = None
    if media.status != MediaStatus.CONFIRMED:
        media.status = MediaStatus.CANCELLED
    media.updated_at = now
    project.version += 1
    project.updated_at = now
    append_event(
        db,
        project_id=project.id,
        type=EventType.MEDIA_DELETED,
        payload={"media_id": media.id, "cancelled_runs": cancelled},
        media_asset_id=media.id,
    )
    db.commit()
    logger.info("media_soft_deleted", extra={"media_id": media.id, "cancelled_runs": cancelled})
    return media


def restore_media(db: Session, *, project: GuideProject, media: MediaAsset) -> MediaAsset:
    """恢复被软删除的照片。晚到的任务不得让它复活内容（编排器写回时会检查 deleted_at）。"""
    if media.deleted_at is None:
        return media
    media.deleted_at = None
    media.updated_at = utcnow()
    if media.status == MediaStatus.CANCELLED:
        media.status = MediaStatus.CONFIRMED if _has_card(db, media.id) else MediaStatus.UPLOADED
    project.version += 1
    project.updated_at = utcnow()
    append_event(
        db,
        project_id=project.id,
        type=EventType.MEDIA_RESTORED,
        payload={"media_id": media.id},
        media_asset_id=media.id,
    )
    db.commit()
    return media


def _has_card(db: Session, media_id: str) -> bool:
    return db.execute(select(GuideCard.id).where(GuideCard.media_asset_id == media_id)).scalar_one_or_none() is not None


def update_media_note(
    db: Session, *, project: GuideProject, media: MediaAsset, note: str | None
) -> MediaAsset:
    """用户笔记：只由用户写入，模型结果不覆盖（D4 要求保留笔记）。"""
    if media.deleted_at is not None:
        raise MediaOperationError("media_deleted")
    media.note = note
    media.updated_at = utcnow()
    project.version += 1
    project.updated_at = utcnow()
    append_event(
        db,
        project_id=project.id,
        type=EventType.MEDIA_UPDATED,
        payload={"media_id": media.id, "field": "note"},
        media_asset_id=media.id,
    )
    db.commit()
    return media


def change_media_place(
    db: Session,
    settings: Settings,
    *,
    project: GuideProject,
    media: MediaAsset,
    name: str,
    address: str | None = None,
    region: str | None = None,
    expected_version: int | None = None,
    regenerate: bool = False,
    session_row: DemoSession | None = None,
    client_hash: str | None = None,
) -> tuple[Place, AgentRun | None, bool]:
    """用户改地点（B5：改地点后旧讲解标记过期但保留历史，可选择性重新生成）。

    返回 (place, 新运行或 None, 是否创建了新运行)。
    """
    if media.deleted_at is not None:
        raise MediaOperationError("media_deleted")
    place = db.execute(select(Place).where(Place.media_asset_id == media.id)).scalar_one_or_none()
    if place is None:
        # 尚未确认过地点：这是首次设定，不算冲突
        place = Place(
            project_id=project.id,
            media_asset_id=media.id,
            run_id=None,
            name=name.strip(),
            provider="user",
            confirmed_by="user_input",
            source="user",
            match_status="pending",
            version=1,
        )
        db.add(place)
    else:
        if expected_version is not None and place.version != expected_version:
            # 条件更新：两个标签页同时改地点时，后提交的一方必须看到冲突
            raise MediaOperationError("place_version_conflict")
        place.name = name.strip()
        place.version += 1
        place.confirmed_by = "user_input"
        place.provider = "user"
        place.provider_place_id = None
        place.provider_payload = None
        place.address = address
        place.region = region
        place.latitude = None
        place.longitude = None
        place.query_at = None
        place.match_status = "pending"
        place.match_note = None
        place.updated_at = utcnow()

    # 受影响讲解标记过期：保留历史内容，但明确标出"基于旧地点"
    stale_cards = 0
    for card in db.execute(select(GuideCard).where(GuideCard.media_asset_id == media.id)).scalars():
        card.stale = True
        card.stale_reason = f"地点已改为「{place.name}」（地点版本 {place.version}）"
        card.updated_at = utcnow()
        stale_cards += 1

    media.updated_at = utcnow()
    project.version += 1
    project.updated_at = utcnow()
    append_event(
        db,
        project_id=project.id,
        type=EventType.PLACE_CHANGED,
        payload={
            "media_id": media.id,
            "place_id": place.id,
            "place_name": place.name,
            "place_version": place.version,
            "stale_cards": stale_cards,
        },
        media_asset_id=media.id,
    )

    new_run: AgentRun | None = None
    created = False
    if regenerate:
        if session_row is None:
            raise MediaOperationError("session_required", status_code=500)
        reserve_run_quota(db, settings, session_row, client_hash)
        new_run = AgentRun(
            project_id=project.id,
            media_asset_id=media.id,
            status=RunStatus.QUEUED,
            current_step=StepName.LOOKUP_PLACE,
            trigger="place_changed",
            input_snapshot={
                "media_id": media.id,
                "position": media.position,
                "city_hint": project.city_hint,
                "mime_type": media.mime_type,
                "place_id": place.id,
                "place_version": place.version,
                "input_version": project.version,
            },
            input_version=project.version,
            budget_max_steps=settings.run_max_steps,
            budget_max_tool_calls=settings.run_max_tool_calls,
            budget_max_tokens=settings.run_max_tokens,
            budget_max_cost=settings.run_max_cost,
        )
        db.add(new_run)
        db.flush()
        media.status = MediaStatus.QUEUED
        media.active_run_id = new_run.id
        append_event(
            db,
            project_id=project.id,
            type=EventType.RUN_QUEUED,
            payload={"trigger": "place_changed", "start_step": StepName.LOOKUP_PLACE, "media_asset_id": media.id},
            run_id=new_run.id,
            media_asset_id=media.id,
        )
        created = True

    db.commit()
    if created and new_run is not None:
        enqueue_run(new_run.id)
    logger.info("media_place_changed", extra={"media_id": media.id, "place_version": place.version, "regenerate": created})
    return place, new_run, created


def retry_run(
    db: Session,
    settings: Settings,
    *,
    project: GuideProject,
    media: MediaAsset,
    source_run: AgentRun,
    session_row: DemoSession,
    idempotency_key: str | None = None,
    client_hash: str | None = None,
) -> tuple[AgentRun, bool]:
    """从任意终态运行重做：FAILED / PARTIAL / **CANCELLED** 都允许（B5）。

    终态运行本身不复活：新运行写 `source_run_id`。
    """
    if source_run.status not in (RunStatus.FAILED, RunStatus.PARTIAL, RunStatus.CANCELLED):
        raise MediaOperationError("run_not_retryable")
    if media.deleted_at is not None:
        raise MediaOperationError("media_deleted")

    key = (idempotency_key or "").strip() or f"retry:{source_run.id}"
    existing = db.execute(
        select(AgentRun).where(AgentRun.project_id == project.id, AgentRun.idempotency_key == key)
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    place = db.execute(select(Place).where(Place.media_asset_id == media.id)).scalar_one_or_none()
    start_step = StepName.LOOKUP_PLACE if place is not None else StepName.ANALYZE_IMAGE
    reserve_run_quota(db, settings, session_row, client_hash)

    run = AgentRun(
        project_id=project.id,
        media_asset_id=media.id,
        status=RunStatus.QUEUED,
        current_step=start_step,
        trigger="retry",
        source_run_id=source_run.id,
        input_snapshot={
            "media_id": media.id,
            "position": media.position,
            "city_hint": project.city_hint,
            "mime_type": media.mime_type,
            "input_version": project.version,
        },
        input_version=project.version,
        idempotency_key=key,
        budget_max_steps=settings.run_max_steps,
        budget_max_tool_calls=settings.run_max_tool_calls,
        budget_max_tokens=settings.run_max_tokens,
        budget_max_cost=settings.run_max_cost,
    )
    db.add(run)
    db.flush()
    media.status = MediaStatus.QUEUED
    media.active_run_id = run.id
    media.updated_at = utcnow()
    project.version += 1
    project.updated_at = utcnow()
    session_row.runs_started += 1
    append_event(
        db,
        project_id=project.id,
        type=EventType.RUN_RETRY_QUEUED,
        payload={"trigger": "retry", "source_run_id": source_run.id, "start_step": start_step},
        run_id=run.id,
        media_asset_id=media.id,
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.execute(
            select(AgentRun).where(AgentRun.project_id == project.id, AgentRun.idempotency_key == key)
        ).scalar_one_or_none()
        if existing is not None:
            return existing, False
        raise
    return run, True
