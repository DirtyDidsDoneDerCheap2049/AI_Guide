<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { MessageOut, RunOut } from '../api/types'
import type { OutboxEntry } from '../composables/useWorkspace'
import { useWorkspace } from '../composables/useWorkspace'
import { useGuidePlaces } from '../composables/useGuidePlaces'
import { searchMessages } from '../api/client'
import TravelResources from './TravelResources.vue'
const props = defineProps<{
  messages: MessageOut[]
  outbox: OutboxEntry[]
  runs: Record<string, RunOut>
  sending: boolean
}>()
defineEmits<{
  (e: 'retry-send', id: string): void
  (e: 'discard-send', id: string): void
  (e: 'resend-answer', message: MessageOut): void
  (e: 'focus-media', id: string): void
}>()
const guide = useGuidePlaces()
const workspace = useWorkspace()
function stateResearch(message: MessageOut) {
  workspace.state.researchQuery = message.attachments?.places?.[0]?.name ?? ''
  workspace.setRightTab('research')
}
const root = ref<HTMLElement | null>(null)
const editing = ref<string | null>(null)
const deleting = ref<string | null>(null)
const editText = ref('')
const editTarget = ref<MessageOut | null>(null)
const deleteTarget = ref<MessageOut | null>(null)
const saving = ref(false)
const search = ref('')
const searchResult = ref<MessageOut[] | null>(null)
const searchError = ref('')
const searching = ref(false)
let searchGeneration = 0
const visible = computed(() => (searchResult.value ?? props.messages).filter((m) => !m.deleted_at))
async function findMessages() {
  const id = workspace.project.value?.id
  const generation = ++searchGeneration
  if (!id || !search.value.trim()) {
    searchResult.value = null
    return
  }
  searching.value = true
  searchError.value = ''
  try {
    const rows = (await searchMessages(id, search.value)).messages
    if (generation === searchGeneration && workspace.project.value?.id === id)
      searchResult.value = rows
  } catch {
    if (generation === searchGeneration) searchError.value = '没能搜索到记录，请重试。'
  } finally {
    if (generation === searchGeneration) searching.value = false
  }
}
function beginEdit(message: MessageOut) {
  editing.value = message.id
  editText.value = message.content
  editTarget.value = { ...message }
}
function beginDelete(message: MessageOut) {
  deleting.value = message.id
  deleteTarget.value = { ...message }
}
function clearSearch() {
  searchGeneration++
  searchResult.value = null
  search.value = ''
  searching.value = false
}
watch(
  () => workspace.project.value?.id,
  () => {
    clearSearch()
    editing.value = null
    deleting.value = null
    editTarget.value = null
    deleteTarget.value = null
    searchError.value = ''
  },
)
async function change(message: MessageOut, content?: string) {
  saving.value = true
  try {
    if (await workspace.manageMessage(message, content)) {
      editing.value = null
      deleting.value = null
      if (searchResult.value) await findMessages()
    }
  } finally {
    saving.value = false
  }
}
watch(
  () => props.messages,
  () => {
    if (searchResult.value)
      searchResult.value = searchResult.value.map(
        (m) => props.messages.find((p) => p.id === m.id) ?? m,
      )
  },
)
const active = computed(() =>
  Object.values(props.runs).filter((r) => ['QUEUED', 'RUNNING'].includes(r.status)),
)
let scroll: HTMLElement | null = null
let follow = true
// Small, safe subset of formatting. Vue escapes all text; no model HTML is rendered.
function answerLines(content: string) {
  return content.split('\n').map((line) => line.replace(/^#{1,6}\s+/, ''))
}
function inlineParts(line: string) {
  return line.split(/(\*\*[^*]+\*\*)/g).map((text) => ({
    text: text.startsWith('**') && text.endsWith('**') ? text.slice(2, -2) : text,
    bold: text.startsWith('**') && text.endsWith('**'),
  }))
}
function onScroll() {
  if (scroll) follow = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 90
}
onMounted(() => {
  scroll = root.value?.closest('.conversation__scroll') ?? null
  scroll?.addEventListener('scroll', onScroll, { passive: true })
})
onBeforeUnmount(() => scroll?.removeEventListener('scroll', onScroll))
watch(
  () => [props.messages.length, props.outbox.length, active.value.length],
  async () => {
    await nextTick()
    if (follow && scroll) scroll.scrollTop = scroll.scrollHeight
  },
)
</script>
<template>
  <div ref="root" class="chat-thread" aria-live="polite">
    <form class="conversation-search" @submit.prevent="findMessages">
      <input
        v-model="search"
        aria-label="搜索本次旅行对话"
        placeholder="搜索这次旅行的对话"
        maxlength="200"
      /><button class="text-button" :disabled="searching">查找</button
      ><button v-if="searchResult" type="button" class="text-button" @click="clearSearch">
        全部
      </button>
    </form>
    <p v-if="searchError" class="field__error">{{ searchError }}</p>
    <p v-if="searchResult" class="gentle-note">
      找到 {{ visible.length }} 条记录{{
        visible.length === 200 ? '，请缩小关键词查看更早记录' : ''
      }}
    </p>
    <button
      v-if="!searchResult && (messages[0]?.seq ?? 0) > 1"
      class="text-button"
      @click="workspace.loadOlderMessages()"
    >
      查看更早的对话
    </button>
    <article
      v-for="message in visible"
      :key="message.id"
      class="chat-message"
      :class="`chat-message--${message.role}`"
    >
      <div v-if="message.role === 'assistant'" class="message-author"><span>↗</span> AI·Guide</div>
      <form
        v-if="editing === message.id && editTarget"
        class="message-edit"
        @submit.prevent="change(editTarget, editText)"
      >
        <textarea v-model="editText" aria-label="修改这条提问" maxlength="2000" rows="4" />
        <p>后续旧回答将标为待更新。已收藏的地点和路线保留。</p>
        <button class="btn btn--primary" :disabled="saving || !editText.trim()">保存修改</button
        ><button type="button" class="btn" @click="editing = null">取消</button>
      </form>
      <div v-else class="message-body">
        <p v-if="message.role !== 'assistant'">{{ message.content }}</p>
        <div v-else class="answer-copy">
          <p v-for="(line, index) in answerLines(message.content)" :key="index">
            <template v-for="(part, p) in inlineParts(line)" :key="p"
              ><strong v-if="part.bold">{{ part.text }}</strong
              ><template v-else>{{ part.text }}</template></template
            ><br v-if="!line" />
          </p>
        </div>
      </div>
      <div class="message-controls">
        <small v-if="message.edited_at">已编辑</small
        ><button
          v-if="message.role === 'user'"
          class="text-button"
          :disabled="saving || sending"
          @click="beginEdit(message)"
        >
          编辑</button
        ><button
          v-if="message.role === 'user'"
          class="text-button"
          :disabled="saving || sending"
          @click="workspace.askQuestion(message.content)"
        >
          重新提问</button
        ><button class="text-button" :disabled="saving || sending" @click="beginDelete(message)">
          删除
        </button>
      </div>
      <div v-if="deleting === message.id && deleteTarget" class="message-delete-confirm">
        <p>删除这条记录？不会撤销已保存的地点或路线。</p>
        <button class="btn" :disabled="saving" @click="change(deleteTarget)">确认删除</button
        ><button class="text-button" @click="deleting = null">保留</button>
      </div>
      <p v-if="message.stale" class="stale-answer">前面的对话已改动，这份回答可能不再适用。</p>
      <template v-if="!message.stale">
        <button
          v-if="message.status === 'FAILED' && message.role === 'assistant'"
          class="btn"
          :disabled="sending"
          @click="$emit('resend-answer', message)"
        >
          重新回答
        </button>
        <div v-if="message.attachments?.places?.length" class="message-places">
          <button
            v-for="place in message.attachments.places"
            :key="place.provider_place_id ?? place.name"
            class="message-place"
            @click="guide.select(place)"
          >
            <span class="place-pin">↗</span
            ><span
              ><strong>{{ place.name }}</strong
              ><small>{{ place.region }}{{ place.fixture ? ' · 测试数据' : '' }}</small></span
            ><span>→</span>
          </button>
        </div>
        <div v-if="message.attachments?.route_change?.route_id" class="message-actions">
          <button class="btn" @click="workspace.setRightTab('routes')">查看更新后的路线 ↗</button>
        </div>
        <p
          v-for="error in message.attachments?.search_errors ?? []"
          :key="error"
          class="gentle-note"
        >
          {{ error }}
        </p>
        <div
          v-if="message.role === 'assistant' && message.attachments?.followups?.length"
          class="followup-list"
        >
          <button
            v-for="question in message.attachments.followups"
            :key="question"
            class="suggestion-chip"
            :disabled="sending"
            @click="workspace.askQuestion(question)"
          >
            {{ question }} <span>↗</span>
          </button>
        </div>
        <TravelResources
          v-if="message.attachments?.resources?.length"
          :resources="message.attachments.resources"
        />
        <button
          v-if="message.role === 'assistant' && message.attachments?.places?.length"
          class="text-button"
          @click="stateResearch(message)"
        >
          查看相关图片和攻略 ↗
        </button>
      </template>
    </article>
    <article v-for="entry in outbox" :key="entry.clientId" class="chat-message chat-message--user">
      <div class="message-body">
        <p>{{ entry.content }}</p>
      </div>
      <small v-if="entry.status === 'sending'">发送中…</small>
      <div v-else class="outbox-error">
        <p>{{ entry.errorText }}</p>
        <button
          class="text-button"
          :disabled="sending"
          @click="$emit('retry-send', entry.clientId)"
        >
          重试发送</button
        ><button class="text-button muted" @click="$emit('discard-send', entry.clientId)">
          取消
        </button>
      </div>
    </article>
    <div v-if="active.length" class="answer-pending" role="status">
      <span class="thinking-dots"><i></i><i></i><i></i></span>导游正在整理资料<button
        class="text-button"
        @click="workspace.cancelCurrentRun(active[0]!.id)"
      >
        停止
      </button>
    </div>
  </div>
</template>
