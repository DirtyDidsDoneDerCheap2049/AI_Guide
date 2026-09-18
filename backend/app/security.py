"""匿名会话 Cookie 的签名与校验。

- Cookie 使用 itsdangerous 签名，HttpOnly，SameSite=Lax，生产环境 Secure。
- 只保存随机的会话 ID；不含任何用户身份信息，也不保存任何 Provider 密钥。
"""

from __future__ import annotations

from fastapi import Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import Settings

_SALT = "ai-guide.anonymous-session.v1"


class SessionSigner:
    def __init__(self, secret: str, ttl_seconds: int) -> None:
        self._serializer = URLSafeTimedSerializer(secret_key=secret, salt=_SALT)
        self._ttl = ttl_seconds

    def sign(self, session_id: str) -> str:
        return self._serializer.dumps({"sid": session_id})

    def unsign(self, token: str | None) -> str | None:
        if not token:
            return None
        try:
            payload = self._serializer.loads(token, max_age=self._ttl)
        except (BadSignature, SignatureExpired):
            return None
        if not isinstance(payload, dict):
            return None
        session_id = payload.get("sid")
        return session_id if isinstance(session_id, str) and session_id else None


def set_session_cookie(response, settings: Settings, session_id: str) -> None:
    response.set_cookie(
        key=settings.session_cookie_name,
        value=SessionSigner(settings.session_secret, settings.session_ttl_seconds).sign(session_id),
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.session_cookie_samesite,
        path="/",
    )


def clear_session_cookie(response, settings: Settings) -> None:
    response.delete_cookie(key=settings.session_cookie_name, path="/")

def set_auth_cookie(response: Response, settings: Settings, token: str) -> None:
    """下发登录 Cookie（D2）。

    HttpOnly 恒开；SameSite=Lax；Secure 由 auth_cookie_secure_effective 决定
    （生产被强制为 true，见 config.auth_cookie_secure_effective）。
    """
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=token,
        max_age=settings.auth_session_days * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.auth_cookie_secure_effective,
        path="/",
    )


def clear_auth_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        key=settings.auth_cookie_name,
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.auth_cookie_secure_effective,
    )
