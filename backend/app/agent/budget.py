"""运行预算：最大步骤数、Provider 调用次数、token 与费用上限。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.config import Settings
from app.models import AgentRun


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    code: str | None = None
    detail: str | None = None


def check_budget(run: AgentRun, settings: Settings, *, next_tool_calls: int = 1) -> BudgetDecision:
    """在一次 Provider 调用前检查预算。"""
    if run.used_steps >= min(run.budget_max_steps, settings.run_max_steps):
        return BudgetDecision(False, "budget_steps_exceeded", f"max={run.budget_max_steps}")
    if run.used_tool_calls + next_tool_calls > min(run.budget_max_tool_calls, settings.run_max_tool_calls):
        return BudgetDecision(False, "budget_tool_calls_exceeded", f"max={run.budget_max_tool_calls}")
    if run.used_tokens >= min(run.budget_max_tokens, settings.run_max_tokens):
        return BudgetDecision(False, "budget_tokens_exceeded", f"max={run.budget_max_tokens}")
    if run.used_cost >= run.budget_max_cost:
        return BudgetDecision(False, "budget_cost_exceeded", f"max={run.budget_max_cost}")
    return BudgetDecision(True)


def estimate_cost(settings: Settings, prompt_tokens: int, completion_tokens: int) -> float:
    return round(
        (prompt_tokens / 1000.0) * settings.model_prompt_price_per_1k
        + (completion_tokens / 1000.0) * settings.model_completion_price_per_1k,
        6,
    )


def charge_usage(
    run: AgentRun,
    settings: Settings,
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    tool_calls: int = 1,
    place_calls: int = 0,
) -> float:
    """累计本次调用的用量，返回本次费用。"""
    cost = estimate_cost(settings, prompt_tokens, completion_tokens) + place_calls * settings.place_call_price
    run.used_tool_calls += tool_calls
    run.used_tokens += prompt_tokens + completion_tokens
    run.used_cost = (run.used_cost or Decimal("0")) + Decimal(str(cost))
    return cost
