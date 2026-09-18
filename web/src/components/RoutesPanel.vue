<script setup lang="ts">
import { nextTick, ref, onMounted, onBeforeUnmount, watch } from 'vue'
import PlaceSearchPanel from './PlaceSearchPanel.vue'
import { usePlacesRoutes } from '../composables/usePlacesRoutes'
import { useWorkspace } from '../composables/useWorkspace'
import { formatDistanceKm, formatDurationText } from '../utils/format'
const places = usePlacesRoutes()
const { state, selectedDraft } = places
const workspace = useWorkspace()
const searchOpen = ref(false)
const searchContainer = ref<HTMLElement | null>(null)
async function openSearch() {
  searchOpen.value = true
  places.clearSearch()
  await nextTick()
  searchContainer.value?.querySelector('input')?.focus()
}
watch(
  () => state.projectId,
  () => {
    searchOpen.value = false
  },
)
onMounted(() => places.ensureRoutes())
// Wait for a stable edit; pending requests are serialized by the store.
let timer: ReturnType<typeof setTimeout> | undefined
const attempted = new Set<string>()
onBeforeUnmount(() => clearTimeout(timer))
watch(
  () =>
    [selectedDraft.value?.id, selectedDraft.value?.input_version, state.routes.pending] as const,
  () => {
    clearTimeout(timer)
    const draft = selectedDraft.value
    if (
      !draft ||
      draft.stops.length < 2 ||
      state.routes.pending ||
      draft.current_revision?.is_current ||
      draft.route_unavailable_reason?.startsWith('compute_failed')
    )
      return
    const key = `${draft.id}:${draft.input_version}`
    if (attempted.has(key)) return
    timer = setTimeout(() => {
      if (!state.routes.pending && selectedDraft.value?.id === draft.id) {
        attempted.add(key)
        void places.computeDraft(draft.id)
      }
    }, 650)
  },
)
</script>
<template>
  <section class="map-side-panel route-editor">
    <div class="section-heading">
      <h2>我的路线</h2>
      <button
        class="text-button"
        :disabled="state.routes.pending !== null"
        @click="places.createDraft(`路线 ${state.routes.drafts.length + 1}`, 'walking')"
      >
        ＋ 新路线
      </button>
    </div>
    <select
      v-if="state.routes.drafts.length > 1"
      class="field__input route-select"
      :value="state.routes.selectedId"
      aria-label="切换路线"
      @change="places.selectDraft(($event.target as HTMLSelectElement).value)"
    >
      <option v-for="draft in state.routes.drafts" :key="draft.id" :value="draft.id">
        {{ draft.name }}
      </option>
    </select>
    <div v-if="!selectedDraft && !searchOpen" class="empty-state">
      <span>⌁</span>
      <h3>把想去的地方连起来</h3>
      <p>添加第一个地点，就可以开始安排。</p>
    </div>
    <template v-if="selectedDraft">
      <input
        class="route-name"
        aria-label="路线名称"
        :value="selectedDraft.name"
        :disabled="state.routes.pending !== null"
        @change="places.renameDraft(selectedDraft.id, ($event.target as HTMLInputElement).value)"
      />
      <div class="segmented">
        <button
          :class="{ active: selectedDraft.mode === 'walking' }"
          :disabled="!!state.routes.pending"
          @click="places.setDraftMode(selectedDraft.id, 'walking')"
        >
          步行</button
        ><button
          :class="{ active: selectedDraft.mode === 'driving' }"
          :disabled="!!state.routes.pending"
          @click="places.setDraftMode(selectedDraft.id, 'driving')"
        >
          驾车
        </button>
      </div>
      <ol class="route-stops">
        <li v-for="(stop, index) in selectedDraft.stops" :key="`${index}-${stop.name}`">
          <span class="stop-number">{{ index + 1 }}</span
          ><strong>{{ stop.name }}</strong>
          <div class="stop-actions">
            <button
              class="icon-button"
              :disabled="index === 0 || !!state.routes.pending"
              aria-label="上移地点"
              @click="places.moveStop(selectedDraft.id, index, -1)"
            >
              ↑</button
            ><button
              class="icon-button"
              :disabled="index === selectedDraft.stops.length - 1 || !!state.routes.pending"
              aria-label="下移地点"
              @click="places.moveStop(selectedDraft.id, index, 1)"
            >
              ↓</button
            ><button
              class="icon-button"
              :disabled="!!state.routes.pending"
              aria-label="移除地点"
              @click="places.removeStop(selectedDraft.id, index)"
            >
              ×
            </button>
          </div>
        </li>
      </ol>
    </template>
    <div v-if="searchOpen" ref="searchContainer" class="route-place-picker">
      <div class="section-heading">
        <h3>添加地点</h3>
        <button class="text-button" @click="searchOpen = false">收起</button>
      </div>
      <PlaceSearchPanel route-mode />
    </div>
    <button
      v-else
      class="btn route-add-place"
      :disabled="!!state.routes.pending"
      @click="openSearch"
    >
      ＋ 添加地点
    </button>
    <p v-if="!selectedDraft && state.routes.errorText" class="field__error" role="alert">
      {{ state.routes.errorText }}
    </p>
    <template v-if="selectedDraft">
      <p v-if="selectedDraft.stops.length < 2" class="gentle-note">
        {{
          selectedDraft.stops.length ? '再加一个地点，就可以查看路线。' : '搜索并添加想去的地点。'
        }}
      </p>
      <div v-if="selectedDraft.current_revision?.is_current" class="route-summary">
        <span>预计{{ selectedDraft.mode === 'walking' ? '步行' : '驾车' }}</span
        ><strong>{{ formatDurationText(selectedDraft.current_revision.duration_seconds) }}</strong>
        <p>
          {{ formatDistanceKm(selectedDraft.current_revision.distance_meters) }} ·
          {{
            selectedDraft.current_revision.provider.includes('fixture') ? '测试路线' : '高德地图'
          }}
        </p>
      </div>
      <p
        v-else-if="places.computingDraftId.value === selectedDraft.id"
        class="gentle-note"
        role="status"
      >
        正在计算路线…
      </p>
      <p
        v-else-if="selectedDraft.route_unavailable_reason?.startsWith('compute_failed')"
        class="field__error"
      >
        暂时没能算出路线，地点已保留。可以稍后重试或更换出行方式。
      </p>
      <p v-else-if="selectedDraft.stops.length >= 2" class="gentle-note">
        路线已更新，正在准备重新计算。
      </p>
      <p v-if="state.routes.errorText" class="field__error" role="alert">
        {{ state.routes.errorText }}
      </p>
      <button
        v-if="selectedDraft.stops.length >= 2 && !selectedDraft.current_revision?.is_current"
        class="btn"
        :disabled="!!state.routes.pending"
        @click="places.computeDraft(selectedDraft.id)"
      >
        重新计算
      </button>
      <button
        class="suggestion-chip route-ask"
        @click="workspace.askQuestion(`看看我的路线「${selectedDraft.name}」，有什么建议？`)"
      >
        问导游：这条路线怎么逛？ ↗
      </button>
    </template>
  </section>
</template>
<style scoped>
.route-add-place {
  width: 100%;
  margin-top: 16px;
  border-style: dashed;
}
.route-place-picker {
  margin-top: 16px;
  padding-top: 16px;
  border-top: 1px solid var(--border);
}
.route-place-picker :deep(.search-results) {
  max-height: 340px;
  overflow-y: auto;
}
</style>
