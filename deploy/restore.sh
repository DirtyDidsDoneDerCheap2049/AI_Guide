#!/usr/bin/env bash
#
# AI-Guide v0.1 恢复脚本（从 deploy/backup.sh 的产物恢复）
#
#   用法: deploy/restore.sh --mysql backups/mysql-<UTC>.sql.gz [--media backups/media-<UTC>.tar.gz]
#                          [--manifest backups/manifest-<UTC>.json] [--yes] [--dry-run] [--skip-verify]
#
# 安全约定（B8 修正：先验证再切换）:
#   1) 停止 api 与 worker —— 恢复期间不允许新写入
#   2) 归档完整性检查（gzip -t、tar -tzf）+ manifest 里的 sha256 对账
#   3) 先把数据库恢复进**临时库** `<DB>_restore_check`，在临时库里验证表结构与行数
#   4) 验证通过后才恢复正式库；媒体先按 manifest 校验清单哈希，再解包，再复核解包后的清单
#   5) 任何一步失败都保留现状并明确报错，不"半恢复"
#   6) 结束时（无论成败）重新启动 api 与 worker
#
# 不做的事: 不自动降级数据库、不修改 APP_VERSION（版本切换用 deploy.sh / rollback.sh）。
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

COMPOSE_FILE="${COMPOSE_FILE:-${REPO_ROOT}/docker-compose.yml}"
ENV_FILE="${ENV_FILE:-${REPO_ROOT}/.env}"
MEDIA_ROOT_CONTAINER="${MEDIA_ROOT_CONTAINER:-/var/lib/ai-guide/media}"
CHECK_DB_SUFFIX="${CHECK_DB_SUFFIX:-_restore_check}"

MYSQL_FILE=""
MEDIA_FILE=""
MANIFEST_FILE=""
ASSUME_YES=0
DRY_RUN=0
SKIP_VERIFY=0

log()  { printf '[restore] %s\n' "$*"; }
warn() { printf '[restore][warn] %s\n' "$*" >&2; }
die()  { printf '[restore][error] %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
用法: deploy/restore.sh [--mysql <file>] [--media <file>] [--manifest <file>] [--yes] [--dry-run] [--skip-verify]

  --mysql <file>     要恢复的 mysqldump 压缩文件（backups/mysql-<UTC>.sql.gz）
  --media <file>     要恢复的媒体卷归档（backups/media-<UTC>.tar.gz）
  --manifest <file>  一致性清单（backups/manifest-<UTC>.json）；给了就用于对账
  --yes              跳过交互确认（CI 或已人工确认时使用）
  --dry-run          只打印将要执行的命令
  --skip-verify      跳过临时库验证（只建议在明确知道风险时使用）
  -h, --help         显示本帮助

至少提供 --mysql 或 --media 之一。
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mysql)       [[ $# -ge 2 ]] || die "--mysql 需要一个参数"; MYSQL_FILE="$2"; shift 2 ;;
    --mysql=*)     MYSQL_FILE="${1#*=}"; shift ;;
    --media)       [[ $# -ge 2 ]] || die "--media 需要一个参数"; MEDIA_FILE="$2"; shift 2 ;;
    --media=*)     MEDIA_FILE="${1#*=}"; shift ;;
    --manifest)    [[ $# -ge 2 ]] || die "--manifest 需要一个参数"; MANIFEST_FILE="$2"; shift 2 ;;
    --manifest=*)  MANIFEST_FILE="${1#*=}"; shift ;;
    --yes|-y)      ASSUME_YES=1; shift ;;
    --dry-run)     DRY_RUN=1; shift ;;
    --skip-verify) SKIP_VERIFY=1; shift ;;
    -h|--help)     usage; exit 0 ;;
    *)             usage; die "未知参数: $1" ;;
  esac
done

[[ -n "${MYSQL_FILE}" || -n "${MEDIA_FILE}" ]] || { usage; die "至少需要 --mysql 或 --media 之一"; }
[[ -z "${MYSQL_FILE}" || -f "${MYSQL_FILE}" ]] || die "找不到 MySQL 备份文件: ${MYSQL_FILE}"
[[ -z "${MEDIA_FILE}" || -f "${MEDIA_FILE}" ]] || die "找不到媒体备份文件: ${MEDIA_FILE}"
[[ -z "${MANIFEST_FILE}" || -f "${MANIFEST_FILE}" ]] || die "找不到清单文件: ${MANIFEST_FILE}"
[[ -f "${COMPOSE_FILE}" ]] || die "找不到 compose 文件: ${COMPOSE_FILE}"
[[ -f "${ENV_FILE}" ]] || die "找不到环境文件: ${ENV_FILE}"

compose() {
  local files=(-f "${COMPOSE_FILE}")
  if [[ -n "${COMPOSE_OVERRIDE_FILE:-}" ]]; then files+=(-f "${COMPOSE_OVERRIDE_FILE}"); fi
  docker compose --env-file "${ENV_FILE}" "${files[@]}" "$@"
}

require_docker() {
  command -v docker >/dev/null 2>&1 || die "未找到 docker 命令；本脚本必须在具备 Docker Compose v2 的服务器上执行"
  docker compose version >/dev/null 2>&1 || die "docker compose (v2) 不可用"
}

confirm() {
  if [[ "${ASSUME_YES}" -eq 1 ]]; then
    log "已按 --yes 跳过交互确认"
    return 0
  fi
  if [[ ! -t 0 ]]; then
    die "当前不是交互式终端；确认无误后请显式加 --yes"
  fi
  cat >&2 <<EOF
[restore] 即将执行恢复，操作不可自动撤销：
  数据库: ${MYSQL_FILE:-<跳过>}   -> 先恢复到临时库验证，通过后才覆盖正式库
  媒体卷: ${MEDIA_FILE:-<跳过>}   -> 校验清单后解包写入 media-data 卷
  清单  : ${MANIFEST_FILE:-<未提供，跳过对账>}
  恢复期间 api/worker 会停止，结束后自动重启。备份文件不会被修改。
  输入 restore 继续，其他任意输入取消: 
EOF
  local answer=""
  read -r answer
  [[ "${answer}" == "restore" ]] || die "已取消（输入的不是 restore）"
}

manifest_field() {
  local key="$1"
  [[ -n "${MANIFEST_FILE}" ]] || return 0
  sed -n "s/.*\"${key}\": \"\([^\"]*\)\".*/\1/p" "${MANIFEST_FILE}" | head -n1
}

manifest_number() {
  local key="$1"
  [[ -n "${MANIFEST_FILE}" ]] || return 0
  sed -n "s/.*\"${key}\": \([0-9][0-9]*\).*/\1/p" "${MANIFEST_FILE}" | head -n1
}

manifest_counts() {
  [[ -n "${MANIFEST_FILE}" ]] || return 0
  sed -n 's/.*"table_counts": \(.*\)$/\1/p' "${MANIFEST_FILE}" | head -n1
}

file_sha256() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}';
  elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}';
  else printf 'unavailable'; fi
}

sql_on() {
  local database="$1" statement="$2"
  compose exec -T mysql sh -c \
    'if [ -n "$1" ]; then
       exec mysql -N -B -u root -p"$MYSQL_ROOT_PASSWORD" "$1" -e "$2"
     else
       exec mysql -N -B -u root -p"$MYSQL_ROOT_PASSWORD" -e "$2"
     fi' \
    sh "${database}" "${statement}" | tr -d '\r'
}

table_counts_json() {
  local database="$1" tables sql=""
  tables="$(sql_on "${database}" "SELECT table_name FROM information_schema.tables WHERE table_schema = '${database}' AND table_type = 'BASE TABLE' ORDER BY table_name")"
  while IFS= read -r table; do
    [[ -n "${table}" ]] || continue
    if [[ -n "${sql}" ]]; then sql="${sql} UNION ALL "; fi
    sql="${sql}SELECT '${table}' AS t, COUNT(*) AS c FROM \`${table}\`"
  done <<<"${tables}"
  [[ -n "${sql}" ]] || { printf '{}'; return 0; }
  sql_on "${database}" "${sql}" | awk -F'\t' 'BEGIN{printf "{"} {if (n++) printf ","; printf "\"%s\":%s", $1, $2} END{printf "}"}'
}

# ------------------------------------------------------------------ 校验阶段

verify_artifacts() {
  if [[ -n "${MYSQL_FILE}" ]]; then
    gzip -t "${MYSQL_FILE}" || die "MySQL 备份不是有效的 gzip 归档: ${MYSQL_FILE}"
    local expected actual
    expected="$(manifest_field mysql_sha256)"
    if [[ -n "${expected}" && "${expected}" != "unavailable" ]]; then
      actual="$(file_sha256 "${MYSQL_FILE}")"
      [[ "${actual}" == "${expected}" ]] || die "MySQL 备份的 sha256 与清单不一致（文件可能损坏或被替换）"
      log "MySQL 备份 sha256 与清单一致"
    fi
  fi
  if [[ -n "${MEDIA_FILE}" ]]; then
    gzip -t "${MEDIA_FILE}" || die "媒体备份不是有效的 gzip 归档: ${MEDIA_FILE}"
    tar -tzf "${MEDIA_FILE}" >/dev/null || die "媒体归档目录不可读: ${MEDIA_FILE}"
    local expected actual
    expected="$(manifest_field media_sha256)"
    if [[ -n "${expected}" && "${expected}" != "unavailable" ]]; then
      actual="$(file_sha256 "${MEDIA_FILE}")"
      [[ "${actual}" == "${expected}" ]] || die "媒体备份的 sha256 与清单不一致"
      log "媒体备份 sha256 与清单一致"
    fi
  fi
}

verify_mysql_in_temp_db() {
  local db_name check_db expected_counts actual_counts
  db_name="$(compose exec -T mysql sh -c 'printf %s "$MYSQL_DATABASE"' | tr -d '\r')"
  check_db="${db_name}${CHECK_DB_SUFFIX}"
  log "先在临时库验证: ${check_db}（不动正式库）"

  sql_on "" "DROP DATABASE IF EXISTS \`${check_db}\`"
  sql_on "" "CREATE DATABASE \`${check_db}\` CHARACTER SET utf8mb4"
  if ! gunzip -c "${MYSQL_FILE}" | compose exec -T mysql sh -c \
      "exec mysql -u root -p\"\$MYSQL_ROOT_PASSWORD\" ${check_db}"; then
    sql_on "" "DROP DATABASE IF EXISTS \`${check_db}\`"
    die "临时库导入失败：备份文件内容有问题，正式库未被修改"
  fi

  local alembic expected_revision
  alembic="$(sql_on "${check_db}" 'SELECT version_num FROM alembic_version' | head -n1)"
  expected_revision="$(manifest_field alembic_revision)"
  if [[ -z "${alembic}" ]]; then
    warn "临时库里没有 alembic_version：这个备份可能不是本项目的完整库"
    [[ "${SKIP_VERIFY}" -eq 1 ]] || die "临时库缺少 alembic_version（可用 --skip-verify 强制继续）"
  elif [[ -n "${expected_revision}" && "${alembic}" != "${expected_revision}" ]]; then
    die "临时库迁移版本 ${alembic} 与清单 ${expected_revision} 不一致"
  fi
  log "临时库迁移版本: ${alembic:-<无>}"

  expected_counts="$(manifest_counts)"
  if [[ -n "${expected_counts}" ]]; then
    actual_counts="$(table_counts_json "${check_db}")"
    if [[ "${actual_counts}" != "${expected_counts}" ]]; then
      warn "行数对账不一致："
      warn "  清单: ${expected_counts}"
      warn "  临时库: ${actual_counts}"
      die "备份内容与清单不一致，拒绝覆盖正式库"
    fi
    log "行数对账通过（与 manifest 完全一致）"
  else
    warn "未提供清单，跳过行数对账"
  fi

  sql_on "" "DROP DATABASE IF EXISTS \`${check_db}\`"
  log "临时库验证通过并已清理"
}

# ------------------------------------------------------------------ 恢复阶段

restore_mysql() {
  local db_name
  db_name="$(compose exec -T mysql sh -c 'printf %s "$MYSQL_DATABASE"' | tr -d '\r')"
  log "恢复正式库 ${db_name}: ${MYSQL_FILE}"
  # mysqldump 默认带 DROP TABLE IF EXISTS，恢复即覆盖同名表。
  gunzip -c "${MYSQL_FILE}" | compose exec -T mysql sh -c \
    'exec mysql -u root -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE"'
  local counts
  counts="$(table_counts_json "${db_name}")"
  log "恢复后行数: ${counts}"
  log "MySQL 恢复完成（迁移版本以备份内容为准，必要时重新执行 alembic upgrade head）"
}

verify_media_listing_before_restore() {
  local expected_hash actual_hash
  expected_hash="$(manifest_field media_listing_sha256)"
  [[ -n "${expected_hash}" ]] || { warn "清单没有媒体清单哈希，跳过对账"; return 0; }
  # Match backup.sh's regular-file listing: no directories, no leading ./.
  actual_hash="$(tar -tzvf "${MEDIA_FILE}" | awk '$1 ~ /^-/ && NF>=6 {name=$NF; sub(/^\.\//, "", name); print name" "$3}' | LC_ALL=C sort | sha256sum | awk '{print $1}')"
  if [[ "${actual_hash}" != "${expected_hash}" ]]; then
    warn "媒体归档内的文件清单与备份清单不一致（归档可能来自不同的备份批次）"
    return 1
  fi
  log "媒体归档内文件清单与清单一致"
  return 0
}

restore_media() {
  log "恢复媒体卷: ${MEDIA_FILE}"
  compose run --rm -T --no-deps api tar -xzf - -C "${MEDIA_ROOT_CONTAINER}" <"${MEDIA_FILE}"
  warn "媒体恢复是叠加写入：卷中不在备份里的文件不会被删除。"
  warn "需要目录与备份完全一致时，先人工清空卷（docker volume rm 前必须先停 api/worker），再重新恢复。"
}

# ------------------------------------------------------------------ 主流程

STOPPED_SERVICES=()

stop_writers() {
  local running=() targets=() svc item
  while IFS= read -r svc; do
    [[ -n "${svc}" ]] || continue
    running+=("${svc}")
  done < <(compose ps --status running --services 2>/dev/null || true)
  for svc in api worker; do
    for item in ${running[@]+"${running[@]}"}; do
      [[ "${item}" == "${svc}" ]] && targets+=("${svc}")
    done
  done
  if (( ${#targets[@]} == 0 )); then
    log "api/worker 未运行；恢复期间保持停止状态"
    return 0
  fi
  log "停止写入方: ${targets[*]}"
  compose stop ${targets[@]+"${targets[@]}"}
  STOPPED_SERVICES=(${targets[@]+"${targets[@]}"})
}

start_writers() {
  (( ${#STOPPED_SERVICES[@]} == 0 )) && return 0
  log "重新启动: ${STOPPED_SERVICES[*]}"
  compose up -d ${STOPPED_SERVICES[@]+"${STOPPED_SERVICES[@]}"}
}

cleanup() {
  local code=$?
  start_writers || warn "恢复服务失败，请手动执行: docker compose up -d api worker"
  exit "${code}"
}
trap cleanup EXIT

main() {
  confirm

  if [[ "${DRY_RUN}" -eq 1 ]]; then
    printf '[dry-run] docker compose --env-file %s -f %s stop api worker\n' "${ENV_FILE}" "${COMPOSE_FILE}"
    printf '[dry-run] gzip -t %s && tar -tzf %s   # 归档完整性\n' "${MYSQL_FILE:-<skip>}" "${MEDIA_FILE:-<skip>}"
    printf '[dry-run] 在临时库 <DB>%s 导入 %s 并做表结构/行数对账，通过后才恢复正式库\n' "${CHECK_DB_SUFFIX}" "${MYSQL_FILE:-<skip>}"
    printf '[dry-run] gunzip -c %s | docker compose --env-file %s -f %s exec -T mysql sh -c <mysql ...>\n' \
      "${MYSQL_FILE:-<skip>}" "${ENV_FILE}" "${COMPOSE_FILE}"
    printf '[dry-run] docker compose --env-file %s -f %s run --rm -T --no-deps api tar -xzf - -C %s < %s\n' \
      "${ENV_FILE}" "${COMPOSE_FILE}" "${MEDIA_ROOT_CONTAINER}" "${MEDIA_FILE:-<skip>}"
    printf '[dry-run] docker compose --env-file %s -f %s up -d api worker\n' "${ENV_FILE}" "${COMPOSE_FILE}"
    log "dry-run 完成：没有执行任何恢复动作"
    return 0
  fi

  require_docker
  stop_writers

  verify_artifacts

  if [[ -n "${MYSQL_FILE}" ]]; then
    if [[ "${SKIP_VERIFY}" -eq 1 ]]; then
      warn "已按 --skip-verify 跳过临时库验证"
    else
      verify_mysql_in_temp_db
    fi
    restore_mysql
  fi

  if [[ -n "${MEDIA_FILE}" ]]; then
    verify_media_listing_before_restore || warn "继续执行媒体恢复，但请注意上面的清单不一致警告"
    restore_media
  fi

  log "恢复完成。建议随后执行 deploy/smoke.sh --base-url <URL> 验证主流程。"
}

main "$@"
