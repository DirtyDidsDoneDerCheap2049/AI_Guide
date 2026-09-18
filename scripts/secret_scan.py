"""凭据与硬编码主机扫描器（脱敏输出）。

规则：只报告 文件路径 / 行号 / 规则类型 / 掩码长度，绝不输出、记录或回显匹配到的值。
用法：
    python scripts/secret_scan.py --root . --json work/logs/secret-scan.json
    python scripts/secret_scan.py --view frontend/src/services/api.ts   # 掩码预览，便于安全地改写
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

SKIP_DIRS = {
    ".git",
    "work",  # 本地脚本/日志/虚拟环境/本地 MySQL·Redis 数据，按 .gitignore 永不提交
    "node_modules",
    "__pycache__",
    ".venv",
    "dist",
    ".mysql",
    ".redis",
    "logs",
}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".db", ".zip", ".pdf", ".woff", ".woff2", ".ttf"}

# (规则名, 正则, 是否需要值为引号内字符串)
RULES: list[tuple[str, re.Pattern[str]]] = [
    ("env_secret", re.compile(r"(?m)^\s*(?:[A-Z0-9_]*(?:API_KEY|SECURITY_CODE|PASSWORD|SECRET|ACCESS_TOKEN))\s*=\s*['\"]?([^\s#'\"]{8,})")),
    ("openai_like_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}")),
    ("url_with_key_param", re.compile(r"https?://[^\s'\"`]*[?&](?:key|api_?key|token|access_?token)=[A-Za-z0-9._\-]{8,}")),
    ("credential_url", re.compile(r"[a-z][a-z0-9+.\-]*://[^\s:/'\"@]+:[^\s:/'\"@]+@[^\s'\"`]+")),
    (
        "assigned_secret_literal",
        re.compile(
            r"(?i)\b(api[_-]?key|apikey|secret[_-]?key|secret|access[_-]?token|auth[_-]?token|token|password|passwd|pwd)"
            r"\b\s*[:=]\s*(['\"])([^'\"\n]{8,})\2"
        ),
    ),
    ("hex32_literal", re.compile(r"\b[0-9a-f]{32}\b")),
    ("hardcoded_host", re.compile(r"https?://(?:localhost|127\.0\.0\.1|\d{1,3}(?:\.\d{1,3}){3}|[a-z0-9.\-]+\.(?:com|cn|net|org|io|top|xyz))\b")),
]

# 允许出现的公开/占位主机（文档与示例中的说明性地址）
ALLOW_HOST_SUBSTRINGS = (
    "example.com",
    "example.org",
    "localhost:8000",
    "localhost:5173",
    "127.0.0.1:8000",
    "127.0.0.1:5173",
    "127.0.0.1:3307",
    "127.0.0.1:6380",
    "github.com",
    "ghcr.io",
    "pypi.org",
    "registry.npmjs.org",
    "nodejs.org",
    "www.w3.org",
    "amap.com",
    "restapi.amap.com",
    "openai.com",
    "api.deepseek.com",
    "dashscope.aliyuncs.com",
    "docs.",
)


PLACEHOLDER_MARKERS = (
    "change_me", "changeme", "placeholder", "example", "dummy", "fake", "sample",
    "your_", "user:pass", "root:root", "***", "<", ">", "${",
    "test-only", "test_only", "test-secret", "ci-test", "ci_test", "local-dev",
)


def looks_like_placeholder(text: str) -> bool:
    """示例值 / CI 一次性口令 / 模板变量不算凭据泄露，但仍会出现在报告里（标注 placeholder）。"""
    low = text.lower()
    return any(marker in low for marker in PLACEHOLDER_MARKERS)


def mask(value: str) -> str:
    return f"***masked(len={len(value)})***"


SKIP_FILE_NAMES = {".env"}
ENV_TEMPLATES = {".env.example", "deploy/.env.production.example"}


def forbidden_public_path(name: str) -> bool:
    """即使被强制添加，实际配置、私钥、数据与内部资料也不允许提交。"""
    path = Path(name)
    return (
        (path.name.startswith(".env") and name not in ENV_TEMPLATES)
        or path.suffix.lower() in {".pem", ".key", ".p12", ".pfx", ".p8", ".db", ".sqlite", ".sqlite3", ".har", ".dump", ".bak"}
        or name.endswith((".sql.gz", ".tar.gz", ".zip"))
        or path.name in {"id_rsa", "id_ed25519"}
        or name.startswith(("work/", "legacy/", "logs/", "backups/", "uploads/", "backend/var/", "docs/productization/", "docs/legacy/", "docs/ai-guide-visuals/", "docs/assets/desktop-guide/"))
        or name in {"start-local.ps1", "docs/AI-Guide产品化改造方案.md", "docs/CODEX-REVIEW-CHECKLIST.md", "docs/RELEASE-CHECKLIST.md", "deploy/LOCAL-DRY-RUN.md"}
    )


def git_paths(root: Path, mode: str) -> list[str]:
    args = ["git", "-C", str(root)]
    if mode == "staged":
        args += ["diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"]
    else:
        args += ["ls-files", "--cached"]
        if mode == "worktree":
            args += ["--others", "--exclude-standard"]
        args += ["-z"]
    result = subprocess.run(args, capture_output=True, check=True)
    return sorted(set(name for name in result.stdout.decode("utf-8").split("\0") if name))


def iter_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        # 跳过 .env / .env.local 等本地环境文件（CI 里另有"禁止提交 .env"的显式检查）
        name = path.name
        if name.startswith(".env") and path.relative_to(root).as_posix() not in ENV_TEMPLATES:
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        yield path


def scan_file(path: Path, root: Path) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return scan_text(text, path.relative_to(root).as_posix())


def scan_text(text: str, name: str) -> list[dict]:
    findings: list[dict] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        for rule_name, pattern in RULES:
            if rule_name == "env_secret" and not (Path(name).name.startswith(".env") or Path(name).suffix in {".yml", ".yaml", ".sh"}):
                continue
            for match in pattern.finditer(line):
                value = match.group(0)
                if rule_name == "assigned_secret_literal":
                    value = match.group(3)
                if rule_name == "env_secret":
                    value = match.group(1)
                if rule_name == "hex32_literal" and not re.search(r"(?i)(key|secret|token|id)\s*[:=]", line):
                    continue
                if rule_name == "hardcoded_host":
                    if any(allowed in value for allowed in ALLOW_HOST_SUBSTRINGS):
                        continue
                    if stripped.startswith(("#", "//", "*", "<!--")) and "http" in stripped:
                        # 注释中的说明性链接仍然记录，但不计为泄漏密钥
                        findings.append(
                            {
                                "rule": "host_in_comment",
                                "file": name,
                                "line": lineno,
                                "masked": mask(value),
                            }
                        )
                        continue
                    if "localhost" in value or "127.0.0.1" in value:
                        rule_name = "localhost_reference"
                findings.append(
                    {
                        "rule": rule_name,
                        "file": name,
                        "line": lineno,
                        "masked": mask(value),
                        "placeholder": looks_like_placeholder(value),
                    }
                )
    return findings


def view_masked(path: Path) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    text = path.read_text(encoding="utf-8", errors="replace")
    out_lines = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        masked_line = line
        for rule_name, pattern in RULES:
            for match in pattern.finditer(line):
                value = match.group(3) if (rule_name == "assigned_secret_literal" and match.lastindex == 3) else match.group(0)
                masked_line = masked_line.replace(value, mask(value))
        out_lines.append(f"{lineno:5d}| {masked_line}")
    sys.stdout.write("\n".join(out_lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--json", dest="json_path", default=None)
    parser.add_argument("--view", default=None, help="打印指定文件的掩码版本")
    parser.add_argument(
        "--staged",
        action="store_true",
        help="只扫描暂存区文件（提交前钩子用；比扫描整个工作区更精确）",
    )
    parser.add_argument("--quiet", action="store_true", help="只输出统计")
    parser.add_argument("--tracked", action="store_true", help="扫描 Git 跟踪文件（CI）")
    parser.add_argument("--git-worktree", action="store_true", help="扫描跟踪文件和未忽略的待提交文件")
    parser.add_argument(
        "--fail-on-credentials",
        action="store_true",
        help="出现凭据类命中时以非零退出（供 CI / 提交前钩子使用）",
    )
    args = parser.parse_args()

    if args.view:
        view_masked(Path(args.view))
        return 0

    root = Path(args.root).resolve()
    findings: list[dict] = []
    scanned = 0
    if sum((args.staged, args.tracked, args.git_worktree)) > 1:
        parser.error("choose only one Git scan mode")
    git_mode = "staged" if args.staged else "tracked" if args.tracked else "worktree" if args.git_worktree else None
    if git_mode:
        targets = [root / name for name in git_paths(root, git_mode)]
    else:
        targets = list(iter_files(root))

    for path in targets:
        name = path.relative_to(root).as_posix()
        if git_mode and forbidden_public_path(name):
            findings.append({"rule": "forbidden_public_file", "file": name, "line": 0})
        if path.is_symlink():
            findings.append({"rule": "symlink", "file": name, "line": 0})
            continue
        if args.staged:
            result = subprocess.run(["git", "-C", str(root), "show", f":{name}"], capture_output=True, check=True)
            content = result.stdout
        elif path.is_file():
            content = path.read_bytes()
        else:
            continue
        scanned += 1
        if path.suffix.lower() not in SKIP_SUFFIXES and b"\x00" not in content:
            findings.extend(scan_text(content.decode("utf-8", "replace"), name))

    counts: dict[str, int] = {}
    for item in findings:
        counts[item["rule"]] = counts.get(item["rule"], 0) + 1

    if not args.quiet:
        print(f"scanned files: {scanned}")
        print(f"findings: {len(findings)}")
        for rule in sorted(counts):
            print(f"  {rule}: {counts[rule]}")
        if findings:
            print("\n# 明细（值已遮盖）")
            for item in findings:
                print(f"  [{item['rule']}] {item['file']}:{item['line']}")

    credential_rules = {
        "env_secret",
        "forbidden_public_file",
        "symlink",
        "openai_like_key",
        "aws_access_key",
        "github_token",
        "private_key_block",
        "bearer_token",
        "url_with_key_param",
        "credential_url",
        "assigned_secret_literal",
        "hex32_literal",
    }
    credential_hits = [
        item
        for item in findings
        if item["rule"] in credential_rules and not item.get("placeholder", False)
    ]
    placeholder_count = sum(1 for item in findings if item.get("placeholder", False))
    if args.fail_on_credentials and credential_hits:
        print(f"[secret-scan] FAIL: {len(credential_hits)} credential-like finding(s); values are never printed.")
        for item in credential_hits[:20]:
            print(f"  [{item['rule']}] {item['file']}:{item['line']}")
        print(f"[secret-scan] 另有 {placeholder_count} 处示例/占位值命中（已忽略）")
        if args.json_path:
            out = Path(args.json_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(
                json.dumps(
                    {"scanned_files": scanned, "counts": counts, "findings": findings},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(f"report written: {out}")
        return 1

    if args.json_path:
        out = Path(args.json_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps({"scanned_files": scanned, "counts": counts, "findings": findings}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"report written: {out}")
    if args.quiet:
        print(f"[secret-scan] PASS: {scanned} files; no unreviewed credential matches.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
