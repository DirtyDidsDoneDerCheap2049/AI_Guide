"""收藏地点与路线草稿服务（D3-b）。

设计原则：
- **草稿的停留点列表是用户资产**：算路失败只写一条 FAILED 修订，stops 永远不动。
- **不拿过期结果冒充现状**：修订记录产生它的 `input_version`；草稿改过（重新排序/换模式/增减点）后，
  旧修订标记 `stale`，只有与当前 `input_version` 一致的修订才被当作"当前路线"。
- **距离/时长/几何只来自供应商**：拿不到就失败，不用直线距离估算。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agent.providers.base import ProviderError, ProviderSet
from app.events import EventType, append_event
from app.models import (
    GuideProject,
    MediaAsset,
    Place,
    RouteDraft,
    RouteRevision,
    SavedPlace,
    utcnow,
)

logger = logging.getLogger("app.services.places_routes")

MODES = ("walking", "driving")


class RouteError(RuntimeError):
    def __init__(self, code: str, *, status_code: int = 409, detail: str | None = None) -> None:
        self.code = code
        self.status_code = status_code
        self.detail = detail
        super().__init__(code)


def _dedupe_key(*, provider_place_id: str | None, name: str, region: str | None) -> str:
    if provider_place_id:
        return f"poi:{provider_place_id}"[:160]
    return f"name:{name.strip().lower()}|{(region or '').strip().lower()}"[:160]


# --------------------------------------------------------------------------- 收藏


def save_place(
    db: Session,
    *,
    project: GuideProject,
    name: str,
    address: str | None = None,
    region: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    provider: str = "user",
    provider_place_id: str | None = None,
    source: str = "manual",
    note: str | None = None,
    place_id: str | None = None,
    media_asset_id: str | None = None,
) -> tuple[SavedPlace, bool]:
    """收藏一个地点。同一地点重复收藏返回原记录（幂等），并补齐缺失的坐标。"""
    key = _dedupe_key(provider_place_id=provider_place_id, name=name, region=region)
    existing = db.execute(
        select(SavedPlace).where(SavedPlace.project_id == project.id, SavedPlace.dedupe_key == key)
    ).scalar_one_or_none()
    if existing is not None:
        if existing.latitude is None and latitude is not None:
            existing.latitude = latitude
            existing.longitude = longitude
        if existing.address is None and address:
            existing.address = address
        if note and existing.note != note:
            existing.note = note
        existing.updated_at = utcnow()
        db.commit()
        return existing, False

    position = int(
        db.execute(
            select(func.coalesce(func.max(SavedPlace.position), 0)).where(SavedPlace.project_id == project.id)
        ).scalar()
        or 0
    ) + 1
    row = SavedPlace(
        project_id=project.id,
        place_id=place_id,
        media_asset_id=media_asset_id,
        dedupe_key=key,
        name=name.strip(),
        address=address,
        region=region,
        latitude=latitude,
        longitude=longitude,
        provider=provider,
        provider_place_id=provider_place_id,
        source=source,
        note=note,
        position=position,
    )
    db.add(row)
    append_event(
        db,
        project_id=project.id,
        type=EventType.PLACE_SAVED,
        payload={"saved_place_id": row.id, "name": row.name, "source": source},
        media_asset_id=media_asset_id,
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        again = db.execute(
            select(SavedPlace).where(SavedPlace.project_id == project.id, SavedPlace.dedupe_key == key)
        ).scalar_one_or_none()
        if again is not None:
            return again, False
        raise
    return row, True


def save_from_media_place(
    db: Session, *, project: GuideProject, media: MediaAsset, note: str | None = None
) -> tuple[SavedPlace, bool]:
    """把照片上已确认的地点加入收藏（未核实的地点不允许收藏为事实）。"""
    place = db.execute(select(Place).where(Place.media_asset_id == media.id)).scalar_one_or_none()
    if place is None:
        raise RouteError("place_not_confirmed", status_code=409)
    verified = ("matched", "matched_name_only", "user_selected")
    if place.match_status not in verified:
        raise RouteError("place_not_verified", status_code=409, detail=f"match_status={place.match_status}")
    return save_place(
        db,
        project=project,
        name=place.name,
        address=place.address,
        region=place.region,
        latitude=place.latitude,
        longitude=place.longitude,
        provider=place.provider,
        provider_place_id=place.provider_place_id,
        source="photo",
        note=note,
        place_id=place.id,
        media_asset_id=media.id,
    )


def delete_saved_place(db: Session, *, project: GuideProject, saved: SavedPlace) -> None:
    append_event(
        db,
        project_id=project.id,
        type=EventType.PLACE_UNSAVED,
        payload={"saved_place_id": saved.id, "name": saved.name},
        media_asset_id=saved.media_asset_id,
    )
    db.delete(saved)
    db.commit()


def list_saved_places(db: Session, project_id: str) -> list[SavedPlace]:
    return list(
        db.execute(
            select(SavedPlace)
            .where(SavedPlace.project_id == project_id)
            .order_by(SavedPlace.position, SavedPlace.created_at)
        ).scalars()
    )


# --------------------------------------------------------------------------- 路线草稿


def create_route_draft(
    db: Session, *, project: GuideProject, name: str, mode: str = "walking", stops: list[dict[str, Any]] | None = None
) -> RouteDraft:
    if mode not in MODES:
        raise RouteError("unsupported_mode", status_code=422)
    draft = RouteDraft(project_id=project.id, name=name.strip(), mode=mode, stops=stops or [])
    db.add(draft)
    append_event(
        db,
        project_id=project.id,
        type=EventType.ROUTE_DRAFT_CREATED,
        payload={"draft_id": draft.id, "name": draft.name, "mode": mode},
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise RouteError("draft_name_taken", status_code=409) from None
    return draft


def _stale_previous_revisions(db: Session, draft: RouteDraft) -> int:
    """草稿输入变化后，把不再代表当前输入的修订标记为过期（保留历史）。"""
    count = 0
    for revision in db.execute(
        select(RouteRevision).where(RouteRevision.draft_id == draft.id, RouteRevision.stale.is_(False))
    ).scalars():
        if revision.input_version != draft.input_version:
            revision.stale = True
            count += 1
    return count


def update_route_draft(
    db: Session,
    *,
    project: GuideProject,
    draft: RouteDraft,
    mode: str | None = None,
    stops: list[dict[str, Any]] | None = None,
    name: str | None = None,
    expected_version: int | None = None,
) -> RouteDraft:
    """改模式 / 重新排序 / 增删停留点。任何改动都会让 input_version +1 并让旧修订过期。"""
    db.refresh(draft, with_for_update=True)
    if expected_version is not None and draft.input_version != expected_version:
        raise RouteError("draft_version_conflict", status_code=409)
    if mode is not None and mode not in MODES:
        raise RouteError("unsupported_mode", status_code=422)

    changed = False
    if name is not None and name.strip() and name.strip() != draft.name:
        draft.name = name.strip()
        changed = True
    if mode is not None and mode != draft.mode:
        draft.mode = mode
        changed = True
    if stops is not None:
        if len(stops) > 20:
            raise RouteError("too_many_stops", status_code=422)
        if stops != (draft.stops or []):
            draft.stops = stops
            changed = True

    if changed:
        draft.input_version += 1
        draft.updated_at = utcnow()
        stale = _stale_previous_revisions(db, draft)
        append_event(
            db,
            project_id=project.id,
            type=EventType.ROUTE_DRAFT_UPDATED,
            payload={
                "draft_id": draft.id,
                "mode": draft.mode,
                "stops": len(draft.stops or []),
                "input_version": draft.input_version,
                "stale_revisions": stale,
            },
        )
    db.commit()
    return draft


def draft_points(draft: RouteDraft) -> list[tuple[float, float]]:
    """从 stops 提取 GCJ-02 坐标；缺坐标的停留点直接报错（不能拿 0,0 当真）。"""
    points: list[tuple[float, float]] = []
    for index, stop in enumerate(draft.stops or []):
        latitude = stop.get("latitude")
        longitude = stop.get("longitude")
        if latitude is None or longitude is None:
            raise RouteError("stop_missing_coordinates", status_code=422, detail=f"stop_index={index}")
        points.append((float(longitude), float(latitude)))
    return points


def compute_route(
    db: Session,
    *,
    project: GuideProject,
    draft: RouteDraft,
    providers: ProviderSet,
) -> RouteRevision:
    """算一次路：成功写 OK 修订并更新 current_revision_id；失败写 FAILED 修订但保留 stops。

    距离与时长来自供应商（高德）；步行按"不支持途经点"拆成多段调用，分段结果落在 legs 里。
    """
    if providers.direction is None:
        raise RouteError("direction_provider_not_configured", status_code=503)

    points = draft_points(draft)
    if len(points) < 2:
        raise RouteError("need_at_least_two_stops", status_code=422)

    # 同一草稿的序号在行锁内分配：并发算路不会拿到同一个 seq，排序也稳定
    db.execute(select(RouteDraft.id).where(RouteDraft.id == draft.id).with_for_update())
    next_seq = int(
        db.execute(
            select(func.coalesce(func.max(RouteRevision.seq), 0)).where(RouteRevision.draft_id == draft.id)
        ).scalar()
        or 0
    ) + 1

    revision = RouteRevision(
        draft_id=draft.id,
        project_id=project.id,
        seq=next_seq,
        input_version=draft.input_version,
        mode=draft.mode,
        status="FAILED",
        provider=providers.direction.name,
    )
    db.add(revision)
    db.flush()

    legs: list[dict[str, Any]] = []
    geometry_parts: list[str] = []
    distance = 0
    duration = 0
    provider_route_id: str | None = None
    try:
        for index in range(len(points) - 1):
            result = providers.direction.route(
                mode=draft.mode,
                origin=points[index],
                destination=points[index + 1],
                waypoints=None,
            )
            legs.append(
                {
                    "from": index,
                    "to": index + 1,
                    "distance_meters": result.distance_meters,
                    "duration_seconds": result.duration_seconds,
                    "polyline": result.geometry,
                    "provider": result.provider,
                }
            )
            geometry_parts.append(result.geometry)
            distance += result.distance_meters
            duration += result.duration_seconds
            provider_route_id = provider_route_id or str((result.raw or {}).get("strategy") or "") or None
    except ProviderError as exc:
        revision.status = "FAILED"
        revision.error_code = exc.code
        revision.error_message = (exc.detail or "")[:300]
        db.commit()
        logger.warning("route_compute_failed", extra={"draft_id": draft.id, "error_code": exc.code})
        return revision
    except Exception as exc:  # noqa: BLE001 - 未知错误同样留痕
        revision.status = "FAILED"
        revision.error_code = "unexpected_error"
        revision.error_message = type(exc).__name__
        db.commit()
        raise

    revision.status = "OK"
    revision.distance_meters = distance
    revision.duration_seconds = duration
    revision.legs = legs
    revision.geometry = ";".join(part for part in geometry_parts if part)
    revision.provider_route_id = provider_route_id
    revision.computed_at = utcnow()
    revision.error_code = None
    revision.error_message = None
    # 只有当这次输入仍然是最新输入时才成为"当前路线"
    if revision.input_version != draft.input_version:
        revision.stale = True
    else:
        draft.current_revision_id = revision.id
        draft.updated_at = utcnow()
    append_event(
        db,
        project_id=project.id,
        type=EventType.ROUTE_COMPUTED,
        payload={
            "draft_id": draft.id,
            "revision_id": revision.id,
            "status": revision.status,
            "mode": revision.mode,
            "distance_meters": revision.distance_meters,
            "duration_seconds": revision.duration_seconds,
            "input_version": revision.input_version,
            "stale": revision.stale,
        },
    )
    db.commit()
    return revision


def current_revision(db: Session, draft: RouteDraft) -> RouteRevision | None:
    """当前路线：必须与草稿输入版本一致且未被标记过期。"""
    if draft.current_revision_id:
        revision = db.get(RouteRevision, draft.current_revision_id)
        if revision is not None and not revision.stale and revision.input_version == draft.input_version:
            return revision
    return None


def latest_revision(db: Session, draft: RouteDraft) -> RouteRevision | None:
    # seq 是自增列，同一秒内的多次算路也能稳定排序
    return db.execute(
        select(RouteRevision)
        .where(RouteRevision.draft_id == draft.id)
        .order_by(RouteRevision.seq.desc())
    ).scalars().first()


@dataclass
class RouteSummary:
    """给接口用的汇总（含"当前路线是否过期"这一关键诚实信息）。"""

    draft: RouteDraft
    current: RouteRevision | None
    latest: RouteRevision | None

    @property
    def is_stale(self) -> bool:
        return self.current is None and self.latest is not None and self.latest.status == "OK"


def summarize(db: Session, draft: RouteDraft) -> RouteSummary:
    return RouteSummary(draft=draft, current=current_revision(db, draft), latest=latest_revision(db, draft))
