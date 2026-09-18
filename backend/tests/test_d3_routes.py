"""D3-b：收藏地点与路线（真实距离/时长/几何来自供应商）。

验收点（对应 08 号文档与 07 号文档的 R2/R3）：
1. 收藏：手工收藏与照片地点收藏；同一地点重复收藏幂等；未核实地点不能收藏为事实。
2. 地点检索：返回多候选（含 match_kind），fixture 模式显式标注 fixture=true。
3. 路线：算路结果（距离/时长/几何/分段）来自 Provider，不出现"估算"数值。
4. 失败保留列表：算路失败只写 FAILED 修订，stops 不变，可重试。
5. 过期保护：改模式/重新排序后，旧结果标记 stale，不再当"当前路线"。
6. 条件版本冲突：两个标签页同时改草稿时，后提交的一方 409。
7. 真实 HTTP 桩：AMapDirectionProvider 解析高德 walking/driving 响应（协议层，不是厂商数据）。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest
from sqlalchemy import select

from app.agent.providers import build_provider_set
from app.agent.providers.direction import AMapDirectionProvider
from app.config import Settings
from app.models import Place, RouteDraft, RouteRevision, SavedPlace
from tests.conftest import create_project, make_image_bytes, unique_title
from tests.test_s2_agent_flow import setup_run

pytestmark = pytest.mark.integration

STOPS = [
    {"name": "起点", "latitude": 30.25, "longitude": 120.15},
    {"name": "中点", "latitude": 30.26, "longitude": 120.16},
    {"name": "终点", "latitude": 30.27, "longitude": 120.17},
]


def _settings_with(settings, **overrides) -> Settings:
    base = {
        "app_env": "test",
        "database_url": settings.database_url,
        "redis_url": settings.redis_url,
        "session_secret": settings.session_secret,
        "provider_mode": "mock",
        "media_root": settings.media_root,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


# --------------------------------------------------------------------------- 收藏


def test_manual_favorite_is_idempotent(app, client, settings):
    project = create_project(client, title=unique_title("d3-fav"), city_hint="杭州")
    payload = {"name": "西湖", "region": "杭州", "latitude": 30.25, "longitude": 120.15}

    first = client.post(f"/api/v1/projects/{project['id']}/saved-places", json=payload)
    assert first.status_code == 201, first.text
    second = client.post(f"/api/v1/projects/{project['id']}/saved-places", json=payload)
    assert second.status_code == 201
    assert second.json()["id"] == first.json()["id"], "同一地点重复收藏必须返回原记录"

    listing = client.get(f"/api/v1/projects/{project['id']}/saved-places").json()["saved_places"]
    assert len(listing) == 1
    assert listing[0]["name"] == "西湖"

    removed = client.delete(f"/api/v1/projects/{project['id']}/saved-places/{first.json()['id']}")
    assert removed.status_code == 204
    assert client.get(f"/api/v1/projects/{project['id']}/saved-places").json()["saved_places"] == []


def test_favorite_from_photo_requires_verified_place(app, client, settings, session_factory, providers):
    setup = setup_run(client)
    run_id = setup["run"]["id"]
    media_id = setup["media"]["id"]
    project_id = setup["project"]["id"]

    # 还没确认地点 → 409
    too_early = client.post(f"/api/v1/projects/{project_id}/saved-places", json={"name": "x", "media_asset_id": media_id})
    assert too_early.status_code in (409, 404, 422), too_early.text

    from app.agent.orchestrator import execute_run

    assert execute_run(session_factory, run_id, providers=providers, settings=settings) == "WAITING_USER"
    with session_factory() as db:
        from app.models import PlaceCandidate

        candidate = db.execute(select(PlaceCandidate).where(PlaceCandidate.run_id == run_id)).scalars().first()
        candidate_id = candidate.id
    client.post(f"/api/v1/runs/{run_id}/confirm-place", json={"decision": "confirm", "candidate_id": candidate_id})
    assert execute_run(session_factory, run_id, providers=providers, settings=settings) == "SUCCEEDED"

    saved = client.post(
        f"/api/v1/projects/{project_id}/saved-places",
        json={"name": "ignored-name", "media_asset_id": media_id, "note": "照片里的地方"},
    )
    assert saved.status_code == 201, saved.text
    body = saved.json()
    assert body["source"] == "photo"
    assert body["media_asset_id"] == media_id
    assert body["note"] == "照片里的地方"
    assert body["latitude"] is not None and body["longitude"] is not None

    # 人为降级为未核实后：不允许再收藏（不能把未核实地点当事实）
    with session_factory() as db:
        place = db.execute(select(Place).where(Place.media_asset_id == media_id)).scalar_one()
        place.match_status = "conflict"
        db.commit()
    client.delete(f"/api/v1/projects/{project_id}/saved-places/{body['id']}")
    rejected = client.post(
        f"/api/v1/projects/{project_id}/saved-places", json={"name": "x", "media_asset_id": media_id}
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "place_not_verified"


def test_place_search_returns_multiple_candidates_and_marks_fixture(app, client, settings):
    project = create_project(client, title=unique_title("d3-search"), city_hint="杭州")
    response = client.get(f"/api/v1/projects/{project['id']}/places/search", params={"q": "人民公园"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["fixture"] is True, "fixture 模式必须显式标注，不能冒充真实地点数据"
    assert body["query"] == "人民公园"
    assert len(body["candidates"]) >= 1
    assert all("match_kind" in item for item in body["candidates"])


# --------------------------------------------------------------------------- 路线


def test_route_compute_uses_provider_numbers(app, client, settings):
    project = create_project(client, title=unique_title("d3-route"), city_hint="杭州")
    created = client.post(
        f"/api/v1/projects/{project['id']}/routes",
        json={"name": "半日步行", "mode": "walking", "stops": STOPS},
    )
    assert created.status_code == 201, created.text
    draft = created.json()
    assert draft["route_unavailable_reason"] == "not_computed"

    computed = client.post(f"/api/v1/projects/{project['id']}/routes/{draft['id']}/compute")
    assert computed.status_code == 200, computed.text
    body = computed.json()
    revision = body["current_revision"]
    assert revision is not None and revision["status"] == "OK"
    assert revision["provider"] == "fixture-route"
    # fixture 步行每段 1200m / 900s，两段
    assert revision["distance_meters"] == 2400
    assert revision["duration_seconds"] == 1800
    assert len(revision["legs"]) == 2
    assert revision["geometry"], "必须带回几何折线"
    assert body["route_unavailable_reason"] is None


def test_route_failure_keeps_stops_and_can_retry(app, client, settings, session_factory):
    """算路失败只写 FAILED 修订，stops 保留，换成可用 Provider 后同一草稿可重试成功。"""
    from app.agent.providers import build_provider_set
    from app.models import GuideProject
    from app.services import places_routes

    project = create_project(client, title=unique_title("d3-fail"), city_hint="杭州")
    created = client.post(
        f"/api/v1/projects/{project['id']}/routes",
        json={"name": "会失败", "mode": "driving", "stops": STOPS},
    ).json()

    failing = build_provider_set(_settings_with(settings, fixture_behaviors="route=http_error"))
    with session_factory() as db:
        draft_row = db.get(RouteDraft, created["id"])
        project_row = db.get(GuideProject, project["id"])
        revision = places_routes.compute_route(db, project=project_row, draft=draft_row, providers=failing)
        assert revision.status == "FAILED"
        assert revision.error_code == "provider_http_error"

    listed = client.get(f"/api/v1/projects/{project['id']}/routes").json()["drafts"][0]
    assert listed["current_revision"] is None
    assert listed["latest_revision"]["status"] == "FAILED"
    assert listed["route_unavailable_reason"].startswith("compute_failed")
    assert len(listed["stops"]) == 3, "算路失败不能丢失停留点"

    # 用正常 Provider 重试同一个草稿（stops 不变）
    retried = client.post(f"/api/v1/projects/{project['id']}/routes/{created['id']}/compute")
    assert retried.status_code == 200, retried.text
    body = retried.json()
    assert body["current_revision"]["status"] == "OK"
    assert body["route_unavailable_reason"] is None
    assert len(body["stops"]) == 3


def test_route_mode_change_marks_previous_result_stale(app, client, settings, session_factory):
    project = create_project(client, title=unique_title("d3-stale"), city_hint="杭州")
    created = client.post(
        f"/api/v1/projects/{project['id']}/routes", json={"name": "混合", "mode": "walking", "stops": STOPS}
    ).json()
    first = client.post(f"/api/v1/projects/{project['id']}/routes/{created['id']}/compute").json()
    first_revision_id = first["current_revision"]["id"]
    assert first["current_revision"]["duration_seconds"] == 1800

    # 换驾车模式：旧结果必须标记过期，并给出可解释的原因
    updated = client.patch(
        f"/api/v1/projects/{project['id']}/routes/{created['id']}",
        json={"mode": "driving", "expected_version": created["input_version"]},
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["mode"] == "driving"
    assert body["input_version"] == created["input_version"] + 1
    assert body["current_revision"] is None
    assert body["route_unavailable_reason"] == "stale_after_edit"

    with session_factory() as db:
        old = db.get(RouteRevision, first_revision_id)
        assert old.stale is True, "输入版本变化后旧修订必须标记过期"

    recomputed = client.post(f"/api/v1/projects/{project['id']}/routes/{created['id']}/compute").json()
    assert recomputed["current_revision"]["mode"] == "driving"
    # fixture: 驾车每段 900//4=225s → 两段 450s，距离仍按每段 1200m
    assert recomputed["current_revision"]["duration_seconds"] == 450

    revisions = client.get(f"/api/v1/projects/{project['id']}/routes/{created['id']}/revisions").json()
    assert len(revisions["revisions"]) == 2
    assert revisions["revisions"][1]["stale"] is True
    assert revisions["revisions"][0]["is_current"] is True


def test_route_reorder_and_version_conflict(app, client, settings):
    project = create_project(client, title=unique_title("d3-conflict"), city_hint="杭州")
    created = client.post(
        f"/api/v1/projects/{project['id']}/routes", json={"name": "顺序", "mode": "walking", "stops": STOPS}
    ).json()
    version_one = created["input_version"]

    reordered = list(reversed(STOPS))
    ok = client.patch(
        f"/api/v1/projects/{project['id']}/routes/{created['id']}",
        json={"stops": reordered, "expected_version": version_one},
    )
    assert ok.status_code == 200
    assert [stop["name"] for stop in ok.json()["stops"]] == ["终点", "中点", "起点"]
    assert ok.json()["input_version"] == version_one + 1

    # 另一个标签页拿着旧版本提交 → 冲突，不能静默覆盖
    stale_tab = client.patch(
        f"/api/v1/projects/{project['id']}/routes/{created['id']}",
        json={"mode": "driving", "expected_version": version_one},
    )
    assert stale_tab.status_code == 409
    assert stale_tab.json()["detail"]["code"] == "draft_version_conflict"

    with client.app.state.session_factory() as db:  # type: ignore[attr-defined]
        draft = db.get(RouteDraft, created["id"])
        assert draft.mode == "walking", "冲突请求不得改模式"


def test_route_without_coordinates_is_rejected(app, client, settings):
    project = create_project(client, title=unique_title("d3-nocoord"), city_hint="杭州")
    created = client.post(
        f"/api/v1/projects/{project['id']}/routes",
        json={"name": "缺坐标", "mode": "walking", "stops": [{"name": "只有名字"}]},
    ).json()
    response = client.post(f"/api/v1/projects/{project['id']}/routes/{created['id']}/compute")
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "stop_missing_coordinates"

    single = client.post(
        f"/api/v1/projects/{project['id']}/routes",
        json={"name": "只有一个点", "mode": "walking", "stops": [STOPS[0]]},
    ).json()
    too_short = client.post(f"/api/v1/projects/{project['id']}/routes/{single['id']}/compute")
    assert too_short.status_code == 422
    assert too_short.json()["detail"]["code"] == "need_at_least_two_stops"


# --------------------------------------------------------------------------- 协议层


class _DirectionStub(BaseHTTPRequestHandler):
    """按高德 v3/direction 响应格式应答的本地桩（不是厂商数据）。"""

    def log_message(self, *args):  # noqa: D102 - 静音
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        params = parse_qs(raw)
        mode = "walking" if "/walking" in self.path else "driving"
        if params.get("mode") == ["empty"]:
            self._send({"status": "1", "route": {"paths": []}})
            return
        distance = 1500 if mode == "walking" else 4200
        duration = 1100 if mode == "walking" else 600
        self._send(
            {
                "status": "1",
                "route": {
                    "paths": [
                        {
                            "distance": str(distance),
                            "duration": str(duration),
                            "strategy": "STUB_STRATEGY",
                            "steps": [
                                {
                                    "instruction": "向东步行",
                                    "distance": str(distance // 2),
                                    "duration": str(duration // 2),
                                    "polyline": "120.150000,30.250000;120.152000,30.251000",
                                },
                                {
                                    "instruction": "继续向东",
                                    "distance": str(distance - distance // 2),
                                    "duration": str(duration - duration // 2),
                                    "polyline": "120.152000,30.251000;120.160000,30.260000",
                                },
                            ],
                        }
                    ]
                },
            }
        )

    def _send(self, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def direction_stub():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DirectionStub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def test_amap_direction_provider_parses_walking_and_driving(direction_stub, settings):
    real = Settings(
        _env_file=None,
        app_env="test",
        database_url=settings.database_url,
        session_secret=settings.session_secret,
        provider_mode="real",
        place_api_base_url=direction_stub,
        place_api_key="0" * 32,
        vision_api_key="stub-key-not-a-real-credential",
        text_api_key="stub-key-not-a-real-credential",
    )
    provider = AMapDirectionProvider(real)

    walking = provider.route(mode="walking", origin=(120.15, 30.25), destination=(120.16, 30.26))
    assert walking.distance_meters == 1500
    assert walking.duration_seconds == 1100
    assert len(walking.legs) == 2
    assert walking.geometry.startswith("120.150000,30.250000")

    driving = provider.route(mode="driving", origin=(120.15, 30.25), destination=(120.16, 30.26))
    assert driving.distance_meters == 4200
    assert driving.duration_seconds == 600
    assert driving.raw["coordinate_system"] == "gcj02"

    providers = build_provider_set(real)
    assert providers.direction is not None
    assert providers.direction.name == "amap-route"
