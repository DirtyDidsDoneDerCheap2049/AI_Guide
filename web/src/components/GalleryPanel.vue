<script setup lang="ts">
/**
 * 小相册：最多 3 张图，逐张串行上传。
 *
 * 详情面板提供移出与恢复；相册负责浏览、选择和上传。
 */
import { ref } from 'vue'

import type { MediaAssetOut } from '../api/types'
import type { UploadProgress } from '../composables/useWorkspace'
import { formatBytes, formatDimensions } from '../utils/format'
import { mediaStatusLabel, mediaStatusTone } from '../utils/labels'

const props = defineProps<{
  media: MediaAssetOut[]
  selectedId: string | null
  maxCount: number
  remaining: number
  uploading: UploadProgress | null
  busy: boolean
}>()

const emit = defineEmits<{
  (event: 'select', mediaId: string): void
  (event: 'upload', files: File[]): void
}>()

const fileInput = ref<HTMLInputElement | null>(null)

function openPicker(): void {
  if (props.busy || props.remaining === 0) {
    return
  }
  fileInput.value?.click()
}

function onPicked(event: Event): void {
  const input = event.target as HTMLInputElement
  const files = input.files === null ? [] : Array.from(input.files)
  // 立刻清空 value：否则再次选择同一个文件不会触发 change。
  input.value = ''
  if (files.length > 0) {
    emit('upload', files)
  }
}

function sizeText(media: MediaAssetOut): string {
  return formatBytes(media.size_bytes)
}

function dimensionText(media: MediaAssetOut): string {
  return formatDimensions(media.width, media.height)
}
</script>

<template>
  <section class="panel gallery">
    <header class="panel__head">
      <h2 class="panel__title">相册</h2>
      <span class="panel__count">{{ media.length }}/{{ maxCount }}</span>
    </header>

    <div v-if="uploading !== null" class="gallery__progress">
      正在上传 {{ uploading.index }}/{{ uploading.total }}：{{ uploading.filename }}
    </div>

    <ul class="gallery__list">
      <li v-for="item in media" :key="item.id">
        <button
          type="button"
          class="thumb"
          :class="{ 'thumb--active': item.id === selectedId }"
          :disabled="busy"
          @click="emit('select', item.id)"
        >
          <img
            class="thumb__img"
            :src="item.content_url"
            :alt="item.original_filename"
            loading="lazy"
          />
          <span class="thumb__pos">第 {{ item.position }} 张</span>
          <span class="badge" :class="`badge--${mediaStatusTone(item.status)}`">{{
            mediaStatusLabel(item.status)
          }}</span>
          <span class="thumb__meta" :title="item.original_filename">{{
            item.original_filename
          }}</span>
          <span class="thumb__sub">{{ sizeText(item) }} · {{ dimensionText(item) }}</span>
        </button>
      </li>
      <li v-if="media.length === 0" class="gallery__empty">还没有图片，先上传一张吧。</li>
    </ul>

    <input
      ref="fileInput"
      class="gallery__input"
      type="file"
      accept="image/jpeg,image/png,image/webp"
      multiple
      @change="onPicked"
    />

    <button
      class="btn btn--ghost btn--block"
      type="button"
      :disabled="busy || remaining === 0"
      @click="openPicker"
    >
      {{ remaining === 0 ? '已达 3 张上限' : `选择图片上传（还能放 ${remaining} 张）` }}
    </button>
    <p class="panel__note">
      支持 JPG / PNG / WebP，单张不超过 8MB；选择多张时逐张串行上传。 上传后会帮你识别照片里的地点。
    </p>
  </section>
</template>
