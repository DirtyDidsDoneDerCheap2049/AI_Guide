<script setup lang="ts">
import { ref } from 'vue'
import { useWorkspace } from '../composables/useWorkspace'
defineProps<{ compact?: boolean }>()
const workspace = useWorkspace()
const { state, selectedMedia } = workspace
const picker = ref<HTMLInputElement | null>(null)
function submitOnEnter(event: KeyboardEvent) {
  if (event.isComposing) return
  event.preventDefault()
  void workspace.sendDraftQuestion()
}
function upload(event: Event) {
  const el = event.target as HTMLInputElement
  const files = Array.from(el.files ?? [])
  el.value = ''
  if (files.length) void workspace.uploadFiles(files)
}
</script>
<template>
  <form class="guide-composer" @submit.prevent="workspace.sendDraftQuestion()">
    <button
      v-if="state.composerScope === 'photo' && selectedMedia"
      class="context-pill"
      type="button"
      @click="workspace.setComposerScope('workspace')"
    >
      照片：{{ selectedMedia.place?.name || selectedMedia.media.original_filename }} <span>×</span>
    </button>
    <textarea
      v-model="state.draftQuestion"
      aria-label="给导游发消息"
      placeholder="继续问，或告诉我想怎么调整…"
      maxlength="2000"
      rows="2"
      @keydown.enter.exact="submitOnEnter"
    />
    <div class="guide-composer-actions">
      <button
        class="icon-button"
        type="button"
        title="添加照片"
        aria-label="添加照片"
        @click="picker?.click()"
      >
        ＋</button
      ><button
        v-if="selectedMedia && state.composerScope !== 'photo'"
        type="button"
        class="text-button"
        @click="workspace.setComposerScope('photo')"
      >
        问这张照片</button
      ><span class="composer-key">Enter 发送</span
      ><button
        class="send-button"
        aria-label="发送消息"
        :disabled="state.sendingMessage || !state.draftQuestion.trim()"
      >
        ↑
      </button>
    </div>
    <input
      ref="picker"
      type="file"
      class="hidden-input"
      accept="image/jpeg,image/png,image/webp"
      multiple
      @change="upload"
    />
    <p v-if="state.composerHint" class="field__error" role="status">{{ state.composerHint }}</p>
  </form>
</template>
