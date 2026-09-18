#!/usr/bin/env bash
#
# AI-Guide v0.1 Compose 静态检查（不需要 Docker，可离线运行）
#
#   用法: deploy/check-compose.sh [compose 文件] [env 模板]
#   默认: docker-compose.yml 与 deploy/.env.production.example
#
# 检查项:
#   1. 必需服务存在: web / api / worker / mysql / redis / migrate
#   2. 除 web 外没有任何服务声明 ports:
#   3. 每个 image 都有明确 tag，且不是 latest（禁止浮动标签）
#   4. compose 中引用的每个 ${VAR...} 都在 env 模板里定义
#   5. 文件里不出现 "latest" 标签
#
# 本脚本只做静态检查，不代替 docker compose config / build / up。
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

COMPOSE_FILE="${1:-${REPO_ROOT}/docker-compose.yml}"
ENV_TEMPLATE="${2:-${SCRIPT_DIR}/.env.production.example}"

failures=0
pass() { printf '  [PASS] %s\n' "$*"; }
fail() { printf '  [FAIL] %s\n' "$*" >&2; failures=$((failures + 1)); }

[[ -f "${COMPOSE_FILE}" ]] || { printf '[check][error] 找不到 %s\n' "${COMPOSE_FILE}" >&2; exit 2; }
[[ -f "${ENV_TEMPLATE}" ]] || { printf '[check][error] 找不到 %s\n' "${ENV_TEMPLATE}" >&2; exit 2; }

printf '[check] compose: %s\n' "${COMPOSE_FILE}"
printf '[check] env 模板: %s\n' "${ENV_TEMPLATE}"

# --- 解析：服务名 / 端口声明 / 镜像 -----------------------------------------
# 输出格式（用 | 分隔，避免镜像值里的空格破坏解析）:
#   SERVICE|api
#   PORTS|web
#   IMAGE|api|ghcr.io/owner/ai-guide-api:0.1.0
structure="$(
  awk '
    /^services:[[:space:]]*$/ { in_services = 1; next }
    in_services && /^[^[:space:]]/ { in_services = 0 }
    in_services && /^  [A-Za-z0-9_.-]+:[[:space:]]*$/ {
      svc = $1; sub(/:$/, "", svc); print "SERVICE|" svc; next
    }
    in_services && svc != "" && /^[[:space:]]+ports:/ { print "PORTS|" svc }
    in_services && /^[[:space:]]*image:/ {
      value = $0
      sub(/^[[:space:]]*image:[[:space:]]*/, "", value)
      gsub(/["'\'']/, "", value)
      print "IMAGE|" svc "|" value
    }
  ' "${COMPOSE_FILE}"
)"

# --- 1. 必需服务 -------------------------------------------------------------
for required in web api worker mysql redis migrate; do
  if printf '%s\n' "${structure}" | grep -qx "SERVICE|${required}"; then
    pass "服务存在: ${required}"
  else
    fail "缺少服务: ${required}"
  fi
done

# --- 2. 端口只允许出现在 web ------------------------------------------------
ports_services="$(printf '%s\n' "${structure}" | awk -F'|' '/^PORTS\|/{print $2}' | sort -u)"
if [[ -z "${ports_services}" ]]; then
  fail "没有任何服务声明 ports:；web 必须发布 'WEB_HTTP_PORT:80'"
elif [[ "${ports_services}" == "web" ]]; then
  pass "只有 web 声明了 ports:"
else
  while IFS= read -r svc; do
    [[ -n "${svc}" ]] || continue
    if [[ "${svc}" == "web" ]]; then
      pass "web 声明了 ports:（允许）"
    else
      fail "服务 ${svc} 不应对外发布端口（只允许 web）"
    fi
  done <<<"${ports_services}"
fi

# --- 3. 镜像必须有固定 tag 且不能是 latest ----------------------------------
image_count=0
while IFS='|' read -r kind svc image; do
  [[ "${kind}" == "IMAGE" ]] || continue
  [[ -n "${image:-}" ]] || continue
  image_count=$((image_count + 1))
  last_segment="${image##*/}"
  if [[ "${last_segment}" != *:* ]]; then
    fail "${svc}: image 没有 tag（${image}）"
    continue
  fi
  tag="${last_segment#*:}"
  if [[ -z "${tag}" ]]; then
    fail "${svc}: image tag 为空（${image}）"
  elif [[ "${tag}" == "latest" ]]; then
    fail "${svc}: image 使用了 latest（${image}）"
  elif [[ "${tag}" == *'${'* ]]; then
    # 变量插值的 tag（例如 ${APP_VERSION}）: 由 .env 显式固定，不是浮动标签。
    pass "${svc}: image tag 由 .env 变量插值固定（${tag}）"
  else
    pass "${svc}: image tag = ${tag}"
  fi
done <<<"${structure}"

if (( image_count == 0 )); then
  fail "没有解析到任何 image:（compose 结构可能已变化）"
fi

# --- 4. compose 引用到的变量必须在 env 模板里定义 ---------------------------
referenced="$(grep -oE '\$\{[A-Za-z_][A-Za-z0-9_]*' "${COMPOSE_FILE}" | sed 's/^\${//' | sort -u)"
declared="$(grep -oE '^[A-Za-z_][A-Za-z0-9_]*=' "${ENV_TEMPLATE}" | sed 's/=$//' | sort -u)"
referenced_count="$(printf '%s\n' "${referenced}" | grep -c . || true)"
missing="$(comm -23 <(printf '%s\n' "${referenced}") <(printf '%s\n' "${declared}") || true)"
if [[ -z "${missing}" ]]; then
  pass "compose 引用的 ${referenced_count} 个变量都在 env 模板中定义"
else
  while IFS= read -r var; do
    [[ -n "${var}" ]] || continue
    fail "env 模板缺少 compose 引用的变量: ${var}"
  done <<<"${missing}"
fi

# --- 5. 全局禁止 latest（只看非注释行）---------------------------------------
latest_hits="$(grep -n 'latest' "${COMPOSE_FILE}" | grep -vE '^[0-9]+:[[:space:]]*#' || true)"
if [[ -n "${latest_hits}" ]]; then
  fail "compose 文件出现 latest:"
  printf '%s\n' "${latest_hits}" >&2
else
  pass "compose 文件中没有 latest 标签（注释除外）"
fi

# --- 6. 锚点合并后的服务语义（可选，需要 PyYAML）-----------------------------
# compose 里的 x-backend-env 用 YAML 锚点合并进 api/worker/migrate；
# 这里用 PyYAML 的合并语义还原结果，确认容器内固定项、卷和依赖关系没有被写漏。
PY_BIN=""
for candidate in python3 python; do
  if command -v "${candidate}" >/dev/null 2>&1 && "${candidate}" -c 'import yaml' >/dev/null 2>&1; then
    PY_BIN="${candidate}"
    break
  fi
done

if [[ -z "${PY_BIN}" ]]; then
  printf '  [SKIP] 没有可用的 python + PyYAML，跳过锚点合并语义检查（请在有 PyYAML 的环境重跑）\n'
else
  if "${PY_BIN}" - "${COMPOSE_FILE}" <<'PY'
import sys

import yaml

MEDIA_ROOT = "/var/lib/ai-guide/media"
WORKER_CMD = ["python", "-m", "app.worker.cli", "worker", "--processes", "2", "--threads", "4"]

with open(sys.argv[1], encoding="utf-8") as handle:
    doc = yaml.safe_load(handle)

services = doc["services"]
problems: list[str] = []

for name in ("api", "worker", "migrate"):
    service = services[name]
    env = service.get("environment") or {}
    if env.get("MEDIA_ROOT") != MEDIA_ROOT:
        problems.append(f"{name}: MEDIA_ROOT 不是 {MEDIA_ROOT}")
    if not any(str(item).startswith("media-data:") for item in (service.get("volumes") or [])):
        problems.append(f"{name}: 缺少 media-data 卷挂载")
    if not service.get("env_file"):
        problems.append(f"{name}: 缺少 env_file（.env 注入）")
    deps = service.get("depends_on") or {}
    if (deps.get("mysql") or {}).get("condition") != "service_healthy":
        problems.append(f"{name}: 未依赖 mysql service_healthy")
    if (deps.get("redis") or {}).get("condition") != "service_healthy":
        problems.append(f"{name}: 未依赖 redis service_healthy")

for name in ("api", "worker"):
    deps = services[name].get("depends_on") or {}
    if (deps.get("migrate") or {}).get("condition") != "service_completed_successfully":
        problems.append(f"{name}: 未依赖 migrate service_completed_successfully")

if services["migrate"].get("restart") != "no":
    problems.append("migrate: restart 必须是 \"no\"")
if services["migrate"].get("command") != ["python", "-m", "alembic", "upgrade", "head"]:
    problems.append("migrate: 命令必须是 python -m alembic upgrade head")
if services["worker"].get("command") != WORKER_CMD:
    problems.append("worker: 启动命令与要求不一致")
if services["worker"].get("ports"):
    problems.append("worker: 不应有 ports")
if services["api"].get("command"):
    problems.append("api: 不应覆盖镜像 CMD（生产启动方式由 Dockerfile 提供）")
if services["api"].get("healthcheck", {}).get("test", [None])[-1].find("/api/health/ready") < 0:
    problems.append("api: healthcheck 未指向 /api/health/ready")
if (services["web"].get("depends_on") or {}).get("api", {}).get("condition") not in (
    "service_healthy",
    "service_started",
):
    problems.append("web: 未依赖 api")
expected_ports = ["${WEB_HTTP_PORT:-80}:80", "${WEB_HTTPS_PORT:-443}:443"]
if services["web"].get("ports") != expected_ports:
    problems.append(f"web: 端口映射不是 {expected_ports}（B8：TLS 需要 443 映射）")
if (services["web"].get("environment") or {}).get("SITE_ADDRESS") != "${SITE_ADDRESS:-:80}":
    problems.append("web: 缺少 SITE_ADDRESS（决定 Caddy 是否自动申请证书）")
for caddy_volume in ("caddy-data:/data", "caddy-config:/config"):
    if caddy_volume not in (services["web"].get("volumes") or []):
        problems.append(f"web: 缺少证书持久化卷映射 {caddy_volume}")
for required in ("mysql-data", "redis-data", "media-data", "caddy-data", "caddy-config"):
    if required not in (doc.get("volumes") or {}):
        problems.append(f"缺少命名卷: {required}")

for item in problems:
    print(f"  [FAIL] {item}")
sys.exit(1 if problems else 0)
PY
  then
    pass "锚点合并语义正确（容器内 MEDIA_ROOT、media-data 卷、migrate 依赖链、worker 命令、web 端口与 TLS 证书卷）"
  else
    failures=$((failures + 1))
  fi
fi

if (( failures == 0 )); then
  printf '\n[check] 结果: 全部通过\n'
else
  printf '\n[check] 结果: %s 项失败\n' "${failures}"
fi
(( failures == 0 ))
