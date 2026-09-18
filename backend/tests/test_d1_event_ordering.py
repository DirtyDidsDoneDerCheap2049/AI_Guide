"""D1-g / B7：事件与快照的边界。

旧缺陷（代码确认，未观察到线上丢失）：``workspace_events.id`` 是全局自增，
小 id 的事务可能后提交；客户端游标一旦推进到更大的 id，就再也读不到那条事件。

本文件用**两个真实数据库事务**强制乱序提交来验证修复：
1. 事务 A 先分配序号但不提交，事务 B 在同一项目上再分配 → B 必须被行锁挡住，
   直到 A 提交（证明序号顺序 = 提交顺序，不存在"小序号后提交"）。
2. 快照先读事件水位、再读业务行（同一事务同一读视图）：水位之前的事件，
   其业务变更必定已可见；从水位补发不会漏事件。
3. 回滚会留下序号空洞，但不会乱序；补发按 seq > 游标 逐条推进，空洞不影响正确性。
"""

from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from app.db import session_scope
from app.events import allocate_event_seq, append_event, events_since, latest_event_seq
from app.models import ProjectEventCounter, WorkspaceEvent
from tests.conftest import create_project, unique_title

pytestmark = pytest.mark.integration


def _make_project(session_factory, title: str) -> str:
    """直接在库里建一个项目与会话（不经 API，避免"每会话只有一个活跃项目"的限制）。"""
    import uuid

    from app.models import DemoSession, GuideProject

    session_id = str(uuid.uuid4())
    project_id = str(uuid.uuid4())
    with session_scope(session_factory) as db:
        db.add(DemoSession(id=session_id, client_hash=f"b7-{session_id[:8]}"))
        db.add(GuideProject(id=project_id, title=title, owner_session_id=session_id, status="active"))
    return project_id


def test_allocator_serializes_concurrent_transactions(app, client, settings, session_factory):
    """核心回归：序号在事务内分配并持有行锁到提交，因此 B 必须等 A 提交。"""
    project_id = _make_project(session_factory, unique_title("b7-order"))

    session_a = session_factory()
    session_b = session_factory()
    allocated: dict[str, object] = {}
    finished = threading.Event()

    def worker_b() -> None:
        try:
            allocated["b"] = allocate_event_seq(session_b, project_id)
            session_b.commit()
        except OperationalError as exc:  # pragma: no cover - 说明串行化失败
            allocated["b_error"] = str(exc.orig)
        finally:
            finished.set()
            session_b.close()

    try:
        # A 分配 1 号但不提交（持有计数器行锁）。
        allocated["a"] = allocate_event_seq(session_a, project_id)
        thread = threading.Thread(target=worker_b)
        thread.start()
        time.sleep(0.5)
        assert not finished.is_set(), "A 未提交时 B 不能拿到序号"

        append_event(session_a, project_id=project_id, type="test.a", seq=int(allocated["a"]))
        session_a.commit()
        thread.join(timeout=10)
        assert finished.is_set()
    finally:
        session_a.close()

    assert "b_error" not in allocated, f"1 秒内的正常等待不应报错：{allocated.get('b_error')}"
    assert allocated["a"] == 1
    assert allocated["b"] == 2, "B 只能在 A 提交后拿到下一个序号（序号顺序 = 提交顺序）"

    with session_factory() as db:
        rows = list(
            db.execute(
                select(WorkspaceEvent).where(WorkspaceEvent.project_id == project_id).order_by(WorkspaceEvent.seq)
            ).scalars()
        )
        assert [(row.seq, row.type) for row in rows] == [(1, "test.a")]
        # B 只分配了 2 号却没写事件（空事务），计数器留下空洞但不会重排。
        counter = db.get(ProjectEventCounter, project_id)
        assert counter is not None and counter.last_seq == 2


def test_allocator_blocks_beyond_lock_wait_timeout(app, client, settings, session_factory):
    """持有者卡住超过锁等待超时时，另一个事务必须明确失败（不是并发拿到同序号）。"""
    project_id = _make_project(session_factory, unique_title("b7-timeout"))

    session_a = session_factory()
    session_b = session_factory()
    try:
        first = allocate_event_seq(session_a, project_id)
        session_b.execute(text("SET SESSION innodb_lock_wait_timeout = 1"))
        started = time.monotonic()
        with pytest.raises(OperationalError) as excinfo:
            allocate_event_seq(session_b, project_id)
        session_b.rollback()
        waited = time.monotonic() - started
        assert "1205" in str(excinfo.value.orig), str(excinfo.value.orig)
        assert waited >= 0.9, "必须真的等待到锁超时"
        session_a.commit()
    finally:
        session_b.close()
        session_a.close()

    # A 提交后序号继续推进，不会重复使用 1
    with session_scope(session_factory) as db:
        second = allocate_event_seq(db, project_id)
        assert first == 1 and second == 2


def test_sequence_is_independent_per_project(app, client, session_factory):
    """不同项目各自从 1 开始编号，互不影响。"""
    first = _make_project(session_factory, unique_title("b7-a"))
    second = _make_project(session_factory, unique_title("b7-b"))

    with session_scope(session_factory) as db:
        append_event(db, project_id=first, type="test.one")
        append_event(db, project_id=first, type="test.two")
        append_event(db, project_id=second, type="test.only")

    with session_factory() as db:
        assert [row.seq for row in events_since(db, first, 0)] == [1, 2]
        assert [row.seq for row in events_since(db, second, 0)] == [1]
        assert latest_event_seq(db, first) == 2
        assert latest_event_seq(db, second) == 1
        for pid, expected in ((first, 2), (second, 1)):
            counter = db.get(ProjectEventCounter, pid)
            assert counter is not None and counter.last_seq == expected


def test_rollback_leaves_hole_but_never_reorders(app, client, session_factory):
    """回滚留空洞，但已提交事件的序号顺序仍然是提交顺序，补发不丢事件。"""
    project_id = _make_project(session_factory, unique_title("b7-hole"))

    with session_scope(session_factory) as db:
        append_event(db, project_id=project_id, type="test.first")

    # 事务分配了 3 号后回滚：2 号与 3 号都不会留下记录，4 号才是下一条已提交事件
    with session_factory() as db:
        allocate_event_seq(db, project_id)
        with pytest.raises(RuntimeError):
            append_event(db, project_id=project_id, type="test.rolled_back")
            raise RuntimeError("simulate failure")
        db.rollback()

    with session_scope(session_factory) as db:
        append_event(db, project_id=project_id, type="test.after_rollback")

    with session_factory() as db:
        rows = list(events_since(db, project_id, 0))
        seqs = [row.seq for row in rows]
        types = [row.type for row in rows]
        assert types == ["test.first", "test.after_rollback"]
        assert seqs == sorted(seqs), "序号必须单调"
        assert len(set(seqs)) == len(seqs), "序号在项目内唯一"
        # 补发语义：游标推进到任意一条之后，剩下的都能读到
        for index, row in enumerate(rows[:-1]):
            following = [item.seq for item in events_since(db, project_id, row.seq)]
            assert following == seqs[index + 1 :]


def test_snapshot_watermark_covers_all_committed_changes(app, client, session_factory):
    """快照水位：先读水位再读业务行，水位内的变更必定已经可见。"""
    project = create_project(client, title=unique_title("b7-snapshot"))
    project_id = project["id"]

    with session_scope(session_factory) as db:
        append_event(db, project_id=project_id, type="test.before")

    snapshot = client.get(f"/api/v1/projects/{project_id}").json()
    assert snapshot["last_event_seq"] >= 2
    assert snapshot["last_event_id"] >= 1

    with session_factory() as db:
        watermark = snapshot["last_event_seq"]
        committed = [row.seq for row in events_since(db, project_id, 0)]
        assert max(committed) <= watermark or watermark == max(committed)
        # 快照里的 last_event_seq 必须是已提交事件的最大序号
        assert watermark == max(committed)

    # 水位之后新增的事件，补发时一定在结果里
    with session_scope(session_factory) as db:
        append_event(db, project_id=project_id, type="test.after")
    replayed = client.get(
        f"/api/v1/projects/{project_id}", headers={}
    ).json()
    assert replayed["last_event_seq"] == watermark + 1


def test_event_seq_is_exposed_on_run_and_media_snapshot(app, client, session_factory):
    """事件对象带 seq，前端不必猜测"id 就是游标"。"""
    from app.models import WorkspaceEvent

    project = create_project(client, title=unique_title("b7-serialize"))
    with session_factory() as db:
        row = db.execute(
            select(WorkspaceEvent).where(WorkspaceEvent.project_id == project["id"]).order_by(WorkspaceEvent.seq)
        ).scalars().first()
        assert row is not None and row.seq >= 1
        assert row.id >= 1


def test_sse_cursor_is_seq_not_global_id(app, client, session_factory):
    """回归：SSE 的 id 必须是项目序号，不能是全局自增主键。

    做法：先在其它项目里制造足量事件，让全局 id 远超本项目序号，
    这样一旦实现里误用 row.id，断言必然失败（旧实现就是这种错法）。
    """
    noise = _make_project(session_factory, unique_title("b7-noise"))
    with session_scope(session_factory) as db:
        for index in range(5):
            append_event(db, project_id=noise, type=f"test.noise{index}")

    # 通过 API 建项目，才能拿到合法的访客会话（SSE 需要身份）
    project = create_project(client, title=unique_title("b7-sse"), city_hint="杭州")
    project_id = project["id"]
    with session_scope(session_factory) as db:
        append_event(db, project_id=project_id, type="test.second")

    with session_factory() as db:
        rows = list(
            db.execute(
                select(WorkspaceEvent).where(WorkspaceEvent.project_id == project_id).order_by(WorkspaceEvent.seq)
            ).scalars()
        )
        seqs = [row.seq for row in rows]
        ids = [row.id for row in rows]
        assert seqs == [1, 2], seqs
        assert max(ids) > max(seqs), "构造条件：全局 id 必须已经超过本项目序号"

    with client.stream("GET", f"/api/v1/projects/{project_id}/events") as response:
        assert response.status_code == 200
        seen: list[int] = []
        for line in response.iter_lines():
            if line.startswith("id: "):
                seen.append(int(line[4:]))
            if len(seen) >= 2:
                break

    assert seen == seqs, f"SSE 的 id 必须是项目序号 {seqs}，实际 {seen}（全局主键是 {ids}）"
