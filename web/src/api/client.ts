/**
 * 后端 HTTP 客户端。
 *
 * 约定：
 * - 只用原生 fetch，只请求同源相对路径 `/api/...`，不拼接任何主机名（生产由 Caddy 反代）。
 * - 所有请求显式带 `credentials: 'same-origin'`：GET /api/v1/session 会在响应中下发
 *   HttpOnly 匿名会话 Cookie，后续项目、上传、取图和 SSE 都依赖这个 Cookie。
 * - 后端错误统一形如 `{"detail": {"code": "..."}}` 或 `{"detail": "code"}`
 *   （见 backend/app/api/*.py 的 HTTPException），这里归一成 ApiError。
 */

import type {
  AuthMeResponse,
  AuthSessionListOut,
  AuthSessionResponse,
  AuthStatusResponse,
  ClaimRequest,
  ClaimResponse,
  ConfirmPlaceRequest,
  CreateMessageRequest,
  CreateMessageResponse,
  CreateRunResponse,
  LoginRequest,
  MediaAssetOut,
  MediaNoteRequest,
  MediaOperationResponse,
  MediaPlaceChangeRequest,
  MediaUploadResponse,
  MessageListOut,
  MessageOut,
  DiscoveryResult,
  PasswordChangeRequest,
  PasswordResetRequest,
  PlaceSearchOut,
  ProjectCreateRequest,
  ProjectOut,
  ProjectSnapshot,
  RegisterRequest,
  RouteDraftCreateRequest,
  RouteDraftListOut,
  RouteDraftOut,
  RouteDraftUpdateRequest,
  RouteRevisionListOut,
  RunOut,
  SavedPlaceCreateRequest,
  SavedPlaceListOut,
  SavedPlaceOut,
  SessionOut,
  TripListOut,
  UserOut,
  VerifyEmailRequest,
} from './types'

const API_PREFIX = '/api/v1'

export function discoverCity(city: string, query = ''): Promise<DiscoveryResult> {
  return request(`${API_PREFIX}/discovery?${new URLSearchParams({ city, q: query })}`)
}
export function editMessage(
  project: string,
  message: MessageOut,
  content: string,
): Promise<MessageOut> {
  return request(`${API_PREFIX}/projects/${project}/messages/${message.id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content, expected_version: message.version }),
  })
}
export function deleteMessage(project: string, message: MessageOut): Promise<MessageOut> {
  return request(
    `${API_PREFIX}/projects/${project}/messages/${message.id}?expected_version=${message.version}`,
    { method: 'DELETE' },
  )
}
export function searchMessages(project: string, query: string): Promise<MessageListOut> {
  return request(
    `${API_PREFIX}/projects/${project}/messages?${new URLSearchParams({ q: query, limit: '200' })}`,
  )
}
export function olderMessages(project: string, before: number): Promise<MessageListOut> {
  return request(`${API_PREFIX}/projects/${project}/messages?before_seq=${before}&limit=50`)
}

/** 归一化后的接口错误；code 用于中文提示映射。 */
export class ApiError extends Error {
  readonly status: number
  readonly code: string | null
  readonly detail: unknown

  constructor(status: number, code: string | null, detail: unknown) {
    super(code ? `HTTP ${status} ${code}` : `HTTP ${status}`)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.detail = detail
  }
}

/** detail 可能是字符串或对象，两种形态都取 code。 */
function extractDetailCode(detail: unknown): string | null {
  if (typeof detail === 'string') {
    return detail
  }
  if (detail !== null && typeof detail === 'object' && 'code' in detail) {
    const code = (detail as { code?: unknown }).code
    return typeof code === 'string' ? code : null
  }
  return null
}

async function toApiError(response: Response): Promise<ApiError> {
  let detail: unknown = null
  try {
    const body: unknown = await response.json()
    if (body !== null && typeof body === 'object' && 'detail' in body) {
      detail = (body as { detail: unknown }).detail
    }
  } catch {
    // 非 JSON 错误体（例如代理返回的 502 页面）不阻断错误处理。
    detail = null
  }
  return new ApiError(response.status, extractDetailCode(detail), detail)
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  if (!headers.has('Accept')) {
    headers.set('Accept', 'application/json')
  }

  const response = await fetch(path, {
    ...init,
    credentials: 'same-origin',
    headers,
  })

  if (!response.ok) {
    throw await toApiError(response)
  }
  if (response.status === 204) {
    return undefined as T
  }
  return (await response.json()) as T
}

/** 从错误 detail 里读一个附加字段（例如 quota_exceeded 的 scope）。 */
export function readDetailField(detail: unknown, field: string): string | null {
  if (detail === null || typeof detail !== 'object' || !(field in detail)) {
    return null
  }
  const value = (detail as Record<string, unknown>)[field]
  return typeof value === 'string' ? value : null
}

/**
 * 允许「响应体不存在或不是 JSON」的请求：成功时返回解析到的响应体（拿不到就是 null）。
 *
 * 用在契约没规定响应体、或响应体只是个状态串的接口上（POST /auth/logout、
 * DELETE /auth/sessions/{id}）：HTTP 成功就是成功，绝不因为「响应体不是 JSON」
 * 把一次真实的退出登录 / 撤销会话显示成失败。
 */
async function requestMaybe<T>(path: string, init: RequestInit = {}): Promise<T | null> {
  const headers = new Headers(init.headers)
  if (!headers.has('Accept')) {
    headers.set('Accept', 'application/json')
  }
  const response = await fetch(path, { ...init, credentials: 'same-origin', headers })
  if (!response.ok) {
    throw await toApiError(response)
  }
  if (response.status === 204) {
    return null
  }
  try {
    return (await response.json()) as T
  } catch {
    return null
  }
}

/** POST/DELETE + JSON 请求体的简写（认证接口几乎都是这个形状）。 */
function sendJson<T>(path: string, method: 'POST' | 'DELETE' | 'PATCH', body: unknown): Promise<T> {
  return request<T>(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

// --------------------------------------------------------------------- 会话

/** 取得/创建匿名会话（必须先调用，Cookie 才会下发）。 */
export function getSession(): Promise<SessionOut> {
  return request<SessionOut>(`${API_PREFIX}/session`)
}

// --------------------------------------------------------------------- 账号
// 契约：docs/productization/D2-账户接口契约.md（前缀 /api/v1）。
// 登录态由 HttpOnly 的 ai_guide_auth Cookie 承载，前端拿不到也存不了令牌。

/** POST /auth/register（201）：注册成功即登录；可同时认领访客工作区。 */
export function registerAccount(body: RegisterRequest): Promise<AuthSessionResponse> {
  return sendJson<AuthSessionResponse>(`${API_PREFIX}/auth/register`, 'POST', body)
}

/** POST /auth/login（200）：401 invalid_credentials 表示邮箱或密码不对。 */
export function loginAccount(body: LoginRequest): Promise<AuthSessionResponse> {
  return sendJson<AuthSessionResponse>(`${API_PREFIX}/auth/login`, 'POST', body)
}

/**
 * POST /auth/logout：撤销当前会话并清 Cookie。
 * 响应体契约未规定（后端实现返回 {"status":"logged_out", ...}），这里能读到就返回，读不到也不影响成功判定。
 */
export function logoutAccount(): Promise<AuthStatusResponse | null> {
  return requestMaybe<AuthStatusResponse>(`${API_PREFIX}/auth/logout`, { method: 'POST' })
}

/** GET /auth/me：未登录时 401 auth_required（调用方按「未登录」处理）。 */
export function getMe(): Promise<AuthMeResponse> {
  return request<AuthMeResponse>(`${API_PREFIX}/auth/me`)
}

/** POST /auth/verify-email：令牌无效或过期 -> 400 invalid_token。 */
export function verifyEmail(body: VerifyEmailRequest): Promise<{ user: UserOut }> {
  return sendJson<{ user: UserOut }>(`${API_PREFIX}/auth/verify-email`, 'POST', body)
}

/**
 * POST /auth/verify-email/resend：重发验证邮件（按 IP + 邮箱限流）。
 *
 * 契约没有给这个接口定义请求体（邮箱取自登录态），所以这里**不发请求体**：
 * 多传字段在 extra="forbid" 的模型上会变成 422。
 */
export function resendVerificationEmail(): Promise<AuthStatusResponse> {
  return request<AuthStatusResponse>(`${API_PREFIX}/auth/verify-email/resend`, { method: 'POST' })
}

/** POST /auth/password/forgot（202）：永远 202，不泄露邮箱是否存在。 */
export function requestPasswordReset(email: string): Promise<AuthStatusResponse> {
  return sendJson<AuthStatusResponse>(`${API_PREFIX}/auth/password/forgot`, 'POST', { email })
}

/** POST /auth/password/reset（200）：成功后后端撤销该用户全部会话。 */
export function resetPassword(body: PasswordResetRequest): Promise<AuthStatusResponse> {
  return sendJson<AuthStatusResponse>(`${API_PREFIX}/auth/password/reset`, 'POST', body)
}

/** POST /auth/password/change（200）：成功后撤销**其他**会话，当前会话保留。 */
export function changePassword(body: PasswordChangeRequest): Promise<AuthStatusResponse> {
  return sendJson<AuthStatusResponse>(`${API_PREFIX}/auth/password/change`, 'POST', body)
}

/** GET /auth/sessions：当前会话的 current 为 true。 */
export function listAuthSessions(): Promise<AuthSessionListOut> {
  return request<AuthSessionListOut>(`${API_PREFIX}/auth/sessions`)
}

/**
 * DELETE /auth/sessions/{id}：只能撤销自己的会话（别人的会话返回 404 session_not_found）。
 * 返回响应体里的 status：后端用它区分「撤销了别的会话」（revoked）和「撤销的就是当前会话」
 * （current_session_revoked），取消后者等价于退出登录。
 */
export function revokeAuthSession(sessionId: string): Promise<AuthStatusResponse | null> {
  return requestMaybe<AuthStatusResponse>(
    `${API_PREFIX}/auth/sessions/${encodeURIComponent(sessionId)}`,
    {
      method: 'DELETE',
    },
  )
}

/** POST /auth/claim：认领访客工作区（幂等）。404 project_not_found = 不属于当前访客会话。 */
export function claimGuestProject(body: ClaimRequest): Promise<ClaimResponse> {
  return sendJson<ClaimResponse>(`${API_PREFIX}/auth/claim`, 'POST', body)
}

/** GET /trips：登录 = 账号下全部旅行；访客 = 当前访客工作区。 */
export function listTrips(): Promise<TripListOut> {
  return request<TripListOut>(`${API_PREFIX}/trips`)
}

/**
 * DELETE /auth/account：停用账号（撤销全部会话 + 清 Cookie + 未用邮件令牌作废）。
 *
 * 后端语义（backend/app/services/accounts.py::cleanup_account）：用户状态置为 disabled，
 * 之后用这个邮箱登录会直接拿到 401 invalid_credentials；工作区与讲解**不做物理删除**，
 * 但 owner_user_id 被置空、归还为无人拥有。响应是
 * AccountStatusOut{status:"account_disabled", sessions_revoked}。
 * 未登录时 401 auth_required；账号功能开关关闭时 503 auth_disabled。
 */
export function deactivateAccount(): Promise<AuthStatusResponse | null> {
  return requestMaybe<AuthStatusResponse>(`${API_PREFIX}/auth/account`, { method: 'DELETE' })
}

// --------------------------------------------------------------------- 项目

/** 当前活动项目快照；没有活动项目时后端返回 404，这里归一成 null。 */
export async function getCurrentProject(): Promise<ProjectSnapshot | null> {
  try {
    return await request<ProjectSnapshot>(`${API_PREFIX}/projects/current`)
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      return null
    }
    throw error
  }
}

export function getProject(projectId: string): Promise<ProjectSnapshot> {
  return request<ProjectSnapshot>(`${API_PREFIX}/projects/${encodeURIComponent(projectId)}`)
}

export function createProject(body: ProjectCreateRequest): Promise<ProjectOut> {
  return request<ProjectOut>(`${API_PREFIX}/projects`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

export function updateProject(
  projectId: string,
  body: { title?: string; city_hint?: string },
): Promise<ProjectOut> {
  return sendJson<ProjectOut>(`${API_PREFIX}/projects/${projectId}`, 'PATCH', body)
}

/**
 * 归档项目：只把状态改成 archived 并释放「一个匿名会话一个活动项目」的唯一键，
 * 不删除任何数据。归档后才能新建下一个工作区（后端没有历史列表接口）。
 */
export function archiveProject(projectId: string): Promise<ProjectOut> {
  return request<ProjectOut>(`${API_PREFIX}/projects/${encodeURIComponent(projectId)}/archive`, {
    method: 'POST',
  })
}

// --------------------------------------------------------------------- 上传

export interface UploadMediaOptions {
  /** true = 上传后立即创建 AgentRun 开始分析。 */
  startAnalysis: boolean
  /** 幂等键：同一次上传重试不会重复创建运行。 */
  idempotencyKey?: string
}

export function uploadMedia(
  projectId: string,
  file: File,
  options: UploadMediaOptions,
): Promise<MediaUploadResponse> {
  const form = new FormData()
  form.append('file', file, file.name)
  // 后端字段是 Form(bool)，显式写 "true"/"false" 字符串，避免序列化差异。
  form.append('start_analysis', options.startAnalysis ? 'true' : 'false')

  const headers: Record<string, string> = {}
  if (options.idempotencyKey) {
    headers['Idempotency-Key'] = options.idempotencyKey
  }

  // 不手动设置 Content-Type：必须让浏览器生成 multipart boundary。
  return request<MediaUploadResponse>(
    `${API_PREFIX}/projects/${encodeURIComponent(projectId)}/media`,
    {
      method: 'POST',
      body: form,
      headers,
    },
  )
}

// --------------------------------------------------------------------- 相册操作（D4）

/**
 * PATCH /api/v1/media/{media_id}：写入用户笔记（模型不覆盖）。
 *
 * `note: null` 表示**清空**笔记（不是"不修改"）。媒体已移出相册时返回 409 media_deleted。
 */
export function updateMediaNote(
  mediaId: string,
  note: string | null,
): Promise<MediaOperationResponse> {
  const body: MediaNoteRequest = { note }
  return request<MediaOperationResponse>(`${API_PREFIX}/media/${encodeURIComponent(mediaId)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

/**
 * POST /api/v1/media/{media_id}/analyze：对已保存的照片新建分析运行（202）。
 *
 * 业务冲突是 409：`media_busy`（照片本身在分析中）、`run_in_progress`（已有未完成任务）、
 * `media_deleted`（已移出相册）；额度用完是 429 quota_exceeded。
 * 同一个 Idempotency-Key 重放返回原运行（不算冲突）——所以重试**同一次请求**时复用键，
 * 用户再次点则换新键。
 */
export function analyzeMedia(
  mediaId: string,
  idempotencyKey?: string,
): Promise<MediaOperationResponse> {
  const headers: Record<string, string> = {}
  if (idempotencyKey) {
    headers['Idempotency-Key'] = idempotencyKey
  }
  return request<MediaOperationResponse>(
    `${API_PREFIX}/media/${encodeURIComponent(mediaId)}/analyze`,
    {
      method: 'POST',
      headers,
    },
  )
}

/**
 * POST /api/v1/media/{media_id}/place：修改照片地点（照片已完成也可以改）。
 *
 * 带 `expected_version`（= 当前 PlaceOut.version）时，若别人先改过，后端返回
 * 409 `place_version_conflict`，这里原样抛出，界面必须显示真实冲突而不是假装成功。
 * `regenerate=true` 时后端会同时新建"按新地点重新生成讲解"的运行，响应里的 run 就是它。
 */
export function changeMediaPlace(
  mediaId: string,
  body: MediaPlaceChangeRequest,
): Promise<MediaOperationResponse> {
  return request<MediaOperationResponse>(
    `${API_PREFIX}/media/${encodeURIComponent(mediaId)}/place`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    },
  )
}

/**
 * DELETE /api/v1/media/{media_id}：把照片移出相册（软删除，可恢复；未完成任务会被取消）。
 * 响应里 `media.deleted=true`；照片本身与历史都还在，只是不再出现在相册与快照里。
 */
export function deleteMedia(mediaId: string): Promise<MediaOperationResponse> {
  return request<MediaOperationResponse>(`${API_PREFIX}/media/${encodeURIComponent(mediaId)}`, {
    method: 'DELETE',
  })
}

/** POST /api/v1/media/{media_id}/restore：把移出相册的照片恢复回来（200）。 */
export function restoreMedia(mediaId: string): Promise<MediaOperationResponse> {
  return request<MediaOperationResponse>(
    `${API_PREFIX}/media/${encodeURIComponent(mediaId)}/restore`,
    {
      method: 'POST',
    },
  )
}

/**
 * GET /api/v1/projects/{project_id}/media/deleted：已移出相册的照片列表。
 * 后端返回的是**裸数组**（不是 {media: [...]} 包装），所以这里直接请求数组类型。
 */
export function listDeletedMedia(projectId: string): Promise<MediaAssetOut[]> {
  return request<MediaAssetOut[]>(
    `${API_PREFIX}/projects/${encodeURIComponent(projectId)}/media/deleted`,
  )
}

/**
 * POST /api/v1/media/{media_id}/runs/retry：从这张照片最近一次失败/局部/已取消的运行重做（202）。
 *
 * 与运行级 `POST /runs/{id}/retry` 的区别：这里由后端挑"最近一次可重做的运行"，
 * 并且**允许已取消的运行**；没有可重做的运行时返回 409 no_retryable_run。
 * 终态运行本身不会复活：后端会新建一条运行（source_run_id 指向原运行）。
 */
export function retryMediaRun(
  mediaId: string,
  idempotencyKey?: string,
): Promise<MediaOperationResponse> {
  const headers: Record<string, string> = {}
  if (idempotencyKey) {
    headers['Idempotency-Key'] = idempotencyKey
  }
  return request<MediaOperationResponse>(
    `${API_PREFIX}/media/${encodeURIComponent(mediaId)}/runs/retry`,
    {
      method: 'POST',
      headers,
    },
  )
}

// --------------------------------------------------------------------- 运行

export function getRun(runId: string): Promise<RunOut> {
  return request<RunOut>(`${API_PREFIX}/runs/${encodeURIComponent(runId)}`)
}
export function confirmPlace(runId: string, body: ConfirmPlaceRequest): Promise<RunOut> {
  return request<RunOut>(`${API_PREFIX}/runs/${encodeURIComponent(runId)}/confirm-place`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

export function cancelRun(runId: string): Promise<RunOut> {
  return request<RunOut>(`${API_PREFIX}/runs/${encodeURIComponent(runId)}/cancel`, {
    method: 'POST',
  })
}

export function retryRun(runId: string, idempotencyKey?: string): Promise<CreateRunResponse> {
  const headers: Record<string, string> = {}
  if (idempotencyKey) {
    headers['Idempotency-Key'] = idempotencyKey
  }
  return request<CreateRunResponse>(`${API_PREFIX}/runs/${encodeURIComponent(runId)}/retry`, {
    method: 'POST',
    headers,
  })
}

// --------------------------------------------------------------------- 持久对话

/**
 * 发送一条工作区消息（POST /projects/{id}/messages，201）。
 *
 * `idempotencyKey` 必须由调用方提供：一次用户动作一个键；重试**同一次 HTTP 请求**
 * 才复用同一个键（复用会让后端返回原有消息与运行，不重复扣额度），
 * 用户再次点发送属于新动作，必须换新键。
 */
export function createMessage(
  projectId: string,
  body: CreateMessageRequest,
  idempotencyKey: string,
): Promise<CreateMessageResponse> {
  return request<CreateMessageResponse>(
    `${API_PREFIX}/projects/${encodeURIComponent(projectId)}/messages`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': idempotencyKey },
      body: JSON.stringify(body),
    },
  )
}

/** 按 seq 增量读取持久消息（after_seq=0 时返回最早的一页）。 */
export function listMessages(
  projectId: string,
  afterSeq: number,
  limit = 100,
): Promise<MessageListOut> {
  const query = new URLSearchParams({ after_seq: String(afterSeq), limit: String(limit) })
  return request<MessageListOut>(
    `${API_PREFIX}/projects/${encodeURIComponent(projectId)}/messages?${query.toString()}`,
  )
}

// --------------------------------------------------------------------- 收藏与路线（D3-b）
// 契约：docs/productization/D3b-收藏与路线接口契约.md（前缀 /api/v1）。

/**
 * GET /projects/{id}/places/search：按名称检索地点候选。
 *
 * 结果可能带 `fixture: true`（后端处于 fixture 模式）：那是**测试数据**，
 * 界面必须如实标注，不能当真实地图结果使用。
 * 供应商失败时后端返回 502/503（provider_timeout / provider_not_configured 等），
 * 这里原样抛出 ApiError，由界面显示真实错误码。
 */
export function searchPlaces(
  projectId: string,
  query: string,
  city?: string | null,
): Promise<PlaceSearchOut> {
  const params = new URLSearchParams({ q: query })
  if (city !== null && city !== undefined && city.trim() !== '') {
    params.set('city', city.trim())
  }
  return request<PlaceSearchOut>(
    `${API_PREFIX}/projects/${encodeURIComponent(projectId)}/places/search?${params.toString()}`,
  )
}

/** GET /projects/{id}/saved-places：收藏列表（按服务器给的 position 排序）。 */
export function listSavedPlaces(projectId: string): Promise<SavedPlaceListOut> {
  return request<SavedPlaceListOut>(
    `${API_PREFIX}/projects/${encodeURIComponent(projectId)}/saved-places`,
  )
}

/**
 * POST /projects/{id}/saved-places：收藏一个地点。
 *
 * 幂等：同一地点（同一 provider_place_id，或同名同区域）重复收藏返回**原记录**（仍 201），
 * 所以界面必须按 id 合并，不能盲目往列表里追加一行。
 * 照片上的地点未核实时返回 409 place_not_verified；照片还没有确认地点时 409 place_not_confirmed。
 */
export function createSavedPlace(
  projectId: string,
  body: SavedPlaceCreateRequest,
): Promise<SavedPlaceOut> {
  return request<SavedPlaceOut>(
    `${API_PREFIX}/projects/${encodeURIComponent(projectId)}/saved-places`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    },
  )
}

/** DELETE /projects/{id}/saved-places/{saved_place_id}：取消收藏（204）。 */
export function deleteSavedPlace(projectId: string, savedPlaceId: string): Promise<void> {
  return request<void>(
    `${API_PREFIX}/projects/${encodeURIComponent(projectId)}/saved-places/${encodeURIComponent(savedPlaceId)}`,
    { method: 'DELETE' },
  )
}

/** GET /projects/{id}/routes：路线草稿列表（含当前/最新修订与不可用原因）。 */
export function listRoutes(projectId: string): Promise<RouteDraftListOut> {
  return request<RouteDraftListOut>(
    `${API_PREFIX}/projects/${encodeURIComponent(projectId)}/routes`,
  )
}

/** POST /projects/{id}/routes：新建草稿（201）。stops 可为空，之后再加停留点。 */
export function createRoute(
  projectId: string,
  body: RouteDraftCreateRequest,
): Promise<RouteDraftOut> {
  return request<RouteDraftOut>(`${API_PREFIX}/projects/${encodeURIComponent(projectId)}/routes`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

/**
 * PATCH /projects/{id}/routes/{draft_id}：改模式 / 顺序 / 停留点 / 名字。
 *
 * 任何真实改动都会让 `input_version` +1 并把旧修订标记为过期（后端负责），
 * 响应里回的就是改动后的草稿——界面必须以这个响应为准，不能自己猜版本号。
 * 带上 expected_version 时，若另一个标签页已经改过，后端返回 409 draft_version_conflict。
 */
export function updateRoute(
  projectId: string,
  draftId: string,
  body: RouteDraftUpdateRequest,
): Promise<RouteDraftOut> {
  return request<RouteDraftOut>(
    `${API_PREFIX}/projects/${encodeURIComponent(projectId)}/routes/${encodeURIComponent(draftId)}`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    },
  )
}

/**
 * POST /projects/{id}/routes/{draft_id}/compute：算路。
 *
 * 后端**同步**调用供应商（高德 /v3/direction），可能耗时数秒；这是一个真实在途请求。
 * 算路失败也是 200：响应里 latest_revision.status=FAILED 且 route_unavailable_reason
 * 变成 `compute_failed:<code>`，停留点原样保留（可以重试）。
 * 输入不合法（点数不足、缺坐标）才是 422；供应商未配置是 503。
 */
export function computeRoute(projectId: string, draftId: string): Promise<RouteDraftOut> {
  return request<RouteDraftOut>(
    `${API_PREFIX}/projects/${encodeURIComponent(projectId)}/routes/${encodeURIComponent(draftId)}/compute`,
    { method: 'POST' },
  )
}

/** DELETE /projects/{id}/routes/{draft_id}：删除草稿（204，连同它的修订记录）。 */
export function deleteRoute(projectId: string, draftId: string): Promise<void> {
  return request<void>(
    `${API_PREFIX}/projects/${encodeURIComponent(projectId)}/routes/${encodeURIComponent(draftId)}`,
    { method: 'DELETE' },
  )
}

/**
 * GET /projects/{id}/routes/{draft_id}/revisions：修订历史（含失败与过期）。
 * 失败的历史条目也有 error_code，界面必须按 `is_current` 区分"当前"与"历史"。
 */
export function listRouteRevisions(
  projectId: string,
  draftId: string,
  limit = 20,
): Promise<RouteRevisionListOut> {
  const params = new URLSearchParams({ limit: String(limit) })
  return request<RouteRevisionListOut>(
    `${API_PREFIX}/projects/${encodeURIComponent(projectId)}/routes/${encodeURIComponent(draftId)}/revisions?${params.toString()}`,
  )
}
