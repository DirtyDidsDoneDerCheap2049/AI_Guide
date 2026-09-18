<script setup lang="ts">
/**
 * 应用外壳：只负责「探索首页 ↔ 导游工作页」的切换、账号面板与启动。
 *
 * 页面生命周期只有三件自动动作：
 *   1. 加载时 bootstrap()：并行做两件互不依赖的事 ——
 *      GET /auth/me（账号层：确认有没有登录）与 /session -> /projects/current（工作区层）；
 *      随后按需读一次 GET /trips（访客的 /trips 依赖 /session 已经下发匿名会话 Cookie）；
 *   2. 离开页面时 dispose()：关闭事件流，避免 EventSource 留在后台重连；
 *   3. 账号面板由这里常驻挂载：账号入口在首屏顶栏与工作页顶栏都能打开同一个面板。
 * 没有任何业务轮询定时器；断线只改连接状态徽章，不影响任务状态显示。
 */
import { onBeforeUnmount, onMounted } from 'vue'

import AccountPanel from './components/AccountPanel.vue'
import PlanningHome from './components/PlanningHome.vue'
import WorkbenchView from './components/WorkbenchView.vue'
import { useAuth } from './composables/useAuth'
import { useWorkspace } from './composables/useWorkspace'

const workspace = useWorkspace()
const { state } = workspace
const auth = useAuth()

onMounted(async () => {
  await Promise.all([workspace.bootstrap(), auth.bootstrap()])
  // 有工作区时，项目 ID 的变化已经触发过 useAuth 里的旅行列表刷新（见那边的 watch）；
  // 没有工作区时不会触发，所以这里补一次，保证首屏「我的旅行」有真实结果。
  if (workspace.project.value === null) {
    void auth.loadTrips()
  }
})

onBeforeUnmount(() => {
  workspace.dispose()
})
</script>

<template>
  <main v-if="state.phase === 'loading'" class="center">
    <p>正在打开你的旅行…</p>
  </main>

  <main v-else-if="state.phase === 'error'" class="center">
    <p class="field__error">加载失败：{{ state.bootstrapError }}</p>
    <button class="btn btn--primary" type="button" @click="workspace.bootstrap()">重新加载</button>
  </main>

  <WorkbenchView v-else-if="state.view === 'work' && state.snapshot !== null" />
  <PlanningHome v-else />

  <!-- 账号面板：覆盖层，不改变下面的页面结构（首屏与工作页共用同一个面板） -->
  <AccountPanel />
</template>
