"""FastAPI 依赖：配置、数据库会话、匿名访客会话、登录会话、所有权校验。

D2 起有两条身份路径：
- 访客：签名 Cookie 里的 DemoSession（可读写自己创建的工作区）；
- 账号：登录 Cookie 里的 AuthSession，可读写 owner_user_id 指向自己的工作区。
所有权判定统一走 :func:`load_owned_project` / :func:`load_owned_media`，
不能只给列表接口加过滤。
"""

from __future__ import annotations

import hashlib
import ipaddress
from collections.abc import Iterator

from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    AuthSession,
    DemoSession,
    GuideProject,
    MediaAsset,
    User,
    utcnow,
)
from app.security import SessionSigner, set_session_cookie
from app.services.accounts import load_session

__all__ = [
    "client_ip",
    "get_client_hash",
    "get_current_session",
    "get_current_user",
    "get_db",
    "get_existing_session",
    "get_optional_user",
    "get_settings_dep",
    "load_owned_media",
    "load_owned_project",
]


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Iterator[Session]:
    factory = request.app.state.session_factory
    session = factory()
    try:
        yield session
    finally:
        session.close()


def client_ip(request: Request, settings: Settings) -> str:
    """解析客户端地址（B6）。

    只有**直连对端**属于可信反向代理时，才采用 X-Forwarded-For 的第一段；
    否则一律使用 TCP 对端地址。这样公网访客无法通过伪造 XFF 绕过入口限流。
    """
    peer = request.client.host if request.client else "unknown"
    if peer in settings.trusted_proxy_set:
        forwarded = request.headers.get("x-forwarded-for", "")
        candidate = forwarded.split(",")[0].strip()
        if candidate:
            try:
                ipaddress.ip_address(candidate)
            except ValueError:
                return peer
            return candidate
    return peer


def client_fingerprint(request: Request, settings: Settings) -> str:
    """不可逆的客户端指纹（限流用），不保存 IP 原文。"""
    raw = f"{settings.session_secret}|{client_ip(request, settings)}".encode()
    return hashlib.sha256(raw).hexdigest()[:32]


def get_client_hash(
    request: Request,
    settings: Settings = Depends(get_settings_dep),
) -> str:
    """入口维度额度键（不可逆）。"""
    return client_fingerprint(request, settings)


def get_current_session(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> DemoSession:
    """读取签名 Cookie 中的匿名会话；不存在时创建并下发新 Cookie。"""
    signer = SessionSigner(settings.session_secret, settings.session_ttl_seconds)
    session_id = signer.unsign(request.cookies.get(settings.session_cookie_name))
    row: DemoSession | None = db.get(DemoSession, session_id) if session_id else None
    if row is not None and row.revoked:
        row = None

    if row is None:
        row = DemoSession(client_hash=client_fingerprint(request, settings))
        db.add(row)
        db.commit()
        db.refresh(row)
        set_session_cookie(response, settings, row.id)
    else:
        row.last_seen_at = utcnow()
        db.commit()
    return row


def get_existing_session(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> DemoSession:
    """只读取已存在的匿名会话（不创建、不下发 Cookie），用于 SSE 长连接。"""
    signer = SessionSigner(settings.session_secret, settings.session_ttl_seconds)
    session_id = signer.unsign(request.cookies.get(settings.session_cookie_name))
    row: DemoSession | None = db.get(DemoSession, session_id) if session_id else None
    if row is None or row.revoked:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="session_required")
    return row




def get_optional_guest_session(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> DemoSession | None:
    """只读取已存在的访客会话（不创建、不下发 Cookie）。没有就返回 None。

    用于"可能已登录"的端点：登录用户没有访客 Cookie 也应当能访问自己的数据。
    """
    signer = SessionSigner(settings.session_secret, settings.session_ttl_seconds)
    session_id = signer.unsign(request.cookies.get(settings.session_cookie_name))
    row: DemoSession | None = db.get(DemoSession, session_id) if session_id else None
    if row is None or row.revoked:
        return None
    return row


def get_optional_user(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> User | None:
    """读取登录用户（未登录返回 None）。会话被撤销/过期即视为未登录。"""
    raw = request.cookies.get(settings.auth_cookie_name)
    session = load_session(db, settings, raw)
    if session is None:
        return None
    user = db.get(User, session.user_id)
    if user is None or user.status != "active":
        return None
    request.state.auth_session = session
    return user


def get_auth_session(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> AuthSession | None:
    raw = request.cookies.get(settings.auth_cookie_name)
    return load_session(db, settings, raw)


def require_user(
    user: User | None = Depends(get_optional_user),
) -> User:
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": "auth_required"})
    return user


def project_access_allows(project: GuideProject | None, *, user: User | None, session_row: DemoSession | None) -> bool:
    """工作区访问规则：登录用户看自己的；访客只能看自己会话创建且尚未被认领的。"""
    if project is None:
        return False
    if user is not None and project.owner_user_id == user.id:
        return True
    if project.owner_user_id is not None:
        # 已归属某个账号：访客 Cookie 不能再读（认领后原访客也失去访问权）
        return False
    return session_row is not None and project.owner_session_id == session_row.id


def load_owned_project(
    db: Session,
    project_id: str,
    session_row: DemoSession | None,
    user: User | None = None,
) -> GuideProject:
    """所有权校验：不属于当前身份的项目一律 404，不泄露是否存在。"""
    project = db.get(GuideProject, project_id)
    if not project_access_allows(project, user=user, session_row=session_row):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project_not_found")
    return project


def load_owned_media(
    db: Session, media_id: str, session_row: DemoSession | None, user: User | None = None
) -> MediaAsset:
    media = db.get(MediaAsset, media_id)
    if media is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="media_not_found")
    project = db.get(GuideProject, media.project_id)
    if not project_access_allows(project, user=user, session_row=session_row):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="media_not_found")
    return media
