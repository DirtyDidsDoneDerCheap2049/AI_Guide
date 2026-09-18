"""账户服务（D2）：口令哈希、登录会话、邮件令牌、访客工作区认领。

安全要点：
- 口令只用 Argon2id 哈希，任何日志/响应都不出现明文或哈希。
- 会话令牌与邮件令牌是 32 字节随机值，数据库只存 SHA-256 摘要。
- 令牌校验是"查摘要 + 检查过期/已用/已撤销"，不是把令牌当主键。
- 注册/登录/重置的错误信息不区分"邮箱不存在"与"密码错误"，避免账号枚举。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.events import EventType, append_event
from app.models import (
    AuthSession,
    DemoSession,
    EmailToken,
    GuideProject,
    User,
    WorkspaceClaim,
    utcnow,
)

logger = logging.getLogger("app.services.accounts")

PURPOSE_VERIFY_EMAIL = "verify_email"
PURPOSE_RESET_PASSWORD = "reset_password"

MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 200
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")

# 内存兜底限流：Redis 不可用时按进程计数（宁可保守拒绝，也不完全放开）
_MEMORY_ATTEMPTS: dict[str, tuple[int, float]] = {}


class AccountError(RuntimeError):
    def __init__(self, code: str, *, status_code: int = 400, detail: str | None = None) -> None:
        self.code = code
        self.status_code = status_code
        self.detail = detail
        super().__init__(code)


class EmailTaken(AccountError):
    def __init__(self) -> None:
        super().__init__("email_taken", status_code=409)


class InvalidCredentials(AccountError):
    def __init__(self) -> None:
        super().__init__("invalid_credentials", status_code=401)


class AuthRequired(AccountError):
    def __init__(self) -> None:
        super().__init__("auth_required", status_code=401)


class InvalidToken(AccountError):
    def __init__(self) -> None:
        super().__init__("invalid_token", status_code=400)


class WeakPassword(AccountError):
    def __init__(self, detail: str) -> None:
        super().__init__("password_too_weak", status_code=422, detail=detail)


class RateLimited(AccountError):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after
        super().__init__("too_many_attempts", status_code=429, detail=f"retry_after={retry_after}")


@dataclass
class ClaimOutcome:
    project_id: str
    claimed: bool
    already_claimed: bool


# --------------------------------------------------------------------------- 口令与令牌


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def validate_email(email: str) -> str:
    value = normalize_email(email)
    if not value or len(value) > 254 or not _EMAIL_RE.match(value):
        raise AccountError("invalid_email", status_code=422, detail="邮箱格式不正确")
    return value


def validate_password(password: str) -> str:
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPassword(f"密码至少 {MIN_PASSWORD_LENGTH} 位")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise WeakPassword(f"密码最多 {MAX_PASSWORD_LENGTH} 位")
    if password.strip() == "":
        raise WeakPassword("密码不能只有空白字符")
    return password


def _hasher(settings: Settings):
    from argon2 import PasswordHasher
    from argon2.low_level import Type

    return PasswordHasher(
        time_cost=settings.argon2_time_cost,
        memory_cost=settings.argon2_memory_cost,
        parallelism=settings.argon2_parallelism,
        hash_len=32,
        salt_len=16,
        type=Type.ID,
    )


def hash_password(settings: Settings, password: str) -> str:
    return _hasher(settings).hash(validate_password(password))


def verify_password(settings: Settings, password_hash: str, password: str) -> bool:
    from argon2 import PasswordHasher
    from argon2.exceptions import InvalidHashError, VerifyMismatchError

    ph = PasswordHasher()
    try:
        ph.verify(password_hash, password)
        return True
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False


def password_needs_rehash(settings: Settings, password_hash: str) -> bool:
    try:
        return _hasher(settings).check_needs_rehash(password_hash)
    except Exception:  # noqa: BLE001 - 哈希串异常时按需要重算处理
        return True


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_token() -> str:
    return secrets.token_urlsafe(32)


def hash_ip(settings: Settings, ip: str | None) -> str | None:
    if not ip:
        return None
    secret = settings.session_secret.encode("utf-8")
    return hmac.new(secret, ip.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


# --------------------------------------------------------------------------- 限流


def check_rate_limit(settings: Settings, *, scope: str, key: str, limit: int, window_seconds: int, now: float | None = None) -> None:
    """按 scope+key 限流。Redis 可用时用 INCR/EXPIRE；不可用时用进程内计数兜底。"""
    import time

    moment = now if now is not None else time.time()
    bucket = f"authrl:{scope}:{key}"
    try:
        import redis

        client = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=0.5, socket_timeout=0.5)
        count = client.incr(bucket)
        if count == 1:
            client.expire(bucket, window_seconds)
        ttl = client.ttl(bucket)
        client.close()
        if count > limit:
            raise RateLimited(max(int(ttl), 1))
        return
    except RateLimited:
        raise
    except Exception as exc:  # noqa: BLE001 - Redis 故障时降级
        logger.warning("auth_rate_limit_redis_unavailable", extra={"scope": scope, "error_type": type(exc).__name__})

    count, window_start = _MEMORY_ATTEMPTS.get(bucket, (0, moment))
    if moment - window_start > window_seconds:
        count, window_start = 0, moment
    count += 1
    _MEMORY_ATTEMPTS[bucket] = (count, window_start)
    if count > limit:
        raise RateLimited(max(int(window_seconds - (moment - window_start)), 1))


# --------------------------------------------------------------------------- 会话


def create_session(
    db: Session,
    settings: Settings,
    *,
    user: User,
    user_agent: str | None = None,
    ip: str | None = None,
) -> tuple[str, AuthSession]:
    token = issue_token()
    session = AuthSession(
        user_id=user.id,
        token_hash=_token_digest(token),
        user_agent=(user_agent or "")[:200] or None,
        ip_hash=hash_ip(settings, ip),
        expires_at=utcnow() + timedelta(days=settings.auth_session_days),
    )
    db.add(session)
    db.flush()
    return token, session


def load_session(db: Session, settings: Settings, token: str | None) -> AuthSession | None:
    """按 Cookie 令牌找有效会话；撤销/过期/用户停用一律视为无效。"""
    if not token:
        return None
    session = db.execute(
        select(AuthSession).where(AuthSession.token_hash == _token_digest(token))
    ).scalar_one_or_none()
    if session is None or session.revoked_at is not None:
        return None
    if session.expires_at <= utcnow():
        return None
    user = db.get(User, session.user_id)
    if user is None or user.status != "active":
        return None
    return session


def revoke_session(db: Session, session: AuthSession, *, reason: str = "logout") -> None:
    if session.revoked_at is None:
        session.revoked_at = utcnow()
        session.revoked_reason = reason


def revoke_all_sessions(db: Session, user_id: str, *, reason: str, keep_session_id: str | None = None) -> int:
    count = 0
    for row in db.execute(
        select(AuthSession).where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
    ).scalars():
        if keep_session_id and row.id == keep_session_id:
            continue
        revoke_session(db, row, reason=reason)
        count += 1
    return count


# --------------------------------------------------------------------------- 邮件令牌


def issue_email_token(
    db: Session, settings: Settings, *, user: User, purpose: str, ttl_minutes: int | None = None
) -> str:
    minutes = ttl_minutes or settings.auth_token_ttl_minutes
    token = issue_token()
    db.add(
        EmailToken(
            user_id=user.id,
            purpose=purpose,
            token_hash=_token_digest(token),
            expires_at=utcnow() + timedelta(minutes=minutes),
        )
    )
    db.flush()
    return token


def consume_email_token(db: Session, *, token: str, purpose: str) -> User:
    row = db.execute(
        select(EmailToken).where(
            EmailToken.token_hash == _token_digest(token),
            EmailToken.purpose == purpose,
        )
    ).scalar_one_or_none()
    if row is None or row.used_at is not None or row.expires_at <= utcnow():
        raise InvalidToken()
    user = db.get(User, row.user_id)
    if user is None or user.status != "active":
        raise InvalidToken()
    row.used_at = utcnow()
    return user


# --------------------------------------------------------------------------- 注册 / 登录


def register_user(
    db: Session,
    settings: Settings,
    *,
    email: str,
    password: str,
    display_name: str | None = None,
) -> tuple[User, str, AuthSession]:
    """创建账号并发登录会话。重复邮箱抛 EmailTaken（不区分是否已验证）。"""
    normalized = validate_email(email)
    password_hash = hash_password(settings, password)

    existing = db.execute(select(User).where(User.email == normalized)).scalar_one_or_none()
    if existing is not None:
        raise EmailTaken()

    user = User(
        email=normalized,
        password_hash=password_hash,
        display_name=(display_name or "").strip()[:60] or None,
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise EmailTaken() from None
    token, session = create_session(db, settings, user=user)
    user.last_login_at = utcnow()
    return user, token, session


def authenticate(db: Session, settings: Settings, *, email: str, password: str) -> User:
    normalized = normalize_email(email)
    user = db.execute(select(User).where(User.email == normalized)).scalar_one_or_none()
    if user is None or user.status != "active":
        # 仍然做一次哈希运算，避免通过响应时间区分"账号不存在"
        verify_password(settings, "$argon2id$v=19$m=19456,t=2,p=1$c29tZXNhbHR2YWx1ZQ$" + "0" * 43, password)
        raise InvalidCredentials()
    if not verify_password(settings, user.password_hash, password):
        raise InvalidCredentials()
    if password_needs_rehash(settings, user.password_hash):
        user.password_hash = hash_password(settings, password)
    user.last_login_at = utcnow()
    return user


def change_password(
    db: Session, settings: Settings, *, user: User, current_password: str, new_password: str, keep_session_id: str | None
) -> int:
    if not verify_password(settings, user.password_hash, current_password):
        raise InvalidCredentials()
    user.password_hash = hash_password(settings, new_password)
    user.password_changed_at = utcnow()
    revoked = revoke_all_sessions(db, user.id, reason="password_changed", keep_session_id=keep_session_id)
    return revoked


def reset_password(db: Session, settings: Settings, *, user: User, new_password: str) -> int:
    user.password_hash = hash_password(settings, new_password)
    user.password_changed_at = utcnow()
    return revoke_all_sessions(db, user.id, reason="password_reset")


# --------------------------------------------------------------------------- 工作区认领


def claim_workspace(
    db: Session,
    *,
    project: GuideProject,
    user: User,
    guest_session: DemoSession | None,
) -> ClaimOutcome:
    """把访客工作区转给登录用户。幂等：重复调用返回同一条认领记录。"""
    existing = db.execute(
        select(WorkspaceClaim).where(WorkspaceClaim.project_id == project.id)
    ).scalar_one_or_none()
    if existing is not None:
        if existing.user_id != user.id:
            # 已属于别的账号：不泄露内容，按"不属于你"处理
            raise AccountError("project_not_found", status_code=404)
        return ClaimOutcome(project_id=project.id, claimed=False, already_claimed=True)

    if project.owner_user_id is not None and project.owner_user_id != user.id:
        raise AccountError("project_not_found", status_code=404)

    project.owner_user_id = user.id
    project.updated_at = utcnow()
    db.add(
        WorkspaceClaim(
            project_id=project.id,
            user_id=user.id,
            guest_session_id=guest_session.id if guest_session is not None else None,
        )
    )
    append_event(
        db,
        project_id=project.id,
        type=EventType.PROJECT_CLAIMED,
        payload={"user_id": user.id, "guest_session_id": guest_session.id if guest_session else None},
    )
    try:
        db.flush()
    except IntegrityError:
        # 并发认领：唯一约束兜底，回滚后按"已认领"返回
        db.rollback()
        again = db.execute(
            select(WorkspaceClaim).where(WorkspaceClaim.project_id == project.id)
        ).scalar_one_or_none()
        if again is not None and again.user_id == user.id:
            return ClaimOutcome(project_id=project.id, claimed=False, already_claimed=True)
        raise
    return ClaimOutcome(project_id=project.id, claimed=True, already_claimed=False)


def user_trips(db: Session, user_id: str, *, limit: int = 50) -> list[GuideProject]:
    return list(
        db.execute(
            select(GuideProject)
            .where(GuideProject.owner_user_id == user_id, GuideProject.status != "deleted")
            .order_by(GuideProject.updated_at.desc())
            .limit(limit)
        ).scalars()
    )


def cleanup_account(
    db: Session, *, user: User, now: datetime | None = None
) -> dict[str, int]:
    """账号删除：停用账号 + 撤销全部会话 + 清除未用令牌；工作区保留但归还为无人拥有。

    这是"可追溯清理"：不物理删除工作区与讲解（用户资料可能仍需要导出），
    但账号立刻不可登录、所有会话失效、邮件令牌作废。
    """
    moment = now or utcnow()
    user.status = "disabled"
    user.updated_at = moment
    revoked = revoke_all_sessions(db, user.id, reason="account_disabled")
    tokens = 0
    for row in db.execute(
        select(EmailToken).where(EmailToken.user_id == user.id, EmailToken.used_at.is_(None))
    ).scalars():
        row.used_at = moment
        tokens += 1
    projects = 0
    for project in db.execute(select(GuideProject).where(GuideProject.owner_user_id == user.id)).scalars():
        project.owner_user_id = None
        project.updated_at = moment
        projects += 1
    return {"sessions_revoked": revoked, "tokens_invalidated": tokens, "projects_released": projects}
