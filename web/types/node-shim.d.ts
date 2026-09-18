/**
 * 只为构建配置声明用到的 Node 全局。
 *
 * vite.config.ts 需要读取 VITE_DEV_API_TARGET，但引入完整 @types/node 会让
 * 前端工程多出一整套服务端类型；这里只声明 process.env 这一处最小面。
 * 该文件不参与浏览器产物（不是模块，没有运行时代码）。
 */
declare const process: {
  env: Record<string, string | undefined>
}
