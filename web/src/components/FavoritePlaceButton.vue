<script setup lang="ts">
/**
 * 照片上的「收藏这个地点」。
 *
 * 规则（契约 1 / 2.1）：
 *  - 只有**已核实**的地点（match_status ∈ matched / matched_name_only / user_selected）
 *    才能收藏为事实；其余状态按钮禁用并写明真实原因（未核实的地点后端也会拒绝）。
 *  - 没有确认地点时不渲染这个按钮（那一栏本来就在说明"还没有确认地点"）。
 *  - 收藏成功后按钮变成「已收藏」；再点就是取消收藏（真实 DELETE，失败会显示真实错误）。
 *  - 幂等由后端保证：重复收藏返回原记录，列表里不会出现两行。
 */
import { computed, onMounted } from 'vue'

import type { MediaSnapshot } from '../api/types'
import { usePlacesRoutes } from '../composables/usePlacesRoutes'
import { isPlaceVerified, placeMatchStatusLabel } from '../utils/labels'

const props = defineProps<{
  item: MediaSnapshot | null
}>()

const places = usePlacesRoutes()
const { state } = places

const place = computed(() => props.item?.place ?? null)
/** 已核实 = 可以收藏为事实；未知状态一律按未核实处理。 */
const verified = computed(() => place.value !== null && isPlaceVerified(place.value.match_status))
/** 同一个地点（按 place_id 匹配）是否已经在收藏里。 */
const saved = computed(() => places.savedPlaceForPhotoPlace(place.value?.id ?? null))
const busy = computed(() => state.saved.pending !== null)

const unverifiedReason = computed<string>(() => {
  const current = place.value
  if (current === null) {
    return ''
  }
  return `地点还没核实（${placeMatchStatusLabel(current.match_status)}），不能作为事实收藏。`
})

onMounted(() => {
  // 只有真的要显示这个按钮时才去读收藏列表：界面上的「已收藏」不能靠猜。
  if (verified.value) {
    places.ensureSavedPlaces()
  }
})

async function save(): Promise<void> {
  const current = place.value
  const media = props.item?.media
  if (current === null || media === undefined || media === null) {
    return
  }
  await places.savePlaceFromPhoto(media.id, current.name)
}

async function unsave(): Promise<void> {
  const row = saved.value
  if (row === null) {
    return
  }
  await places.removeSavedPlace(row.id)
}
</script>

<template>
  <div v-if="place !== null" class="favorite">
    <template v-if="verified">
      <button
        v-if="saved === null"
        class="btn btn--sm"
        type="button"
        :disabled="busy"
        @click="save"
      >
        {{ state.saved.pending === 'from-photo' ? '收藏中…' : '收藏这个地点' }}
      </button>
      <template v-else>
        <span class="favorite__state">
          <span class="tag tag--ok">已收藏</span>
          {{ saved.name }}（{{ saved.source === 'photo' ? '来自照片' : '手工填写' }}）
        </span>
        <button class="btn btn--ghost btn--sm" type="button" :disabled="busy" @click="unsave">
          {{ state.saved.pending === `delete:${saved.id}` ? '取消中…' : '取消收藏' }}
        </button>
      </template>
    </template>
    <template v-else>
      <button class="btn btn--sm" type="button" disabled title="地点未核实，不能收藏为事实">
        收藏这个地点
      </button>
      <span class="favorite__reason">{{ unverifiedReason }}</span>
    </template>

    <p v-if="state.saved.notice !== null" class="favorite__notice">{{ state.saved.notice }}</p>
    <p v-if="state.saved.errorText !== null" class="favorite__error">
      {{ state.saved.errorText }}

      <button class="btn btn--ghost btn--sm" type="button" @click="places.clearSavedError()">
        知道了
      </button>
    </p>
  </div>
</template>
