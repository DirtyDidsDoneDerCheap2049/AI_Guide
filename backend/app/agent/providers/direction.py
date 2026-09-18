"""高德路径规划（D3-b）：步行 / 驾车。

严格区分"供应商事实"与"我们算的"：
- 距离、时长、几何全部来自高德返回（``route.paths[0].distance/duration/steps[].polyline``）；
- 拿不到就报错（``ProviderNoResult`` / ``ProviderHTTPError``），绝不用直线距离或估算值冒充。
- 与地点检索一样：密钥放请求体，URL 与日志里都不出现 key。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.agent.providers.base import (
    ProviderHTTPError,
    ProviderInvalidResponse,
    ProviderNoResult,
    ProviderNotConfigured,
    ProviderTimeout,
)
from app.config import Settings

MODE_WALKING = "walking"
MODE_DRIVING = "driving"

_EXPECTED_COORD_SYSTEM = "gcj02"


@dataclass
class RouteLegResult:
    """一段相邻停留点之间的结果（全部来自供应商）。"""

    index_from: int
    index_to: int
    distance_meters: int
    duration_seconds: int
    polyline: str = ""


@dataclass
class RouteResult:
    mode: str
    provider: str
    distance_meters: int
    duration_seconds: int
    legs: list[RouteLegResult] = field(default_factory=list)
    geometry: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    response_status: int | None = 200
    request_summary: dict[str, Any] = field(default_factory=dict)


class AMapDirectionProvider:
    """高德 Web 服务路径规划（``/v3/direction/{walking,driving}``）。"""

    name = "amap-route"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base_url = settings.place_api_base_url.rstrip("/")
        self.api_key = settings.place_api_key

    def route(
        self,
        *,
        mode: str,
        origin: tuple[float, float],
        destination: tuple[float, float],
        waypoints: list[tuple[float, float]] | None = None,
    ) -> RouteResult:
        """一次算路。

        ``origin``/``destination`` 是 GCJ-02 的 ``(经度, 纬度)``（高德坐标系）。
        """
        if not self.api_key:
            raise ProviderNotConfigured(self.name)
        if mode not in (MODE_WALKING, MODE_DRIVING):
            raise ProviderHTTPError(f"unsupported_mode_{mode}", response_status=None)

        path = "/v3/direction/walking" if mode == MODE_WALKING else "/v3/direction/driving"
        params: dict[str, Any] = {
            "key": self.api_key,
            "origin": _format_point(origin),
            "destination": _format_point(destination),
            # 需要 steps 才能拿到分段折线与分段距离
            "extensions": "all",
            "strategy": self.settings.route_driving_strategy if mode == MODE_DRIVING else 0,
        }
        if waypoints:
            # 高德驾车支持途经点（最多 16 个）；步行不支持，需要调用方分段请求
            params["waypoints"] = ";".join(_format_point(point) for point in waypoints[:16])

        started = time.monotonic()
        try:
            with httpx.Client(timeout=self.settings.provider_timeout_seconds) as client:
                response = client.post(
                    f"{self.base_url}{path}",
                    data=params,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
        except httpx.TimeoutException:
            raise ProviderTimeout("amap_route_timeout") from None
        except httpx.HTTPError as exc:
            raise ProviderHTTPError(type(exc).__name__) from None
        duration_ms = int((time.monotonic() - started) * 1000)

        if response.status_code >= 400:
            raise ProviderHTTPError(f"amap_route_http_{response.status_code}", response_status=response.status_code)
        try:
            body = response.json()
        except ValueError:
            raise ProviderInvalidResponse("amap_route_non_json", response_status=response.status_code) from None
        if str(body.get("status")) != "1":
            raise ProviderHTTPError(
                f"amap_route_status_{body.get('infocode') or body.get('info')}",
                response_status=response.status_code,
            )

        route = body.get("route") or {}
        paths = route.get("paths") or []
        if not paths:
            raise ProviderNoResult("amap_route_no_path", response_status=response.status_code)
        path0 = paths[0]

        legs: list[RouteLegResult] = []
        polylines: list[str] = []
        for step in path0.get("steps") or []:
            polyline = str(step.get("polyline") or "")
            if polyline:
                polylines.append(polyline)
            legs.append(
                RouteLegResult(
                    index_from=len(legs),
                    index_to=len(legs) + 1,
                    distance_meters=int(step.get("distance") or 0),
                    duration_seconds=int(step.get("duration") or 0),
                    polyline=polyline,
                )
            )

        summary = {
            "provider": self.name,
            "mode": mode,
            "duration_ms": duration_ms,
            "steps": len(legs),
        }
        return RouteResult(
            mode=mode,
            provider=self.name,
            distance_meters=int(path0.get("distance") or 0),
            duration_seconds=int(path0.get("duration") or 0),
            legs=legs,
            geometry=";".join(polylines),
            raw={
                "strategy": path0.get("strategy"),
                "tolls": path0.get("tolls"),
                "traffic_lights": path0.get("traffic_lights"),
                "restriction": path0.get("restriction"),
                "coordinate_system": _EXPECTED_COORD_SYSTEM,
            },
            response_status=response.status_code,
            request_summary=summary,
        )


def _format_point(point: tuple[float, float]) -> str:
    return f"{point[0]:.6f},{point[1]:.6f}"


__all__ = [
    "AMapDirectionProvider",
    "MODE_DRIVING",
    "MODE_WALKING",
    "RouteLegResult",
    "RouteResult",
]
