#!/usr/bin/env python3
"""Compose 端到端冒烟（只为"镜像能真正跑起来"这件事提供证据）。

与 `backend/scripts/smoke.py` 的区别：
- `smoke.py` 面向**真实供应商**的小样本验证（需要真密钥）。
- 本脚本面向**部署链路**：证明 MySQL/Redis/api/worker/媒体卷在一个 compose 环境里确实串起来了。
  它会在 CI 里用占位密钥运行，因此**不声称**模型调用成功；它断言的是：

  1. API 可创建项目、可接收真实图片上传（媒体卷可写）；
  2. 触发的分析任务被 Redis 投递、被独立 Worker 认领并推进到某个确定状态
     （WAITING_USER / SUCCEEDED / PARTIAL / FAILED 都算"Worker 真的在工作"）；
  3. 状态变化写进了 MySQL，而不是只留在队列里；
  4. 用 --expect 可以收紧期望（例如真实密钥环境下要求 WAITING_USER）。

用法:
  python3 backend/scripts/compose_smoke.py --base-url http://127.0.0.1:8080 \
      [--expect WAITING_USER,SUCCEEDED] [--timeout 180]
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import struct
import sys
import time
import urllib.error
import urllib.request
import uuid
import zlib

TERMINAL_OR_WAITING = {"WAITING_USER", "SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"}
HTTP_CLIENT = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def build_png(width: int = 64, height: int = 64) -> bytes:
    """生成一张最小合法 PNG（不依赖 Pillow，CI 镜像里不用装图像库）。"""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + bytes([(x * 4) % 256, 128, 200]) * width for _ in range(height)) if False else None
    if raw is None:
        rows = []
        for y in range(height):
            rows.append(b"\x00" + bytes([(x + y) % 256 for x in range(width) for _ in range(3)]))
        raw = b"".join(rows)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


def request(method: str, url: str, *, data: bytes | None = None, headers: dict[str, str] | None = None):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with HTTP_CLIENT.open(req, timeout=30) as response:
            body = response.read()
            return response.status, body, dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers or {})


def multipart(fields: dict[str, str], filename: str, content: bytes, mime: str) -> tuple[bytes, str]:
    boundary = f"----ai-guide-smoke-{uuid.uuid4().hex}"
    lines: list[bytes] = []
    for key, value in fields.items():
        lines.append(f"--{boundary}\r\n".encode())
        lines.append(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
        lines.append(f"{value}\r\n".encode())
    lines.append(f"--{boundary}\r\n".encode())
    lines.append(f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode())
    lines.append(f"Content-Type: {mime}\r\n\r\n".encode())
    lines.append(content)
    lines.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(lines), boundary


def main() -> int:
    parser = argparse.ArgumentParser(description="Compose 部署链路冒烟")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--expect", default="", help="逗号分隔的期望运行状态，例如 WAITING_USER,SUCCEEDED")
    parser.add_argument("--timeout", type=int, default=180, help="等待运行进入确定状态的秒数")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    expected = {item.strip() for item in args.expect.split(",") if item.strip()}

    status, body, _ = request("GET", f"{base}/api/health/ready")
    if status != 200:
        print(f"[smoke] readiness 失败: HTTP {status} {body[:200]!r}", file=sys.stderr)
        return 2
    print(f"[smoke] readiness OK: {body[:200].decode('utf-8', 'replace')}")

    status, body, _ = request(
        "POST",
        f"{base}/api/v1/projects",
        data=json.dumps({"title": f"compose-smoke-{uuid.uuid4().hex[:8]}", "city_hint": "杭州"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    if status not in (200, 201):
        print(f"[smoke] 创建项目失败: HTTP {status} {body[:300]!r}", file=sys.stderr)
        return 2
    project_id = json.loads(body)["id"]
    print(f"[smoke] 项目: {project_id}")

    payload, boundary = multipart(
        {"start_analysis": "false"}, "smoke.png", build_png(), "image/png"
    )
    status, body, _ = request(
        "POST",
        f"{base}/api/v1/projects/{project_id}/media",
        data=payload,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    if status != 201:
        print(f"[smoke] 上传失败: HTTP {status} {body[:300]!r}", file=sys.stderr)
        return 2
    media = json.loads(body)["media"]
    print(f"[smoke] 媒体: {media['id']} ({media['size_bytes']} 字节, {media['mime_type']})")

    status, body, _ = request("POST", f"{base}/api/v1/media/{media['id']}/analyze")
    if status not in (200, 202):
        print(f"[smoke] 触发分析失败: HTTP {status} {body[:300]!r}", file=sys.stderr)
        return 2
    run_id = json.loads(body)["run"]["id"]
    print(f"[smoke] 运行: {run_id}（已投递队列，等待 Worker 推进）")

    deadline = time.time() + args.timeout
    last_state = ""
    while time.time() < deadline:
        status, body, _ = request("GET", f"{base}/api/v1/runs/{run_id}")
        if status == 200:
            run = json.loads(body)
            state = run["status"]
            if state != last_state:
                print(f"[smoke] 状态: {state}（step={run['current_step']} error={run.get('error_code')}）")
                last_state = state
            if state in TERMINAL_OR_WAITING:
                if expected and state not in expected:
                    print(
                        f"[smoke] 运行状态 {state} 不在期望集合 {sorted(expected)} 内",
                        file=sys.stderr,
                    )
                    return 1
                if state == "FAILED":
                    print(
                        "[smoke] 说明: 任务被 Worker 认领并失败（CI 使用占位密钥时属预期）。"
                        "这证明 API/队列/Worker/MySQL 链路可用，但不代表模型调用成功。"
                    )
                print(f"[smoke] 通过：Worker 已推进到 {state}")
                return 0
        time.sleep(3)

    print(f"[smoke] 超时：{args.timeout}s 内运行没有进入确定状态（最后状态 {last_state or '未知'}）", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
