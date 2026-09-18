/**
 * 工作台状态与动作。
 *
 * 页面结构（本轮重做的信息结构）：
 *   - 探索首页（view='explore'）：一句用途说明 + 问题输入框 + 添加照片 + 3 个示例主题；
 *     有活动工作区时额外展示「继续上次」。首屏不展开空地图、空相册、空活动记录。
 *   - 导游工作页（view='work'）：左侧持久对话 + 运行诊断（当前任务/候选/事件流），
 *     右侧 地图 / 相册 / 卡片 / 收藏 / 路线 五个标签；切标签不丢输入、选中照片与地图视野。
 *
 * 数据流刻意做得很窄，便于验收「业务状态只来自接口与用户操作后的显式刷新」：
 *   1. 页面加载：GET /session（拿匿名 Cookie）-> GET /projects/current（快照渲染，
 *      含最近 30 条持久消息）-> 用快照里的 last_event_seq 订阅 SSE；
 *   2. SSE 事件到达：追加活动记录 + 触发一次快照刷新；消息事件（message.*）另外按
 *      after_seq 增量重取消息（事件只有 ID，正文只存在于 MySQL）；
 *   3. 用户操作（创建/上传/发问/确认/取消/重试/归档）：调用接口后显式刷新一次快照。
 * 全程没有任何定时轮询；唯一的定时器是 events.ts 里的 SSE 重连退避。
 *
 * 对话的「乐观显示」有明确边界：只有用户自己刚发的那条会先以本地条目（outbox）出现，
 * 并标注「发送中」；服务器响应一到就用服务器返回的 seq/status 覆盖，失败则保留原文与
 * 真实错误，绝不把没有发出的内容伪装成已发送，也绝不用内存里的临时列表冒充对话历史。
 */

import { computed, reactive } from 'vue'

import {
  analyzeMedia as analyzeMediaRequest,
  ApiError,
  cancelRun,
  changeMediaPlace as changeMediaPlaceRequest,
  confirmPlace,
  createMessage as createMessageRequest,
  createProject as createProjectRequest,
  deleteMedia as deleteMediaRequest,
  getCurrentProject,
  getProject,
  getRun,
  getSession,
  listDeletedMedia as listDeletedMediaRequest,
  listMessages,
  restoreMedia as restoreMediaRequest,
  retryMediaRun as retryMediaRunRequest,
  updateMediaNote as updateMediaNoteRequest,
  uploadMedia,
  updateProject,
  editMessage as editMessageRequest,
  deleteMessage as deleteMessageRequest,
  olderMessages,
} from '../api/client'
import {
  openProjectEventStream,
  type ConnectionState,
  type ProjectEventStream,
  type StreamStopReason,
} from '../api/events'
import { notifyAuthRequired, notifyWorkspaceMissing } from '../api/authEvents'
import { usePlacesRoutes } from './usePlacesRoutes'
import type {
  ConfirmPlaceRequest,
  MediaAssetOut,
  MediaPlaceChangeRequest,
  MediaSnapshot,
  MessageOut,
  ProjectSnapshot,
  RunOut,
  SessionOut,
  WorkspaceEventFrame,
} from '../api/types'
import {
  describeApiError,
  describeEvent,
  describeMessageSendError,
  eventErrorCode,
  mediaStatusLabel,
  runStatusLabel,
  triggerLabel,
} from '../utils/labels'

/** 与后端 settings.media_max_per_project 保持一致（客户端先拦一次，后端仍会复核）。 */
export const MAX_MEDIA_PER_PROJECT = 3
/** 与后端 settings.media_max_bytes 保持一致（8MB）。 */
export const MAX_UPLOAD_BYTES = 8 * 1024 * 1024
/** 与后端 settings.media_allowed_mime 保持一致。 */
export const ALLOWED_MIME_TYPES = ['image/jpeg', 'image/png', 'image/webp'] as const
/** 没有输入问题时，用这个标题创建工作区（后端要求标题非空，最长 200）。 */
export const DEFAULT_WORKSPACE_TITLE = '我的旅行'
/** 与后端 CreateMessageRequest.content 的 max_length 保持一致（1..2000）。 */
export const MAX_MESSAGE_LENGTH = 2000
/** 消息增量拉取的分页大小，与后端 GET /messages 的默认 limit 一致。 */
const MESSAGE_PAGE_LIMIT = 100
/** 单次增量刷新最多翻几页，避免后端异常时无限循环。 */
const MESSAGE_PAGE_MAX = 5
/** 对话记录最多保留多少条（防止长时间运行后内存无上限增长）。 */
const MESSAGE_LIMIT = 500

export type WorkspaceView = 'explore' | 'work'
/** 已移出相册列表的加载状态（独立于快照：它只能单独读）。 */
export type DeletedMediaPhase = 'idle' | 'loading' | 'ready' | 'error'
/** 用户笔记保存状态。 */
export type NoteSaveStatus = 'idle' | 'saving' | 'saved' | 'error'
export type RightTab = 'map' | 'album' | 'card' | 'saved' | 'routes' | 'research'
export type WorkspacePhase = 'loading' | 'ready' | 'error'
/** 提问范围：photo=当前选中的照片，workspace=整个工作区。 */
export type ComposerScope = 'photo' | 'workspace'

/** 活动记录条目：真实 SSE 事件与本地提示分开标记，界面不会把本地提示伪装成事件。 */
export interface ActivityEntry {
  id: number
  type: string
  text: string
  created_at: string
  error_code: string | null
  source: 'sse' | 'local'
}

/**
 * 本地发送条目（outbox）：用户点了发送、但服务器还没确认的那一条。
 *
 * 它只在「还没拿到服务器响应」和「发送失败但文本必须留着」这两种情况下存在；
 * 一旦 POST 成功返回，就用服务器返回的消息替换它，绝不让本地条目长期充当历史。
 */
export interface OutboxEntry {
  /** 本地唯一 ID（与幂等键一样使用 randomUUID），也是界面上的 key。 */
  clientId: string
  content: string
  /** 发送时的提问范围：null = 整个工作区。 */
  mediaId: string | null
  /** 发送时的范围文案（照片范围会记下文件名，发送后仍能看出这条问的是什么）。 */
  scopeLabel: string
  status: 'sending' | 'failed'
  errorText: string | null
  errorCode: string | null
  created_at: string
}

export interface Notice {
  kind: 'error' | 'info'
  text: string
  code: string | null
}

export interface UploadProgress {
  index: number
  total: number
  filename: string
}

/**
 * 「在地图上看」的跳转请求。
 *
 * 两种目标：
 *  - 快照里已确认的地点（placeId，坐标由地点服务给出）；
 *  - 收藏地点的坐标（longitude/latitude，GCJ-02，直接来自收藏记录）。
 * nonce 用来让地图组件区分「再次点同一个目标」；label 是给用户看的说明（可为 null）。
 */
export interface MapFocusRequest {
  placeId: string | null
  longitude: number | null
  latitude: number | null
  label: string | null
  nonce: number
}

const ACTIVITY_LIMIT = 300

const state = reactive({
  phase: 'loading' as WorkspacePhase,
  view: 'explore' as WorkspaceView,
  rightTab: 'map' as RightTab,
  /** 1024px 等窄桌面下左侧对话区的展开状态（宽屏由 CSS 强制展开）。 */
  leftOpen: false,
  bootstrapError: null as string | null,
  session: null as SessionOut | null,
  snapshot: null as ProjectSnapshot | null,
  selectedMediaId: null as string | null,
  connection: 'stopped' as ConnectionState,
  connectionReason: null as StreamStopReason | null,
  activity: [] as ActivityEntry[],
  notice: null as Notice | null,
  uploading: null as UploadProgress | null,
  pending: null as string | null,
  /** 输入框草稿（切标签、切照片、收起对话区都不丢）。 */
  draftQuestion: '',
  destination: '杭州',
  researchQuery: '',
  /** 输入框自己的一句提示（例如「先写一句想问的」）；不用于伪造对话。 */
  composerHint: null as string | null,
  /**
   * 持久对话消息：来自快照、after_seq 增量接口与发送响应，永远以服务器数据为准。
   * 界面不会用内存里的临时列表顶替它。
   */
  messages: [] as MessageOut[],
  /** 已合并的最大消息 seq（增量拉取游标）。 */
  messageCursor: 0,
  /** 尚未被服务器确认 / 发送失败的用户消息。 */
  outbox: [] as OutboxEntry[],
  /** 是否有一次发送在途（同一时刻只允许发一条，避免多条问题互相穿插）。 */
  sendingMessage: false,
  /** 输入框的提问范围选择；没有照片时实际发送范围会自动回落到整个工作区。 */
  composerScope: 'workspace' as ComposerScope,
  /** 消息关联的运行状态（按 run_id），用于「等待回答」指示器；只从接口读取。 */
  messageRuns: {} as Record<string, RunOut>,
  /** 地图需要聚焦的地点（来自相册/卡片里的「在地图上看」）。 */
  mapFocus: null as MapFocusRequest | null,
  /** 可重试上传的文件名；为 null 时不显示重试入口。 */
  uploadRetryName: null as string | null,
  /**
   * D4：已移出相册的照片。
   *
   * 快照**不会**再返回被移出的照片（后端 `MediaAsset.deleted_at` 过滤），所以这份列表只能
   * 从 `GET /projects/{id}/media/deleted` 单独读；界面在用户展开「已移出相册」时才读第一次。
   */
  deletedMedia: {
    phase: 'idle' as DeletedMediaPhase,
    items: [] as MediaAssetOut[],
    errorText: null as string | null,
    errorCode: null as string | null,
  },
  /**
   * D4：用户笔记的保存状态（只记当前这一次操作，按 media_id 匹配）。
   * 草稿本身留在组件里（视图状态），这里只保存"请求的真实结果"。
   */
  noteSave: {
    mediaId: null as string | null,
    status: 'idle' as NoteSaveStatus,
    errorText: null as string | null,
    errorCode: null as string | null,
    /** 服务器确认保存的时间（本地时钟，仅用于"刚才保存成功"这类提示）。 */
    savedAt: null as string | null,
  },
  /**
   * D4：地点表单提交后的真实结果（含"新任务是否入队"），供照片详情与卡片面板显示。
   * null = 还没有提交过。
   */
  lastPlaceChange: null as {
    mediaId: string
    placeName: string
    placeVersion: number
    /** regenerate=true 且后端真的建了运行时，这里是新运行的 id。 */
    regenerateRunId: string | null
    /** 这一次是否要求了 regenerate（与 regenerateRunId 分开：要求了不等于后端建了）。 */
    regenerateRequested: boolean
  } | null,
})

let stream: ProjectEventStream | null = null
/** 已处理的最大事件 ID：重连补发会重复投递，靠它去重（事件 ID 单调递增）。 */
let lastAppliedEventId = 0
let refreshInFlight: Promise<void> | null = null
let refreshQueued = false
let messagesInFlight: Promise<void> | null = null
let messagesQueued = false
let mapFocusNonce = 0
/** 已经查过（或正在查）状态的 run_id：同一个 run 只查一次，不做任何轮询。 */
const requestedRunIds = new Set<string>()
/** 上传失败时保留 File 对象（只在内存里），供「重试上传」使用。 */
let failedUpload: { file: File; filename: string } | null = null

/**
 * 收藏与路线（D3-b）的状态挂在同一个工作区上：这里只做两件事——
 * 快照落地时把工作区 id 交给它（切换/归档会清空，避免串数据），
 * 以及把收藏/路线的事件转给它做一次服务器对齐。
 */
const placesRoutes = usePlacesRoutes()

const project = computed(() => state.snapshot?.project ?? null)
const mediaList = computed<MediaAssetOut[]>(
  () => state.snapshot?.media.map((item) => item.media) ?? [],
)
const mediaSnapshots = computed<MediaSnapshot[]>(() => state.snapshot?.media ?? [])
const selectedMedia = computed<MediaSnapshot | null>(() => {
  if (state.snapshot === null || state.selectedMediaId === null) {
    return null
  }
  return state.snapshot.media.find((item) => item.media.id === state.selectedMediaId) ?? null
})
const remainingSlots = computed(() => Math.max(0, MAX_MEDIA_PER_PROJECT - mediaList.value.length))
/** 等待用户确认的运行（左侧面板与窄屏提示共用）。 */
const waitingRun = computed<{ mediaId: string; run: RunOut } | null>(() => {
  for (const item of mediaSnapshots.value) {
    const run =
      item.active_run ?? item.runs.find((entry) => entry.status === 'WAITING_USER') ?? null
    if (run !== null && run.status === 'WAITING_USER') {
      return { mediaId: item.media.id, run }
    }
  }
  return null
})
/** 实际生效的提问范围：选了「这张照片」但当前没有照片时，只能按整个工作区提问。 */
const composerScopeTarget = computed<ComposerScope>(() =>
  state.composerScope === 'photo' && selectedMedia.value !== null ? 'photo' : 'workspace',
)

function codeOf(error: unknown): string | null {
  return error instanceof ApiError ? error.code : null
}

/**
 * 工作区请求返回 404 时的**唯一**处理出口（不要在各处复制这段判断）。
 *
 * 契约里 404 = 「不存在或不属于你」。但当本地以为自己已登录时，404 还可能是
 * 「登录会话被撤销/过期，于是这个工作区不再属于当前身份」——两种情况对用户的意义完全不同
 * （一个要重新登录，一个只是换工作区）。所以这里请账号层去复核一次 `GET /auth/me`：
 *
 *   返回 true  = 账号层确认登录已失效，并且已经清空私有数据、提示重新登录；
 *                调用方应当直接 return，不要再写自己的「工作区不可用」提示。
 *   返回 false = 这就是一个普通 404（未登录，或登录仍然有效），调用方按原样处理。
 */
async function handleMissingProject(error: unknown): Promise<boolean> {
  if (!(error instanceof ApiError) || error.status !== 404) {
    return false
  }
  return await notifyWorkspaceMissing()
}

/**
 * 业务请求收到 401 时，把「这次请求需要登录」这件事转给 useAuth（见 api/authEvents.ts）。
 *
 * 为什么不在这一层直接清数据：401 有两种含义 —— 匿名会话失效（访客数据，界面自己提示
 * 就行）和登录会话被撤销/过期（账号数据，必须立刻清空并提示重新登录）。哪种含义只有
 * useAuth 知道（它持有登录状态），所以这里只负责如实上报。
 *
 * 返回 true = 账号层已经接管这次 401（提示由它来写），调用方不要再补一条提示。
 */
function noteAuthRequired(error: unknown): boolean {
  if (error instanceof ApiError && error.status === 401) {
    return notifyAuthRequired(error.code)
  }
  return false
}

function setNotice(kind: Notice['kind'], text: string, code: string | null = null): void {
  state.notice = { kind, text, code }
}

function pushActivity(entry: ActivityEntry): void {
  state.activity.push(entry)
  if (state.activity.length > ACTIVITY_LIMIT) {
    state.activity.splice(0, state.activity.length - ACTIVITY_LIMIT)
  }
}

function localActivity(text: string): void {
  pushActivity({
    id: 0,
    type: 'local.note',
    text,
    created_at: new Date().toISOString(),
    error_code: null,
    source: 'local',
  })
}

function applySnapshot(snapshot: ProjectSnapshot): void {
  state.snapshot = snapshot
  // 工作区唯一入口：收藏与路线在这里绑定/换绑（同 id 是空操作）。
  placesRoutes.bindWorkspace(snapshot.project.id)
  const ids = snapshot.media.map((item) => item.media.id)
  if (state.selectedMediaId === null || !ids.includes(state.selectedMediaId)) {
    // 刷新或重建连接后要回到「等待用户确认」的那张图，用户才能接着操作。
    const waiting = snapshot.media.find((item) => item.media.status === 'WAITING_USER')
    state.selectedMediaId = waiting?.media.id ?? ids[0] ?? null
  }
  // 快照里的 messages 是最近 30 条（升序）：合并而不是替换，避免把已经增量取到的
  // 更早消息丢掉；游标取「已知最大 seq」与快照 last_message_seq 的较大值。
  mergeMessages(snapshot.messages)
  state.messageCursor = Math.max(state.messageCursor, snapshot.last_message_seq)
}

// ------------------------------------------------------------------ 对话消息

/**
 * 合并服务器消息。
 *
 * 规则：以 id 去重，服务器返回的版本永远覆盖本地缓存（seq/status 都以服务器为准），
 * 最后按 seq 升序排列。任何消息都只能通过这里进入 state.messages —— 本地乐观条目
 * 留在 outbox 里，不会混进对话历史。
 */
function mergeMessages(incoming: MessageOut[]): void {
  if (incoming.length === 0) {
    return
  }
  const byId = new Map<string, MessageOut>()
  for (const message of state.messages) {
    byId.set(message.id, message)
  }
  for (const message of incoming) {
    const known = byId.get(message.id)
    if (!known || (known.version ?? 1) <= (message.version ?? 1)) byId.set(message.id, message)
  }
  const merged = [...byId.values()].sort((left, right) => left.seq - right.seq)
  state.messages =
    merged.length > MESSAGE_LIMIT ? merged.slice(merged.length - MESSAGE_LIMIT) : merged

  for (const message of incoming) {
    state.messageCursor = Math.max(state.messageCursor, message.seq)
  }
  // 状态仍是 QUEUED 的消息：它的运行到底结束没有，只有 run 接口知道。
  for (const message of incoming) {
    if (message.status === 'QUEUED' && message.run_id !== null) {
      void ensureRunStatus(message.run_id)
    }
  }
}

/**
 * 查一次某个运行的权威状态（GET /runs/{id}）。
 *
 * 为什么需要它：消息接口只给 run_id，运行状态不在消息字段里；而「等待回答」是不是
 * 还在进行、失败的回答属于哪次运行，都必须来自服务器，不能靠猜。同一个 run 只查一次
 * （`force` 用于事件明确告知状态已变化时再查一次），没有任何轮询。
 */
async function ensureRunStatus(runId: string, force = false): Promise<void> {
  if (!force && (state.messageRuns[runId] !== undefined || requestedRunIds.has(runId))) {
    return
  }
  requestedRunIds.add(runId)
  try {
    const run = await getRun(runId)
    state.messageRuns[runId] = run
  } catch {
    // 读不到运行状态不影响消息本身的展示：界面会退回到消息自己的 status 文案。
  }
}

/**
 * 按 after_seq 增量拉取消息（打开页面后补齐、SSE 消息事件到达后核对）。
 * 分页上限很小：正常情况下一次就够，上限只是防止后端异常时死循环。
 */
async function fetchMessages(): Promise<void> {
  const projectId = state.snapshot?.project.id
  if (projectId === undefined) {
    return
  }
  try {
    for (let page = 0; page < MESSAGE_PAGE_MAX; page += 1) {
      const result = await listMessages(projectId, state.messageCursor, MESSAGE_PAGE_LIMIT)
      mergeMessages(result.messages)
      if (result.messages.length < MESSAGE_PAGE_LIMIT) {
        // 没有更多了：last_seq 是项目内最大序号，可以安全地推进游标。
        state.messageCursor = Math.max(state.messageCursor, result.last_seq)
        return
      }
    }
  } catch (error) {
    // 增量拉取失败不改动已经显示的历史（也不会清空对话）：这只是「核对」，
    // 下一条 message.* 事件或下一次用户操作会再拉一次。用错误弹条打断聊天不值得。
    // 但 401 要如实上报：登录会话可能已经被撤销。
    noteAuthRequired(error)
  }
}

/** 消息增量刷新合并：同一时刻只有一个在途请求，期间的新请求合并成下一次。 */
function scheduleMessagesRefresh(): Promise<void> {
  if (messagesInFlight !== null) {
    messagesQueued = true
    return messagesInFlight
  }
  const task = fetchMessages().finally(() => {
    messagesInFlight = null
    if (messagesQueued) {
      messagesQueued = false
      void scheduleMessagesRefresh()
    }
  })
  messagesInFlight = task
  return task
}

function closeStream(): void {
  if (stream !== null) {
    stream.close()
    stream = null
  }
}

/** 一次性探测：订阅是否已经彻底没意义（项目归档 / 会话过期）。 */
async function probeUnavailable(): Promise<StreamStopReason | null> {
  const projectId = state.snapshot?.project.id
  if (projectId === undefined) {
    return 'project_unavailable'
  }
  try {
    await getProject(projectId)
    return null
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      // SSE 拿不到状态码，这里是它唯一的判定入口：如实上报给账号层，再按「会话不可用」停订阅。
      noteAuthRequired(error)
      return 'session_expired'
    }
    if (await handleMissingProject(error)) {
      // 复核发现登录已失效：这不是「项目没了」，按会话不可用停订阅。
      return 'session_expired'
    }
    if (error instanceof ApiError && error.status === 404) {
      return 'project_unavailable'
    }
    // 网络本身不通：属于可恢复断线，继续退避重连。
    return null
  }
}

function handleEvent(frame: WorkspaceEventFrame): void {
  if (frame.id <= lastAppliedEventId) {
    return
  }
  lastAppliedEventId = frame.id

  pushActivity({
    id: frame.id,
    type: frame.type,
    text: describeEvent(frame),
    created_at: frame.created_at,
    error_code: eventErrorCode(frame),
    source: 'sse',
  })

  if (['run.cancelled', 'run.failed', 'run.partial'].includes(frame.type)) {
    for (const run of Object.values(state.messageRuns)) {
      if (['QUEUED', 'RUNNING'].includes(run.status)) void ensureRunStatus(run.id, true)
    }
  }
  if (frame.type === 'run.failed') {
    // 注意：这里的失败来自事件本身，跟连接状态无关；断线永远不算失败。
    const code = eventErrorCode(frame)
    setNotice('error', `${describeEvent(frame)}。可在左侧「当前任务」里点「重试」。`, code)
  } else if (frame.type === 'run.partial') {
    const code = eventErrorCode(frame)
    setNotice(
      'info',
      `${describeEvent(frame)}。可在左侧「当前任务」里点「重试」补齐地点信息。`,
      code,
    )
  } else if (frame.type === 'message.updated' || frame.type === 'message.deleted') {
    void reloadConversation().catch(() => setNotice('error', '对话更新暂未同步，请刷新页面重试。'))
  } else if (
    frame.type === 'message.created' ||
    frame.type === 'message.ready' ||
    frame.type === 'message.failed'
  ) {
    // 消息事件只带 ID 与序号，正文只存在于 MySQL：按 after_seq 增量重取一次。
    // message.created/ready/failed 都意味着「这个 run 的状态变了」，所以强制重查一次运行。
    const runId = readFrameString(frame, 'run_id')
    if (runId !== null) {
      void ensureRunStatus(runId, true)
    }
    if (frame.type === 'message.failed') {
      const code = eventErrorCode(frame)
      setNotice('error', '这次回答没有生成成功，可以重新提问（新的一次回答任务）。', code)
    }
    void scheduleMessagesRefresh()
  } else if (
    frame.type === 'run.started' ||
    frame.type === 'step.started' ||
    frame.type === 'step.finished'
  ) {
    // 事件载荷里没有 run_id（只有 id/type/payload），所以这里统一把「还在排队的那几条
    // 消息运行」核对一次：正在进行的提问才能从「排队中」变成「执行中」。
    void refreshPendingRunStatuses()
  }

  // D3-b：收藏/路线事件（可能来自另一个标签页）到来时，把已读过的列表对齐一次。
  placesRoutes.handleWorkspaceEvent(frame.type)

  // 事件驱动的快照刷新（不是轮询）：只在事件到达时拉一次，并发时合并。
  void scheduleSnapshotRefresh()
}

/** 从事件载荷里读一个字符串字段（字段不存在或类型不对时返回 null）。 */
function readFrameString(frame: WorkspaceEventFrame, key: string): string | null {
  const value = frame.payload[key]
  return typeof value === 'string' && value !== '' ? value : null
}

/**
 * 把仍在排队中的消息运行状态各核对一次（每个 run 最多各一次）。
 *
 * 为什么需要：run.* 事件不携带 run_id，无法直接对应到某条消息；但没有它，用户消息就会
 * 一直停在「排队中」。这里只处理「消息还没就绪、且已知状态还是 QUEUED」的运行，
 * 一旦读到 RUNNING 就不会再查——不是轮询。
 */
async function refreshPendingRunStatuses(): Promise<void> {
  for (const message of state.messages) {
    if (message.status !== 'QUEUED' || message.run_id === null) {
      continue
    }
    const known = state.messageRuns[message.run_id]
    if (known === undefined || known.status === 'QUEUED') {
      await ensureRunStatus(message.run_id, true)
    }
  }
}

function connectStream(snapshot: ProjectSnapshot): void {
  closeStream()
  lastAppliedEventId = 0
  state.connection = 'connecting'
  state.connectionReason = null
  stream = openProjectEventStream({
    projectId: snapshot.project.id,
    initialLastEventId: snapshot.last_event_seq,
    onEvent: handleEvent,
    onState: (connection, reason) => {
      state.connection = connection
      state.connectionReason = reason ?? null
    },
    probeUnavailable,
  })
}

async function refreshSnapshot(): Promise<void> {
  const projectId = state.snapshot?.project.id
  if (projectId === undefined) {
    return
  }
  try {
    const snapshot = await getProject(projectId)
    if (state.snapshot?.project.id !== projectId) return
    applySnapshot(snapshot)
  } catch (error) {
    if (noteAuthRequired(error)) {
      // 账号层已经接管（会清空私有数据并提示重新登录），这里不重复提示。
      return
    }
    if (await handleMissingProject(error)) {
      // 账号层已确认登录失效并清空私有数据，无需再回到探索首页（它已经清了）。
      return
    }
    if (error instanceof ApiError && error.status === 404) {
      // 项目不再可读（例如被归档 / 不属于当前身份）：回到探索首页，而不是留在半残状态。
      closeStream()
      state.snapshot = null
      state.selectedMediaId = null
      state.view = 'explore'
      setNotice('info', '当前工作区已不可用（可能已归档），已返回探索首页。')
      return
    }
    setNotice('error', `刷新工作区快照失败：${describeApiError(error)}`, codeOf(error))
  }
}

/** 快照刷新合并：同一时刻只允许一个在途请求，期间的新请求合并成下一次。 */
function scheduleSnapshotRefresh(): Promise<void> {
  if (refreshInFlight !== null) {
    refreshQueued = true
    return refreshInFlight
  }
  const task = refreshSnapshot().finally(() => {
    refreshInFlight = null
    if (refreshQueued) {
      refreshQueued = false
      void scheduleSnapshotRefresh()
    }
  })
  refreshInFlight = task
  return task
}

async function bootstrap(): Promise<void> {
  state.phase = 'loading'
  state.bootstrapError = null
  try {
    // 第一步必须是 session：它会在响应里下发 HttpOnly 匿名会话 Cookie。
    state.session = await getSession()
    const savedProject = new URLSearchParams(window.location.search).get('trip')
    const snapshot = savedProject ? await getProject(savedProject) : await getCurrentProject()
    state.phase = 'ready'
    if (snapshot === null) {
      // 没有活动工作区是正常状态：留在探索首页。
      state.snapshot = null
      state.view = 'explore'
      return
    }
    applySnapshot(snapshot)
    // 默认先停在探索首页：有活动工作区时展示「继续上次」，由用户决定是否进入。
    state.view = savedProject ? 'work' : 'explore'
    localActivity(
      `已从服务器快照恢复工作区「${snapshot.project.title}」（${snapshot.media.length} 张图片，事件游标 ${snapshot.last_event_seq}）`,
    )
    connectStream(snapshot)
  } catch (error) {
    if (error instanceof ApiError && [403, 404].includes(error.status)) {
      clearPrivateData()
      state.phase = 'ready'
      setNotice('error', '这次旅行无法打开。请登录原来的账号，或从下方选择另一段旅行。')
      return
    }
    state.phase = 'error'
    state.bootstrapError = describeApiError(error)
  }
}

// ------------------------------------------------------------------ 视图切换

function enterWork(): void {
  state.view = 'work'
  rememberView()
}

function openExplore(): void {
  state.view = 'explore'
  state.leftOpen = false
  rememberView()
}

function rememberView(): void {
  const url = new URL(window.location.href)
  if (state.view === 'work' && state.snapshot)
    url.searchParams.set('trip', state.snapshot.project.id)
  else url.searchParams.delete('trip')
  const next = url.pathname + url.search + url.hash
  if (next !== window.location.pathname + window.location.search + window.location.hash)
    window.history.pushState({}, '', next)
}

window.addEventListener('popstate', () => {
  void bootstrap()
})

async function newTrip(): Promise<void> {
  closeStream()
  resetConversation()
  placesRoutes.bindWorkspace(null)
  state.snapshot = null
  state.selectedMediaId = null
  state.mapFocus = null
  state.activity = []
  state.rightTab = 'map'
  state.draftQuestion = ''
  state.notice = null
  openExplore()
}

async function renameTrip(title: string): Promise<void> {
  if (!state.snapshot || !title.trim()) return
  try {
    await updateProject(state.snapshot.project.id, { title: title.trim() })
    await refreshSnapshot()
  } catch (error) {
    setNotice('error', describeApiError(error))
  }
}

function setRightTab(tab: RightTab): void {
  // 只换右侧内容：输入草稿、选中照片、地图实例都由状态/常驻组件保留。
  state.rightTab = tab
}

// ------------------------------------------------------------------ 工作区

interface EnsureResult {
  snapshot: ProjectSnapshot
  created: boolean
}

/**
 * 确保有一个活动工作区：没有就创建（POST /api/v1/projects），已有就复用。
 * 只创建空工作区，不产生任何模型调用。
 */
async function ensureWorkspace(
  title: string,
  cityHint: string | null,
): Promise<EnsureResult | null> {
  if (state.snapshot !== null) {
    return { snapshot: state.snapshot, created: false }
  }

  state.pending = 'create-project'
  const safeTitle = title.trim().slice(0, 200)
  let createdId: string | null = null
  try {
    const created = await createProjectRequest({
      title: safeTitle === '' ? DEFAULT_WORKSPACE_TITLE : safeTitle,
      city_hint: cityHint,
    })
    createdId = created.id
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) {
      // 服务端已经有活动工作区（例如另一个标签页刚创建）：改为复用，不报错。
      try {
        const existing = await getCurrentProject()
        if (existing !== null) {
          applySnapshot(existing)
          connectStream(existing)
          return { snapshot: existing, created: false }
        }
      } catch (reloadError) {
        if (!noteAuthRequired(reloadError)) {
          setNotice('error', describeApiError(reloadError), codeOf(reloadError))
        }
        return null
      }
    }
    if (!noteAuthRequired(error)) {
      setNotice('error', describeApiError(error), codeOf(error))
    }
    return null
  } finally {
    state.pending = null
  }

  try {
    // 用户操作后的显式刷新：项目刚创建，SSE 还没订阅上，必须主动拉一次。
    const snapshot = createdId ? await getProject(createdId) : await getCurrentProject()
    if (snapshot === null) {
      setNotice('error', '工作区创建后没有读到快照，请刷新页面重试。')
      return null
    }
    applySnapshot(snapshot)
    connectStream(snapshot)
    localActivity(
      `工作区「${snapshot.project.title}」已就绪。上传照片后才会创建识别任务（会产生模型调用）。`,
    )
    return { snapshot, created: true }
  } catch (error) {
    if (!noteAuthRequired(error)) {
      setNotice('error', describeApiError(error), codeOf(error))
    }
    return null
  }
}

async function startPlanning(question = '', city = state.destination): Promise<void> {
  if (state.pending || state.sendingMessage) return
  await newTrip()
  const result = await ensureWorkspace(`${city}旅行`, city)
  if (!result) return
  enterWork()
  state.rightTab = 'research'
  state.researchQuery = ''
  if (question.trim()) {
    state.draftQuestion = question.trim()
    await sendDraftQuestion()
  }
}

async function reloadConversation(): Promise<void> {
  const id = project.value?.id
  if (!id) return
  let cursor = Math.max(0, (state.messages[0]?.seq ?? 1) - 1)
  for (let page = 0; page < MESSAGE_PAGE_MAX; page++) {
    const result = await listMessages(id, cursor, MESSAGE_PAGE_LIMIT)
    if (project.value?.id !== id) return
    mergeMessages(result.messages)
    if (result.messages.length < MESSAGE_PAGE_LIMIT) return
    cursor = result.messages[result.messages.length - 1]!.seq
  }
}

async function loadOlderMessages(): Promise<void> {
  const id = project.value?.id
  if (!id || !state.messages.length) return
  try {
    const result = await olderMessages(id, state.messages[0]!.seq)
    if (project.value?.id === id) {
      // Preserve the older page the user explicitly asked to read.
      const rows = [...result.messages, ...state.messages]
      state.messages = rows.filter((m, i) => rows.findIndex((other) => other.id === m.id) === i)
    }
  } catch {
    setNotice('error', '更早的对话没有加载成功，请重试。')
  }
}

async function manageMessage(message: MessageOut, content?: string): Promise<boolean> {
  const id = project.value?.id
  if (!id) return false
  try {
    const row =
      content === undefined
        ? await deleteMessageRequest(id, message)
        : await editMessageRequest(id, message, content)
    if (project.value?.id !== id) return true
    mergeMessages([row])
    await reloadConversation()
    return true
  } catch (error) {
    const code = codeOf(error)
    const text =
      code === 'message_run_active'
        ? '请先停止正在生成的回答，再修改或删除对话。'
        : code === 'message_version_conflict'
          ? '这条对话已在其他页面修改，请刷新后重试。'
          : describeApiError(error)
    setNotice('error', text, code)
    return false
  }
}

/** 地点卡片和快捷问题：确保旅行存在，然后直接提交文字任务。 */
async function askQuestion(question: string): Promise<void> {
  if (state.pending !== null || state.sendingMessage) return
  const text = question.trim()
  if (text === '') {
    setNotice('error', '请先写一句想问的问题，或者点一个示例主题。')
    return
  }
  state.draftQuestion = text
  state.composerHint = null
  const result = await ensureWorkspace(text, null)
  if (result === null) {
    return
  }
  state.rightTab = 'map'
  enterWork()
  state.leftOpen = true
  state.notice = null
  state.composerScope = 'workspace'
  await sendDraftQuestion()
}

/**
 * 打开指定工作区（「我的旅行」列表里点某一条）。
 *
 * 与「继续上次」的区别：这里不依赖 owner_session_id 的唯一活动项目，直接用项目 ID 读快照
 * （GET /projects/{id}，后端按所有者过滤：别人的项目返回 404，不泄露存在性）。
 * 切换工作区时必须先清掉本地对话缓存，否则上一个工作区的消息会被 merge 进新工作区。
 */
async function openProject(projectId: string): Promise<boolean> {
  state.pending = 'open-project'
  try {
    const snapshot = await getProject(projectId)
    closeStream()
    resetConversation()
    state.activity = []
    state.mapFocus = null
    state.deletedMedia.phase = 'idle'
    state.deletedMedia.items = []
    state.lastPlaceChange = null
    state.rightTab = 'map'
    applySnapshot(snapshot)
    connectStream(snapshot)
    localActivity(
      `已打开工作区「${snapshot.project.title}」（${snapshot.media.length} 张图片，事件游标 ${snapshot.last_event_seq}）`,
    )
    setNotice('info', `已打开「${snapshot.project.title}」。`)
    enterWork()
    return true
  } catch (error) {
    if (noteAuthRequired(error)) {
      return false
    }
    if (await handleMissingProject(error)) {
      return false
    }
    if (error instanceof ApiError && error.status === 404) {
      setNotice(
        'error',
        '这个旅行打不开了：它不存在，或不属于当前账号/会话。可以刷新列表重试。',
        codeOf(error),
      )
      return false
    }
    setNotice('error', `打开旅行失败：${describeApiError(error)}`, codeOf(error))
    return false
  } finally {
    state.pending = null
  }
}

/**
 * 清空本地私有数据（退出登录 / 登录会话被撤销时使用）。
 *
 * 清的是**当前这次会话在浏览器里留下的东西**：工作区快照、对话消息、未发出的草稿、
 * 上传进度、活动记录与提示。服务器上的数据一行都不动 —— 退出登录只撤销会话，
 * 数据仍在账号里，重新登录就能看到。
 */
function clearPrivateData(): void {
  closeStream()
  resetConversation()
  placesRoutes.bindWorkspace(null)
  state.snapshot = null
  state.selectedMediaId = null
  state.activity = []
  state.mapFocus = null
  state.uploading = null
  state.uploadRetryName = null
  state.deletedMedia.phase = 'idle'
  state.deletedMedia.items = []
  state.deletedMedia.errorText = null
  state.deletedMedia.errorCode = null
  state.noteSave = {
    mediaId: null,
    status: 'idle',
    errorText: null,
    errorCode: null,
    savedAt: null,
  }
  state.lastPlaceChange = null
  failedUpload = null
  state.draftQuestion = ''
  state.notice = null
  state.pending = null
  state.view = 'explore'
  state.leftOpen = false
  const url = new URL(window.location.href)
  url.searchParams.delete('trip')
  window.history.replaceState(null, '', url)
  lastAppliedEventId = 0
}

/** 清空本地对话缓存（归档/切换工作区时使用；不影响服务器数据）。 */
function resetConversation(): void {
  state.messages = []
  state.messageCursor = 0
  state.outbox = []
  state.sendingMessage = false
  state.messageRuns = {}
  state.composerHint = null
  requestedRunIds.clear()
}

// ------------------------------------------------------------------ 上传

function newIdempotencyKey(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  // 非安全上下文（明文 HTTP 且非环回地址）没有 randomUUID，退化成足够唯一的字符串。
  return `idem-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}

/** 只有网络错误或 5xx 才值得重试：429/409/413/415/422 是确定性拒绝。 */
function isRetryableUploadError(error: unknown): boolean {
  if (error instanceof ApiError) {
    return error.status >= 500
  }
  return error instanceof Error
}

function splitAcceptableFiles(files: File[]): File[] {
  const accepted: File[] = []
  for (const file of files) {
    if (!ALLOWED_MIME_TYPES.includes(file.type as (typeof ALLOWED_MIME_TYPES)[number])) {
      setNotice(
        'error',
        `「${file.name}」格式不支持，只接受 JPG、PNG、WebP。`,
        'unsupported_mime_type',
      )
      continue
    }
    if (file.size > MAX_UPLOAD_BYTES) {
      setNotice('error', `「${file.name}」超过 8MB 上限。`, 'file_too_large')
      continue
    }
    accepted.push(file)
  }
  return accepted
}

/**
 * 逐张串行上传（start_analysis=true：上传即创建识别任务）。
 * 串行是刻意的：后端按项目加行锁分配 position，串行能让顺序与用户选择一致，
 * 也避免一次性打满额度检查与文件写入。
 */
async function uploadFiles(files: File[]): Promise<void> {
  const snapshot = state.snapshot
  if (snapshot === null) {
    setNotice('error', '还没有工作区，无法上传。')
    return
  }

  const accepted = splitAcceptableFiles(files)
  if (accepted.length === 0) {
    return
  }
  if (accepted.length > remainingSlots.value) {
    setNotice(
      'error',
      `每个工作区最多 ${MAX_MEDIA_PER_PROJECT} 张图片，本次只会上传前 ${remainingSlots.value} 张。`,
      'media_limit_reached',
    )
  }
  const queue = accepted.slice(0, remainingSlots.value)
  if (queue.length === 0) {
    return
  }

  state.pending = 'upload'
  state.uploadRetryName = null
  failedUpload = null
  for (let index = 0; index < queue.length; index += 1) {
    const file = queue[index]
    if (file === undefined) {
      continue
    }
    state.uploading = { index: index + 1, total: queue.length, filename: file.name }
    try {
      const response = await uploadMedia(snapshot.project.id, file, {
        startAnalysis: true,
        idempotencyKey: newIdempotencyKey(),
      })
      state.selectedMediaId = response.media.id
      state.notice = null
      await refreshSnapshot()
    } catch (error) {
      // 上传失败就停下：继续排队只会重复同一个错误（额度、上限、格式）。
      const retryable = isRetryableUploadError(error)
      if (retryable) {
        failedUpload = { file, filename: file.name }
        state.uploadRetryName = file.name
      }
      if (noteAuthRequired(error) || (await handleMissingProject(error))) {
        break
      }
      setNotice(
        'error',
        `上传「${file.name}」失败：${describeApiError(error)}${retryable ? ' 可以点「重试上传」。' : ''}`,
        codeOf(error),
      )
      break
    }
  }
  state.uploading = null
  state.pending = null
}

/** 服务端是否已经有同名同尺寸的图片（上次上传其实成功、只是响应丢了）。 */
function findSameMedia(filename: string, sizeBytes: number): MediaSnapshot | null {
  const list = state.snapshot?.media ?? []
  return (
    list.find(
      (item) => item.media.original_filename === filename && item.media.size_bytes === sizeBytes,
    ) ?? null
  )
}

/**
 * 重试上一次失败的上传。
 * 重试前先刷新快照：如果服务端已经有同名同尺寸的图片，说明上次其实成功了，
 * 就不重复上传（避免白占 3 张上限里的名额）。
 */
async function retryFailedUpload(): Promise<void> {
  const item = failedUpload
  if (item === null || state.snapshot === null) {
    return
  }
  const projectId = state.snapshot.project.id
  state.pending = 'upload'
  state.uploading = { index: 1, total: 1, filename: item.filename }
  state.notice = null
  try {
    await refreshSnapshot()
    const duplicated = findSameMedia(item.filename, item.file.size)
    if (duplicated !== null) {
      state.selectedMediaId = duplicated.media.id
      failedUpload = null
      state.uploadRetryName = null
      setNotice(
        'info',
        `服务端已经有同名同尺寸的图片「${item.filename}」，判断上次上传其实已经成功，因此没有重复上传。`,
      )
      return
    }
    const response = await uploadMedia(projectId, item.file, {
      startAnalysis: true,
      idempotencyKey: newIdempotencyKey(),
    })
    state.selectedMediaId = response.media.id
    failedUpload = null
    state.uploadRetryName = null
    await refreshSnapshot()
  } catch (error) {
    setNotice(
      'error',
      `重试上传「${item.filename}」仍然失败：${describeApiError(error)}`,
      codeOf(error),
    )
  } finally {
    state.uploading = null
    state.pending = null
  }
}

// ------------------------------------------------------------------ 照片与运行

function selectMedia(mediaId: string): void {
  state.selectedMediaId = mediaId
}

function openMediaInAlbum(mediaId: string): void {
  state.selectedMediaId = mediaId
  state.rightTab = 'album'
}

function openMediaCard(mediaId: string): void {
  state.selectedMediaId = mediaId
  state.rightTab = 'card'
}

/** 「在地图上看」：切到地图标签并把视野移到这个地点（地图组件通过 mapFocus 收到请求）。 */
function showPlaceOnMap(placeId: string, mediaId?: string): void {
  if (mediaId !== undefined) {
    state.selectedMediaId = mediaId
  }
  mapFocusNonce += 1
  state.mapFocus = { placeId, longitude: null, latitude: null, label: null, nonce: mapFocusNonce }
  state.rightTab = 'map'
}

/**
 * 按坐标把地图居中（收藏地点的「在地图上居中」）。
 * 坐标必须是收藏记录里真实存在的 GCJ-02 坐标：没有坐标的收藏在界面上就是禁用的，
 * 不会走到这里，也绝不会用 0,0 或示例城市顶上。
 */
function showCoordinatesOnMap(
  payload: {
    longitude: number
    latitude: number
    label: string | null
  },
  keepCurrentTab = false,
): void {
  mapFocusNonce += 1
  state.mapFocus = {
    placeId: null,
    longitude: payload.longitude,
    latitude: payload.latitude,
    label: payload.label,
    nonce: mapFocusNonce,
  }
  if (!keepCurrentTab) state.rightTab = 'map'
}

async function submitPlaceDecision(runId: string, payload: ConfirmPlaceRequest): Promise<void> {
  state.pending = 'confirm-place'
  try {
    await confirmPlace(runId, payload)
    state.notice = null
    // 用户操作后的显式刷新：后端已把 run 重新排入队列。
    await refreshSnapshot()
  } catch (error) {
    if (!noteAuthRequired(error) && !(await handleMissingProject(error))) {
      setNotice('error', describeApiError(error), codeOf(error))
    }
  } finally {
    state.pending = null
  }
}

async function cancelCurrentRun(runId: string): Promise<void> {
  state.pending = 'cancel-run'
  try {
    state.messageRuns[runId] = await cancelRun(runId)
    state.notice = null
    await refreshSnapshot()
  } catch (error) {
    if (!noteAuthRequired(error) && !(await handleMissingProject(error))) {
      setNotice('error', describeApiError(error), codeOf(error))
    }
  } finally {
    state.pending = null
  }
}

// ------------------------------------------------------------------ 对话输入框与发送

/** 切换提问范围（没有照片时「这张照片」不可选，直接回落到整个工作区）。 */
function setComposerScope(scope: ComposerScope): void {
  if (scope === 'photo' && selectedMedia.value === null) {
    state.composerScope = 'workspace'
    return
  }
  state.composerScope = scope
}

/** 把一条本地条目按状态更新（发送失败时保留原文，绝不丢）。 */
function updateOutbox(clientId: string, patch: Partial<OutboxEntry>): void {
  const index = state.outbox.findIndex((item) => item.clientId === clientId)
  const current = state.outbox[index]
  if (current !== undefined) {
    state.outbox[index] = { ...current, ...patch }
  }
}

/**
 * 真正发送一条消息：POST /projects/{id}/messages。
 *
 * 同一条发件箱消息重试时复用 clientId。响应丢失也不会创建第二次任务。
 * 成功后用服务器返回的 MessageOut 覆盖本地乐观条目（seq/status 以服务器为准）。
 */
async function postMessage(entry: OutboxEntry): Promise<void> {
  const projectId = state.snapshot?.project.id
  if (projectId === undefined) {
    updateOutbox(entry.clientId, {
      status: 'failed',
      errorText: '发送失败：当前没有可用的工作区，请先回到探索首页创建工作区。',
      errorCode: 'project_not_found',
    })
    return
  }

  state.sendingMessage = true
  try {
    const response = await createMessageRequest(
      projectId,
      { content: entry.content, media_asset_id: entry.mediaId, intent: 'answer_question' },
      entry.clientId,
    )
    if (state.snapshot?.project.id !== projectId) return
    // 服务器已确认：移除本地条目，再合并服务器消息（seq/status 以服务器为准）。
    state.outbox = state.outbox.filter((item) => item.clientId !== entry.clientId)
    state.composerHint = null
    mergeMessages([response.message])
    if (response.idempotent_replay) {
      setNotice('info', '这条问题已经发送成功，没有重复发送。')
    }
    // 响应里已经带了运行快照，不需要再查一次；界面立刻能显示「排队中/执行中」。
    state.messageRuns[response.run.id] = response.run
    await scheduleMessagesRefresh()
  } catch (error) {
    if (state.snapshot?.project.id !== projectId) return
    if (await handleMissingProject(error)) {
      // 登录已失效：账号层已经清空整个工作区（连这条本地条目一起），不再补提示。
      return
    }
    // 失败：保留原文与真实错误，界面上提供「重试发送」；不静默丢弃，也不假装成功。
    updateOutbox(entry.clientId, {
      status: 'failed',
      errorText: `发送失败：${describeMessageSendError(error)}`,
      errorCode: codeOf(error),
    })
    noteAuthRequired(error)
  } finally {
    state.sendingMessage = false
  }
}

/** 用一段文本构造本地条目（发送中的用户消息）。 */
function buildOutboxEntry(
  content: string,
  mediaId: string | null,
  scopeLabel: string,
): OutboxEntry {
  return {
    clientId: newIdempotencyKey(),
    content,
    mediaId,
    scopeLabel,
    status: 'sending',
    errorText: null,
    errorCode: null,
    created_at: new Date().toISOString(),
  }
}

/**
 * 输入框的「发送」（点按钮或回车都会走这里）。
 *
 * 范围规则：工作区里选中了照片且用户选择「这张照片」时带 media_asset_id（照片范围提问），
 * 否则传 null（整个工作区提问）。范围会显示在输入框上方，界面与实际发送永远一致。
 */
function sendDraftQuestion(): void {
  const text = state.draftQuestion.trim()
  if (text === '') {
    state.composerHint = '先写一句想问的，再点发送。'
    return
  }
  if (text.length > MAX_MESSAGE_LENGTH) {
    state.composerHint = `问题最长 ${MAX_MESSAGE_LENGTH} 字，请缩短后再发送。`
    return
  }
  if (state.sendingMessage) {
    state.composerHint = '上一条问题还在发送中，等它返回后再发下一条。'
    return
  }
  if (state.snapshot === null) {
    state.composerHint = '当前没有可用的工作区，请先回到探索首页创建工作区。'
    return
  }

  const media = composerScopeTarget.value === 'photo' ? selectedMedia.value : null
  const scopeLabel =
    media === null
      ? `整个工作区（${mediaList.value.length} 张照片）`
      : `第 ${media.media.position} 张照片：${media.media.original_filename}`
  const entry = buildOutboxEntry(text, media === null ? null : media.media.id, scopeLabel)

  // 先清空输入框、再把条目放进 outbox：文字立刻变成界面上一条「发送中」的消息，
  // 不会既留在输入框又出现在对话里。
  state.draftQuestion = ''
  state.composerHint = null
  state.outbox.push(entry)
  void postMessage(entry)
}

/** 重试发送失败的那条消息（新幂等键 = 一次新的发送动作）。 */
function retryOutboxMessage(clientId: string): void {
  const index = state.outbox.findIndex((item) => item.clientId === clientId)
  if (index < 0 || state.sendingMessage) {
    return
  }
  const entry = state.outbox[index] as OutboxEntry
  const resent: OutboxEntry = { ...entry, status: 'sending', errorText: null, errorCode: null }
  state.outbox[index] = resent
  void postMessage(resent)
}

/** 放弃一条发送失败的消息（显式动作，不是静默丢弃）。 */
function discardOutboxMessage(clientId: string): void {
  state.outbox = state.outbox.filter((item) => item.clientId !== clientId)
}

/**
 * 失败的回答：用同样的提问内容再发一次（新的消息、新的运行，会消耗额度）。
 * 找不到原提问时只提示，不猜内容 —— 猜出来的问题会比失败更糟。
 */
function resendQuestionForAnswer(message: MessageOut): void {
  if (state.sendingMessage) {
    state.composerHint = '上一条问题还在发送中，等它返回后再重试。'
    return
  }
  const origin = state.messages.find(
    (item) =>
      item.role === 'user' &&
      item.run_id !== null &&
      item.run_id === message.run_id &&
      item.seq < message.seq,
  )
  if (origin === undefined) {
    setNotice(
      'error',
      '找不到这条回答对应的提问内容，请在输入框里重新写一遍问题。',
      message.error_code,
    )
    return
  }
  const mediaId = origin.media_ids.length > 0 ? origin.media_ids[0] : null
  const media =
    mediaId === null
      ? null
      : (mediaSnapshots.value.find((item) => item.media.id === mediaId) ?? null)
  const scopeLabel =
    media === null
      ? `整个工作区（${mediaList.value.length} 张照片）`
      : `第 ${media.media.position} 张照片：${media.media.original_filename}`
  const entry = buildOutboxEntry(origin.content, media?.media.id ?? null, scopeLabel)
  state.outbox.push(entry)
  void postMessage(entry)
}

// ------------------------------------------------------------------ 相册操作（D4）

/**
 * 读「已移出相册」列表（GET /projects/{id}/media/deleted）。
 *
 * 只在这一个地方读：被移出的照片不在快照里，所以它必须单独取。界面在用户展开时才第一次读，
 * 之后每次移出/恢复都会重新读一次（服务器权威）。
 */
async function loadDeletedMedia(): Promise<void> {
  const projectId = state.snapshot?.project.id
  if (projectId === undefined) {
    return
  }
  state.deletedMedia.phase =
    state.deletedMedia.items.length === 0 ? 'loading' : state.deletedMedia.phase
  try {
    const items = await listDeletedMediaRequest(projectId)
    state.deletedMedia.items = items
    state.deletedMedia.phase = 'ready'
    state.deletedMedia.errorText = null
    state.deletedMedia.errorCode = null
  } catch (error) {
    state.deletedMedia.phase = 'error'
    state.deletedMedia.errorText = describeApiError(error)
    state.deletedMedia.errorCode = codeOf(error)
  }
}

/** 只在还没读过的时候读一次（展开「已移出相册」时调用）。 */
function ensureDeletedMedia(): void {
  if (state.deletedMedia.phase === 'idle') {
    void loadDeletedMedia()
  }
}

/** 已经读过就重新对齐一次（移出/恢复之后调用；没读过就不主动发请求）。 */
function refreshDeletedMediaIfLoaded(): void {
  if (state.deletedMedia.phase !== 'idle') {
    void loadDeletedMedia()
  }
}

/**
 * 保存用户笔记（PATCH /media/{id}）。
 *
 * 笔记是用户资产：模型结果不会覆盖它，这里也只把用户写的内容发上去（`null` = 清空）。
 * 失败时保留草稿在输入框里（组件负责），并把真实错误写进 `noteSave`。
 */
async function saveMediaNote(mediaId: string, note: string | null): Promise<boolean> {
  state.pending = 'save-note'
  state.noteSave = { mediaId, status: 'saving', errorText: null, errorCode: null, savedAt: null }
  try {
    const response = await updateMediaNoteRequest(mediaId, note)
    state.noteSave = {
      mediaId,
      status: 'saved',
      errorText: null,
      errorCode: null,
      savedAt: new Date().toISOString(),
    }
    state.notice = null
    // 服务器返回的 media 是权威值（note/version）；快照刷新后界面从它取值。
    localActivity(
      response.media.note === null || response.media.note === ''
        ? `第 ${response.media.position} 张照片的笔记已清空。`
        : `第 ${response.media.position} 张照片的笔记已保存（${response.media.note.length} 字）。`,
    )
    await refreshSnapshot()
    return true
  } catch (error) {
    state.noteSave = {
      mediaId,
      status: 'error',
      errorText: describeApiError(error),
      errorCode: codeOf(error),
      savedAt: null,
    }
    if (!noteAuthRequired(error) && !(await handleMissingProject(error))) {
      setNotice('error', `保存笔记失败：${describeApiError(error)}`, codeOf(error))
    }
    return false
  } finally {
    state.pending = null
  }
}

/**
 * 「重新分析这张」（POST /media/{id}/analyze，202）。
 *
 * 返回的是**已入队**的任务，不是已完成的结果：提示里必须说"已排队"，不能说"分析完成"。
 * 409 的三种冲突（media_busy / run_in_progress / media_deleted）与 429 额度都按真实错误码提示。
 */
async function analyzeMedia(mediaId: string): Promise<boolean> {
  state.pending = 'analyze-media'
  try {
    const response = await analyzeMediaRequest(mediaId, newIdempotencyKey())
    const run = response.run
    setNotice(
      'info',
      run === null
        ? '重新分析的请求已受理，但服务器没有返回新的运行记录（请刷新快照核对）。'
        : `重新分析已入队：第 ${response.media.position} 张照片的新任务状态是「${runStatusLabel(run.status)}」，不是已完成。`,
    )
    localActivity(
      `已提交「重新分析这张」：第 ${response.media.position} 张照片的新任务已入队（trigger=${run?.trigger ?? '—'}）。`,
    )
    await refreshSnapshot()
    return true
  } catch (error) {
    if (!noteAuthRequired(error) && !(await handleMissingProject(error))) {
      setNotice('error', `重新分析失败：${describeApiError(error)}`, codeOf(error))
    }
    return false
  } finally {
    state.pending = null
  }
}

/**
 * 修改照片地点（POST /media/{id}/place）。
 *
 * 一律带 `expected_version`（当前 PlaceOut.version）：另一个标签页先改过就返回
 * 409 place_version_conflict，这里显示真实冲突并刷新快照，绝不用本地值覆盖服务器。
 * `regenerate=true` 时后端会同时建一个"按新地点重新生成讲解"的运行，响应里的 run 就是它；
 * 只有真的拿到 run 才说"已入队"。
 */
async function changeMediaPlace(
  mediaId: string,
  input: { name: string; address: string | null; region: string | null; regenerate: boolean },
): Promise<boolean> {
  const item = mediaSnapshots.value.find((entry) => entry.media.id === mediaId) ?? null
  const name = input.name.trim()
  if (name === '') {
    setNotice('error', '地点名称不能为空。', 'missing_place_name')
    return false
  }
  const body: MediaPlaceChangeRequest = {
    name,
    address: input.address,
    region: input.region,
    expected_version: item?.place?.version ?? null,
    regenerate: input.regenerate,
  }
  state.pending = 'change-place'
  try {
    const response = await changeMediaPlaceRequest(mediaId, body)
    const place = response.place
    state.lastPlaceChange = {
      mediaId,
      placeName: place?.name ?? name,
      placeVersion: place?.version ?? 0,
      regenerateRunId: response.run?.id ?? null,
      regenerateRequested: input.regenerate,
    }
    const suffix =
      response.run !== null
        ? `，并已把「按新地点重新生成讲解」的任务入队（状态：${runStatusLabel(response.run.status)}）`
        : input.regenerate
          ? '，但服务器没有返回新的运行记录（请刷新快照核对）'
          : ''
    setNotice(
      'info',
      `地点已改为「${place?.name ?? name}」（地点版本 ${place?.version ?? '—'}）${suffix}。` +
        (suffix === '' ? '旧讲解已标记为过期，需要时可以点「按新地点重新生成」。' : ''),
    )
    localActivity(`第 ${response.media.position} 张照片的地点已改为「${place?.name ?? name}」。`)
    await refreshSnapshot()
    return true
  } catch (error) {
    if (!noteAuthRequired(error) && !(await handleMissingProject(error))) {
      setNotice('error', `修改地点失败：${describeApiError(error)}`, codeOf(error))
    }
    // 冲突/失败之后以服务器为准刷新一次：用户看到的地点版本必须是服务器上的。
    await refreshSnapshot()
    return false
  } finally {
    state.pending = null
  }
}

/**
 * 把照片移出相册（DELETE /media/{id}，软删除，可恢复）。
 * 后端会同时取消这张照片上未完成的任务——提示里必须说清楚，不能只说"已删除"。
 */
async function removeMedia(mediaId: string): Promise<boolean> {
  state.pending = 'delete-media'
  try {
    const response = await deleteMediaRequest(mediaId)
    setNotice(
      'info',
      `已把「${response.media.original_filename}」移出相册：内容与历史都还在，可以在「已移出相册」里恢复；` +
        '后端同时取消了这张照片上未完成的任务。',
    )
    localActivity(`第 ${response.media.position} 张照片已移出相册（可恢复）。`)
    await refreshSnapshot()
    refreshDeletedMediaIfLoaded()
    return true
  } catch (error) {
    if (!noteAuthRequired(error) && !(await handleMissingProject(error))) {
      setNotice('error', `移出相册失败：${describeApiError(error)}`, codeOf(error))
    }
    return false
  } finally {
    state.pending = null
  }
}

/** 恢复被移出的照片（POST /media/{id}/restore）。 */
async function restoreMedia(mediaId: string): Promise<boolean> {
  state.pending = 'restore-media'
  try {
    const response = await restoreMediaRequest(mediaId)
    setNotice(
      'info',
      `已把「${response.media.original_filename}」恢复到相册（当前状态：${mediaStatusLabel(response.media.status)}）。` +
        '之前被取消的任务不会自己复活，需要时可以点「重做」。',
    )
    localActivity(`已恢复第 ${response.media.position} 张照片到相册。`)
    await refreshSnapshot()
    refreshDeletedMediaIfLoaded()
    return true
  } catch (error) {
    if (!noteAuthRequired(error) && !(await handleMissingProject(error))) {
      setNotice('error', `恢复失败：${describeApiError(error)}`, codeOf(error))
    }
    return false
  } finally {
    state.pending = null
  }
}

/**
 * 从这张照片最近一次失败/局部/已取消的运行重做（POST /media/{id}/runs/retry，202）。
 * 后端挑运行、并且允许已取消的运行；没有可重做的运行时返回 409 no_retryable_run。
 */
async function retryMediaRun(mediaId: string): Promise<boolean> {
  state.pending = 'retry-media-run'
  try {
    const response = await retryMediaRunRequest(mediaId, newIdempotencyKey())
    const run = response.run
    setNotice(
      'info',
      run === null
        ? '重做的请求已受理，但服务器没有返回新的运行记录（请刷新快照核对）。'
        : `已提交重做：新的任务状态是「${runStatusLabel(run.status)}」（触发方式：${triggerLabel(run.trigger)}），不是已完成。`,
    )
    localActivity(`第 ${response.media.position} 张照片已提交重做。`)
    await refreshSnapshot()
    return true
  } catch (error) {
    if (!noteAuthRequired(error) && !(await handleMissingProject(error))) {
      setNotice('error', `重做失败：${describeApiError(error)}`, codeOf(error))
    }
    return false
  } finally {
    state.pending = null
  }
}

function dismissNotice(): void {
  state.notice = null
}

function dispose(): void {
  closeStream()
}

export function useWorkspace() {
  return {
    state,
    project,
    mediaList,
    mediaSnapshots,
    selectedMedia,
    waitingRun,
    remainingSlots,
    bootstrap,
    enterWork,
    openExplore,
    setRightTab,
    startPlanning,
    manageMessage,
    loadOlderMessages,
    newTrip,
    renameTrip,
    askQuestion,
    openProject,
    clearPrivateData,
    uploadFiles,
    retryFailedUpload,
    selectMedia,
    openMediaInAlbum,
    openMediaCard,
    showPlaceOnMap,
    showCoordinatesOnMap,
    submitPlaceDecision,
    cancelCurrentRun,
    // D4 相册操作
    loadDeletedMedia,
    ensureDeletedMedia,
    saveMediaNote,
    analyzeMedia,
    changeMediaPlace,
    removeMedia,
    restoreMedia,
    retryMediaRun,
    setComposerScope,
    sendDraftQuestion,
    retryOutboxMessage,
    discardOutboxMessage,
    resendQuestionForAnswer,
    refreshSnapshot: scheduleSnapshotRefresh,
    refreshMessages: scheduleMessagesRefresh,
    dismissNotice,
    dispose,
  }
}
