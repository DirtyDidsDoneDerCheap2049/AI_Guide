"""Provider 抽象与错误类型。

真实实现见 providers/http.py；自动回归使用 providers/fixtures.py（明确标注为 fixture）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class ProviderError(RuntimeError):
    """所有 Provider 错误的基类。code 会写入 ToolInvocation 和 RunStep。"""

    code = "provider_error"
    retryable = True

    def __init__(self, detail: str = "", *, response_status: int | None = None) -> None:
        self.detail = detail
        self.response_status = response_status
        super().__init__(self.code if not detail else f"{self.code}: {detail}")


class ProviderTimeout(ProviderError):
    code = "provider_timeout"
    retryable = True


class ProviderHTTPError(ProviderError):
    code = "provider_http_error"
    retryable = True


class ProviderRateLimited(ProviderError):
    code = "provider_rate_limited"
    retryable = True


class ProviderInvalidResponse(ProviderError):
    code = "provider_invalid_response"
    retryable = True


class ProviderNotConfigured(ProviderError):
    code = "provider_not_configured"
    retryable = False


class ProviderNoResult(ProviderError):
    """Provider 正常返回但没有匹配结果（例如地点库查不到）。"""

    code = "provider_no_result"
    retryable = False


@dataclass
class ModelResult:
    """一次模型调用的结果（原始文本 + 用量）。"""

    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    response_status: int | None = 200
    request_summary: dict[str, Any] = field(default_factory=dict)


@dataclass
class PlaceResult:
    """地点 Provider 的结果（供应商事实）。"""

    name: str
    address: str | None
    region: str | None
    latitude: float | None
    longitude: float | None
    provider: str
    provider_place_id: str | None
    raw: dict[str, Any] = field(default_factory=dict)
    response_status: int | None = 200
    request_summary: dict[str, Any] = field(default_factory=dict)


@dataclass
class PlaceSearchResult:
    """地点搜索的多候选结果。

    B4：地名搜索天然返回多条同名 POI，只取第一条会把用户确认过的地点换成
    另一个城市/另一个同名地点。因此 Provider 必须返回候选列表，由
    :mod:`app.services.place_match` 按名称/城市/POI ID 判定，歧义时交回用户。
    """

    candidates: list[PlaceResult] = field(default_factory=list)
    response_status: int | None = 200
    request_summary: dict[str, Any] = field(default_factory=dict)
    returned: int = 0


class VisionProvider(Protocol):
    name: str

    def analyze_image(
        self,
        *,
        image_bytes: bytes,
        mime_type: str,
        city_hint: str | None,
        messages: list[dict[str, Any]],
    ) -> ModelResult: ...


class TextProvider(Protocol):
    name: str

    def generate_guide_card(self, *, messages: list[dict[str, Any]]) -> ModelResult: ...

    def answer_question(self, *, messages: list[dict[str, Any]]) -> ModelResult: ...


class PlaceProvider(Protocol):
    name: str

    def search(
        self, *, name: str, city_hint: str | None = None, limit: int = 5
    ) -> PlaceSearchResult: ...

    def lookup(self, *, name: str, city_hint: str | None) -> PlaceResult: ...


class DirectionProvider(Protocol):
    """路径规划 Provider（D3-b）：距离、时长、几何必须来自供应商。"""

    name: str

    def route(
        self,
        *,
        mode: str,
        origin: tuple[float, float],
        destination: tuple[float, float],
        waypoints: list[tuple[float, float]] | None = None,
    ) -> Any: ...


@dataclass
class ProviderSet:
    vision: VisionProvider
    text: TextProvider
    place: PlaceProvider
    mode: str = "real"
    # D3-b：路线规划。旧调用方不传时保持 None，路线相关代码会明确报"未配置"
    direction: DirectionProvider | None = None
