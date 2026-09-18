"""模型输出的 JSON 提取与 Pydantic 校验（含有限次修复）。"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app.agent.attempts import BudgetBlocked
from app.agent.providers.base import ModelResult, ProviderInvalidResponse

T = TypeVar("T", bound=BaseModel)

_FENCE_START = re.compile(r"^```[a-zA-Z0-9_-]*\s*")
_FENCE_END = re.compile(r"```\s*$")


def extract_json(text: str) -> Any:
    """从模型输出中提取 JSON 对象；失败抛 ValueError。"""
    if not text or not text.strip():
        raise ValueError("empty_model_output")
    cleaned = _FENCE_START.sub("", text.strip())
    cleaned = _FENCE_END.sub("", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(cleaned)):
            char = cleaned[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidate = cleaned[start : index + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break
        start = cleaned.find("{", start + 1)
    raise ValueError("no_json_object_found")


def _error_summary(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        parts = []
        for error in exc.errors()[:5]:
            loc = ".".join(str(item) for item in error.get("loc", ()))
            parts.append(f"{loc}: {error.get('type')}")
        return "; ".join(parts)
    return type(exc).__name__


def complete_validated(
    call: Callable[[list[dict[str, Any]]], ModelResult],
    messages: list[dict[str, Any]],
    schema: type[T],
    *,
    max_attempts: int,
    recorder: Any | None = None,
    provider: str = "",
    model: str | None = None,
    operation: str = "",
) -> tuple[T, list[ModelResult]]:
    """调用 Provider 并校验结构化输出；失败时带错误摘要做有限次修复。

    返回 (校验通过的对象, 所有调用结果)。全部失败时抛 ProviderInvalidResponse。
    """
    conversation = [dict(message) for message in messages]
    calls: list[ModelResult] = []
    last_error: str = "unknown"

    for attempt in range(max(1, max_attempts)):
        # B3：每次真实请求都先原子预留预算并落 RESERVED 行，调用后再结算。
        handle = None
        if recorder is not None:
            handle = recorder.start(provider=provider, model=model, operation=operation)
            if handle is None:
                raise BudgetBlocked("budget_exhausted_before_call")
        started = time.monotonic()
        try:
            result = call(conversation)
        except Exception as exc:
            if recorder is not None:
                recorder.finish(
                    handle,
                    status="FAILED",
                    duration_ms=int((time.monotonic() - started) * 1000),
                    error_code=getattr(exc, "code", "unexpected_error"),
                )
            raise
        if recorder is not None:
            recorder.finish(
                handle,
                status="SUCCEEDED",
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                response_status=result.response_status,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        calls.append(result)
        try:
            payload = extract_json(result.text)
            return schema.model_validate(payload), calls
        except (ValueError, ValidationError) as exc:
            last_error = _error_summary(exc)
            if attempt + 1 >= max(1, max_attempts):
                break
            conversation = conversation + [
                {"role": "assistant", "content": result.text[:2000]},
                {
                    "role": "user",
                    "content": (
                        "上一条输出不是合法且符合要求的 JSON（错误：" + last_error + "）。"
                        "请只输出一个符合约定的 JSON 对象，不要包含解释或代码块。"
                    ),
                },
            ]

    raise ProviderInvalidResponse(f"schema_validation_failed:{last_error}")
