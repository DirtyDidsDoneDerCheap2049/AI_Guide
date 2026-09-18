<script setup lang="ts">
/**
 * 「已移出相册」——被软删除的照片（D4）。
 *
 * 为什么单独一个列表：工作区快照**不再返回**已移出的照片（后端按 `deleted_at` 过滤），
 * 所以它们只能从 `GET /api/v1/projects/{id}/media/deleted` 单独读。
 *
 * 诚实边界：
 * - 不在挂载时就发请求：用户第一次展开时才读（读过后由 useWorkspace 在移出/恢复后对齐）；
 * - 「恢复」是真实请求，失败显示真实错误，绝不先把那一行从列表里删掉；
 * - 恢复不会让之前被取消的任务复活（后端语义），所以提示里写明"需要时可以重做"。
 */
import { computed, ref } from 'vue'

import { useWorkspace } from '../composables/useWorkspace'
import { formatBytes, formatDateTime } from '../utils/format'
import { mediaStatusLabel, mediaStatusTone } from '../utils/labels'

const workspace = useWorkspace()
const { state } = workspace

const expanded = ref(false)

const deleted = computed(() => state.deletedMedia)
const restoring = computed(() => state.pending === 'restore-media')
/** 正在恢复的那一条（pending 里没有 id，用最近点击的 id 标记按钮状态）。 */
const restoringId = ref<string | null>(null)

function toggle(): void {
  expanded.value = !expanded.value
  if (expanded.value) {
    workspace.ensureDeletedMedia()
  }
}

async function restore(mediaId: string): Promise<void> {
  restoringId.value = mediaId
  try {
    await workspace.restoreMedia(mediaId)
  } finally {
    restoringId.value = null
  }
}

function statusLabel(status: string): string {
  return mediaStatusLabel(status)
}

function statusTone(status: string): string {
  return mediaStatusTone(status)
}
</script>

<template>
  <section class="panel deleted">
    <header class="panel__head">
      <h2 class="panel__title">已移出相册</h2>
      <span v-if="deleted.phase !== 'idle'" class="panel__count"
        >{{ deleted.items.length }} 张</span
      >
      <button class="btn btn--ghost btn--sm" type="button" @click="toggle">
        {{ expanded ? '收起' : '展开' }}
      </button>
    </header>

    <p class="panel__note">移出的照片和笔记会保留，可以随时恢复。</p>

    <template v-if="expanded">
      <p v-if="deleted.phase === 'loading'" class="panel__placeholder">正在读取已移出的照片…</p>

      <p v-else-if="deleted.phase === 'error'" class="field__error" role="status">
        读取失败：{{ deleted.errorText }}

        <button class="btn btn--sm" type="button" @click="workspace.loadDeletedMedia()">
          重试
        </button>
      </p>

      <p v-else-if="deleted.phase === 'ready' && deleted.items.length === 0" class="panel__note">
        目前没有被移出的照片。
      </p>

      <ul v-else-if="deleted.items.length > 0" class="saved__list">
        <li v-for="item in deleted.items" :key="item.id" class="saved__item">
          <div class="saved__head">
            <span class="saved__name">{{ item.original_filename }}</span>
            <span class="badge" :class="`badge--${statusTone(item.status)}`">{{
              statusLabel(item.status)
            }}</span>
            <span class="tag tag--muted">第 {{ item.position }} 张</span>
          </div>
          <p class="saved__meta">
            {{ formatBytes(item.size_bytes) }} · 上传于 {{ formatDateTime(item.created_at) }}
          </p>
          <p v-if="item.note !== null && item.note !== ''" class="saved__meta">
            笔记：{{ item.note }}
          </p>
          <div class="saved__actions">
            <button
              class="btn btn--sm"
              type="button"
              :disabled="restoring"
              @click="restore(item.id)"
            >
              {{ restoring && restoringId === item.id ? '恢复中…' : '恢复到相册' }}
            </button>
          </div>
          <p class="saved__meta">恢复后可以重新分析。</p>
        </li>
      </ul>
    </template>
  </section>
</template>
