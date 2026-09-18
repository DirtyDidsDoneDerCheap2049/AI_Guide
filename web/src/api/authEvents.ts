/**
 * 业务层 ↔ 账号层之间的两根转发线（都只转发，不持有状态）。
 *
 * 为什么单独一个小模块：401 `auth_required` 与「工作区 404」都可能出现在任何业务请求上
 * （快照刷新、打开旅行、上传、发消息、SSE 订阅前的探测……），而这些请求都发生在
 * useWorkspace 里；真正要做的处置（清空私有数据、切回未登录、提示重新登录）属于 useAuth。
 * 让两边互相 import 会形成循环依赖，所以这里只放两个没有状态的转发点：
 *
 *   useWorkspace（检测到 401）    --notifyAuthRequired-->   useAuth（决定怎么处置）
 *   useWorkspace（工作区 404）    --notifyWorkspaceMissing--> useAuth（先复核 /auth/me 再决定）
 *
 * 订阅者各只允许有一个（useAuth 在模块加载时注册），重复注册会覆盖上一个。
 * 两个函数的返回值都是「账号层接管了吗」：接管了就由账号层负责提示，业务层不再多写一条
 * 可能互相矛盾的提示（例如同一时刻既说「登录已失效」又说「工作区已被归档」）。
 */

export type AuthRequiredListener = (code: string | null) => boolean

/**
 * 工作区请求返回 404 时的复核回调。
 *
 * 契约里 404 的含义是「不存在或不属于你」，但当**本地以为已登录**时，404 也可能是
 * 「登录会话已经被撤销/过期，于是这个工作区不再属于当前身份」。两者对用户的意义完全不同
 * （一个要重新登录、一个只是换工作区），所以由账号层去问一次 `GET /auth/me` 来区分。
 * 返回 true = 已经确认登录失效并完成处置（清空私有数据、提示重新登录）。
 */
export type WorkspaceMissingListener = () => Promise<boolean>

let listener: AuthRequiredListener | null = null
let missingListener: WorkspaceMissingListener | null = null

/** 注册 401 处置函数（重复调用会覆盖上一个，避免热更新时叠加多个）。 */
export function onAuthRequired(next: AuthRequiredListener): void {
  listener = next
}

/**
 * 业务请求收到 401 时调用；`code` 是后端 detail.code（可能为 null）。
 * 返回 true = 账号层已接管（调用方不要再补一条提示）。
 */
export function notifyAuthRequired(code: string | null): boolean {
  return listener === null ? false : listener(code)
}

/** 注册「工作区 404 复核」函数（同样只保留最后一个订阅者）。 */
export function onWorkspaceMissing(next: WorkspaceMissingListener): void {
  missingListener = next
}

/**
 * 工作区请求收到 404 时调用。
 * 返回 true = 账号层确认「登录会话已失效」并已完成处置（调用方不要再补自己的提示、
 * 也不要再按「工作区不可用」处理）；返回 false = 这就是一个普通的 404（未登录，或登录仍然有效）。
 */
export async function notifyWorkspaceMissing(): Promise<boolean> {
  return missingListener === null ? false : await missingListener()
}
