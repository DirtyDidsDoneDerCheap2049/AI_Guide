<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { TripOut } from '../api/types'
import AmapPanel from './AmapPanel.vue'
import ConversationPanel from './ConversationPanel.vue'
import GalleryPanel from './GalleryPanel.vue'
import CurrentMediaPanel from './CurrentMediaPanel.vue'
import GuideCardPanel from './GuideCardPanel.vue'
import DeletedMediaPanel from './DeletedMediaPanel.vue'
import RoutesPanel from './RoutesPanel.vue'
import SavedPlacesPanel from './SavedPlacesPanel.vue'
import PlaceSearchPanel from './PlaceSearchPanel.vue'
import DiscoveryPanel from './DiscoveryPanel.vue'
import ConnectionBadge from './ConnectionBadge.vue'
import { useWorkspace } from '../composables/useWorkspace'
import { useAuth } from '../composables/useAuth'
import { usePlacesRoutes } from '../composables/usePlacesRoutes'
import { useGuidePlaces } from '../composables/useGuidePlaces'
import { buildMapRoute } from '../map/route-overlay'
const workspace = useWorkspace()
const { state, project, mediaList, mediaSnapshots, selectedMedia, remainingSlots } = workspace
const auth = useAuth()
const places = usePlacesRoutes()
const guide = useGuidePlaces()
watch(
  () => project.value?.id,
  () => {
    guide.selectedPlace.value = null
    guide.actionNotice.value = ''
  },
)
const mapRoute = computed(() => buildMapRoute(places.selectedDraft.value))
const photoOpen = computed(() => state.rightTab === 'album' || state.rightTab === 'card')
const titleEdit = ref(false)
const title = ref('')
const tripsOpen = ref(false)
function toggleTrips() {
  tripsOpen.value = !tripsOpen.value
  void auth.loadTrips()
}
function openTrip(trip: TripOut) {
  void auth.openTrip(trip)
  tripsOpen.value = false
}
const allMapPlaces = computed(() => {
  const rows = [
    ...places.state.saved.items,
    ...(['map', 'routes'].includes(state.rightTab)
      ? (places.state.saved.search.result?.candidates ?? [])
      : []),
    ...state.messages
      .filter((m) => !m.deleted_at && !m.stale)
      .flatMap((m) => m.attachments?.places ?? []),
  ]
  if (guide.selectedPlace.value) rows.push(guide.selectedPlace.value as (typeof rows)[number])
  return rows.filter(
    (p, i) =>
      rows.findIndex(
        (other) => (other.provider_place_id ?? other.name) === (p.provider_place_id ?? p.name),
      ) === i,
  )
})
function editTitle() {
  title.value = project.value?.title ?? ''
  titleEdit.value = true
}
async function saveTitle() {
  await workspace.renameTrip(title.value)
  titleEdit.value = false
}
</script>
<template>
  <div class="travel-app">
    <header class="travel-topbar">
      <button class="brand" @click="workspace.openExplore()">
        <span class="brand-symbol">↗</span><span>AI·Guide</span>
      </button>
      <span class="topbar-divider"></span>
      <form v-if="titleEdit" @submit.prevent="saveTitle">
        <input
          v-model="title"
          class="trip-title-input"
          aria-label="旅行名称"
          maxlength="200"
          @keydown.esc="titleEdit = false"
        /><button class="text-button">保存</button>
      </form>
      <button v-else class="trip-title-button" title="修改旅行名称" @click="editTitle">
        {{ project?.title }} <span>⌄</span>
      </button>
      <span class="save-status">{{
        auth.projectScope.value === 'account' ? '已保存到账号' : '保存在此浏览器'
      }}</span>
      <div class="topbar-actions">
        <ConnectionBadge :state="state.connection" :reason="state.connectionReason" /><button
          class="btn btn--ghost"
          @click="toggleTrips"
        >
          我的旅行</button
        ><button class="btn" @click="auth.openPanel()">
          {{ auth.state.user?.display_name || (auth.state.user ? '账号' : '登录 / 保存') }}
        </button>
      </div>
    </header>
    <div v-if="tripsOpen" class="trip-drawer">
      <div class="section-heading">
        <h2>我的旅行</h2>
        <button class="icon-button" aria-label="关闭旅行列表" @click="tripsOpen = false">×</button>
      </div>
      <button class="btn btn--primary" @click="workspace.newTrip()">＋ 开始新旅行</button
      ><button
        v-for="trip in auth.state.trips"
        :key="trip.id"
        class="trip-drawer-item"
        @click="openTrip(trip)"
      >
        {{ trip.title }}
      </button>
    </div>
    <div
      v-if="state.notice"
      class="work-notice"
      :class="{ 'work-notice--error': state.notice.kind === 'error' }"
      role="status"
    >
      <span>{{ state.notice.text }}</span
      ><button
        v-if="state.uploadRetryName"
        class="text-button"
        @click="workspace.retryFailedUpload()"
      >
        重试上传</button
      ><button class="icon-button" aria-label="关闭提示" @click="workspace.dismissNotice()">
        ×
      </button>
    </div>
    <main class="travel-layout">
      <aside class="travel-chat"><ConversationPanel /></aside>
      <section class="travel-board">
        <nav class="board-tabs" aria-label="旅行内容">
          <button
            :class="{ active: state.rightTab === 'research' }"
            @click="workspace.setRightTab('research')"
          >
            旅行资料</button
          ><button
            :class="{ active: state.rightTab === 'map' }"
            @click="workspace.setRightTab('map')"
          >
            探索地图</button
          ><button
            :class="{ active: state.rightTab === 'saved' }"
            @click="workspace.setRightTab('saved')"
          >
            想去的地方 <span>{{ places.state.saved.items.length || '' }}</span></button
          ><button
            :class="{ active: state.rightTab === 'routes' }"
            @click="workspace.setRightTab('routes')"
          >
            路线</button
          ><button :class="{ active: photoOpen }" @click="workspace.setRightTab('album')">
            参考照片 <span>{{ mediaList.length || '' }}</span>
          </button>
        </nav>
        <div v-if="state.rightTab === 'research'" class="research-workspace">
          <DiscoveryPanel
            :city="project?.city_hint || state.destination"
            :query="state.researchQuery"
          />
        </div>
        <div v-show="!photoOpen && state.rightTab !== 'research'" class="map-workspace">
          <div class="map-tools">
            <PlaceSearchPanel v-show="state.rightTab === 'map'" /><SavedPlacesPanel
              v-show="state.rightTab === 'saved'"
            /><RoutesPanel v-show="state.rightTab === 'routes'" />
          </div>
          <div class="map-surface">
            <AmapPanel
              :items="mediaSnapshots"
              :extra-places="allMapPlaces"
              :selected-id="state.selectedMediaId"
              :city-hint="project?.city_hint ?? null"
              :focus="state.mapFocus"
              :active="!photoOpen && state.rightTab !== 'research'"
              :route="mapRoute.overlay"
              :route-note="mapRoute.note"
              @open-media="({ mediaId }) => workspace.openMediaInAlbum(mediaId)"
              @select-place="guide.select"
            />
            <article v-if="guide.selectedPlace.value" class="map-place-detail">
              <button
                class="detail-close icon-button"
                aria-label="关闭地点详情"
                @click="guide.selectedPlace.value = null"
              >
                ×</button
              ><span class="destination-label">{{
                guide.selectedPlace.value.region || '地点详情'
              }}</span>
              <h3>{{ guide.selectedPlace.value.name }}</h3>
              <p>{{ guide.selectedPlace.value.address }}</p>
              <div class="place-actions">
                <button class="btn btn--primary" @click="guide.ask(guide.selectedPlace.value)">
                  问问导游 ↗</button
                ><button class="btn" @click="guide.save(guide.selectedPlace.value)">收藏</button
                ><button
                  class="btn"
                  :disabled="guide.adding.value || guide.selectedPlace.value.latitude === null"
                  @click="guide.add(guide.selectedPlace.value)"
                >
                  加入路线
                </button>
              </div>
              <small
                >来源：{{ guide.selectedPlace.value.fixture ? '测试数据' : '高德地图'
                }}<span v-if="guide.actionNotice.value">
                  · {{ guide.actionNotice.value }}</span
                ></small
              >
            </article>
          </div>
        </div>
        <div v-show="photoOpen" class="album-workspace">
          <GalleryPanel
            :media="mediaList"
            :selected-id="state.selectedMediaId"
            :max-count="mediaList.length + remainingSlots"
            :remaining="remainingSlots"
            :uploading="state.uploading"
            :busy="state.pending !== null"
            @select="workspace.selectMedia"
            @upload="workspace.uploadFiles"
          />
          <div class="photo-detail-grid">
            <CurrentMediaPanel
              :item="selectedMedia"
              :pending="state.pending"
              @cancel="workspace.cancelCurrentRun"
              @retry="() => selectedMedia && workspace.retryMediaRun(selectedMedia.media.id)"
              @show-on-map="workspace.showPlaceOnMap"
              @open-card="workspace.openMediaCard"
            /><GuideCardPanel
              :item="selectedMedia"
              :pending="state.pending"
              @show-on-map="workspace.showPlaceOnMap"
              @regenerate="workspace.analyzeMedia"
            />
          </div>
          <DeletedMediaPanel />
        </div>
      </section>
    </main>
  </div>
</template>
