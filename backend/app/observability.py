"""结构化日志：只记录关联 ID、状态、耗时和错误类型，不记录请求头、Cookie 或密钥。"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

_RESERVED = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "module",
    "msecs",
    "message",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "thread",
    "threadName",
    "taskName",
}

SENSITIVE_KEYS = {
    "authorization",
    "cookie",
    "set-cookie",
    "api_key",
    "apikey",
    "secret",
    "password",
    "session_secret",
    "token",
    "access_token",
    "refresh_token",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            if key.lower() in SENSITIVE_KEYS:
                payload[key] = "***redacted***"
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                payload[key] = value
        if record.exc_info:
            payload["error_type"] = getattr(record.exc_info[0], "__name__", "Exception")
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    # 访问日志会被 uvicorn 默认打印，这里压低噪声并交给结构化日志记录关键操作。
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    # 关键：httpx 默认在 INFO 级打印完整请求 URL，而高德的地点接口把 key 放在 query string 里，
    # 一旦记录就等于把密钥写进日志。这里统一压到 WARNING，杜绝密钥落盘。
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
