"""对运行中的真实实例验证 D3-b：收藏 + 真实高德算路（步行/驾车）。

用法: python scripts/real_route_probe.py --base-url http://127.0.0.1:8000
产出 JSON 摘要（距离/时长/几何点数/供应商），用于 D5 证据。
"""

from __future__ import annotations

import argparse
import json

import httpx

STOPS = [
    {"name": "西湖（断桥）", "latitude": 30.2589, "longitude": 120.1510},
    {"name": "平湖秋月", "latitude": 30.2536, "longitude": 120.1440},
    {"name": "雷峰塔", "latitude": 30.2313, "longitude": 120.1489},
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--query", default="西湖")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    summary: dict = {"base_url": base, "checks": []}
    with httpx.Client(base_url=base, timeout=httpx.Timeout(30.0, read=60.0)) as client:
        ready = client.get("/api/health/ready").json()
        summary["provider_mode"] = ready["checks"].get("provider_mode")
        summary["checks"].append({"check": "ready", "status": ready["status"]})

        project = client.post("/api/v1/projects", json={"title": "real-route-probe", "city_hint": "杭州"}).json()
        project_id = project["id"]
        summary["project_id"] = project_id

        search = client.get(f"/api/v1/projects/{project_id}/places/search", params={"q": args.query}).json()
        summary["search"] = {
            "fixture": search["fixture"],
            "provider": search["provider"],
            "candidates": [
                {
                    "name": item["name"],
                    "region": item["region"],
                    "match_kind": item["match_kind"],
                    "provider_place_id": item["provider_place_id"],
                    "latitude": item["latitude"],
                    "longitude": item["longitude"],
                }
                for item in search["candidates"][:3]
            ],
        }
        summary["checks"].append({"check": "places.search", "candidates": len(search["candidates"]), "fixture": search["fixture"]})

        saved = []
        for stop in STOPS:
            response = client.post(
                f"/api/v1/projects/{project_id}/saved-places",
                json={
                    "name": stop["name"],
                    "region": "浙江省杭州市西湖区",
                    "latitude": stop["latitude"],
                    "longitude": stop["longitude"],
                    "provider": "user",
                    "note": "真实高德算路探针",
                },
            )
            response.raise_for_status()
            saved.append(response.json())
        duplicate = client.post(
            f"/api/v1/projects/{project_id}/saved-places",
            json={
                "name": STOPS[0]["name"],
                "region": "浙江省杭州市西湖区",
                "latitude": STOPS[0]["latitude"],
                "longitude": STOPS[0]["longitude"],
                "provider": "user",
            },
        ).json()
        listing = client.get(f"/api/v1/projects/{project_id}/saved-places").json()["saved_places"]
        summary["favorites"] = {"created": len(saved), "rows": len(listing), "idempotent_same_id": duplicate["id"] == saved[0]["id"]}
        summary["checks"].append({"check": "favorites.idempotent", "same_id": duplicate["id"] == saved[0]["id"]})

        for mode in ("walking", "driving"):
            draft = client.post(
                f"/api/v1/projects/{project_id}/routes",
                json={"name": f"真实算路-{mode}", "mode": mode, "stops": STOPS},
            ).json()
            computed = client.post(f"/api/v1/projects/{project_id}/routes/{draft['id']}/compute").json()
            revision = computed["current_revision"]
            summary.setdefault("routes", {})[mode] = {
                "status": revision["status"] if revision else None,
                "provider": revision["provider"] if revision else None,
                "distance_meters": revision["distance_meters"] if revision else None,
                "duration_seconds": revision["duration_seconds"] if revision else None,
                "legs": [
                    {"distance_meters": leg["distance_meters"], "duration_seconds": leg["duration_seconds"]}
                    for leg in (revision["legs"] if revision else [])
                ],
                "geometry_points": len((revision["geometry"] or "").split(";")) if revision else 0,
                "route_unavailable_reason": computed["route_unavailable_reason"],
            }
            summary["checks"].append(
                {"check": f"route.{mode}", "status": revision["status"] if revision else None, "provider": revision["provider"] if revision else None}
            )

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if all(item.get("status") not in (None, "FAILED") or "status" not in item for item in summary["checks"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
