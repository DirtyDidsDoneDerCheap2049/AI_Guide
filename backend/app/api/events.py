"""SSE 事件流：按**项目事件序号**增量读取 MySQL，支持 Last-Event-ID 补发。

- 业务事件持久化在 workspace_events；心跳只是注释行，不写业务表。
- B7：``id:`` 字段是项目内事件序号（``workspace_events.seq``），不是全局自增 id。
  序号在写事件的事务内分配并持有行锁到提交，因此序号顺序 = 提交顺序，
  客户端带 Last-Event-ID 重连不会跳过"小序号后提交"的事件。
- 为兼容旧客户端，查询参数 ``last_event_id`` 仍被接受，但语义是序号游标。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from app.api.deps import (
    get_db,
    get_optional_guest_session,
    get_optional_user,
    get_settings_dep,
    load_owned_project,
)
from app.config import Settings
from app.db import session_scope
from app.events import events_since
from app.models import DemoSession, User

router = APIRouter()


def format_event(event_id: int, event_type: str, payload: dict[str, Any], created_at: str) -> str:
    body = json.dumps({"payload": payload, "created_at": created_at}, ensure_ascii=False)
    return f"id: {event_id}\nevent: {event_type}\ndata: {body}\n\n"


def _fetch_events(factory: sessionmaker[Session], project_id: str, last_seq: int) -> list[tuple[int, str, dict, str]]:
    """按项目序号取事件。注意：这里必须用 row.seq，不能用全局自增 row.id，
    否则客户端拿到的游标（id）会被当成 seq 使用，从而漏掉后续所有事件。"""
    with session_scope(factory) as db:
        rows = events_since(db, project_id, last_seq)
        return [
            (row.seq, row.type, row.payload or {}, row.created_at.isoformat())
            for row in rows
        ]


@router.get(
    "/projects/{project_id}/events",
    summary="项目事件流（SSE，支持 Last-Event-ID 补发）",
    response_class=StreamingResponse,
)
async def stream_project_events(
    project_id: str,
    request: Request,
    last_event_id: str | None = Query(default=None, description="从该项目事件序号之后开始补发（兼容字段名）"),
    last_event_seq: str | None = Query(default=None, description="从该项目事件序号之后开始补发"),
    session_row: DemoSession | None = Depends(get_optional_guest_session),
    user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> StreamingResponse:
    # D2：登录用户用自己的账号身份订阅；会话被撤销后这里会直接 401/404，
    # 已撤销的会话不能继续收旧项目的 SSE。
    if session_row is None and user is None:
        # 既没有访客会话也没有登录会话：明确 401，不偷偷创建新会话
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="session_required")
    project = load_owned_project(db, project_id, session_row, user)

    header_last = request.headers.get("last-event-id")
    try:
        start_id = int(last_event_seq or last_event_id or header_last or 0)
    except ValueError:
        start_id = 0

    factory = request.app.state.session_factory

    async def generator() -> AsyncIterator[str]:
        yield "retry: 3000\n\n"
        yield ": connected\n\n"
        last = start_id
        deadline = time.monotonic() + settings.sse_max_stream_seconds
        last_heartbeat = time.monotonic()

        while True:
            if await request.is_disconnected():
                break
            rows = await run_in_threadpool(_fetch_events, factory, project.id, last)
            for seq, event_type, payload, created_at in rows:
                last = seq
                yield format_event(seq, event_type, payload, created_at)

            now = time.monotonic()
            if now - last_heartbeat >= settings.sse_heartbeat_seconds:
                # 心跳不写业务表：只是 SSE 注释行。
                yield f": heartbeat {int(time.time())}\n\n"
                last_heartbeat = now

            if now >= deadline:
                # 主动结束长连接，客户端会自动带 Last-Event-ID 重连，可继续补发。
                break
            await asyncio.sleep(settings.sse_poll_interval_seconds)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
