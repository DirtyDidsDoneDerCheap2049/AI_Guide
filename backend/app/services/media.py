"""媒体上传命令（B1）：把“上传照片 + 可选创建任务”当作**一条**幂等命令。

07 号报告的问题：旧实现里媒体提交与 run 创建是两次事务，幂等键只加在 run 唯一键上，
于是“同一 key + 同一图片”会创建两条媒体、只建一个 run，第二次响应里的 run 还指向第一张媒体；
重复请求还会撞三张上限而不是重放原响应。

本模块的做法：
1. 计算请求指纹（内容 sha256 + start_analysis + 目标项目）。
2. 同一 (owner_session_id, idempotency_key) 用 ``SELECT ... FOR UPDATE`` 串行化：
   - 指纹相同 → 重放已存的响应（不再写文件、不再扣额度）；
   - 指纹不同 → 抛 :class:`IdempotencyConflict`（API 映射 409）。
3. 文件先落盘，随后**单事务**写入媒体、额度预留、运行、事件与命令记录；失败则删文件回滚。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.events import EventType, append_event
from app.limits import QuotaExceeded, reserve_run_quota
from app.models import (
    AgentRun,
    DemoSession,
    GuideProject,
    MediaAsset,
    RunStatus,
    StepName,
    UploadCommand,
    utcnow,
)
from app.storage import MediaValidationError, delete_stored_file, store_image, validate_image_bytes

logger = logging.getLogger("app.services.media")


class IdempotencyConflict(RuntimeError):
    """同一幂等键提交了不同内容。"""

    code = "idempotency_key_conflict"


class MediaLimitReached(RuntimeError):
    code = "media_limit_reached"


@dataclass
class UploadOutcome:
    media: MediaAsset
    run: AgentRun | None
    replayed: bool
    run_created: bool


def request_fingerprint(data: bytes, project_id: str, start_analysis: bool) -> str:
    digest = hashlib.sha256()
    digest.update(data)
    digest.update(b"|")
    digest.update(project_id.encode())
    digest.update(b"|")
    digest.update(b"analysis" if start_analysis else b"store-only")
    return digest.hexdigest()


def _active_media_count(db: Session, project_id: str) -> int:
    return int(
        db.execute(
            select(func.count(MediaAsset.id)).where(
                MediaAsset.project_id == project_id, MediaAsset.deleted_at.is_(None)
            )
        ).scalar()
        or 0
    )


def _reserve_next_position(db: Session, project_id: str) -> int:
    """删除后不再复用旧 position，避免唯一约束冲突。"""
    current = db.execute(
        select(func.max(MediaAsset.position)).where(MediaAsset.project_id == project_id)
    ).scalar()
    return int(current or 0) + 1


def upload_media_command(
    db: Session,
    settings: Settings,
    *,
    project: GuideProject,
    session_row: DemoSession,
    client_hash: str | None,
    data: bytes,
    filename: str,
    declared_mime: str | None,
    start_analysis: bool,
    idempotency_key: str | None,
) -> UploadOutcome:
    key = (idempotency_key or "").strip() or None
    info = validate_image_bytes(data, settings, declared_mime)
    fingerprint = request_fingerprint(data, project.id, start_analysis)

    # Lock existing rows before probing a potentially absent command key. Two
    # gap locks on the missing key cannot safely serialize concurrent inserts.
    db.execute(select(DemoSession.id).where(DemoSession.id == session_row.id).with_for_update()).first()
    db.refresh(project, with_for_update=True)

    # --- 1) 幂等命中检查（行锁串行化同一 key 的并发请求） ---
    if key:
        existing = db.execute(
            select(UploadCommand)
            .where(UploadCommand.owner_session_id == session_row.id, UploadCommand.idempotency_key == key)
            .with_for_update()
        ).scalar_one_or_none()
        if existing is not None:
            if existing.request_fingerprint != fingerprint:
                raise IdempotencyConflict()
            # Current reads are required after waiting: a REPEATABLE READ
            # snapshot created by ownership lookup may predate the first upload.
            media = db.execute(select(MediaAsset).where(MediaAsset.id == existing.media_asset_id).with_for_update()).scalar_one_or_none() if existing.media_asset_id else None
            run = db.execute(select(AgentRun).where(AgentRun.id == existing.run_id).with_for_update()).scalar_one_or_none() if existing.run_id else None
            if media is None:
                # 极少数情况：媒体已被删除，重放没有意义，按冲突处理而不是返回坏数据。
                raise IdempotencyConflict()
            logger.info("upload_replayed", extra={"media_id": media.id, "run_id": existing.run_id})
            return UploadOutcome(media=media, run=run, replayed=True, run_created=False)

    # --- 2) 容量与额度：写文件之前先判定 ---
    active_ids = db.execute(select(MediaAsset.id).where(MediaAsset.project_id == project.id, MediaAsset.deleted_at.is_(None)).with_for_update()).scalars().all()
    if len(active_ids) >= settings.media_max_per_project:
        raise MediaLimitReached()

    # --- 3) 文件落盘；后续任何失败都要删掉它 ---
    storage_key = store_image(data, project.id, info, settings.media_root)
    try:
        # 锁项目行，避免并发上传算出相同 position
        db.execute(select(GuideProject.id).where(GuideProject.id == project.id).with_for_update()).first()

        media = MediaAsset(
            project_id=project.id,
            storage_key=storage_key,
            original_filename=filename[:255],
            sha256=info.sha256,
            mime_type=info.mime_type,
            size_bytes=info.size_bytes,
            width=info.width,
            height=info.height,
            status="UPLOADED",
            position=max(db.execute(select(MediaAsset.position).where(MediaAsset.project_id == project.id).with_for_update()).scalars(), default=0) + 1,
        )
        db.add(media)
        db.flush()

        run: AgentRun | None = None
        if start_analysis:
            # B6：额度与业务写入同一事务，超额则整体回滚（文件也会被删）
            reserve_run_quota(db, settings, session_row, client_hash)
            run = AgentRun(
                project_id=project.id,
                media_asset_id=media.id,
                status=RunStatus.QUEUED,
                current_step=StepName.ANALYZE_IMAGE,
                trigger="initial",
                intent="analyze_image",
                input_snapshot={
                    "media_id": media.id,
                    "mime_type": media.mime_type,
                    "size_bytes": media.size_bytes,
                    "sha256": media.sha256,
                    "city_hint": project.city_hint,
                    "position": media.position,
                },
                idempotency_key=key,
                budget_max_steps=settings.run_max_steps,
                budget_max_tool_calls=settings.run_max_tool_calls,
                budget_max_tokens=settings.run_max_tokens,
                budget_max_cost=settings.run_max_cost,
            )
            db.add(run)
            media.status = "QUEUED"
            db.flush()
            session_row.runs_started += 1
            project.version += 1
            project.updated_at = utcnow()
            append_event(
                db,
                project_id=project.id,
                type=EventType.RUN_QUEUED,
                payload={"trigger": "initial", "media_asset_id": media.id},
                run_id=run.id,
                media_asset_id=media.id,
            )

        append_event(
            db,
            project_id=project.id,
            type=EventType.MEDIA_UPLOADED,
            payload={
                "media_id": media.id,
                "position": media.position,
                "mime_type": media.mime_type,
                "size_bytes": media.size_bytes,
            },
            media_asset_id=media.id,
        )

        if key:
            db.add(
                UploadCommand(
                    owner_session_id=session_row.id,
                    project_id=project.id,
                    idempotency_key=key,
                    request_fingerprint=fingerprint,
                    status="COMPLETED",
                    media_asset_id=media.id,
                    run_id=run.id if run else None,
                )
            )

        db.commit()
        db.refresh(media)
        if run is not None:
            db.refresh(run)
        logger.info(
            "media_uploaded",
            extra={
                "media_id": media.id,
                "project_id": project.id,
                "position": media.position,
                "run_created": run is not None,
                "idempotent_key": bool(key),
            },
        )
        return UploadOutcome(media=media, run=run, replayed=False, run_created=run is not None)
    except IntegrityError:
        db.rollback()
        delete_stored_file(settings.media_root, storage_key)
        # 并发同 key 竞态：另一个请求已写入命令记录 → 重放它的结果
        if key:
            existing = db.execute(
                select(UploadCommand).where(
                    UploadCommand.owner_session_id == session_row.id, UploadCommand.idempotency_key == key
                )
            ).scalar_one_or_none()
            if existing is not None and existing.media_asset_id:
                media = db.get(MediaAsset, existing.media_asset_id)
                run = db.get(AgentRun, existing.run_id) if existing.run_id else None
                if media is not None:
                    return UploadOutcome(media=media, run=run, replayed=True, run_created=False)
        raise
    except Exception:
        db.rollback()
        delete_stored_file(settings.media_root, storage_key)
        raise


def mark_upload_failed(db: Session, session_row: DemoSession, key: str, reason: str) -> None:
    """记录一次失败的上传命令，便于审计（不参与幂等命中）。"""
    db.execute(
        text(
            "UPDATE upload_commands SET status = :status, updated_at = UTC_TIMESTAMP() "
            "WHERE owner_session_id = :owner AND idempotency_key = :key"
        ),
        {"status": f"FAILED:{reason}"[:16], "owner": session_row.id, "key": key},
    )


__all__ = [
    "IdempotencyConflict",
    "MediaLimitReached",
    "MediaValidationError",
    "QuotaExceeded",
    "UploadOutcome",
    "request_fingerprint",
    "upload_media_command",
]
