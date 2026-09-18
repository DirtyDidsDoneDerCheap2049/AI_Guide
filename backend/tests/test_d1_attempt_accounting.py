"""D1 / B3：每一次真实的 Provider 请求都必须单独记账，失败与 JSON 修复不能漏。

07 号报告的复现：持续无效 JSON 时实际调用 6 次、180 token，而数据库只记 2 次、0 token。
原因是 JSON 修复失败丢失 calls、外层再重试，并按 `outcome.calls or 1` 记账；
成功修复时也把多次请求压成一条 ToolInvocation。
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.agent.orchestrator import execute_run
from app.agent.providers import build_provider_set
from app.agent.providers import fixtures
from app.config import Settings
from app.models import AgentRun, InvocationAttempt, RunStatus
from tests.conftest import create_project, make_image_bytes, unique_title

pytestmark = pytest.mark.integration


def _settings_with(settings: Settings, **overrides) -> Settings:
    base = {
        "app_env": "test",
        "database_url": settings.database_url,
        "redis_url": settings.redis_url,
        "session_secret": settings.session_secret,
        "provider_mode": "mock",
        "media_root": settings.media_root,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _make_run(client) -> tuple[str, str]:
    project = create_project(client, title=unique_title("b3"))
    response = client.post(
        f"/api/v1/projects/{project['id']}/media",
        files={"file": ("b3.png", make_image_bytes("PNG"), "image/png")},
        data={"start_analysis": "true"},
    )
    assert response.status_code == 201, response.text
    return project["id"], response.json()["run"]["id"]


def test_invalid_json_attempts_are_all_recorded(app, client, settings, session_factory):
    """修复循环 + 步骤重试产生的每一次调用都要有一行 InvocationAttempt。"""
    strict = _settings_with(
        settings, fixture_behaviors="vision=invalid_json", run_json_repair_attempts=2, run_step_max_attempts=2
    )
    providers = build_provider_set(strict)
    fixtures.CALL_COUNTER.clear()

    _, run_id = _make_run(client)
    status = execute_run(session_factory, run_id, providers=providers, settings=strict)
    assert status == RunStatus.FAILED

    actual_calls = sum(fixtures.CALL_COUNTER.values())
    assert actual_calls == 6, f"1 + 2 次修复，再乘 2 次步骤重试 = 6 次，实际 {actual_calls}"

    with app.state.session_factory() as db:
        attempts = db.execute(select(InvocationAttempt).where(InvocationAttempt.run_id == run_id)).scalars().all()
        assert len(attempts) == actual_calls, f"记账行数 {len(attempts)} 必须等于实际调用次数 {actual_calls}"
        known = [a for a in attempts if a.usage_known]
        assert len(known) == actual_calls, "fixture 在无效 JSON 时也会返回用量，应全部已知"
        tokens = db.execute(
            select(func.sum(InvocationAttempt.tokens_prompt + InvocationAttempt.tokens_completion)).where(
                InvocationAttempt.run_id == run_id
            )
        ).scalar()
        assert int(tokens) == 180, f"6 次 × (10+20) = 180 token，实际 {tokens}"
        run = db.get(AgentRun, run_id)
        assert run.used_tokens == 180, "运行的用量统计必须等于逐次记账之和"
        assert run.reserved_tokens == 0, "结算后不能残留预留"


def test_unknown_usage_is_not_written_as_zero(app, client, settings, session_factory):
    """Provider 直接报错（拿不到 usage）时，必须标记未知，而不是写 0 冒充免费。"""
    strict = _settings_with(settings, fixture_behaviors="vision=http_error", run_step_max_attempts=1)
    providers = build_provider_set(strict)
    fixtures.CALL_COUNTER.clear()

    _, run_id = _make_run(client)
    assert execute_run(session_factory, run_id, providers=providers, settings=strict) == RunStatus.FAILED

    with app.state.session_factory() as db:
        attempts = db.execute(select(InvocationAttempt).where(InvocationAttempt.run_id == run_id)).scalars().all()
        assert len(attempts) == sum(fixtures.CALL_COUNTER.values()) == 1
        attempt = attempts[0]
        assert attempt.status == "FAILED"
        assert attempt.usage_known is False
        assert attempt.tokens_prompt is None and attempt.tokens_completion is None
        assert attempt.cost is None, "未知费用不能写成 0"
        assert attempt.error_code == "provider_http_error"
        run = db.get(AgentRun, run_id)
        assert run.used_tool_calls == 1, "失败的调用同样占用调用次数上限"


def test_budget_reservation_blocks_extra_calls(app, client, settings, session_factory):
    """预留式预算：只剩 1 次调用额度时，不能偷偷多做一次 JSON 修复。"""
    strict = _settings_with(
        settings, fixture_behaviors="vision=invalid_json", run_json_repair_attempts=3, run_max_tool_calls=1
    )
    providers = build_provider_set(strict)
    fixtures.CALL_COUNTER.clear()

    _, run_id = _make_run(client)
    status = execute_run(session_factory, run_id, providers=providers, settings=strict)
    assert status == RunStatus.FAILED
    assert sum(fixtures.CALL_COUNTER.values()) == 1, "额度只允许一次调用，修复循环必须被预算拦住"

    with app.state.session_factory() as db:
        run = db.get(AgentRun, run_id)
        assert run.error_code == "budget_tool_calls_exceeded"
        attempts = db.execute(select(InvocationAttempt).where(InvocationAttempt.run_id == run_id)).scalars().all()
        # 真实调用 1 次 + 被预算拦下的修复尝试 1 次；两者都必须留痕，否则用量无法解释
        real = [item for item in attempts if item.status != "BUDGET_BLOCKED"]
        blocked = [item for item in attempts if item.status == "BUDGET_BLOCKED"]
        assert len(real) == 1 and real[0].usage_known is True
        assert len(blocked) == 1
        assert blocked[0].usage_known is False and blocked[0].cost is None
        assert blocked[0].reserved_tokens == 0  # 没放行的预留不能占额度
