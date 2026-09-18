"""FIXTURE Provider —— 只允许用于自动测试和本地故障演练。

安全边界：
- 本模块所有输出都带 ``"fixture": true`` 标记，便于在数据库和界面中一眼识别。
- :func:`app.agent.providers.build_provider_set` 在生产环境直接拒绝 fixture。
- fixture 结果不能作为线上成功结果：S4 会单独跑真实 Provider smoke。

故障注入（用于 S4 故障测试，由 ``FIXTURE_BEHAVIORS`` 环境变量配置）：
``ok`` / ``slow`` / ``timeout`` / ``http_error`` / ``invalid_json`` / ``no_result``

``slow`` 会阻塞 30 秒，用于"Worker 执行中被杀"的恢复演练。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.agent.providers.base import (
    ModelResult,
    PlaceResult,
    PlaceSearchResult,
    ProviderHTTPError,
    ProviderNoResult,
    ProviderTimeout,
)

FIXTURE_MARKER = "fixture"

# 仅测试使用：记录每个 fixture Provider 实际被调用了几次，用于核对"调用次数 = 记账行数"。
CALL_COUNTER: dict[str, int] = {}


def _count(provider: str) -> None:
    CALL_COUNTER[provider] = CALL_COUNTER.get(provider, 0) + 1


def parse_behaviors(raw: str) -> dict[str, str]:
    behaviors: dict[str, str] = {}
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        behaviors[key.strip().lower()] = value.strip().lower()
    return behaviors


def _maybe_fail(behavior: str | None, provider: str) -> None:
    if behavior and behavior.startswith("slow"):
        # 仅用于测试：让步骤阻塞指定秒数（slow 或 slow:3），
        # 便于在它执行期间杀掉 Worker，或制造"旧持有者晚返回"的竞态。
        import time as _time

        duration = 30.0
        if ":" in behavior:
            try:
                duration = float(behavior.split(":", 1)[1])
            except ValueError:
                duration = 30.0
        _time.sleep(duration)
    if behavior == "timeout":
        raise ProviderTimeout(f"{provider}_fixture_timeout")
    if behavior == "http_error":
        raise ProviderHTTPError(f"{provider}_fixture_http_500", response_status=500)
    if behavior == "no_result":
        raise ProviderNoResult(f"{provider}_fixture_no_result")


class FixtureVisionProvider:
    name = "fixture-vision"

    def __init__(self, behavior: str = "ok") -> None:
        self.behavior = behavior

    def analyze_image(
        self,
        *,
        image_bytes: bytes,
        mime_type: str,
        city_hint: str | None,
        messages: list[dict[str, Any]],
    ) -> ModelResult:
        _count(self.name)
        _maybe_fail(self.behavior, "vision")
        digest = hashlib.sha256(image_bytes).hexdigest()[:8]
        if self.behavior == "invalid_json":
            text = "这不是 JSON {"
        else:
            text = json.dumps(
                {
                    "fixture": True,
                    "scene_summary": f"[FIXTURE] 测试用场景摘要 {digest}（未调用真实多模态模型）",
                    "candidates": [
                        {
                            "name": f"fixture-place-{digest}",
                            "address": None,
                            "region": city_hint,
                            "rationale": "[FIXTURE] 由测试替身生成的视觉依据，不来自真实模型。",
                            "confidence": 0.5,
                            "uncertainty": "[FIXTURE] 自动测试数据。",
                        }
                    ],
                    "needs_user_confirmation": True,
                },
                ensure_ascii=False,
            )
        return ModelResult(
            text=text,
            model="fixture-vision",
            prompt_tokens=10,
            completion_tokens=20,
            response_status=200,
            request_summary={"provider": self.name, "fixture": True},
        )


class FixtureTextProvider:
    name = "fixture-text"

    def __init__(self, behavior: str = "ok") -> None:
        self.behavior = behavior

    def generate_guide_card(self, *, messages: list[dict[str, Any]]) -> ModelResult:
        _count(self.name)
        _maybe_fail(self.behavior, "text")
        place_name = "[未知地点]"
        for message in messages:
            content = message.get("content")
            if isinstance(content, str) and "place_name:" in content:
                for line in content.splitlines():
                    if line.strip().startswith("place_name:"):
                        place_name = line.split(":", 1)[1].strip()
        if self.behavior == "invalid_json":
            text = "not-a-json{}"
        else:
            text = json.dumps(
                {
                    "fixture": True,
                    "title": f"[FIXTURE] {place_name} 导游卡片",
                    "summary": "[FIXTURE] 自动测试使用的导游摘要，未调用真实文本模型。",
                    "sections": [
                        {"heading": "看点", "body": "[FIXTURE] 测试正文。"},
                        {"heading": "建议", "body": "[FIXTURE] 测试建议。"},
                    ],
                    "tips": ["[FIXTURE] 测试提示"],
                },
                ensure_ascii=False,
            )
        return ModelResult(
            text=text,
            model="fixture-text",
            prompt_tokens=15,
            completion_tokens=25,
            response_status=200,
            request_summary={"provider": self.name, "fixture": True},
        )

    def answer_question(self, *, messages: list[dict[str, Any]]) -> ModelResult:
        """[FIXTURE] 工作区问答替身：回显问题，绝不调用视觉模型。"""
        _count(self.name + ":answer")
        _maybe_fail(self.behavior, "text")
        question = ""
        for message in messages:
            content = message.get("content")
            if isinstance(content, str) and "question:" in content:
                question = content.split("question:", 1)[1].strip().splitlines()[0]
        if self.behavior == "invalid_json":
            text = "not-a-json{}"
        else:
            text = json.dumps(
                {
                    "fixture": True,
                    "answer": f"[FIXTURE] 对「{question}」的测试回答，未调用真实文本模型。",
                    "followups": ["[FIXTURE] 还想了解什么？"],
                    "used_place_ids": [],
                    "uncertainty": "[FIXTURE] 自动测试数据，不代表真实答案。",
                    "needs_place_choice": False,
                },
                ensure_ascii=False,
            )
        return ModelResult(
            text=text,
            model="fixture-text",
            prompt_tokens=12,
            completion_tokens=18,
            response_status=200,
            request_summary={"provider": self.name, "fixture": True},
        )



class FixtureDirectionProvider:
    """[FIXTURE] 算路替身：按停留点顺序生成固定距离/时长，用于自动测试。

    输出带 fixture 标记，绝不能当作真实高德结果：
    真实距离/时长必须在 real 模式下由 AMapDirectionProvider 返回。
    """

    name = "fixture-route"

    def __init__(self, behavior: str = "ok", *, step_meters: int = 1200, step_seconds: int = 900) -> None:
        self.behavior = behavior
        self.step_meters = step_meters
        self.step_seconds = step_seconds

    def route(self, *, mode, origin, destination, waypoints=None):  # noqa: ANN001, ANN201
        from app.agent.providers.direction import RouteLegResult, RouteResult

        _count(self.name)
        _maybe_fail(self.behavior, "route")
        points = [origin, *(waypoints or []), destination]
        if len(points) < 2:
            raise ProviderNoResult("fixture_route_needs_two_points")
        legs: list[RouteLegResult] = []
        polylines: list[str] = []
        for index in range(len(points) - 1):
            start, end = points[index], points[index + 1]
            polyline = f"{start[0]:.6f},{start[1]:.6f};{end[0]:.6f},{end[1]:.6f}"
            polyline_src = polyline.replace(",", ",")
            polylines.append(polyline_src)
            legs.append(
                RouteLegResult(
                    index_from=index,
                    index_to=index + 1,
                    distance_meters=self.step_meters,
                    duration_seconds=self.step_seconds if mode == "walking" else max(self.step_seconds // 4, 60),
                    polyline=polyline_src,
                )
            )
        return RouteResult(
            mode=mode,
            provider=self.name,
            distance_meters=self.step_meters * (len(points) - 1),
            duration_seconds=(self.step_seconds if mode == "walking" else max(self.step_seconds // 4, 60))
            * (len(points) - 1),
            legs=legs,
            geometry=";".join(polylines),
            raw={"fixture": True, "note": "fixture 算路数据，不是真实高德结果"},
            response_status=200,
            request_summary={"provider": self.name, "fixture": True, "points": len(points), "mode": mode},
        )


class FixturePlaceProvider:
    name = "fixture-amap"

    def __init__(self, behavior: str = "ok") -> None:
        self.behavior = behavior

    # B4 回归场景：place=ambiguous 三个同名不同城；place=city_conflict 同名但城市冲突；
    # place=name_mismatch 只返回模糊名称结果（不能直接采用）。
    SCENARIOS: dict[str, list[dict[str, Any]]] = {
        "ambiguous": [
            {"id": "fixture-hz-01", "name": "{name}", "address": "[FIXTURE] 杭州市同名 POI", "region": "浙江省杭州市西湖区", "lat": 30.25, "lng": 120.15},
            {"id": "fixture-yz-01", "name": "{name}", "address": "[FIXTURE] 扬州市同名 POI", "region": "江苏省扬州市邗江区", "lat": 32.39, "lng": 119.42},
            {"id": "fixture-hz-02", "name": "{name}", "address": "[FIXTURE] 惠州市同名 POI", "region": "广东省惠州市惠城区", "lat": 23.11, "lng": 114.41},
        ],
        "city_conflict": [
            {"id": "fixture-yz-02", "name": "{name}", "address": "[FIXTURE] 外省同名 POI", "region": "江苏省扬州市邗江区", "lat": 32.39, "lng": 119.42},
        ],
        "name_mismatch": [
            {"id": "fixture-fuzzy-01", "name": "{name}-附近景点", "address": "[FIXTURE] 模糊结果", "region": None, "lat": 30.0, "lng": 120.0},
        ],
    }

    def search(
        self, *, name: str, city_hint: str | None = None, limit: int = 5
    ) -> PlaceSearchResult:
        _count(self.name)
        _maybe_fail(self.behavior, "place")
        scenario = self.SCENARIOS.get(self.behavior)
        if scenario is None:
            candidates = [
                {
                    "id": f"fixture-{abs(hash(name)) % 10**8}",
                    "name": name,
                    "address": f"[FIXTURE] {name} 的测试地址",
                    "region": city_hint,
                    "lat": 30.0,
                    "lng": 120.0,
                }
            ]
        else:
            candidates = scenario
        results = [
            PlaceResult(
                name=str(item["name"]).replace("{name}", name),
                address=item.get("address"),
                region=item.get("region"),
                latitude=item.get("lat"),
                longitude=item.get("lng"),
                provider=self.name,
                provider_place_id=item.get("id"),
                raw={"fixture": True, "scenario": self.behavior, "note": "fixture 地点数据，不是真实供应商结果"},
                response_status=200,
            )
            for item in candidates[: max(1, limit)]
        ]
        return PlaceSearchResult(
            candidates=results,
            response_status=200,
            request_summary={"provider": self.name, "fixture": True},
            returned=len(candidates),
        )

    def lookup(self, *, name: str, city_hint: str | None) -> PlaceResult:
        found = self.search(name=name, city_hint=city_hint, limit=1)
        if not found.candidates:
            raise ProviderNoResult(f"{self.name}_fixture_no_result")
        return found.candidates[0]
