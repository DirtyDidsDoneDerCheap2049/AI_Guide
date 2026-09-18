"""邮件发送（D2）：验证邮箱与重置密码。

- 生产用 SMTP（配置驱动）；本地/测试用 console 后端（写日志，不联网）。
- **发送失败不能让注册/重置事务失败**：令牌照常入库，调用方记录 email_sent=false。
- 邮件正文只包含一次性令牌与提示，不包含口令或任何会话令牌。
"""

from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

from app.config import Settings

logger = logging.getLogger("app.services.email")


@dataclass
class EmailMessagePayload:
    to: str
    subject: str
    body: str


class EmailSender(Protocol):
    name: str

    def send(self, message: EmailMessagePayload) -> None: ...


class ConsoleEmailSender:
    """本地开发：只写日志与可选文件，不联网。"""

    name = "console"

    def __init__(self, dump_path: str | None = None) -> None:
        self.dump_path = dump_path

    def send(self, message: EmailMessagePayload) -> None:
        logger.info(
            "email_console",
            extra={"to": message.to, "subject": message.subject, "body_len": len(message.body)},
        )
        if self.dump_path:
            try:
                with open(self.dump_path, "a", encoding="utf-8") as handle:
                    handle.write(f"=== {message.subject} -> {message.to}\n{message.body}\n")
            except OSError as exc:  # pragma: no cover - 本地辅助功能
                logger.warning("email_dump_failed", extra={"error_type": type(exc).__name__})


class SmtpEmailSender:
    name = "smtp"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def send(self, message: EmailMessagePayload) -> None:
        payload = EmailMessage()
        payload["From"] = self.settings.email_from
        payload["To"] = message.to
        payload["Subject"] = message.subject
        payload.set_content(message.body)
        if self.settings.email_smtp_starttls:
            with smtplib.SMTP(self.settings.email_smtp_host, self.settings.email_smtp_port, timeout=10) as client:
                client.starttls()
                if self.settings.email_smtp_user:
                    client.login(self.settings.email_smtp_user, self.settings.email_smtp_password)
                client.send_message(payload)
        else:
            with smtplib.SMTP(self.settings.email_smtp_host, self.settings.email_smtp_port, timeout=10) as client:
                if self.settings.email_smtp_user:
                    client.login(self.settings.email_smtp_user, self.settings.email_smtp_password)
                client.send_message(payload)


def build_sender(settings: Settings) -> EmailSender:
    if settings.email_backend == "smtp":
        return SmtpEmailSender(settings)
    return ConsoleEmailSender(dump_path=settings.email_dump_path or None)


def verification_message(settings: Settings, *, to: str, token: str) -> EmailMessagePayload:
    link = f"{settings.public_base_url.rstrip('/')}/verify-email?token={token}"
    return EmailMessagePayload(
        to=to,
        subject="AI-Guide 邮箱验证",
        body=(
            "欢迎使用 AI-Guide。\n\n"
            f"请在 {settings.auth_token_ttl_minutes} 分钟内打开下面的链接完成邮箱验证：\n{link}\n\n"
            f"如果按钮打不开，可以把这段令牌粘贴到页面里：{token}\n"
            "这不是你的密码，任何人拿到它都能验证这个邮箱，请不要转发。\n"
        ),
    )


def reset_message(settings: Settings, *, to: str, token: str) -> EmailMessagePayload:
    link = f"{settings.public_base_url.rstrip('/')}/reset-password?token={token}"
    return EmailMessagePayload(
        to=to,
        subject="AI-Guide 密码重置",
        body=(
            "我们收到了重置密码的请求。\n\n"
            f"请在 {settings.auth_token_ttl_minutes} 分钟内打开下面的链接设置新密码：\n{link}\n\n"
            f"如果按钮打不开，可以把这段令牌粘贴到页面里：{token}\n"
            "如果这不是你发起的，可以忽略这封邮件——你的密码没有被改动。\n"
        ),
    )


def send_or_log(settings: Settings, message: EmailMessagePayload, sender: EmailSender | None = None) -> bool:
    """发送邮件；返回是否真的送出去了。失败只记日志，不抛异常。"""
    active = sender or build_sender(settings)
    try:
        active.send(message)
        return True
    except Exception as exc:  # noqa: BLE001 - 邮件失败不应影响账号事务
        logger.warning(
            "email_send_failed",
            extra={"backend": active.name, "to_domain": message.to.split("@")[-1], "error_type": type(exc).__name__},
        )
        return False
