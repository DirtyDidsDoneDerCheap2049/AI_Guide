"""ASGI 入口：uvicorn app.asgi:app 使用。导入期构造应用并读取配置。"""

from __future__ import annotations

from app.main import create_app

app = create_app()
