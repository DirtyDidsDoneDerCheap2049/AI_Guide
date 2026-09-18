/// <reference types="vite/client" />

/**
 * 构建期注入的环境变量。
 *
 * VITE_AMAP_JS_KEY：高德「网页端 JS API」Key，只用于加载 JS API 2.0。
 * - 只允许网页端 Key，不是 Web 服务 Key，也不是 JS 安全密钥；
 * - JS 安全密钥（jscode）永远不进入前端，由后端 /_AMapService 代理转发时追加；
 * - 未配置时地图区域显示「未配置地图 Key」，页面其余部分照常可用。
 */
interface ImportMetaEnv {
  readonly VITE_AMAP_JS_KEY?: string
}
