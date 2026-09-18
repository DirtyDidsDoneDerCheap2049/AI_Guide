#!/usr/bin/env python3
"""对比根 .env.example 与 deploy/.env.production.example 的变量名集合。

用法（仓库根目录执行）:
    python deploy/check-env-parity.py
    python deploy/check-env-parity.py --root .

判定规则:
    - 只比较「未被注释的赋值行」``NAME=``，不比较值。
    - 根 .env.example 中出现的每个变量都必须在生产模板中出现（缺失即失败）。
    - 生产模板中额外的变量视为部署项（Compose/脚本使用，应用不读取），只报告不失败。
    - 两边的值一律不打印，避免把任何疑似密钥打进日志。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")

LOCAL_TEMPLATE = Path(".env.example")
PRODUCTION_TEMPLATE = Path("deploy/.env.production.example")

# 生产模板允许额外存在的部署项（Compose / 脚本使用，应用不读取）。
ALLOWED_EXTRA = {
    "IMAGE_REGISTRY",
    "WEB_HTTP_PORT",
    "MYSQL_DATABASE",
    "MYSQL_USER",
    "MYSQL_PASSWORD",
    "MYSQL_ROOT_PASSWORD",
}


def names(path: Path) -> list[str]:
    found: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = ASSIGNMENT.match(line)
        if match and match.group(1) not in found:
            found.append(match.group(1))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description="检查两个环境变量模板的变量名一致性")
    parser.add_argument("--root", default=".", help="仓库根目录，默认当前目录")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    local_path = root / LOCAL_TEMPLATE
    prod_path = root / PRODUCTION_TEMPLATE

    for path in (local_path, prod_path):
        if not path.is_file():
            print(f"[env-parity][error] 找不到 {path}", file=sys.stderr)
            return 2

    local_names = names(local_path)
    prod_names = names(prod_path)
    local_set = set(local_names)
    prod_set = set(prod_names)

    missing = sorted(local_set - prod_set)
    extra = sorted(prod_set - local_set)
    unexpected_extra = sorted(set(extra) - ALLOWED_EXTRA)

    print(f"[env-parity] {LOCAL_TEMPLATE}: {len(local_names)} 个变量")
    print(f"[env-parity] {PRODUCTION_TEMPLATE}: {len(prod_names)} 个变量")

    if missing:
        print(f"[env-parity][FAIL] 生产模板缺少 {len(missing)} 个变量: {', '.join(missing)}")
    else:
        print(f"[env-parity][PASS] 根 .env.example 的 {len(local_names)} 个变量在生产模板中全部存在")

    if extra:
        print(f"[env-parity][INFO] 生产模板额外变量 {len(extra)} 个: {', '.join(extra)}")
    if unexpected_extra:
        print(
            "[env-parity][WARN] 以下额外变量不在已知部署项清单中，请确认是否真的需要: "
            + ", ".join(unexpected_extra)
        )

    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
