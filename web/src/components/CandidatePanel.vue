<script setup lang="ts">
/**
 * 候选确认区：候选名称 / 视觉依据 / 置信度 / 不确定性 + 确认、纠正、拒绝。
 *
 * 确认与纠正都提交到 POST /api/v1/runs/{id}/confirm-place；只有 WAITING_USER 的运行可以提交
 * （否则后端返回 409 run_not_waiting_user）。
 */
import { computed, ref, watch } from 'vue'

import type { CandidateOut, ConfirmPlaceRequest, MediaSnapshot, RunOut } from '../api/types'
import { confidencePercent, formatConfidence } from '../utils/format'
import { runStatusLabel } from '../utils/labels'

const props = defineProps<{
  item: MediaSnapshot | null
  pending: string | null
}>()

const emit = defineEmits<{
  (event: 'decide', payload: { runId: string; request: ConfirmPlaceRequest }): void
}>()

const selectedCandidateId = ref<string | null>(null)
const correcting = ref(false)
const correctName = ref('')
const correctAddress = ref('')
const correctRegion = ref('')
const formError = ref<string | null>(null)

const waitingRun = computed<RunOut | null>(() => {
  const item = props.item
  if (item === null) {
    return null
  }
  if (item.active_run !== null && item.active_run.status === 'WAITING_USER') {
    return item.active_run
  }
  return item.runs.find((entry) => entry.status === 'WAITING_USER') ?? null
})

const candidates = computed<CandidateOut[]>(() => props.item?.candidates ?? [])
const pendingCandidates = computed<CandidateOut[]>(() =>
  candidates.value.filter((entry) => entry.status === 'PENDING'),
)
const confirmedPlace = computed(() => props.item?.place ?? null)
const busy = computed(() => props.pending === 'confirm-place')

// 候选集合变化时（candidate.ready、确认后刷新、切换图片）重置选择：
// 只在当前选择已经失效时才改，避免用户刚点选的候选被一次快照刷新清掉。
watch(
  () =>
    `${props.item?.media.id ?? ''}|${candidates.value.map((entry) => `${entry.id}:${entry.status}`).join(',')}`,
  () => {
    if (
      selectedCandidateId.value === null ||
      !pendingCandidates.value.some((entry) => entry.id === selectedCandidateId.value)
    ) {
      selectedCandidateId.value = pendingCandidates.value[0]?.id ?? null
    }
    if (waitingRun.value === null) {
      correcting.value = false
      formError.value = null
    }
  },
  { immediate: true },
)

function confirmSelected(): void {
  const run = waitingRun.value
  if (run === null) {
    return
  }
  if (selectedCandidateId.value === null) {
    formError.value = '请先选择一个候选地点'
    return
  }
  formError.value = null
  emit('decide', {
    runId: run.id,
    request: { decision: 'confirm', candidate_id: selectedCandidateId.value },
  })
}

function startCorrecting(): void {
  const candidate = pendingCandidates.value.find((entry) => entry.id === selectedCandidateId.value)
  correcting.value = true
  formError.value = null
  // 纠正表单预填当前候选，用户只需要改错的部分。
  correctName.value = candidate?.name ?? ''
  correctAddress.value = candidate?.address ?? ''
  correctRegion.value = candidate?.region ?? ''
}

function submitCorrection(): void {
  const run = waitingRun.value
  if (run === null) {
    return
  }
  const name = correctName.value.trim()
  if (name === '') {
    formError.value = '请填写正确的地点名称'
    return
  }
  const address = correctAddress.value.trim()
  const region = correctRegion.value.trim()
  formError.value = null
  emit('decide', {
    runId: run.id,
    request: {
      decision: 'correct',
      name,
      address: address === '' ? null : address,
      region: region === '' ? null : region,
    },
  })
}

function rejectAll(): void {
  const run = waitingRun.value
  if (run === null) {
    return
  }
  // 拒绝是不可撤销的：后端会把这次运行置为 CANCELLED、图片置为 REJECTED，所以先确认一次。
  const ok = window.confirm('拒绝后将取消这次分析，且无法撤销。确定拒绝所有候选地点吗？')
  if (!ok) {
    return
  }
  formError.value = null
  emit('decide', { runId: run.id, request: { decision: 'reject' } })
}
</script>

<template>
  <section class="panel candidates">
    <header class="panel__head">
      <h2 class="panel__title">候选地点确认</h2>
      <span v-if="waitingRun !== null" class="badge badge--waiting">{{
        runStatusLabel(waitingRun.status)
      }}</span>
    </header>

    <p v-if="item === null" class="panel__placeholder">先选择一张图片。</p>

    <template v-else>
      <!-- 已确认的地点：确认结果来自后端 places 表，不在这里重复供应商事实 -->
      <div v-if="confirmedPlace !== null" class="confirmed">
        <p class="confirmed__line">
          <strong>已确认地点：</strong>{{ confirmedPlace.name }}
          <span class="tag">{{
            confirmedPlace.confirmed_by === 'user_input' ? '用户填写' : '来自候选'
          }}</span>
        </p>
        <p v-if="confirmedPlace.address !== null" class="confirmed__meta">
          地址：{{ confirmedPlace.address }}
        </p>
        <p v-if="confirmedPlace.region !== null" class="confirmed__meta">
          地区：{{ confirmedPlace.region }}
        </p>
        <p class="panel__note">地点服务事实与模型讲解已分区展示在右侧导游卡片中。</p>
      </div>

      <!-- 等待确认：展示候选并允许确认/纠正/拒绝 -->
      <template v-if="waitingRun !== null && candidates.length > 0">
        <ul class="cand-list">
          <li v-for="candidate in candidates" :key="candidate.id">
            <label
              class="cand"
              :class="{
                'cand--selected': candidate.id === selectedCandidateId,
                'cand--done': candidate.status !== 'PENDING',
              }"
            >
              <input
                v-model="selectedCandidateId"
                class="cand__radio"
                type="radio"
                name="candidate"
                :value="candidate.id"
                :disabled="candidate.status !== 'PENDING' || busy"
              />
              <span class="cand__head">
                <span class="cand__rank">#{{ candidate.rank }}</span>
                <span class="cand__name">{{ candidate.name }}</span>
                <span v-if="candidate.status !== 'PENDING'" class="tag">{{
                  candidate.status === 'CONFIRMED' ? '已确认' : '已拒绝'
                }}</span>
              </span>
              <span v-if="candidate.address !== null" class="cand__meta"
                >地址：{{ candidate.address }}</span
              >
              <span v-if="candidate.region !== null" class="cand__meta"
                >地区：{{ candidate.region }}</span
              >

              <span class="cand__confidence">
                <span class="cand__confidence-label"
                  >置信度 {{ formatConfidence(candidate.confidence) }}</span
                >
                <span class="bar"
                  ><span
                    class="bar__fill"
                    :style="{ width: `${confidencePercent(candidate.confidence)}%` }"
                  ></span
                ></span>
              </span>

              <span class="cand__block"><strong>视觉依据：</strong>{{ candidate.rationale }}</span>
              <span
                v-if="candidate.uncertainty !== null && candidate.uncertainty !== ''"
                class="cand__block cand__block--warn"
              >
                <strong>不确定性：</strong>{{ candidate.uncertainty }}
              </span>
            </label>
          </li>
        </ul>

        <p v-if="formError !== null" class="field__error">{{ formError }}</p>

        <div v-if="!correcting" class="cand__actions">
          <button class="btn btn--primary" type="button" :disabled="busy" @click="confirmSelected">
            {{ busy ? '提交中…' : '确认所选地点' }}
          </button>
          <button class="btn btn--ghost" type="button" :disabled="busy" @click="startCorrecting">
            纠正地点
          </button>
          <button class="btn btn--danger" type="button" :disabled="busy" @click="rejectAll">
            拒绝全部
          </button>
        </div>

        <form v-else class="correct" @submit.prevent="submitCorrection">
          <h3 class="correct__title">纠正为正确地点</h3>
          <label class="field">
            <span class="field__label">正确地点名称 <em>*</em></span>
            <input
              v-model="correctName"
              class="field__input"
              type="text"
              maxlength="200"
              :disabled="busy"
            />
          </label>
          <label class="field">
            <span class="field__label">地址（可选）</span>
            <input
              v-model="correctAddress"
              class="field__input"
              type="text"
              maxlength="400"
              :disabled="busy"
            />
          </label>
          <label class="field">
            <span class="field__label">地区（可选）</span>
            <input
              v-model="correctRegion"
              class="field__input"
              type="text"
              maxlength="200"
              :disabled="busy"
            />
          </label>
          <div class="cand__actions">
            <button class="btn btn--primary" type="submit" :disabled="busy">
              {{ busy ? '提交中…' : '提交纠正' }}
            </button>
            <button
              class="btn btn--ghost"
              type="button"
              :disabled="busy"
              @click="correcting = false"
            >
              返回
            </button>
          </div>
          <p class="panel__note">直接填写的地点会以「用户填写」身份保存，不会伪造地点服务结果。</p>
        </form>
      </template>

      <p v-else-if="item.active_run !== null" class="panel__placeholder">
        运行状态：{{ runStatusLabel(item.active_run.status) }}，暂时没有可确认的候选。
      </p>
      <p v-else class="panel__placeholder">这张图片还没有候选地点。</p>
    </template>
  </section>
</template>
