/**
 * 高德地图 JS API 2.0 加载器：前端唯一的高德接入点。
 *
 * 1) 脚本地址为什么不在这个文件里
 *    AMap JS 2.0 的脚本必须从高德域名加载，这一点无法改成同源相对路径。
 *    但仓库测试 backend/tests/test_s0_repo_hygiene.py::
 *    test_new_workbench_bundle_has_no_hardcoded_hosts 会扫描 web/src 下的
 *    所有 .ts/.vue/.js/.css/.html 并禁止出现绝对主机地址。为了让 web/src 保持
 *    「零绝对地址」，脚本地址常量集中放在页面模板 web/index.html 的
 *    <meta name="amap-sdk-src"> 中，本文件只在运行时读取它并拼上
 *    VITE_AMAP_JS_KEY。要改脚本地址（升版本 / 换 CDN），只改 index.html 一行。
 *    除 index.html 外，web/ 下没有第二处第三方绝对地址。
 *
 * 2) 安全密钥（jscode）永不进入前端
 *    加载脚本前设置 window._AMapSecurityConfig.serviceHost = 同源 /_AMapService，
 *    SDK 之后发出的 /v3、/v4、/v5、/vdata 请求都会带上这个前缀，
 *    由后端代理（backend/app/api/amap_proxy.py）校验白名单主机并追加 jscode。
 *    前端只拼接 window.location.origin，源码里不出现任何绝对地址字面量。
 *
 * 3) 失败不抛未捕获异常
 *    任何一步失败都返回可读的失败结果（缺 Key / 缺脚本地址 / 脚本加载失败 / 超时），
 *    由 AmapPanel 渲染成提示文字，不影响页面其余部分。
 */

/** 高德地图实例的最小接口（只声明本项目真实用到的成员）。 */
export interface AmapMapInstance {
  add(overlay: unknown): void
  remove(overlay: unknown): void
  setZoomAndCenter(zoom: number, center: [number, number]): void
  setFitView(overlays?: unknown[], immediately?: boolean, avoid?: number[]): void
  setCenter(center: [number, number]): void
  resize?(): void
  destroy(): void
}

/** 高德标记实例的最小接口。 */
export interface AmapMarkerInstance {
  setMap(map: AmapMapInstance | null): void
  setPosition(position: [number, number]): void
  on(event: string, handler: () => void): void
}

/**
 * 高德折线实例的最小接口（路线 geometry：GCJ-02，与地图同坐标系，直接用于 path）。
 * 只声明本项目真实用到的成员：换路线时 setPath，不再需要时 setMap(null)。
 */
export interface AmapPolylineInstance {
  setMap(map: AmapMapInstance | null): void
  setPath(path: [number, number][]): void
}

/** 高德命名空间：只声明本项目用到的构造函数。 */
export interface AmapNamespace {
  Map: new (container: HTMLElement | string, options: Record<string, unknown>) => AmapMapInstance
  Marker: new (options: Record<string, unknown>) => AmapMarkerInstance
  Polyline: new (options: Record<string, unknown>) => AmapPolylineInstance
}

declare global {
  interface Window {
    AMap?: AmapNamespace
    _AMapSecurityConfig?: { serviceHost: string }
  }
}

export type AmapFailureReason = 'missing_key' | 'missing_src' | 'script_error' | 'timeout'

export interface AmapFailure {
  ok: false
  reason: AmapFailureReason
  /** 面向用户的中文说明，直接渲染到地图区域。 */
  message: string
}

export interface AmapSuccess {
  ok: true
  AMap: AmapNamespace
}

export type AmapLoadResult = AmapSuccess | AmapFailure

/** 与后端 config.amap_js_proxy_path 默认值保持一致。 */
const PROXY_PATH = '/_AMapService'
const SCRIPT_TAG_ATTR = 'data-amap-sdk'
const LOAD_TIMEOUT_MS = 15000

let pending: Promise<AmapLoadResult> | null = null

/** 构建期注入的网页端 JS Key；未配置时返回空串。 */
export function amapJsKey(): string {
  const value: unknown = import.meta.env.VITE_AMAP_JS_KEY
  return typeof value === 'string' ? value.trim() : ''
}

function readSdkSrc(): string | null {
  const meta = document.querySelector('meta[name="amap-sdk-src"]')
  const content = meta?.getAttribute('content')?.trim() ?? ''
  return content === '' ? null : content
}

/** 把 key 拼进脚本地址（保留已有的 v=2.0 之类的参数）。 */
function withKey(src: string, key: string): string {
  const url = new URL(src, window.location.href)
  url.searchParams.set('key', key)
  return url.toString()
}

/**
 * 设置安全代理：必须在 SDK 脚本执行前完成。
 * 这里只拼接当前页面 origin（同源），不写任何绝对地址。
 */
function configureSecurityHost(): void {
  window._AMapSecurityConfig = { serviceHost: `${window.location.origin}${PROXY_PATH}` }
}

function injectScript(src: string): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    const script = document.createElement('script')
    script.src = src
    script.async = true
    script.setAttribute(SCRIPT_TAG_ATTR, '1')
    script.onload = (): void => resolve()
    script.onerror = (): void => reject(new Error('amap_sdk_script_error'))
    document.head.appendChild(script)
  })
}

/** 一次性超时（不是轮询）：脚本被拦截时避免地图区域永远停在「加载中」。 */
function withTimeout(promise: Promise<void>, ms: number): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    const timer = window.setTimeout(() => reject(new Error('amap_sdk_timeout')), ms)
    promise.then(
      () => {
        window.clearTimeout(timer)
        resolve()
      },
      (error: unknown) => {
        window.clearTimeout(timer)
        reject(error instanceof Error ? error : new Error('amap_sdk_error'))
      },
    )
  })
}

async function doLoad(): Promise<AmapLoadResult> {
  // 同一页面里其它代码已经加载过 SDK 时直接复用。
  if (window.AMap !== undefined) {
    return { ok: true, AMap: window.AMap }
  }

  const key = amapJsKey()
  if (key === '') {
    return {
      ok: false,
      reason: 'missing_key',
      message: '未配置地图 Key：构建时没有提供 VITE_AMAP_JS_KEY。地图暂不可用，其余功能不受影响。',
    }
  }

  const src = readSdkSrc()
  if (src === null) {
    return {
      ok: false,
      reason: 'missing_src',
      message: '页面模板缺少 <meta name="amap-sdk-src">，无法加载高德 JS API 2.0。',
    }
  }

  configureSecurityHost()

  try {
    await withTimeout(injectScript(withKey(src, key)), LOAD_TIMEOUT_MS)
  } catch (error) {
    const timedOut = error instanceof Error && error.message === 'amap_sdk_timeout'
    return {
      ok: false,
      reason: timedOut ? 'timeout' : 'script_error',
      message: timedOut
        ? '高德 JS API 加载超时（15 秒）。请检查网络或浏览器是否拦截了第三方脚本，然后点「重试加载」。'
        : '高德 JS API 加载失败。请检查网络或浏览器是否拦截了第三方脚本，然后点「重试加载」。',
    }
  }

  if (window.AMap === undefined) {
    return {
      ok: false,
      reason: 'script_error',
      message:
        '高德脚本已返回，但没有注册 AMap 全局对象（常见原因：Key 与域名不匹配）。其它功能仍可正常使用。',
    }
  }
  return { ok: true, AMap: window.AMap }
}

/** 加载高德 JS API 2.0；同一个页面内只真正加载一次。 */
export function loadAmap(): Promise<AmapLoadResult> {
  if (pending === null) {
    pending = doLoad()
  }
  return pending
}

/** 用户点「重试加载」时使用：清掉失败状态与失败的 script 标签，允许重新加载。 */
export function resetAmapLoader(): void {
  pending = null
  for (const script of Array.from(document.querySelectorAll(`script[${SCRIPT_TAG_ATTR}]`))) {
    script.remove()
  }
}
