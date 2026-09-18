"""B3：每一次真实的 Provider 请求都单独记账，并在调用前原子预留预算。

为什么需要独立模块：
- 07 号报告的缺陷是"修复循环失败时丢失 calls、外层再按 outcome.calls or 1 记账"，
  导致 6 次真实调用只落 2 行、180 token 记成 0。
- 因此记账不能依赖工具返回的聚合结果，必须在**每次调用前后**各写一次数据库：
  调用前 ``start()``（原子预留 + RESERVED 行），调用后 ``finish()``（结算 + 结果行）。
- 用量拿不到时写 NULL 并标记 ``usage_known=False``，绝不写 0 冒充免费。
- 预留是防超额的近似手段；token 与调用次数是硬约束，费用只作估算上限（等真实账单验证）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import text, update
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db import session_scope
from app.models import InvocationAttempt, UsageLedger, utcnow

logger = logging.getLogger("app.agent.attempts")


class BudgetBlocked(RuntimeError):
    """预算预留失败：调用方不得再发起 Provider 请求。"""

    code = "budget_tool_calls_exceeded"

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


@dataclass
class AttemptHandle:
    attempt_id: int
    reserved_tokens: int
    provider: str
    model: str | None
    operation: str


@dataclass
class AttemptTotals:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: Decimal = Decimal("0")
    unknown_usage_calls: int = 0
    blocked_calls: int = 0
    statuses: list[str] = field(default_factory=list)

    @property
    def usage_known(self) -> bool:
        return self.unknown_usage_calls == 0


class AttemptRecorder:
    """绑定到一次工具执行的记账器；每次 Provider 调用都用它包一层。"""

    def __init__(
        self,
        factory: sessionmaker[Session],
        settings: Settings,
        *,
        run_id: str,
        step_id: str | None,
        session_id: str | None,
        project_id: str | None = None,
    ) -> None:
        self._factory = factory
        self._settings = settings
        self._run_id = run_id
        self._step_id = step_id
        self._session_id = session_id
        self._project_id = project_id
        self._attempt_no = 0
        self.totals = AttemptTotals()

    # ------------------------------------------------------------------ 内部
    def _price(self, provider: str) -> tuple[float, float]:
        """按 Provider 角色取价：视觉与文本是不同模型，价格不能混用。"""
        name = provider.lower()
        if "vision" in name or "vl" in name:
            return self._settings.vision_prompt_price_per_1k, self._settings.vision_completion_price_per_1k
        if "text" in name:
            return self._settings.text_prompt_price_per_1k, self._settings.text_completion_price_per_1k
        return self._settings.model_prompt_price_per_1k, self._settings.model_completion_price_per_1k

    def estimate_cost(
        self, provider: str, prompt_tokens: int | None, completion_tokens: int | None
    ) -> Decimal | None:
        if prompt_tokens is None or completion_tokens is None:
            return None
        prompt_price, completion_price = self._price(provider)
        value = (prompt_tokens / 1000.0) * prompt_price + (completion_tokens / 1000.0) * completion_price
        return Decimal(str(round(value, 6)))

    # ------------------------------------------------------------------ API
    def start(
        self,
        *,
        provider: str,
        model: str | None,
        operation: str,
        request_summary: dict | None = None,
    ) -> AttemptHandle | None:
        """原子预留预算并写入 RESERVED 行；预算不足时写入 BUDGET_BLOCKED 行并返回 None。"""
        reserve = 0 if operation == "lookup_place" else int(self._settings.run_reserve_tokens_per_call)
        self._attempt_no += 1
        attempt_no = self._attempt_no

        with session_scope(self._factory) as db:
            result = db.execute(
                text(
                    """
                    UPDATE agent_runs
                    SET used_tool_calls = used_tool_calls + 1,
                        reserved_tokens = reserved_tokens + :reserve,
                        updated_at = UTC_TIMESTAMP()
                    WHERE id = :run_id
                      AND status IN ('QUEUED', 'RUNNING', 'WAITING_USER')
                      AND used_steps <= LEAST(budget_max_steps, :max_steps)
                      AND used_tool_calls + 1 <= LEAST(budget_max_tool_calls, :max_calls)
                      AND used_tokens + reserved_tokens + :reserve <= LEAST(budget_max_tokens, :max_tokens)
                      AND used_cost < LEAST(budget_max_cost, :max_cost)
                    """
                ),
                {
                    "run_id": self._run_id,
                    "reserve": reserve,
                    # run 上的预算快照与当前配置取更严的一侧（配置下调立即生效）
                    "max_steps": self._settings.run_max_steps,
                    "max_calls": self._settings.run_max_tool_calls,
                    "max_tokens": self._settings.run_max_tokens,
                    "max_cost": self._settings.run_max_cost,
                },
            )
            granted = bool(result.rowcount)

            attempt = InvocationAttempt(
                run_id=self._run_id,
                step_id=self._step_id,
                attempt_no=attempt_no,
                provider=provider,
                model=model,
                operation=operation,
                status="RESERVED" if granted else "BUDGET_BLOCKED",
                reserved_tokens=reserve if granted else 0,
                currency=self._settings.cost_currency,
                price_version=self._settings.price_version,
                started_at=utcnow(),
            )
            db.add(attempt)
            db.flush()
            attempt_id = int(attempt.id)

        self.totals.calls += 1
        self.totals.statuses.append("RESERVED" if granted else "BUDGET_BLOCKED")
        if not granted:
            self.totals.blocked_calls += 1
            logger.warning("budget_reservation_blocked", extra={"run_id": self._run_id, "provider": provider})
            return None
        return AttemptHandle(
            attempt_id=attempt_id,
            reserved_tokens=reserve,
            provider=provider,
            model=model,
            operation=operation,
        )

    def finish(
        self,
        handle: AttemptHandle | None,
        *,
        status: str,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        response_status: int | None = None,
        duration_ms: int = 0,
        error_code: str | None = None,
    ) -> Decimal | None:
        """结算一次调用：写用量、释放预留、累计费用（未知用量写 NULL）。"""
        if handle is None:
            return None
        usage_known = prompt_tokens is not None and completion_tokens is not None
        cost = self.estimate_cost(handle.provider, prompt_tokens, completion_tokens) if usage_known else None
        if handle.operation == "lookup_place" and usage_known:
            cost = (cost or Decimal("0")) + Decimal(str(self._settings.place_call_price))

        with session_scope(self._factory) as db:
            db.execute(
                update(InvocationAttempt)
                .where(InvocationAttempt.id == handle.attempt_id)
                .values(
                    status=status,
                    tokens_prompt=prompt_tokens,
                    tokens_completion=completion_tokens,
                    usage_known=usage_known,
                    cost=cost,
                    response_status=response_status,
                    duration_ms=duration_ms,
                    error_code=error_code,
                    finished_at=utcnow(),
                )
            )
            db.execute(
                text(
                    """
                    UPDATE agent_runs
                    SET reserved_tokens = GREATEST(reserved_tokens - :reserve, 0),
                        used_tokens = used_tokens + :tokens,
                        used_cost = used_cost + :cost,
                        updated_at = UTC_TIMESTAMP()
                    WHERE id = :run_id
                    """
                ),
                {
                    "reserve": handle.reserved_tokens,
                    "tokens": ((prompt_tokens or 0) + (completion_tokens or 0)) if usage_known else 0,
                    "cost": float(cost or 0),
                    "run_id": self._run_id,
                },
            )
            if self._session_id is not None:
                db.add(
                    UsageLedger(
                        session_id=self._session_id,
                        project_id=self._project_id,
                        run_id=self._run_id,
                        kind="place" if handle.operation == "lookup_place" else "model",
                        units=1,
                        tokens=((prompt_tokens or 0) + (completion_tokens or 0)) if usage_known else 0,
                        cost=cost,
                        currency=self._settings.cost_currency,
                        price_version=self._settings.price_version,
                        day=utcnow().date(),
                    )
                )

        if usage_known:
            self.totals.prompt_tokens += int(prompt_tokens or 0)
            self.totals.completion_tokens += int(completion_tokens or 0)
            self.totals.cost = (self.totals.cost or Decimal("0")) + (cost or Decimal("0"))
        else:
            self.totals.unknown_usage_calls += 1
        self.totals.statuses.append(status)
        return cost
