"""B5 / D4：媒体生命周期与编辑语义。

覆盖 07 号文档 B5 列出的缺口：
1. 已保存照片（上传时 start_analysis=false）有独立的"新建分析"入口，且重复点击不会重复建运行。
2. 取消运行与删除照片是两件事：取消不动照片；删除会取消未完成任务并软删除，可恢复。
3. 删除后晚到的任务不得复活内容（编排器写回丢弃，运行 CANCELLED）。
4. 改地点：版本 +1、旧讲解标记过期但保留、用户笔记保留；条件版本冲突返回 409。
5. 终态运行不复活：重做总是新建运行并带 source_run_id；CANCELLED 也允许重做。
6. 项目 PATCH（标题/城市）与笔记接口真实生效。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.agent.orchestrator import execute_run
from app.config import Settings
from app.models import (
    AgentRun,
    GuideCard,
    MediaAsset,
    Place,
    RunStatus,
    StepName,
)
from tests.conftest import create_project, make_image_bytes, unique_title

pytestmark = pytest.mark.integration


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


def _upload(client, project_id: str, *, start_analysis: bool = True) -> dict:
    response = client.post(
        f"/api/v1/projects/{project_id}/media",
        files={"file": ("p.png", make_image_bytes("PNG"), "image/png")},
        data={"start_analysis": "true" if start_analysis else "false"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _finish_run(client, session_factory, providers, settings, run_id: str) -> str:
    return execute_run(session_factory, run_id, providers=providers, settings=settings)


def test_saved_photo_can_start_analysis_later(app, client, settings, session_factory, providers):
    project = create_project(client, title=unique_title("b5-later"), city_hint="杭州")
    uploaded = _upload(client, project["id"], start_analysis=False)
    assert uploaded["run"] is None
    media_id = uploaded["media"]["id"]
    assert uploaded["media"]["status"] == "UPLOADED"

    first = client.post(f"/api/v1/media/{media_id}/analyze")
    assert first.status_code == 202, first.text
    assert first.json()["run"]["current_step"] == StepName.ANALYZE_IMAGE
    assert first.json()["media"]["active_run_id"] == first.json()["run"]["id"]

    # 任务在跑到待确认之前，不允许再叠一个分析运行
    conflict = client.post(f"/api/v1/media/{media_id}/analyze")
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] in {"media_busy", "run_in_progress"}

    assert _finish_run(client, session_factory, providers, settings, first.json()["run"]["id"]) == RunStatus.WAITING_USER
    # 待确认状态下重复点击：同样不新建运行（幂等键之外的状态保护）
    again = client.post(f"/api/v1/media/{media_id}/analyze")
    assert again.status_code == 409


def test_analyze_is_idempotent_by_key(app, client, settings):
    project = create_project(client, title=unique_title("b5-key"), city_hint="杭州")
    uploaded = _upload(client, project["id"], start_analysis=False)
    media_id = uploaded["media"]["id"]

    headers = {"Idempotency-Key": "analyze-1"}
    first = client.post(f"/api/v1/media/{media_id}/analyze", headers=headers)
    assert first.status_code == 202
    replay = client.post(f"/api/v1/media/{media_id}/analyze", headers=headers)
    assert replay.status_code == 202
    assert replay.json()["run"]["id"] == first.json()["run"]["id"]

    with client.app.state.session_factory() as db:
        runs = db.execute(select(AgentRun).where(AgentRun.media_asset_id == media_id)).scalars().all()
        assert len(runs) == 1


def test_delete_cancels_active_run_and_restore_keeps_history(app, client, settings, session_factory, providers):
    project = create_project(client, title=unique_title("b5-del"), city_hint="杭州")
    uploaded = _upload(client, project["id"])
    media_id = uploaded["media"]["id"]
    run_id = uploaded["run"]["id"]

    deleted = client.delete(f"/api/v1/media/{media_id}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["media"]["deleted"] is True

    with session_factory() as db:
        run = db.get(AgentRun, run_id)
        assert run.status == RunStatus.CANCELLED
        assert run.error_code == "media_deleted"

    snapshot = client.get(f"/api/v1/projects/{project['id']}").json()
    assert [item["media"]["id"] for item in snapshot["media"]] == []

    listing = client.get(f"/api/v1/projects/{project['id']}/media/deleted").json()
    assert [item["id"] for item in listing] == [media_id]

    restored = client.post(f"/api/v1/media/{media_id}/restore")
    assert restored.status_code == 200
    assert restored.json()["media"]["deleted"] is False
    snapshot_after = client.get(f"/api/v1/projects/{project['id']}").json()
    assert [item["media"]["id"] for item in snapshot_after["media"]] == [media_id]


def test_late_run_after_delete_does_not_resurrect_content(app, client, settings, session_factory, providers):
    """照片被删除后，晚到的写回必须丢弃：不生成候选/卡片，运行 CANCELLED。"""
    project = create_project(client, title=unique_title("b5-late"), city_hint="杭州")
    uploaded = _upload(client, project["id"])
    media_id = uploaded["media"]["id"]
    run_id = uploaded["run"]["id"]

    # 先推进到待确认（产生候选），再把照片移出相册，然后让"旧任务"尝试继续写回
    assert _finish_run(client, session_factory, providers, settings, run_id) == RunStatus.WAITING_USER
    client.delete(f"/api/v1/media/{media_id}")

    with session_factory() as db:
        run = db.get(AgentRun, run_id)
        assert run.status == RunStatus.CANCELLED
        # 再次执行不会复活任务
        assert execute_run(session_factory, run_id, providers=providers, settings=settings) == RunStatus.CANCELLED
        media = db.get(MediaAsset, media_id)
        assert media.deleted_at is not None
        cards = db.execute(select(GuideCard).where(GuideCard.media_asset_id == media_id)).scalars().all()
        assert cards == [], "已删除照片不得生成讲解卡片"


def test_change_place_marks_card_stale_and_keeps_note(app, client, settings, session_factory, providers):
    project = create_project(client, title=unique_title("b5-place"), city_hint="杭州")
    uploaded = _upload(client, project["id"])
    media_id = uploaded["media"]["id"]
    run_id = uploaded["run"]["id"]
    assert _finish_run(client, session_factory, providers, settings, run_id) == RunStatus.WAITING_USER

    with session_factory() as db:
        from app.models import PlaceCandidate

        candidate = db.execute(select(PlaceCandidate).where(PlaceCandidate.run_id == run_id)).scalars().first()
        candidate_id = candidate.id
    client.post(f"/api/v1/runs/{run_id}/confirm-place", json={"decision": "confirm", "candidate_id": candidate_id})
    assert _finish_run(client, session_factory, providers, settings, run_id) == RunStatus.SUCCEEDED

    noted = client.patch(f"/api/v1/media/{media_id}", json={"note": "这是我自己写的备注"})
    assert noted.status_code == 200
    assert noted.json()["media"]["note"] == "这是我自己写的备注"

    with session_factory() as db:
        place = db.execute(select(Place).where(Place.media_asset_id == media_id)).scalar_one()
        version_before = place.version

    changed = client.post(
        f"/api/v1/media/{media_id}/place",
        json={"name": "用户改的地点", "region": "杭州", "expected_version": version_before, "regenerate": False},
    )
    assert changed.status_code == 200, changed.text
    body = changed.json()
    assert body["place"]["name"] == "用户改的地点"
    assert body["run"] is None, "regenerate=false 时不应新建运行"

    with session_factory() as db:
        place = db.execute(select(Place).where(Place.media_asset_id == media_id)).scalar_one()
        assert place.version == version_before + 1
        card = db.execute(select(GuideCard).where(GuideCard.media_asset_id == media_id)).scalar_one()
        assert card.stale is True, "旧讲解必须标记过期"
        assert "用户改的地点" in (card.stale_reason or "")
        assert card.title  # 内容保留，不删除历史
        media = db.get(MediaAsset, media_id)
        assert media.note == "这是我自己写的备注", "改地点不得覆盖用户笔记"

    # 过期状态必须出现在接口里，否则前端无法提示
    snapshot = client.get(f"/api/v1/projects/{project['id']}").json()
    entry = next(item for item in snapshot["media"] if item["media"]["id"] == media_id)
    assert entry["card"]["stale"] is True
    assert entry["place"]["version"] == version_before + 1


def test_change_place_version_conflict_returns_409(app, client, settings, session_factory, providers):
    project = create_project(client, title=unique_title("b5-conflict"), city_hint="杭州")
    uploaded = _upload(client, project["id"])
    media_id = uploaded["media"]["id"]
    run_id = uploaded["run"]["id"]
    assert _finish_run(client, session_factory, providers, settings, run_id) == RunStatus.WAITING_USER

    first = client.post(f"/api/v1/media/{media_id}/place", json={"name": "第一处"})
    assert first.status_code == 200
    version_one = first.json()["place"]["version"]

    # 标签页 A 带着自己看到的版本提交 → 成功并 +1
    second = client.post(
        f"/api/v1/media/{media_id}/place",
        json={"name": "第二处", "expected_version": version_one},
    )
    assert second.status_code == 200, second.text
    assert second.json()["place"]["version"] == version_one + 1

    # 标签页 B 仍拿着旧版本提交 → 必须冲突，不能静默覆盖
    third = client.post(
        f"/api/v1/media/{media_id}/place",
        json={"name": "第三处", "expected_version": version_one},
    )
    assert third.status_code == 409
    assert third.json()["detail"]["code"] == "place_version_conflict"

    with session_factory() as db:
        place = db.execute(select(Place).where(Place.media_asset_id == media_id)).scalar_one()
        assert place.name == "第二处", "冲突请求不得改写地点"


def test_change_place_with_regenerate_creates_new_run(app, client, settings, session_factory, providers):
    project = create_project(client, title=unique_title("b5-regen"), city_hint="杭州")
    uploaded = _upload(client, project["id"])
    media_id = uploaded["media"]["id"]
    run_id = uploaded["run"]["id"]
    assert _finish_run(client, session_factory, providers, settings, run_id) == RunStatus.WAITING_USER
    with session_factory() as db:
        from app.models import PlaceCandidate

        candidate = db.execute(select(PlaceCandidate).where(PlaceCandidate.run_id == run_id)).scalars().first()
        candidate_id = candidate.id
    client.post(f"/api/v1/runs/{run_id}/confirm-place", json={"decision": "confirm", "candidate_id": candidate_id})
    assert _finish_run(client, session_factory, providers, settings, run_id) == RunStatus.SUCCEEDED

    changed = client.post(
        f"/api/v1/media/{media_id}/place",
        json={"name": "改成新的地点", "region": "杭州", "regenerate": True},
    )
    assert changed.status_code == 200, changed.text
    new_run = changed.json()["run"]
    assert new_run is not None
    assert new_run["id"] != run_id
    assert new_run["current_step"] == StepName.LOOKUP_PLACE

    with session_factory() as db:
        old = db.get(AgentRun, run_id)
        assert old.status == RunStatus.SUCCEEDED, "终态运行不复活"
        fresh = db.get(AgentRun, new_run["id"])
        assert fresh.source_run_id is None  # 改地点不是重试，但仍要新运行
        place = db.execute(select(Place).where(Place.media_asset_id == media_id)).scalar_one()
        assert place.name == "改成新的地点"


def test_cancelled_run_can_be_retried(app, client, settings, session_factory, providers):
    """B5：取消后必须能重做（旧实现返回 409）。"""
    project = create_project(client, title=unique_title("b5-retry"), city_hint="杭州")
    uploaded = _upload(client, project["id"])
    media_id = uploaded["media"]["id"]
    run_id = uploaded["run"]["id"]
    cancelled = client.post(f"/api/v1/runs/{run_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == RunStatus.CANCELLED

    retried = client.post(f"/api/v1/media/{media_id}/runs/retry")
    assert retried.status_code == 202, retried.text
    new_run_id = retried.json()["run"]["id"]
    assert new_run_id != run_id

    with session_factory() as db:
        old = db.get(AgentRun, run_id)
        new = db.get(AgentRun, new_run_id)
        assert old.status == RunStatus.CANCELLED, "取消的运行保持终态"
        assert new.source_run_id == run_id
        assert new.status == RunStatus.QUEUED
        assert new.trigger == "retry"

    # 新运行可以正常推进到待确认
    assert _finish_run(client, session_factory, providers, settings, new_run_id) == RunStatus.WAITING_USER


def test_project_patch_updates_title_and_city(app, client, settings):
    project = create_project(client, title=unique_title("b5-project"), city_hint="杭州")
    response = client.patch(f"/api/v1/projects/{project['id']}", json={"title": "新标题", "city_hint": "苏州"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["title"] == "新标题"
    assert body["city_hint"] == "苏州"
    assert body["version"] >= project["version"] + 1

    snapshot = client.get(f"/api/v1/projects/{project['id']}").json()
    assert snapshot["project"]["title"] == "新标题"


def test_operations_on_deleted_media_are_rejected(app, client, settings):
    project = create_project(client, title=unique_title("b5-rejected"), city_hint="杭州")
    uploaded = _upload(client, project["id"], start_analysis=False)
    media_id = uploaded["media"]["id"]
    client.delete(f"/api/v1/media/{media_id}")

    assert client.post(f"/api/v1/media/{media_id}/analyze").status_code == 409
    assert client.patch(f"/api/v1/media/{media_id}", json={"note": "x"}).status_code == 409
    assert client.post(f"/api/v1/media/{media_id}/place", json={"name": "y"}).status_code == 409
