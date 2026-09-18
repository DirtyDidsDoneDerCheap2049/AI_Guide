"""健康检查：/api/health/live 与 /api/health/ready。

ready 会真实检查 MySQL 连接、迁移版本和 Redis 连接；任一失败返回 503 并给出原因码。
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from app.config import Settings
from app.db import check_database, current_migration_revision
from app.schemas import HealthOut

router = APIRouter()


def _expected_migration_head() -> str | None:
    """读取 Alembic head（不建立数据库连接）。"""
    try:
        from alembic.config import Config as AlembicConfig
        from alembic.script import ScriptDirectory

        from app.config import BACKEND_DIR

        config = AlembicConfig(str(BACKEND_DIR / "alembic.ini"))
        config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
        return ScriptDirectory.from_config(config).get_current_head()
    except Exception:
        return None


def _redis_ping(settings: Settings) -> tuple[bool, str | None]:
    try:
        import redis

        # 显式使用 RESP2：redis-py 6+ 默认走 RESP3（HELLO 3），而 RESP3 需要 Redis 6+。
        # RESP2 在所有版本上都可用（含 5.x 与 Windows 移植版），避免开发/生产版本差异导致误报。
        client = redis.Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=1.0,
            socket_timeout=1.0,
            protocol=2,
        )
        client.ping()
        client.close()
        return True, None
    except Exception as exc:
        return False, type(exc).__name__


@router.get("/live", response_model=HealthOut, summary="存活检查")
def live(request: Request) -> HealthOut:
    settings: Settings = request.app.state.settings
    return HealthOut(
        status="ok",
        version=settings.app_version,
        app_env=settings.app_env,
        checks={"process": "up"},
    )


@router.get(
    "/ready",
    response_model=HealthOut,
    summary="就绪检查（MySQL、迁移版本、Redis）",
    responses={503: {"model": HealthOut}},
)
def ready(request: Request, response: Response) -> HealthOut:
    settings: Settings = request.app.state.settings
    engine = request.app.state.db_engine

    db_ok, db_error = check_database(engine)
    revision = current_migration_revision(engine) if db_ok else None
    head = _expected_migration_head()
    migration_ok = bool(db_ok and head and revision == head)
    redis_ok, redis_error = _redis_ping(settings)

    checks = {
        "mysql": "up" if db_ok else "down",
        "mysql_error_type": db_error,
        "redis": "up" if redis_ok else "down",
        "redis_error_type": redis_error,
        "migration_head": head,
        "migration_current": revision,
        "provider_mode": settings.provider_mode,
    }
    ok = db_ok and redis_ok and migration_ok
    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthOut(
        status="ok" if ok else "unavailable",
        version=settings.app_version,
        app_env=settings.app_env,
        checks=checks,
    )
