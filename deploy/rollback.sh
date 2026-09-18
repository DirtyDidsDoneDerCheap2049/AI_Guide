#!/usr/bin/env bash
#
# AI-Guide v0.1 回退脚本
#
#   用法: deploy/rollback.sh --version 0.0.9 [--dry-run]
#
# 做的事: 备份 .env -> 把 APP_VERSION 改回指定标签 -> 拉取镜像 -> up -d -> 健康检查。
#
# 明确不做的事（脚本会把它打印出来，不要对外宣称自动化）:
#   - 不会自动回退数据库。Alembic 迁移需要人工判断：如果目标版本没有对应的 downgrade 路径，
#     必须用备份文件恢复 MySQL（backups/mysql-<时间戳>.sql.gz）。
#   - 不会回退 Redis。Redis 只承载队列，不是业务数据的事实来源。
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

COMPOSE_FILE="${COMPOSE_FILE:-${REPO_ROOT}/docker-compose.yml}"
ENV_FILE="${ENV_FILE:-${REPO_ROOT}/.env}"
BACKUP_DIR="${BACKUP_DIR:-${REPO_ROOT}/backups}"
SERVICES=(mysql redis api worker web)
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"
HEALTH_INTERVAL="${HEALTH_INTERVAL:-5}"
WEB_HTTP_PORT="${WEB_HTTP_PORT:-80}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:${WEB_HTTP_PORT}/api/health/ready}"

VERSION=""
DRY_RUN=0

log()  { printf '[rollback] %s\n' "$*"; }
warn() { printf '[rollback][warn] %s\n' "$*" >&2; }
die()  { printf '[rollback][error] %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
用法: deploy/rollback.sh --version <tag> [--dry-run]

  --version <tag>   必填。要回退到的历史发布标签（例如 0.0.9）。
  --dry-run         只打印将要执行的命令，不修改 .env、不调用 Docker。
  -h, --help        显示本帮助。
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)   [[ $# -ge 2 ]] || die "--version 需要一个参数"; VERSION="$2"; shift 2 ;;
    --version=*) VERSION="${1#*=}"; shift ;;
    --dry-run)   DRY_RUN=1; shift ;;
    -h|--help)   usage; exit 0 ;;
    *)           usage; die "未知参数: $1" ;;
  esac
done

[[ -n "${VERSION}" ]] || { usage; die "缺少必填参数 --version <tag>"; }
[[ -f "${COMPOSE_FILE}" ]] || die "找不到 compose 文件: ${COMPOSE_FILE}"
[[ -f "${ENV_FILE}" ]] || die "找不到环境文件: ${ENV_FILE}"

compose() { docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" "$@"; }

run() {
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    printf '[dry-run] %s\n' "$*"
    return 0
  fi
  printf '+ %s\n' "$*"
  "$@"
}

require_docker() {
  command -v docker >/dev/null 2>&1 || die "未找到 docker 命令；本脚本必须在具备 Docker Compose v2 的服务器上执行"
  docker compose version >/dev/null 2>&1 || die "docker compose (v2) 不可用"
}

current_version() {
  # 只取第一个非空白、非注释字符序列，兼容 "APP_VERSION=0.1.0  # 说明" 这种行内注释。
  sed -n 's/^APP_VERSION=[[:space:]]*\([^[:space:]#]*\).*$/\1/p' "${ENV_FILE}" | tail -n 1
}

set_env_var() {
  local key="$1" value="$2" file="$3"
  if grep -qE "^${key}=" "${file}"; then
    sed -i -E "s|^${key}=.*$|${key}=${value}|" "${file}"
  else
    printf '%s=%s\n' "${key}" "${value}" >>"${file}"
  fi
}

probe_health() {
  if command -v curl >/dev/null 2>&1; then
    curl -fsS -o /dev/null --max-time 5 "${HEALTH_URL}"
    return $?
  fi
  compose exec -T api python -c \
    "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health/ready', timeout=3).status == 200 else 1)"
}

wait_healthy() {
  local deadline=$(( SECONDS + HEALTH_TIMEOUT ))
  log "轮询健康检查: ${HEALTH_URL}（超时 ${HEALTH_TIMEOUT}s）"
  while true; do
    if probe_health; then
      log "健康检查通过"
      return 0
    fi
    if (( SECONDS >= deadline )); then
      warn "健康检查在 ${HEALTH_TIMEOUT}s 内未通过；请查看 compose logs api migrate"
      return 1
    fi
    sleep "${HEALTH_INTERVAL}"
  done
}

print_manual_db_notice() {
  cat >&2 <<EOF

[rollback][重要] 数据库没有自动回退，必须人工判断：
  1. 镜像回退不会撤销 Alembic 迁移。旧版代码遇到新版 schema 时，可能仍然可用，也可能直接失败。
  2. 先确认要回退的版本对应的迁移版本: docker compose --env-file ${ENV_FILE} -f ${COMPOSE_FILE} run --rm migrate python -m alembic history
  3. 只有在确认该版本支持时才执行降级:
       docker compose --env-file ${ENV_FILE} -f ${COMPOSE_FILE} run --rm migrate python -m alembic downgrade <revision>
  4. 数据已被破坏或降级不可行时，用备份恢复（备份目录: ${BACKUP_DIR}）:
       ${SCRIPT_DIR}/restore.sh --mysql ${BACKUP_DIR}/mysql-<UTC时间戳>.sql.gz
  5. Redis 不是业务数据的事实来源，不需要（也不能）用它做数据回退。

EOF
}

main() {
  local previous_version
  previous_version="$(current_version || true)"

  log "目标回退版本: ${VERSION}"
  log "当前版本: ${previous_version:-<未设置>}"

  if [[ "${DRY_RUN}" -eq 0 ]]; then
    require_docker
    [[ "${VERSION}" != "latest" ]] || die "禁止回退到 latest；请使用具体的语义版本标签"
  fi

  local backup_env="${SCRIPT_DIR}/.env.bak.$(date -u +%Y%m%dT%H%M%SZ)"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    printf '[dry-run] cp %s %s\n' "${ENV_FILE}" "${backup_env}"
    printf '[dry-run] set_env_var APP_VERSION=%s in %s\n' "${VERSION}" "${ENV_FILE}"
  else
    cp "${ENV_FILE}" "${backup_env}"
    log "已备份 .env 到 ${backup_env}"
    set_env_var APP_VERSION "${VERSION}" "${ENV_FILE}"
    log "APP_VERSION 已改为 ${VERSION}"
  fi

  print_manual_db_notice

  run compose pull "${SERVICES[@]}"
  run compose up -d

  if [[ "${DRY_RUN}" -eq 1 ]]; then
    printf '[dry-run] wait_healthy (%s)\n' "${HEALTH_URL}"
    log "dry-run 完成：没有修改 .env，也没有调用 Docker"
    return 0
  fi

  if ! wait_healthy; then
    warn "回退后健康检查仍未通过。上一个版本: ${previous_version:-未知}"
    warn "请检查 Provider 配置、迁移版本和 compose logs；必要时用 ${SCRIPT_DIR}/restore.sh 恢复备份。"
    return 1
  fi

  log "回退完成，当前版本: $(current_version)"
  compose ps
  print_manual_db_notice
}

main "$@"
