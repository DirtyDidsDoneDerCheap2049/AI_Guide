"""AI-Guide 后端包。

v0.1 内核：FastAPI + MySQL + Redis(Dramatiq) + SSE + 受控 Agent 状态机。
本包与旧的 Flask 演示后端完全解耦，不导入其中任何模块。
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
