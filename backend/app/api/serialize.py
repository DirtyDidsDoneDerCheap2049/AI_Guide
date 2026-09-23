"""ORM 对象 -> API DTO 的转换。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AgentRun,
    GuideCard,
    GuideProject,
    MediaAsset,
    Message,
    Place,
    RouteDraft,
    RouteRevision,
    SavedPlace,
    User,
    PlaceCandidate,
    PlaceMatchCandidate,
    RunStatus,
    WorkspaceEvent,
)
from app.schemas import (
    CandidateOut,
    EventOut,
    GuideCardOut,
    MediaAssetOut,
    MediaSnapshot,
    MessageOut,
    PlaceOut,
    ProjectOut,
    ProviderPlaceCandidateOut,
    RouteDraftOut,
    RouteRevisionOut,
    RouteStop,
    SavedPlaceOut,
    UserOut,
    ProjectSnapshot,
    RunOut,
)


def media_content_url(media_id: str) -> str:
    # 同源相对路径：浏览器包中不出现主机名、IP 或固定域名。
    return f"/api/v1/media/{media_id}/content"


def media_to_out(media: MediaAsset) -> MediaAssetOut:
    return MediaAssetOut(
        id=media.id,
        project_id=media.project_id,
        position=media.position,
        status=media.status,
        mime_type=media.mime_type,
        size_bytes=media.size_bytes,
        width=media.width,
        height=media.height,
        original_filename=media.original_filename,
        content_url=media_content_url(media.id),
        created_at=media.created_at,
        note=media.note,
        version=media.version,
        deleted=media.deleted_at is not None,
        active_run_id=media.active_run_id,
    )


def project_to_out(project: GuideProject, media_count: int = 0) -> ProjectOut:
    return ProjectOut(
        id=project.id,
        title=project.title,
        city_hint=project.city_hint,
        status=project.status,
        version=project.version,
        media_count=media_count,
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


def run_to_out(run: AgentRun) -> RunOut:
    selection = (run.input_snapshot or {}).get("model_selection") or {}
    return RunOut(
        thinking_level=selection.get("thinking_level"),
        model_id=selection.get("id"),
        model_label=selection.get("label"),
        thinking=selection.get("thinking"),
        id=run.id,
        project_id=run.project_id,
        media_asset_id=run.media_asset_id,
        status=run.status,
        current_step=run.current_step,
        trigger=run.trigger,
        attempt=run.attempt,
        error_code=run.error_code,
        error_message=run.error_message,
        used_steps=run.used_steps,
        used_tool_calls=run.used_tool_calls,
        used_tokens=run.used_tokens,
        used_cost=float(run.used_cost or 0),
        budget_max_steps=run.budget_max_steps,
        budget_max_tool_calls=run.budget_max_tool_calls,
        budget_max_tokens=run.budget_max_tokens,
        budget_max_cost=float(run.budget_max_cost or 0),
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def candidate_to_out(candidate: PlaceCandidate) -> CandidateOut:
    return CandidateOut(
        id=candidate.id,
        rank=candidate.rank,
        name=candidate.name,
        address=candidate.address,
        region=candidate.region,
        rationale=candidate.rationale,
        confidence=candidate.confidence,
        uncertainty=candidate.uncertainty,
        status=candidate.status,
        run_id=candidate.run_id,
    )


def place_to_out(place: Place) -> PlaceOut:
    return PlaceOut(
        id=place.id,
        name=place.name,
        address=place.address,
        region=place.region,
        latitude=place.latitude,
        longitude=place.longitude,
        provider=place.provider,
        provider_place_id=place.provider_place_id,
        query_at=place.query_at,
        confirmed_by=place.confirmed_by,
        match_status=place.match_status,
        match_note=place.match_note,
        source=place.source,
        version=place.version,
    )


def provider_candidate_to_out(item: PlaceMatchCandidate) -> ProviderPlaceCandidateOut:
    return ProviderPlaceCandidateOut(
        id=item.id,
        rank=item.rank,
        name=item.name,
        address=item.address,
        region=item.region,
        latitude=item.latitude,
        longitude=item.longitude,
        provider=item.provider,
        provider_place_id=item.provider_place_id,
        match_kind=item.match_kind,
        status=item.status,
    )


def user_to_out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        email_verified=user.email_verified_at is not None,
        created_at=user.created_at,
    )


def messages_to_out(db: Session, messages: list[Message]) -> list[MessageOut]:
    ids = {m.run_id for m in messages if m.run_id}
    runs = {run.id: run for run in db.scalars(select(AgentRun).where(AgentRun.id.in_(ids)))} if ids else {}
    return [message_to_out(message, runs.get(message.run_id)) for message in messages]


def message_to_out(message: Message, run: AgentRun | None = None) -> MessageOut:
    selection = (run.input_snapshot or {}).get("model_selection", {}) if run else {}
    return MessageOut(
        thinking_level=selection.get("thinking_level"),
        model_id=selection.get("id"),
        model_label=selection.get("label"),
        thinking=selection.get("thinking"),
        version=message.version,
        edited_at=message.edited_at,
        deleted_at=message.deleted_at,
        stale=message.stale,
        attachments=None if message.deleted_at else message.attachments,
        id=message.id,
        seq=message.seq,
        role=message.role,
        intent=message.intent,
        status=message.status,
        content="" if message.deleted_at else message.content,
        media_ids=list(message.media_ids or []),
        place_ids=list(message.place_ids or []),
        run_id=message.run_id,
        error_code=message.error_code,
        created_at=message.created_at,
    )


def saved_place_to_out(row: SavedPlace) -> SavedPlaceOut:
    return SavedPlaceOut(
        id=row.id,
        project_id=row.project_id,
        place_id=row.place_id,
        media_asset_id=row.media_asset_id,
        name=row.name,
        address=row.address,
        region=row.region,
        latitude=row.latitude,
        longitude=row.longitude,
        provider=row.provider,
        provider_place_id=row.provider_place_id,
        source=row.source,
        note=row.note,
        position=row.position,
        created_at=row.created_at,
    )


def route_revision_to_out(revision: RouteRevision, draft: RouteDraft) -> RouteRevisionOut:
    return RouteRevisionOut(
        id=revision.id,
        draft_id=revision.draft_id,
        input_version=revision.input_version,
        mode=revision.mode,
        status=revision.status,
        provider=revision.provider,
        distance_meters=revision.distance_meters,
        duration_seconds=revision.duration_seconds,
        legs=revision.legs or [],
        geometry=revision.geometry,
        error_code=revision.error_code,
        error_message=revision.error_message,
        stale=revision.stale,
        computed_at=revision.computed_at,
        is_current=(
            revision.status == "OK"
            and not revision.stale
            and revision.input_version == draft.input_version
            and draft.current_revision_id == revision.id
        ),
    )


def route_draft_to_out(db: Session, draft: RouteDraft) -> RouteDraftOut:
    from app.services import places_routes

    summary = places_routes.summarize(db, draft)
    reason: str | None = None
    if summary.current is None:
        if summary.latest is None:
            reason = "not_computed"
        elif summary.latest.status == "FAILED":
            reason = f"compute_failed:{summary.latest.error_code or 'unknown'}"
        else:
            reason = "stale_after_edit"
    return RouteDraftOut(
        id=draft.id,
        project_id=draft.project_id,
        name=draft.name,
        mode=draft.mode,
        stops=[RouteStop(**stop) for stop in (draft.stops or [])],
        input_version=draft.input_version,
        current_revision=route_revision_to_out(summary.current, draft) if summary.current else None,
        latest_revision=route_revision_to_out(summary.latest, draft) if summary.latest else None,
        route_unavailable_reason=reason,
        created_at=draft.created_at,
        updated_at=draft.updated_at,
    )


def card_to_out(card: GuideCard) -> GuideCardOut:
    return GuideCardOut(
        id=card.id,
        run_id=card.run_id,
        title=card.title,
        summary=card.summary,
        sections=card.sections or [],
        tips=card.tips or [],
        place_facts=card.place_facts,
        model_name=card.model_name,
        partial=card.partial,
        created_at=card.created_at,
        stale=card.stale,
        stale_reason=card.stale_reason,
    )


def event_to_out(event: WorkspaceEvent) -> EventOut:
    return EventOut(
        id=event.id,
        seq=event.seq,
        type=event.type,
        run_id=event.run_id,
        media_asset_id=event.media_asset_id,
        payload=event.payload or {},
        created_at=event.created_at,
    )


def build_media_snapshot(db: Session, media: MediaAsset) -> MediaSnapshot:
    runs = list(
        db.execute(
            select(AgentRun)
            .where(AgentRun.media_asset_id == media.id)
            .order_by(AgentRun.created_at.desc())
            .limit(10)
        ).scalars()
    )
    latest_run = runs[0] if runs else None
    active_run = next((run for run in runs if run.status not in RunStatus.TERMINAL), None)

    candidates: list[PlaceCandidate] = []
    if latest_run is not None:
        candidates = list(
            db.execute(
                select(PlaceCandidate)
                .where(PlaceCandidate.run_id == latest_run.id)
                .order_by(PlaceCandidate.rank)
            ).scalars()
        )
    place = db.execute(select(Place).where(Place.media_asset_id == media.id)).scalar_one_or_none()
    card = db.execute(select(GuideCard).where(GuideCard.media_asset_id == media.id)).scalar_one_or_none()

    provider_candidates: list[PlaceMatchCandidate] = []
    if latest_run is not None:
        provider_candidates = list(
            db.execute(
                select(PlaceMatchCandidate)
                .where(PlaceMatchCandidate.run_id == latest_run.id)
                .order_by(PlaceMatchCandidate.rank)
            ).scalars()
        )

    return MediaSnapshot(
        media=media_to_out(media),
        candidates=[candidate_to_out(item) for item in candidates],
        provider_candidates=[provider_candidate_to_out(item) for item in provider_candidates],
        place=place_to_out(place) if place else None,
        card=card_to_out(card) if card else None,
        runs=[run_to_out(run) for run in runs],
        active_run=run_to_out(active_run) if active_run else None,
    )


def build_project_snapshot(db: Session, project: GuideProject) -> ProjectSnapshot:
    from app.events import latest_event_id, latest_event_seq

    # B7：先读事件水位，再读业务行（同一事务、同一读视图）。
    # 序号顺序 = 提交顺序，因此水位之前的事件对应的业务变更必定已经可见，
    # 客户端用水位当游标补发时不会漏掉任何"尚未提交但序号更小"的变更。
    watermark_seq = latest_event_seq(db, project.id)

    media_list = list(
        db.execute(
            select(MediaAsset)
            .where(MediaAsset.project_id == project.id, MediaAsset.deleted_at.is_(None))
            .order_by(MediaAsset.position)
        ).scalars()
    )
    # D1-h：最近 30 条持久消息随快照返回，刷新/重开后对话历史不丢。
    recent = list(
        db.execute(
            select(Message)
            .where(Message.project_id == project.id)
            .order_by(Message.seq.desc())
            .limit(30)
        ).scalars()
    )
    recent.reverse()
    return ProjectSnapshot(
        project=project_to_out(project, media_count=len(media_list)),
        media=[build_media_snapshot(db, media) for media in media_list],
        last_event_id=latest_event_id(db, project.id),
        last_event_seq=watermark_seq,
        messages=messages_to_out(db, recent),
        last_message_seq=int(recent[-1].seq) if recent else 0,
    )
