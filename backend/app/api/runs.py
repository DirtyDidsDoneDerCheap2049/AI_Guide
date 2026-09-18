"""运行 API：读取状态、确认/纠正/拒绝地点、取消与重试。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.agent.queue import enqueue_run
from app.api.deps import (
    get_client_hash,
    get_current_session,
    get_db,
    get_optional_user,
    get_settings_dep,
    load_owned_project,
)
from app.api.serialize import run_to_out
from app.config import Settings
from app.limits import QuotaExceeded
from app.models import AgentRun, DemoSession, MediaAsset, User
from app.schemas import ConfirmPlaceRequest, CreateRunResponse, RunOut
from app.services.runs import (
    PlaceInputError,
    RunConflict,
    cancel_run,
    confirm_place,
    create_retry_run,
)

logger = logging.getLogger("app.api.runs")

router = APIRouter()


def _load_owned_run(
    db: Session, run_id: str, session_row: DemoSession, user: User | None = None
) -> tuple[AgentRun, MediaAsset | None]:
    run = db.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run_not_found")
    # 文字任务（intent=answer_question）没有图片，media 允许为空
    project = load_owned_project(db, run.project_id, session_row, user)
    media = db.get(MediaAsset, run.media_asset_id) if run.media_asset_id else None
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run_not_found")
    return run, media


@router.get("/runs/{run_id}", response_model=RunOut, summary="读取运行状态")
def read_run(
    run_id: str,
    session_row: DemoSession = Depends(get_current_session),
    user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db),
) -> RunOut:
    run, _media = _load_owned_run(db, run_id, session_row, user)
    return run_to_out(run)


@router.post("/runs/{run_id}/confirm-place", response_model=RunOut, summary="确认、纠正或拒绝地点并恢复运行")
def confirm_place_endpoint(
    run_id: str,
    payload: ConfirmPlaceRequest,
    session_row: DemoSession = Depends(get_current_session),
    user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> RunOut:
    run, media = _load_owned_run(db, run_id, session_row, user)
    project = load_owned_project(db, run.project_id, session_row, user)
    try:
        confirm_place(db, run=run, media=media, project=project, payload=payload)
    except RunConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": exc.code}) from None
    except PlaceInputError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"code": exc.code}) from None

    if payload.decision != "reject":
        # 恢复同一个 AgentRun：重新排入 Redis，立即返回，不等待模型。
        enqueue_run(run.id)
    db.refresh(run)
    return run_to_out(run)


@router.post("/runs/{run_id}/cancel", response_model=RunOut, summary="取消尚未完成的运行")
def cancel_run_endpoint(
    run_id: str,
    session_row: DemoSession = Depends(get_current_session),
    user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db),
) -> RunOut:
    run, media = _load_owned_run(db, run_id, session_row, user)
    project = load_owned_project(db, run.project_id, session_row, user)
    try:
        cancel_run(db, run=run, media=media, project=project)
    except RunConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": exc.code}) from None
    db.refresh(run)
    return run_to_out(run)


@router.post(
    "/runs/{run_id}/retry",
    response_model=CreateRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="对 FAILED 或 PARTIAL 的运行创建重试运行",
)
def retry_run_endpoint(
    run_id: str,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    session_row: DemoSession = Depends(get_current_session),
    user: User | None = Depends(get_optional_user),
    client_hash: str = Depends(get_client_hash),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> CreateRunResponse:
    run, media = _load_owned_run(db, run_id, session_row, user)
    project = load_owned_project(db, run.project_id, session_row, user)
    try:
        new_run, created = create_retry_run(
            db,
            settings,
            source_run=run,
            project=project,
            media=media,
            session_row=session_row,
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
        enqueue_run(new_run.id)
    return CreateRunResponse(
        run_id=new_run.id,
        status=new_run.status,
        media_asset_id=new_run.media_asset_id,
        project_id=new_run.project_id,
        idempotent_replay=not created,
    )
