"""工作区对话 API（D1-h）：发送消息创建非图片 AgentRun，读取持久消息列表。

设计要点：
- 消息与运行在同一事务内创建：运行失败不会留下"已发送但无对应任务"的消息。
- 幂等：同一个 Idempotency-Key 重发只返回原有消息与运行，不重复扣额度、不重复调用模型。
- 对话任务不接触图片：run.media_asset_id 可空，编排器走文字链路。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.exc import OperationalError

from app.agent.queue import enqueue_run
from app.api.deps import (
    get_client_hash,
    get_current_session,
    get_db,
    get_optional_user,
    get_settings_dep,
    load_owned_project,
)
from app.api.serialize import message_to_out, run_to_out
from app.config import Settings
from app.limits import QuotaExceeded
from app.models import AgentRun, GuideProject, DemoSession, MediaAsset, Message, User, utcnow
from app.schemas import CreateMessageRequest, CreateMessageResponse, MessageListOut, MessageOut, MessageEditRequest
from app.events import EventType, append_event
from app.services.runs import RunConflict, create_message_run

logger = logging.getLogger("app.api.messages")

router = APIRouter()


@router.post(
    "/projects/{project_id}/messages",
    response_model=CreateMessageResponse,
    status_code=status.HTTP_201_CREATED,
    summary="发送工作区消息并创建回答任务（非图片链路）",
)
def create_message_endpoint(
    project_id: str,
    payload: CreateMessageRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    session_row: DemoSession = Depends(get_current_session),
    user: User | None = Depends(get_optional_user),
    client_hash: str = Depends(get_client_hash),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> CreateMessageResponse:
    project = load_owned_project(db, project_id, session_row, user)

    media = None
    if payload.media_asset_id:
        media = db.get(MediaAsset, payload.media_asset_id)
        if media is None or media.project_id != project.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="media_not_found")

    try:
        message, run, created = create_message_run(
            db,
            settings,
            project=project,
            session_row=session_row,
            content=payload.content,
            media=media,
            intent=payload.intent,
            idempotency_key=idempotency_key,
            client_hash=client_hash,
        )
    except RunConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": exc.code}) from None
    except QuotaExceeded as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "quota_exceeded", "scope": exc.scope, "limit": exc.limit},
        ) from None

    if created:
        enqueue_run(run.id)
    db.refresh(message)
    db.refresh(run)
    return CreateMessageResponse(
        message=message_to_out(message),
        run=run_to_out(run),
        idempotent_replay=not created,
    )


@router.get(
    "/projects/{project_id}/messages",
    response_model=MessageListOut,
    summary="读取工作区持久消息（按 seq 增量）",
)
def list_messages_endpoint(
    project_id: str,
    after_seq: int = Query(default=0, ge=0, description="只返回 seq 大于该值的消息"),
    limit: int = Query(default=100, ge=1, le=200),
    q: str | None = Query(default=None, max_length=200),
    before_seq: int | None = Query(default=None, ge=1),
    session_row: DemoSession = Depends(get_current_session),
    user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db),
) -> MessageListOut:
    project = load_owned_project(db, project_id, session_row, user)

    statement = select(Message).where(Message.project_id == project.id, Message.seq > after_seq)
    if q and q.strip():
        statement = statement.where(Message.deleted_at.is_(None), Message.content.contains(q.strip(), autoescape=True))
    if before_seq:
        statement = statement.where(Message.seq < before_seq)
    rows = list(
        db.execute(
            statement
            .order_by(Message.seq.desc() if before_seq else Message.seq)
            .limit(limit)
        ).scalars()
    )
    last_seq = db.execute(
        select(func.max(Message.seq)).where(Message.project_id == project.id)
    ).scalar()
    if before_seq:
        rows.reverse()
    return MessageListOut(
        messages=[message_to_out(item) for item in rows],
        last_seq=int(last_seq or 0),
    )


def _editable_message(db, project, message_id, expected_version):
    # Same project lock as message creation. A running answer may already have
    # captured history: require stopping it before altering its inputs.
    db.execute(select(GuideProject).where(GuideProject.id == project.id).with_for_update())
    row = db.execute(select(Message).where(Message.id == message_id, Message.project_id == project.id).with_for_update()).scalar_one_or_none()
    if row is None or row.deleted_at:
        raise HTTPException(404, detail={"code": "message_not_found"})
    if row.version != expected_version:
        raise HTTPException(409, detail={"code": "message_version_conflict"})
    try:
        active = db.execute(select(AgentRun.id).where(AgentRun.project_id == project.id, AgentRun.status.in_(["QUEUED", "RUNNING", "WAITING_USER"])).with_for_update(nowait=True)).first()
    except OperationalError as exc:
        # Worker locks a run before writing its final message. Never wait on it
        # while holding the project lock needed for that final write.
        if getattr(exc.orig, 'args', [None])[0] not in (3572, 1205, 1213):
            raise
        db.rollback()
        raise HTTPException(409, detail={"code": "message_run_active"}) from None
    if active:
        raise HTTPException(409, detail={"code": "message_run_active"})
    return row


def _invalidate_answers(db, project, row):
    # Later answers may depend on the altered history. Preserve them for reading
    # but exclude them from future model context and disable their action chips.
    for answer in db.execute(select(Message).where(Message.project_id == project.id, Message.seq > row.seq, Message.role == "assistant", Message.deleted_at.is_(None)).with_for_update()).scalars():
        answer.stale = True
        answer.version += 1
    project.version += 1
    project.updated_at = utcnow()


@router.patch("/projects/{project_id}/messages/{message_id}", response_model=MessageOut)
def edit_message(project_id: str, message_id: str, payload: MessageEditRequest,
                 session_row: DemoSession = Depends(get_current_session), user: User | None = Depends(get_optional_user), db: Session = Depends(get_db)):
    project = load_owned_project(db, project_id, session_row, user)
    row = _editable_message(db, project, message_id, payload.expected_version)
    if row.role != "user":
        raise HTTPException(403, detail={"code": "only_user_message_editable"})
    content = payload.content.strip()
    if not content:
        raise HTTPException(422, detail={"code": "empty_message"})
    if content != row.content:
        row.content, row.edited_at = content, utcnow()
        row.version += 1
        _invalidate_answers(db, project, row)
        append_event(db, project_id=project.id, type=EventType.MESSAGE_UPDATED, payload={"message_id": row.id, "seq": row.seq})
        db.commit()
    return message_to_out(row)


@router.delete("/projects/{project_id}/messages/{message_id}", response_model=MessageOut)
def delete_message(project_id: str, message_id: str, expected_version: int = Query(ge=1),
                   session_row: DemoSession = Depends(get_current_session), user: User | None = Depends(get_optional_user), db: Session = Depends(get_db)):
    project = load_owned_project(db, project_id, session_row, user)
    row = _editable_message(db, project, message_id, expected_version)
    row.deleted_at = utcnow()
    row.version += 1
    _invalidate_answers(db, project, row)
    append_event(db, project_id=project.id, type=EventType.MESSAGE_DELETED, payload={"message_id": row.id, "seq": row.seq})
    db.commit()
    return message_to_out(row)


__all__ = ["router"]
