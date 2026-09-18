"""端到端 smoke：对运行中的 AI-Guide 实例跑完整主流程。

用法：
    python scripts/smoke.py --base-url http://127.0.0.1:8000 [--image <path>] [--timeout 180]

覆盖：ready 健康检查 → 匿名会话 → 创建项目 → SSE 实时订阅 → 上传图片 →
等待 WAITING_USER（靠 SSE 事件，不轮询业务状态）→ 确认地点 → 等待卡片 →
校验供应商事实与模型讲解分离 → Last-Event-ID 补发。

退出码：0 全部通过；1 有断言失败；2 连接或超时。
本脚本用于本地与线上 smoke；生成的内置图片明确标记为 fixture，不代表真实拍摄素材。
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import threading
import time
from dataclasses import dataclass, field

import httpx

TERMINAL_RUN_STATES = {"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"}


@dataclass
class SseRecord:
    events: list[dict] = field(default_factory=list)
    connect_count: int = 0
    errors: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def append(self, event: dict) -> None:
        with self.lock:
            self.events.append(event)

    def snapshot(self) -> list[dict]:
        with self.lock:
            return list(self.events)

    def types(self) -> list[str]:
        return [event["event"] for event in self.snapshot()]


class SseListener(threading.Thread):
    """真实 SSE 客户端：断线自动重连，并带 Last-Event-ID 续订。"""

    def __init__(self, base_url: str, cookie: str, project_id: str, record: SseRecord, timeout: float) -> None:
        super().__init__(daemon=True)
        self.base_url = base_url.rstrip("/")
        self.cookie = cookie
        self.project_id = project_id
        self.record = record
        self.deadline = time.monotonic() + timeout
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        last_event_id = 0
        while not self._stop.is_set() and time.monotonic() < self.deadline:
            url = f"{self.base_url}/api/v1/projects/{self.project_id}/events"
            params = {"last_event_id": str(last_event_id)} if last_event_id else None
            try:
                with httpx.Client(timeout=httpx.Timeout(10.0, read=self.deadline)) as client:
                    with client.stream(
                        "GET",
                        url,
                        params=params,
                        headers={"Cookie": self.cookie, "Accept": "text/event-stream"},
                    ) as response:
                        if response.status_code != 200:
                            self.record.errors += 1
                            time.sleep(1.0)
                            continue
                        self.record.connect_count += 1
                        current: dict = {}
                        for line in response.iter_lines():
                            if self._stop.is_set():
                                return
                            if line == "":
                                if current.get("event"):
                                    current["received_at"] = time.time()
                                    self.record.append(current)
                                    last_event_id = max(last_event_id, int(current.get("id") or 0))
                                current = {}
                                continue
                            if line.startswith("id: "):
                                current["id"] = int(line[4:])
                            elif line.startswith("event: "):
                                current["event"] = line[7:]
                            elif line.startswith("data: "):
                                try:
                                    current["data"] = json.loads(line[6:])
                                except json.JSONDecodeError:
                                    current["data"] = {"raw": line[6:]}
            except httpx.HTTPError:
                self.record.errors += 1
            time.sleep(0.5)


def detect_mime(data: bytes) -> str:
    """按实际字节判断 MIME，避免把 JPEG 当成 PNG 上传（服务端会返回 415）。"""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            fmt = (image.format or "").upper()
    except Exception:
        return "application/octet-stream"
    return {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}.get(fmt, "application/octet-stream")


def fixture_png() -> bytes:
    """测试用图片（fixture）：带 FIXTURE 水印的合成图，故意不含任何可识别场景。

    它只用于验证"上传 -> 队列 -> Worker -> 候选 -> 确认 -> 卡片"的链路，
    不能用来评估视觉识别效果。真实识别需要真实照片 + PROVIDER_MODE=real。
    """
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (320, 240), (235, 238, 242))
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, 319, 44], fill=(180, 40, 40))
    draw.text((12, 14), "FIXTURE / TEST IMAGE", fill=(255, 255, 255))
    draw.text((12, 70), "not a real photo", fill=(60, 60, 60))
    draw.text((12, 92), "no scene content", fill=(60, 60, 60))
    draw.text((12, 114), "vision provider is mocked", fill=(60, 60, 60))
    draw.rectangle([12, 150, 308, 228], outline=(150, 150, 150))
    draw.text((20, 182), "AI-Guide smoke fixture", fill=(120, 120, 120))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def wait_for(record: SseRecord, predicate, timeout: float, description: str) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for event in record.snapshot():
            if predicate(event):
                return event
        time.sleep(0.2)
    raise TimeoutError(f"timeout waiting for {description}; events={record.types()}")


def run_smoke(base_url: str, image_path: str | None, timeout: float, confirm_name: str | None = None) -> dict:
    base_url = base_url.rstrip("/")
    summary: dict = {"base_url": base_url, "checks": [], "events": [], "provider_mode": None, "image_source": None}
    client = httpx.Client(base_url=base_url, timeout=httpx.Timeout(30.0, read=30.0), follow_redirects=True)

    ready = client.get("/api/health/ready")
    summary["checks"].append({"check": "health.ready", "status": ready.status_code})
    if ready.status_code != 200:
        raise RuntimeError(f"service not ready: {ready.status_code} {ready.text[:300]}")
    summary["provider_mode"] = ready.json().get("checks", {}).get("provider_mode")

    session = client.get("/api/v1/session")
    session.raise_for_status()
    cookie = "; ".join(f"{key}={value}" for key, value in client.cookies.items())
    summary["checks"].append({"check": "session", "status": session.status_code})

    title = f"smoke-{int(time.time())}"
    project = client.post("/api/v1/projects", json={"title": title, "city_hint": "杭州"})
    if project.status_code == 409:
        # 该会话已有活动项目：直接复用（smoke 可重复执行）。
        project = client.get("/api/v1/projects/current")
    project.raise_for_status()
    project_id = project.json()["id"]
    summary["project_id"] = project_id
    summary["checks"].append({"check": "project", "status": project.status_code})

    record = SseRecord()
    listener = SseListener(base_url, cookie, project_id, record, timeout)
    listener.start()
    time.sleep(0.5)

    if image_path:
        data = open(image_path, "rb").read()
        filename = image_path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        summary["image_source"] = f"file:{filename}"
    else:
        data = fixture_png()
        filename = "smoke-fixture.png"
        summary["image_source"] = "generated_fixture_png"

    upload = client.post(
        f"/api/v1/projects/{project_id}/media",
        files={"file": (filename, data, detect_mime(data))},
        data={"start_analysis": "true"},
        headers={"Idempotency-Key": f"smoke-{project_id}-{filename}"},
    )
    if upload.status_code == 429:
        raise RuntimeError("demo run quota exhausted (429)")
    upload.raise_for_status()
    upload_body = upload.json()
    run_id = upload_body["run"]["id"]
    summary["run_id"] = run_id
    summary["checks"].append({"check": "upload", "status": upload.status_code, "media_id": upload_body["media"]["id"]})

    wait_for(record, lambda event: event["event"] == "run.waiting_user", timeout, "run.waiting_user")
    summary["checks"].append({"check": "sse.waiting_user", "status": "received"})

    snapshot = client.get(f"/api/v1/projects/{project_id}").json()
    media_snapshot = next(
        item for item in snapshot["media"] if item["media"]["id"] == upload_body["media"]["id"]
    )
    if not media_snapshot["candidates"]:
        raise RuntimeError("no place candidates returned")
    candidate = media_snapshot["candidates"][0]
    summary["candidate"] = {"name": candidate["name"], "confidence": candidate["confidence"]}

    # 默认确认模型给出的候选；--confirm-name 用于模拟用户纠正地点名（真实地图检索更稳）。
    if confirm_name:
        body = {"decision": "correct", "candidate_id": candidate["id"], "name": confirm_name}
        summary["user_corrected_to"] = confirm_name
    else:
        body = {"decision": "confirm", "candidate_id": candidate["id"]}
    confirm = client.post(f"/api/v1/runs/{run_id}/confirm-place", json=body)
    confirm.raise_for_status()
    summary["checks"].append({"check": "confirm_place", "status": confirm.status_code})

    # B4：供应商返回多条同名 POI 或城市冲突时，运行会停在"地点消歧"步骤，
    # 必须由用户从**供应商候选**里选一条；这一步可能重复出现，因此用循环推进：
    # 每次等到"消歧要求 / 卡片就绪 / 失败"三类事件之一，再决定下一步。
    # 演进循环：以**运行状态**为准（事件只用于证明实时性）。
    # 消歧可能反复出现，因此这里每轮都重新读状态，避免把已经处理过的旧事件当成新要求。
    def resolve_disambiguation() -> bool:
        snapshot = client.get(f"/api/v1/projects/{project_id}").json()
        media_snapshot = next(
            item for item in snapshot["media"] if item["media"]["id"] == upload_body["media"]["id"]
        )
        provider_candidates = media_snapshot.get("provider_candidates") or []
        if not provider_candidates:
            raise RuntimeError("disambiguation required but no provider candidates exposed")
        chosen = provider_candidates[0]
        summary.setdefault("disambiguation_rounds", []).append(
            {
                "candidates": len(provider_candidates),
                "chosen": chosen["name"],
                "chosen_region": chosen["region"],
                "chosen_provider_place_id": chosen["provider_place_id"],
                "match_kind": chosen.get("match_kind"),
            }
        )
        summary["checks"].append(
            {"check": "disambiguation", "status": "required", "candidates": len(provider_candidates)}
        )
        resolved = client.post(
            f"/api/v1/runs/{run_id}/confirm-place",
            json={"decision": "correct", "provider_candidate_id": chosen["id"]},
        )
        resolved.raise_for_status()
        summary["checks"].append({"check": "disambiguation_choice", "status": resolved.status_code})
        return True

    deadline = time.monotonic() + timeout
    rounds = 0
    final_state = None
    while time.monotonic() < deadline:
        state = client.get(f"/api/v1/runs/{run_id}").json()
        if state["status"] == "WAITING_USER" and state["current_step"] == "wait_for_place_disambiguation":
            if rounds >= 3:
                raise RuntimeError("disambiguation repeated more than 3 times")
            rounds += 1
            # 等到"消歧要求"事件真的到达（证明 SSE 实时性），再提交选择
            wait_for(
                record,
                lambda event: event["event"] == "place.disambiguation_required",
                timeout,
                "place.disambiguation_required",
            )
            resolve_disambiguation()
            continue
        if state["status"] in TERMINAL_RUN_STATES:
            final_state = state
            break
        time.sleep(2)

    if final_state is None:
        raise TimeoutError(f"timeout waiting for terminal state; events={record.types()}")

    # 终态必须能从 SSE 事件里看到（卡片就绪 / 失败 / 局部 / 取消）
    terminal = wait_for(
        record,
        lambda event: event["event"] in {"card.ready", "run.failed", "run.cancelled", "run.partial"},
        min(timeout, 30.0),
        "card.ready or failure",
    )
    summary["terminal_event"] = terminal["event"]

    final = client.get(f"/api/v1/runs/{run_id}").json()
    summary["run_status"] = final["status"]
    summary["checks"].append({"check": "run_status", "status": final["status"], "error_code": final["error_code"]})

    snapshot = client.get(f"/api/v1/projects/{project_id}").json()
    media_snapshot = next(item for item in snapshot["media"] if item["media"]["id"] == upload_body["media"]["id"])
    card = media_snapshot["card"]
    if card is None:
        raise RuntimeError(f"no guide card produced (run_status={final['status']})")
    summary["card"] = {
        "title": card["title"],
        "sections": len(card["sections"]),
        "partial": card["partial"],
        "place_facts_present": card["place_facts"] is not None,
        "place_provider": (media_snapshot["place"] or {}).get("provider"),
    }
    summary["checks"].append({"check": "card", "status": "present"})

    # SSE 断线补发：以第二条事件为起点重连，应当收到其后所有事件。
    events_so_far = record.snapshot()
    if len(events_so_far) >= 3:
        resume_from = events_so_far[1]["id"]
        expected_ids = [event["id"] for event in events_so_far if event["id"] > resume_from]
        replay = httpx.Client(base_url=base_url, timeout=httpx.Timeout(10.0, read=15.0))
        replay_ids: list[int] = []
        with replay.stream(
            "GET",
            f"/api/v1/projects/{project_id}/events",
            headers={"Cookie": cookie, "Last-Event-ID": str(resume_from)},
        ) as response:
            current: dict = {}
            for line in response.iter_lines():
                if line == "":
                    if current.get("id"):
                        replay_ids.append(current["id"])
                        if len(replay_ids) >= len(expected_ids):
                            break
                    current = {}
                    continue
                if line.startswith("id: "):
                    current["id"] = int(line[4:])
        replay.close()
        missing = [event_id for event_id in expected_ids if event_id not in replay_ids]
        summary["checks"].append(
            {"check": "sse.replay", "status": "ok" if not missing else "missing", "missing": missing}
        )
        if missing:
            raise RuntimeError(f"SSE replay missing events: {missing}")

    listener.stop()
    listener.join(timeout=5)
    client.close()

    if summary.get("provider_mode") == "mock":
        # 明确标注：fixture 模式下的候选与卡片是占位内容，不能当作识别结果。
        summary["fixture_notice"] = (
            "本次 smoke 使用 fixture Provider（PROVIDER_MODE=mock），候选地点与导游卡片均为占位内容，"
            "不验证真实模型与地图服务的识别效果。真实识别需要 --image 传真实照片并把 PROVIDER_MODE 设为 real。"
        )
    summary["events"] = [
        {"id": event["id"], "event": event["event"], "received_at": round(event.get("received_at", 0), 3)}
        for event in record.snapshot()
    ]
    summary["sse_connections"] = record.connect_count
    summary["sse_errors"] = record.errors
    summary["status"] = "ok"
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI-Guide 端到端 smoke")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--image", default=None, help="可选：使用真实图片文件代替内置 fixture 图")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--confirm-name", default=None, help="可选：以纠正方式提交的地点名（模拟用户纠正）")
    args = parser.parse_args(argv)

    try:
        summary = run_smoke(args.base_url, args.image, args.timeout, args.confirm_name)
    except TimeoutError as exc:
        print(json.dumps({"status": "timeout", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"status": "failed", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False, indent=2))
        return 1

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
