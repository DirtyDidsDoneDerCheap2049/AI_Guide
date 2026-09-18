/**
 * SSE 订阅客户端（GET /api/v1/projects/{id}/events）。
 *
 * 为什么自己管重连：
 * 1. EventSource 自带的自动重连会遵守服务端的 `retry:` 字段，退避策略不可控；
 * 2. 服务端在 SSE_MAX_STREAM_SECONDS（默认 900s）后会主动结束长连接，这是
 *    正常行为，不是故障，客户端必须带 Last-Event-ID 重新接上并补齐断线事件；
 * 3. 断线只影响「连接状态」，绝不能推断成任务失败——任务失败只能来自
 *    run.failed / run.partial 事件。
 */

import type { WorkspaceEventFrame } from './types'

/** 与 backend/app/events.py::EventType 一一对应；漏订阅会静默丢事件，新增类型时必须同步这里。 */
export const WORKSPACE_EVENT_TYPES = [
  'project.created',
  'media.uploaded',
  'run.queued',
  'run.started',
  'step.started',
  'step.finished',
  'candidate.ready',
  'run.waiting_user',
  'place.confirmed',
  'place.rejected',
  // D4 相册操作：改地点后旧讲解被标记过期，媒体笔记/移出/恢复都会改快照
  'place.changed',
  'media.updated',
  'media.deleted',
  'media.restored',
  'card.ready',
  // D3-b 收藏与路线：这些事件来自真实写操作（本标签页或另一个标签页），
  // 收到后要把收藏列表 / 路线草稿重新对齐一次服务器状态。
  'place.saved',
  'place.unsaved',
  'route.draft_created',
  'route.draft_updated',
  'route.computed',
  // D1-h 持久对话：收到任何一个都要按 seq 增量重取一次消息（事件只带 ID，不带正文）。
  'message.created',
  'message.ready',
  'message.failed',
  'message.updated',
  'message.deleted',
  'project.updated',
  'run.partial',
  'run.failed',
  'run.cancelled',
  'run.retry_queued',
] as const

/** 连接通道状态（不代表任务状态）。 */
export type ConnectionState = 'connecting' | 'open' | 'reconnecting' | 'stopped'

/** 停止重连的原因：只有这两种情况才允许放弃订阅。 */
export type StreamStopReason = 'project_unavailable' | 'session_expired'

/** 指数退避：1s → 2s → 4s → 8s → 15s（上限 15s，之后一直用 15s）。 */
const RECONNECT_DELAYS_MS = [1000, 2000, 4000, 8000, 15000] as const

export interface ProjectEventStreamOptions {
  projectId: string
  /** 续订起点：页面加载时用快照里的 last_event_seq（项目内事件序号）。 */
  initialLastEventId: number
  onEvent: (frame: WorkspaceEventFrame) => void
  onState: (state: ConnectionState, reason?: StreamStopReason) => void
  /**
   * 一次性判定订阅是否已彻底不可用（项目归档 / 会话过期）。
   * EventSource 拿不到 HTTP 状态码，所以只在「致命断开」时探测一次；
   * 这不是轮询：没有间隔、没有重复调用。
   */
  probeUnavailable?: () => Promise<StreamStopReason | null>
}

export interface ProjectEventStream {
  close: () => void
  currentLastEventId: () => number
}

export function openProjectEventStream(options: ProjectEventStreamOptions): ProjectEventStream {
  const { projectId, onEvent, onState, probeUnavailable } = options

  let lastEventId = options.initialLastEventId
  let source: EventSource | null = null
  let retryTimer: number | null = null
  let attempt = 0
  let closed = false

  function clearRetryTimer(): void {
    if (retryTimer !== null) {
      window.clearTimeout(retryTimer)
      retryTimer = null
    }
  }

  function handleFrame(eventType: string, message: MessageEvent<string>): void {
    let parsed: unknown
    try {
      parsed = JSON.parse(message.data)
    } catch {
      // 心跳是注释行（不会触发 message 事件），能走到这里的只可能是坏帧，丢弃即可。
      return
    }
    if (parsed === null || typeof parsed !== 'object') {
      return
    }

    const id = Number.parseInt(message.lastEventId, 10)
    if (!Number.isFinite(id)) {
      // 没有 id 就无法续订，后端每帧都带 id，这里直接忽略这种异常帧。
      return
    }

    const raw = parsed as { payload?: unknown; created_at?: unknown }
    const payload =
      raw.payload !== null && typeof raw.payload === 'object'
        ? (raw.payload as Record<string, unknown>)
        : {}
    const createdAt = typeof raw.created_at === 'string' ? raw.created_at : new Date().toISOString()

    lastEventId = Math.max(lastEventId, id)
    onEvent({ id, type: eventType, payload, created_at: createdAt })
  }

  function connect(): void {
    if (closed) {
      return
    }

    // 显式把游标放进查询参数：EventSource 只在自身自动重连时才回填 Last-Event-ID 头，
    // 我们接管了重连，就必须自己带上游标（last_event_seq；后端也接受 last_event_id 字段名）。
    const url = `/api/v1/projects/${encodeURIComponent(projectId)}/events?last_event_seq=${lastEventId}`
    onState(attempt === 0 ? 'connecting' : 'reconnecting')

    // 同源 EventSource 会自动携带会话 Cookie（withCredentials 只对跨域生效），
    // 因此这里不需要也不应该配置任何跨域凭据。
    const next = new EventSource(url)
    source = next

    next.onopen = (): void => {
      if (closed || source !== next) {
        return
      }
      attempt = 0
      onState('open')
    }

    for (const eventType of WORKSPACE_EVENT_TYPES) {
      next.addEventListener(eventType, (event: Event): void => {
        handleFrame(eventType, event as MessageEvent<string>)
      })
    }

    next.onerror = (): void => {
      void handleDisconnect(next)
    }
  }

  async function handleDisconnect(failed: EventSource): Promise<void> {
    if (closed || source !== failed) {
      return
    }
    // 浏览器在「响应不是 200 / 不是 text/event-stream」这类致命错误时会把
    // readyState 置为 CLOSED；网络抖动和 900s 正常收流则是 CONNECTING。
    const fatal = failed.readyState === EventSource.CLOSED

    // 必须显式 close()：否则 EventSource 会按服务端 retry: 自行重连，
    // 我们就失去了「带 last_event_id 的指数退避」这一可控行为。
    failed.close()
    source = null

    // 这里的措辞很关键：断线 = 重连中，绝不显示任务失败。
    onState('reconnecting')

    if (fatal && probeUnavailable) {
      let reason: StreamStopReason | null = null
      try {
        reason = await probeUnavailable()
      } catch {
        reason = null
      }
      if (closed) {
        return
      }
      if (reason !== null) {
        onState('stopped', reason)
        return
      }
    }

    const delay = RECONNECT_DELAYS_MS[Math.min(attempt, RECONNECT_DELAYS_MS.length - 1)]
    attempt += 1
    clearRetryTimer()
    // 整个页面里唯一的定时器：SSE 重连退避。业务状态不靠任何定时器刷新。
    retryTimer = window.setTimeout(() => {
      retryTimer = null
      connect()
    }, delay)
  }

  connect()

  return {
    close: (): void => {
      closed = true
      clearRetryTimer()
      if (source !== null) {
        source.close()
        source = null
      }
    },
    currentLastEventId: (): number => lastEventId,
  }
}
