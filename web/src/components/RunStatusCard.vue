<script setup lang="ts">
/**
 * 当前任务卡片：运行状态 + 取消/重做 + 可展开的「处理详情」。
 *
 * 状态呈现原则：
 * - 断线（连接状态）永远不算任务失败：失败只能来自 run.status / run.failed 事件；
 * - 技术细节（run_id、预算、token、时间戳）默认收在 <details> 里，不占主视图；
 * - 取消只对未结束的运行可用；重试/重做的可用范围与**对应接口**一致：
 *     retryMode='run'   → POST /runs/{id}/retry，只有 FAILED / PARTIAL 可用；
 *     retryMode='media' → POST /media/{id}/runs/retry，后端还会挑**已取消**的运行重做。
 *   不可用时按钮直接不渲染，而不是渲染一个点了报错的按钮。
 */
import { computed } from 'vue'

import type { MediaSnapshot, RunOut } from '../api/types'
import { formatDateTime, formatRunBudget, formatRunDuration } from '../utils/format'
import {
  errorCodeLabel,
  isMediaRunRetryable,
  isRunActive,
  isRunRetryable,
  runStatusLabel,
  runStatusTone,
  stepLabel,
} from '../utils/labels'

const props = defineProps<{
  item: MediaSnapshot | null
  pending: string | null
  /** 标题，默认「当前任务」。 */
  heading?: string
  /**
   * 重试按钮对应哪个接口（默认 'run'）：
   * 'run' → 重试这一次运行；'media' → 重做这张照片最近一次可重做的运行（含已取消）。
   */
  retryMode?: 'run' | 'media'
  /** 重试按钮的文案（默认「重试」）。 */
  retryLabel?: string
}>()

const emit = defineEmits<{
  (event: 'cancel', runId: string): void
  (event: 'retry', runId: string): void
}>()

const run = computed<RunOut | null>(() => {
  const item = props.item
  if (item === null) {
    return null
  }
  // 优先展示仍在运行的这一次；都结束了展示最近一次。
  return item.active_run ?? item.runs[0] ?? null
})

const canCancel = computed(() => run.value !== null && isRunActive(run.value.status))
const canRetry = computed(() => {
  const status = run.value?.status
  if (status === undefined) {
    return false
  }
  return props.retryMode === 'media' ? isMediaRunRetryable(status) : isRunRetryable(status)
})
const runErrorText = computed(() =>
  run.value === null ? null : errorCodeLabel(run.value.error_code),
)
const busy = computed(
  () =>
    props.pending === 'cancel-run' ||
    props.pending === 'retry-run' ||
    props.pending === 'retry-media-run',
)
const retryText = computed(() => props.retryLabel ?? '重试')
/** 重做（媒体级）等待中的文案与运行级不同，避免用户以为在重试同一条运行。 */
const retryPendingText = computed(() => (props.retryMode === 'media' ? '重做中…' : '重试中…'))

const hint = computed<string>(() => {
  if (props.item === null) {
    return '先在右侧「相册」里选择一张照片。'
  }
  if (run.value === null) {
    return '这张照片还没有运行记录。'
  }
  const status = run.value.status
  // 文案跟着「按钮实际会调用哪个接口」走，不写死一句可能与接口不符的话。
  switch (status) {
    case 'QUEUED':
      return '正在排队，请稍等。'
    case 'RUNNING':
      return '正在识别照片并整理资料。'
    case 'WAITING_USER':
      return '请在左侧确认照片里的地点。'
    case 'SUCCEEDED':
      return '讲解已生成。'
    case 'PARTIAL':
      return '讲解已生成，但未查到地点资料，可以重试。'
    case 'FAILED':
      return `失败：可以点「${retryText.value}」重新分析。`
    case 'CANCELLED':
      return props.retryMode === 'media'
        ? `已取消：可以点「${retryText.value}」继续分析。`
        : '已停止，可以在照片旁重新分析。'
    default:
      return `当前状态：${runStatusLabel(status)}。`
  }
})
</script>

<template>
  <section class="runstatus">
    <header class="runstatus__head">
      <h3 class="runstatus__title">{{ heading ?? '当前任务' }}</h3>
      <span v-if="run !== null" class="badge" :class="`badge--${runStatusTone(run.status)}`">
        {{ runStatusLabel(run.status) }}
      </span>
    </header>

    <p v-if="item !== null" class="runstatus__which">
      第 {{ item.media.position }} 张照片 · {{ item.media.original_filename }}
    </p>
    <p class="runstatus__hint">{{ hint }}</p>

    <template v-if="run !== null">
      <div class="runstatus__meta">
        <span>当前步骤：{{ stepLabel(run.current_step) }}</span>

        <span>第 {{ run.attempt }} 次尝试</span>
        <span>耗时：{{ formatRunDuration(run) }}</span>
      </div>

      <p v-if="runErrorText !== null" class="runstatus__error">
        {{ runErrorText }}
      </p>

      <div class="runstatus__actions">
        <button
          v-if="canCancel"
          class="btn btn--danger btn--sm"
          type="button"
          :disabled="busy"
          @click="emit('cancel', run.id)"
        >
          {{ pending === 'cancel-run' ? '取消中…' : '取消运行' }}
        </button>
        <button
          v-if="canRetry"
          class="btn btn--primary btn--sm"
          type="button"
          :disabled="busy"
          @click="emit('retry', run.id)"
        >
          {{
            pending === 'retry-run' || pending === 'retry-media-run' ? retryPendingText : retryText
          }}
        </button>
      </div>

      <!-- 技术细节默认收起：run_id、预算、token 不占主视图 -->
      <details class="details">
        <summary class="details__summary">处理详情</summary>
        <dl class="details__list">
          <dt>run_id</dt>
          <dd class="mono">{{ run.id }}</dd>
          <dt>状态</dt>
          <dd>{{ runStatusLabel(run.status) }}（{{ run.status }}）</dd>
          <dt>步骤</dt>
          <dd>{{ stepLabel(run.current_step) }}（{{ run.current_step }}）</dd>
          <dt>预算用量</dt>
          <dd>{{ formatRunBudget(run) }}</dd>
          <dt>错误码</dt>
          <dd class="mono">{{ run.error_code ?? '—' }}</dd>
          <dt>创建时间</dt>
          <dd>{{ formatDateTime(run.created_at) }}</dd>
          <dt>开始时间</dt>
          <dd>{{ formatDateTime(run.started_at) }}</dd>
          <dt>结束时间</dt>
          <dd>{{ formatDateTime(run.finished_at) }}</dd>
        </dl>
      </details>
    </template>
  </section>
</template>
