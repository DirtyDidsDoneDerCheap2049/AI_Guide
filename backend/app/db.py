"""数据库引擎与会话工厂。

MySQL 是唯一事实来源：这里只负责连接和事务边界，schema 一律由 Alembic 迁移维护，
不在应用启动时自动建表。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import Settings

# 统一约束命名，保证 Alembic 迁移在不同环境下生成一致的 DDL。
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


_engines: dict[str, Engine] = {}
_factories: dict[str, sessionmaker[Session]] = {}


def create_db_engine(settings: Settings) -> Engine:
    url = settings.database_url
    engine = _engines.get(url)
    if engine is None:
        engine = create_engine(
            url,
            pool_pre_ping=True,
            pool_recycle=1800,
            pool_size=5,
            max_overflow=10,
            future=True,
        )
        _engines[url] = engine
    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    key = str(engine.url)
    factory = _factories.get(key)
    if factory is None:
        factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
        _factories[key] = factory
    return factory


def session_factory_for(settings: Settings) -> sessionmaker[Session]:
    return create_session_factory(create_db_engine(settings))


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """事务边界：正常提交，异常回滚。"""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def check_database(engine: Engine) -> tuple[bool, str | None]:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, None
    except Exception as exc:  # pragma: no cover - 由健康检查与测试覆盖
        return False, type(exc).__name__


def current_migration_revision(engine: Engine) -> str | None:
    """读取 alembic_version；表不存在时返回 None（说明迁移未执行）。"""
    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).first()
        return None if row is None else str(row[0])
    except Exception:
        return None


def dispose_engines() -> None:
    for engine in _engines.values():
        engine.dispose()
    _engines.clear()
    _factories.clear()
