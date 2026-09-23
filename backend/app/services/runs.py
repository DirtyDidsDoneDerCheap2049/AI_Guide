"""运行生命周期服务：创建（幂等）、地点确认、取消、重试。

所有写操作在同一个事务里更新业务表与 WorkspaceEvent，保证 SSE 补发的完整性。
"""

from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agent.state import transition
from app.config import Settings
from app.events import EventType, append_event
from app.limits import reserve_run_quota
from app.models import (
    AgentRun,
    DemoSession,
    GuideProject,
    Intent,
    MediaAsset,
    MediaStatus,
    Message,
    Place,
    PlaceCandidate,
    PlaceMatchCandidate,
    RunStatus,
    RunStep,
    StepName,
    utcnow,
)
from app.schemas import ConfirmPlaceRequest
from app.services.model_options import select_model

logger = logging.getLogger("app.services.runs")


class RunConflict(RuntimeError):
    """当前状态不允许该操作；API 映射为 409。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class PlaceInputError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _snapshot(media: MediaAsset, project: GuideProject) -> dict:
    return {
        "media_id": media.id,
        "mime_type": media.mime_type,
        "size_bytes": media.size_bytes,
        "sha256": media.sha256,
        "city_hint": project.city_hint,
        "position": media.position,
    }


def create_run_for_media(
    db: Session,
    settings: Settings,
    *,
    project: GuideProject,
    media: MediaAsset,
    session_row: DemoSession,
    idempotency_key: str | None = None,
    trigger: str = "initial",
    start_step: str = StepName.ANALYZE_IMAGE,
    client_hash: str | None = None,
    quota_reserved: bool = False,
) -> tuple[AgentRun, bool]:
    """创建 AgentRun。返回 (run, created)；幂等命中时 created=False。"""
    key = (idempotency_key or "").strip() or None
    if key:
        existing = db.execute(
            select(AgentRun).where(AgentRun.project_id == project.id, AgentRun.idempotency_key == key)
        ).scalar_one_or_none()
        if existing is not None:
            return existing, False

    # B6：额度以数据库额度桶原子预留；上传路径已在写文件前预留，这里不重复扣。
    if not quota_reserved:
        reserve_run_quota(db, settings, session_row, client_hash)

    run = AgentRun(
        project_id=project.id,
        media_asset_id=media.id,
        status=RunStatus.QUEUED,
        current_step=start_step,
        trigger=trigger,
        input_snapshot=_snapshot(media, project),
        idempotency_key=key,
        budget_max_steps=settings.run_max_steps,
        budget_max_tool_calls=settings.run_max_tool_calls,
        budget_max_tokens=settings.run_max_tokens,
        budget_max_cost=Decimal(str(settings.run_max_cost)),
    )
    db.add(run)
    media.status = MediaStatus.QUEUED
    media.updated_at = utcnow()
    project.version += 1
    project.updated_at = utcnow()
    session_row.runs_started += 1
    db.flush()
    append_event(
        db,
        project_id=project.id,
        # 重试产生的新运行使用专门的事件类型，便于前端与排障区分。
        type=EventType.RUN_RETRY_QUEUED if trigger == "retry" else EventType.RUN_QUEUED,
        payload={"trigger": trigger, "start_step": start_step, "media_asset_id": media.id},
        run_id=run.id,
        media_asset_id=media.id,
    )
    try:
        db.commit()
    except IntegrityError:
        # 唯一约束兜底：并发或重复提交时不产生第二条运行。
        db.rollback()
        if key:
            existing = db.execute(
                select(AgentRun).where(AgentRun.project_id == project.id, AgentRun.idempotency_key == key)
            ).scalar_one_or_none()
            if existing is not None:
                return existing, False
        raise
    logger.info("run_created", extra={"run_id": run.id, "project_id": project.id, "trigger": trigger})
    return run, True


def confirm_place(
    db: Session,
    *,
    run: AgentRun,
    media: MediaAsset,
    project: GuideProject,
    payload: ConfirmPlaceRequest,
) -> AgentRun:
    """用户确认、纠正或拒绝地点候选，然后恢复同一个 AgentRun。

    B4：如果这次等待是"供应商消歧"（``wait_for_place_disambiguation``），用户可以从
    供应商候选里选一条具体 POI；只有在那一刻才把供应商地址与坐标写进地点。
    """
    if run.status != RunStatus.WAITING_USER:
        raise RunConflict("run_not_waiting_user")

    disambiguating = run.current_step == StepName.WAIT_FOR_PLACE_DISAMBIGUATION
    wait_step = db.execute(
        select(RunStep).where(RunStep.run_id == run.id, RunStep.name == run.current_step)
    ).scalar_one_or_none()

    if payload.decision == "reject":
        for candidate in db.execute(
            select(PlaceCandidate).where(PlaceCandidate.run_id == run.id, PlaceCandidate.status == "PENDING")
        ).scalars():
            candidate.status = "REJECTED"
        for row in db.execute(
            select(PlaceMatchCandidate).where(PlaceMatchCandidate.run_id == run.id)
        ).scalars():
            if row.status == "CANDIDATE":
                row.status = "REJECTED"
        if wait_step is not None:
            wait_step.status = "FAILED"
            wait_step.error_code = "place_rejected"
            wait_step.finished_at = utcnow()
            wait_step.output_summary = {"decision": "reject", "step": run.current_step}
            append_event(
                db,
                project_id=project.id,
                type=EventType.STEP_FINISHED,
                payload={"step": StepName.WAIT_FOR_PLACE_CONFIRMATION, "status": "FAILED", "error_code": "place_rejected"},
                run_id=run.id,
                media_asset_id=media.id,
            )
        transition(run, RunStatus.CANCELLED, error_code="place_rejected")
        media.status = MediaStatus.REJECTED
        media.updated_at = utcnow()
        append_event(
            db,
            project_id=project.id,
            type=EventType.PLACE_REJECTED,
            payload={"decision": "reject"},
            run_id=run.id,
            media_asset_id=media.id,
        )
        append_event(
            db,
            project_id=project.id,
            type=EventType.RUN_CANCELLED,
            payload={"reason": "place_rejected"},
            run_id=run.id,
            media_asset_id=media.id,
        )
        db.commit()
        return run

    name = (payload.name or "").strip()
    address = payload.address
    region = payload.region
    confirmed_by = "user_input"

    if payload.candidate_id:
        candidate = db.get(PlaceCandidate, payload.candidate_id)
        if candidate is None or candidate.run_id != run.id:
            raise PlaceInputError("candidate_not_found")
        name = name or candidate.name
        address = address if address is not None else candidate.address
        region = region if region is not None else candidate.region
        confirmed_by = "candidate" if payload.decision == "confirm" else "user_input"

    # B4：用户从供应商候选中选定具体 POI（消歧结果）
    provider_row: PlaceMatchCandidate | None = None
    if payload.provider_candidate_id:
        provider_row = db.get(PlaceMatchCandidate, payload.provider_candidate_id)
        if provider_row is None or provider_row.run_id != run.id:
            raise PlaceInputError("provider_candidate_not_found")
        name = name or provider_row.name
        # 列宽 16：candidate / user_input / provider_poi
        confirmed_by = "provider_poi"

    if not name:
        raise PlaceInputError("missing_place_name")

    place = db.execute(select(Place).where(Place.media_asset_id == media.id)).scalar_one_or_none()
    if place is None:
        place = Place(
            project_id=project.id,
            media_asset_id=media.id,
            run_id=run.id,
            name=name,
            provider="user",
            confirmed_by=confirmed_by,
            source="user",
        )
        db.add(place)
    place.run_id = run.id
    # B4：名称始终是用户确认的身份；供应商只补充地址/坐标等事实。
    place.name = name
    place.address = address
    place.region = region
    place.confirmed_by = confirmed_by
    place.updated_at = utcnow()
    if provider_row is not None:
        place.provider = provider_row.provider
        place.provider_place_id = provider_row.provider_place_id
        place.provider_payload = {
            **(provider_row.payload or {}),
            "selected_name": provider_row.name,
            "selected_region": provider_row.region,
            "match_status": "user_selected",
        }
        place.address = provider_row.address
        place.region = provider_row.region
        place.latitude = provider_row.latitude
        place.longitude = provider_row.longitude
        place.query_at = utcnow()
        place.source = "amap"
        place.match_status = "user_selected"
        place.match_note = "用户在供应商候选中选择了具体 POI。"
        for row in db.execute(
            select(PlaceMatchCandidate).where(PlaceMatchCandidate.run_id == run.id)
        ).scalars():
            row.status = "SELECTED" if row.id == provider_row.id else "REJECTED"
    else:
        place.match_status = "pending"
        place.match_note = None
        for row in db.execute(
            select(PlaceMatchCandidate).where(PlaceMatchCandidate.run_id == run.id)
        ).scalars():
            if row.status == "CANDIDATE":
                row.status = "REJECTED"

    for candidate in db.execute(select(PlaceCandidate).where(PlaceCandidate.run_id == run.id)).scalars():
        if payload.candidate_id and candidate.id == payload.candidate_id:
            candidate.status = "CONFIRMED"
        elif candidate.status == "PENDING":
            candidate.status = "REJECTED"

    if wait_step is not None:
        wait_step.status = "SUCCEEDED"
        wait_step.finished_at = utcnow()
        wait_step.error_code = None
        wait_step.output_summary = {
            "decision": payload.decision,
            "place_name": name,
            "match_status": place.match_status,
            "provider_place_id": place.provider_place_id,
        }
        append_event(
            db,
            project_id=project.id,
            type=EventType.STEP_FINISHED,
            payload={"step": StepName.WAIT_FOR_PLACE_CONFIRMATION, "status": "SUCCEEDED", "decision": payload.decision},
            run_id=run.id,
            media_asset_id=media.id,
        )

    # B4：消歧完成后直接生成讲解；首次确认仍回到供应商检索步骤。
    run.current_step = StepName.GENERATE_GUIDE_CARD if disambiguating else StepName.LOOKUP_PLACE
    if disambiguating:
        # 用户做出了选择：快照必须随之更新，否则卡片仍会带着"地点未核实"的旧结论。
        snapshot = dict(run.input_snapshot or {})
        if provider_row is not None:
            snapshot["place_lookup_status"] = "user_selected"
            snapshot["place_lookup_failed"] = False
            snapshot.pop("place_lookup_error", None)
        else:
            snapshot["place_lookup_status"] = "unverified_by_user"
            snapshot["place_lookup_failed"] = True
        run.input_snapshot = snapshot
    transition(run, RunStatus.QUEUED)
    media.status = MediaStatus.QUEUED
    media.updated_at = utcnow()
    project.version += 1
    project.updated_at = utcnow()
    append_event(
        db,
        project_id=project.id,
        type=EventType.PLACE_CONFIRMED,
        payload={
            "place_name": name,
            "decision": payload.decision,
            "confirmed_by": confirmed_by,
            "provider_place_id": place.provider_place_id,
            "match_status": place.match_status,
            "resume_step": run.current_step,
        },
        run_id=run.id,
        media_asset_id=media.id,
    )
    db.commit()
    logger.info("place_confirmed", extra={"run_id": run.id, "decision": payload.decision})
    return run


def _next_message_seq(db: Session, project_id: str) -> int:
    """项目内消息序号：锁项目行后取 max+1（并发发送不会重号）。"""
    db.execute(select(GuideProject.id).where(GuideProject.id == project_id).with_for_update())
    current = db.execute(select(func.max(Message.seq)).where(Message.project_id == project_id)).scalar()
    return int(current or 0) + 1


def create_message_run(
    db: Session,
    settings: Settings,
    *,
    project: GuideProject,
    session_row: DemoSession,
    content: str,
    model_id: str | None = None,
    thinking: bool | None = None,
    thinking_level: str | None = None,
    model_selection: dict | None = None,
    media: MediaAsset | None = None,
    intent: str = Intent.ANSWER_QUESTION,
    idempotency_key: str | None = None,
    client_hash: str | None = None,
) -> tuple[Message, AgentRun, bool]:
    """创建用户消息 + 非图片 AgentRun（D1-h）。返回 (message, run, created)。

    幂等：同一 Idempotency-Key 重发返回原有消息与运行，不重复扣额度。
    """
    key = (idempotency_key or "").strip() or None
    if key:
        existing_run = db.execute(
            select(AgentRun).where(AgentRun.project_id == project.id, AgentRun.idempotency_key == key)
        ).scalar_one_or_none()
        if existing_run is not None:
            message_id = (existing_run.input_snapshot or {}).get("message_id")
            existing_message = db.get(Message, message_id) if message_id else None
            if existing_message is not None:
                return existing_message, existing_run, False

    selection = model_selection or select_model(settings, model_id, thinking, thinking_level)
    # B6：额度与图片任务共用同一批额度桶（对话同样消耗模型额度）。
    reserve_run_quota(db, settings, session_row, client_hash)

    seq = _next_message_seq(db, project.id)
    message = Message(
        project_id=project.id,
        seq=seq,
        role="user",
        intent=intent,
        status="QUEUED",
        content=content,
        media_ids=[media.id] if media is not None else [],
        place_ids=[],
    )
    db.add(message)
    db.flush()

    run = AgentRun(
        project_id=project.id,
        media_asset_id=media.id if media is not None else None,
        status=RunStatus.QUEUED,
        current_step=StepName.ANSWER_QUESTION,
        trigger="message",
        intent=intent,
        input_snapshot={
            "message_id": message.id,
            "model_selection": selection,
            "message_seq": seq,
            "content": content,
            "media_asset_id": media.id if media is not None else None,
            "city_hint": project.city_hint,
        },
        input_version=project.version,
        idempotency_key=key,
        budget_max_steps=settings.run_max_steps,
        budget_max_tool_calls=settings.run_max_tool_calls,
        budget_max_tokens=settings.run_max_tokens,
        budget_max_cost=Decimal(str(settings.run_max_cost)),
    )
    db.add(run)
    db.flush()
    message.run_id = run.id
    project.version += 1
    project.updated_at = utcnow()
    session_row.runs_started += 1
    append_event(
        db,
        project_id=project.id,
        type=EventType.MESSAGE_CREATED,
        payload={"message_id": message.id, "run_id": run.id, "seq": seq, "intent": intent},
        run_id=run.id,
        media_asset_id=media.id if media is not None else None,
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if key:
            existing_run = db.execute(
                select(AgentRun).where(AgentRun.project_id == project.id, AgentRun.idempotency_key == key)
            ).scalar_one_or_none()
            if existing_run is not None:
                message_id = (existing_run.input_snapshot or {}).get("message_id")
                existing_message = db.get(Message, message_id) if message_id else None
                if existing_message is not None:
                    return existing_message, existing_run, False
        raise
    logger.info("message_run_created", extra={"run_id": run.id, "project_id": project.id, "intent": intent})
    return message, run, True


def cancel_run(db: Session, *, run: AgentRun, media: MediaAsset | None, project: GuideProject, reason: str = "user_cancelled") -> AgentRun:
    db.refresh(run, with_for_update=True)
    if run.status in RunStatus.TERMINAL:
        raise RunConflict("run_already_finished")
    transition(run, RunStatus.CANCELLED, error_code=reason)
    if media is not None and media.status != MediaStatus.CONFIRMED:
        media.status = MediaStatus.CANCELLED
        media.updated_at = utcnow()
    append_event(
        db,
        project_id=project.id,
        type=EventType.RUN_CANCELLED,
        payload={"reason": reason},
        run_id=run.id,
        media_asset_id=media.id if media else None,
    )
    db.commit()
    logger.info("run_cancelled", extra={"run_id": run.id, "reason": reason})
    return run


def create_retry_run(
    db: Session,
    settings: Settings,
    *,
    source_run: AgentRun,
    project: GuideProject,
    media: MediaAsset | None,
    session_row: DemoSession,
    idempotency_key: str | None = None,
    client_hash: str | None = None,
) -> tuple[AgentRun, bool]:
    # B5：取消后的任务也必须能重做（旧实现只允许 FAILED/PARTIAL，取消后无法继续）。
    # 终态运行本身不复活：新运行写 source_run_id，旧运行保持原状态。
    if source_run.status not in (RunStatus.FAILED, RunStatus.PARTIAL, RunStatus.CANCELLED):
        raise RunConflict("run_not_retryable")
    if source_run.intent == Intent.ANSWER_QUESTION:
        db.refresh(project, with_for_update=True)
        original = db.get(Message, (source_run.input_snapshot or {}).get('message_id'))
        if original is None or original.deleted_at is not None:
            raise RunConflict('message_not_found')
        if original.content != (source_run.input_snapshot or {}).get('content'):
            raise RunConflict('message_changed')
        _, run, created = create_message_run(db, settings, project=project, session_row=session_row, content=original.content, media=media, idempotency_key=idempotency_key or f'retry:{source_run.id}', client_hash=client_hash, model_selection=(source_run.input_snapshot or {}).get("model_selection"))
        if created:
            run.source_run_id = source_run.id
            db.commit()
        return run, created
    if media is None:
        raise RunConflict('media_not_found')
    if media.deleted_at is not None:
        raise RunConflict("media_deleted")

    place = db.execute(select(Place).where(Place.media_asset_id == media.id)).scalar_one_or_none()
    start_step = StepName.LOOKUP_PLACE if place is not None else StepName.ANALYZE_IMAGE
    key = (idempotency_key or "").strip() or f"retry:{source_run.id}"
    run, created = create_run_for_media(
        db,
        settings,
        project=project,
        media=media,
        session_row=session_row,
        idempotency_key=key,
        trigger="retry",
        start_step=start_step,
    )
    if created:
        # 终态运行不复活：新运行记录来源，便于"谁从哪次重做"的追溯
        run.source_run_id = source_run.id
        db.commit()
        logger.info("run_retry_created", extra={"run_id": run.id, "source_run_id": source_run.id, "start_step": start_step})
    return run, created
