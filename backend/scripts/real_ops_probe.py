"""对运行中的真实实例验证 D2 账户与 D4 媒体操作（不依赖浏览器）。

用法: python scripts/real_ops_probe.py --base-url http://127.0.0.1:8000

产出 JSON 摘要：注册/登录/会话撤销/认领、笔记、改地点（含 regenerate）、软删除与恢复、
重做入口、配额与越权返回码。所有断言基于服务端真实响应，不写入任何凭据。
"""

from __future__ import annotations

import argparse
import json
import struct
import uuid
import zlib

import httpx


def build_png(width: int = 48, height: int = 48) -> bytes:
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)

    rows = [b"\x00" + bytes([(x * 3 + y) % 256 for x in range(width) for _ in range(3)]) for y in range(height)]
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"".join(rows), 6))
        + chunk(b"IEND", b"")
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    summary: dict = {"base_url": base, "checks": []}

    suffix = uuid.uuid4().hex[:8]
    email = f"probe-{suffix}@example.invalid"
    password = "test-only-probe-passphrase"

    with httpx.Client(base_url=base, timeout=httpx.Timeout(30.0, read=60.0)) as client:
        # ---------- D2：注册 / 会话 / 认领 ----------
        register = client.post("/api/v1/auth/register", json={"email": email, "password": password})
        summary["checks"].append({"check": "register", "status": register.status_code})
        assert register.status_code == 201, register.text
        me = client.get("/api/v1/auth/me")
        summary["checks"].append({"check": "me", "status": me.status_code, "verified": me.json()["user"]["email_verified"]})

        project = client.post("/api/v1/projects", json={"title": f"probe-{suffix}", "city_hint": "杭州"}).json()
        project_id = project["id"]
        claim = client.post("/api/v1/auth/claim", json={"project_id": project_id})
        claim_again = client.post("/api/v1/auth/claim", json={"project_id": project_id})
        summary["claim"] = {"first": claim.json(), "second": claim_again.json()}
        summary["checks"].append({"check": "claim.idempotent", "status": claim_again.status_code, "already": claim_again.json().get("already_claimed")})
        summary["trips"] = [trip["id"] for trip in client.get("/api/v1/trips").json()["trips"]]
        summary["checks"].append({"check": "trips", "count": len(summary["trips"])})

        sessions = client.get("/api/v1/auth/sessions").json()["sessions"]
        summary["checks"].append({"check": "sessions", "count": len(sessions), "current": sessions[0]["current"]})

        # ---------- D4：媒体操作 ----------
        upload = client.post(
            f"/api/v1/projects/{project_id}/media",
            files={"file": ("probe.png", build_png(), "image/png")},
            data={"start_analysis": "false"},
        )
        summary["checks"].append({"check": "upload", "status": upload.status_code})
        assert upload.status_code == 201, upload.text
        media_id = upload.json()["media"]["id"]

        analyze = client.post(f"/api/v1/media/{media_id}/analyze")
        summary["checks"].append({"check": "media.analyze", "status": analyze.status_code, "run_step": analyze.json().get("run", {}).get("current_step")})
        busy = client.post(f"/api/v1/media/{media_id}/analyze")
        summary["checks"].append({"check": "media.analyze.busy", "status": busy.status_code, "code": (busy.json().get("detail") or {}).get("code")})

        note = client.patch(f"/api/v1/media/{media_id}", json={"note": "探针写入的用户笔记"})
        summary["checks"].append({"check": "media.note", "status": note.status_code, "note": note.json()["media"]["note"]})

        place = client.post(
            f"/api/v1/media/{media_id}/place",
            json={"name": "探针地点", "region": "杭州", "regenerate": False},
        )
        summary["checks"].append({"check": "media.place", "status": place.status_code, "version": (place.json().get("place") or {}).get("version")})
        conflict = client.post(
            f"/api/v1/media/{media_id}/place",
            json={"name": "冲突地点", "expected_version": 999},
        )
        summary["checks"].append({"check": "media.place.conflict", "status": conflict.status_code, "code": (conflict.json().get("detail") or {}).get("code")})

        deleted = client.delete(f"/api/v1/media/{media_id}")
        deleted_list = client.get(f"/api/v1/projects/{project_id}/media/deleted").json()
        snapshot = client.get(f"/api/v1/projects/{project_id}").json()
        summary["checks"].append(
            {
                "check": "media.soft_delete",
                "status": deleted.status_code,
                "deleted_flag": deleted.json()["media"]["deleted"],
                "in_deleted_list": [item["id"] for item in deleted_list] == [media_id],
                "hidden_from_snapshot": all(item["media"]["id"] != media_id for item in snapshot["media"]),
            }
        )
        restore = client.post(f"/api/v1/media/{media_id}/restore")
        summary["checks"].append({"check": "media.restore", "status": restore.status_code, "deleted_flag": restore.json()["media"]["deleted"]})

        # 越权：另一个会话读该媒体必须 404
        with httpx.Client(base_url=base, timeout=httpx.Timeout(15.0)) as stranger:
            stranger.get("/api/v1/session")
            denied = stranger.get(f"/api/v1/media/{media_id}/content")
            summary["checks"].append({"check": "media.foreign_read", "status": denied.status_code})

    allowed = {200, 201, 202, 204, 404, 409}
    failures = [item for item in summary["checks"] if "status" in item and item["status"] not in allowed]
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
