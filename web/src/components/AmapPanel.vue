<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'

import type { MediaSnapshot, PlaceOut, GuidePlace } from '../api/types'
import type { MapFocusRequest } from '../composables/useWorkspace'
import {
  loadAmap,
  resetAmapLoader,
  type AmapMapInstance,
  type AmapMarkerInstance,
  type AmapPolylineInstance,
} from '../map/amap-loader'
import { DEFAULT_CITY_KEY, MAP_CITIES, findCity, findCityByName } from '../map/cities'
import { parseRouteGeometry, type MapRouteOverlay } from '../map/route-overlay'

const props = defineProps<{
  extraPlaces?: GuidePlace[]
  /** 项目快照里的全部图片快照（每张图最多一个已确认地点）。 */
  items: MediaSnapshot[]
  /** 当前选中的照片（用于联动高亮）。 */
  selectedId: string | null
  /** 工作区标题里带的城市线索，用来选默认的示例范围。 */
  cityHint: string | null
  /** 「在地图上看」请求（地点或坐标）；nonce 变化即重新聚焦。 */
  focus: MapFocusRequest | null
  /** 地图标签是否处于显示状态：隐藏时不初始化（容器尺寸为 0 会画出坏图）。 */
  active: boolean
  /**
   * 要画在地图上的当前路线。**只有 current_revision.is_current=true 时才有值**
   * （构造逻辑见 map/route-overlay.ts）：过期/失败时这里是 null，地图上不留旧几何。
   */
  route: MapRouteOverlay | null
  /** 为什么地图上没有路线；null = 有当前路线或没有选中任何草稿。 */
  routeNote: string | null
}>()

const emit = defineEmits<{
  (event: 'select-place', place: GuidePlace): void
  (event: 'open-media', payload: { mediaId: string; target: 'album' | 'card' }): void
}>()

interface MapPlace {
  place: PlaceOut
  mediaId: string
  position: number
}

const containerRef = ref<HTMLDivElement | null>(null)
const status = ref<'idle' | 'loading' | 'ready' | 'error'>('idle')
const errorMessage = ref('')
const activePlaceId = ref<string | null>(null)
const cityKey = ref<string>(findCityByName(props.cityHint)?.key ?? DEFAULT_CITY_KEY)
/** 最近一次「在地图上居中」的目标说明（收藏地点的聚焦用它）。 */
const focusNote = ref<string | null>(null)

let mapInstance: AmapMapInstance | null = null
let markers: { placeId: string; marker: AmapMarkerInstance }[] = []
let routeMarkers: AmapMarkerInstance[] = []
let routeLine: AmapPolylineInstance | null = null
let initPromise: Promise<void> | null = null

/** 有坐标的已确认地点。 */
const mapPlaces = computed<MapPlace[]>(() => {
  const result: MapPlace[] = []
  for (const item of props.items) {
    const place = item.place
    if (place === null || place.latitude === null || place.longitude === null) {
      continue
    }
    result.push({ place, mediaId: item.media.id, position: item.media.position })
  }
  return result
})

/** 已确认但没有坐标的地点：不能假装能定位，单独说明。 */
const placesWithoutCoords = computed<MediaSnapshot[]>(() =>
  props.items.filter(
    (item) =>
      item.place !== null && (item.place.latitude === null || item.place.longitude === null),
  ),
)

const activePlace = computed<MapPlace | null>(
  () => mapPlaces.value.find((entry) => entry.place.id === activePlaceId.value) ?? null,
)

/** 地点集合的指纹：坐标或集合变化时才重建标记。 */
const placeSignature = computed<string>(() =>
  mapPlaces.value
    .map((entry) => `${entry.place.id}:${entry.place.longitude},${entry.place.latitude}`)
    .join('|'),
)

function currentCenter(): { longitude: number; latitude: number } {
  const first = mapPlaces.value[0]
  if (first !== undefined) {
    return { longitude: first.place.longitude ?? 0, latitude: first.place.latitude ?? 0 }
  }
  const city = findCity(cityKey.value) ?? findCity(DEFAULT_CITY_KEY)
  return { longitude: city?.longitude ?? 120.153576, latitude: city?.latitude ?? 30.287459 }
}

function destroyMarkers(): void {
  for (const entry of markers) {
    entry.marker.setMap(null)
  }
  markers = []
}

/** 已确认地点的标记（与路线停留点分开管理，互不干扰）。 */
function rebuildMarkers(): void {
  const map = mapInstance
  if (map === null) {
    return
  }
  destroyMarkers()
  for (const entry of mapPlaces.value) {
    const latitude = entry.place.latitude
    const longitude = entry.place.longitude
    if (latitude === null || longitude === null) {
      continue
    }
    const marker = new window.AMap!.Marker({
      position: [longitude, latitude],
      title: entry.place.name,
      anchor: 'bottom-center',
      zIndex: 100,
    })
    marker.on('click', () => {
      // 点标记只打开详情，不发起任何请求，也不扣模型额度。
      activePlaceId.value = entry.place.id
    })
    map.add(marker)
    markers.push({ placeId: entry.place.id, marker })
  }
  for (const place of props.extraPlaces ?? []) {
    if (place.latitude === null || place.longitude === null) continue
    if (
      props.route?.stops.some(
        (stop) => stop.latitude === place.latitude && stop.longitude === place.longitude,
      )
    )
      continue
    const marker = new window.AMap!.Marker({
      position: [place.longitude, place.latitude],
      title: place.name,
      anchor: 'bottom-center',
      zIndex: 110,
    })
    marker.on('click', () => emit('select-place', place))
    map.add(marker)
    markers.push({ placeId: place.provider_place_id ?? place.name, marker })
  }
}

/** 路线叠加层：折线与停留点标记都是可销毁的覆盖物，切换路线时必须先清干净。 */
function destroyRouteOverlays(): void {
  routeLine?.setMap(null)
  routeLine = null
  for (const marker of routeMarkers) {
    marker.setMap(null)
  }
  routeMarkers = []
}

/**
 * 重画当前路线。**只画 props.route**（构造它的时候已经保证只有当前修订才会有值），
 * 而且 geometry 为空时只落停留点标记 + 在面板上说明"供应商没有返回折线"，
 * 绝不自己连一条直线充数。
 */
function rebuildRoute(): void {
  const map = mapInstance
  if (map === null) {
    return
  }
  destroyRouteOverlays()
  const overlay = props.route
  if (overlay === null) {
    return
  }
  const path = parseRouteGeometry(overlay.geometry)
  if (path.length >= 2) {
    routeLine = new window.AMap!.Polyline({
      path,
      strokeColor: '#2563eb',
      strokeWeight: 6,
      strokeOpacity: 0.85,
      lineJoin: 'round',
      lineCap: 'round',
      zIndex: 60,
    })
    map.add(routeLine)
  }
  overlay.stops.forEach((stop, index) => {
    const marker = new window.AMap!.Marker({
      position: [stop.longitude, stop.latitude],
      title: stop.name,
      // 序号标签：地图上能看出停留顺序，不需要点开也能读懂。
      label: { content: String(index + 1), direction: 'top' },
      anchor: 'bottom-center',
      zIndex: 120,
    })
    map.add(marker)
    routeMarkers.push(marker)
  })
}

/** 当前地图上真正存在的覆盖物（路线优先：有路线时视野以路线为准）。 */
function contentOverlays(): unknown[] {
  if (routeLine !== null || routeMarkers.length > 0) {
    return routeLine === null ? [...routeMarkers] : [...routeMarkers, routeLine]
  }
  return markers.map((entry) => entry.marker)
}

function fitToPlaces(): void {
  const map = mapInstance
  if (map === null) {
    return
  }
  const overlays = contentOverlays()
  if (overlays.length === 0) {
    const center = currentCenter()
    map.setZoomAndCenter(findCity(cityKey.value) === null ? 12 : 11, [
      center.longitude,
      center.latitude,
    ])
    return
  }
  if (overlays.length === 1) {
    const only = overlays[0]
    if (only !== undefined) {
      map.setFitView([only], false, [80, 80, 80, 80])
      return
    }
  }
  map.setFitView(overlays, false, [60, 60, 60, 60])
}

function centerOnPlace(entry: MapPlace): void {
  activePlaceId.value = entry.place.id
  focusNote.value = null
  const map = mapInstance
  const latitude = entry.place.latitude
  const longitude = entry.place.longitude
  if (map === null || latitude === null || longitude === null) {
    return
  }
  map.setZoomAndCenter(15, [longitude, latitude])
}

async function mountMap(): Promise<void> {
  if (status.value === 'ready' || status.value === 'loading') {
    return
  }
  await nextTick()
  const container = containerRef.value
  if (container === null) {
    return
  }
  status.value = 'loading'
  errorMessage.value = ''
  // 等 DOM 反映「加载中」：容器此刻必须是可见且有高度的，否则高德会按 0×0 初始化。
  await nextTick()

  const result = await loadAmap()
  if (!result.ok) {
    status.value = 'error'
    errorMessage.value = result.message
    return
  }
  const center = currentCenter()
  try {
    mapInstance = new result.AMap.Map(container, {
      zoom: mapPlaces.value.length > 0 ? 12 : 11,
      center: [center.longitude, center.latitude],
      viewMode: '2D',
      resizeEnable: true,
    })
  } catch {
    status.value = 'error'
    errorMessage.value =
      '高德地图初始化失败（容器尺寸或 Key 权限异常）。可以重试，或先用下方地点列表。'
    mapInstance = null
    return
  }
  status.value = 'ready'
  rebuildMarkers()
  rebuildRoute()
  fitToPlaces()
  const focus = props.focus
  if (focus?.longitude != null && focus?.latitude != null)
    mapInstance?.setZoomAndCenter(15, [focus.longitude, focus.latitude])
}

/** 显示地图标签时才初始化；再次显示时重新计算尺寸并恢复视野。 */
function ensureMap(): void {
  if (initPromise !== null) {
    void initPromise
    return
  }
  initPromise = mountMap().finally(() => {
    initPromise = null
  })
  void initPromise
}

watch(
  () => props.active,
  (active) => {
    if (!active) {
      return
    }
    ensureMap()
    // v-show 从 display:none 变回可见后，高德需要重新量尺寸。
    window.setTimeout(() => {
      mapInstance?.resize?.()
    }, 0)
  },
  { immediate: true },
)

watch(placeSignature, () => {
  if (status.value !== 'ready') {
    return
  }
  rebuildMarkers()
  fitToPlaces()
})

watch(
  () => JSON.stringify(props.extraPlaces ?? []),
  () => {
    if (status.value === 'ready') {
      rebuildMarkers()
      fitToPlaces()
    }
  },
)

watch(
  () => props.focus?.nonce ?? 0,
  () => {
    const request = props.focus
    if (request === null) {
      return
    }
    if (request.placeId !== null) {
      const entry = mapPlaces.value.find((item) => item.place.id === request.placeId)
      if (entry === undefined) {
        return
      }
      if (status.value !== 'ready') {
        ensureMap()
      }
      centerOnPlace(entry)
      return
    }
    // 按坐标聚焦（收藏地点的「在地图上居中」）：坐标本身就是 GCJ-02，直接用。
    if (request.longitude === null || request.latitude === null) {
      return
    }
    focusNote.value = request.label
    if (status.value !== 'ready') {
      ensureMap()
    }
    if (mapInstance === null) {
      return
    }
    activePlaceId.value = null
    mapInstance.setZoomAndCenter(15, [request.longitude, request.latitude])
  },
)

/**
 * 路线指纹：折线点串 + 停留点坐标 + 草稿 id。任何一项变化都要重画
 * （切到另一条路线、重新算路拿到新几何、停留点被改过）。
 */
const routeSignature = computed<string>(() => {
  const overlay = props.route
  if (overlay === null) {
    return ''
  }
  const stops = overlay.stops.map((stop) => `${stop.longitude},${stop.latitude}`).join('|')
  return `${overlay.draftId}#${overlay.inputVersion}#${overlay.geometry}#${stops}`
})

watch(routeSignature, () => {
  if (status.value !== 'ready') {
    return
  }
  rebuildMarkers()
  rebuildRoute()
  fitToPlaces()
})

watch(
  () => props.selectedId,
  (mediaId) => {
    if (mediaId === null) {
      return
    }
    const entry = mapPlaces.value.find((item) => item.mediaId === mediaId)
    if (entry !== undefined) {
      activePlaceId.value = entry.place.id
    }
  },
)

function retryLoad(): void {
  resetAmapLoader()
  mapInstance = null
  markers = []
  destroyRouteOverlays()
  status.value = 'idle'
  errorMessage.value = ''
  ensureMap()
}

function selectCity(key: string): void {
  cityKey.value = key
  const city = findCity(key)
  if (city === null || mapInstance === null) {
    return
  }
  mapInstance.setZoomAndCenter(11, [city.longitude, city.latitude])
}

onBeforeUnmount(() => {
  destroyMarkers()
  destroyRouteOverlays()
  mapInstance?.destroy()
  mapInstance = null
})
</script>

<template>
  <section class="panel amap">
    <div v-if="status === 'loading'" class="map-status">地图加载中…</div>
    <div v-if="status === 'error'" class="map-status map-status--error">
      <p>地图暂时没有加载出来。你仍然可以搜索地点、收藏和编辑路线。</p>
      <button class="btn btn--ghost btn--sm" @click="retryLoad">重新加载</button>
      <details>
        <summary>查看原因</summary>
        <p>{{ errorMessage }}</p>
      </details>
    </div>
    <div
      ref="containerRef"
      class="amap__canvas"
      :class="{ 'amap__canvas--collapsed': status === 'idle' || status === 'error' }"
    ></div>
    <div v-if="!mapPlaces.length && !extraPlaces?.length && !route" class="map-city-picker">
      <label
        >浏览城市
        <select :value="cityKey" @change="selectCity(($event.target as HTMLSelectElement).value)">
          <option v-for="city in MAP_CITIES" :key="city.key" :value="city.key">
            {{ city.name }}
          </option>
        </select></label
      >
    </div>
    <div v-if="route && status === 'ready'" class="map-route-summary">
      <span>{{ route.name }} · {{ route.distanceText }} · {{ route.durationText }}</span>
      <span v-if="route.fixture">测试数据</span>
      <small v-if="parseRouteGeometry(route.geometry).length < 2"
        >暂时只有停留点，未获取到路线图。</small
      >
    </div>
    <div v-if="activePlace" class="amap__detail">
      <div class="amap__detail-head">
        <h3>{{ activePlace.place.name }}</h3>
        <button class="icon-button" aria-label="关闭地点详情" @click="activePlaceId = null">
          ×
        </button>
      </div>
      <p>{{ activePlace.place.address || activePlace.place.region }}</p>
      <button
        class="text-button"
        @click="emit('open-media', { mediaId: activePlace.mediaId, target: 'album' })"
      >
        查看这张照片 →
      </button>
    </div>
    <div v-if="status === 'error' && mapPlaces.length" class="placelist">
      <button
        v-for="entry in mapPlaces"
        :key="entry.place.id"
        class="placelist__item"
        @click="activePlaceId = entry.place.id"
      >
        {{ entry.place.name }}
      </button>
    </div>
    <span v-if="placesWithoutCoords.length" class="map-missing"
      >有 {{ placesWithoutCoords.length }} 个照片地点尚未确定位置</span
    >
  </section>
</template>
