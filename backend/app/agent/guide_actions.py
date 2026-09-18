"""Apply a bounded route edit in the same transaction as the assistant reply.

Only existing places from this workspace may be used. No external calls here.
The caller holds the run lock and verifies the worker lease before applying.
"""
from sqlalchemy import select
from app.models import RouteDraft, SavedPlace, utcnow
from app.events import append_event, EventType
from app.services.places_routes import _stale_previous_revisions


def apply_route_action(db, project_id: str, action: dict, known_routes: list[dict]) -> dict:
    if action['operation'] == 'update':
        known = next((r for r in known_routes if r['id'] == action.get('route_id')), None)
        if known is None or action.get('expected_version') != known['version']:
            return {'ok': False, 'message': '没有找到要修改的路线，请先选择一条路线。'}
        draft = db.execute(select(RouteDraft).where(RouteDraft.id == known['id'], RouteDraft.project_id == project_id).with_for_update()).scalar_one_or_none()
        if draft is None or draft.input_version != known['version']:
            return {'ok': False, 'message': '路线刚刚被修改过。这次没有覆盖它，请查看最新路线后再试。'}
        indexes = action.get('stop_indices')
        if indexes is None:
            indexes = list(range(len(draft.stops)))
        if len(set(indexes)) != len(indexes) or any(i < 0 or i >= len(draft.stops) for i in indexes):
            return {'ok': False, 'message': '没有确定要调整哪些地点，路线保持不变。'}
        draft.stops = [draft.stops[i] for i in indexes]
        draft.mode = action.get('mode') or draft.mode
        draft.input_version += 1
        draft.updated_at = utcnow()
        _stale_previous_revisions(db, draft)
        event = EventType.ROUTE_DRAFT_UPDATED
        message = f'已更新「{draft.name}」，现在有 {len(draft.stops)} 个地点。'
    else:
        ids = action.get('saved_place_ids', [])
        if not ids or len(ids) != len(set(ids)):
            return {'ok': False, 'message': '先选好要去的地点，再帮你安排路线。'}
        places = {p.id: p for p in db.execute(select(SavedPlace).where(SavedPlace.project_id == project_id, SavedPlace.id.in_(ids))).scalars()}
        if len(places) != len(ids) or any(p.latitude is None or p.longitude is None for p in places.values()):
            return {'ok': False, 'message': '部分地点还没有确定位置，请在地图上确认后再安排路线。'}
        stops = [{'saved_place_id': i, 'name': places[i].name, 'latitude': places[i].latitude, 'longitude': places[i].longitude} for i in ids]
        import uuid
        draft = RouteDraft(project_id=project_id, name='导游建议路线 ' + uuid.uuid4().hex[:8], mode=action.get('mode') or 'walking', stops=stops)
        db.add(draft)
        db.flush()
        event = EventType.ROUTE_DRAFT_CREATED
        message = f'已按顺序整理好 {len(stops)} 个地点，可以在地图旁查看和调整。'
    append_event(db, project_id=project_id, type=event, payload={'draft_id': draft.id, 'input_version': draft.input_version})
    return {'ok': True, 'message': message, 'route_id': draft.id}
