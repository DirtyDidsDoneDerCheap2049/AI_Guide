"""D1 / B1：上传命令幂等必须覆盖整条命令（媒体 + 文件 + 运行），而不只是运行唯一键。

复现 07 号报告的行为：同一 key + 同一图片上传两次会创建两条媒体、只建一个 run，
且第二次响应里的 run 指向第一张媒体；重复请求还会撞三张上限而不是重放原响应。
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import AgentRun, MediaAsset, UploadCommand
from tests.conftest import create_project, make_image_bytes, unique_title

pytestmark = pytest.mark.integration


def _upload(client: TestClient, project_id: str, *, key: str | None, color=(10, 20, 30), start: bool = True):
    headers = {"Idempotency-Key": key} if key else {}
    return client.post(
        f"/api/v1/projects/{project_id}/media",
        files={"file": ("photo.png", make_image_bytes("PNG", color=color), "image/png")},
        data={"start_analysis": "true" if start else "false"},
        headers=headers,
    )


def test_same_key_same_content_replays_single_media_and_run(app, client):
    project = create_project(client, title=unique_title("b1"))
    first = _upload(client, project["id"], key="upload-key-1")
    second = _upload(client, project["id"], key="upload-key-1")

    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["media"]["id"] == second.json()["media"]["id"]
    assert first.json()["run"]["id"] == second.json()["run"]["id"]

    with app.state.session_factory() as db:
        media = db.execute(select(MediaAsset).where(MediaAsset.project_id == project["id"])).scalars().all()
        runs = db.execute(select(AgentRun).where(AgentRun.project_id == project["id"])).scalars().all()
        commands = db.execute(select(UploadCommand)).scalars().all()
    assert len(media) == 1, "同一 key 同一内容不应产生第二张照片"
    assert len(runs) == 1, "同一 key 同一内容不应产生第二个 run"
    assert len(commands) == 1
    # 第二次响应里的 run 必须指向同一个媒体，而不是第一张（旧实现会串位）
    assert second.json()["run"]["media_asset_id"] == second.json()["media"]["id"]

    # 文件只应有一份
    snapshot = client.get(f"/api/v1/projects/{project['id']}").json()
    assert len(snapshot["media"]) == 1


def test_same_key_different_content_conflicts(client):
    project = create_project(client, title=unique_title("b1-conflict"))
    assert _upload(client, project["id"], key="dup-key").status_code == 201
    conflict = _upload(client, project["id"], key="dup-key", color=(200, 30, 40))
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "idempotency_key_conflict"


def test_replay_works_after_media_limit_reached(app, client):
    """额度/容量用满后，重放历史请求仍应成功（而不是撞 409）。"""
    project = create_project(client, title=unique_title("b1-replay"))
    for index in range(3):
        response = _upload(client, project["id"], key=f"k{index}", color=(index * 20 + 5, 40, 60))
        assert response.status_code == 201, response.text
    # 第四张会被容量拦下
    blocked = _upload(client, project["id"], key="k-new", color=(9, 9, 9))
    assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "media_limit_reached"
    # 但重放第 1 张仍然成功
    replay = _upload(client, project["id"], key="k0", color=(5, 40, 60))
    assert replay.status_code == 201
    assert replay.json()["media"]["id"]


def test_concurrent_same_key_creates_single_media(app, client):
    project = create_project(client, title=unique_title("b1-concurrent"))
    results: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        response = _upload(client, project["id"], key="concurrent-key")
        with lock:
            results.append(response.status_code)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(results) == len(threads), 'Every concurrent request must finish, including failures'
    assert not any(thread.is_alive() for thread in threads)
    assert all(code == 201 for code in results), results
    with app.state.session_factory() as db:
        media = db.execute(select(MediaAsset).where(MediaAsset.project_id == project["id"])).scalars().all()
        runs = db.execute(select(AgentRun).where(AgentRun.project_id == project["id"])).scalars().all()
    assert len(media) == 1
    assert len(runs) == 1


def test_store_only_upload_keeps_buffer_for_later_analysis(app, client):
    """start_analysis=false 只保存照片，不建 run；容量对未删除照片计数。"""
    project = create_project(client, title=unique_title("b1-store"))
    response = _upload(client, project["id"], key=None, start=False)
    assert response.status_code == 201
    assert response.json()["run"] is None
    with app.state.session_factory() as db:
        runs = db.execute(select(AgentRun).where(AgentRun.project_id == project["id"])).scalars().all()
    assert runs == []
