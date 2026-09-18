<script setup lang="ts">
/**
 * 连接状态徽章。
 *
 * 严格三态：已连接 / 重连中 / 未连接。
 * 断开只影响这里，界面上任何地方都不能因为断开而显示「任务失败」。
 */
import { computed } from 'vue'

import type { ConnectionState, StreamStopReason } from '../api/events'

const props = defineProps<{
  state: ConnectionState
  reason?: StreamStopReason | null
}>()

const text = computed<string>(() => {
  switch (props.state) {
    case 'open':
      return '已连接'
    case 'connecting':
    case 'reconnecting':
      return '重连中'
    default:
      return '未连接'
  }
})

const tone = computed<string>(() => {
  switch (props.state) {
    case 'open':
      return 'ok'
    case 'connecting':
    case 'reconnecting':
      return 'waiting'
    default:
      return 'bad'
  }
})

const detail = computed<string>(() => {
  switch (props.state) {
    case 'open':
      return '正在同步旅行内容'
    case 'connecting':
      return '正在连接'
    case 'reconnecting':
      return '连接暂时中断，恢复后会自动同步'
    default:
      if (props.reason === 'project_unavailable') {
        return '项目已不可用（可能被归档），请刷新页面'
      }
      if (props.reason === 'session_expired') {
        return '匿名会话已过期，请刷新页面重新开始'
      }
      return '暂未连接'
  }
})
</script>

<template>
  <span class="conn" :class="`conn--${tone}`" :title="detail">
    <span class="conn__dot" aria-hidden="true"></span>
    <span class="conn__text">{{ text }}</span>
  </span>
</template>
