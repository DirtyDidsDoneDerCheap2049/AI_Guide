"""S4：真实 HTTP Provider 代码路径测试（本地协议桩，不是厂商调用）。

为什么需要这一层：
- `PROVIDER_MODE=mock` 走的是 fixture 替身，**不能**证明真实 Provider 的 HTTP 代码可用。
- 真实厂商调用需要受限密钥（当前未提供，属于发布阻塞），所以这里用本地 HTTP 协议桩
  按 DashScope OpenAI 兼容格式与高德 v3/place/text 格式应答，验证：
  HTTP 请求构造、图片以 data URL 附带、usage 解析、Pydantic 校验、JSON 修复重试、
  429/超时/业务错误码到 ProviderError 的映射。

⚠️ 本文件所有响应都来自 127.0.0.1 上的桩服务，**不代表**任何真实厂商返回结果。
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select

from app.agent import tools
from app.agent.orchestrator import execute_run
from app.agent.providers import build_provider_set
from app.agent.providers.base import ProviderSet
from app.config import Settings
from app.models import AgentRun, GuideCard, PlaceCandidate, RunStatus
from tests.conftest import create_project, make_image_bytes, unique_title

pytestmark = [pytest.mark.integration, pytest.mark.fixture_provider]

STATE: dict = {}

VISION_PAYLOAD = {
    "scene_summary": "[STUB] 协议桩返回的场景摘要",
    "candidates": [
        {
            "name": "西湖",
            "address": "浙江省杭州市西湖区",
            "region": "杭州市",
            "rationale": "[STUB] 协议桩返回的视觉依据",
            "confidence": 0.72,
            "uncertainty": None,
        }
    ],
    "needs_user_confirmation": True,
}

CARD_PAYLOAD = {
    "title": "西湖 · 导游卡片",
    "summary": "[STUB] 协议桩返回的概述",
    "sections": [{"heading": "看点", "body": "[STUB] 协议桩返回的正文"}],
    "tips": ["[STUB] 协议桩返回的提示"],
}


class StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # 静默
        return

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - http.server 约定
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        # 地点检索现在走 POST + 表单体（密钥不进 URL），桩要同时支持。
        if self.path.startswith("/v3/place/text"):
            STATE["last_place_query"] = {k: v for k, v in parse_qs(raw).items()}
            self._send_place()
            return
        STATE["last_chat_body"] = raw
        STATE["last_auth_header_present"] = bool(self.headers.get("Authorization"))
        if STATE.get("sleep_seconds"):
            time.sleep(float(STATE["sleep_seconds"]))

        mode = STATE.get("chat_mode", "ok")
        if mode == "429":
            self._send(429, {"error": {"message": "rate limited"}})
            return
        if mode == "500":
            self._send(500, {"error": {"message": "server error"}})
            return
        if mode == "invalid_first" and STATE.get("chat_calls", 0) == 0:
            text = "这不是 JSON {"
        else:
            text = STATE.get("chat_text") or json.dumps(VISION_PAYLOAD, ensure_ascii=False)
        STATE["chat_calls"] = STATE.get("chat_calls", 0) + 1
        self._send(
            200,
            {
                "model": STATE.get("stub_model", "stub-model"),
                "choices": [{"message": {"content": text}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            },
        )

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        STATE["last_place_query"] = parse_qs(parsed.query)
        self._send_place()

    def _send_place(self) -> None:
        mode = STATE.get("place_mode", "ok")
        if mode == "timeout":
            time.sleep(float(STATE.get("sleep_seconds") or 2))
        if mode == "status0":
            self._send(200, {"status": "0", "info": "INVALID_USER_KEY", "infocode": "10001"})
            return
        if mode == "empty":
            self._send(200, {"status": "1", "pois": []})
            return
        if mode == "multi":
            # B4：同名多候选协议桩——旧实现会静默取 pois[0]（杭州），
            # 现在必须整批返回并交由判定逻辑处理。
            self._send(
                200,
                {
                    "status": "1",
                    "pois": [
                        {
                            "id": "B0FFMULTI01",
                            "name": "人民公园",
                            "address": "浙江省杭州市西湖区",
                            "location": "120.150000,30.250000",
                            "pname": "浙江省",
                            "cityname": "杭州市",
                            "adname": "西湖区",
                        },
                        {
                            "id": "B0FFMULTI02",
                            "name": "人民公园",
                            "address": "江苏省扬州市邗江区",
                            "location": "119.420000,32.390000",
                            "pname": "江苏省",
                            "cityname": "扬州市",
                            "adname": "邗江区",
                        },
                        {
                            "id": "B0FFMULTI03",
                            "name": "人民公园",
                            "address": "广东省惠州市惠城区",
                            "location": "114.410000,23.110000",
                            "pname": "广东省",
                            "cityname": "惠州市",
                            "adname": "惠城区",
                        },
                    ],
                },
            )
            return
        self._send(
            200,
            {
                "status": "1",
                "pois": [
                    {
                        "id": "B0FFSTUB01",
                        "name": "西湖",
                        "address": "浙江省杭州市西湖区龙井路1号",
                        "location": "120.150000,30.250000",
                        "pname": "浙江省",
                        "cityname": "杭州市",
                        "adname": "西湖区",
                        "typecode": "110000",
                        "adcode": "330106",
                        "citycode": "0571",
                    }
                ],
            },
        )


@pytest.fixture(scope="module")
def stub_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


@pytest.fixture(autouse=True)
def reset_state():
    STATE.clear()
    STATE["chat_mode"] = "ok"
    STATE["place_mode"] = "ok"
    STATE["chat_calls"] = 0
    STATE["chat_text"] = json.dumps(VISION_PAYLOAD, ensure_ascii=False)
    yield STATE
    STATE.clear()


def real_settings(settings: Settings, base_url: str, **overrides) -> Settings:
    values = {
        "app_env": "test",
        "database_url": settings.database_url,
        "redis_url": settings.redis_url,
        "session_secret": settings.session_secret,
        "media_root": settings.media_root,
        "provider_mode": "real",
        "vision_api_base_url": f"{base_url}/v1",
        "vision_api_key": "stub-key-not-a-real-credential",
        "vision_model": "stub-vl",
        "text_api_base_url": f"{base_url}/v1",
        "text_api_key": "stub-key-not-a-real-credential",
        "text_model": "stub-text",
        "text_thinking_mode": "qwen",
        "vision_thinking_mode": "qwen",
        "place_api_base_url": base_url,
        "place_api_key": "00000000000000000000000000000000",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_real_provider_set_requires_keys(settings):
    from app.config import ConfigError

    with pytest.raises(ConfigError) as excinfo:
        build_provider_set(
            Settings(
                _env_file=None,
                app_env="test",
                database_url=settings.database_url,
                session_secret=settings.session_secret,
                provider_mode="real",
                vision_api_key="",
                text_api_key="",
                place_api_key="",
            )
        )
    assert set(excinfo.value.names) == {"VISION_API_KEY", "TEXT_API_KEY", "PLACE_API_KEY"}


def test_vision_http_call_attaches_image_and_parses_usage(stub_server, settings):
    real = real_settings(settings, stub_server)
    providers = build_provider_set(real)

    outcome = tools.analyze_image(
        providers,
        real,
        image_bytes=make_image_bytes("PNG"),
        mime_type="image/png",
        city_hint="杭州",
    )

    assert outcome.ok is True, outcome.error_detail
    assert outcome.provider == "vision"
    assert outcome.data["candidates"][0]["name"] == "西湖"
    assert outcome.prompt_tokens == 11 and outcome.completion_tokens == 7
    # 请求体必须真的带上 base64 data URL 图片，而不是只发文本。
    assert "data:image/png;base64," in STATE["last_chat_body"]
    assert STATE["last_auth_header_present"] is True

    # 请求体契约：模型名来自配置、关闭思考模式、未开高分辨率时不发送该参数。
    body = json.loads(STATE["last_chat_body"])
    assert body["model"] == "stub-vl"
    assert body["enable_thinking"] is False
    assert body["response_format"] == {"type": "json_object"}
    assert "vl_high_resolution_images" not in body
    assert "max_pixels" not in body


def test_json_repair_loop_retries_once_then_succeeds(stub_server, settings):
    real = real_settings(settings, stub_server)
    providers = build_provider_set(real)
    STATE["chat_mode"] = "invalid_first"

    outcome = tools.analyze_image(
        providers,
        real,
        image_bytes=make_image_bytes("PNG"),
        mime_type="image/png",
        city_hint=None,
    )

    assert outcome.ok is True, outcome.error_detail
    assert outcome.calls == 2  # 第一次非法 JSON，修复后第二次通过
    assert outcome.prompt_tokens == 22  # 两次调用的用量都要计入预算


def test_invalid_json_after_all_repairs_fails_clearly(stub_server, settings):
    real = real_settings(settings, stub_server, run_json_repair_attempts=1)
    providers = build_provider_set(real)
    STATE["chat_mode"] = "invalid_always"
    STATE["chat_text"] = "still not json"

    outcome = tools.analyze_image(
        providers,
        real,
        image_bytes=make_image_bytes("PNG"),
        mime_type="image/png",
        city_hint=None,
    )

    assert outcome.ok is False
    assert outcome.error_code == "provider_invalid_response"
    assert outcome.calls == 0  # 失败路径不产出候选


def test_rate_limit_maps_to_provider_rate_limited(stub_server, settings):
    real = real_settings(settings, stub_server)
    providers = build_provider_set(real)
    STATE["chat_mode"] = "429"

    outcome = tools.generate_guide_card(
        providers,
        real,
        place_name="西湖",
        city_hint="杭州",
        place_facts={"provider": "amap", "address": "x"},
        scene_summary=None,
        visual_rationale=[],
    )
    assert outcome.ok is False
    assert outcome.error_code == "provider_rate_limited"
    assert outcome.retryable is True


def test_http_500_maps_to_provider_http_error(stub_server, settings):
    real = real_settings(settings, stub_server)
    providers = build_provider_set(real)
    STATE["chat_mode"] = "500"

    outcome = tools.analyze_image(
        providers, real, image_bytes=make_image_bytes("PNG"), mime_type="image/png", city_hint=None
    )
    assert outcome.ok is False
    assert outcome.error_code == "provider_http_error"
    assert outcome.response_status == 500


def test_timeout_maps_to_provider_timeout(stub_server, settings):
    real = real_settings(settings, stub_server, provider_timeout_seconds=0.4)
    providers = build_provider_set(real)
    STATE["sleep_seconds"] = 2.0

    outcome = tools.analyze_image(
        providers, real, image_bytes=make_image_bytes("PNG"), mime_type="image/png", city_hint=None
    )
    assert outcome.ok is False
    assert outcome.error_code == "provider_timeout"


def test_place_provider_parses_amap_poi(stub_server, settings):
    real = real_settings(settings, stub_server)
    providers = build_provider_set(real)

    outcome = tools.lookup_place(providers, real, place_name="西湖", city_hint="杭州")

    assert outcome.ok is True, outcome.error_detail
    place = outcome.data["place"]
    assert place["name"] == "西湖"
    assert place["latitude"] == pytest.approx(30.25)
    assert place["longitude"] == pytest.approx(120.15)
    assert place["provider"] == "amap"
    assert place["provider_place_id"] == "B0FFSTUB01"
    assert STATE["last_place_query"]["keywords"] == ["西湖"]


def test_place_provider_multi_same_name_is_not_auto_selected(stub_server, settings):
    """B4：协议层返回多条同名 POI 时，工具层不得把第一条当成已核实地点。"""
    real = real_settings(settings, stub_server)
    providers = build_provider_set(real)
    STATE["place_mode"] = "multi"

    outcome = tools.lookup_place(providers, real, place_name="人民公园", city_hint=None)

    assert outcome.ok is True, outcome.error_detail
    assert outcome.data["place"] is None, "歧义时不能返回任何供应商事实"
    match = outcome.data["match"]
    assert match["status"] == "ambiguous"
    assert match["reason"] == "multiple_same_name"
    assert [item["provider_place_id"] for item in match["candidates"]] == [
        "B0FFMULTI01",
        "B0FFMULTI02",
        "B0FFMULTI03",
    ]
    assert len(outcome.data["raw_candidates"]) == 3
    # 必须一次取回多候选，而不是 offset=1 只看第一条
    assert STATE["last_place_query"]["offset"] == ["5"]


def test_place_provider_single_exact_match_is_accepted(stub_server, settings):
    real = real_settings(settings, stub_server)
    providers = build_provider_set(real)

    outcome = tools.lookup_place(providers, real, place_name="西湖", city_hint="杭州")

    assert outcome.ok is True, outcome.error_detail
    assert outcome.data["match"]["status"] in {"matched", "matched_name_only"}
    assert outcome.data["place"]["provider_place_id"] == "B0FFSTUB01"
    assert outcome.data["raw_candidates"][0]["provider_place_id"] == "B0FFSTUB01"


def test_place_provider_amap_error_and_empty_result(stub_server, settings):
    real = real_settings(settings, stub_server)
    providers = build_provider_set(real)

    STATE["place_mode"] = "status0"
    outcome = tools.lookup_place(providers, real, place_name="西湖", city_hint=None)
    assert outcome.ok is False
    assert outcome.error_code == "provider_http_error"
    assert "10001" in (outcome.error_detail or "")  # 只保留 infocode，不回显请求 URL

    STATE["place_mode"] = "empty"
    outcome = tools.lookup_place(providers, real, place_name="不存在的地点", city_hint=None)
    assert outcome.ok is False
    assert outcome.error_code == "provider_no_result"
    assert outcome.retryable is False


def test_full_agent_run_with_real_http_providers(app, client, settings, session_factory, stub_server):
    """真实 Provider 类（HTTP）走完整状态机：WAITING_USER → 确认 → 卡片。"""
    real = real_settings(settings, stub_server)
    providers: ProviderSet = build_provider_set(real)

    project = create_project(client, title=unique_title("realpath"))
    upload = client.post(
        f"/api/v1/projects/{project['id']}/media",
        files={"file": ("real.png", make_image_bytes("PNG"), "image/png")},
        data={"start_analysis": "true"},
    )
    assert upload.status_code == 201
    run_id = upload.json()["run"]["id"]

    assert execute_run(session_factory, run_id, providers=providers, settings=real) == RunStatus.WAITING_USER

    with app.state.session_factory() as db:
        candidate = db.execute(select(PlaceCandidate).where(PlaceCandidate.run_id == run_id)).scalar_one()
        assert candidate.name == "西湖"

    client.post(f"/api/v1/runs/{run_id}/confirm-place", json={"decision": "confirm", "candidate_id": candidate.id})

    # 卡片步骤使用文本模型，桩要返回卡片结构。
    STATE["chat_text"] = json.dumps(CARD_PAYLOAD, ensure_ascii=False)
    assert execute_run(session_factory, run_id, providers=providers, settings=real) == RunStatus.SUCCEEDED

    with app.state.session_factory() as db:
        card = db.execute(select(GuideCard).where(GuideCard.media_asset_id == upload.json()["media"]["id"])).scalar_one()
        assert card.title == "西湖 · 导游卡片"
        assert card.model_name == "stub-model"
        # 供应商事实来自地图接口，模型讲解来自文本接口，两者分开保存。
        assert card.place_facts["provider"] == "amap"
        assert card.place_facts["provider_place_id"] == "B0FFSTUB01"
        run = db.get(AgentRun, run_id)
        assert run.status == RunStatus.SUCCEEDED
        assert run.used_tool_calls == 3

    snapshot = client.get(f"/api/v1/projects/{project['id']}").json()
    media_snapshot = snapshot["media"][0]
    assert media_snapshot["place"]["provider"] == "amap"
    assert media_snapshot["place"]["latitude"] == pytest.approx(30.25)
    assert media_snapshot["card"]["sections"][0]["heading"] == "看点"

def test_thinking_mode_sends_thinking_budget(stub_server, settings):
    """开启思考模式时必须显式下发 enable_thinking 与 thinking_budget。"""
    real = real_settings(settings, stub_server, vision_enable_thinking=True, thinking_budget=2048)
    providers = build_provider_set(real)

    outcome = tools.analyze_image(
        providers, real, image_bytes=make_image_bytes("PNG"), mime_type="image/png", city_hint=None
    )
    assert outcome.ok is True, outcome.error_detail
    body = json.loads(STATE["last_chat_body"])
    assert body["enable_thinking"] is True
    assert body["thinking_budget"] == 2048


def test_high_resolution_flag_is_forwarded(stub_server, settings):
    """高分辨率开关与 max_pixels 必须进入请求体（识别招牌小字时用）。"""
    hi = real_settings(settings, stub_server, vision_high_resolution_images=True)
    tools.analyze_image(
        build_provider_set(hi), hi, image_bytes=make_image_bytes("PNG"), mime_type="image/png", city_hint=None
    )
    body = json.loads(STATE["last_chat_body"])
    assert body["vl_high_resolution_images"] is True
    assert "max_pixels" not in body  # 高分辨率策略会忽略 max_pixels

    capped = real_settings(settings, stub_server, vision_max_pixels=8000000)
    tools.analyze_image(
        build_provider_set(capped), capped, image_bytes=make_image_bytes("PNG"), mime_type="image/png", city_hint=None
    )
    body = json.loads(STATE["last_chat_body"])
    assert body["max_pixels"] == 8000000
    assert "vl_high_resolution_images" not in body


def test_text_model_thinking_flag_is_forwarded(stub_server, settings):
    real = real_settings(settings, stub_server, text_enable_thinking=True)
    providers = build_provider_set(real)
    STATE["chat_text"] = json.dumps(CARD_PAYLOAD, ensure_ascii=False)

    outcome = tools.generate_guide_card(
        providers,
        real,
        place_name="西湖",
        city_hint="杭州",
        place_facts={"provider": "amap", "address": "x"},
        scene_summary=None,
        visual_rationale=[],
    )
    assert outcome.ok is True, outcome.error_detail
    body = json.loads(STATE["last_chat_body"])
    assert body["model"] == "stub-text"
    assert body["enable_thinking"] is True
