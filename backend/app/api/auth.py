"""账户 API（D2）：注册、登录、登出、邮箱验证、密码重置/修改、会话管理、旅行列表、访客认领。

账户边界：
- 口令与令牌细节在 app/services/accounts.py；本模块只做 HTTP 边界与 Cookie。
- 错误码稳定且不泄露账号是否存在。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import (
    client_ip,
    get_auth_session,
    get_client_hash,
    get_current_session,
    get_db,
    get_optional_user,
    get_settings_dep,
    load_owned_project,
    require_user,
)
from app.api.serialize import project_to_out, user_to_out
from app.config import Settings
from app.models import (
    AuthSession,
    DemoSession,
    GuideProject,
    MediaAsset,
    User,
    utcnow,
)
from app.schemas import (
    AccountStatusOut,
    AuthSessionListOut,
    AuthSessionOut,
    ClaimRequest,
    ClaimResponse,
    LoginRequest,
    MeResponse,
    PasswordChangeRequest,
    PasswordForgotRequest,
    PasswordResetRequest,
    RegisterRequest,
    RegisterResponse,
    TripListOut,
    VerifyEmailRequest,
)
from app.security import clear_auth_cookie, set_auth_cookie
from app.services import accounts, email as email_service

logger = logging.getLogger("app.api.auth")

router = APIRouter()


def require_auth_enabled(settings: Settings = Depends(get_settings_dep)) -> None:
    """特性开关：关闭时只拒绝新入口，不删除任何账号或数据。"""
    if not settings.auth_enabled:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail={"code": "auth_disabled"})

VERIFY_PURPOSE = accounts.PURPOSE_VERIFY_EMAIL
RESET_PURPOSE = accounts.PURPOSE_RESET_PASSWORD


def _account_error(exc: accounts.AccountError) -> HTTPException:
    detail: dict[str, object] = {"code": exc.code}
    if exc.detail:
        detail["message"] = exc.detail
    headers = {"Retry-After": str(exc.retry_after)} if isinstance(exc, accounts.RateLimited) else None
    return HTTPException(status_code=exc.status_code, detail=detail, headers=headers)


def _media_count(db: Session, project_id: str) -> int:
    return int(
        db.execute(
            select(func.count(MediaAsset.id)).where(
                MediaAsset.project_id == project_id, MediaAsset.deleted_at.is_(None)
            )
        ).scalar()
        or 0
    )


def _send_verification(db: Session, settings: Settings, user: User) -> bool:
    token = accounts.issue_email_token(db, settings, user=user, purpose=VERIFY_PURPOSE)
    return email_service.send_or_log(settings, email_service.verification_message(settings, to=user.email, token=token))


@router.post(
    "/auth/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="注册账号（可选同时认领当前访客工作区）",
)
def register(
    payload: RegisterRequest,
    request: Request,
    response: Response,
    session_row: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    client_hash: str = Depends(get_client_hash),
    _flag: None = Depends(require_auth_enabled),
) -> RegisterResponse:
    email_key = accounts.normalize_email(payload.email)
    try:
        accounts.check_rate_limit(
            settings,
            scope="register",
            key=f"{client_hash}:{email_key}",
            limit=settings.auth_register_attempt_limit,
            window_seconds=settings.auth_rate_limit_window_seconds,
        )
    except accounts.AccountError as exc:
        raise _account_error(exc) from None

    try:
        user, token, session = accounts.register_user(
            db, settings, email=payload.email, password=payload.password, display_name=payload.display_name
        )
    except accounts.AccountError as exc:
        db.rollback()
        raise _account_error(exc) from None

    claimed: str | None = None
    if payload.claim_project_id:
        project = load_owned_project(db, payload.claim_project_id, session_row, user)
        try:
            outcome = accounts.claim_workspace(db, project=project, user=user, guest_session=session_row)
            claimed = outcome.project_id
        except accounts.AccountError as exc:
            db.rollback()
            raise _account_error(exc) from None

    email_sent = _send_verification(db, settings, user)
    session.user_agent = (request.headers.get("user-agent") or "")[:200] or None
    session.ip_hash = accounts.hash_ip(settings, client_ip(request, settings))
    db.commit()
    set_auth_cookie(response, settings, token)
    return RegisterResponse(
        user=user_to_out(user),
        claimed_project_id=claimed,
        email_verification_required=user.email_verified_at is None,
        email_sent=email_sent,
    )


@router.post("/auth/login", response_model=RegisterResponse, summary="登录（可选同时认领当前访客工作区）")
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session_row: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    client_hash: str = Depends(get_client_hash),
    _flag: None = Depends(require_auth_enabled),
) -> RegisterResponse:
    email_key = accounts.normalize_email(payload.email)
    try:
        accounts.check_rate_limit(
            settings,
            scope="login",
            key=f"{client_hash}:{email_key}",
            limit=settings.auth_login_attempt_limit,
            window_seconds=settings.auth_rate_limit_window_seconds,
        )
        user = accounts.authenticate(db, settings, email=payload.email, password=payload.password)
    except accounts.AccountError as exc:
        db.rollback()
        raise _account_error(exc) from None

    claimed: str | None = None
    if payload.claim_project_id:
        project = load_owned_project(db, payload.claim_project_id, session_row, user)
        try:
            outcome = accounts.claim_workspace(db, project=project, user=user, guest_session=session_row)
            claimed = outcome.project_id
        except accounts.AccountError as exc:
            db.rollback()
            raise _account_error(exc) from None

    token, session = accounts.create_session(
        db,
        settings,
        user=user,
        user_agent=request.headers.get("user-agent"),
        ip=client_ip(request, settings),
    )
    db.commit()
    set_auth_cookie(response, settings, token)
    logger.info("auth_login", extra={"user_id": user.id, "claimed": bool(claimed)})
    return RegisterResponse(
        user=user_to_out(user),
        claimed_project_id=claimed,
        email_verification_required=user.email_verified_at is None,
        email_sent=True,
    )


@router.post("/auth/logout", response_model=AccountStatusOut, summary="退出登录（撤销当前会话）")
def logout(
    response: Response,
    auth_session: AuthSession | None = Depends(get_auth_session),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> AccountStatusOut:
    if auth_session is not None:
        accounts.revoke_session(db, auth_session, reason="logout")
        db.commit()
    clear_auth_cookie(response, settings)
    return AccountStatusOut(status="logged_out")


@router.get("/auth/me", response_model=MeResponse, summary="当前登录用户")
def me(user: User | None = Depends(get_optional_user)) -> MeResponse:
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": "auth_required"})
    return MeResponse(user=user_to_out(user))


@router.post("/auth/verify-email", response_model=MeResponse, summary="用邮件令牌验证邮箱")
def verify_email(
    payload: VerifyEmailRequest,
    db: Session = Depends(get_db),
) -> MeResponse:
    try:
        user = accounts.consume_email_token(db, token=payload.token, purpose=VERIFY_PURPOSE)
    except accounts.AccountError as exc:
        db.rollback()
        raise _account_error(exc) from None
    user.email_verified_at = utcnow()
    db.commit()
    return MeResponse(user=user_to_out(user))


@router.post("/auth/verify-email/resend", response_model=AccountStatusOut, summary="重发验证邮件")
def resend_verification(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    client_hash: str = Depends(get_client_hash),
) -> AccountStatusOut:
    try:
        accounts.check_rate_limit(
            settings,
            scope="verify",
            key=f"{client_hash}:{user.email}",
            limit=settings.auth_email_attempt_limit,
            window_seconds=settings.auth_rate_limit_window_seconds,
        )
    except accounts.AccountError as exc:
        raise _account_error(exc) from None

    if user.email_verified_at is not None:
        return AccountStatusOut(status="already_verified")
    sent = _send_verification(db, settings, user)
    db.commit()
    return AccountStatusOut(status="sent" if sent else "send_failed")


@router.post(
    "/auth/password/forgot",
    response_model=AccountStatusOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="申请重置密码（永远返回已受理，不泄露邮箱是否存在）",
)
def forgot_password(
    payload: PasswordForgotRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    client_hash: str = Depends(get_client_hash),
) -> AccountStatusOut:
    email_key = accounts.normalize_email(payload.email)
    try:
        accounts.check_rate_limit(
            settings,
            scope="forgot",
            key=f"{client_hash}:{email_key}",
            limit=settings.auth_email_attempt_limit,
            window_seconds=settings.auth_rate_limit_window_seconds,
        )
    except accounts.AccountError as exc:
        raise _account_error(exc) from None

    user = db.execute(select(User).where(User.email == email_key)).scalar_one_or_none()
    if user is not None and user.status == "active":
        token = accounts.issue_email_token(db, settings, user=user, purpose=RESET_PURPOSE)
        email_service.send_or_log(settings, email_service.reset_message(settings, to=user.email, token=token))
        db.commit()
    return AccountStatusOut(status="accepted")


@router.post("/auth/password/reset", response_model=AccountStatusOut, summary="用令牌重置密码（撤销全部会话）")
def reset_password(
    payload: PasswordResetRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> AccountStatusOut:
    try:
        user = accounts.consume_email_token(db, token=payload.token, purpose=RESET_PURPOSE)
        revoked = accounts.reset_password(db, settings, user=user, new_password=payload.new_password)
    except accounts.AccountError as exc:
        db.rollback()
        raise _account_error(exc) from None
    db.commit()
    logger.info("password_reset", extra={"user_id": user.id, "sessions_revoked": revoked})
    return AccountStatusOut(status="password_reset")


@router.post("/auth/password/change", response_model=AccountStatusOut, summary="修改密码（撤销其他会话）")
def change_password(
    payload: PasswordChangeRequest,
    auth_session: AuthSession | None = Depends(get_auth_session),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> AccountStatusOut:
    try:
        revoked = accounts.change_password(
            db,
            settings,
            user=user,
            current_password=payload.current_password,
            new_password=payload.new_password,
            keep_session_id=auth_session.id if auth_session else None,
        )
    except accounts.AccountError as exc:
        db.rollback()
        raise _account_error(exc) from None
    db.commit()
    return AccountStatusOut(status="password_changed", sessions_revoked=revoked)


@router.get("/auth/sessions", response_model=AuthSessionListOut, summary="我的活动会话")
def list_sessions(
    auth_session: AuthSession | None = Depends(get_auth_session),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> AuthSessionListOut:
    rows = list(
        db.execute(
            select(AuthSession)
            .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
            .order_by(AuthSession.created_at.desc())
        ).scalars()
    )
    return AuthSessionListOut(
        sessions=[
            AuthSessionOut(
                id=row.id,
                created_at=row.created_at,
                expires_at=row.expires_at,
                last_seen_at=row.last_seen_at,
                current=auth_session is not None and row.id == auth_session.id,
                user_agent=row.user_agent,
            )
            for row in rows
        ]
    )


@router.delete("/auth/sessions/{session_id}", response_model=AccountStatusOut, summary="撤销指定会话")
def revoke_session(
    session_id: str,
    auth_session: AuthSession | None = Depends(get_auth_session),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> AccountStatusOut:
    row = db.get(AuthSession, session_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "session_not_found"})
    accounts.revoke_session(db, row, reason="user_revoked")
    db.commit()
    current = auth_session is not None and auth_session.id == session_id
    return AccountStatusOut(status="current_session_revoked" if current else "revoked")


@router.post("/auth/claim", response_model=ClaimResponse, summary="把当前访客工作区认领到账号（幂等）")
def claim(
    payload: ClaimRequest,
    session_row: DemoSession = Depends(get_current_session),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> ClaimResponse:
    project = load_owned_project(db, payload.project_id, session_row, user)
    try:
        outcome = accounts.claim_workspace(db, project=project, user=user, guest_session=session_row)
    except accounts.AccountError as exc:
        db.rollback()
        raise _account_error(exc) from None
    db.commit()
    return ClaimResponse(
        project_id=outcome.project_id,
        claimed=outcome.claimed,
        already_claimed=outcome.already_claimed,
    )


@router.get("/trips", response_model=TripListOut, summary="我的旅行列表（登录=账号全部；访客=当前访客工作区）")
def list_trips(
    session_row: DemoSession = Depends(get_current_session),
    user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db),
) -> TripListOut:
    if user is not None:
        projects = accounts.user_trips(db, user.id)
    else:
        projects = list(
            db.execute(
                select(GuideProject)
                .where(
                    GuideProject.owner_session_id == session_row.id,
                    GuideProject.owner_user_id.is_(None),
                    GuideProject.status != "deleted",
                )
                .order_by(GuideProject.updated_at.desc())
            ).scalars()
        )
    return TripListOut(trips=[project_to_out(item, media_count=_media_count(db, item.id)) for item in projects])


@router.delete("/auth/account", response_model=AccountStatusOut, summary="停用账号（撤销会话、释放工作区，可追溯）")
def delete_account(
    response: Response,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> AccountStatusOut:
    result = accounts.cleanup_account(db, user=user)
    db.commit()
    clear_auth_cookie(response, settings)
    logger.info("account_disabled", extra={"user_id": user.id, **result})
    return AccountStatusOut(status="account_disabled", sessions_revoked=result["sessions_revoked"])
