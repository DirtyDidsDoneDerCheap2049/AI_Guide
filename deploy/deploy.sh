#!/usr/bin/env bash
#
# AI-Guide v0.1 部署脚本（在服务器上、仓库根目录的父级路径执行都可以）
#
#   用法: deploy/deploy.sh --version 0.1.0 [--dry-run] [--skip-backup]
#
# 流程: 记录当前版本 -> 备份(backup.sh) -> 切换 APP_VERSION -> config -q -> 拉取镜像 -> 迁移(migrate)
#       -> up -d -> 健康检查 -> 打印版本和状态；任何一步失败都会把 .env 恢复成上一个版本
#
# 重要事实（不要宣称脚本没做到的事）:
#   - 本脚本只负责「把 APP_VERSION 指向已构建好的镜像标签并重启服务」。
#   - 镜像必须已经由 CI 推到 IMAGE_REGISTRY；脚本不做 docker build。
#   - 数据库迁移只向前执行（alembic upgrade head）。回退不会自动降级数据库，
#     需要人工判断，见 rollback.sh 的输出提示。
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

COMPOSE_FILE="${COMPOSE_FILE:-${REPO_ROOT}/docker-compose.yml}"
ENV_FILE="${ENV_FILE:-${REPO_ROOT}/.env}"
SERVICES=(mysql redis api worker web)
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"
HEALTH_INTERVAL="${HEALTH_INTERVAL:-5}"
WEB_HTTP_PORT="${WEB_HTTP_PORT:-80}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:${WEB_HTTP_PORT}/api/health/ready}"

VERSION=""
DRY_RUN=0
SKIP_BACKUP=0

log()  { printf '[deploy] %s\n' "$*"; }
warn() { printf '[deploy][warn] %s\n' "$*" >&2; }
die()  { printf '[deploy][error] %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
用法: deploy/deploy.sh --version <tag> [--dry-run] [--skip-backup]

  --version <tag>   必填。要部署的发布标签，必须与 .env 的 APP_VERSION、GHCR 上的镜像 tag 一致（例如 0.1.0）。
  --dry-run         只打印将要执行的命令，不修改 .env、不调用 Docker。
  --skip-backup     跳过 backup.sh（仅用于已经手工备份过的场景）。
  -h, --help        显示本帮助。

环境变量（可选）:
  COMPOSE_FILE      默认 <仓库根>/docker-compose.yml
  ENV_FILE          默认 <仓库根>/.env
  HEALTH_URL        默认 http://127.0.0.1:${WEB_HTTP_PORT}/api/health/ready
  HEALTH_TIMEOUT    健康检查总超时秒数，默认 180
  HEALTH_INTERVAL   健康检查轮询间隔秒数，默认 5
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)     [[ $# -ge 2 ]] || die "--version 需要一个参数"; VERSION="$2"; shift 2 ;;
    --version=*)   VERSION="${1#*=}"; shift ;;
    --dry-run)     DRY_RUN=1; shift ;;
    --skip-backup) SKIP_BACKUP=1; shift ;;
    -h|--help)     usage; exit 0 ;;
    *)             usage; die "未知参数: $1" ;;
  esac
done

[[ -n "${VERSION}" ]] || { usage; die "缺少必填参数 --version <tag>"; }
[[ -f "${COMPOSE_FILE}" ]] || die "找不到 compose 文件: ${COMPOSE_FILE}"
[[ -f "${ENV_FILE}" ]] || die "找不到环境文件: ${ENV_FILE}（服务器上从 deploy/.env.production.example 复制并填写）"

compose() { docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" "$@"; }

# 统一执行入口：dry-run 时只打印。
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

# 健康检查：优先 curl，没有 curl 时借 api 容器内的 python 探测。
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
      warn "健康检查在 ${HEALTH_TIMEOUT}s 内未通过"
      warn "排查: docker compose --env-file ${ENV_FILE} -f ${COMPOSE_FILE} ps / logs api migrate"
      return 1
    fi
    sleep "${HEALTH_INTERVAL}"
  done
}

main() {
  local previous_version
  previous_version="$(current_version || true)"

  log "仓库根目录: ${REPO_ROOT}"
  log "compose: ${COMPOSE_FILE}"
  log "env    : ${ENV_FILE}"
  log "目标版本: ${VERSION}"
  log "当前版本: ${previous_version:-<未设置>}"

  if [[ "${DRY_RUN}" -eq 0 ]]; then
    require_docker
    [[ "${VERSION}" != "latest" ]] || die "禁止部署 latest；请使用语义版本标签（例如 0.1.0）"
  fi

  # 1) 备份：先备份，再改任何东西。
  if [[ "${SKIP_BACKUP}" -eq 1 ]]; then
    warn "已按 --skip-backup 跳过备份"
  else
    run bash "${SCRIPT_DIR}/backup.sh"
  fi

  # 2) 先把 APP_VERSION 切到目标版本，再校验配置、再拉镜像（B8 修正）。
  #    顺序很重要：compose pull 用的是当前 .env 插值出来的镜像标签，
  #    先 pull 后改版本会拉到旧标签，等于没有预拉取。
  local backup_env=""
  if [[ "$(current_version || true)" == "${VERSION}" ]]; then
    log "APP_VERSION 已经是 ${VERSION}，无需改写"
  else
    backup_env="${SCRIPT_DIR}/.env.bak.$(date -u +%Y%m%dT%H%M%SZ)"
    if [[ "${DRY_RUN}" -eq 1 ]]; then
      printf '[dry-run] cp %s %s\n' "${ENV_FILE}" "${backup_env}"
      printf '[dry-run] set_env_var APP_VERSION=%s in %s\n' "${VERSION}" "${ENV_FILE}"
    else
      cp "${ENV_FILE}" "${backup_env}"
      log "已备份 .env 到 ${backup_env}"
      set_env_var APP_VERSION "${VERSION}" "${ENV_FILE}"
      log "APP_VERSION 已更新为 ${VERSION}（失败会自动恢复该文件）"
    fi
  fi

  # 切换版本后任何一步失败都要把 .env 恢复到上一个版本，避免留下"半切换"状态。
  restore_env_on_failure() {
    local code=$?
    if (( code != 0 )) && [[ -n "${backup_env}" && -f "${backup_env}" && "${DRY_RUN}" -eq 0 ]]; then
      cp "${backup_env}" "${ENV_FILE}"
      warn "部署失败（退出码 ${code}），已把 .env 恢复到上一个版本"
    fi
    return "${code}"
  }
  trap restore_env_on_failure EXIT

  # 3) 用目标版本校验 compose 配置（插值缺失/端口冲突在这里就会失败）。
  run compose config -q

  # 4) 拉取目标版本镜像（api/worker/migrate 共用同一个后端镜像）。
  run compose pull "${SERVICES[@]}"

  # 5) 数据库迁移：一次性容器，成功退出后才启动 api/worker。
  run compose run --rm migrate

  # 6) 启动/更新全部服务。
  run compose up -d

  if [[ "${DRY_RUN}" -eq 1 ]]; then
    printf '[dry-run] wait_healthy (%s)\n' "${HEALTH_URL}"
    printf '[dry-run] %s\n' "compose ps && print version"
    log "dry-run 完成：没有修改 .env，也没有调用 Docker"
    return 0
  fi

  # 7) 健康检查 + 状态输出。
  if ! wait_healthy; then
    warn "部署未通过健康检查。当前 .env 的 APP_VERSION=${VERSION}（上一个版本: ${previous_version:-未知}）"
    warn "回退: ${SCRIPT_DIR}/rollback.sh --version ${previous_version:-<上一个版本>}"
    return 1
  fi

  log "版本: $(current_version)"
  compose ps
  if command -v curl >/dev/null 2>&1; then
    log "readiness 原始响应:"
    curl -fsS "${HEALTH_URL}" || true
    printf '\n'
  fi
  log "部署完成。失败时请用 ${SCRIPT_DIR}/rollback.sh --version <上一标签> 回退（数据库迁移需人工判断，见脚本输出）。"
}

main "$@"
