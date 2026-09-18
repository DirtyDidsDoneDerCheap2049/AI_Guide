/**
 * 账号状态与动作（D2 账户体系，契约见 docs/productization/D2-账户接口契约.md）。
 *
 * 设计边界（和仓库里其它状态一样：业务事实只来自接口）：
 *   1. 启动时用 GET /auth/me 读登录状态。**没有**「默认已登录」这种默认值：
 *      status === 'unknown' 时界面显示中性的「正在确认登录状态…」，不显示用户名，也不显示
 *      登录表单的结果；请求失败（非 401）时保持 unknown 并如实说明读不到，而不是假装已退出。
 *   2. 登录态由 HttpOnly 的 ai_guide_auth Cookie 承载：前端既拿不到也存不了令牌，
 *      所以刷新页面后唯一可信的来源就是 /auth/me。
 *   3. 退出登录、以及**检测到 401**（登录会话被撤销/过期）时，都会立刻清空本地私有数据
 *      （工作区快照、对话消息、草稿），并用一句明确的话说明会话结束了 —— 见 endSession()。
 *   4. 认领访客工作区是幂等的：界面按后端返回的 claimed / already_claimed 如实显示，
 *      失败时给出真实错误并允许直接重试。
 *   5. 「数据归属」不靠猜：未登录时看到的一律是访客数据；登录后由 GET /trips 的回答来决定
 *      当前工作区是不是账号里的（在列表里 = 账号数据，不在 = 还没保存的访客数据）。
 */

import { computed, reactive, watch } from 'vue'

import {
  ApiError,
  changePassword as changePasswordRequest,
  claimGuestProject,
  deactivateAccount as deactivateAccountRequest,
  getMe,
  listAuthSessions,
  listTrips,
  loginAccount,
  logoutAccount,
  registerAccount,
  requestPasswordReset,
  resendVerificationEmail,
  resetPassword as resetPasswordRequest,
  revokeAuthSession,
  verifyEmail as verifyEmailRequest,
} from '../api/client'
import { onAuthRequired, onWorkspaceMissing } from '../api/authEvents'
import type { AuthSessionOut, AuthSessionResponse, TripOut, UserOut } from '../api/types'
import { formatDateTime } from '../utils/format'
import { authErrorCode, describeApiError, describeAuthError } from '../utils/labels'
import { useWorkspace } from './useWorkspace'

/**
 * 密码长度下限：契约的示例把密码写成 `min-10-chars`，这里按「至少 10 位」做客户端预检，
 * 只为省掉一次必然失败的请求；真正的判定仍在后端（422 password_too_weak）。
 */
export const MIN_PASSWORD_LENGTH = 10

export type AuthStatus = 'unknown' | 'anonymous' | 'authenticated'
export type AuthForm = 'login' | 'register' | 'forgot' | 'reset'
export type TripsStatus = 'idle' | 'loading' | 'ready' | 'error'
export type SessionsStatus = 'idle' | 'loading' | 'ready' | 'error'

/**
 * 当前打开的工作区相对账号的归属（界面据此区分「访客数据」与「账号数据」）：
 *   none      没有打开任何工作区；
 *   unknown   登录状态还没确认，或账号的旅行列表还没读回来 —— 不下结论；
 *   guest     未登录看到的工作区（按契约只有原访客会话能读，换设备就没了）；
 *   claimable 已经登录，但这个工作区不在账号的旅行列表里（还没认领的访客数据）；
 *   account   已经在账号里（旅行列表里有它，或刚刚认领成功）。
 */
export type ProjectScope = 'none' | 'unknown' | 'guest' | 'claimable' | 'account'

const workspace = useWorkspace()

const state = reactive({
  status: 'unknown' as AuthStatus,
  user: null as UserOut | null,
  /** 读 /auth/me 失败时的真实原因；有值时界面说的是「读不到登录状态」，不是「未登录」。 */
  meError: null as string | null,

  /** 账号面板的展开状态与当前表单。 */
  open: false,
  form: 'login' as AuthForm,
  /** 正在进行的账号动作（用于禁用按钮，避免重复提交）。 */
  pending: null as string | null,

  /** 表单级提示：错误原文 + 后端错误码，成功/中性提示单独放 formNotice。 */
  formError: null as string | null,
  formErrorCode: null as string | null,
  formNotice: null as string | null,

  // 表单字段（切换表单不丢，失败后可以接着改）
  email: '',
  password: '',
  passwordConfirm: '',
  displayName: '',
  currentPassword: '',
  newPassword: '',
  newPasswordConfirm: '',
  /** 重置令牌：来自邮件链接，也允许直接粘贴（开发便利，界面上有明确标注）。 */
  resetToken: '',
  resetFromLink: false,
  /** 邮箱验证令牌：来自验证链接，同样允许粘贴。 */
  verifyToken: '',
  /** 登录/注册时是否顺带认领当前访客工作区（默认勾选，用户可以取消）。 */
  claimOnSubmit: true,

  // 邮箱验证
  resendNotice: null as string | null,
  resendError: null as string | null,
  verifyNotice: null as string | null,
  verifyError: null as string | null,

  // 改密码
  changeNotice: null as string | null,
  changeError: null as string | null,

  // 登录会话
  sessions: [] as AuthSessionOut[],
  sessionsStatus: 'idle' as SessionsStatus,
  sessionsError: null as string | null,
  sessionsNotice: null as string | null,
  revokingId: null as string | null,

  // 我的旅行
  trips: [] as TripOut[],
  tripsStatus: 'idle' as TripsStatus,
  tripsError: null as string | null,
  openingTripId: null as string | null,

  // 停用账号（DELETE /auth/account）：deactivateConfirm = 是否已展开二次确认
  deactivateConfirm: false,
  deactivateError: null as string | null,

  // 认领
  claimedProjectIds: [] as string[],
  claimNotice: null as string | null,
  claimError: null as string | null,
})

/** 正在执行「会话结束」流程：防止 401 处置被同一次操作重复触发。 */
let ending = false

// ------------------------------------------------------------------ 小工具

function setFormError(text: string, code: string | null = null): void {
  state.formError = text
  state.formErrorCode = code
  state.formNotice = null
}

function clearFormMessages(): void {
  state.formError = null
  state.formErrorCode = null
  state.formNotice = null
}

function recordClaimed(projectId: string): void {
  if (!state.claimedProjectIds.includes(projectId)) {
    state.claimedProjectIds.push(projectId)
  }
}

/** 用一句话写到首屏/工作页的提示条上（账号动作的结果不能只活在账号面板里）。 */
function pageNotice(kind: 'info' | 'error', text: string, code: string | null = null): void {
  workspace.state.notice = { kind, text, code }
}

/**
 * 账号自己的接口返回 401 时的统一处置。
 *
 * 工作区请求的 401 由 useWorkspace 通过 api/authEvents.ts 上报（那条路走的是同一个终点），
 * 而 /auth/sessions、/trips 这类请求发生在账号层内部，必须自己接：只要「我们以为已登录」
 * 却拿到 401，就说明会话已经被撤销或过期了。
 * 返回 true = 已经接管，调用方不要再写自己的错误提示。
 */
function noteAuthFailure(error: unknown): boolean {
  if (!(error instanceof ApiError) || error.status !== 401) {
    return false
  }
  if (state.status !== 'authenticated' || ending) {
    return false
  }
  void endSession(
    '登录状态已失效（会话被撤销或已过期），已退出登录并清空本地快照、对话与草稿。请重新登录。',
    {
      form: 'login',
    },
  )
  return true
}

// ------------------------------------------------------------------ 派生状态

const isAuthenticated = computed(() => state.status === 'authenticated')

/**
 * 邮箱是否还没验证（不阻断使用，只在界面上提示 + 提供重发）。
 *
 * 以 `UserOut.email_verified` 为准：那是服务器当前的事实。登录响应里的
 * `email_verification_required` 不参与这个判断 —— 否则用户验证完邮箱之后，
 * 界面会因为登录那一刻的旧提示继续显示「未验证」。
 */
const emailUnverified = computed(() => state.user !== null && state.user.email_verified === false)

const projectScope = computed<ProjectScope>(() => {
  const project = workspace.project.value
  if (project === null) {
    return 'none'
  }
  if (state.status === 'unknown') {
    return 'unknown'
  }
  if (state.status === 'anonymous') {
    return 'guest'
  }
  if (state.claimedProjectIds.includes(project.id)) {
    return 'account'
  }
  if (state.tripsStatus !== 'ready') {
    // 旅行列表还没回来：不下结论，等 GET /trips 的回答。
    return 'unknown'
  }
  return state.trips.some((trip) => trip.id === project.id) ? 'account' : 'claimable'
})

/** 登录/注册时要顺带认领的项目；勾选被取消、或已经知道在账号里时不带。 */
const claimTargetId = computed<string | null>(() => {
  const project = workspace.project.value
  if (project === null || !state.claimOnSubmit) {
    return null
  }
  if (state.claimedProjectIds.includes(project.id)) {
    return null
  }
  return project.id
})

const currentSession = computed<AuthSessionOut | null>(
  () => state.sessions.find((session) => session.current) ?? null,
)
const otherSessions = computed<AuthSessionOut[]>(() =>
  state.sessions.filter((session) => !session.current),
)

// ------------------------------------------------------------------ 启动

/** 邮件链接带进来的令牌（地址栏解析结果）。 */
interface AuthLink {
  resetToken: string | null
  verifyToken: string | null
}

/**
 * 从地址栏读取重置/验证令牌。
 *
 * 邮件里的链接最终落回这个单页应用（生产环境 Caddy 用 try_files 回退到 index.html），
 * 所以令牌会出现在 URL 上。契约没有规定链接的具体形状，因此这里同时兼容两种常见写法：
 *   /reset?token=… 或 ?reset_token=…（重置）；/verify-email?token=… 或 ?verify_token=…（验证）。
 * 读到后立刻把参数从地址栏抹掉：令牌已经进了表单，留在 URL 里只会让刷新重复提交。
 */
function readAuthLink(): AuthLink {
  if (typeof window === 'undefined') {
    return { resetToken: null, verifyToken: null }
  }
  const url = new URL(window.location.href)
  const params = url.searchParams
  const path = url.pathname.toLowerCase()
  const bare = params.get('token')
  const resetToken =
    params.get('reset_token') ?? (bare !== null && path.includes('reset') ? bare : null)
  const verifyToken =
    params.get('verify_token') ??
    (bare !== null && (path.includes('verify') || path.includes('confirm')) ? bare : null)

  if (params.has('token') || params.has('reset_token') || params.has('verify_token')) {
    params.delete('token')
    params.delete('reset_token')
    params.delete('verify_token')
    const query = params.toString()
    window.history.replaceState(
      {},
      '',
      `${url.pathname}${query === '' ? '' : `?${query}`}${url.hash}`,
    )
  }
  return { resetToken, verifyToken }
}

/**
 * 启动：读当前登录状态（GET /auth/me）。
 *
 * 401 是**正常结果**（未登录），其它失败（网络、5xx）保持 status='unknown' 并记录原因：
 * 界面会说「读不到登录状态」，而不是假装已退出登录。旅行列表由调用方在两个启动请求都
 * 结束之后再读（访客的 /trips 依赖已经下发匿名会话 Cookie 的 GET /session）。
 */
async function bootstrap(): Promise<void> {
  const link = readAuthLink()
  if (link.resetToken !== null) {
    state.resetToken = link.resetToken
    state.resetFromLink = true
    state.form = 'reset'
    state.open = true
    state.formNotice = '设置新密码即可完成重置。'
  }

  try {
    const result = await getMe()
    state.status = 'authenticated'
    state.user = result.user
    state.meError = null
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      state.status = 'anonymous'
      state.user = null
      state.meError = null
    } else {
      state.meError = describeAuthError(error)
    }
  }

  if (link.verifyToken !== null) {
    state.verifyToken = link.verifyToken
    state.open = true
    if (state.form !== 'reset') {
      state.form = state.status === 'authenticated' ? state.form : 'login'
    }
    await submitVerifyEmail(link.verifyToken)
  }
}

// ------------------------------------------------------------------ 表单动作

/** 登录。勾选了「同时保存当前工作区」时带上 claim_project_id（认领是后端的同一个事务）。 */
async function login(): Promise<void> {
  if (state.pending !== null) {
    return
  }
  const email = state.email.trim()
  if (email === '' || state.password === '') {
    setFormError('请填写邮箱和密码。')
    return
  }
  const claimId = claimTargetId.value
  state.pending = 'login'
  clearFormMessages()
  try {
    const result = await loginAccount({
      email,
      password: state.password,
      claim_project_id: claimId,
    })
    state.password = ''
    await afterSignedIn('已登录', result, claimId)
  } catch (error) {
    setFormError(describeAuthError(error), authErrorCode(error))
  } finally {
    state.pending = null
  }
}

/** 注册（成功即登录）。同样可以顺带认领当前访客工作区。 */
async function register(): Promise<void> {
  if (state.pending !== null) {
    return
  }
  const email = state.email.trim()
  if (email === '' || state.password === '') {
    setFormError('请填写邮箱和密码。')
    return
  }
  if (state.password.length < MIN_PASSWORD_LENGTH) {
    setFormError(`密码至少 ${MIN_PASSWORD_LENGTH} 位。`)
    return
  }
  if (state.password !== state.passwordConfirm) {
    setFormError('两次输入的密码不一致。')
    return
  }
  const claimId = claimTargetId.value
  const displayName = state.displayName.trim()
  state.pending = 'register'
  clearFormMessages()
  try {
    const result = await registerAccount({
      email,
      password: state.password,
      display_name: displayName === '' ? null : displayName,
      claim_project_id: claimId,
    })
    state.password = ''
    state.passwordConfirm = ''
    await afterSignedIn('注册成功并已登录', result, claimId)
  } catch (error) {
    setFormError(describeAuthError(error), authErrorCode(error))
  } finally {
    state.pending = null
  }
}

/**
 * 登录/注册成功之后的统一处理：写入登录状态、如实报告认领结果、重新读取工作区与旅行列表。
 *
 * 为什么要重新读工作区：认领会把归属写给账号，同时让访客 Cookie 失去访问权；
 * 界面必须按现在的身份重新拿一次服务器数据，而不是继续用登录前的那份快照。
 */
/**
 * 说明：响应里的 `email_verification_required` 只表示「后端建议验证邮箱」，不参与界面判断 ——
 * 「未验证」提示一律以 `UserOut.email_verified` 为准（见 emailUnverified），否则用户验证完之后
 * 界面还会因为登录那一刻的旧标记继续显示「未验证」。
 * 而 `email_sent === false` 是「注册成功但验证邮件没发出去」的真实信号，必须如实告诉用户。
 */
async function afterSignedIn(
  label: string,
  result: AuthSessionResponse,
  requestedClaimId: string | null,
): Promise<void> {
  state.status = 'authenticated'
  state.user = result.user
  state.meError = null
  state.open = false

  let claimedText: string
  if (result.claimed_project_id !== null) {
    recordClaimed(result.claimed_project_id)
    claimedText = '这次旅行已保存到账号'
  } else if (requestedClaimId !== null) {
    claimedText = '旅行暂未保存到账号，请在账号窗口重试'
  } else {
    claimedText = ''
  }
  const mailText = result.email_sent === false ? '；账号已注册，验证邮件未发出，请稍后重发' : ''
  const summary = `${label}：${result.user.email}；${claimedText}${mailText}。`
  clearFormMessages()
  state.formNotice = summary
  pageNotice('info', summary)

  // 登录会改变「当前工作区」的答案（访客工作区可能刚被认领，账号里也可能本来就有别的活动工作区）：
  // 所以要么按新身份重读一次当前快照，要么（本来就没有工作区时）重新走一遍工作区启动流程。
  if (workspace.state.snapshot === null) {
    await workspace.bootstrap()
  } else {
    await workspace.refreshSnapshot()
  }
  await loadTrips()
}

/**
 * 退出登录（POST /auth/logout 撤销当前会话）。
 * 服务端失败时**不假装已退出**：只有成功（或 401 —— 会话本来就已经失效）才清本地数据。
 */
async function logout(): Promise<void> {
  if (state.pending !== null) {
    return
  }
  state.pending = 'logout'
  clearFormMessages()
  try {
    await logoutAccount()
  } catch (error) {
    if (!(error instanceof ApiError && error.status === 401)) {
      setFormError(`退出登录失败：${describeAuthError(error)}`, authErrorCode(error))
      state.pending = null
      return
    }
    // 401：Cookie 已经无效（会话此前就被撤销/过期），按已退出处理。
  }
  state.pending = null
  await endSession('已退出登录。旅行仍在账号里，下次登录可继续。', { closePanel: true })
}

/** 忘记密码：申请重置（后端永远 202，不泄露邮箱是否存在）。 */
async function submitForgot(): Promise<void> {
  if (state.pending !== null) {
    return
  }
  const email = state.email.trim()
  if (email === '') {
    setFormError('请填写邮箱地址。')
    return
  }
  state.pending = 'forgot'
  clearFormMessages()
  try {
    const result = await requestPasswordReset(email)
    state.formNotice =
      result.email_sent === false
        ? '暂时无法发送重置邮件，请稍后重试。'
        : '如果这个邮箱注册过，你会收到一封重置邮件。'
  } catch (error) {
    setFormError(describeAuthError(error), authErrorCode(error))
  } finally {
    state.pending = null
  }
}

/** 用令牌重置密码（令牌来自邮件链接，也支持直接粘贴）。成功后后端撤销该账号全部会话。 */
async function submitReset(): Promise<void> {
  if (state.pending !== null) {
    return
  }
  const token = state.resetToken.trim()
  if (token === '') {
    setFormError('请从重置邮件中的链接打开此页面。')
    return
  }
  if (state.newPassword.length < MIN_PASSWORD_LENGTH) {
    setFormError(`新密码至少 ${MIN_PASSWORD_LENGTH} 位。`)
    return
  }
  if (state.newPassword !== state.newPasswordConfirm) {
    setFormError('两次输入的新密码不一致。')
    return
  }
  state.pending = 'reset'
  clearFormMessages()
  try {
    await resetPasswordRequest({ token, new_password: state.newPassword })
    state.newPassword = ''
    state.newPasswordConfirm = ''
    state.resetToken = ''
    state.resetFromLink = false
    if (state.status === 'authenticated') {
      // 契约：重置密码撤销该用户**全部**会话，包括当前这个浏览器。
      await endSession(
        '密码已重置，后端已撤销该账号的全部登录会话（包括当前这个浏览器）。请用新密码重新登录。',
        {
          form: 'login',
        },
      )
    } else {
      state.form = 'login'
      state.formNotice = '密码已重置成功，请用新密码登录。'
    }
  } catch (error) {
    setFormError(describeAuthError(error), authErrorCode(error))
  } finally {
    state.pending = null
  }
}

/** 已登录改密码；成功后后端撤销其它会话，当前会话保留。 */
async function submitChangePassword(): Promise<void> {
  if (state.pending !== null || state.status !== 'authenticated') {
    return
  }
  if (state.currentPassword === '') {
    state.changeError = '请填写当前密码。'
    return
  }
  if (state.newPassword.length < MIN_PASSWORD_LENGTH) {
    state.changeError = `新密码至少 ${MIN_PASSWORD_LENGTH} 位。`
    return
  }
  if (state.newPassword !== state.newPasswordConfirm) {
    state.changeError = '两次输入的新密码不一致。'
    return
  }
  state.pending = 'change-password'
  state.changeError = null
  state.changeNotice = null
  try {
    await changePasswordRequest({
      current_password: state.currentPassword,
      new_password: state.newPassword,
    })
    state.currentPassword = ''
    state.newPassword = ''
    state.newPasswordConfirm = ''
    state.changeNotice = '密码已修改，其他设备需要重新登录。'
    await loadSessions()
  } catch (error) {
    state.changeError = describeAuthError(error)
  } finally {
    state.pending = null
  }
}

/** 重发验证邮件（限流；后端可能报告 email_sent=false）。 */
async function resendVerification(): Promise<void> {
  if (state.pending !== null || state.status !== 'authenticated') {
    return
  }
  state.pending = 'resend-verify'
  state.resendError = null
  state.resendNotice = null
  try {
    const result = await resendVerificationEmail()
    if (result.status === 'already_verified') {
      state.resendNotice = '邮箱已经验证，无需重发。'
    } else if (result.status === 'send_failed' || result.email_sent === false) {
      state.resendNotice = '验证邮件没有发出，请稍后重试。'
    } else if (result.status === 'sent' || result.email_sent === true) {
      state.resendNotice = '验证邮件已发送，请查收。'
    } else {
      state.resendNotice = '暂时无法确认邮件是否已发送，请查看邮箱或稍后重试。'
    }
  } catch (error) {
    state.resendError = describeAuthError(error)
  } finally {
    state.pending = null
  }
}

/** 用令牌验证邮箱（验证链接或手动粘贴）。 */
async function submitVerifyEmail(tokenFromLink?: string): Promise<void> {
  if (state.pending !== null) {
    return
  }
  const token = (tokenFromLink ?? state.verifyToken).trim()
  if (token === '') {
    state.verifyError = '请从验证邮件中的链接打开此页面。'
    return
  }
  state.pending = 'verify-email'
  state.verifyError = null
  state.verifyNotice = null
  try {
    const result = await verifyEmailRequest({ token })
    state.verifyToken = ''
    if (state.user !== null && state.user.id === result.user.id) {
      // 只有确认是同一个人的时候才更新界面上的用户信息，绝不凭一个令牌伪造登录态。
      state.user = result.user
      state.verifyNotice = '邮箱已验证。'
    } else if (state.status === 'authenticated') {
      state.verifyNotice = '这个邮箱已验证。当前登录的账号是另一个邮箱，界面显示的仍是当前账号。'
    } else {
      state.verifyNotice = '邮箱已验证。现在可以用它登录了。'
    }
  } catch (error) {
    state.verifyError = describeAuthError(error)
  } finally {
    state.pending = null
  }
}

// ------------------------------------------------------------------ 登录会话

async function loadSessions(): Promise<void> {
  if (state.status !== 'authenticated') {
    state.sessions = []
    state.sessionsStatus = 'idle'
    return
  }
  state.sessionsStatus = 'loading'
  state.sessionsError = null
  try {
    const result = await listAuthSessions()
    state.sessions = result.sessions
    state.sessionsStatus = 'ready'
  } catch (error) {
    if (noteAuthFailure(error)) {
      state.sessionsStatus = 'idle'
      return
    }
    state.sessionsStatus = 'error'
    state.sessionsError = describeAuthError(error)
  }
}

/**
 * 撤销一个会话（先确认再发请求）。
 *
 * 撤销「当前会话」等价于退出登录：后端撤销后这个 Cookie 立刻失效，界面必须当场切回未登录
 * 并清空私有数据，而不是等下一次请求 401 才发现。
 */
async function revokeSession(session: AuthSessionOut): Promise<void> {
  if (state.pending !== null || state.revokingId !== null) {
    return
  }
  const ok = window.confirm(
    session.current
      ? '撤销当前这个会话？\n\n撤销后这个浏览器会立刻退出登录，需要重新输入密码。'
      : `撤销这个登录会话？\n\n创建时间：${formatDateTime(session.created_at)}\n设备：${session.user_agent ?? '未提供'}\n\n撤销后那台设备需要重新登录。`,
  )
  if (!ok) {
    return
  }
  state.revokingId = session.id
  state.sessionsError = null
  state.sessionsNotice = null
  try {
    const result = await revokeAuthSession(session.id)
    // 后端会用 status 明确说明撤销的是不是当前会话（revoked / current_session_revoked）；
    // 读不到响应体时退回列表里的 current 标记。
    const revokedCurrent =
      result?.status === 'current_session_revoked' ||
      (result?.status !== 'revoked' && session.current)
    if (revokedCurrent) {
      await endSession('当前会话已被撤销，需要重新登录。', { form: 'login' })
      return
    }
    state.sessionsNotice = '已退出该设备，下次使用需要重新登录。'
    await loadSessions()
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      await endSession('撤销会话时登录状态已经失效，需要重新登录。', { form: 'login' })
      return
    }
    state.sessionsError = `撤销失败：${describeAuthError(error)}`
  } finally {
    state.revokingId = null
  }
}

// ------------------------------------------------------------------ 我的旅行

async function loadTrips(): Promise<void> {
  state.tripsStatus = 'loading'
  state.tripsError = null
  try {
    const result = await listTrips()
    state.trips = result.trips
    state.tripsStatus = 'ready'
  } catch (error) {
    if (noteAuthFailure(error)) {
      state.tripsStatus = 'idle'
      return
    }
    state.tripsStatus = 'error'
    state.tripsError = describeApiError(error)
  }
}

/** 打开一份旅行：真实读取该项目快照并切到工作页（详见 useWorkspace.openProject）。 */
async function openTrip(trip: TripOut): Promise<void> {
  if (state.openingTripId !== null) {
    return
  }
  state.openingTripId = trip.id
  try {
    const opened = await workspace.openProject(trip.id)
    if (opened) {
      state.open = false
    }
  } finally {
    state.openingTripId = null
  }
}

/**
 * 当前工作区变了（新建 / 打开别的旅行 / 归档 / 退出登录清空）就重新读一次旅行列表。
 *
 * 为什么需要：「我的旅行」必须反映服务器现在的状态，而不是打开页面那一刻的快照；
 * 这跟着项目 ID 变化触发（事件驱动），不是轮询、也没有定时器。
 */
watch(
  () => workspace.project.value?.id ?? null,
  (projectId, previous) => {
    if (projectId !== previous) {
      void loadTrips()
    }
  },
)

// ------------------------------------------------------------------ 认领访客工作区

/**
 * 把当前访客工作区保存到账号（POST /auth/claim，幂等）。
 * 界面按返回的 claimed / already_claimed 如实说明，失败时保留错误原文并允许直接重试。
 */
async function claimCurrentProject(): Promise<void> {
  if (state.pending !== null) {
    return
  }
  const project = workspace.project.value
  state.claimNotice = null
  state.claimError = null
  if (project === null) {
    state.claimError = '当前没有可以保存的工作区：先创建（或打开）一个工作区再认领。'
    return
  }
  if (state.status !== 'authenticated') {
    state.claimError = '请先登录或注册，再把这个工作区保存到账号。'
    state.open = true
    state.form = 'login'
    return
  }
  state.pending = 'claim'
  try {
    const result = await claimGuestProject({ project_id: project.id })
    recordClaimed(project.id)
    state.claimNotice = result.already_claimed
      ? `「${project.title}」已经保存在你的账号里。`
      : `已把「${project.title}」保存到你的账号。换设备登录同一账号也能打开它；这台浏览器的匿名会话已经不再拥有访问权。`
    pageNotice('info', state.claimNotice)
    await workspace.refreshSnapshot()
    await loadTrips()
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      await endSession('保存到账号时登录状态已经失效，需要重新登录。', { form: 'login' })
      return
    }
    state.claimError = `保存失败：${describeAuthError(error)} 认领是幂等的，可以直接重试。`
  } finally {
    state.pending = null
  }
}

// ------------------------------------------------------------------ 停用账号

/** 第一步：展开二次确认（这一步不发任何请求）。 */
function beginDeactivate(): void {
  state.deactivateError = null
  state.deactivateConfirm = true
}

/** 取消：收起确认块，什么都不做。 */
function cancelDeactivate(): void {
  state.deactivateConfirm = false
  state.deactivateError = null
}

/**
 * 第二步：真的调用 DELETE /auth/account（停用账号）。
 *
 * 成功（HTTP 200 + status=account_disabled）后按退出登录处理：清空本地私有数据、切回未登录，
 * 并把后端返回的真实 status 与撤销会话数写进提示。
 * 失败一律显示服务端真实结果并**保持登录态**，绝不假装已经停用。
 */
async function deactivateAccount(): Promise<void> {
  if (state.pending !== null) {
    return
  }
  if (state.status !== 'authenticated') {
    state.deactivateError = '需要先登录才能停用账号。'
    return
  }
  state.pending = 'deactivate'
  state.deactivateError = null
  try {
    const result = await deactivateAccountRequest()
    const status = result?.status ?? '（后端没有返回状态串）'
    const revoked = result?.sessions_revoked
    const revokedText = typeof revoked === 'number' ? `，撤销了 ${revoked} 个登录会话` : ''
    state.deactivateConfirm = false
    await endSession(
      `账号已停用（后端返回 status=${status}${revokedText}）：这个邮箱现在无法再登录，本地保存的对话与草稿已清空，账号数据不再从界面上可见。` +
        '工作区与讲解数据没有删除，但已经归还为无人拥有 —— 如果它们仍能被这台浏览器的匿名会话读取，' +
        '会以「访客数据」的身份重新出现在首屏（标签会写明是访客数据）。' +
        '恢复需要在服务端处理，界面上没有自助恢复入口。',
      { closePanel: true },
    )
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      await endSession('停用账号时登录状态已经失效，需要重新登录。', { form: 'login' })
      return
    }
    state.deactivateError = `停用账号失败：${describeAuthError(error)}`
  } finally {
    state.pending = null
  }
}

// ------------------------------------------------------------------ 会话结束

/**
 * 清空本地私有数据并切回未登录。
 *
 * 这是「退出登录」和「检测到 401 登录已失效」共用的收尾动作：
 *   1. 先同步清空（快照/对话/草稿立刻从界面上消失），再重新读一次服务器数据；
 *   2. 清空之后用 workspace.bootstrap() 重新读当前 Cookie 还能看到什么 —— 认领过的账号工作区
 *      会因为访客 Cookie 失去访问权而不再出现，尚未认领的访客工作区仍然会回来（那本来就是
 *      访客自己的数据，不是账号私有数据）。
 */
async function applySignedOut(message: string): Promise<void> {
  state.status = 'anonymous'
  state.form = 'login'
  state.user = null
  state.sessions = []
  state.sessionsStatus = 'idle'
  state.sessionsError = null
  state.sessionsNotice = null
  state.claimedProjectIds = []
  state.claimNotice = null
  state.claimError = null
  state.deactivateConfirm = false
  state.deactivateError = null
  state.changeNotice = null
  state.changeError = null
  state.resendNotice = null
  state.resendError = null
  state.verifyNotice = null
  state.verifyError = null
  // 密码类字段不留在内存里
  state.password = ''
  state.passwordConfirm = ''
  state.currentPassword = ''
  state.newPassword = ''
  state.newPasswordConfirm = ''

  workspace.clearPrivateData()
  pageNotice('info', message)
  // 重新读一次：现在这台浏览器还能合法访问什么，由服务器说了算。
  await workspace.bootstrap()
  void loadTrips()
}

interface EndSessionOptions {
  /** 指定要切换到的表单（会顺带打开面板），不指定就保持面板当前状态。 */
  form?: AuthForm
  /** 关掉面板（退出登录时用）。 */
  closePanel?: boolean
}

async function endSession(message: string, options: EndSessionOptions = {}): Promise<void> {
  if (ending) {
    return
  }
  ending = true
  try {
    await applySignedOut(message)
    if (options.closePanel === true) {
      state.open = false
    }
    if (options.form !== undefined) {
      state.form = options.form
      state.open = true
      setFormError(message)
    }
  } finally {
    ending = false
  }
}

/**
 * 401 的统一处置入口（由 useWorkspace 通过 api/authEvents.ts 上报）。
 *
 * 只在「我们以为已登录」时接管：此时 401 意味着登录会话被撤销或过期，必须立刻清空并提示；
 * 未登录时的 401 是匿名会话自己的事，由工作区层按原有文案提示，这里不插手。
 * 返回 true 表示已接管，工作区层不再补一条可能互相矛盾的提示。
 */
onAuthRequired((code) => {
  if (state.status !== 'authenticated' || ending) {
    return false
  }
  const detail = code === null ? '' : `（${code}）`
  void endSession(
    `登录状态已失效${detail}：会话被撤销或已过期，已退出登录并清空本地快照、对话与草稿。请重新登录。`,
    {
      form: 'login',
    },
  )
  return true
})

/**
 * 工作区请求 404 的复核入口（由 useWorkspace 通过 api/authEvents.ts 上报，工作区侧只有一个出口）。
 *
 * 为什么要复核：契约说「访问不属于自己的资源返回 404」，所以登录会话被撤销后，工作区请求同样
 * 是 404（而不是 401）。本地以为已登录时，先用一次 GET /auth/me 问清楚：
 *   - 401 → 登录确实已经失效：按「登录状态已失效」清空私有数据并提示重新登录，返回 true；
 *   - 200 → 登录仍然有效：这个 404 就是「工作区不存在 / 不属于你 / 已归档」，交回工作区层按原样提示；
 *   - 其它错误（网络、5xx）→ 不擅自判定登录失效，同样交回工作区层（宁可说「工作区不可用」，
 *     也不谎称「登录失效」）。
 */
onWorkspaceMissing(async () => {
  if (state.status !== 'authenticated' || ending) {
    return false
  }
  try {
    const result = await getMe()
    // 服务器说登录仍然有效：顺手把用户信息对齐成服务器当前的事实。
    state.user = result.user
    state.meError = null
    return false
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      await endSession(
        '登录状态已失效（会话被撤销或已过期，所以这个工作区不再属于当前身份）：已退出登录并清空本地快照、对话与草稿。请重新登录。',
        { form: 'login' },
      )
      return true
    }
    return false
  }
})

// ------------------------------------------------------------------ 面板

function openPanel(form?: AuthForm): void {
  state.open = true
  if (form !== undefined) {
    state.form = form
  } else if (
    state.status !== 'authenticated' &&
    state.form === 'reset' &&
    state.resetToken === ''
  ) {
    state.form = 'login'
  }
  if (state.status === 'authenticated') {
    void loadSessions()
  }
}

function closePanel(): void {
  state.open = false
}

/** 切换表单：清掉上一个表单的提示，避免「注册失败」的红字挂在登录表单上。 */
function setForm(form: AuthForm): void {
  if (state.form === form) {
    return
  }
  state.form = form
  clearFormMessages()
}

/** 重新读一次登录状态（读 /auth/me 失败后界面上的「重试」用）。 */
async function retryMe(): Promise<void> {
  state.meError = null
  state.status = 'unknown'
  await bootstrap()
  void loadTrips()
}

export function useAuth() {
  return {
    state,
    isAuthenticated,
    emailUnverified,
    projectScope,
    claimTargetId,
    currentSession,
    otherSessions,
    bootstrap,
    retryMe,
    openPanel,
    closePanel,
    setForm,
    login,
    register,
    logout,
    submitForgot,
    submitReset,
    submitChangePassword,
    resendVerification,
    submitVerifyEmail,
    loadSessions,
    revokeSession,
    loadTrips,
    openTrip,
    claimCurrentProject,
    beginDeactivate,
    cancelDeactivate,
    deactivateAccount,
  }
}
