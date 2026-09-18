"""测试夹具：真实 MySQL + 真实 Redis。

环境变量（可覆盖）：
- ``AI_GUIDE_TEST_DATABASE_URL`` 默认 ``mysql+pymysql://root@127.0.0.1:3307/ai_guide_test``
- ``AI_GUIDE_TEST_REDIS_URL``    默认 ``redis://127.0.0.1:6380/1``

测试库会在会话开始时被清空并由 Alembic 迁移到 head —— 不做 ORM 自动建表。
"""

from __future__ import annotations

import io
import os
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
MEDIA_ROOT = REPO_ROOT / "work" / ".pytest-media"

TEST_DATABASE_URL = os.environ.get(
    "AI_GUIDE_TEST_DATABASE_URL",
    "mysql+pymysql://root@127.0.0.1:3307/ai_guide_test?charset=utf8mb4",
)
# 本地开发 Redis 为 5.0.14（Windows 移植版），不支持 RESP3 的 HELLO，故显式 RESP2。
TEST_REDIS_URL = os.environ.get("AI_GUIDE_TEST_REDIS_URL", "redis://127.0.0.1:6380/1?protocol=2")

# 必须在导入 app.* 之前设置：worker/actors.py 在导入期读取配置。
os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["REDIS_URL"] = TEST_REDIS_URL
os.environ["SESSION_SECRET"] = "test-only-session-secret"
os.environ["PROVIDER_MODE"] = "mock"
os.environ["MEDIA_ROOT"] = str(MEDIA_ROOT)
os.environ["LOG_LEVEL"] = "WARNING"
os.environ["DEMO_SESSION_RUN_LIMIT"] = "50"
os.environ["DEMO_DAILY_RUN_LIMIT"] = "5000"
os.environ["SSE_POLL_INTERVAL_SECONDS"] = "0.05"
os.environ["SSE_HEARTBEAT_SECONDS"] = "0.2"
os.environ["SSE_MAX_STREAM_SECONDS"] = "1"
os.environ["FIXTURE_BEHAVIORS"] = ""


@pytest.fixture(scope="session")
def settings():
    from app.config import load_settings, reset_settings_cache

    reset_settings_cache()
    return load_settings(_env_file=None)


def _drop_all_tables(engine) -> None:
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        rows = conn.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE()")
        ).all()
        for (name,) in rows:
            conn.execute(text(f"DROP TABLE IF EXISTS `{name}`"))
        conn.execute(text("SET FOREIGN_KEY_CHECKS=1"))


def _truncate_all_tables(engine) -> None:
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        rows = conn.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE()")
        ).all()
        for (name,) in rows:
            # alembic_version 记录迁移状态，不能被清空。
            if name == "alembic_version":
                continue
            conn.execute(text(f"TRUNCATE TABLE `{name}`"))
        conn.execute(text("SET FOREIGN_KEY_CHECKS=1"))


def run_alembic_upgrade(revision: str = "head") -> None:
    from alembic import command
    from alembic.config import Config as AlembicConfig

    config = AlembicConfig(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    command.upgrade(config, revision)


@pytest.fixture(scope="session")
def engine(settings):
    from app.db import create_db_engine

    eng = create_db_engine(settings)
    _drop_all_tables(eng)
    run_alembic_upgrade("head")
    MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
    yield eng
    from app.db import dispose_engines

    dispose_engines()


@pytest.fixture(autouse=True)
def clean_state(engine):
    from app.agent.providers.fixtures import parse_behaviors  # noqa: F401  (确保 fixture 模块可导入)

    _truncate_all_tables(engine)
    _flush_redis()
    yield
    _flush_redis()


def _flush_redis() -> None:
    try:
        import redis

        client = redis.Redis.from_url(TEST_REDIS_URL, socket_connect_timeout=1.0, socket_timeout=1.0)
        client.flushdb()
        client.close()
    except Exception:
        # Redis 不可用时由具体测试自行断言（例如“Redis 中断”故障测试）。
        pass


@pytest.fixture
def session_factory(settings):
    from app.db import session_factory_for

    return session_factory_for(settings)


@pytest.fixture
def db(session_factory):
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def app(settings):
    from app.main import create_app

    application = create_app(settings)
    return application


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def providers(settings):
    """fixture Provider（自动回归专用，输出带 [FIXTURE] 标记）。"""
    from app.agent.providers import build_provider_set

    return build_provider_set(settings)


def make_image_bytes(fmt: str = "PNG", size: tuple[int, int] = (64, 64), color: tuple[int, int, int] = (120, 160, 200)) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format=fmt)
    return buffer.getvalue()


def unique_title(prefix: str = "project") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def create_project(client, title: str | None = None, city_hint: str = "杭州") -> dict:
    response = client.post("/api/v1/projects", json={"title": title or unique_title(), "city_hint": city_hint})
    assert response.status_code == 201, response.text
    return response.json()


def upload_media(client, project_id: str, *, fmt: str = "PNG", start_analysis: bool = True, idempotency_key: str | None = None) -> dict:
    files = {"file": (f"sample.{fmt.lower()}", make_image_bytes(fmt), f"image/{fmt.lower()}")}
    data = {"start_analysis": "true" if start_analysis else "false"}
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
    response = client.post(f"/api/v1/projects/{project_id}/media", files=files, data=data, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()
