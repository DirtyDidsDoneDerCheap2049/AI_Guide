/**
 * 中文文案映射：后端状态码、步骤名、事件类型、错误码。
 *
 * 后端返回的都是英文常量（backend/app/models.py 的 MediaStatus / RunStatus / StepName、
 * backend/app/events.py 的 EventType），界面统一在这里转成中文，避免文案散落各处。
 * 遇到未知值一律回退成原值，保证界面不会出现空白。
 */

import { ApiError, readDetailField } from '../api/client'
import type { WorkspaceEventFrame } from '../api/types'
import { formatDistanceKm, formatDurationText } from './format'

type LabelMap = Record<string, string>

const MEDIA_STATUS_LABELS: LabelMap = {
  UPLOADED: '已上传',
  QUEUED: '排队中',
  ANALYZING: '分析中',
  WAITING_USER: '等待确认',
  CONFIRMED: '已确认',
  FAILED: '失败',
  REJECTED: '已拒绝',
  CANCELLED: '已取消',
}

const RUN_STATUS_LABELS: LabelMap = {
  QUEUED: '排队中',
  RUNNING: '执行中',
  WAITING_USER: '等待确认',
  PARTIAL: '部分完成',
  SUCCEEDED: '已完成',
  FAILED: '失败',
  CANCELLED: '已取消',
}

/**
 * 消息状态（backend Message.status）。
 * 注意 QUEUED 对用户消息表示「已入库、回答还没生成」，对助手消息几乎不会出现：
 * 后端只在回答就绪或失败时才写 assistant 行。
 */
const MESSAGE_STATUS_LABELS: LabelMap = {
  QUEUED: '等待回答',
  READY: '已就绪',
  FAILED: '失败',
}

const STEP_LABELS: LabelMap = {
  answer_question: '回答问题',
  analyze_image: '分析图片',
  wait_for_place_confirmation: '等待确认地点',
  lookup_place: '查询地点',
  generate_guide_card: '生成导游卡片',
}

const RUN_TRIGGER_LABELS: LabelMap = {
  initial: '首次上传',
  retry: '重试',
  resume: '确认后恢复',
}

/** 取消原因（后端 cancel_run 的 reason 默认 user_cancelled，拒绝地点时是 place_rejected）。 */
const CANCEL_REASON_LABELS: LabelMap = {
  user_cancelled: '用户主动取消',
  place_rejected: '地点候选被拒绝',
}

const ERROR_CODE_LABELS: LabelMap = {
  // 上传与额度
  active_project_exists: '当前会话已经有一个进行中的项目。',
  media_limit_reached: '每个项目最多只能放 3 张图片。',
  file_too_large: '图片超过 8MB 上限。',
  unsupported_mime_type: '只支持 JPG、PNG、WebP 三种格式。',
  unsupported_image_content: '文件内容不是可识别的图片。',
  mime_mismatch: '文件扩展名与真实内容不一致。',
  empty_file: '上传的文件是空的。',
  invalid_dimensions: '图片尺寸无效。',
  quota_exceeded: '本次体验的运行额度已用完。',
  // 运行与确认
  run_not_waiting_user: '这次运行当前不在等待确认状态，无法提交地点。',
  run_already_finished: '这次运行已经结束，不能再取消。',
  run_not_retryable: '只有失败或部分完成的运行才能重试。',
  // 工作区问答（backend/app/agent/orchestrator.py 的回答链路）
  answer_failed: '这次回答没有生成成功，可以重新提问。',
  candidate_not_found: '候选地点不存在，或不属于这次运行。',
  missing_place_name: '请填写正确的地点名称。',
  place_rejected: '地点候选已被拒绝。',
  user_cancelled: '运行已被用户取消。',
  place_lookup_failed: '地点服务查询失败，卡片不含供应商事实。',
  place_lookup_failed_partial: '地点服务查询失败，本次只完成了部分内容。',
  // 同一个错误码出现在两处：确认地点后的查询链路，以及「收藏照片上的地点」。
  // 文案要同时成立：两种情况的原因都是"这张照片还没有已确认的地点"。
  place_not_confirmed:
    '这张照片还没有确认地点，无法继续（查询地点、生成卡片与收藏都要求先确认地点）。',
  // Agent 步骤与预算（backend/app/agent/orchestrator.py 与 budget.py）
  analyze_failed: '图片分析失败，可以重试。',
  card_failed: '导游卡片生成失败，可以重试。',
  step_failed: 'Agent 步骤执行失败，可以重试。',
  step_retry: '该步骤正在重试。',
  unexpected_error: 'Agent 遇到未预期错误，可以重试。',
  budget_exceeded: '本次运行超出预算上限，已停止。',
  budget_steps_exceeded: '本次运行超出步骤上限，已停止。',
  budget_tool_calls_exceeded: '本次运行超出工具调用上限，已停止。',
  budget_tokens_exceeded: '本次运行超出 Token 上限，已停止。',
  budget_cost_exceeded: '本次运行超出费用上限，已停止。',
  // 模型/地点 Provider（backend/app/agent/providers/base.py）
  provider_error: '外部模型或地点服务调用失败。',
  provider_timeout: '外部服务响应超时。',
  provider_http_error: '外部服务返回了错误状态码。',
  provider_rate_limited: '外部服务限流，请稍后重试。',
  provider_invalid_response: '外部服务返回内容不符合约定结构。',
  provider_not_configured: '外部服务未配置，请联系部署方。',
  provider_no_result: '地点服务没有找到匹配结果。',
  // 相册操作（D4）
  media_busy: '这张照片本身正在分析中（状态是排队中/分析中），等它结束再操作。',
  run_in_progress: '这张照片已经有一个未完成的任务在跑，不能再新建一个。',
  media_deleted: '这张照片已被移出相册：先在「已移出相册」里恢复它，再操作。',
  place_version_conflict:
    '地点版本冲突：这个地点已经在别处被改过（另一个标签页或另一次操作），请按最新地点重来。',
  no_retryable_run: '这张照片没有可重做的运行：只有失败、部分完成或已取消的运行能重做。',
  // 资源与会话
  project_not_found: '项目不存在，或不属于当前会话。',
  media_not_found: '图片不存在，或不属于当前会话。',
  media_file_missing: '图片文件已丢失。',
  run_not_found: '运行不存在。',
  session_required: '会话已过期，请刷新页面。',
  // 收藏与路线（docs/productization/D3b-收藏与路线接口契约.md 的错误码表）
  place_not_verified: '地点还没核实（地点服务没有确认它），不能作为事实收藏。',
  saved_place_not_found: '这条收藏不存在，或不属于当前工作区（可能已经在别处取消收藏）。',
  draft_not_found: '这个路线草稿不存在，或不属于当前工作区。',
  draft_version_conflict: '这个草稿已经在别处被改过（另一个标签页或另一次操作先提交了）。',
  draft_name_taken: '同一个工作区里已经有同名的路线草稿，请换一个名字。',
  stop_missing_coordinates:
    '有停留点没有经纬度，算不了路。请换成有坐标的收藏，或先在地图上确认它。',
  need_at_least_two_stops: '至少要有两个停留点才能算路。',
  too_many_stops: '一条路线最多 20 个停留点。',
  unsupported_mode: '只支持「步行」和「驾车」两种出行方式。',
  direction_provider_not_configured: '这个部署没有配置算路服务（高德路径规划），无法算路。',
  // 账号（docs/productization/D2-账户接口契约.md）
  auth_required: '这个操作需要先登录（当前未登录，或登录状态已失效）。',
  invalid_credentials:
    '邮箱或密码不正确。登录失败不区分「邮箱不存在」和「密码错误」，这是后端的有意设计。',
  email_taken: '这个邮箱已经注册过了。可以直接登录，或改用「忘记密码」重设密码。',
  password_too_weak: '密码太弱：至少 10 位，建议混用字母、数字与符号。',
  invalid_token: '链接里的令牌无效或已过期，请重新申请一次。',
  not_found: '请求的资源不存在（或不属于当前账号）。',
  // 限流的错误码契约没有冻结；后端的实现是 too_many_attempts，这里把常见写法都映射上。
  too_many_attempts: '操作太频繁，已被后端限流（按 IP + 邮箱计数）。',
  rate_limited: '操作太频繁，请稍后再试。',
  too_many_requests: '操作太频繁，请稍后再试。',
  session_not_found: '这个登录会话不存在，或不属于当前账号（可能已经被撤销）。',
  auth_disabled:
    '这个部署关闭了账号功能（auth_enabled=false）：注册、登录、找回密码、重发验证邮件当前都不可用。',
}

const PAYLOAD_KEYS = {
  title: 'title',
  cityHint: 'city_hint',
  position: 'position',
  trigger: 'trigger',
  step: 'step',
  errorCode: 'error_code',
  reason: 'reason',
  placeName: 'place_name',
  decision: 'decision',
  cardTitle: 'title',
  partial: 'partial',
  candidates: 'candidates',
  // D1-h 消息事件的载荷字段（backend MessageEvent payload）
  seq: 'seq',
  model: 'model',
  contextChanged: 'context_changed',
} as const

function mapLabel(map: LabelMap, key: string): string {
  return map[key] ?? key
}

export function mediaStatusLabel(status: string): string {
  return mapLabel(MEDIA_STATUS_LABELS, status)
}

export function runStatusLabel(status: string): string {
  return mapLabel(RUN_STATUS_LABELS, status)
}

export function messageStatusLabel(status: string): string {
  return mapLabel(MESSAGE_STATUS_LABELS, status)
}

/** 消息角色的中文名（对话记录里用于无障碍标注）。 */
export function messageRoleLabel(role: string): string {
  if (role === 'user') {
    return '我'
  }
  if (role === 'assistant') {
    return '导游'
  }
  return '系统'
}

export function stepLabel(step: string): string {
  return step === '' ? '（未开始）' : mapLabel(STEP_LABELS, step)
}

export function triggerLabel(trigger: string): string {
  return mapLabel(RUN_TRIGGER_LABELS, trigger)
}

/** 状态对应的视觉色系，用于徽章着色。 */
export type StatusTone = 'neutral' | 'progress' | 'waiting' | 'ok' | 'bad'

export function mediaStatusTone(status: string): StatusTone {
  switch (status) {
    case 'ANALYZING':
    case 'QUEUED':
      return 'progress'
    case 'WAITING_USER':
      return 'waiting'
    case 'CONFIRMED':
      return 'ok'
    case 'FAILED':
    case 'REJECTED':
    case 'CANCELLED':
      return 'bad'
    default:
      return 'neutral'
  }
}

export function runStatusTone(status: string): StatusTone {
  switch (status) {
    case 'QUEUED':
    case 'RUNNING':
      return 'progress'
    case 'WAITING_USER':
      return 'waiting'
    case 'SUCCEEDED':
      return 'ok'
    case 'PARTIAL':
      return 'waiting'
    case 'FAILED':
    case 'CANCELLED':
      return 'bad'
    default:
      return 'neutral'
  }
}

/** 运行中（未进入终态）的判定，与后端 RunStatus.TERMINAL 保持一致。 */
export function isRunActive(status: string): boolean {
  return (
    status !== 'SUCCEEDED' && status !== 'PARTIAL' && status !== 'FAILED' && status !== 'CANCELLED'
  )
}

export function isRunRetryable(status: string): boolean {
  return status === 'FAILED' || status === 'PARTIAL'
}

/**
 * D4：媒体级「重做」可用的运行状态。
 * `POST /media/{id}/runs/retry` 由后端挑"最近一次失败/部分完成/**已取消**的运行"，
 * 比运行级的 `POST /runs/{id}/retry` 多支持一种（后端 media_ops.retry_run 里写明的三种）。
 */
export function isMediaRunRetryable(status: string): boolean {
  return status === 'FAILED' || status === 'PARTIAL' || status === 'CANCELLED'
}

// --------------------------------------------------------------- 地点核实状态

/**
 * 「已核实」的地点身份（backend/app/services/place_match.py::WRITABLE）。
 *
 * 这三个值意味着地点服务真的确认了这个地点；其余状态（pending / ambiguous /
 * conflict / no_result / provider_error …）都可能只是猜测或干脆没查通，
 * 因此后端不会给它们地址与坐标，界面也不允许把它们收藏成事实。
 * 未知的新状态一律按「未核实」处理：宁可少用一个能力，也不能把没核实的地点当事实。
 */
export const VERIFIED_PLACE_MATCH_STATUSES = [
  'matched',
  'matched_name_only',
  'user_selected',
] as const

export function isPlaceVerified(matchStatus: string | null | undefined): boolean {
  if (matchStatus === null || matchStatus === undefined) {
    return false
  }
  return (VERIFIED_PLACE_MATCH_STATUSES as readonly string[]).includes(matchStatus)
}

const PLACE_MATCH_STATUS_LABELS: LabelMap = {
  matched: '地点服务已核实',
  matched_name_only: '地点服务只核实了名称',
  user_selected: '你在候选里亲自选定',
  pending: '还没核实',
  ambiguous: '多个同名地点，还没确定是哪一个',
  city_conflict: '同名地点在其他城市，还没确定',
  conflict: '地点服务的结果相互冲突',
  no_result: '地点服务没有找到匹配结果',
  provider_error: '地点服务查询失败',
}

/** 地点核实状态的中文说明（用于「为什么不能收藏」这类提示）。 */
export function placeMatchStatusLabel(matchStatus: string | null | undefined): string {
  if (matchStatus === null || matchStatus === undefined || matchStatus === '') {
    return '未知核实状态'
  }
  return PLACE_MATCH_STATUS_LABELS[matchStatus] ?? `未知核实状态（${matchStatus}）`
}

// ------------------------------------------------------- 地点检索与收藏来源

/** 检索词与候选名的匹配强度（backend/services/place_match.py::name_kind）。 */
const PLACE_SEARCH_KIND_LABELS: LabelMap = {
  exact: '名称完全相同',
  prefix: '名称部分相同',
  other: '名称不完全相同（模糊结果）',
}

export function placeSearchKindLabel(kind: string): string {
  return PLACE_SEARCH_KIND_LABELS[kind] ?? `匹配强度未知（${kind}）`
}

/** 收藏来源：photo = 由照片上已核实的地点加入；manual = 手工填写。 */
export function savedPlaceSourceLabel(source: string): string {
  if (source === 'photo') {
    return '来自照片'
  }
  if (source === 'manual') {
    return '手工填写'
  }
  return source
}

// ------------------------------------------------------------------- 路线

const ROUTE_MODE_LABELS: LabelMap = {
  walking: '步行',
  driving: '驾车',
}

export function routeModeLabel(mode: string): string {
  return ROUTE_MODE_LABELS[mode] ?? mode
}

const ROUTE_REVISION_STATUS_LABELS: LabelMap = {
  OK: '算路成功',
  FAILED: '算路失败',
}

export function routeRevisionStatusLabel(status: string): string {
  return ROUTE_REVISION_STATUS_LABELS[status] ?? status
}

/** 供应商是 fixture 时，结果只是测试数据（契约 2.5：必须在界面上标注）。 */
export function isFixtureProvider(provider: string | null | undefined): boolean {
  return typeof provider === 'string' && provider.toLowerCase().startsWith('fixture')
}

/**
 * `route_unavailable_reason` 的中文说明（契约 2.2 的三种真实状态）。
 * 返回 null 表示"当前有可用路线"，界面据此决定是否显示距离与时长。
 */
export function routeUnavailableText(reason: string | null | undefined): string | null {
  if (reason === null || reason === undefined || reason === '') {
    return null
  }
  if (reason === 'not_computed') {
    return '尚未算路'
  }
  if (reason === 'stale_after_edit') {
    return '停留点已改动，请重新算路'
  }
  if (reason.startsWith('compute_failed:')) {
    const code = reason.slice('compute_failed:'.length)
    return `算路失败（${code}）`
  }
  return `当前没有可用路线（${reason}）`
}

/** `compute_failed:<code>` 里的供应商错误码；没有就返回 null。 */
export function routeUnavailableErrorCode(reason: string | null | undefined): string | null {
  if (typeof reason !== 'string' || !reason.startsWith('compute_failed:')) {
    return null
  }
  const code = reason.slice('compute_failed:'.length)
  return code === '' ? null : code
}

export function errorCodeLabel(code: string | null): string | null {
  if (code === null || code === '') {
    return null
  }
  return ERROR_CODE_LABELS[code] ?? `未知错误码：${code}`
}

function readString(payload: Record<string, unknown>, key: string): string | null {
  const value = payload[key]
  if (typeof value === 'string' && value !== '') {
    return value
  }
  if (typeof value === 'number') {
    return String(value)
  }
  return null
}

/**
 * 事件 -> 中文描述（活动记录区使用）。
 * 优先展示载荷里的业务信息，让用户看得懂「刚才发生了什么」。
 */
export function describeEvent(frame: WorkspaceEventFrame): string {
  const { type, payload } = frame
  const step = readString(payload, PAYLOAD_KEYS.step)

  switch (type) {
    case 'project.created': {
      const title = readString(payload, PAYLOAD_KEYS.title)
      const city = readString(payload, PAYLOAD_KEYS.cityHint)
      const parts = [title ? `项目已创建：${title}` : '项目已创建']
      if (city) {
        parts.push(`城市线索：${city}`)
      }
      return parts.join('，')
    }
    case 'media.uploaded': {
      const position = readString(payload, PAYLOAD_KEYS.position)
      return position ? `第 ${position} 张图片已上传` : '图片已上传'
    }
    case 'run.queued': {
      const trigger = readString(payload, PAYLOAD_KEYS.trigger)
      return trigger ? `分析任务已排队（${triggerLabel(trigger)}）` : '分析任务已排队'
    }
    case 'run.started':
      return '分析任务开始执行'
    case 'step.started':
      return step ? `开始步骤：${stepLabel(step)}` : '开始新步骤'
    case 'step.finished':
      return step ? `完成步骤：${stepLabel(step)}` : '步骤已完成'
    case 'candidate.ready': {
      const candidates = payload[PAYLOAD_KEYS.candidates]
      const count = Array.isArray(candidates) ? candidates.length : 0
      return count > 0 ? `已生成 ${count} 个候选地点` : '候选地点已生成'
    }
    case 'run.waiting_user':
      return '等待你确认地点'
    case 'place.changed': {
      const name = readString(payload, PAYLOAD_KEYS.placeName)
      const version = readString(payload, 'place_version')
      const staleCards = readString(payload, 'stale_cards')
      const parts = [name ? `地点已改为：${name}` : '地点已修改']
      if (version !== null) {
        parts.push(`地点版本 ${version}`)
      }
      if (staleCards !== null && staleCards !== '0') {
        parts.push(`${staleCards} 张旧讲解已标记为过期`)
      }
      return parts.join('，')
    }
    case 'media.updated': {
      const field = readString(payload, 'field')
      return field === 'note' ? '照片笔记已保存' : '照片信息已更新'
    }
    case 'media.deleted': {
      const cancelled = readString(payload, 'cancelled_runs')
      return cancelled !== null && cancelled !== '0'
        ? `照片已移出相册（同时取消了 ${cancelled} 个未完成任务）`
        : '照片已移出相册（可在「已移出相册」里恢复）'
    }
    case 'media.restored':
      return '照片已恢复到相册'
    case 'place.confirmed': {
      const name = readString(payload, PAYLOAD_KEYS.placeName)
      const decision = readString(payload, PAYLOAD_KEYS.decision)
      const verb = decision === 'correct' ? '已纠正为' : '已确认'
      return name ? `地点${verb}：${name}` : `地点${verb}`
    }
    case 'place.rejected':
      return '地点候选已拒绝，本次分析结束'
    case 'card.ready': {
      const title = readString(payload, PAYLOAD_KEYS.cardTitle)
      const partial = payload[PAYLOAD_KEYS.partial] === true
      const suffix = partial ? '（不含供应商事实）' : ''
      return title ? `导游卡片已生成：${title}${suffix}` : `导游卡片已生成${suffix}`
    }
    // D3-b 收藏与路线：事件只带 ID 与摘要字段，列表本身仍在服务器上。
    case 'place.saved': {
      const name = readString(payload, PAYLOAD_KEYS.placeName)
      const source = readString(payload, 'source')
      const origin = source === 'photo' ? '照片地点' : source === 'manual' ? '手工填写' : null
      const suffix = origin === null ? '' : `（${origin}）`
      return name ? `已收藏地点：${name}${suffix}` : '收藏了一个地点'
    }
    case 'place.unsaved': {
      const name = readString(payload, PAYLOAD_KEYS.placeName)
      return name ? `已取消收藏：${name}` : '取消了一条收藏'
    }
    case 'route.draft_created': {
      const name = readString(payload, PAYLOAD_KEYS.title)
      return name ? `新建路线草稿：${name}` : '新建了一个路线草稿'
    }
    case 'route.draft_updated': {
      const version = readString(payload, 'input_version')
      const count = readString(payload, 'stops')
      const parts = ['路线草稿已改动']
      if (count !== null) {
        parts.push(`停留点 ${count} 个`)
      }
      if (version !== null) {
        parts.push(`输入版本 ${version}（旧算路结果已过期）`)
      }
      return parts.join('，')
    }
    case 'route.computed': {
      // 后端只在算路**成功**时发这条事件（失败走 compute_failed 路径，不写事件），
      // 这里仍然按 status 兜底，避免把未知状态说成成功。
      const status = readString(payload, 'status')
      if (status !== null && status !== 'OK') {
        return `算路没有成功（${routeRevisionStatusLabel(status)}），停留点已保留`
      }
      const distance = payload.distance_meters
      const duration = payload.duration_seconds
      const parts = ['算路完成']
      const distanceText = typeof distance === 'number' ? formatDistanceKm(distance) : null
      const durationText = typeof duration === 'number' ? formatDurationText(duration) : null
      if (distanceText !== null) {
        parts.push(`全程 ${distanceText}`)
      }
      if (durationText !== null) {
        parts.push(`约 ${durationText}`)
      }
      return parts.join('，')
    }
    case 'run.partial': {
      const code = readString(payload, PAYLOAD_KEYS.errorCode)
      return `运行部分完成${code ? `（${errorCodeLabel(code) ?? code}）` : ''}`
    }
    case 'run.failed': {
      const code = readString(payload, PAYLOAD_KEYS.errorCode)
      return `运行失败${code ? `：${errorCodeLabel(code) ?? code}` : ''}`
    }
    case 'run.cancelled': {
      const reason = readString(payload, PAYLOAD_KEYS.reason)
      if (reason === null) {
        return '运行已取消'
      }
      return `运行已取消（${CANCEL_REASON_LABELS[reason] ?? errorCodeLabel(reason) ?? reason}）`
    }
    case 'run.retry_queued':
      return '已重新排队运行'
    // D1-h 持久对话：事件只带 ID 与序号，正文从消息接口按 seq 增量读取。
    case 'message.created': {
      const seq = readString(payload, PAYLOAD_KEYS.seq)
      return seq ? `已收到提问 #${seq}，回答任务已排队` : '已收到提问，回答任务已排队'
    }
    case 'message.ready': {
      const seq = readString(payload, PAYLOAD_KEYS.seq)
      const model = readString(payload, PAYLOAD_KEYS.model)
      const changed = payload[PAYLOAD_KEYS.contextChanged] === true
      const parts = [seq ? `回答已生成 #${seq}` : '回答已生成']
      if (model !== null) {
        parts.push(`模型 ${model}`)
      }
      if (changed) {
        parts.push('期间工作区有变化，回答基于最新上下文')
      }
      return parts.join('，')
    }
    case 'message.failed': {
      const code = readString(payload, PAYLOAD_KEYS.errorCode)
      return `回答生成失败${code ? `：${errorCodeLabel(code) ?? code}` : ''}`
    }
    default:
      return `收到未识别事件：${type}`
  }
}

/** 活动记录里附带展示的原始错误码。 */
export function eventErrorCode(frame: WorkspaceEventFrame): string | null {
  return (
    readString(frame.payload, PAYLOAD_KEYS.errorCode) ??
    readString(frame.payload, PAYLOAD_KEYS.reason)
  )
}

/**
 * 接口错误 -> 中文说明。
 * 覆盖 429/409/415/413 等状态：优先用后端 detail.code，其次退回 HTTP 状态说明。
 */
export function describeApiError(error: unknown): string {
  if (error instanceof ApiError) {
    const code = error.code

    // 额度超限分「本次会话」和「全站每日」两种，提示必须能区分。
    if (code === 'quota_exceeded') {
      const scope = readDetailField(error.detail, 'scope')
      return scope === 'daily'
        ? '今日公开体验额度已用完，请明天再来。'
        : '本次匿名会话的分析次数已用完，无法再发起新的分析。'
    }
    if (code === 'active_project_exists') {
      return '当前会话已经有一个进行中的项目，请刷新页面继续使用它。'
    }
    if (code === 'media_limit_reached') {
      return '每个项目最多 3 张图片，请换一个项目或先完成当前图片。'
    }
    if (code !== null) {
      const label = ERROR_CODE_LABELS[code]
      if (label !== undefined) {
        return `${label}（${code}）`
      }
      // 未知错误码 + 5xx：这是服务端故障，不能说成「请求被拒绝」（那是 4xx 的语义）。
      if (error.status >= 500) {
        return `服务暂时不可用（HTTP ${error.status}，${code}），请稍后重试。`
      }
      return `请求被拒绝（HTTP ${error.status}，${code}）。`
    }

    switch (error.status) {
      case 401:
        return '匿名会话已失效，请刷新页面重新开始。'
      case 404:
        return '请求的资源不存在，可能已被归档或不属于当前会话。'
      case 413:
        return '上传内容过大（HTTP 413）。'
      case 415:
        return '文件格式不被支持（HTTP 415）。'
      case 422:
        return '提交的内容不符合要求（HTTP 422）：地点名称不能为空，地址与地区也有长度上限，请检查后重试。'
      case 429:
        return '请求过于频繁或额度已用完（HTTP 429）。'
      case 500:
      case 502:
      case 503:
        return `服务暂时不可用（HTTP ${error.status}），请稍后重试。`
      default:
        return `请求失败（HTTP ${error.status}）。`
    }
  }

  if (error instanceof Error && error.message !== '') {
    return `网络请求失败：${error.message}`
  }
  return '发生未知错误。'
}

/**
 * 发送消息失败 -> 中文说明。
 *
 * 复用 describeApiError（额度、404、错误码映射都已覆盖），只替换 422 的文案：
 * 通用文案讲的是地点确认字段，用在消息上会误导用户。
 */
export function describeMessageSendError(error: unknown): string {
  if (error instanceof ApiError && error.status === 422 && error.code === null) {
    return '这条消息没有通过服务端校验（HTTP 422）：内容需要 1–2000 字，请修改后重试。'
  }
  return describeApiError(error)
}

/** 旅行（项目）状态：backend/app/api/projects.py 里只用到 active / archived 两种。 */
const PROJECT_STATUS_LABELS: LabelMap = {
  active: '进行中',
  archived: '已归档',
}

export function projectStatusLabel(status: string): string {
  return mapLabel(PROJECT_STATUS_LABELS, status)
}

/**
 * 账号相关请求失败 -> 中文说明。
 *
 * 与 describeApiError 分开的原因：401 在别处表示「匿名会话失效」，在账号接口上表示
 * 「需要登录 / 登录已失效」，一句话讲错会直接误导用户去刷新页面。这里按后端的
 * detail.code 优先映射，其次才按 HTTP 状态给出确定性的说明。
 *
 * 契约：docs/productization/D2-账户接口契约.md
 *   register/login 401 invalid_credentials；register 409 email_taken；
 *   注册/改密/重置 422 password_too_weak；认证接口 429 限流（IP + 邮箱）；
 *   未登录访问需要登录的接口 401 auth_required；令牌无效 400 invalid_token。
 */
export function describeAuthError(error: unknown): string {
  if (error instanceof ApiError) {
    const code = error.code
    if (code === 'invalid_credentials') {
      return '邮箱或密码不正确。为不泄露账号是否存在，后端对「邮箱不存在」和「密码错误」返回同一个错误。'
    }
    if (code === 'email_taken') {
      return '这个邮箱已经注册过了。可以直接用「登录」，或改用「忘记密码」重设密码。'
    }
    if (code === 'password_too_weak') {
      return '密码没有通过后端校验：至少 10 位，建议混用字母、数字与符号。'
    }
    if (code === 'invalid_token') {
      return '令牌无效或已过期。请重新申请一次验证/重置邮件，或在下面重新粘贴新的令牌。'
    }
    if (code === 'auth_required') {
      return '这个操作需要先登录（HTTP 401）：当前未登录，或登录会话已被撤销/过期。'
    }
    if (code === 'too_many_attempts' || code === 'rate_limited' || code === 'too_many_requests') {
      // 后端把等待秒数放在 detail.message（"retry_after=123"）里，并带 Retry-After 头；
      // 读到就如实告诉用户要等多久，读不到就只说「稍后再试」。
      const retryAfter = /retry_after=(\d+)/.exec(readDetailField(error.detail, 'message') ?? '')
      return retryAfter === null
        ? '操作太频繁，已被后端限流（按 IP + 邮箱计数）。请等一会儿再试。'
        : `操作太频繁，已被后端限流：请在约 ${retryAfter[1]} 秒后再试（按 IP + 邮箱计数）。`
    }
    if (code === 'auth_disabled') {
      return '这个部署关闭了账号功能（auth_enabled=false，HTTP 503）：注册、登录、找回密码、重发验证邮件当前都不可用。'
    }
    if (code === 'session_not_found') {
      return '这个登录会话不存在，或不属于当前账号（可能已经被撤销）。'
    }
    if (code === 'project_not_found') {
      return '这个工作区不存在，或不属于当前访客会话（可能已经被认领过或已归档）。'
    }
    if (code !== null) {
      const label = ERROR_CODE_LABELS[code]
      return label === undefined
        ? `请求被拒绝（HTTP ${error.status}，${code}）。`
        : `${label}（${code}）`
    }

    switch (error.status) {
      case 400:
        return '请求被后端拒绝（HTTP 400）：多半是令牌无效或已过期，请重新申请。'
      case 401:
        return '需要登录，或登录状态已失效（HTTP 401）。请重新登录后再试。'
      case 403:
        return '服务器拒绝了这个操作（HTTP 403）。'
      case 404:
        return '请求的资源不存在，或不属于当前账号（HTTP 404）。'
      case 409:
        return '和服务器上已有的数据冲突（HTTP 409）：这个邮箱可能已经注册过。'
      case 422:
        return '提交的内容没有通过后端校验（HTTP 422）：邮箱需要是合法地址，密码至少 10 位。'
      case 429:
        return '操作太频繁，已被后端限流（HTTP 429）：注册、登录、忘记密码、重发验证邮件都按 IP + 邮箱计数，请稍后再试。'
      case 500:
      case 502:
      case 503:
        return `账号服务暂时不可用（HTTP ${error.status}），请稍后重试。`
      default:
        return `请求失败（HTTP ${error.status}）。`
    }
  }
  return describeApiError(error)
}

/** 请求失败时同时展示给用户的原始错误码（没有就返回 null）。 */
export function authErrorCode(error: unknown): string | null {
  return error instanceof ApiError ? error.code : null
}
