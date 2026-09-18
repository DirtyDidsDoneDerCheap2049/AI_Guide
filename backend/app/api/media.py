"""媒体 API：受限图片上传（实际内容校验、数量上限、事务一致性）与受控下载。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
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
from app.api.serialize import media_to_out, place_to_out, run_to_out
from app.config import Settings
from app.limits import QuotaExceeded
from app.models import DemoSession, GuideProject, MediaAsset, RunStatus, User
from app.schemas import (
    MediaNoteRequest,
    MediaOperationResponse,
    MediaUploadResponse,
    PlaceChangeRequest,
)
from app.services.media import (
    IdempotencyConflict,
    MediaLimitReached,
    MediaValidationError,
    upload_media_command,
)
from app.services import media_ops
from app.storage import storage_path

logger = logging.getLogger("app.api.media")

router = APIRouter()


def _load_owned_media(
    db: Session, media_id: str, session_row: DemoSession, user: User | None = None
) -> tuple[MediaAsset, GuideProject]:
    media = db.get(MediaAsset, media_id)
    if media is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="media_not_found")
    project = load_owned_project(db, media.project_id, session_row, user)
    return media, project


@router.post(
    "/projects/{project_id}/media",
    response_model=MediaUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="上传一张图片（JPG/PNG/WebP，单文件 8MB，项目最多 3 张）",
)
def upload_media(
    project_id: str,
    file: UploadFile = File(..., description="图片文件"),
    start_analysis: bool = Form(True, description="是否立即创建分析运行"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    session_row: DemoSession = Depends(get_current_session),
    user: User | None = Depends(get_optional_user),
    client_hash: str = Depends(get_client_hash),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> MediaUploadResponse:
    """上传命令是幂等的：同一 Idempotency-Key + 同一内容重放原结果，内容不同返回 409。

    文件、媒体记录、额度预留、运行与事件在**同一个事务**里提交（B1）。
    """
    project = load_owned_project(db, project_id, session_row, user)

    # The command uses synchronous MySQL/file I/O. Run the endpoint in FastAPI's
    # thread pool so a contended row lock cannot block the ASGI event loop.
    data = file.file.read(settings.media_max_bytes + 1)
    if len(data) > settings.media_max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={"code": "file_too_large", "limit": settings.media_max_bytes},
        )

    try:
        outcome = upload_media_command(
            db,
            settings,
            project=project,
            session_row=session_row,
            client_hash=client_hash,
            data=data,
            filename=file.filename or "upload",
            declared_mime=file.content_type,
            start_analysis=start_analysis,
            idempotency_key=idempotency_key,
        )
    except IdempotencyConflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail={"code": "idempotency_key_conflict"}
        ) from None
    except MediaLimitReached:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "media_limit_reached", "limit": settings.media_max_per_project},
        ) from None
    except QuotaExceeded as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "quota_exceeded", "scope": exc.scope, "limit": exc.limit},
        ) from None
    except MediaValidationError as exc:
        code = (
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
            if exc.code == "file_too_large"
            else status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
        )
        raise HTTPException(status_code=code, detail={"code": exc.code}) from None

    if outcome.run_created and outcome.run is not None:
        enqueue_run(outcome.run.id)

    return MediaUploadResponse(
        media=media_to_out(outcome.media),
        run=run_to_out(outcome.run) if outcome.run else None,
    )




def _operation_errors(exc: Exception) -> HTTPException:
    if isinstance(exc, media_ops.MediaOperationError):
        status_code = {
            404: status.HTTP_404_NOT_FOUND,
            409: status.HTTP_409_CONFLICT,
            422: status.HTTP_422_UNPROCESSABLE_ENTITY,
        }.get(exc.status_code, status.HTTP_409_CONFLICT)
        return HTTPException(status_code=status_code, detail={"code": exc.code})
    if isinstance(exc, QuotaExceeded):
        return HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "quota_exceeded", "scope": exc.scope, "limit": exc.limit},
        )
    raise exc


@router.post(
    "/media/{media_id}/analyze",
    response_model=MediaOperationResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="对已保存的照片新建分析运行（B5：start_analysis=false 的补充入口）",
)
def analyze_media(
    media_id: str,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    session_row: DemoSession = Depends(get_current_session),
    user: User | None = Depends(get_optional_user),
    client_hash: str = Depends(get_client_hash),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> MediaOperationResponse:
    media, project = _load_owned_media(db, media_id, session_row, user)
    try:
        run, created = media_ops.start_analysis_for_media(
            db,
            settings,
            project=project,
            media=media,
            session_row=session_row,
            idempotency_key=idempotency_key,
            client_hash=client_hash,
        )
    except Exception as exc:  # noqa: BLE001 - 统一映射成 HTTP 错误
        raise _operation_errors(exc) from None
    if created:
        enqueue_run(run.id)
    db.refresh(media)
    return MediaOperationResponse(media=media_to_out(media), run=run_to_out(run))


@router.patch(
    "/media/{media_id}",
    response_model=MediaOperationResponse,
    summary="写入用户笔记（模型不覆盖）",
)
def update_media(
    media_id: str,
    payload: MediaNoteRequest,
    session_row: DemoSession = Depends(get_current_session),
    user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db),
) -> MediaOperationResponse:
    media, project = _load_owned_media(db, media_id, session_row, user)
    try:
        media_ops.update_media_note(db, project=project, media=media, note=payload.note)
    except Exception as exc:  # noqa: BLE001
        raise _operation_errors(exc) from None
    db.refresh(media)
    return MediaOperationResponse(media=media_to_out(media))


@router.post(
    "/media/{media_id}/place",
    response_model=MediaOperationResponse,
    summary="修改照片地点（版本化；可选立即重新生成讲解）",
)
def change_media_place(
    media_id: str,
    payload: PlaceChangeRequest,
    session_row: DemoSession = Depends(get_current_session),
    user: User | None = Depends(get_optional_user),
    client_hash: str = Depends(get_client_hash),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> MediaOperationResponse:
    media, project = _load_owned_media(db, media_id, session_row, user)
    try:
        place, run, _created = media_ops.change_media_place(
            db,
            settings,
            project=project,
            media=media,
            name=payload.name,
            address=payload.address,
            region=payload.region,
            expected_version=payload.expected_version,
            regenerate=payload.regenerate,
            session_row=session_row,
            client_hash=client_hash,
        )
    except Exception as exc:  # noqa: BLE001
        raise _operation_errors(exc) from None
    db.refresh(media)
    return MediaOperationResponse(
        media=media_to_out(media),
        place=place_to_out(place),
        run=run_to_out(run) if run is not None else None,
    )


@router.delete(
    "/media/{media_id}",
    response_model=MediaOperationResponse,
    summary="把照片移出相册（软删除，可恢复；未完成任务同时取消）",
)
def delete_media(
    media_id: str,
    session_row: DemoSession = Depends(get_current_session),
    user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db),
) -> MediaOperationResponse:
    media, project = _load_owned_media(db, media_id, session_row, user)
    media_ops.soft_delete_media(db, project=project, media=media, session_row=session_row)
    db.refresh(media)
    return MediaOperationResponse(media=media_to_out(media))


@router.post(
    "/media/{media_id}/restore",
    response_model=MediaOperationResponse,
    summary="恢复被移出相册的照片",
)
def restore_media(
    media_id: str,
    session_row: DemoSession = Depends(get_current_session),
    user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db),
) -> MediaOperationResponse:
    media, project = _load_owned_media(db, media_id, session_row, user)
    media_ops.restore_media(db, project=project, media=media)
    db.refresh(media)
    return MediaOperationResponse(media=media_to_out(media))


@router.post(
    "/media/{media_id}/runs/retry",
    response_model=MediaOperationResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="从该照片最近一次失败/局部/已取消的运行重做（终态运行不复活）",
)
def retry_media_run(
    media_id: str,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    session_row: DemoSession = Depends(get_current_session),
    user: User | None = Depends(get_optional_user),
    client_hash: str = Depends(get_client_hash),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> MediaOperationResponse:
    from sqlalchemy import select

    from app.models import AgentRun

    media, project = _load_owned_media(db, media_id, session_row, user)
    source = db.execute(
        select(AgentRun)
        .where(
            AgentRun.media_asset_id == media.id,
            AgentRun.status.in_((RunStatus.FAILED, RunStatus.PARTIAL, RunStatus.CANCELLED)),
        )
        .order_by(AgentRun.created_at.desc())
    ).scalars().first()
    if source is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": "no_retryable_run"})
    try:
        run, created = media_ops.retry_run(
            db,
            settings,
            project=project,
            media=media,
            source_run=source,
            session_row=session_row,
            idempotency_key=idempotency_key,
            client_hash=client_hash,
        )
    except Exception as exc:  # noqa: BLE001
        raise _operation_errors(exc) from None
    if created:
        enqueue_run(run.id)
    db.refresh(media)
    return MediaOperationResponse(media=media_to_out(media), run=run_to_out(run))


@router.get("/projects/{project_id}/media/deleted", response_model=list, summary="列出已移出相册的照片")
def list_deleted_media(
    project_id: str,
    session_row: DemoSession = Depends(get_current_session),
    user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db),
) -> list:
    from sqlalchemy import select

    project = load_owned_project(db, project_id, session_row, user)
    rows = db.execute(
        select(MediaAsset)
        .where(MediaAsset.project_id == project.id, MediaAsset.deleted_at.is_not(None))
        .order_by(MediaAsset.deleted_at.desc())
    ).scalars()
    return [media_to_out(item) for item in rows]


@router.get("/media/{media_id}/content", summary="读取属于当前会话的图片")
def read_media_content(
    media_id: str,
    download: bool = Query(False, description="true 时作为附件下载"),
    session_row: DemoSession = Depends(get_current_session),
    user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> FileResponse:
    media, _project = _load_owned_media(db, media_id, session_row, user)
    try:
        path = storage_path(settings.media_root, media.storage_key)
    except MediaValidationError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="media_not_found") from None
    if not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="media_file_missing")

    disposition = "attachment" if download else "inline"
    return FileResponse(
        path,
        media_type=media.mime_type,
        headers={
            "Content-Disposition": f'{disposition}; filename="{media.id}.{media.mime_type.split("/")[-1]}"',
            "Cache-Control": "private, max-age=60",
        },
    )
