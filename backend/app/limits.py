"""公开 Demo 额度：并发安全的预留式限流（B6）。

设计要点：
- 额度以 MySQL 的 ``quota_buckets`` 为事实来源，用「条件更新」原子扣减：
  只有 ``used < limit_value`` 时才会 +1，受影响行数为 0 即表示超额。
- 三个维度一起预留（全站 / 会话 / 入口 IP），任一超额即整笔回滚，不会出现"扣了一半"。
- 浏览、下载、读历史不计额度；只有昂贵的**新任务**才预留。
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AgentRun, GuideProject, QuotaBucket


class QuotaExceeded(RuntimeError):
    def __init__(self, scope: str, limit: int) -> None:
        self.scope = scope
        self.limit = limit
        super().__init__(f"quota_exceeded:{scope}")


def _reserve_bucket(db: Session, scope: str, key: str, limit: int, day: date) -> bool:
    """原子占一个名额；返回 False 表示已满。

    - 先在命名锁里串行化同一额度键，避免并发 UPDATE/INSERT 触发 InnoDB 死锁。
    - 锁内先做条件 UPDATE（WHERE used < :limit），命中即成功；
      行不存在时 `INSERT IGNORE` 直接以 used=1 落库；两者都没成功说明已满。
    - 不依赖 rowcount 判定"未变化"：这里的 UPDATE 只要命中就一定 +1，
      因此 rowcount 语义可靠（PyMySQL 默认 FOUND_ROWS 只影响未变化的更新）。
    """
    params = {"scope": scope, "key": key, "day": day, "limit": limit}
    lock_name = f"quota:{scope}:{key}"
    update_sql = text(
        "UPDATE quota_buckets SET used = used + 1, limit_value = :limit, updated_at = UTC_TIMESTAMP() "
        "WHERE scope = :scope AND bucket_key = :key AND bucket_day = :day AND used < :limit"
    )
    insert_sql = text(
        "INSERT IGNORE INTO quota_buckets (scope, bucket_key, bucket_day, used, limit_value, updated_at) "
        "VALUES (:scope, :key, :day, 1, :limit, UTC_TIMESTAMP())"
    )

    db.execute(text("SELECT GET_LOCK(:name, 5)"), {"name": lock_name})
    try:
        if db.execute(update_sql, params).rowcount == 1:
            return True
        if db.execute(insert_sql, params).rowcount == 1:
            return True
        return False
    finally:
        # 命名锁不随事务回滚释放，必须显式释放
        db.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": lock_name})


def reserve_run_quota(db: Session, settings: Settings, session_row, client_hash: str | None) -> None:
    """为新任务预留额度。超限抛 QuotaExceeded；调用方负责回滚事务。"""
    day = date.today()
    buckets = [
        ("global", "all", settings.demo_daily_run_limit),
        ("session", session_row.id, settings.demo_session_run_limit),
    ]
    if client_hash:
        buckets.append(("ip", client_hash, settings.demo_ip_daily_limit))

    for scope, key, limit in buckets:
        if not _reserve_bucket(db, scope, key, limit, day):
            raise QuotaExceeded(scope, limit)


def release_run_quota(db: Session, session_row, client_hash: str | None) -> None:
    """创建失败时归还预留（同一事务内回滚更常见，这里是事务已提交后的补偿路径）。"""
    day = date.today()
    keys = [("global", "all"), ("session", session_row.id)]
    if client_hash:
        keys.append(("ip", client_hash))
    for scope, key in keys:
        db.execute(
            text(
                "UPDATE quota_buckets SET used = GREATEST(used - 1, 0) "
                "WHERE scope = :scope AND bucket_key = :key AND bucket_day = :day"
            ),
            {"scope": scope, "key": key, "day": day},
        )


def daily_runs_used(db: Session, day: date | None = None) -> int:
    """展示用：当日已创建的运行数（事实来源仍是 agent_runs）。"""
    target = day or date.today()
    statement = select(func.count(AgentRun.id)).where(func.date(AgentRun.created_at) == target)
    return int(db.execute(statement).scalar() or 0)


def bucket_snapshot(db: Session, session_id: str, client_hash: str | None) -> dict[str, int]:
    """读取当前维度用量，供 API 展示（不修改额度）。"""
    day = date.today()
    keys = [("global", "all"), ("session", session_id)]
    if client_hash:
        keys.append(("ip", client_hash))
    snapshot: dict[str, int] = {}
    for scope, key in keys:
        row = db.execute(
            select(QuotaBucket.used).where(
                QuotaBucket.scope == scope, QuotaBucket.bucket_key == key, QuotaBucket.bucket_day == day
            )
        ).scalar()
        snapshot[f"{scope}"] = int(row or 0)
    return snapshot


def project_ids_for_session(db: Session, session_id: str) -> list[str]:
    statement = select(GuideProject.id).where(GuideProject.owner_session_id == session_id)
    return [row[0] for row in db.execute(statement).all()]


def check_run_quota(db: Session, settings: Settings, session_row, client_hash: str | None = None) -> None:
    """兼容旧调用名：等价于 reserve_run_quota（预留式，不再先 count 再递增）。"""
    reserve_run_quota(db, settings, session_row, client_hash)
