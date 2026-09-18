/**
 * 收藏地点（SavedPlace）与路线草稿（RouteDraft）的状态与动作。
 *
 * 契约：docs/productization/D3b-收藏与路线接口契约.md。三条不可让步的规则：
 *
 *  1. **收藏是幂等的**：同一地点重复收藏，后端返回原记录（HTTP 201 + 同一个 id）。
 *     所以这里一律按 id 合并，绝不往列表里追加第二行；界面不会出现重复收藏。
 *  2. **stops 是用户资产**：算路失败、结果过期都不会动它。这里也从不为了"看起来干净"
 *     去清空或改写停留点；只有用户显式增删/排序才 PATCH。
 *  3. **数字只能来自供应商**：距离/时长/几何一律取自 `current_revision`（且必须
 *     `is_current=true`）。没有当前修订时界面显示 `route_unavailable_reason` 对应的真实
 *     状态（尚未算路 / 算路失败 + 错误码 / 停留点已改动，请重新算路），不显示任何数字。
 *
 * 状态绑定到工作区：切换/归档工作区时必须清空，否则会把 A 的收藏显示在 B 里。
 * 归属由 useWorkspace 在快照落地时通过 bindWorkspace() 指定（单向依赖，无循环引用）。
 */

import { computed, reactive } from 'vue'

import {
  ApiError,
  computeRoute as computeRouteRequest,
  createRoute as createRouteRequest,
  createSavedPlace as createSavedPlaceRequest,
  deleteRoute as deleteRouteRequest,
  deleteSavedPlace as deleteSavedPlaceRequest,
  listRouteRevisions,
  listRoutes as listRoutesRequest,
  listSavedPlaces as listSavedPlacesRequest,
  searchPlaces as searchPlacesRequest,
  updateRoute as updateRouteRequest,
} from '../api/client'
import type {
  PlaceSearchOut,
  RouteDraftOut,
  RouteRevisionOut,
  RouteStop,
  SavedPlaceOut,
} from '../api/types'
import { describeApiError, routeUnavailableErrorCode, routeUnavailableText } from '../utils/labels'

export type LoadPhase = 'idle' | 'loading' | 'ready' | 'error'

export interface SavedPlacesState {
  phase: LoadPhase
  items: SavedPlaceOut[]
  errorText: string | null
  errorCode: string | null
  /** 最近一次成功动作的一句话（真的发生过的结果，不是预告）。 */
  notice: string | null
  /** 正在进行的收藏动作：'create' | 'from-photo' | `delete:${id}`。 */
  pending: string | null
  search: {
    phase: LoadPhase
    query: string
    result: PlaceSearchOut | null
    errorText: string | null
    errorCode: string | null
  }
}

export interface RevisionsState {
  phase: LoadPhase
  items: RouteRevisionOut[]
  errorText: string | null
  errorCode: string | null
}

export interface RoutesState {
  phase: LoadPhase
  drafts: RouteDraftOut[]
  errorText: string | null
  errorCode: string | null
  /** 正在进行的动作：'create' | `patch:${id}` | `compute:${id}` | `delete:${id}`。 */
  pending: string | null
  /** 正在算路的草稿 id（同步调用供应商，可能持续数秒）。 */
  computingId: string | null
  /** 选中的草稿（编辑器与地图联动都以它为准）。 */
  selectedId: string | null
  /** 修订历史（按草稿 id 懒加载）。 */
  revisions: Record<string, RevisionsState>
}

const state = reactive({
  /** 当前绑定的工作区 id；null = 还没有工作区（界面显示空态而不是别人的数据）。 */
  projectId: null as string | null,
  saved: {
    phase: 'idle',
    items: [],
    errorText: null,
    errorCode: null,
    notice: null,
    pending: null,
    search: {
      phase: 'idle',
      query: '',
      result: null,
      errorText: null,
      errorCode: null,
    },
  } as SavedPlacesState,
  routes: {
    phase: 'idle',
    drafts: [],
    errorText: null,
    errorCode: null,
    pending: null,
    computingId: null,
    selectedId: null,
    revisions: {},
  } as RoutesState,
})

function codeOf(error: unknown): string | null {
  return error instanceof ApiError ? error.code : null
}

function resetSaved(): void {
  state.saved.phase = 'idle'
  state.saved.items = []
  state.saved.errorText = null
  state.saved.errorCode = null
  state.saved.notice = null
  state.saved.pending = null
  state.saved.search.phase = 'idle'
  state.saved.search.query = ''
  state.saved.search.result = null
  state.saved.search.errorText = null
  state.saved.search.errorCode = null
}

function resetRoutes(): void {
  state.routes.phase = 'idle'
  state.routes.drafts = []
  state.routes.errorText = null
  state.routes.errorCode = null
  state.routes.pending = null
  state.routes.computingId = null
  state.routes.selectedId = null
  state.routes.revisions = {}
}

/** 绑定工作区：id 变化（新建/切换/归档后重建）时清空数据。 */
function bindWorkspace(projectId: string | null): void {
  if (state.projectId === projectId) {
    return
  }
  state.projectId = projectId
  resetSaved()
  resetRoutes()
}

// ------------------------------------------------------------------ 收藏列表

function byPosition(left: SavedPlaceOut, right: SavedPlaceOut): number {
  return left.position - right.position || left.created_at.localeCompare(right.created_at)
}

/**
 * 把一条服务器返回的收藏合并进列表（按 id 覆盖，绝不允许同一个 id 出现两行）。
 * 幂等收藏的返回值就是"本来就存在的那条"，所以这里天然不会产生重复行。
 */
function mergeSavedPlace(row: SavedPlaceOut): void {
  const index = state.saved.items.findIndex((item) => item.id === row.id)
  if (index >= 0) {
    state.saved.items[index] = row
  } else {
    state.saved.items.push(row)
  }
  state.saved.items.sort(byPosition)
}

async function loadSavedPlaces(): Promise<void> {
  const projectId = state.projectId
  if (projectId === null) {
    return
  }
  state.saved.phase = state.saved.items.length === 0 ? 'loading' : state.saved.phase
  try {
    const result = await listSavedPlacesRequest(projectId)
    if (state.projectId !== projectId) return
    state.saved.items = [...result.saved_places].sort(byPosition)
    state.saved.phase = 'ready'
    state.saved.errorText = null
    state.saved.errorCode = null
  } catch (error) {
    state.saved.phase = 'error'
    state.saved.errorText = describeApiError(error)
    state.saved.errorCode = codeOf(error)
  }
}

/** 只在还没读过的时候拉一次（进入收藏标签 / 需要显示"已收藏"时调用）。 */
function ensureSavedPlaces(): void {
  if (state.saved.phase === 'idle') {
    void loadSavedPlaces()
  }
}

/** 从照片上已核实的地点收藏（POST，body 只带 media_asset_id 与备注）。 */
async function savePlaceFromPhoto(
  mediaAssetId: string,
  name: string,
  note?: string | null,
): Promise<boolean> {
  const projectId = state.projectId
  if (projectId === null) {
    setSavedError('当前没有可用的工作区，无法收藏。', 'project_not_found')
    return false
  }
  state.saved.pending = 'from-photo'
  try {
    const row = await createSavedPlaceRequest(projectId, {
      // name 只是满足后端 schema 的必填约束：带 media_asset_id 时后端以照片上的地点为准。
      name: name.trim() === '' ? '照片地点' : name.trim(),
      media_asset_id: mediaAssetId,
      note: note ?? null,
    })
    if (state.projectId !== projectId) return false
    mergeSavedPlace(row)
    state.saved.phase = 'ready'
    setSavedNotice(
      `已收藏「${row.name}」（${row.source === 'photo' ? '来自照片上已核实的地点' : '手工填写'}）。`,
    )
    return true
  } catch (error) {
    setSavedError(`收藏失败：${describeApiError(error)}`, codeOf(error))
    return false
  } finally {
    state.saved.pending = null
  }
}

/** 手工收藏（name 必填，其余可选）。 */
async function addManualSavedPlace(input: {
  name: string
  address: string | null
  region: string | null
  note: string | null
}): Promise<boolean> {
  const projectId = state.projectId
  if (projectId === null) {
    setSavedError('当前没有可用的工作区，无法收藏。', 'project_not_found')
    return false
  }
  const name = input.name.trim()
  if (name === '') {
    setSavedError('地点名称不能为空。', null)
    return false
  }
  state.saved.pending = 'create'
  try {
    const row = await createSavedPlaceRequest(projectId, {
      name,
      address: emptyToNull(input.address),
      region: emptyToNull(input.region),
      note: emptyToNull(input.note),
    })
    if (state.projectId !== projectId) return false
    mergeSavedPlace(row)
    state.saved.phase = 'ready'
    setSavedNotice(
      `已收藏「${row.name}」。同一地点重复收藏时后端会返回原记录，列表里不会出现两行。`,
    )
    return true
  } catch (error) {
    setSavedError(`添加收藏失败：${describeApiError(error)}`, codeOf(error))
    return false
  } finally {
    state.saved.pending = null
  }
}

/**
 * 收藏一条检索结果。
 *
 * 供应商（provider）与 provider_place_id 一并存下来：它们是这条候选的**真实身份**，
 * 后端据此去重（同一 POI 重复收藏会被识别成同一条）。不带坐标也能存，只是不能定位。
 */
async function saveSearchCandidate(candidate: {
  name: string
  address: string | null
  region: string | null
  latitude: number | null
  longitude: number | null
  provider: string
  providerPlaceId: string | null
}): Promise<boolean> {
  const projectId = state.projectId
  if (projectId === null) {
    setSavedError('当前没有可用的工作区，无法收藏。', 'project_not_found')
    return false
  }
  state.saved.pending = 'create'
  try {
    const row = await createSavedPlaceRequest(projectId, {
      name: candidate.name,
      address: candidate.address,
      region: candidate.region,
      latitude: candidate.latitude,
      longitude: candidate.longitude,
      provider: candidate.provider,
      provider_place_id: candidate.providerPlaceId,
    })
    if (state.projectId !== projectId) return false
    mergeSavedPlace(row)
    state.saved.phase = 'ready'
    setSavedNotice(`已收藏「${row.name}」（来自地点检索结果）。`)
    return true
  } catch (error) {
    setSavedError(`收藏检索结果失败：${describeApiError(error)}`, codeOf(error))
    return false
  } finally {
    state.saved.pending = null
  }
}

/** 取消收藏。失败时**保留**列表里的那一行并显示真实错误（不能假装删掉了）。 */
async function removeSavedPlace(savedPlaceId: string): Promise<boolean> {
  const projectId = state.projectId
  if (projectId === null) {
    return false
  }
  state.saved.pending = `delete:${savedPlaceId}`
  try {
    const removed = state.saved.items.find((item) => item.id === savedPlaceId) ?? null
    await deleteSavedPlaceRequest(projectId, savedPlaceId)
    state.saved.items = state.saved.items.filter((item) => item.id !== savedPlaceId)
    setSavedNotice(removed === null ? '已取消收藏。' : `已取消收藏「${removed.name}」。`)
    return true
  } catch (error) {
    setSavedError(`取消收藏失败：${describeApiError(error)}`, codeOf(error))
    return false
  } finally {
    state.saved.pending = null
  }
}

function setSavedError(text: string, code: string | null): void {
  state.saved.errorText = text
  state.saved.errorCode = code
  state.saved.notice = null
}

function clearSavedError(): void {
  state.saved.errorText = null
  state.saved.errorCode = null
}

function setSavedNotice(text: string | null): void {
  state.saved.notice = text
  state.saved.errorText = null
  state.saved.errorCode = null
}

function emptyToNull(value: string | null | undefined): string | null {
  if (value === null || value === undefined) {
    return null
  }
  const trimmed = value.trim()
  return trimmed === '' ? null : trimmed
}

// ------------------------------------------------------------------ 地点检索
let searchGeneration = 0

async function searchPlaces(query: string, city: string | null): Promise<void> {
  const generation = ++searchGeneration
  const projectId = state.projectId
  const trimmed = query.trim()
  if (projectId === null) {
    return
  }
  if (trimmed === '') {
    state.saved.search.phase = 'error'
    state.saved.search.errorText = '请先写一个要搜索的地名。'
    state.saved.search.errorCode = null
    return
  }
  state.saved.search.phase = 'loading'
  state.saved.search.result = null
  state.saved.search.query = trimmed
  state.saved.search.errorText = null
  state.saved.search.errorCode = null
  try {
    const result = await searchPlacesRequest(projectId, trimmed, city)
    if (state.projectId !== projectId || generation !== searchGeneration) return
    state.saved.search.result = result
    state.saved.search.phase = 'ready'
  } catch (error) {
    if (state.projectId !== projectId || generation !== searchGeneration) return
    state.saved.search.result = null
    state.saved.search.phase = 'error'
    state.saved.search.errorText = `地点检索失败：${describeApiError(error)}`
    state.saved.search.errorCode = codeOf(error)
  }
}

function clearSearch(): void {
  searchGeneration += 1
  state.saved.search.phase = 'idle'
  state.saved.search.query = ''
  state.saved.search.result = null
  state.saved.search.errorText = null
  state.saved.search.errorCode = null
}

// ------------------------------------------------------------------ 路线草稿

function sortDrafts(): void {
  state.routes.drafts.sort((left, right) => left.created_at.localeCompare(right.created_at))
}

function mergeDraft(draft: RouteDraftOut): void {
  const index = state.routes.drafts.findIndex((item) => item.id === draft.id)
  if (index >= 0) {
    state.routes.drafts[index] = draft
  } else {
    state.routes.drafts.push(draft)
  }
  sortDrafts()
}

/**
 * 读一次路线草稿列表（服务器权威）。
 *
 * `keepError = true` 用于「失败之后顺手对齐服务器状态」这条路径：错误提示必须留着，
 * 否则用户看不到失败原因（例如 409 draft_version_conflict 会被随后的刷新清掉）。
 */
async function loadRoutes(keepError = false): Promise<void> {
  const projectId = state.projectId
  if (projectId === null) {
    return
  }
  state.routes.phase = state.routes.drafts.length === 0 ? 'loading' : state.routes.phase
  try {
    const result = await listRoutesRequest(projectId)
    if (state.projectId !== projectId) return
    state.routes.drafts = [...result.drafts].sort((left, right) =>
      left.created_at.localeCompare(right.created_at),
    )
    if (
      state.routes.selectedId === null ||
      !state.routes.drafts.some((d) => d.id === state.routes.selectedId)
    ) {
      state.routes.selectedId = state.routes.drafts[0]?.id ?? null
    }
    state.routes.phase = 'ready'
    if (!keepError) {
      state.routes.errorText = null
      state.routes.errorCode = null
    }
  } catch (error) {
    state.routes.phase = 'error'
    state.routes.errorText = describeApiError(error)
    state.routes.errorCode = codeOf(error)
  }
}

function ensureRoutes(): void {
  if (state.routes.phase === 'idle') {
    void loadRoutes()
  }
}

function selectDraft(draftId: string | null): void {
  state.routes.selectedId = draftId
}

function setRoutesError(text: string, code: string | null): void {
  state.routes.errorText = text
  state.routes.errorCode = code
}

function clearRoutesError(): void {
  state.routes.errorText = null
  state.routes.errorCode = null
}

/** 只发后端 schema 认得的字段（extra="forbid"）。 */
function stopsPayload(stops: RouteStop[]): RouteStop[] {
  return stops.map((stop) => ({
    saved_place_id: stop.saved_place_id ?? null,
    name: stop.name,
    latitude: stop.latitude ?? null,
    longitude: stop.longitude ?? null,
  }))
}

async function createDraft(name: string, mode: 'walking' | 'driving'): Promise<boolean> {
  const projectId = state.projectId
  const trimmed = name.trim()
  if (projectId === null) {
    setRoutesError('当前没有可用的工作区，无法新建路线。', 'project_not_found')
    return false
  }
  if (trimmed === '') {
    setRoutesError('路线名称不能为空。', null)
    return false
  }
  state.routes.pending = 'create'
  try {
    const draft = await createRouteRequest(projectId, { name: trimmed, mode, stops: [] })
    if (state.projectId !== projectId) return false
    mergeDraft(draft)
    state.routes.selectedId = draft.id
    state.routes.phase = 'ready'
    clearRoutesError()
    return true
  } catch (error) {
    setRoutesError(`新建路线失败：${describeApiError(error)}`, codeOf(error))
    return false
  } finally {
    state.routes.pending = null
  }
}

/**
 * 提交一次改动（PATCH）。
 *
 * 一律带 `expected_version`：如果另一个标签页已经改过这个草稿，后端返回 409
 * draft_version_conflict，这里立刻用服务器上的最新草稿覆盖本地（并如实报错），
 * 绝不把用户基于旧版本的操作悄悄写下去。
 */
async function patchDraft(
  draftId: string,
  body: { name?: string; mode?: 'walking' | 'driving'; stops?: RouteStop[] },
  action: string,
): Promise<boolean> {
  const projectId = state.projectId
  const draft = state.routes.drafts.find((item) => item.id === draftId)
  if (projectId === null || draft === undefined) {
    return false
  }
  state.routes.pending = action
  try {
    const updated = await updateRouteRequest(projectId, draftId, {
      ...body,
      stops: body.stops === undefined ? undefined : stopsPayload(body.stops),
      expected_version: draft.input_version,
    })
    if (state.projectId !== projectId) return false
    mergeDraft(updated)
    clearRoutesError()
    return true
  } catch (error) {
    setRoutesError(`改动没有保存：${describeApiError(error)}`, codeOf(error))
    if (error instanceof ApiError && error.status === 409) {
      // 版本冲突 / 重名：以服务器为准刷新一次（保留上面的错误提示），用户再看一遍现状决定怎么办。
      await loadRoutes(true)
    }
    return false
  } finally {
    state.routes.pending = null
  }
}

function renameDraft(draftId: string, name: string): Promise<boolean> {
  const trimmed = name.trim()
  const draft = state.routes.drafts.find((item) => item.id === draftId)
  if (draft === undefined) {
    return Promise.resolve(false)
  }
  if (trimmed === '') {
    setRoutesError('路线名称不能为空。', null)
    return Promise.resolve(false)
  }
  if (trimmed === draft.name) {
    // 没有改动就不发请求：PATCH 空改动不会让 input_version 变化，没必要打扰服务器。
    clearRoutesError()
    return Promise.resolve(true)
  }
  return patchDraft(draftId, { name: trimmed }, `patch:${draftId}`)
}

function setDraftMode(draftId: string, mode: 'walking' | 'driving'): Promise<boolean> {
  const draft = state.routes.drafts.find((item) => item.id === draftId)
  if (draft === undefined || draft.mode === mode) {
    return Promise.resolve(false)
  }
  return patchDraft(draftId, { mode }, `patch:${draftId}`)
}

/** 上下移动一个停留点（越界不动，界面也会禁用对应的按钮）。 */
function moveStop(draftId: string, index: number, delta: number): Promise<boolean> {
  const draft = state.routes.drafts.find((item) => item.id === draftId)
  if (draft === undefined) {
    return Promise.resolve(false)
  }
  const target = index + delta
  if (index < 0 || index >= draft.stops.length || target < 0 || target >= draft.stops.length) {
    return Promise.resolve(false)
  }
  const stops = [...draft.stops]
  const [moved] = stops.splice(index, 1)
  if (moved === undefined) {
    return Promise.resolve(false)
  }
  stops.splice(target, 0, moved)
  return patchDraft(draftId, { stops }, `patch:${draftId}`)
}

function addStopFromSavedPlace(draftId: string, place: SavedPlaceOut): Promise<boolean> {
  const draft = state.routes.drafts.find((item) => item.id === draftId)
  if (draft === undefined) {
    return Promise.resolve(false)
  }
  if (place.latitude === null || place.longitude === null) {
    // 没有坐标的收藏算不了路：这一步由界面拦住，不发请求，也不假装加进去了。
    setRoutesError(
      `「${place.name}」没有经纬度，不能作为停留点（算路必须有坐标）。`,
      'stop_missing_coordinates',
    )
    return Promise.resolve(false)
  }
  const stops: RouteStop[] = [
    ...draft.stops,
    {
      saved_place_id: place.id,
      name: place.name,
      latitude: place.latitude,
      longitude: place.longitude,
    },
  ]
  return patchDraft(draftId, { stops }, `patch:${draftId}`)
}

function removeStop(draftId: string, index: number): Promise<boolean> {
  const draft = state.routes.drafts.find((item) => item.id === draftId)
  if (draft === undefined || index < 0 || index >= draft.stops.length) {
    return Promise.resolve(false)
  }
  const stops = draft.stops.filter((_, position) => position !== index)
  return patchDraft(draftId, { stops }, `patch:${draftId}`)
}

async function removeDraft(draftId: string): Promise<boolean> {
  const projectId = state.projectId
  if (projectId === null) {
    return false
  }
  state.routes.pending = `delete:${draftId}`
  try {
    await deleteRouteRequest(projectId, draftId)
    state.routes.drafts = state.routes.drafts.filter((item) => item.id !== draftId)
    delete state.routes.revisions[draftId]
    if (state.routes.selectedId === draftId) {
      state.routes.selectedId = state.routes.drafts[0]?.id ?? null
    }
    clearRoutesError()
    return true
  } catch (error) {
    setRoutesError(`删除草稿失败：${describeApiError(error)}`, codeOf(error))
    return false
  } finally {
    state.routes.pending = null
  }
}

/**
 * 算路（同步调用供应商，可能持续数秒）。
 *
 * 失败也是 200：响应里的草稿会带 `route_unavailable_reason = compute_failed:<code>`，
 * 停留点原样保留。这里把响应合并进列表，界面据此显示真实失败原因与「重试」。
 * 只有真正的 HTTP 错误（422 点数不足 / 503 未配置算路服务）才走 catch。
 */
async function computeDraft(draftId: string): Promise<boolean> {
  const projectId = state.projectId
  if (projectId === null) {
    return false
  }
  if (state.routes.computingId !== null) {
    return false
  }
  state.routes.computingId = draftId
  state.routes.pending = `compute:${draftId}`
  try {
    const draft = await computeRouteRequest(projectId, draftId)
    if (state.projectId !== projectId) return false
    mergeDraft(draft)
    clearRoutesError()
    void loadRevisions(draftId)
    return draft.current_revision !== null
  } catch (error) {
    setRoutesError(`算路请求失败：${describeApiError(error)}`, codeOf(error))
    // 失败也要把服务器上的真实草稿状态取回来（例如 stops 没有变，但错误要知道）。
    await loadRoutes(true)
    return false
  } finally {
    state.routes.computingId = null
    state.routes.pending = null
  }
}

async function loadRevisions(draftId: string): Promise<void> {
  const projectId = state.projectId
  if (projectId === null) {
    return
  }
  const current: RevisionsState = state.routes.revisions[draftId] ?? {
    phase: 'idle',
    items: [],
    errorText: null,
    errorCode: null,
  }
  current.phase = current.items.length === 0 ? 'loading' : current.phase
  state.routes.revisions[draftId] = current
  try {
    const result = await listRouteRevisions(projectId, draftId)
    current.items = result.revisions
    current.phase = 'ready'
    current.errorText = null
    current.errorCode = null
  } catch (error) {
    current.phase = 'error'
    current.errorText = describeApiError(error)
    current.errorCode = codeOf(error)
  }
}

// ------------------------------------------------------------------ 派生状态

const selectedDraft = computed<RouteDraftOut | null>(() => {
  const id = state.routes.selectedId
  if (id === null) {
    return null
  }
  return state.routes.drafts.find((item) => item.id === id) ?? null
})

/** 正在算路的草稿 id（地图/面板据此显示真实进行态）。 */
const computingDraftId = computed<string | null>(() => state.routes.computingId)

export type RouteAvailabilityKind = 'current' | 'not_computed' | 'failed' | 'stale' | 'unknown'

export interface RouteAvailability {
  kind: RouteAvailabilityKind
  /** 需要向用户解释时的文案；有当前路线时为 null。 */
  text: string | null
  /** 算路失败时的供应商错误码。 */
  errorCode: string | null
}

/**
 * 把草稿的"当前是否可用"翻译成界面状态。
 *
 * 唯一的判据是后端给的 `current_revision.is_current` 与 `route_unavailable_reason`；
 * 绝不用"有没有 latest_revision""上次算过没有"来猜。
 */
export function routeAvailability(draft: RouteDraftOut): RouteAvailability {
  const current = draft.current_revision
  if (current !== null && current.is_current) {
    return { kind: 'current', text: null, errorCode: null }
  }
  const reason = draft.route_unavailable_reason
  const text = routeUnavailableText(reason)
  const errorCode = routeUnavailableErrorCode(reason)
  if (reason === 'not_computed') {
    return { kind: 'not_computed', text, errorCode }
  }
  if (reason === 'stale_after_edit') {
    return { kind: 'stale', text, errorCode }
  }
  if (typeof reason === 'string' && reason.startsWith('compute_failed:')) {
    return { kind: 'failed', text, errorCode }
  }
  return {
    kind: 'unknown',
    text: text ?? '服务器没有说明这条路线当前是否可用（current_revision 为空且没有不可用原因）',
    errorCode,
  }
}

/** 收藏里是否已经有这张照片上的地点（用 place_id 匹配，界面据此显示「已收藏」）。 */
function savedPlaceForPhotoPlace(placeId: string | null): SavedPlaceOut | null {
  if (placeId === null) {
    return null
  }
  return state.saved.items.find((item) => item.place_id === placeId) ?? null
}

// ------------------------------------------------------------------ 事件驱动刷新

/**
 * SSE 事件到达时的对齐（不是轮询）：收藏/路线的事件都可能来自**另一个标签页**，
 * 所以收到就把已经读过的列表重新拉一次。没读过的列表保持 idle，等用户打开时再拉。
 */
function handleWorkspaceEvent(type: string): void {
  if (state.projectId === null) {
    return
  }
  if (type === 'place.saved' || type === 'place.unsaved') {
    if (state.saved.phase !== 'idle') {
      void loadSavedPlaces()
    }
    return
  }
  if (
    type === 'route.draft_created' ||
    type === 'route.draft_updated' ||
    type === 'route.computed'
  ) {
    if (state.routes.phase !== 'idle') {
      void loadRoutes()
      const selected = state.routes.selectedId
      if (selected !== null && state.routes.revisions[selected] !== undefined) {
        void loadRevisions(selected)
      }
    }
  }
}

async function addPlaceToRoute(place: {
  name: string
  latitude: number | null
  longitude: number | null
}): Promise<boolean> {
  if (state.routes.pending || place.latitude === null || place.longitude === null) return false
  const projectId = state.projectId
  await loadRoutes()
  if (projectId !== state.projectId) return false
  if (
    !selectedDraft.value &&
    !(await createDraft(`我的路线 ${state.routes.drafts.length + 1}`, 'walking'))
  )
    return false
  const draft = selectedDraft.value
  if (!draft) return false
  if (draft.stops.some((s) => s.latitude === place.latitude && s.longitude === place.longitude))
    return true
  const ok = await patchDraft(
    draft.id,
    {
      stops: [
        ...draft.stops,
        { name: place.name, latitude: place.latitude, longitude: place.longitude },
      ],
    },
    'add-place',
  )
  return ok
}

export function usePlacesRoutes() {
  return {
    state,
    selectedDraft,
    computingDraftId,
    // 工作区绑定（由 useWorkspace 调用）
    bindWorkspace,
    // 收藏
    loadSavedPlaces,
    ensureSavedPlaces,
    savePlaceFromPhoto,
    addManualSavedPlace,
    saveSearchCandidate,
    removeSavedPlace,
    clearSavedError,
    setSavedNotice,
    savedPlaceForPhotoPlace,
    // 地点检索
    searchPlaces,
    clearSearch,
    // 路线
    loadRoutes,
    ensureRoutes,
    selectDraft,
    createDraft,
    renameDraft,
    setDraftMode,
    moveStop,
    removeStop,
    addStopFromSavedPlace,
    addPlaceToRoute,
    removeDraft,
    computeDraft,
    loadRevisions,
    clearRoutesError,
    // 事件
    handleWorkspaceEvent,
  }
}
