"""S0：仓库侧脱敏与前端同源约定检查（不依赖运行中的服务）。"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

SECRET_PATTERNS = [
    ("openai_like_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("credential_url", re.compile(r"[a-z][a-z0-9+.\-]*://[^\s:/'\"@]+:[^\s:/'\"@]+@")),
    (
        "assigned_secret_literal",
        re.compile(
            r"(?i)\b(api[_-]?key|apikey|secret[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|pwd)"
            r"\b\s*[:=]\s*(['\"])([^'\"\n]{8,})\2"
        ),
    ),
]

SCAN_TARGETS = [
    REPO_ROOT / "legacy" / "frontend" / "src",
    REPO_ROOT / "backend" / "app",
    REPO_ROOT / "web" / "src",
]


def _iter_source_files():
    for target in SCAN_TARGETS:
        if not target.exists():
            continue
        for path in target.rglob("*"):
            if path.is_file() and path.suffix.lower() in {".ts", ".js", ".vue", ".py", ".json", ".css", ".html"}:
                yield path


def test_no_credentials_in_frontend_or_backend_source():
    findings: list[str] = []
    for path in _iter_source_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for rule, pattern in SECRET_PATTERNS:
                match = pattern.search(line)
                if match:
                    # 只报告位置与规则类型，绝不输出匹配到的值。
                    findings.append(f"{path.relative_to(REPO_ROOT)}:{lineno}:{rule}")
    assert findings == []


def test_frontend_api_service_uses_relative_base_url_only():
    api_service = REPO_ROOT / "legacy" / "frontend" / "src" / "services" / "api.ts"
    if not api_service.exists():
        return
    text = api_service.read_text(encoding="utf-8")
    assert 'baseURL: process.env.VUE_APP_API_BASE_URL || ""' in text
    for forbidden in ("http://localhost", "http://127.0.0.1", "https://restapi.amap.com"):
        assert forbidden not in text, forbidden


def test_new_workbench_bundle_has_no_hardcoded_hosts():
    """新的生产工作台（web/）源码中不允许出现绝对主机地址。"""
    web_src = REPO_ROOT / "web" / "src"
    if not web_src.exists():
        return
    offenders: list[str] = []
    pattern = re.compile(r"https?://(?!www\.w3\.org)")
    for path in web_src.rglob("*"):
        if path.is_file() and path.suffix.lower() in {".ts", ".vue", ".js", ".css", ".html"}:
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if pattern.search(line):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
    assert offenders == []
