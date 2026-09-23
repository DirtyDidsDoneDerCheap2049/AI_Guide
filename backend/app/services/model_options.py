"""Server-owned model allowlist. Persist choices without persisting credentials."""
from __future__ import annotations

import json
import os
from urllib.parse import urlparse
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import ConfigError, Settings


class ModelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    label: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=160)
    base_url: str = ""
    api_key_env: str = Field(default="TEXT_API_KEY", pattern=r"^[A-Z][A-Z0-9_]*$")
    thinking_mode: Literal["none", "qwen", "deepseek", "reasoning_effort"] = "none"
    json_mode: bool = True
    prompt_price: float = Field(ge=0)
    completion_price: float = Field(ge=0)


class ModelSelectionError(ValueError):
    pass


def thinking_levels(mode: str) -> list[str]:
    if mode == "none":
        return ["off"]
    if mode == "deepseek":
        return ["off", "high"]
    return ["off", "low", "medium", "high"]


def thinking_mode(mode: str, base_url: str) -> str:
    if mode != "auto":
        return mode
    host = urlparse(base_url).hostname or ""
    if host == "dashscope.aliyuncs.com" or host.endswith(".dashscope.aliyuncs.com"):
        return "qwen"
    return "none"


def profiles(settings: Settings) -> list[ModelProfile]:
    try:
        raw = json.loads(settings.text_model_profiles)
        if not isinstance(raw, list) or len(raw) > 20:
            raise ValueError()
        items = [ModelProfile.model_validate(item) for item in raw]
        if not items:
            items = [ModelProfile(
                id="default", label=settings.text_model, model=settings.text_model,
                thinking_mode=thinking_mode(settings.text_thinking_mode, settings.text_api_base_url),
                prompt_price=settings.text_prompt_price_per_1k,
                completion_price=settings.text_completion_price_per_1k,
            )]
        if len({p.id for p in items}) != len(items):
            raise ValueError()
        for p in items:
            url = urlparse(p.base_url or settings.text_api_base_url)
            if url.scheme not in ("https", "http") or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError()
            if settings.is_production and url.scheme != "https":
                raise ValueError()
        return items
    except (ValueError, TypeError, ValidationError):
        raise ConfigError(["TEXT_MODEL_PROFILES"], "invalid_model_profiles") from None


def select_model(settings: Settings, model_id: str | None, thinking: bool | None, level: str | None = None) -> dict:
    items = profiles(settings)
    profile = next((p for p in items if p.id == (model_id or items[0].id)), None)
    if profile is None:
        raise ModelSelectionError("model_unavailable")
    enabled = settings.text_enable_thinking if thinking is None else thinking
    if level is not None:
        if thinking is not None and thinking != (level != "off"):
            raise ModelSelectionError("thinking_selection_conflict")
        enabled = level != "off"
    else:
        level = ("high" if profile.thinking_mode in ("deepseek", "reasoning_effort") else "medium") if enabled else "off"
    if enabled and profile.thinking_mode == "none":
        raise ModelSelectionError("thinking_not_supported")
    if level not in thinking_levels(profile.thinking_mode):
        raise ModelSelectionError("thinking_level_not_supported")
    result = profile.model_dump()
    result["base_url"] = profile.base_url or settings.text_api_base_url
    result["thinking"] = enabled
    result["thinking_level"] = level
    result["thinking_budget"] = {"off": 0, "low": max(1, settings.thinking_budget // 4),
                                  "medium": settings.thinking_budget, "high": settings.thinking_budget * 2}[level]
    return result


def settings_for_selection(settings: Settings, selection: dict) -> Settings:
    # Reconstruct a task-local copy: concurrent workers never mutate shared Settings.
    profile = ModelProfile.model_validate({k: v for k, v in selection.items() if k not in ("thinking", "thinking_level", "thinking_budget")})
    key = settings.text_api_key if profile.api_key_env == "TEXT_API_KEY" else os.environ.get(profile.api_key_env, "")
    return settings.model_copy(update={
        "text_model": profile.model, "text_api_base_url": profile.base_url,
        "text_api_key": key, "text_enable_thinking": bool(selection["thinking"]),
        "text_thinking_mode": profile.thinking_mode,
        "thinking_budget": selection.get("thinking_budget") or settings.thinking_budget,
        "text_prompt_price_per_1k": profile.prompt_price,
        "text_completion_price_per_1k": profile.completion_price,
    })
