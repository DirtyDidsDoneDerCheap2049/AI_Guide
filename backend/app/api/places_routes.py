"""收藏地点、地点检索与路线 API（D3-b）。

要点：
- 地点检索直接复用 B4 的多候选搜索：结果只是候选，是否采纳由用户决定；
- 收藏去重幂等；从照片加入时只允许"已核实"的地点（未核实不能当事实收藏）；
- 路线：stops 是用户资产，算路失败只记录 FAILED 修订；改模式/排序会让旧结果标记过期。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.agent.providers import build_provider_set
from app.api.deps import (
    get_current_session,
    get_db,
    get_optional_user,
    get_settings_dep,
    load_owned_project,
)
from app.api.serialize import (
    route_draft_to_out,
    route_revision_to_out,
    saved_place_to_out,
)
from app.config import Settings
from app.models import DemoSession, GuideProject, MediaAsset, RouteDraft, SavedPlace, User
from app.schemas import (
    PlaceSearchItem,
    PlaceSearchOut,
    RouteDraftCreate,
    RouteDraftListOut,
    RouteDraftOut,
    RouteDraftUpdate,
    SavedPlaceCreate,
    SavedPlaceListOut,
    SavedPlaceOut,
)
from app.services import places_routes
from app.services.place_match import resolve_place_match

logger = logging.getLogger("app.api.places_routes")

router = APIRouter()


def _route_error(exc: places_routes.RouteError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.detail})


def _load_draft(db: Session, draft_id: str, project: GuideProject) -> RouteDraft:
    draft = db.get(RouteDraft, draft_id)
    if draft is None or draft.project_id != project.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "draft_not_found"})
    return draft


# --------------------------------------------------------------------- 地点检索


@router.get("/projects/{project_id}/places/search", response_model=PlaceSearchOut, summary="按名称检索地点候选")
def search_places(
    project_id: str,
    q: str = Query(min_length=1, max_length=120),
    city: str | None = Query(default=None, max_length=60),
    session_row: DemoSession = Depends(get_current_session),
    user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> PlaceSearchOut:
    """搜索地点候选。fixture 模式下结果带 fixture=true，不能当作真实地点数据。"""
    project = load_owned_project(db, project_id, session_row, user)
    providers = build_provider_set(settings)
    city_hint = (city or project.city_hint or "").strip() or None
    try:
        found = providers.place.search(name=q, city_hint=city_hint, limit=settings.place_search_limit)
    except Exception as exc:  # noqa: BLE001 - Provider 错误映射为 502/503
        code = getattr(exc, "code", "provider_error")
        status_code = status.HTTP_503_SERVICE_UNAVAILABLE if code == "provider_not_configured" else status.HTTP_502_BAD_GATEWAY
        raise HTTPException(status_code=status_code, detail={"code": code}) from None

    match = resolve_place_match(user_name=q, candidates=found.candidates, city_hints=[city_hint])
    kinds = match.kinds
    items = [
        PlaceSearchItem(
            rank=index,
            name=item.name,
            address=item.address,
            region=item.region,
            latitude=item.latitude,
            longitude=item.longitude,
            provider=item.provider,
            provider_place_id=item.provider_place_id,
            match_kind=kinds[index - 1] if index - 1 < len(kinds) else "other",
        )
        for index, item in enumerate(found.candidates, start=1)
    ]
    fixture = providers.mode == "mock"
    return PlaceSearchOut(query=q, provider=providers.place.name, candidates=items, fixture=fixture)


# --------------------------------------------------------------------- 收藏


@router.get("/projects/{project_id}/saved-places", response_model=SavedPlaceListOut, summary="收藏地点列表")
def list_saved_places(
    project_id: str,
    session_row: DemoSession = Depends(get_current_session),
    user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db),
) -> SavedPlaceListOut:
    project = load_owned_project(db, project_id, session_row, user)
    return SavedPlaceListOut(
        saved_places=[saved_place_to_out(item) for item in places_routes.list_saved_places(db, project.id)]
    )


@router.post(
    "/projects/{project_id}/saved-places",
    response_model=SavedPlaceOut,
    status_code=status.HTTP_201_CREATED,
    summary="收藏地点（幂等：同一地点重复收藏返回原记录）",
)
def create_saved_place(
    project_id: str,
    payload: SavedPlaceCreate,
    session_row: DemoSession = Depends(get_current_session),
    user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db),
) -> SavedPlaceOut:
    project = load_owned_project(db, project_id, session_row, user)

    if payload.media_asset_id:
        media = db.get(MediaAsset, payload.media_asset_id)
        if media is None or media.project_id != project.id or media.deleted_at is not None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "media_not_found"})
        try:
            row, _created = places_routes.save_from_media_place(db, project=project, media=media, note=payload.note)
        except places_routes.RouteError as exc:
            raise _route_error(exc) from None
        return saved_place_to_out(row)

    row, _created = places_routes.save_place(
        db,
        project=project,
        name=payload.name,
        address=payload.address,
        region=payload.region,
        latitude=payload.latitude,
        longitude=payload.longitude,
        provider=payload.provider,
        provider_place_id=payload.provider_place_id,
        source="manual",
        note=payload.note,
    )
    return saved_place_to_out(row)


@router.delete(
    "/projects/{project_id}/saved-places/{saved_place_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="取消收藏",
)
def delete_saved_place(
    project_id: str,
    saved_place_id: str,
    session_row: DemoSession = Depends(get_current_session),
    user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db),
) -> None:
    project = load_owned_project(db, project_id, session_row, user)
    saved = db.get(SavedPlace, saved_place_id)
    if saved is None or saved.project_id != project.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "saved_place_not_found"})
    places_routes.delete_saved_place(db, project=project, saved=saved)


# --------------------------------------------------------------------- 路线


@router.get("/projects/{project_id}/routes", response_model=RouteDraftListOut, summary="路线草稿列表")
def list_routes(
    project_id: str,
    session_row: DemoSession = Depends(get_current_session),
    user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db),
) -> RouteDraftListOut:
    project = load_owned_project(db, project_id, session_row, user)
    drafts = db.query(RouteDraft).filter(RouteDraft.project_id == project.id).order_by(RouteDraft.created_at).all()
    return RouteDraftListOut(drafts=[route_draft_to_out(db, draft) for draft in drafts])


@router.post(
    "/projects/{project_id}/routes",
    response_model=RouteDraftOut,
    status_code=status.HTTP_201_CREATED,
    summary="新建路线草稿（stops 是用户资产，可为空）",
)
def create_route(
    project_id: str,
    payload: RouteDraftCreate,
    session_row: DemoSession = Depends(get_current_session),
    user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db),
) -> RouteDraftOut:
    project = load_owned_project(db, project_id, session_row, user)
    try:
        draft = places_routes.create_route_draft(
            db,
            project=project,
            name=payload.name,
            mode=payload.mode,
            stops=[stop.model_dump() for stop in payload.stops],
        )
    except places_routes.RouteError as exc:
        raise _route_error(exc) from None
    return route_draft_to_out(db, draft)


@router.patch(
    "/projects/{project_id}/routes/{draft_id}",
    response_model=RouteDraftOut,
    summary="改模式 / 重新排序 / 增删停留点（会让旧算路结果标记过期）",
)
def update_route(
    project_id: str,
    draft_id: str,
    payload: RouteDraftUpdate,
    session_row: DemoSession = Depends(get_current_session),
    user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db),
) -> RouteDraftOut:
    project = load_owned_project(db, project_id, session_row, user)
    draft = _load_draft(db, draft_id, project)
    try:
        updated = places_routes.update_route_draft(
            db,
            project=project,
            draft=draft,
            mode=payload.mode,
            stops=[stop.model_dump() for stop in payload.stops] if payload.stops is not None else None,
            name=payload.name,
            expected_version=payload.expected_version,
        )
    except places_routes.RouteError as exc:
        raise _route_error(exc) from None
    return route_draft_to_out(db, updated)


@router.post(
    "/projects/{project_id}/routes/{draft_id}/compute",
    response_model=RouteDraftOut,
    summary="算路（失败只写 FAILED 修订，停留点保留，可重试）",
)
def compute_route(
    project_id: str,
    draft_id: str,
    session_row: DemoSession = Depends(get_current_session),
    user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> RouteDraftOut:
    project = load_owned_project(db, project_id, session_row, user)
    draft = _load_draft(db, draft_id, project)
    providers = build_provider_set(settings)
    try:
        places_routes.compute_route(db, project=project, draft=draft, providers=providers)
    except places_routes.RouteError as exc:
        raise _route_error(exc) from None
    db.refresh(draft)
    return route_draft_to_out(db, draft)


@router.delete(
    "/projects/{project_id}/routes/{draft_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除路线草稿",
)
def delete_route(
    project_id: str,
    draft_id: str,
    session_row: DemoSession = Depends(get_current_session),
    user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db),
) -> None:
    project = load_owned_project(db, project_id, session_row, user)
    draft = _load_draft(db, draft_id, project)
    db.delete(draft)
    db.commit()


@router.get(
    "/projects/{project_id}/routes/{draft_id}/revisions",
    summary="路线修订历史（含失败与过期记录）",
)
def list_revisions(
    project_id: str,
    draft_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    session_row: DemoSession = Depends(get_current_session),
    user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    project = load_owned_project(db, project_id, session_row, user)
    draft = _load_draft(db, draft_id, project)
    from sqlalchemy import select

    from app.models import RouteRevision

    rows = list(
        db.execute(
            select(RouteRevision)
            .where(RouteRevision.draft_id == draft.id)
            .order_by(RouteRevision.seq.desc())
            .limit(limit)
        ).scalars()
    )
    return {
        "draft_id": draft.id,
        "input_version": draft.input_version,
        "revisions": [route_revision_to_out(row, draft).model_dump() for row in rows],
    }
