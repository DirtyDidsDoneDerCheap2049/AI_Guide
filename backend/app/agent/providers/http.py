"""真实 Provider：OpenAI 兼容多模态/文本接口 + 高德地点服务。

这些类只在独立 Dramatiq Worker 中实例化使用；API 进程从不同步调用模型或地图服务。
Provider 抛出的错误统一是 :mod:`app.agent.providers.base` 中的 ProviderError 子类。
"""

from __future__ import annotations

import base64
import time
from typing import Any

import httpx

from app.agent.providers.base import (
    ModelResult,
    PlaceResult,
    PlaceSearchResult,
    ProviderHTTPError,
    ProviderInvalidResponse,
    ProviderNoResult,
    ProviderNotConfigured,
    ProviderRateLimited,
    ProviderTimeout,
)
from app.config import Settings


class _OpenAICompatibleBase:
    def __init__(self, settings: Settings, *, base_url: str, api_key: str, model: str, name: str) -> None:
        self.settings = settings
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.name = name

    def _post_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.2,
        enable_thinking: bool = False,
        extra_body: dict[str, Any] | None = None,
    ) -> ModelResult:
        if not self.api_key:
            raise ProviderNotConfigured(self.name)
        url = f"{self.base_url}/chat/completions"
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
            # 百炼的混合思考模型（qwen3.5~3.8、qwen3-vl-*）默认开启思考；
            # 结构化抽取不需要思考，显式关闭以降低延迟和输出 token。
            "enable_thinking": enable_thinking,
        }
        if enable_thinking:
            payload["thinking_budget"] = self.settings.thinking_budget
        if extra_body:
            payload.update(extra_body)
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        started = time.monotonic()
        try:
            with httpx.Client(timeout=self.settings.provider_timeout_seconds) as client:
                response = client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException:
            raise ProviderTimeout(f"{self.name}_timeout") from None
        except httpx.HTTPError as exc:
            raise ProviderHTTPError(type(exc).__name__) from None

        duration_ms = int((time.monotonic() - started) * 1000)
        if response.status_code == 429:
            raise ProviderRateLimited(self.name, response_status=429)
        if response.status_code >= 400:
            raise ProviderHTTPError(f"{self.name}_http_{response.status_code}", response_status=response.status_code)
        try:
            body = response.json()
        except ValueError:
            raise ProviderInvalidResponse("non_json_body", response_status=response.status_code) from None

        try:
            text = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise ProviderInvalidResponse("missing_choices", response_status=response.status_code) from None
        usage = body.get("usage") or {}
        return ModelResult(
            text=text if isinstance(text, str) else str(text),
            model=str(body.get("model") or self.model),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            response_status=response.status_code,
            request_summary={"provider": self.name, "model": self.model, "messages": len(messages), "duration_ms": duration_ms},
        )


class OpenAICompatibleVisionProvider(_OpenAICompatibleBase):
    """多模态模型：输入受限图片（base64 data URL），输出结构化地点候选。"""

    def __init__(self, settings: Settings) -> None:
        super().__init__(
            settings,
            base_url=settings.vision_api_base_url,
            api_key=settings.vision_api_key,
            model=settings.vision_model,
            name="vision",
        )

    def analyze_image(
        self,
        *,
        image_bytes: bytes,
        mime_type: str,
        city_hint: str | None,
        messages: list[dict[str, Any]],
    ) -> ModelResult:
        data_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
        prepared: list[dict[str, Any]] = []
        attached = False
        for message in reversed(messages):
            if not attached and message.get("role") == "user":
                content = message.get("content")
                if isinstance(content, str):
                    message = dict(message)
                    message["content"] = [
                        {"type": "text", "text": content},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ]
                elif isinstance(content, list):
                    message = dict(message)
                    message["content"] = list(content) + [{"type": "image_url", "image_url": {"url": data_url}}]
                prepared.append(message)
                attached = True
                continue
            prepared.append(message)
        prepared.reverse()

        # 默认压缩策略可能丢失招牌/小字细节；需要时可打开高分辨率或放宽 max_pixels。
        vision_options: dict[str, Any] = {}
        if self.settings.vision_high_resolution_images:
            vision_options["vl_high_resolution_images"] = True
        elif self.settings.vision_max_pixels > 0:
            vision_options["max_pixels"] = self.settings.vision_max_pixels

        return self._post_chat(
            prepared,
            temperature=0.1,
            enable_thinking=self.settings.vision_enable_thinking,
            extra_body=vision_options,
        )


class OpenAICompatibleTextProvider(_OpenAICompatibleBase):
    """文本模型：只使用已确认地点与允许的上下文生成导游卡片。"""

    def __init__(self, settings: Settings) -> None:
        super().__init__(
            settings,
            base_url=settings.text_api_base_url,
            api_key=settings.text_api_key,
            model=settings.text_model,
            name="text",
        )

    def generate_guide_card(self, *, messages: list[dict[str, Any]]) -> ModelResult:
        return self._post_chat(
            messages,
            temperature=0.4,
            enable_thinking=self.settings.text_enable_thinking,
        )

    def answer_question(self, *, messages: list[dict[str, Any]]) -> ModelResult:
        """工作区问答：同一文本模型，但不带图片、不触发视觉调用（D1-h）。"""
        return self._post_chat(
            messages,
            temperature=0.3,
            enable_thinking=self.settings.text_enable_thinking,
        )


class AMapPlaceProvider:
    """高德地图 Web 服务地点检索：返回供应商事实（地址、坐标、POI ID）。"""

    name = "amap"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base_url = settings.place_api_base_url.rstrip("/")
        self.api_key = settings.place_api_key

    def search(
        self, *, name: str, city_hint: str | None = None, limit: int = 5
    ) -> PlaceSearchResult:
        """搜索地点，返回多条候选（B4：不能只取 pois[0]）。"""
        if not self.api_key:
            raise ProviderNotConfigured(self.name)
        size = max(1, min(int(limit), 20))
        params: dict[str, Any] = {
            "key": self.api_key,
            "keywords": name,
            "offset": size,
            "page": 1,
            "extensions": "all",
        }
        if city_hint:
            params["city"] = city_hint

        started = time.monotonic()
        try:
            with httpx.Client(timeout=self.settings.provider_timeout_seconds) as client:
                # 用 POST + 表单体提交参数：密钥留在请求体里，不会出现在 URL，
                # 因此任何访问日志（httpx / 代理 / CDN）都不会记录到凭据。
                response = client.post(
                    f"{self.base_url}/v3/place/text",
                    data=params,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
        except httpx.TimeoutException:
            raise ProviderTimeout("amap_timeout") from None
        except httpx.HTTPError as exc:
            raise ProviderHTTPError(type(exc).__name__) from None
        duration_ms = int((time.monotonic() - started) * 1000)

        if response.status_code >= 400:
            raise ProviderHTTPError(f"amap_http_{response.status_code}", response_status=response.status_code)
        try:
            body = response.json()
        except ValueError:
            raise ProviderInvalidResponse("amap_non_json", response_status=response.status_code) from None

        if str(body.get("status")) != "1":
            # 高德在配额或鉴权失败时返回 status=0 + info，只记录 info 码，不记录请求 URL（含 key）。
            raise ProviderHTTPError(
                f"amap_status_{body.get('infocode') or body.get('info')}", response_status=response.status_code
            )
        pois = body.get("pois") or []
        if not pois:
            raise ProviderNoResult("amap_no_result", response_status=response.status_code)

        summary = {"provider": self.name, "keywords_len": len(name), "duration_ms": duration_ms}
        return PlaceSearchResult(
            candidates=[
                self._to_place_result(poi, fallback_name=name, status=response.status_code, summary=summary)
                for poi in pois[:size]
                if isinstance(poi, dict)
            ],
            response_status=response.status_code,
            request_summary=summary,
            returned=len(pois),
        )

    def lookup(self, *, name: str, city_hint: str | None) -> PlaceResult:
        """兼容入口：只取搜索到的第一条（旧调用方与单候选场景）。"""
        result = self.search(name=name, city_hint=city_hint, limit=1)
        if not result.candidates:
            raise ProviderNoResult("amap_no_result", response_status=result.response_status)
        return result.candidates[0]

    def _to_place_result(
        self, poi: dict[str, Any], *, fallback_name: str, status: int, summary: dict[str, Any]
    ) -> PlaceResult:
        longitude: float | None = None
        latitude: float | None = None
        location = poi.get("location")
        if isinstance(location, str) and "," in location:
            lng_text, lat_text = location.split(",", 1)
            try:
                longitude = float(lng_text)
                latitude = float(lat_text)
            except ValueError:
                longitude = latitude = None
        region_parts = [poi.get("pname"), poi.get("cityname"), poi.get("adname")]
        return PlaceResult(
            name=str(poi.get("name") or fallback_name),
            address=str(poi.get("address")) if poi.get("address") else None,
            region="".join(str(part) for part in region_parts if part) or None,
            latitude=latitude,
            longitude=longitude,
            provider=self.name,
            provider_place_id=str(poi.get("id")) if poi.get("id") else None,
            raw={
                "photos": poi.get("photos") or [],
                "typecode": poi.get("typecode"),
                "adcode": poi.get("adcode"),
                "citycode": poi.get("citycode"),
                "pname": poi.get("pname"),
                "cityname": poi.get("cityname"),
                "adname": poi.get("adname"),
                "location": location,
            },
            response_status=status,
            request_summary=summary,
        )
