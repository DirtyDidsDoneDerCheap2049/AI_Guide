<script setup lang="ts">
/**
 * 导游卡片区。
 *
 * 必须分区展示：
 * - 「地点服务事实」= 地点 Provider 返回的字段（place.provider / address / region / latitude /
 *   longitude / query_at），属于供应商数据，不能和模型生成内容混在一起；
 * - 「模型讲解」= 文本模型生成的标题、摘要、sections、tips。
 * 当 card.partial === true 时，明确提示地点服务不可用、卡片不含供应商事实。
 *
 * D4：用户改过地点之后，旧卡片会被后端标记 `stale`（保留内容但已过期）。
 * 这里必须显眼地说清「结果已过期（基于旧地点）」+ 后端给的原因，并提供
 * 「按新地点重新生成」（POST /api/v1/media/{id}/analyze，从地点查询开始重新跑一遍，
 * 它不会改动用户刚设定的地点）。过期卡片里的供应商事实同样是**旧地点**的事实。
 */
import { computed } from 'vue'

import FavoritePlaceButton from './FavoritePlaceButton.vue'
import type { MediaSnapshot } from '../api/types'
import { formatDateTime } from '../utils/format'
import { runStatusLabel } from '../utils/labels'

const props = defineProps<{
  item: MediaSnapshot | null
  pending: string | null
}>()

const emit = defineEmits<{
  (event: 'show-on-map', placeId: string): void
  (event: 'regenerate', mediaId: string): void
}>()

interface FactRow {
  label: string
  value: string
}

const card = computed(() => props.item?.card ?? null)
const place = computed(() => props.item?.place ?? null)
/** true = 这张讲解基于旧地点（用户改过地点），内容过期，不能当现状。 */
const cardStale = computed(() => card.value?.stale === true)
const staleReason = computed(
  () => card.value?.stale_reason ?? '地点改过之后，这张讲解对应的还是改动之前的地点。',
)
const busy = computed(() => props.pending !== null)

function regenerate(): void {
  const item = props.item
  if (item !== null) {
    emit('regenerate', item.media.id)
  }
}

/** 只有快照里带经纬度的已确认地点才能在地图上定位；没有坐标时不显示按钮。 */
const canShowOnMap = computed(() => {
  const current = place.value
  return current !== null && current.latitude !== null && current.longitude !== null
})

function showOnMap(): void {
  const current = place.value
  if (current !== null && current.latitude !== null && current.longitude !== null) {
    emit('show-on-map', current.id)
  }
}

const factRows = computed<FactRow[]>(() => {
  const facts = card.value?.place_facts ?? null
  if (facts === null) {
    return []
  }
  return [
    { label: '来源', value: facts.provider ?? '—' },
    { label: '地址', value: facts.address ?? '—' },
    { label: '地区', value: facts.region ?? '—' },
    { label: '查询于', value: formatDateTime(facts.query_at) },
  ]
})

/** 没有供应商事实时给出原因，而不是留一片空白。 */
const emptyFactsReason = computed<string>(() => {
  const currentPlace = place.value
  if (currentPlace !== null && currentPlace.provider === 'user') {
    return '地点由你确认，但本次没有取得可核对的地点资料。讲解已保留，地址等信息仍需核实。'
  }
  return '本次卡片没有附带地点服务事实。'
})

const stageHint = computed<string>(() => {
  const item = props.item
  if (item === null) {
    return '先选择一张图片。'
  }
  const run = item.active_run ?? item.runs[0] ?? null
  if (run === null) {
    return '这张图片还没有运行记录。'
  }
  switch (run.status) {
    case 'QUEUED':
      return '分析任务已排队，请稍等。'
    case 'RUNNING':
      return '正在整理讲解，完成后会显示在这里。'
    case 'WAITING_USER':
      return 'Agent 正在等待你确认地点，确认后才会查询地点并生成卡片。'
    case 'CANCELLED':
      // 取消后不可重试（后端只允许 FAILED / PARTIAL 重试），所以这里不能说「点重试」。
      return '这次运行已取消，没有生成导游卡片。可以点击照片旁的“重新分析这张”。'
    case 'FAILED':
      return '这次运行失败了，可以在照片旁重试。'
    default:
      return `当前运行状态：${runStatusLabel(run.status)}。`
  }
})
</script>

<template>
  <section class="panel guide">
    <header class="panel__head">
      <h2 class="panel__title">导游卡片</h2>
      <span v-if="cardStale" class="badge badge--waiting">结果已过期</span>
      <span v-if="card !== null && card.partial" class="badge badge--waiting">部分完成</span>
    </header>

    <p v-if="card === null" class="panel__placeholder">{{ stageHint }}</p>

    <template v-else>
      <!-- 过期：用户改过地点，这张卡片基于旧地点。必须显眼说明，不能让旧内容当现状 -->
      <p v-if="cardStale" class="alert alert--warn" role="status">
        <strong>结果已过期（基于旧地点）</strong>：{{ staleReason }}
        <template v-if="place !== null"> 当前地点是「{{ place.name }}」。 </template>
        下面的讲解与地点事实都是<strong>改动之前</strong>那个地点的内容，不能当作现状使用。
      </p>
      <div v-if="cardStale" class="guide__actions">
        <button class="btn btn--primary btn--sm" type="button" :disabled="busy" @click="regenerate">
          {{ pending === 'analyze-media' ? '提交中…' : '按新地点重新生成' }}
        </button>
      </div>

      <!-- 部分完成：卡片不含供应商事实，必须显式说明，不能让用户误以为地点服务已校验 -->
      <p v-if="card.partial" class="alert alert--warn">
        暂未查到可核对的地点资料。以下讲解仅供参考。
      </p>

      <div class="guide__header">
        <h3 class="guide__title">{{ card.title }}</h3>
        <p class="guide__summary">{{ card.summary }}</p>
        <p class="guide__meta">
          模型：{{ card.model_name }} · 生成时间：{{ formatDateTime(card.created_at) }}
        </p>
      </div>

      <!-- 分区一：地点资料（地图来源，不是模型生成内容） -->
      <section class="guide__section">
        <h4 class="guide__section-title">
          地点服务事实（供应商数据{{ cardStale ? ' · 旧地点' : '' }}）
        </h4>
        <table v-if="factRows.length > 0" class="facts">
          <tbody>
            <tr v-for="row in factRows" :key="row.label">
              <th class="facts__label" scope="row">{{ row.label }}</th>
              <td class="facts__value">{{ row.value }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="panel__note">{{ emptyFactsReason }}</p>
        <p v-if="place !== null" class="panel__note">
          已确认地点：{{ place.name }}（{{
            place.confirmed_by === 'user_input' ? '用户填写' : '来自候选'
          }}）
        </p>
        <div v-if="canShowOnMap" class="guide__actions">
          <button class="btn btn--sm" type="button" @click="showOnMap">在地图上看</button>
        </div>
        <!-- 与照片详情里的收藏入口是同一个动作：未核实的地点不能收藏为事实。 -->
        <FavoritePlaceButton :item="item" />
      </section>

      <!-- 分区二：模型讲解 -->
      <section class="guide__section">
        <h4 class="guide__section-title">AI 讲解 · 仅供参考</h4>
        <article v-for="section in card.sections" :key="section.heading" class="guide__block">
          <h5 class="guide__block-title">{{ section.heading }}</h5>
          <p class="guide__block-body">{{ section.body }}</p>
        </article>
        <div v-if="card.tips.length > 0" class="guide__tips">
          <h5 class="guide__block-title">小贴士</h5>
          <ul class="guide__tips-list">
            <li v-for="tip in card.tips" :key="tip">{{ tip }}</li>
          </ul>
        </div>
      </section>
    </template>
  </section>
</template>
