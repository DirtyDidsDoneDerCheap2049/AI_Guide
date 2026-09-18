#!/usr/bin/env bash
#
# AI-Guide v0.1 对外 smoke 入口
#
#   用法: deploy/smoke.sh --base-url https://<域名> [-- <传给 smoke.py 的额外参数>]
#
# 做什么: 调用 backend/scripts/smoke.py（由后端任务提供）对已部署环境跑真实主流程。
# 不做什么: 本脚本不自己实现任何 HTTP 流程，也不会在缺少 smoke.py 时伪造成功。
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BACKEND_DIR="${REPO_ROOT}/backend"
SMOKE_SCRIPT="${BACKEND_DIR}/scripts/smoke.py"

BASE_URL=""
EXTRA_ARGS=()

log()  { printf '[smoke] %s\n' "$*"; }
die()  { printf '[smoke][error] %s\n' "$*" >&2; exit "${2:-1}"; }

usage() {
  cat <<'EOF'
用法: deploy/smoke.sh --base-url <URL> [-- <额外参数>]

  --base-url <URL>  必填。已部署环境的入口地址，例如 https://guide.example.com
                    （脚本不接受 localhost 之外的相对地址；本机验证请写 http://127.0.0.1）。
  --                其后的参数原样传给 backend/scripts/smoke.py
  -h, --help        显示本帮助。

环境变量:
  PYTHON_BIN        指定 python 解释器，默认优先 python3，其次 python。
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-url)   [[ $# -ge 2 ]] || die "--base-url 需要一个参数"; BASE_URL="$2"; shift 2 ;;
    --base-url=*) BASE_URL="${1#*=}"; shift ;;
    --)           shift; EXTRA_ARGS=("$@"); break ;;
    -h|--help)    usage; exit 0 ;;
    *)            usage; die "未知参数: $1" ;;
  esac
done

[[ -n "${BASE_URL}" ]] || { usage; die "缺少必填参数 --base-url <URL>"; }

if [[ ! -f "${SMOKE_SCRIPT}" ]]; then
  cat >&2 <<EOF
[smoke][error] 找不到 ${SMOKE_SCRIPT}

  backend/scripts/smoke.py 尚未存在于本仓库，因此本次 smoke 没有执行，也没有任何结果可报告。
  该脚本应覆盖：创建匿名会话与项目 -> 上传图片 -> 等待地点候选 -> 确认地点 -> 取得导游卡片 -> SSE 断线补发。
  请在 smoke.py 落地后重新执行本脚本；不要用其它方式伪造 smoke 通过。
EOF
  exit 2
fi

PYTHON_BIN="${PYTHON_BIN:-}"

# 解释器必须真的能跑（Windows 上 python3 可能只是应用商店占位符）。
probe_python() {
  local candidate="$1"
  command -v "${candidate}" >/dev/null 2>&1 || return 1
  "${candidate}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1
}

if [[ -n "${PYTHON_BIN}" ]]; then
  probe_python "${PYTHON_BIN}" || die "PYTHON_BIN=${PYTHON_BIN} 不可用（找不到或版本低于 3.9）"
else
  for candidate in python3 python; do
    if probe_python "${candidate}"; then
      PYTHON_BIN="${candidate}"
      break
    fi
  done
  [[ -n "${PYTHON_BIN}" ]] || die "找不到可用的 python3/python；请用 PYTHON_BIN 指定解释器"
fi

log "解释器: ${PYTHON_BIN}"
log "目标地址: ${BASE_URL}"
log "smoke 脚本: ${SMOKE_SCRIPT}"

set +e
(
  cd "${BACKEND_DIR}"
  "${PYTHON_BIN}" "scripts/smoke.py" --base-url "${BASE_URL}" ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}
)
status=$?
set -e

if [[ "${status}" -eq 0 ]]; then
  log "smoke 通过（退出码 0）: ${BASE_URL}"
else
  printf '[smoke][error] smoke 失败，退出码 %s\n' "${status}" >&2
fi
exit "${status}"
