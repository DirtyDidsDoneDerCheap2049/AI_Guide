"""高德 JS API 2.0 安全代理（受限白名单，禁止任意 URL 转发）。

背景：JS API 2.0 要求把 JS 安全密钥（jscode）随请求发送。若把它写进前端，任何访问者都能取走。
官方方案是在服务端做一层代理：浏览器把请求发给同源 ``/_AMapService/<原路径>``，
代理校验目标主机在白名单内、追加 jscode 后再转发，并把响应原样返回。

安全边界：
- 目标主机必须来自 ``AMAP_JS_PROXY_HOSTS`` 白名单；不存在的目标直接 403。
- 客户端自带的 ``jscode`` 一律被覆盖，避免伪造或重复。
- 不跟随重定向；不转发 Cookie/Authorization；日志只记录主机、路径、状态与耗时。
"""

from __future__ import annotations

import logging
import time
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request, Response

from app.config import Settings

logger = logging.getLogger("app.api.amap_proxy")

router = APIRouter()

# 路径前缀 -> 默认目标主机。JS SDK 的多数请求走 /v3、/v4、/v5（restapi），
# 矢量底图与数据服务走 webapi/vdata；客户端也可用 host 参数显式指定（仍受白名单约束）。
PATH_PREFIX_HOSTS: tuple[tuple[str, str], ...] = (
    ("/vdata", "vdata.amap.com"),
    ("/webapi", "webapi.amap.com"),
    ("/v3", "restapi.amap.com"),
    ("/v4", "restapi.amap.com"),
    ("/v5", "restapi.amap.com"),
    ("/ws", "restapi.amap.com"),
)


def resolve_target_host(path: str, requested_host: str | None, settings: Settings) -> tuple[str | None, str | None]:
    """返回 (host, error_code)。host 为空表示拒绝。"""
    allow = settings.amap_proxy_host_list
    candidate = (requested_host or "").strip().lower()
    if candidate:
        if candidate not in allow:
            return None, "host_not_allowed"
        return candidate, None

    for prefix, host in PATH_PREFIX_HOSTS:
        if path.startswith(prefix):
            return (host, None) if host in allow else (None, "host_not_allowed")
    return None, "path_not_allowed"


def _merge_jscode(query: str, security_code: str) -> str:
    """覆盖客户端传来的 jscode，只保留服务端配置的安全密钥。"""
    pairs = [
        (key, value)
        for key, value in (item.split("=", 1) if "=" in item else (item, "") for item in query.split("&"))
        if key and key != "jscode"
    ]
    pairs.append(("jscode", security_code))
    return urlencode(pairs)


@router.api_route(
    "/{amap_path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    include_in_schema=False,
)
async def proxy_amap(amap_path: str, request: Request) -> Response:  # noqa: D401
    settings: Settings = request.app.state.settings
    if not settings.amap_js_security_code:
        # 未配置安全密钥时不转发，给出可读错误，前端据此提示而不是白屏。
        return Response(
            content=b'{"status":"0","info":"amap_proxy_not_configured"}',
            status_code=503,
            media_type="application/json",
        )

    path = f"/{amap_path}" if not amap_path.startswith("/") else amap_path
    requested_host = request.query_params.get("host")
    host, error = resolve_target_host(path, requested_host, settings)
    if host is None:
        logger.warning("amap_proxy_rejected", extra={"path": path, "reason": error})
        return Response(content=b'{"status":"0","info":"amap_proxy_rejected"}', status_code=403, media_type="application/json")

    body = await request.body()
    query = _merge_jscode(request.url.query, settings.amap_js_security_code)
    url = f"{settings.amap_js_proxy_scheme}://{host}{path}?{query}"

    forwarded_headers = {"user-agent": request.headers.get("user-agent", "ai-guide-amap-proxy")}
    content_type = request.headers.get("content-type")
    if content_type:
        forwarded_headers["content-type"] = content_type

    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=settings.amap_js_proxy_timeout_seconds, follow_redirects=False) as client:
            upstream = await client.request(request.method, url, content=body or None, headers=forwarded_headers)
    except httpx.HTTPError as exc:
        logger.warning("amap_proxy_upstream_error", extra={"host": host, "path": path, "error_type": type(exc).__name__})
        return Response(content=b'{"status":"0","info":"amap_proxy_upstream_error"}', status_code=502, media_type="application/json")

    logger.info(
        "amap_proxy_forwarded",
        extra={
            "host": host,
            "path": path,
            "method": request.method,
            "status": upstream.status_code,
            "duration_ms": int((time.monotonic() - started) * 1000),
        },
    )
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/json"),
        headers={"Cache-Control": "no-store"},
    )
