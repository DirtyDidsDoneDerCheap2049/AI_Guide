"""FastAPI 应用入口。

启动时只做：日志、配置校验、媒体目录准备。不建表、不连接 Worker、不发起任何外部调用。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.api import amap_proxy as amap_proxy_api
from app.api import auth as auth_api
from app.api import events as events_api
from app.api import health as health_api
from app.api import media as media_api
from app.api import messages as messages_api
from app.api import discovery as discovery_api
from app.api import places_routes as places_routes_api
from app.api import projects as projects_api
from app.api import runs as runs_api
from app.api import sessions as sessions_api
from app.config import ConfigError, Settings, get_settings
from app.db import create_db_engine, create_session_factory
from app.observability import configure_logging

logger = logging.getLogger("app.main")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    settings.media_root.mkdir(parents=True, exist_ok=True)

    missing = settings.missing_provider_config()
    if missing:
        # 只报告变量名，绝不报告值。
        raise ConfigError(missing, "missing_configuration")

    if settings.provider_mode == "mock" and settings.is_production:
        raise ConfigError(["PROVIDER_MODE"], "invalid_configuration_for_production")

    if settings.provider_mode == "mock":
        logger.warning(
            "provider_mode_is_mock",
            extra={"app_env": settings.app_env, "note": "仅允许测试环境使用 fixture Provider"},
        )

    logger.info("api_startup", extra=settings.public_summary())
    yield
    logger.info("api_shutdown")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    from app.services.model_options import profiles
    profiles(settings)
    configure_logging(settings.log_level)

    app = FastAPI(
        title="Tripwright API",
        version=settings.app_version or __version__,
        description="桌面端视觉导游 Agent 的 v0.1 后端：项目、图片、受控 Agent 运行、持久事件与 SSE。",
        lifespan=lifespan,
    )
    app.state.settings = settings
    engine = create_db_engine(settings)
    app.state.db_engine = engine
    app.state.session_factory = create_session_factory(engine)

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.exception_handler(ConfigError)
    async def _config_error_handler(_: Request, exc: ConfigError) -> JSONResponse:
        # 只返回变量名，永远不返回配置值。
        return JSONResponse(
            status_code=500,
            content={"detail": "configuration_error", "reason": exc.reason, "variables": exc.names},
        )

    app.include_router(health_api.router, prefix="/api/health", tags=["health"])
    app.include_router(sessions_api.router, prefix="/api/v1", tags=["sessions"])
    app.include_router(auth_api.router, prefix="/api/v1", tags=["auth"])
    app.include_router(projects_api.router, prefix="/api/v1", tags=["projects"])
    app.include_router(media_api.router, prefix="/api/v1", tags=["media"])
    app.include_router(runs_api.router, prefix="/api/v1", tags=["runs"])
    app.include_router(messages_api.router, prefix="/api/v1", tags=["messages"])
    app.include_router(discovery_api.router, prefix="/api/v1", tags=["discovery"])
    app.include_router(places_routes_api.router, prefix="/api/v1", tags=["places-routes"])
    app.include_router(events_api.router, prefix="/api/v1", tags=["events"])
    # 高德 JS 安全代理：路径由配置决定（默认 /_AMapService），必须与前端 serviceHost 一致。
    app.include_router(amap_proxy_api.router, prefix=settings.amap_js_proxy_path, tags=["amap-proxy"])

    return app


app = None  # 由 ASGI 入口 `app.asgi:app` 显式构造，避免导入期读配置失败阻塞测试收集。
