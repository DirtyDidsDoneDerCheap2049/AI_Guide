"""D1-e / B4：地点消歧——用户确认的身份不能被第一条搜索结果替换。

覆盖：
1. 纯函数判定：唯一同名、同名多城（歧义）、城市冲突、POI ID 命中、模糊名称不采信。
2. 端到端：歧义时回到用户消歧（新的等待步骤），地点名保持用户确认值且不写坐标；
   用户选定 POI 后才写入供应商事实，卡片状态为已核实。
3. 城市冲突：不覆盖用户输入，等待用户确认。
4. 无结果/供应商失败：地点保持用户身份，卡片 PARTIAL，绝不伪造地址与坐标。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.agent.orchestrator import execute_run
from app.agent.providers.base import PlaceResult
from app.agent.providers.fixtures import FixturePlaceProvider
from app.config import Settings
from app.models import (
    AgentRun,
    GuideCard,
    Place,
    PlaceCandidate,
    PlaceMatchCandidate,
    RunStatus,
    StepName,
    WorkspaceEvent,
)
from app.services.place_match import (
    AMBIGUOUS,
    CONFLICT,
    MATCHED,
    MATCHED_NAME_ONLY,
    resolve_place_match,
)
from tests.conftest import create_project, make_image_bytes, unique_title

pytestmark = pytest.mark.integration

HANGZHOU = "浙江省杭州市西湖区"
YANGZHOU = "江苏省扬州市邗江区"
HUIZHOU = "广东省惠州市惠城区"


def _place(name: str, region: str | None, poi_id: str) -> PlaceResult:
    return PlaceResult(
        name=name,
        address=f"{region or ''}地址",
        region=region,
        latitude=30.0,
        longitude=120.0,
        provider="amap",
        provider_place_id=poi_id,
    )


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


def _setup_run(client, *, start_analysis: bool = True) -> dict:
    project = create_project(client, title=unique_title("b4"), city_hint="杭州")
    response = client.post(
        f"/api/v1/projects/{project['id']}/media",
        files={"file": ("photo.png", make_image_bytes("PNG"), "image/png")},
        data={"start_analysis": "true" if start_analysis else "false"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return {"project": project, "media": body["media"], "run": body["run"]}


def _confirm(client, run_id: str, payload: dict) -> dict:
    response = client.post(f"/api/v1/runs/{run_id}/confirm-place", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def _first_candidate(db, run_id: str) -> PlaceCandidate:
    return db.execute(
        select(PlaceCandidate).where(PlaceCandidate.run_id == run_id).order_by(PlaceCandidate.rank)
    ).scalars().first()


# --------------------------------------------------------------------------- 纯函数判定


def test_resolver_accepts_single_exact_match_with_region():
    match = resolve_place_match(
        user_name="西湖",
        candidates=[_place("西湖", HANGZHOU, "poi-hz")],
        city_hints=["杭州"],
    )
    assert match.status == MATCHED
    assert match.candidate is not None and match.candidate.provider_place_id == "poi-hz"
    assert match.writable is True


def test_resolver_same_name_multiple_cities_is_ambiguous():
    match = resolve_place_match(
        user_name="人民公园",
        candidates=[
            _place("人民公园", HANGZHOU, "poi-hz"),
            _place("人民公园", YANGZHOU, "poi-yz"),
            _place("人民公园", HUIZHOU, "poi-hz2"),
        ],
        city_hints=["杭州"],
    )
    # 城市线索只用于排序提示，绝不代替用户确认。
    assert match.status == AMBIGUOUS
    assert match.candidate is None
    assert match.needs_user is True
    assert match.candidates[0].provider_place_id == "poi-hz", "城市一致的候选排在最前"

    # 没有任何城市线索时同样交回用户，且不能默认取第一条。
    blind = resolve_place_match(
        user_name="人民公园",
        candidates=[
            _place("人民公园", HANGZHOU, "poi-hz"),
            _place("人民公园", YANGZHOU, "poi-yz"),
        ],
        city_hints=[None, ""],
    )
    assert blind.status == AMBIGUOUS
    assert blind.candidate is None
    assert blind.needs_user is True
    assert len(blind.candidates) == 2


def test_resolver_reports_city_conflict_without_selecting():
    match = resolve_place_match(
        user_name="西湖",
        candidates=[_place("西湖", YANGZHOU, "poi-yz")],
        city_hints=["杭州"],
    )
    assert match.status == CONFLICT
    assert match.writable is False
    assert match.needs_user is True


def test_resolver_prefers_explicit_poi_id():
    candidates = [_place("西湖", HANGZHOU, "poi-a"), _place("西湖", HANGZHOU, "poi-b")]
    match = resolve_place_match(user_name="西湖", candidates=candidates, city_hints=["杭州"], provider_place_id="poi-b")
    assert match.status == MATCHED
    assert match.reason == "provider_place_id"
    assert match.candidate.provider_place_id == "poi-b"


def test_resolver_rejects_fuzzy_name_only_results():
    match = resolve_place_match(
        user_name="西湖",
        candidates=[_place("西湖-附近景点", HANGZHOU, "poi-x")],
        city_hints=["杭州"],
    )
    assert match.status == AMBIGUOUS
    assert match.reason == "no_exact_name_match"
    assert match.candidate is None


def test_resolver_marks_name_only_match_when_region_unknown():
    match = resolve_place_match(user_name="西湖", candidates=[_place("西湖", None, "poi-a")], city_hints=[None])
    assert match.status == MATCHED_NAME_ONLY
    assert match.writable is True
    assert "未校验城市" in match.reason or match.reason == "exact_name_region_unknown"


# --------------------------------------------------------------------------- 端到端


def test_ambiguous_match_waits_for_user_and_keeps_confirmed_name(
    app, client, settings, session_factory
):
    """同名多候选：地点名保持用户确认值，坐标不写；用户选 POI 后才落供应商事实。"""
    strict = _settings_with(settings, fixture_behaviors="place=ambiguous")
    from app.agent.providers import build_provider_set

    providers = build_provider_set(strict)
    setup = _setup_run(client)
    run_id = setup["run"]["id"]

    assert execute_run(session_factory, run_id, providers=providers, settings=strict) == RunStatus.WAITING_USER
    with session_factory() as db:
        candidate = _first_candidate(db, run_id)
        confirmed_name = candidate.name

    _confirm(client, run_id, {"decision": "confirm", "candidate_id": candidate.id})
    status = execute_run(session_factory, run_id, providers=providers, settings=strict)
    assert status == RunStatus.WAITING_USER, "歧义必须回到用户确认，而不是自动采用第一条"

    with session_factory() as db:
        run = db.get(AgentRun, run_id)
        assert run.current_step == StepName.WAIT_FOR_PLACE_DISAMBIGUATION
        place = db.execute(select(Place).where(Place.media_asset_id == setup["media"]["id"])).scalar_one()
        assert place.name == confirmed_name, "供应商结果不能改写用户确认的名称"
        assert place.match_status == AMBIGUOUS
        assert place.address is None and place.latitude is None and place.longitude is None
        assert place.provider == "user"
        rows = list(
            db.execute(
                select(PlaceMatchCandidate)
                .where(PlaceMatchCandidate.run_id == run_id)
                .order_by(PlaceMatchCandidate.rank)
            ).scalars()
        )
        assert [row.region for row in rows] == [HANGZHOU, YANGZHOU, HUIZHOU], "全部候选都要独立保存"
        chosen = rows[1]
        events = [event.type for event in db.execute(select(WorkspaceEvent)).scalars()]
        assert "place.disambiguation_required" in events

    # 接口必须把供应商候选暴露给界面，否则用户无法选
    snapshot = client.get(f"/api/v1/projects/{setup['project']['id']}").json()
    media_snapshot = next(item for item in snapshot["media"] if item["media"]["id"] == setup["media"]["id"])
    assert len(media_snapshot["provider_candidates"]) == 3
    assert media_snapshot["place"]["match_status"] == AMBIGUOUS
    assert media_snapshot["place"]["match_note"]

    resumed = _confirm(client, run_id, {"decision": "correct", "provider_candidate_id": chosen.id})
    assert resumed["current_step"] == StepName.GENERATE_GUIDE_CARD
    assert execute_run(session_factory, run_id, providers=providers, settings=strict) == RunStatus.SUCCEEDED

    with session_factory() as db:
        place = db.execute(select(Place).where(Place.media_asset_id == setup["media"]["id"])).scalar_one()
        assert place.name == confirmed_name
        assert place.provider_place_id == chosen.provider_place_id
        assert place.region == YANGZHOU
        assert place.latitude is not None and place.longitude is not None
        assert place.match_status == "user_selected"
        card = db.execute(select(GuideCard).where(GuideCard.media_asset_id == setup["media"]["id"])).scalar_one()
        assert card.partial is False
        assert card.place_facts is not None and card.place_facts["match_status"] == "user_selected"
        statuses = {
            row.id: row.status
            for row in db.execute(select(PlaceMatchCandidate).where(PlaceMatchCandidate.run_id == run_id)).scalars()
        }
        assert statuses[chosen.id] == "SELECTED"
        assert set(statuses.values()) == {"SELECTED", "REJECTED"}


def test_city_conflict_does_not_overwrite_user_place(app, client, settings, session_factory):
    strict = _settings_with(settings, fixture_behaviors="place=city_conflict")
    from app.agent.providers import build_provider_set

    providers = build_provider_set(strict)
    setup = _setup_run(client)
    run_id = setup["run"]["id"]
    assert execute_run(session_factory, run_id, providers=providers, settings=strict) == RunStatus.WAITING_USER
    with session_factory() as db:
        candidate = _first_candidate(db, run_id)

    # 用户明确纠正为"杭州"的地名，供应商只返回扬州同名 POI
    _confirm(client, run_id, {"decision": "correct", "name": "西湖", "candidate_id": candidate.id, "region": "杭州"})
    status = execute_run(session_factory, run_id, providers=providers, settings=strict)
    assert status == RunStatus.WAITING_USER
    with session_factory() as db:
        run = db.get(AgentRun, run_id)
        assert run.current_step == StepName.WAIT_FOR_PLACE_DISAMBIGUATION
        place = db.execute(select(Place).where(Place.media_asset_id == setup["media"]["id"])).scalar_one()
        assert place.name == "西湖"
        assert place.match_status == CONFLICT
        assert place.address is None
        assert place.latitude is None and place.longitude is None
        assert place.provider == "user"

    # 用户选择"保留我自己填的地点"：不写供应商事实，卡片如实标记为局部结果
    _confirm(client, run_id, {"decision": "correct", "name": "西湖", "region": "杭州"})
    assert execute_run(session_factory, run_id, providers=providers, settings=strict) == RunStatus.PARTIAL
    with session_factory() as db:
        run = db.get(AgentRun, run_id)
        assert run.error_code == "place_lookup_failed"
        place = db.execute(select(Place).where(Place.media_asset_id == setup["media"]["id"])).scalar_one()
        assert place.name == "西湖" and place.latitude is None
        card = db.execute(select(GuideCard).where(GuideCard.media_asset_id == setup["media"]["id"])).scalar_one()
        assert card.partial is True
        assert card.place_facts is None


def test_no_result_keeps_user_identity_and_partial_card(app, client, settings, session_factory):
    strict = _settings_with(settings, fixture_behaviors="place=no_result")
    from app.agent.providers import build_provider_set

    providers = build_provider_set(strict)
    setup = _setup_run(client)
    run_id = setup["run"]["id"]
    execute_run(session_factory, run_id, providers=providers, settings=strict)
    with session_factory() as db:
        candidate = _first_candidate(db, run_id)

    _confirm(client, run_id, {"decision": "confirm", "candidate_id": candidate.id})
    assert execute_run(session_factory, run_id, providers=providers, settings=strict) == RunStatus.PARTIAL
    with session_factory() as db:
        place = db.execute(select(Place).where(Place.media_asset_id == setup["media"]["id"])).scalar_one()
        assert place.name == candidate.name
        assert place.match_status == "provider_error"
        assert place.provider == "user"
        assert place.address is None and place.latitude is None
        card = db.execute(select(GuideCard).where(GuideCard.media_asset_id == setup["media"]["id"])).scalar_one()
        assert card.partial is True and card.place_facts is None


def test_fixture_ambiguous_scenario_returns_three_candidates():
    """fixture 场景自身可回归（避免测试依赖一个不存在的场景名）。"""
    provider = FixturePlaceProvider("ambiguous")
    found = provider.search(name="人民公园", city_hint="杭州", limit=5)
    assert found.returned == 3
    assert [item.region for item in found.candidates] == [HANGZHOU, YANGZHOU, HUIZHOU]
    assert all(item.raw.get("fixture") is True for item in found.candidates)
    assert len(provider.search(name="人民公园", city_hint=None, limit=1).candidates) == 1
