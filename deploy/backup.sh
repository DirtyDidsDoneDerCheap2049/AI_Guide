#!/usr/bin/env bash
#
# AI-Guide v0.1 备份脚本
#
#   用法: deploy/backup.sh [--dry-run] [--skip-maintenance]
#
# 一致性屏障（B8 修正）——默认按维护窗口执行，保证「数据库 + 媒体」是同一时刻的一致集合：
#   1) 停止 api 与 worker：不再有新上传、新任务、新外部调用
#   2) mysqldump --single-transaction：数据库一致快照
#   3) 打包媒体卷
#   4) 记录 manifest（版本、迁移版本、各表行数、媒体文件清单哈希、两个产物的 sha256）
#   5) 在**屏障仍然有效**时复核一遍行数与媒体清单，和 manifest 比对
#   6) 无论成功失败都恢复 api 与 worker（trap）
#
# 产出（文件名使用 UTC 时间戳）:
#   backups/mysql-<UTC>.sql.gz        mysqldump 全库（含 routines/events）
#   backups/media-<UTC>.tar.gz        媒体命名卷 media-data 的打包
#   backups/manifest-<UTC>.json       一致性清单（恢复脚本会用它对账）
#
# 事实说明:
#   - Redis 只承载 Dramatiq 队列，不备份，也不能当作业务数据恢复来源。
#   - --skip-maintenance 会在**有写入**的情况下备份，数据库本身仍然一致，
#     但媒体归档可能包含比数据库更新的文件；脚本会明确打印这个风险。
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

COMPOSE_FILE="${COMPOSE_FILE:-${REPO_ROOT}/docker-compose.yml}"
ENV_FILE="${ENV_FILE:-${REPO_ROOT}/.env}"
BACKUP_DIR="${BACKUP_DIR:-${REPO_ROOT}/backups}"
BACKUP_RETENTION="${BACKUP_RETENTION:-7}"
MEDIA_ROOT_CONTAINER="${MEDIA_ROOT_CONTAINER:-/var/lib/ai-guide/media}"

DRY_RUN=0
SKIP_MAINTENANCE=0
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"

log()  { printf '[backup] %s\n' "$*"; }
warn() { printf '[backup][warn] %s\n' "$*" >&2; }
die()  { printf '[backup][error] %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
用法: deploy/backup.sh [--dry-run] [--skip-maintenance]

  --dry-run            只打印将要执行的命令，不写任何文件。
  --skip-maintenance   不停止 api/worker（数据库仍一致，但媒体归档可能与数据库不同步）。
  -h, --help           显示本帮助。

环境变量（可选）:
  BACKUP_DIR            备份目录，默认 <仓库根>/backups
  BACKUP_RETENTION      每个类型保留的份数，默认 7
  MEDIA_ROOT_CONTAINER  容器内媒体目录，默认 /var/lib/ai-guide/media
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)          DRY_RUN=1; shift ;;
    --skip-maintenance) SKIP_MAINTENANCE=1; shift ;;
    -h|--help)          usage; exit 0 ;;
    *)                  usage; die "未知参数: $1" ;;
  esac
done

[[ -f "${COMPOSE_FILE}" ]] || die "找不到 compose 文件: ${COMPOSE_FILE}"
[[ -f "${ENV_FILE}" ]] || die "找不到环境文件: ${ENV_FILE}"
[[ "${BACKUP_RETENTION}" =~ ^[0-9]+$ ]] || die "BACKUP_RETENTION 必须是整数，当前: ${BACKUP_RETENTION}"

compose() {
  local files=(-f "${COMPOSE_FILE}")
  if [[ -n "${COMPOSE_OVERRIDE_FILE:-}" ]]; then files+=(-f "${COMPOSE_OVERRIDE_FILE}"); fi
  docker compose --env-file "${ENV_FILE}" "${files[@]}" "$@"
}

require_docker() {
  command -v docker >/dev/null 2>&1 || die "未找到 docker 命令；本脚本必须在具备 Docker Compose v2 的服务器上执行"
  docker compose version >/dev/null 2>&1 || die "docker compose (v2) 不可用"
}

mysql_out="${BACKUP_DIR}/mysql-${TIMESTAMP}.sql.gz"
media_out="${BACKUP_DIR}/media-${TIMESTAMP}.tar.gz"
manifest_out="${BACKUP_DIR}/manifest-${TIMESTAMP}.json"

STOPPED_SERVICES=()

# ------------------------------------------------------------------ 维护窗口

enter_maintenance() {
  if [[ "${SKIP_MAINTENANCE}" -eq 1 ]]; then
    warn "已按 --skip-maintenance 跳过维护窗口：数据库快照一致，但媒体归档可能与数据库不同步"
    return 0
  fi
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
    log "api/worker 当前未运行，无需进入维护窗口"
    return 0
  fi
  log "进入维护窗口：停止 ${targets[*]}（不再有新写入与新外部调用）"
  compose stop ${targets[@]+"${targets[@]}"}
  STOPPED_SERVICES=(${targets[@]+"${targets[@]}"})
}

leave_maintenance() {
  (( ${#STOPPED_SERVICES[@]} == 0 )) && return 0
  log "退出维护窗口：重新启动 ${STOPPED_SERVICES[*]}"
  compose up -d ${STOPPED_SERVICES[@]+"${STOPPED_SERVICES[@]}"}
}

cleanup() {
  local code=$?
  leave_maintenance || warn "恢复服务失败，请手动执行: docker compose up -d api worker"
  exit "${code}"
}
trap cleanup EXIT

# ------------------------------------------------------------------ 采集

sql_scalar() {
  # SQL 作为位置参数传给容器内 shell，避免反引号、$() 或引号被二次解析。
  compose exec -T mysql sh -c \
    'exec mysql -N -B -u root -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE" -e "$1"' \
    sh "$1"
}

dump_mysql() {
  log "mysqldump -> ${mysql_out}"
  # 变量在容器内展开（compose 里的 $$ 转义与这里无关：这是 sh -c 的单引号脚本）。
  compose exec -T mysql sh -c \
    'exec mysqldump --single-transaction --quick --routines --events --default-character-set=utf8mb4 -u root -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE"' \
    | gzip -c >"${mysql_out}"
}

dump_media() {
  log "媒体卷打包 -> ${media_out}"
  # 优先在运行中的 api 容器里执行（stdout 干净）；api 未运行时用一次性容器。
  if compose ps --status running --services 2>/dev/null | grep -qx api; then
    compose exec -T api tar -czf - -C "${MEDIA_ROOT_CONTAINER}" . >"${media_out}"
  else
    compose run --rm -T --no-deps api tar -czf - -C "${MEDIA_ROOT_CONTAINER}" . >"${media_out}"
  fi
  # 防止 compose 的状态输出混进归档：落盘后校验 gzip 完整性。
  gzip -t "${media_out}" || die "媒体备份不是有效的 gzip 归档（可能被容器输出污染）: ${media_out}"
}

media_listing() {
  # 媒体文件清单（相对路径 + 大小），用于 manifest 与恢复对账。
  local listing_cmd="find ${MEDIA_ROOT_CONTAINER} -type f -printf '%P %s\\n' 2>/dev/null | LC_ALL=C sort"
  if compose ps --status running --services 2>/dev/null | grep -qx api; then
    compose exec -T api sh -c "${listing_cmd}"
  else
    compose run --rm -T --no-deps api sh -c "${listing_cmd}"
  fi
}

table_counts_json() {
  # 每个基表的行数，写成 JSON 片段（不含 mysql 系统库）。
  local tables sql
  tables="$(compose exec -T mysql sh -c \
    'exec mysql -N -B -u root -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE" -e "SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE() AND table_type = \"BASE TABLE\" ORDER BY table_name"' \
    | tr -d '\r')"
  sql=""
  while IFS= read -r table; do
    [[ -n "${table}" ]] || continue
    if [[ -n "${sql}" ]]; then sql="${sql} UNION ALL "; fi
    sql="${sql}SELECT '${table}' AS t, COUNT(*) AS c FROM \`${table}\`"
  done <<<"${tables}"
  [[ -n "${sql}" ]] || { printf '{}'; return 0; }
  compose exec -T mysql sh -c \
    'exec mysql -N -B -u root -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE" -e "$1"' \
    sh "${sql}" \
    | tr -d '\r' \
    | awk -F'\t' 'BEGIN{printf "{"} {if (n++) printf ","; printf "\"%s\":%s", $1, $2} END{printf "}"}'
}

json_escape() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'; }

file_sha256() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}';
  elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}';
  else printf 'unavailable'; fi
}

manifest_field() { sed -n "s/.*\"$1\": \"\([^\"]*\)\".*/\1/p" "${manifest_out}" | head -n1; }

write_manifest() {
  local alembic app_version media_hash media_files counts maintenance
  alembic="$(sql_scalar 'SELECT version_num FROM alembic_version' | tr -d '\r' | head -n1)"
  app_version="$(sed -n 's/^APP_VERSION=[[:space:]]*\([^[:space:]#]*\).*$/\1/p' "${ENV_FILE}" | tail -n1)"
  media_hash="$(media_listing | sha256sum | awk '{print $1}')"
  media_files="$(media_listing | grep -c . || true)"
  counts="$(table_counts_json)"
  maintenance="true"
  [[ "${SKIP_MAINTENANCE}" -eq 1 ]] && maintenance="false"

  cat >"${manifest_out}" <<EOF
{
  "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "app_version": "$(json_escape "${app_version}")",
  "alembic_revision": "$(json_escape "${alembic}")",
  "maintenance_window": ${maintenance},
  "mysql_artifact": "$(json_escape "$(basename "${mysql_out}")")",
  "mysql_sha256": "$(file_sha256 "${mysql_out}")",
  "media_artifact": "$(json_escape "$(basename "${media_out}")")",
  "media_sha256": "$(file_sha256 "${media_out}")",
  "media_files": ${media_files},
  "media_listing_sha256": "${media_hash}",
  "table_counts": ${counts}
}
EOF
  log "一致性清单 -> ${manifest_out}"
}

verify_consistency() {
  local media_hash media_files now_counts expected_counts
  media_hash="$(media_listing | sha256sum | awk '{print $1}')"
  media_files="$(media_listing | grep -c . || true)"
  now_counts="$(table_counts_json)"
  expected_counts="$(sed -n 's/.*"table_counts": \(.*\)$/\1/p' "${manifest_out}" | head -n1)"

  if [[ "${media_hash}" != "$(manifest_field media_listing_sha256)" ]]; then
    warn "媒体清单在备份期间发生变化（屏障期间不应发生）：请检查是否有外部写入"
    return 1
  fi
  if [[ "${now_counts}" != "${expected_counts}" ]]; then
    warn "数据库行数与清单不一致（屏障期间不应发生）"
    warn "  清单: ${expected_counts}"
    warn "  现在: ${now_counts}"
    return 1
  fi
  log "一致性复核通过：媒体文件 ${media_files} 个、各表行数与清单一致"
  return 0
}

prune() {
  local pattern="$1" keep="$2" count=0 file
  while IFS= read -r file; do
    [[ -n "${file}" ]] || continue
    count=$((count + 1))
    if (( count > keep )); then
      rm -f -- "${file}"
      log "清理旧备份: $(basename "${file}")"
    fi
  done < <(find "${BACKUP_DIR}" -maxdepth 1 -type f -name "${pattern}" -printf '%T@ %p\n' 2>/dev/null | sort -rn | cut -d' ' -f2-)
}

report() {
  local file="$1"
  [[ -f "${file}" ]] || die "备份文件不存在: ${file}"
  local bytes human
  bytes="$(wc -c <"${file}" | tr -d ' ')"
  human="$(du -h "${file}" | cut -f1)"
  printf '[backup] 生成: %s (大小 %s / %s 字节)\n' "${file}" "${human}" "${bytes}"
}

if [[ "${DRY_RUN}" -eq 1 ]]; then
  printf '[dry-run] mkdir -p %s\n' "${BACKUP_DIR}"
  printf '[dry-run] docker compose --env-file %s -f %s stop api worker   # 维护窗口（除非 --skip-maintenance）\n' "${ENV_FILE}" "${COMPOSE_FILE}"
  printf '[dry-run] docker compose --env-file %s -f %s exec -T mysql sh -c <mysqldump ...> | gzip -c > %s\n' \
    "${ENV_FILE}" "${COMPOSE_FILE}" "${mysql_out}"
  printf '[dry-run] docker compose --env-file %s -f %s exec -T api tar -czf - -C %s . > %s\n' \
    "${ENV_FILE}" "${COMPOSE_FILE}" "${MEDIA_ROOT_CONTAINER}" "${media_out}"
  printf '[dry-run] 写 manifest（迁移版本、各表行数、媒体清单哈希、产物 sha256）: %s\n' "${manifest_out}"
  printf '[dry-run] 一致性复核（媒体清单 + 各表行数与 manifest 比对）\n'
  printf '[dry-run] docker compose --env-file %s -f %s up -d api worker   # 退出维护窗口\n' "${ENV_FILE}" "${COMPOSE_FILE}"
  printf '[dry-run] prune: 每类保留最近 %s 份\n' "${BACKUP_RETENTION}"
  log "dry-run 完成：没有写任何文件"
  exit 0
fi

require_docker
mkdir -p "${BACKUP_DIR}"

enter_maintenance

if ! dump_mysql; then
  rm -f -- "${mysql_out}"
  die "mysqldump 失败（已删除半成品文件）"
fi
dump_media
write_manifest

verify_status=0
verify_consistency || verify_status=1

report "${mysql_out}"
report "${media_out}"

prune 'mysql-*.sql.gz' "${BACKUP_RETENTION}"
prune 'media-*.tar.gz' "${BACKUP_RETENTION}"
prune 'manifest-*.json' "${BACKUP_RETENTION}"

if (( verify_status != 0 )); then
  die "备份已生成，但一致性复核未通过：请人工确认后再用于恢复"
fi

log "备份完成（一致集合）。MySQL 是唯一事实来源，回退时不能把 Redis 当作恢复来源。"
