import { ref } from 'vue'
import type { GuidePlace } from '../api/types'
import { useWorkspace } from './useWorkspace'
import { usePlacesRoutes } from './usePlacesRoutes'

const selectedPlace = ref<GuidePlace | null>(null)
const actionNotice = ref('')
const adding = ref(false)

export function useGuidePlaces() {
  const workspace = useWorkspace()
  const places = usePlacesRoutes()
  function select(place: GuidePlace) {
    selectedPlace.value = place
    actionNotice.value = ''
    if (place.latitude !== null && place.longitude !== null) {
      workspace.showCoordinatesOnMap(
        { longitude: place.longitude, latitude: place.latitude, label: place.name },
        workspace.state.rightTab === 'routes',
      )
    }
  }
  async function save(place: GuidePlace) {
    const ok = await places.saveSearchCandidate({
      ...place,
      providerPlaceId: place.provider_place_id,
    })
    actionNotice.value = ok ? '已收藏' : (places.state.saved.errorText ?? '收藏失败，请重试')
  }
  async function add(place: GuidePlace) {
    if (adding.value) return
    adding.value = true
    try {
      const ok = await places.addPlaceToRoute(place)
      actionNotice.value = ok ? '已加入路线' : (places.state.routes.errorText ?? '请先确定地点位置')
      if (ok) workspace.setRightTab('routes')
    } finally {
      adding.value = false
    }
  }
  function ask(place: GuidePlace) {
    workspace.state.composerScope = 'workspace'
    workspace.state.leftOpen = true
    void workspace.askQuestion(`讲讲${place.region ?? ''}的${place.name}，有什么值得看的？`)
  }
  return { selectedPlace, actionNotice, adding, select, save, add, ask }
}
