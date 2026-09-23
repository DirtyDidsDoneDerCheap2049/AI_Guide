/**
 * 后端 API 的 TypeScript 契约。
 *
 * 字段名与 backend/app/schemas.py 完全一致（snake_case），不做驼峰转换：
 * 转换层会让「接口对不上」这类错误更难定位，这里保持一一对应，改后端字段时编译器会直接报错。
 */

// --------------------------------------------------------------------- 会话

/** GET /api/v1/session */
export interface SessionOut {
  id: string
  runs_started: number
  session_run_limit: number
  daily_runs_used: number
  daily_run_limit: number
}

// --------------------------------------------------------------------- 项目

/** POST /api/v1/projects 请求体（ProjectCreate，extra="forbid"） */
export interface ProjectCreateRequest {
  title: string
  city_hint?: string | null
}

/** ProjectOut */
export interface ProjectOut {
  id: string
  title: string
  city_hint: string | null
  status: string
  version: number
  media_count: number
  created_at: string
  updated_at: string
}

// --------------------------------------------------------------------- 媒体

/** MediaAssetOut */
export interface MediaAssetOut {
  id: string
  project_id: string
  position: number
  status: string
  mime_type: string
  size_bytes: number
  width: number | null
  height: number | null
  original_filename: string
  /** 后端下发的同源相对路径，直接用于 <img src> 或 CSS url()。 */
  content_url: string
  created_at: string
  /**
   * 用户笔记（D4）：只能由用户通过 PATCH /media/{id} 写入，模型结果不会覆盖它。
   * 为空表示用户没有写过笔记。
   */
  note: string | null
  /** 媒体版本：媒体自身的写操作（例如笔记）会让它 +1，用于条件更新。 */
  version: number
  /** true = 已被移出相册（软删除）。快照不再返回它，只能在「已移出」列表里看到。 */
  deleted: boolean
  /** 当前活动运行 id（没有进行中的运行时为 null）。 */
  active_run_id: string | null
}

/** MediaUploadResponse */
export interface MediaUploadResponse {
  media: MediaAssetOut
  run: RunOut | null
}

/**
 * MediaOperationResponse（D4 统一响应形状）：
 * `PATCH /media/{id}`、`POST /media/{id}/analyze`、`POST /media/{id}/place`、
 * `DELETE /media/{id}`、`POST /media/{id}/restore`、`POST /media/{id}/runs/retry` 都返回它。
 * 只有 `media` 一定有；`place` / `run` 视操作而定（没有就是 null）。
 */
export interface MediaOperationResponse {
  media: MediaAssetOut
  place: PlaceOut | null
  run: RunOut | null
}

/** PATCH /api/v1/media/{media_id} 请求体（extra="forbid"，note 最长 2000）。 */
export interface MediaNoteRequest {
  /** null = 清空笔记（不是"不修改"）。 */
  note: string | null
}

/**
 * POST /api/v1/media/{media_id}/place 请求体（extra="forbid"）。
 *
 * `expected_version` 是地点版本（PlaceOut.version）的条件更新：
 * 与服务器不一致时返回 409 place_version_conflict，不会覆盖别人的改动。
 * `regenerate=true` 时后端会同时新建一个重新生成讲解的运行（返回在 run 字段）。
 */
export interface MediaPlaceChangeRequest {
  name: string
  address?: string | null
  region?: string | null
  expected_version?: number | null
  regenerate?: boolean
}

// --------------------------------------------------------------------- 候选与地点

/** CandidateOut */
export interface CandidateOut {
  id: string
  rank: number
  name: string
  address: string | null
  region: string | null
  /** 视觉依据：画面中支持该判断的线索。 */
  rationale: string
  confidence: number | null
  uncertainty: string | null
  status: string
  run_id: string
}

/** PlaceOut：用户确认后的地点与地点服务返回的基础字段。 */
export interface PlaceOut {
  id: string
  name: string
  address: string | null
  region: string | null
  latitude: number | null
  longitude: number | null
  provider: string
  provider_place_id: string | null
  query_at: string | null
  confirmed_by: string
  /**
   * 地点身份是否经过供应商核实（backend/app/services/place_match.py）。
   * 只有 `matched` / `matched_name_only` / `user_selected` 算「已核实」；
   * 其余（pending / ambiguous / conflict / no_result / provider_error …）都不允许当作事实使用
   * （未核实的地点后端不会返回地址与坐标，这里也不能拿它去收藏）。
   */
  match_status: string
  /** 匹配结论的补充说明（例如歧义、城市冲突的原因）。 */
  match_note: string | null
  /** 地点来源：user = 用户填写，amap = 供应商。 */
  source: string
  version: number
}

/**
 * GuideCardOut.place_facts（后端由地点 Provider 结果组装）。
 * 注意：这是「供应商事实」，与模型生成内容必须分区展示。
 */
export interface GuideCardPlaceFacts {
  provider?: string | null
  provider_place_id?: string | null
  address?: string | null
  region?: string | null
  latitude?: number | null
  longitude?: number | null
  query_at?: string | null
  payload?: Record<string, unknown> | null
}

/** GuideCardSection */
export interface GuideCardSection {
  heading: string
  body: string
}

/** GuideCardOut */
export interface GuideCardOut {
  id: string
  run_id: string
  title: string
  summary: string
  sections: GuideCardSection[]
  tips: string[]
  place_facts: GuideCardPlaceFacts | null
  model_name: string
  /** true = 地点服务不可用，卡片不含供应商事实。 */
  partial: boolean
  created_at: string
  /**
   * true = 这张讲解基于**旧地点**（用户改过地点）：内容保留但已过期，
   * 界面上必须明确标注，不能当现状使用（D4）。
   */
  stale: boolean
  /** 过期的原因（后端写明：地点已改为「X」（地点版本 N））。 */
  stale_reason: string | null
}

// --------------------------------------------------------------------- 运行

/** RunOut */
export interface RunOut {
  thinking_level?: string | null
  model_id?: string | null
  model_label?: string | null
  thinking?: boolean | null
  id: string
  project_id: string
  /** 图片任务的媒体 ID；工作区问答（message 链路）不带图片，这里是 null。 */
  media_asset_id: string | null
  status: string
  current_step: string
  trigger: string
  attempt: number
  error_code: string | null
  error_message: string | null
  used_steps: number
  used_tool_calls: number
  used_tokens: number
  used_cost: number
  budget_max_steps: number
  budget_max_tool_calls: number
  budget_max_tokens: number
  budget_max_cost: number
  created_at: string
  started_at: string | null
  finished_at: string | null
}

/** ConfirmPlaceRequest（extra="forbid"） */
export interface ConfirmPlaceRequest {
  decision: 'confirm' | 'correct' | 'reject'
  candidate_id?: string | null
  name?: string | null
  address?: string | null
  region?: string | null
}

/** CreateRunResponse（POST /runs/{id}/retry） */
export interface CreateRunResponse {
  run_id: string
  status: string
  media_asset_id: string | null
  project_id: string
  idempotent_replay: boolean
}

// --------------------------------------------------------------------- 持久对话

/** 消息角色：用户提问、助手回答、系统提示。 */
export type MessageRole = 'user' | 'assistant' | 'system'

/** 消息状态：QUEUED=已入库但回答还没生成，READY=已就绪，FAILED=没有生成成功。 */
export type MessageStatus = 'QUEUED' | 'READY' | 'FAILED'

/**
 * MessageOut（D1-h 持久对话）。
 *
 * 与 RunOut 的关系：一条用户消息会创建一个非图片 AgentRun（`run_id`），
 * 回答由 Worker 在同一个 run 里写入下一条 assistant 消息；
 * 因此「回答是否还在进行」要看这个 run，而不是猜。
 */
export interface MessageOut {
  thinking_level?: string | null
  model_id?: string | null
  model_label?: string | null
  thinking?: boolean | null
  version: number
  edited_at: string | null
  deleted_at: string | null
  stale: boolean
  attachments?: {
    resources?: TravelResource[]
    places?: GuidePlace[]
    followups?: string[]
    route_change?: { ok: boolean; message: string; route_id?: string } | null
    search_errors?: string[]
  } | null
  id: string
  /** 项目内消息序号（递增，服务器权威）。 */
  seq: number
  role: MessageRole
  intent: string | null
  status: MessageStatus
  content: string
  /** 提问范围：非空 = 针对这些照片提问；空 = 整个工作区。 */
  media_ids: string[]
  place_ids: string[]
  run_id: string | null
  error_code: string | null
  created_at: string
}

/** POST /api/v1/projects/{project_id}/messages 请求体（extra="forbid"）。 */
export interface CreateMessageRequest {
  thinking_level?: string
  model_id?: string
  thinking?: boolean
  /** 1..2000 字，后端会复核。 */
  content: string
  /** 选定某张照片时带上它的 ID（照片范围提问）；否则 null（整个工作区）。 */
  media_asset_id: string | null
  intent: 'answer_question'
}

/** POST /api/v1/projects/{project_id}/messages 响应（201）。 */
export interface CreateMessageResponse {
  message: MessageOut
  run: RunOut
  /** true = 命中幂等键，后端返回了原有消息与运行，没有重复扣额度。 */
  idempotent_replay: boolean
}

/** GET /api/v1/projects/{project_id}/messages 响应。 */
export interface MessageListOut {
  messages: MessageOut[]
  /** 项目内最大消息序号（不是本次返回的最大值）。 */
  last_seq: number
}

// --------------------------------------------------------------------- 快照

/** MediaSnapshot */
export interface MediaSnapshot {
  media: MediaAssetOut
  candidates: CandidateOut[]
  place: PlaceOut | null
  card: GuideCardOut | null
  runs: RunOut[]
  active_run: RunOut | null
}

/** ProjectSnapshot：页面渲染的唯一业务事实来源。 */
export interface ProjectSnapshot {
  project: ProjectOut
  media: MediaSnapshot[]
  /** 诊断用：项目内最新事件的数据库主键，不能当游标。 */
  last_event_id: number
  /** B7 事件游标：项目内事件序号（提交顺序一致），断线补发从这里继续。 */
  last_event_seq: number
  /** D1-h：最近 30 条持久消息（按 seq 升序），刷新页面后对话从里恢复。 */
  messages: MessageOut[]
  /** 项目内最大消息序号：增量拉取（after_seq）的起点。 */
  last_message_seq: number
}

// --------------------------------------------------------------------- 账号（D2）

/**
 * UserOut（docs/productization/D2-账户接口契约.md）。
 *
 * 注册/登录/me/verify-email 都返回这一个结构，字段名与后端完全一致。
 * `email_verified` 为 false 时**不阻断使用**：界面只做提示与重发，不假装已验证。
 */
export interface UserOut {
  id: string
  email: string
  display_name: string | null
  email_verified: boolean
  created_at: string
}

/** POST /auth/register 请求体；claim_project_id 为空表示不顺带认领访客工作区。 */
export interface RegisterRequest {
  email: string
  password: string
  display_name?: string | null
  claim_project_id?: string | null
}

/** POST /auth/login 请求体。 */
export interface LoginRequest {
  email: string
  password: string
  claim_project_id?: string | null
}

/** POST /auth/register（201）与 POST /auth/login（200）的响应。 */
export interface AuthSessionResponse {
  user: UserOut
  /** 本次请求真的完成了认领时是项目 ID；没有认领、或认领没成功时是 null。 */
  claimed_project_id: string | null
  /** true = 后端要求先验证邮箱（界面提示 + 重发，不阻断使用）。 */
  email_verification_required: boolean
  /**
   * 后端在注册成功时是否真的把验证邮件发出去了（契约 2 节：发信失败不影响注册事务，
   * 但会在响应里给 email_sent: false）。缺失时不代表失败，界面不会据此说邮件已发出。
   */
  email_sent?: boolean | null
}

/** GET /auth/me -> 200。 */
export interface AuthMeResponse {
  user: UserOut
}

/** POST /auth/verify-email 请求体。 */
export interface VerifyEmailRequest {
  token: string
}

/**
 * 「状态串」类响应：POST /auth/logout、/auth/verify-email/resend、/auth/password/forgot、
 * /auth/password/reset、/auth/password/change、DELETE /auth/sessions/{id}。
 *
 * 形状对齐后端 schemas.AccountStatusOut（`status` + `sessions_revoked`），并兼容契约里提到的
 * `email_sent`。界面只按 status 说事实：例如重发验证邮件可能是 sent / send_failed /
 * already_verified，绝不能一律显示「邮件已发出」。
 */
export interface AuthStatusResponse {
  status: string
  /** 本次动作撤销了多少个会话（重置密码、改密码、退出登录会用到）。 */
  sessions_revoked?: number
  email_sent?: boolean | null
}

/** POST /auth/password/reset 请求体（令牌来自邮件里的重置链接）。 */
export interface PasswordResetRequest {
  token: string
  new_password: string
}

/** POST /auth/password/change 请求体（已登录改密码）。 */
export interface PasswordChangeRequest {
  current_password: string
  new_password: string
}

/** GET /auth/sessions 里的单条登录会话（不是匿名会话，见上面的 SessionOut）。 */
export interface AuthSessionOut {
  id: string
  created_at: string
  expires_at: string
  last_seen_at: string | null
  /** true = 就是当前这次请求所用的会话。 */
  current: boolean
  user_agent: string | null
}

/** GET /auth/sessions -> 200。 */
export interface AuthSessionListOut {
  sessions: AuthSessionOut[]
}

/** POST /auth/claim 请求体。 */
export interface ClaimRequest {
  project_id: string
}

/** POST /auth/claim -> 200（幂等：重复认领返回同一条认领记录）。 */
export interface ClaimResponse {
  project_id: string
  /** 本次请求是否真的写入了归属。 */
  claimed: boolean
  /** true = 之前已经认领过（幂等命中）。 */
  already_claimed: boolean
}

/** GET /trips 里的单条旅行（只含列表需要的字段）。 */
export interface TripOut {
  id: string
  title: string
  city_hint: string | null
  status: string
  version: number
  media_count: number
  created_at: string
  updated_at: string
}

/** GET /trips -> 200（登录 = 账号下全部；访客 = 当前访客工作区）。 */
export interface TripListOut {
  trips: TripOut[]
}

// --------------------------------------------------------------------- 收藏与路线（D3-b）
// 契约：docs/productization/D3b-收藏与路线接口契约.md（前缀 /api/v1）。

/**
 * 地点检索的单条候选（GET /projects/{id}/places/search）。
 * 候选只是候选：采纳与否由用户决定，界面不能把第一条当成"就是这里"。
 */
export interface PlaceSearchItem {
  photos?: { url: string; title: string; source: string }[]
  rank: number
  name: string
  address: string | null
  region: string | null
  latitude: number | null
  longitude: number | null
  provider: string
  provider_place_id: string | null
  /** 与查询词的关系：exact / same_city / name_only / other（后端 resolve_place_match 给出）。 */
  match_kind: string
}

export type GuidePlace = Omit<PlaceSearchItem, 'rank' | 'match_kind'> & { fixture?: boolean }

export interface TravelResource {
  title: string
  kind: 'guide' | 'video'
  source: string
  url: string
  is_search: boolean
  published_at?: string | null
  reviewed_at?: string
}
export interface DiscoveryResult {
  city: string
  query: string
  places: GuidePlace[]
  resources: TravelResource[]
  fetched_at: string
  cached: boolean
  fixture: boolean
  error: string | null
}

/** PlaceSearchOut：`fixture=true` 表示这是测试数据，不是真实地图结果。 */
export interface PlaceSearchOut {
  query: string
  provider: string
  candidates: PlaceSearchItem[]
  fixture: boolean
}

/**
 * SavedPlaceOut：工作区里的一条收藏。
 * `source` = photo（由照片上已核实的地点加入）/ manual（手工填写）。
 * 手工收藏的 `provider="user"`、`provider_place_id=null`。
 */
export interface SavedPlaceOut {
  id: string
  project_id: string
  place_id: string | null
  media_asset_id: string | null
  name: string
  address: string | null
  region: string | null
  latitude: number | null
  longitude: number | null
  provider: string
  provider_place_id: string | null
  source: string
  note: string | null
  /** 工作区内的展示顺序（服务器分配）。 */
  position: number
  created_at: string
}

export interface SavedPlaceListOut {
  saved_places: SavedPlaceOut[]
}

/**
 * SavedPlaceCreateRequest（extra="forbid"）。
 * 二选一：手工填写（name 必填），或引用照片上已确认的地点（media_asset_id，
 * 此时名字/坐标/来源以照片上的地点为准，未核实的地点后端返回 409 place_not_verified）。
 */
export interface SavedPlaceCreateRequest {
  name: string
  address?: string | null
  region?: string | null
  latitude?: number | null
  longitude?: number | null
  /** 供应商名字（默认 "user"=手工填写）；从检索结果收藏时带上真实供应商。 */
  provider?: string | null
  /** 供应商 POI ID：后端的去重键优先用它（`poi:<id>`），手工填写时留空。 */
  provider_place_id?: string | null
  note?: string | null
  media_asset_id?: string | null
}

/** RouteStop：草稿里的一个停留点（最多 20 个）。没有坐标的停留点算不了路。 */
export interface RouteStop {
  saved_place_id?: string | null
  name: string
  latitude?: number | null
  longitude?: number | null
}

/** RouteLegOut：分段结果（距离/时长/折线都来自供应商）。 */
export interface RouteLegOut {
  from: number
  to: number
  distance_meters: number
  duration_seconds: number
  /** 该段的折线：`"lng,lat;lng,lat;…"`（GCJ-02）。 */
  polyline: string
  provider?: string
}

/**
 * RouteRevisionOut：一次算路的修订记录。
 *
 * 只有 `is_current=true` 的那条代表"当前路线"：`status=OK`、未被标记过期、
 * 且 `input_version` 与草稿当前输入一致。其余都是历史，不能当现状展示。
 */
export interface RouteRevisionOut {
  id: string
  draft_id: string
  mode: string
  status: string
  /** 供应商名字：`amap-route` = 真实高德；`fixture-route` = 测试数据。 */
  provider: string
  distance_meters: number | null
  duration_seconds: number | null
  legs: RouteLegOut[]
  /** 全程折线：`"lng,lat;lng,lat;…"`（GCJ-02，直接给地图 SDK 用）。 */
  geometry: string | null
  error_code: string | null
  error_message: string | null
  stale: boolean
  computed_at: string
  is_current: boolean
  input_version: number
}

/**
 * RouteDraftOut：路线草稿。
 * `stops` 是用户资产——算路失败、结果过期都不会动它。
 * `route_unavailable_reason` 是界面唯一可以据以说明"为什么没有路线"的字段：
 *   null | "not_computed" | "compute_failed:<code>" | "stale_after_edit"
 */
export interface RouteDraftOut {
  id: string
  project_id: string
  name: string
  mode: string
  stops: RouteStop[]
  /** 输入版本：任何改动（模式/顺序/停留点/名字）都会 +1，旧修订因此过期。 */
  input_version: number
  current_revision: RouteRevisionOut | null
  latest_revision: RouteRevisionOut | null
  route_unavailable_reason: string | null
  created_at: string
  updated_at: string
}

export interface RouteDraftListOut {
  drafts: RouteDraftOut[]
}

/** RouteDraftCreateRequest（extra="forbid"）。 */
export interface RouteDraftCreateRequest {
  name: string
  mode: 'walking' | 'driving'
  stops?: RouteStop[]
}

/**
 * RouteDraftUpdateRequest（extra="forbid"）。
 * 带 `expected_version` 时，若草稿在别处已被改动，后端返回 409 draft_version_conflict。
 */
export interface RouteDraftUpdateRequest {
  name?: string | null
  mode?: 'walking' | 'driving' | null
  stops?: RouteStop[] | null
  expected_version?: number | null
}

/** GET /projects/{id}/routes/{draft_id}/revisions 的响应（含失败与过期记录）。 */
export interface RouteRevisionListOut {
  draft_id: string
  input_version: number
  revisions: RouteRevisionOut[]
}

// --------------------------------------------------------------------- SSE

/** SSE 帧：后端 api/events.py 里 data 为 {"payload": {...}, "created_at": "..."}。 */
export interface WorkspaceEventFrame {
  id: number
  type: string
  payload: Record<string, unknown>
  created_at: string
}
