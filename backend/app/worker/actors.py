"""Dramatiq Worker：独立进程执行 Agent 步骤。

启动方式：
- 生产：``dramatiq app.worker.actors``（Compose 中通过 ``python -m app.worker.cli worker`` 启动）
- 本机：``python -m app.worker.cli worker --processes 1 --threads 4``
"""

from __future__ import annotations

import logging

import dramatiq
from dramatiq.brokers.redis import RedisBroker

from app.agent.orchestrator import execute_run
from app.agent.providers import build_provider_set
from app.config import get_settings
from app.db import session_factory_for

logger = logging.getLogger("app.worker")

# dramatiq CLI 会在导入本模块之前配置 logging，因此这里再压一次：
# httpx 默认在 INFO 级打印完整请求 URL，任何带凭据的 URL 都会因此落盘。
for _noisy in ("httpx", "httpcore", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

settings = get_settings()
broker = RedisBroker(url=settings.redis_url)
dramatiq.set_broker(broker)


@dramatiq.actor(queue_name=settings.redis_queue_name, max_retries=0, time_limit=900_000)
def advance_run(run_id: str) -> None:
    """推进一次 AgentRun。重复投递由编排器的条件认领和步骤状态保证幂等。"""
    providers = build_provider_set(settings)
    factory = session_factory_for(settings)
    status = execute_run(factory, run_id, providers=providers, settings=settings)
    logger.info("run_advanced", extra={"run_id": run_id, "status": status, "provider_mode": providers.mode})
