<script setup lang="ts">
import { ref, watch } from 'vue'
import { useWorkspace } from '../composables/useWorkspace'
import { usePlacesRoutes } from '../composables/usePlacesRoutes'
import { useGuidePlaces } from '../composables/useGuidePlaces'
import type { GuidePlace } from '../api/types'
const props = defineProps<{ routeMode?: boolean }>()
const workspace = useWorkspace()
const places = usePlacesRoutes()
const guide = useGuidePlaces()
const query = ref('')
const city = ref(workspace.project.value?.city_hint ?? '')
watch(
  () => workspace.project.value?.city_hint,
  (value) => {
    city.value = value ?? ''
  },
)
async function search() {
  await places.searchPlaces(query.value, city.value || null)
}
function alreadyAdded(p: GuidePlace) {
  return (
    places.selectedDraft.value?.stops.some(
      (s) => s.latitude === p.latitude && s.longitude === p.longitude,
    ) ?? false
  )
}
</script>
<template>
  <section class="place-search">
    <form class="map-search-form" @submit.prevent="search">
      <span aria-hidden="true">⌕</span
      ><input
        v-model="query"
        aria-label="搜索地点"
        placeholder="搜索景点、街区、住宿…"
        maxlength="120"
      /><button type="submit" :disabled="places.state.saved.search.phase === 'loading'">
        {{ places.state.saved.search.phase === 'loading' ? '查找中' : '搜索' }}
      </button>
    </form>
    <div class="search-city">
      <span>城市</span
      ><input v-model="city" aria-label="搜索城市" placeholder="不限，可填写城市" maxlength="60" />
    </div>
    <p v-if="places.state.saved.search.errorText" class="field__error">
      {{ places.state.saved.search.errorText }}
    </p>
    <div v-if="places.state.saved.search.result" class="search-results">
      <div class="section-heading">
        <h3>找到的地点</h3>
        <button class="icon-button" aria-label="关闭搜索结果" @click="places.clearSearch()">
          ×
        </button>
      </div>
      <p v-if="places.state.saved.search.result.fixture" class="gentle-note">以下为测试数据</p>
      <p v-if="!places.state.saved.search.result.candidates.length" class="gentle-note">
        没找到这个地点。试试补充城市，或换一个名称。
      </p>
      <article
        v-for="p in places.state.saved.search.result.candidates"
        :key="p.provider_place_id ?? p.name"
        class="place-card"
      >
        <button class="place-card-main" @click="guide.select(p)">
          <span class="place-pin">↗</span
          ><span
            ><strong>{{ p.name }}</strong
            ><small>{{ p.address || p.region }}</small></span
          >
        </button>
        <div class="place-actions">
          <template v-if="!props.routeMode"
            ><button class="text-button" @click="guide.ask(p)">问导游</button
            ><button
              class="text-button"
              :disabled="!!places.state.saved.pending"
              @click="guide.save(p)"
            >
              收藏
            </button></template
          ><button
            class="text-button"
            :disabled="
              guide.adding.value ||
              !!places.state.routes.pending ||
              p.latitude === null ||
              p.longitude === null ||
              (props.routeMode && alreadyAdded(p))
            "
            @click="guide.add(p)"
          >
            {{ props.routeMode ? (alreadyAdded(p) ? '已加入' : '＋ 加入路线') : '＋ 路线' }}
          </button>
        </div>
      </article>
    </div>
    <div v-else-if="!props.routeMode" class="map-intro">
      <p>找一处想去的地方</p>
      <span>从搜索开始，或点开导游推荐的地点。</span>
    </div>
    <p v-if="guide.actionNotice.value" class="action-toast" role="status">
      {{ guide.actionNotice.value }}
    </p>
  </section>
</template>
