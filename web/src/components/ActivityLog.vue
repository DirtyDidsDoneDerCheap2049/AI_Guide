<script setup lang="ts">
/**
 * 活动记录区：按时间顺序展示 SSE 事件。
 *
 * 只记录事件流（含断线后补发的事件）与明确标注的本地提示（快照恢复），
 * 不把任何轮询结果写成「事件」，避免活动记录说谎。
 */
import { nextTick, ref, watch } from 'vue'

import type { ActivityEntry } from '../composables/useWorkspace'
import { formatTime } from '../utils/format'
import { errorCodeLabel } from '../utils/labels'

const props = defineProps<{ entries: ActivityEntry[] }>()

const listRef = ref<HTMLOListElement | null>(null)

// 新事件到达后滚到底部：活动记录是时间流，用户最关心最新一条。
watch(
  () => props.entries.length,
  async () => {
    await nextTick()
    const element = listRef.value
    if (element !== null) {
      element.scrollTop = element.scrollHeight
    }
  },
)
</script>

<template>
  <section class="panel activity">
    <header class="panel__head">
      <h2 class="panel__title">活动记录</h2>
      <span class="panel__count">{{ entries.length }} 条</span>
    </header>

    <p v-if="entries.length === 0" class="panel__placeholder">等待事件流推送…</p>

    <ol v-else ref="listRef" class="activity__list">
      <li
        v-for="(entry, index) in entries"
        :key="`${entry.source}-${entry.id}-${index}`"
        class="activity__item"
        :class="{ 'activity__item--local': entry.source === 'local' }"
      >
        <time class="activity__time">{{ formatTime(entry.created_at) }}</time>
        <span class="activity__text">{{ entry.text }}</span>
        <span v-if="entry.source === 'local'" class="tag tag--muted">快照</span>
        <span v-else class="tag tag--muted">{{ entry.type }}</span>
        <span
          v-if="entry.error_code !== null"
          class="tag tag--bad"
          :title="errorCodeLabel(entry.error_code) ?? ''"
        >
          {{ entry.error_code }}
        </span>
      </li>
    </ol>
  </section>
</template>
