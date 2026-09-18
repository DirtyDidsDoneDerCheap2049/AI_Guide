"""S3：WorkspaceEvent 持久化、SSE Last-Event-ID 补发、心跳不写业务表。

B7 起：SSE 的 ``id`` 是项目内事件序号 ``workspace_events.seq``（提交顺序一致），
不再是全局自增 id；断言一律用 seq，避免把"数据库主键"当成游标。
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models import WorkspaceEvent
from tests.conftest import create_project, make_image_bytes, unique_title

pytestmark = pytest.mark.integration


def collect_events(
    client: TestClient,
    project_id: str,
    *,
    last_event_id: int | None = None,
    use_query_param: bool = False,
    stop_after: int = 1,
    timeout: float = 6.0,
    wait_for_heartbeat: bool = False,
) -> dict:
    """读取 SSE 流；返回 {events: [...], comments: [...], status_code: int}。"""
    headers: dict[str, str] = {}
    params: dict[str, str] = {}
    if last_event_id is not None:
        if use_query_param:
            params["last_event_id"] = str(last_event_id)
        else:
            headers["Last-Event-ID"] = str(last_event_id)

    events: list[dict] = []
    comments: list[str] = []
    current: dict = {}
    deadline = time.monotonic() + timeout

    with client.stream(
        "GET",
        f"/api/v1/projects/{project_id}/events",
        headers=headers,
        params=params,
        timeout=timeout,
    ) as response:
        status_code = response.status_code
        if status_code != 200:
            return {"events": events, "comments": comments, "status_code": status_code}

        for line in response.iter_lines():
            if time.monotonic() > deadline:
                break
            if line == "":
                if current.get("event"):
                    events.append(current)
                    current = {}
                    if len(events) >= stop_after:
                        break
                continue
            if line.startswith(":"):
                comments.append(line)
                if wait_for_heartbeat and "heartbeat" in line:
                    break
                continue
            if line.startswith("id: "):
                current["id"] = int(line[4:])
            elif line.startswith("event: "):
                current["event"] = line[7:]
            elif line.startswith("data: "):
                current["data"] = json.loads(line[6:])

    return {"events": events, "comments": comments, "status_code": status_code}


def _upload(client: TestClient, project_id: str) -> dict:
    response = client.post(
        f"/api/v1/projects/{project_id}/media",
        files={"file": ("a.png", make_image_bytes("PNG"), "image/png")},
        data={"start_analysis": "false"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_events_are_persisted_and_streamed_in_order(client, app):
    project = create_project(client, title=unique_title("sse"))
    _upload(client, project["id"])

    with app.state.session_factory() as db:
        rows = db.execute(
            select(WorkspaceEvent).where(WorkspaceEvent.project_id == project["id"]).order_by(WorkspaceEvent.seq)
        ).scalars().all()
        assert [row.type for row in rows] == ["project.created", "media.uploaded"]
        seqs = [row.seq for row in rows]

    result = collect_events(client, project["id"], stop_after=2)
    assert result["status_code"] == 200
    assert [event["event"] for event in result["events"]] == ["project.created", "media.uploaded"]
    assert [event["id"] for event in result["events"]] == seqs


def test_last_event_id_header_replays_only_missing_events(client, app):
    project = create_project(client, title=unique_title("replay"))
    _upload(client, project["id"])
    _upload(client, project["id"])

    with app.state.session_factory() as db:
        seqs = [
            row.seq
            for row in db.execute(
                select(WorkspaceEvent).where(WorkspaceEvent.project_id == project["id"]).order_by(WorkspaceEvent.seq)
            ).scalars()
        ]
    assert len(seqs) == 3

    result = collect_events(client, project["id"], last_event_id=seqs[1], stop_after=1)
    assert [event["id"] for event in result["events"]] == [seqs[2]]
    assert result["events"][0]["event"] == "media.uploaded"


def test_last_event_id_query_parameter_is_supported(client, app):
    project = create_project(client, title=unique_title("query"))
    _upload(client, project["id"])

    with app.state.session_factory() as db:
        seqs = [
            row.seq
            for row in db.execute(
                select(WorkspaceEvent).where(WorkspaceEvent.project_id == project["id"]).order_by(WorkspaceEvent.seq)
            ).scalars()
        ]

    result = collect_events(client, project["id"], last_event_id=seqs[0], use_query_param=True, stop_after=1)
    assert [event["id"] for event in result["events"]] == [seqs[1]]


def test_reconnect_after_disconnect_receives_new_events(client, app):
    project = create_project(client, title=unique_title("reconnect"))
    first_upload = _upload(client, project["id"])

    # 第一次连接：读到 media.uploaded 后主动断开（模拟浏览器断线）。
    first_stream = collect_events(client, project["id"], stop_after=2)
    last_seen = first_stream["events"][-1]["id"]
    assert first_stream["events"][-1]["event"] == "media.uploaded"

    # 断线期间产生新事件。
    second_upload = _upload(client, project["id"])
    assert second_upload["media"]["id"] != first_upload["media"]["id"]

    # 重连：浏览器会自动带上 Last-Event-ID。
    second_stream = collect_events(client, project["id"], last_event_id=last_seen, stop_after=1)
    assert len(second_stream["events"]) == 1
    assert second_stream["events"][0]["id"] > last_seen
    assert second_stream["events"][0]["data"]["payload"]["media_id"] == second_upload["media"]["id"]


def test_heartbeat_is_not_written_to_business_tables(client, app):
    project = create_project(client, title=unique_title("heartbeat"))

    with app.state.session_factory() as db:
        before = db.execute(select(func.count(WorkspaceEvent.id))).scalar_one()

    result = collect_events(client, project["id"], wait_for_heartbeat=True, stop_after=50, timeout=8.0)
    assert any("heartbeat" in comment for comment in result["comments"]), result

    with app.state.session_factory() as db:
        after = db.execute(select(func.count(WorkspaceEvent.id))).scalar_one()
        heartbeat_rows = db.execute(
            select(func.count(WorkspaceEvent.id)).where(WorkspaceEvent.type.like("heartbeat%"))
        ).scalar_one()
    assert after == before
    assert heartbeat_rows == 0


def test_events_endpoint_requires_ownership(app):
    with TestClient(app) as owner:
        project = create_project(owner, title=unique_title("owner"))
        assert collect_events(owner, project["id"], stop_after=1)["status_code"] == 200

    with TestClient(app) as other:
        assert other.get("/api/v1/session").status_code == 200
        assert other.get(f"/api/v1/projects/{project['id']}/events").status_code == 404
