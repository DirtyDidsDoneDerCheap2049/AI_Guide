/**
 * 路线在地图上的叠加层（折线 + 停留点标记）。
 *
 * 三条规则（契约 2.4 / 4）：
 *  1. **只有当前修订能被画出来**：`current_revision.is_current === true` 才画折线；
 *     过期或失败时**不画旧几何**——地图上留一条旧线比不画更糟。
 *  2. **几何原样使用**：`geometry` 是高德返回的 `"lng,lat;lng,lat;…"`（GCJ-02），
 *     与 JS API 同一坐标系，只做拆分，不做任何投影换算或插值。
 *  3. **没有几何就说没有**：供应商没返回折线时只落停留点标记，并在界面上说明，
 *     不用两点连线之类的办法凑一条线出来。
 */

import type { RouteDraftOut } from '../api/types'
import { formatDistanceKm, formatDurationText } from '../utils/format'
import { isFixtureProvider } from '../utils/labels'

export interface MapRouteStop {
  name: string
  /** GCJ-02 纬度 */
  latitude: number
  /** GCJ-02 经度 */
  longitude: number
}

export interface MapRouteOverlay {
  draftId: string
  name: string
  mode: string
  /** 供应商返回的折线点串（`"lng,lat;lng,lat;…"`，GCJ-02）。空串 = 供应商没给折线。 */
  geometry: string
  provider: string
  /** true = 供应商是 fixture，界面必须标注"测试数据（不是真实地图结果）"。 */
  fixture: boolean
  distanceText: string | null
  durationText: string | null
  inputVersion: number
  stops: MapRouteStop[]
}

/** 解析 `"lng,lat;lng,lat;…"`；坏点直接丢弃（不猜、不补）。 */
export function parseRouteGeometry(geometry: string | null | undefined): [number, number][] {
  if (typeof geometry !== 'string' || geometry.trim() === '') {
    return []
  }
  const points: [number, number][] = []
  for (const chunk of geometry.split(';')) {
    const parts = chunk.split(',')
    if (parts.length < 2) {
      continue
    }
    const longitude = Number(parts[0])
    const latitude = Number(parts[1])
    if (!Number.isFinite(longitude) || !Number.isFinite(latitude)) {
      continue
    }
    points.push([longitude, latitude])
  }
  return points
}

export interface MapRouteBuild {
  /** 可以画在地图上的当前路线；null = 没有可画的东西。 */
  overlay: MapRouteOverlay | null
  /** 为什么没有画（null = 有当前路线，不需要解释）。 */
  note: string | null
}

/**
 * 从草稿构造地图叠加层。
 *
 * ⚠️ 只有 `current_revision.is_current === true` 才有 overlay；其余情况一律
 * `overlay = null` + 一句真实原因（尚未算路 / 算路失败 / 停留点已改动）。
 */
export function buildMapRoute(draft: RouteDraftOut | null): MapRouteBuild {
  if (draft === null) {
    return { overlay: null, note: null }
  }
  const revision = draft.current_revision
  if (revision === null || !revision.is_current) {
    const reason = draft.route_unavailable_reason
    if (reason === 'not_computed') {
      return { overlay: null, note: `「${draft.name}」还没有算路，地图上不显示路线。` }
    }
    if (reason === 'stale_after_edit') {
      return {
        overlay: null,
        note: `「${draft.name}」的停留点已改动，旧结果已过期，地图上不显示这条旧路线。`,
      }
    }
    if (typeof reason === 'string' && reason.startsWith('compute_failed:')) {
      const code = reason.slice('compute_failed:'.length)
      return { overlay: null, note: `「${draft.name}」算路失败（${code}），地图上不显示路线。` }
    }
    return { overlay: null, note: `「${draft.name}」当前没有可用路线，地图上不显示路线。` }
  }

  const stops: MapRouteStop[] = []
  for (const stop of draft.stops) {
    if (
      stop.latitude === null ||
      stop.latitude === undefined ||
      stop.longitude === null ||
      stop.longitude === undefined
    ) {
      continue
    }
    stops.push({ name: stop.name, latitude: stop.latitude, longitude: stop.longitude })
  }

  return {
    overlay: {
      draftId: draft.id,
      name: draft.name,
      mode: draft.mode,
      geometry: revision.geometry ?? '',
      provider: revision.provider,
      fixture: isFixtureProvider(revision.provider),
      distanceText: formatDistanceKm(revision.distance_meters),
      durationText: formatDurationText(revision.duration_seconds),
      inputVersion: revision.input_version,
      stops,
    },
    note: null,
  }
}
