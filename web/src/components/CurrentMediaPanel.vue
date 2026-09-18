<script setup lang="ts">
/**
 * 照片详情（右侧「相册」标签的下半部分）：大图 + 元数据 + 已确认地点 + 我的笔记 + 照片操作 + 当前任务。
 *
 * 真实动作（每一个都对应后端一个接口，没有点了不动的按钮）：
 * - 「在地图上看」：切到地图标签并聚焦这个地点（地点来自后端快照）；
 * - 「查看讲解卡片」：切到卡片标签；
 * - 「收藏这个地点」（FavoritePlaceButton）：只对**已核实**的地点开放；
 * - 「保存笔记」：PATCH /api/v1/media/{id}（笔记是用户资产，模型结果不会覆盖它）；
 * - 「重新分析这张」：POST /api/v1/media/{id}/analyze（202 = 已入队，不是已完成）；
 * - 「修改地点」：POST /api/v1/media/{id}/place（带地点版本做条件更新；可选同时重新生成讲解）；
 * - 「移出相册」：DELETE /api/v1/media/{id}（软删除，可恢复，未完成任务会被后端取消）；
 * - 「重做」由 RunStatusCard 以 retryMode='media' 提供（POST /api/v1/media/{id}/runs/retry）；
 * - 取消运行由 RunStatusCard 提供（与后端状态机一致）。
 *
 * 关于「删除照片」：后端只有**软删除**（`deleted_at`，可恢复），没有物理删除接口，
 * 所以界面上的说法是「移出相册」而不是「彻底删除」。
 */
import { computed, ref, watch } from 'vue'

import FavoritePlaceButton from './FavoritePlaceButton.vue'
import RunStatusCard from './RunStatusCard.vue'
import type { MediaSnapshot } from '../api/types'
import { useWorkspace } from '../composables/useWorkspace'
import { formatBytes, formatDimensions } from '../utils/format'
import { mediaStatusLabel, mediaStatusTone } from '../utils/labels'

const props = defineProps<{
  item: MediaSnapshot | null
  pending: string | null
}>()

const emit = defineEmits<{
  (event: 'cancel', runId: string): void
  (event: 'retry', runId: string): void
  (event: 'show-on-map', placeId: string): void
  (event: 'open-card', mediaId: string): void
}>()

const workspace = useWorkspace()
const { state } = workspace

const media = computed(() => props.item?.media ?? null)
const place = computed(() => props.item?.place ?? null)
const hasCoords = computed(
  () => place.value !== null && place.value.latitude !== null && place.value.longitude !== null,
)
const busy = computed(() => props.pending !== null)

// ------------------------------------------------------------------ 用户笔记
//
// 草稿留在组件里（视图状态），只有"保存"才写服务器。规则：
//  - 没有本地改动时跟随服务器（多标签页/别处改动会自动同步）；
//  - 有本地改动时**不覆盖**用户正在写的内容（快照刷新、模型结果都不会动它）。
const noteValue = ref('')
/** 草稿所基于的服务器值：值相等 = 没有本地改动。 */
const noteBase = ref('')
const noteDrafts = new Map<string, { value: string; base: string }>()

watch(
  () => [props.item?.media.id ?? '', props.item?.media.note ?? ''] as const,
  ([mediaId], previous) => {
    const server = props.item?.media.note ?? ''
    const changedMedia = previous === undefined || previous[0] !== mediaId
    if (changedMedia && previous?.[0])
      noteDrafts.set(previous[0], { value: noteValue.value, base: noteBase.value })
    const draft = noteDrafts.get(mediaId)
    if (changedMedia && draft && draft.value !== draft.base) {
      noteValue.value = draft.value
      noteBase.value = server
      return
    }

    if (changedMedia || noteValue.value === noteBase.value) {
      noteValue.value = server
    }
    noteBase.value = server
  },
  { immediate: true },
)

const noteDirty = computed(() => noteValue.value !== noteBase.value)
const noteSaving = computed(
  () => state.noteSave.status === 'saving' && state.noteSave.mediaId === media.value?.id,
)
const noteSaved = computed(
  () => state.noteSave.status === 'saved' && state.noteSave.mediaId === media.value?.id,
)
const noteSaveError = computed(() =>
  state.noteSave.status === 'error' && state.noteSave.mediaId === media.value?.id
    ? state.noteSave
    : null,
)

function resetNoteDraft(): void {
  noteValue.value = noteBase.value
}

async function saveNote(): Promise<void> {
  const current = media.value
  if (current === null) {
    return
  }
  const text = noteValue.value.trim()
  // 留空保存 = 清空笔记（服务器里 note 为 null），这一点在下面的提示里写明。
  const submitted = noteValue.value
  const ok = await workspace.saveMediaNote(current.id, text === '' ? null : submitted)
  if (ok) {
    noteDrafts.delete(current.id)
    if (media.value?.id === current.id) noteBase.value = submitted
  }
}

// ------------------------------------------------------------------ 修改地点

const showPlaceForm = ref(false)
const placeName = ref('')
const placeAddress = ref('')
const placeRegion = ref('')
const placeRegenerate = ref(false)
const placeHint = ref<string | null>(null)
watch(
  () => media.value?.id,
  () => {
    showPlaceForm.value = false
    placeHint.value = null
  },
)

function togglePlaceForm(): void {
  const current = place.value
  showPlaceForm.value = !showPlaceForm.value
  placeHint.value = null
  if (showPlaceForm.value && current !== null) {
    placeName.value = current.name
    placeAddress.value = current.address ?? ''
    placeRegion.value = current.region ?? ''
    placeRegenerate.value = false
  }
}

async function submitPlace(): Promise<void> {
  const current = media.value
  if (current === null) {
    return
  }
  if (placeName.value.trim() === '') {
    placeHint.value = '地点名称必填。'
    return
  }
  placeHint.value = null
  const ok = await workspace.changeMediaPlace(current.id, {
    name: placeName.value,
    address: placeAddress.value.trim() === '' ? null : placeAddress.value.trim(),
    region: placeRegion.value.trim() === '' ? null : placeRegion.value.trim(),
    regenerate: placeRegenerate.value,
  })
  if (ok) {
    showPlaceForm.value = false
  }
}

/** 最近一次改地点的真实结果（只显示当前这张照片的）。 */
const lastPlaceChange = computed(() => {
  const change = state.lastPlaceChange
  if (change === null || change.mediaId !== media.value?.id) {
    return null
  }
  return change
})

// ------------------------------------------------------------------ 移出相册

function confirmRemove(): void {
  const current = media.value
  if (current === null) {
    return
  }
  const ok = window.confirm(
    `把「${current.original_filename}」移出相册？之后可以恢复，正在进行的分析会停止。`,
  )
  if (ok) {
    void workspace.removeMedia(current.id)
  }
}

async function analyze(): Promise<void> {
  const current = media.value
  if (current !== null) {
    await workspace.analyzeMedia(current.id)
  }
}
</script>

<template>
  <section class="panel viewer">
    <header class="panel__head">
      <h2 class="panel__title">照片详情</h2>
      <span v-if="media !== null" class="badge" :class="`badge--${mediaStatusTone(media.status)}`">
        {{ mediaStatusLabel(media.status) }}
      </span>
    </header>

    <p v-if="media === null" class="panel__placeholder">从上面的相册里选择一张照片。</p>

    <template v-else>
      <figure class="viewer__figure">
        <img class="viewer__img" :src="media.content_url" :alt="media.original_filename" />
        <figcaption class="viewer__caption">
          第 {{ media.position }} 张 · {{ media.original_filename }} ·
          {{ formatBytes(media.size_bytes) }} · {{ formatDimensions(media.width, media.height) }} ·
          {{ media.mime_type }}
        </figcaption>
      </figure>

      <div v-if="place !== null" class="confirmed">
        <p class="confirmed__line">
          <strong>已确认地点：</strong>{{ place.name }}
          <span class="tag">{{
            place.confirmed_by === 'user_input' ? '用户填写' : '来自候选'
          }}</span>
        </p>
        <p v-if="place.region !== null" class="confirmed__meta">区域：{{ place.region }}</p>
        <p v-if="place.address !== null" class="confirmed__meta">地址：{{ place.address }}</p>
        <p v-if="!hasCoords" class="confirmed__meta">尚未确定地图位置。</p>
        <div class="confirmed__actions">
          <button
            v-if="hasCoords"
            class="btn btn--sm"
            type="button"
            @click="emit('show-on-map', place.id)"
          >
            在地图上看
          </button>
          <button class="btn btn--sm" type="button" @click="emit('open-card', media.id)">
            查看讲解卡片
          </button>
        </div>
        <!-- 收藏只对**已核实**的地点开放；未核实时按钮禁用并写明原因。 -->
        <FavoritePlaceButton :item="item" />
      </div>

      <p v-else class="panel__note">先确认照片里的地点，再看看它有什么值得了解的。</p>

      <!-- 我的笔记：用户资产，模型结果不会覆盖 -->
      <section class="note">
        <h3 class="note__title">我的笔记</h3>

        <textarea
          v-model="noteValue"
          class="note__input"
          rows="3"
          maxlength="2000"
          placeholder="例如：下午三点后光线最好；门口那家店周日休息"
        ></textarea>
        <div class="note__actions">
          <button
            class="btn btn--primary btn--sm"
            type="button"
            :disabled="!noteDirty || busy"
            @click="saveNote"
          >
            {{ noteSaving ? '保存中…' : '保存笔记' }}
          </button>
          <button
            class="btn btn--ghost btn--sm"
            type="button"
            :disabled="!noteDirty || busy"
            @click="resetNoteDraft"
          >
            撤销修改
          </button>
          <button
            class="btn btn--ghost btn--sm"
            type="button"
            :disabled="busy || noteValue === ''"
            @click="noteValue = ''"
          >
            清空输入
          </button>
          <span class="note__counter">{{ noteValue.length }}/2000</span>
        </div>
        <p v-if="noteDirty" class="note__hint">笔记尚未保存。</p>
        <p v-else-if="noteSaved" class="note__ok" role="status">已保存。</p>
        <p v-if="noteSaveError !== null" class="note__error" role="status">
          保存失败：{{ noteSaveError.errorText }}

          <button class="btn btn--sm" type="button" :disabled="busy" @click="saveNote">
            重试保存
          </button>
        </p>
      </section>

      <!-- 照片级操作 -->
      <section class="mediaops">
        <h3 class="note__title">照片操作</h3>
        <div class="note__actions">
          <button class="btn btn--sm" type="button" :disabled="busy" @click="analyze">
            {{ pending === 'analyze-media' ? '提交中…' : '重新分析这张' }}
          </button>
          <button class="btn btn--sm" type="button" :disabled="busy" @click="togglePlaceForm">
            {{ showPlaceForm ? '收起地点表单' : '修改地点' }}
          </button>
          <button
            class="btn btn--danger btn--sm"
            type="button"
            :disabled="busy"
            @click="confirmRemove"
          >
            {{ pending === 'delete-media' ? '移出中…' : '移出相册' }}
          </button>
        </div>

        <p v-if="lastPlaceChange !== null" class="alert alert--info" role="status">
          地点已改为「{{ lastPlaceChange.placeName }}」。{{
            lastPlaceChange.regenerateRunId ? '正在更新讲解。' : '旧讲解已过期，可以重新生成。'
          }}
        </p>

        <form v-if="showPlaceForm" class="place" @submit.prevent="submitPlace">
          <p class="panel__note">修改后会重新查询位置，旧讲解将标记为过期。</p>
          <label class="field">
            <span class="field__label">地点名称 <em>（必填）</em></span>
            <input
              v-model="placeName"
              class="field__input"
              type="text"
              maxlength="200"
              placeholder="例如：西湖断桥"
            />
          </label>
          <label class="field">
            <span class="field__label">地址（可选）</span>
            <input
              v-model="placeAddress"
              class="field__input"
              type="text"
              maxlength="400"
              placeholder="例如：杭州市西湖区北山街"
            />
          </label>
          <label class="field">
            <span class="field__label">区域（可选）</span>
            <input
              v-model="placeRegion"
              class="field__input"
              type="text"
              maxlength="200"
              placeholder="例如：浙江省杭州市"
            />
          </label>
          <label class="check">
            <input v-model="placeRegenerate" type="checkbox" />
            同时更新讲解
          </label>
          <p v-if="placeHint !== null" class="field__error">{{ placeHint }}</p>
          <div class="note__actions">
            <button class="btn btn--primary btn--sm" type="submit" :disabled="busy">
              {{ pending === 'change-place' ? '提交中…' : '保存地点' }}
            </button>
            <button
              class="btn btn--ghost btn--sm"
              type="button"
              :disabled="busy"
              @click="showPlaceForm = false"
            >
              取消
            </button>
          </div>
        </form>
      </section>

      <RunStatusCard
        :item="item"
        :pending="pending"
        heading="这张照片的任务"
        retry-mode="media"
        retry-label="重做"
        @cancel="emit('cancel', $event)"
        @retry="emit('retry', $event)"
      />
    </template>
  </section>
</template>
