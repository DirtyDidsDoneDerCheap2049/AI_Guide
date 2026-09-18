<script setup lang="ts">
import { ref, watch } from 'vue'
import { discoverCity } from '../api/client'
import type { DiscoveryResult, GuidePlace } from '../api/types'
import { useWorkspace } from '../composables/useWorkspace'
import { useGuidePlaces } from '../composables/useGuidePlaces'
import TravelResources from './TravelResources.vue'
const props = defineProps<{ city: string; home?: boolean; query?: string }>()
const workspace = useWorkspace()
const guide = useGuidePlaces()
const query = ref(props.query ?? '')
const result = ref<DiscoveryResult | null>(null)
const busy = ref(false)
const error = ref('')
const broken = ref<Record<string, boolean>>({})
let generation = 0
async function search() {
  const turn = ++generation
  busy.value = true
  error.value = ''
  result.value = null
  try {
    const data = await discoverCity(props.city, query.value.trim())
    if (turn === generation) result.value = data
  } catch {
    if (turn === generation) error.value = '资料暂时没有加载成功，请稍后重试。'
  } finally {
    if (turn === generation) busy.value = false
  }
}
watch(
  () => [props.city, props.query],
  () => {
    query.value = props.query ?? ''
    void search()
  },
  { immediate: true },
)
async function openPlace(place: GuidePlace) {
  if (props.home) await workspace.startPlanning('', props.city)
  if (!workspace.project.value) return
  guide.select(place)
}
async function ask(place: GuidePlace) {
  const question = `想去${props.city}的${place.name}，帮我比较它和附近值得去的地方，再安排路线。`
  if (props.home) await workspace.startPlanning(question, props.city)
  else await workspace.askQuestion(question)
}
</script>
<template>
  <section class="discovery-panel" :class="{ 'discovery-panel--home': home }">
    <div class="section-heading">
      <div>
        <span class="destination-label">{{ city }} · 出发前看看</span>
        <h2>{{ home ? '找找想去的地方' : '旅行资料' }}</h2>
      </div>
      <span v-if="result" class="research-time"
        >{{
          new Date(result.fetched_at).toLocaleTimeString('zh-CN', {
            hour: '2-digit',
            minute: '2-digit',
          })
        }}
        获取</span
      >
    </div>
    <form class="research-search" @submit.prevent="search">
      <input
        v-model="query"
        aria-label="搜索旅行资料"
        :placeholder="`搜索${city}的景点、街区…`"
        maxlength="100"
      /><button class="btn btn--primary" :disabled="busy">{{ busy ? '查找中…' : '查找' }}</button>
    </form>
    <div v-if="busy" class="research-loading" role="status">正在查找地点和图片…</div>
    <div v-if="error || result?.error" class="research-error" role="status">
      <span>{{ error || result?.error }}</span
      ><button class="text-button" @click="search">重试</button>
    </div>
    <template v-if="result">
      <p v-if="result.fixture" class="gentle-note">测试环境：以下地点为固定样例</p>
      <div class="discovery-grid">
        <article
          v-for="place in result.places"
          :key="place.provider_place_id ?? place.name"
          class="discovery-card"
        >
          <button
            class="discovery-image"
            :aria-label="`在地图查看${place.name}`"
            @click="openPlace(place)"
          >
            <img
              v-if="place.photos?.[0] && !broken[place.photos[0].url]"
              :src="place.photos[0].url"
              :alt="place.name"
              loading="lazy"
              referrerpolicy="no-referrer"
              @error="broken[place.photos![0]!.url] = true"
            />
            <span v-else class="photo-unavailable"
              >{{ place.name.slice(0, 6) }}<small>暂无可用图片 · 查看地图 ↗</small></span
            >
          </button>
          <div class="discovery-copy">
            <h3>
              <button @click="openPlace(place)">{{ place.name }}</button>
            </h3>
            <p>{{ place.address || place.region }}</p>
            <div class="place-actions">
              <button class="text-button" @click="ask(place)">帮我规划</button
              ><button v-if="!home" class="text-button" @click="guide.save(place)">收藏</button
              ><button
                v-if="!home"
                class="text-button"
                :disabled="guide.adding.value || place.latitude === null"
                @click="guide.add(place)"
              >
                ＋ 路线</button
              ><button v-else class="text-button" @click="openPlace(place)">看地图</button>
            </div>
          </div>
        </article>
      </div>
      <p v-if="!result.places.length && !result.error" class="gentle-note">
        没找到相关地点，换个名称试试。也可以查看下方搜索入口。
      </p>
      <p v-if="result.places.length" class="research-source">地点与图片：高德地图</p>
      <div class="section-heading resource-heading">
        <h3>攻略与 B站视频</h3>
        <span>在新标签页打开</span>
      </div>
      <TravelResources :resources="result.resources" />
      <p class="research-source">
        攻略和视频供规划参考，价格、预约及开放时间请以目的地最新公告为准。
      </p>
    </template>
    <p v-if="!home && guide.actionNotice.value" class="action-toast" role="status">
      {{ guide.actionNotice.value }}
    </p>
  </section>
</template>
