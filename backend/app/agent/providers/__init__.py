"""Provider 装配：按配置返回真实或 fixture Provider。"""

from __future__ import annotations

from app.agent.providers.base import ProviderSet
from app.agent.providers.direction import AMapDirectionProvider
from app.agent.providers.fixtures import (
    FixtureDirectionProvider,
    FixturePlaceProvider,
    FixtureTextProvider,
    FixtureVisionProvider,
    parse_behaviors,
)
from app.agent.providers.http import (
    AMapPlaceProvider,
    OpenAICompatibleTextProvider,
    OpenAICompatibleVisionProvider,
)
from app.config import ConfigError, Settings


def build_provider_set(settings: Settings) -> ProviderSet:
    if settings.provider_mode == "mock":
        if settings.is_production:
            # 生产环境绝不允许 fixture 冒充真实调用。
            raise ConfigError(["PROVIDER_MODE"], "invalid_configuration_for_production")
        behaviors = parse_behaviors(settings.fixture_behaviors)
        return ProviderSet(
            vision=FixtureVisionProvider(behaviors.get("vision", "ok")),
            text=FixtureTextProvider(behaviors.get("text", "ok")),
            place=FixturePlaceProvider(behaviors.get("place", "ok")),
            direction=FixtureDirectionProvider(behaviors.get("route", "ok")),
            mode="mock",
        )

    missing = settings.missing_provider_config()
    if missing:
        raise ConfigError(missing, "missing_configuration")
    return ProviderSet(
        vision=OpenAICompatibleVisionProvider(settings),
        text=OpenAICompatibleTextProvider(settings),
        place=AMapPlaceProvider(settings),
        direction=AMapDirectionProvider(settings),
        mode="real",
    )
