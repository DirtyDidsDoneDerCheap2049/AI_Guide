/** 展示层格式化工具。 */

import type { RunOut } from '../api/types'

function pad(value: number): string {
  return value < 10 ? `0${value}` : String(value)
}

/**
 * 后端所有时间列存的是 UTC naive（见 backend/app/models.py::utcnow），
 * 序列化后不带时区后缀（例如 2026-09-18T09:30:00）。
 * JavaScript 会把「无时区的 ISO 串」当成本地时间解析，导致整体偏移，
 * 所以这里先补上 Z 再解析。
 */
export function parseServerTime(value: string | null | undefined): Date | null {
  if (value === null || value === undefined || value === '') {
    return null
  }
  const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/.test(value)
  const date = new Date(hasZone ? value : `${value}Z`)
  return Number.isNaN(date.getTime()) ? null : date
}

/** 活动记录用的时间：HH:mm:ss（本地时区）。 */
export function formatTime(value: string | null | undefined): string {
  const date = parseServerTime(value)
  if (date === null) {
    return '--:--:--'
  }
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

/** 卡片、地点事实用的时间：YYYY-MM-DD HH:mm。 */
export function formatDateTime(value: string | null | undefined): string {
  const date = parseServerTime(value)
  if (date === null) {
    return '—'
  }
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || Number.isNaN(bytes)) {
    return '—'
  }
  if (bytes < 1024) {
    return `${bytes} B`
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KB`
  }
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`
}

/** 置信度：后端是 0~1 的 float，界面上按百分比展示。 */
export function formatConfidence(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return '未提供'
  }
  const clamped = Math.min(1, Math.max(0, value))
  return `${Math.round(clamped * 100)}%`
}

export function confidencePercent(value: number | null | undefined): number {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return 0
  }
  return Math.round(Math.min(1, Math.max(0, value)) * 100)
}

export function formatCoordinates(
  latitude: number | null | undefined,
  longitude: number | null | undefined,
): string {
  if (
    latitude === null ||
    latitude === undefined ||
    longitude === null ||
    longitude === undefined
  ) {
    return '—'
  }
  return `${latitude.toFixed(5)}, ${longitude.toFixed(5)}`
}

// --------------------------------------------------------------- 路线距离与时长

/**
 * 距离：**只用于供应商返回的距离**（RouteRevisionOut.distance_meters / legs[].distance_meters）。
 *
 * 契约（docs/productization/D3b-收藏与路线接口契约.md 0 节）明确禁止用直线距离、
 * 固定速度或任何估算值显示"公里"；所以这个函数只做单位换算，没有任何推算成分。
 * 返回 null = 没有可显示的数字，调用方必须写明原因（尚未算路 / 算路失败 / 结果已过期），
 * 不能显示 0 或占位数字。
 */
export function formatDistanceKm(meters: number | null | undefined): string | null {
  if (meters === null || meters === undefined || !Number.isFinite(meters)) {
    return null
  }
  return `${(meters / 1000).toFixed(1)} 公里`
}

/** 距离的原始米数（括号里的精确值；同样是供应商数据，不做取整以外的加工）。 */
export function formatDistanceMeters(meters: number | null | undefined): string | null {
  if (meters === null || meters === undefined || !Number.isFinite(meters)) {
    return null
  }
  return `${Math.round(meters)} 米`
}

/**
 * 时长：同样是供应商返回的秒数（duration_seconds）换来的，不参与任何推算。
 * ≥1 小时 → "1 小时 30 分钟"；≥1 分钟 → "30 分钟"；<1 分钟 → "45 秒"。
 */
export function formatDurationText(seconds: number | null | undefined): string | null {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) {
    return null
  }
  const total = Math.max(0, Math.round(seconds))
  if (total < 60) {
    return `${total} 秒`
  }
  const minutes = Math.floor(total / 60)
  if (minutes < 60) {
    return `${minutes} 分钟`
  }
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return rest === 0 ? `${hours} 小时` : `${hours} 小时 ${rest} 分钟`
}

export function formatDimensions(width: number | null, height: number | null): string {
  if (width === null || height === null) {
    return '尺寸未知'
  }
  return `${width}×${height}`
}

/** 运行用量摘要：让用户看到 Agent 的预算消耗。 */
export function formatRunBudget(run: RunOut): string {
  const cost = run.used_cost.toFixed(4)
  const maxCost = run.budget_max_cost.toFixed(4)
  return `步骤 ${run.used_steps}/${run.budget_max_steps} · 工具 ${run.used_tool_calls}/${run.budget_max_tool_calls} · Token ${run.used_tokens}/${run.budget_max_tokens} · 成本 $${cost}/$${maxCost}`
}

export function formatRunDuration(run: RunOut): string {
  const started = parseServerTime(run.started_at)
  if (started === null) {
    return '尚未开始'
  }
  const finished = parseServerTime(run.finished_at) ?? new Date()
  const seconds = Math.max(0, Math.round((finished.getTime() - started.getTime()) / 1000))
  if (seconds < 60) {
    return `${seconds} 秒`
  }
  const minutes = Math.floor(seconds / 60)
  return `${minutes} 分 ${seconds % 60} 秒`
}
