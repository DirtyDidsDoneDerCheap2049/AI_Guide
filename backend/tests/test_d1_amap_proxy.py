"""D1：高德 JS 安全代理的受限转发测试（本地协议桩，不调用真实高德）。

覆盖：jscode 追加与覆盖、白名单拒绝、未配置密钥、POST 体转发、路径映射。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.config import Settings

pytestmark = pytest.mark.integration

STATE: dict = {}


class StubAmap(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # 静默
        return

    def _handle(self) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        STATE["last"] = {
            "method": self.command,
            "path": parsed.path,
            "query": parse_qs(parsed.query),
            "body": body,
            "content_type": self.headers.get("content-type"),
        }
        payload = json.dumps({"status": "1", "info": "OK", "path": parsed.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _handle
    do_POST = _handle


@pytest.fixture(scope="module")
def stub():
    server = ThreadingHTTPServer(("127.0.0.1", 0), StubAmap)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


@pytest.fixture(autouse=True)
def reset_state():
    STATE.clear()
    yield


def make_app(settings: Settings, host: str, **overrides):
    values = {
        "app_env": "test",
        "database_url": settings.database_url,
        "redis_url": settings.redis_url,
        "session_secret": settings.session_secret,
        "provider_mode": "mock",
        "media_root": settings.media_root,
        "amap_js_security_code": "stub-jscode-secret",
        "amap_js_proxy_hosts": host,
        "amap_js_proxy_scheme": "http",
    }
    values.update(overrides)
    from app.main import create_app

    return create_app(Settings(_env_file=None, **values))


def test_proxy_appends_jscode_and_forwards(settings, stub):
    app = make_app(settings, stub)
    with TestClient(app) as client:
        response = client.get(f"/_AMapService/v3/place/text?host={stub}&keywords=westlake")
    assert response.status_code == 200, response.text
    assert response.json()["path"] == "/v3/place/text"
    seen = STATE["last"]
    assert seen["path"] == "/v3/place/text"
    assert seen["query"]["jscode"] == ["stub-jscode-secret"]
    assert seen["query"]["keywords"] == ["westlake"]


def test_client_supplied_jscode_is_overwritten(settings, stub):
    app = make_app(settings, stub)
    with TestClient(app) as client:
        client.get(f"/_AMapService/v3/place/text?host={stub}&jscode=attacker-value")
    assert STATE["last"]["query"]["jscode"] == ["stub-jscode-secret"]


def test_non_allowlisted_host_is_rejected(settings, stub):
    app = make_app(settings, stub)
    with TestClient(app) as client:
        response = client.get("/_AMapService/v3/place/text?host=evil.example.com")
    assert response.status_code == 403
    assert response.json()["info"] == "amap_proxy_rejected"
    assert STATE.get("last") is None  # 没有发生任何转发


def test_unknown_path_without_host_is_rejected(settings, stub):
    app = make_app(settings, stub)
    with TestClient(app) as client:
        response = client.get("/_AMapService/unknown/path")
    assert response.status_code == 403


def test_missing_security_code_returns_503(settings, stub):
    app = make_app(settings, stub, amap_js_security_code="")
    with TestClient(app) as client:
        response = client.get(f"/_AMapService/v3/place/text?host={stub}")
    assert response.status_code == 503
    assert response.json()["info"] == "amap_proxy_not_configured"
    assert STATE.get("last") is None


def test_post_body_is_forwarded(settings, stub):
    app = make_app(settings, stub)
    with TestClient(app) as client:
        response = client.post(
            f"/_AMapService/v3/place/text?host={stub}",
            content="keywords=westlake",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
    assert response.status_code == 200
    assert STATE["last"]["method"] == "POST"
    assert STATE["last"]["body"] == "keywords=westlake"
