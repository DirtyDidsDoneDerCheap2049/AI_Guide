<script setup lang="ts">
import ComposerBar from './ComposerBar.vue'
import MessageThread from './MessageThread.vue'
import CandidatePanel from './CandidatePanel.vue'
import ActivityLog from './ActivityLog.vue'
import { useWorkspace } from '../composables/useWorkspace'
const workspace = useWorkspace()
const { state, selectedMedia } = workspace
</script>
<template>
  <section class="guide-conversation">
    <header class="guide-heading">
      <div>
        <span class="guide-avatar">↗</span>
        <div>
          <h2>旅行导游</h2>
          <p>一起发现、比较、安排</p>
        </div>
      </div>
      <details class="diagnostic-menu">
        <summary title="运行详情">···</summary>
        <ActivityLog :entries="state.activity" />
      </details>
    </header>
    <div class="conversation__scroll">
      <div v-if="!state.messages.length && !state.outbox.length" class="chat-welcome">
        <span class="welcome-mark">✳</span>
        <h3>从你感兴趣的地方聊起</h3>
        <p>可以问一个地点，也可以在右边挑选。收藏和路线会留在这次旅行里。</p>
        <button
          class="suggestion-chip"
          @click="workspace.askQuestion('我收藏了哪些地方？帮我比较一下。')"
        >
          看看我的收藏
        </button>
      </div>
      <MessageThread
        :messages="state.messages"
        :outbox="state.outbox"
        :runs="state.messageRuns"
        :sending="state.sendingMessage"
        @retry-send="workspace.retryOutboxMessage"
        @discard-send="workspace.discardOutboxMessage"
        @resend-answer="workspace.resendQuestionForAnswer"
        @focus-media="workspace.openMediaInAlbum"
      />
      <CandidatePanel
        v-if="selectedMedia?.active_run?.status === 'WAITING_USER'"
        :item="selectedMedia"
        :pending="state.pending"
        @decide="({ runId, request }) => workspace.submitPlaceDecision(runId, request)"
      />
    </div>
    <ComposerBar />
    <p class="guide-disclaimer">AI 建议供参考，开放时间与费用请以当地公布为准</p>
  </section>
</template>
