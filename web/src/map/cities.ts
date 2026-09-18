/**
 * 地图「示例范围」用的城市中心点。
 *
 * 说明（避免误解）：
 * - 这些是高德 GCJ-02 坐标系下的城市中心参考坐标，只用于在没有已确认地点时给地图一个可看的范围；
 * - 它们不是用户定位结果，也不是识别结果，界面上必须标注「示例范围」；
 * - 用户确认地点后，地图一律用项目快照里的 place.latitude/longitude（同样是 GCJ-02），不再用这里的坐标。
 */

export interface MapCity {
  key: string
  name: string
  /** GCJ-02 经度 */
  longitude: number
  /** GCJ-02 纬度 */
  latitude: number
}

export const MAP_CITIES: readonly MapCity[] = [
  { key: 'hangzhou', name: '杭州', longitude: 120.153576, latitude: 30.287459 },
  { key: 'shanghai', name: '上海', longitude: 121.472644, latitude: 31.231706 },
  { key: 'beijing', name: '北京', longitude: 116.407526, latitude: 39.90403 },
  { key: 'chengdu', name: '成都', longitude: 104.065735, latitude: 30.659462 },
  { key: 'xian', name: '西安', longitude: 108.948024, latitude: 34.263161 },
  { key: 'guangzhou', name: '广州', longitude: 113.280637, latitude: 23.125178 },
]

export const DEFAULT_CITY_KEY = 'hangzhou'

export function findCity(key: string | null | undefined): MapCity | null {
  if (key === null || key === undefined) {
    return null
  }
  return MAP_CITIES.find((city) => city.key === key) ?? null
}

/** 按城市名（例如工作区的 city_hint）匹配城市；匹配不到返回 null。 */
export function findCityByName(name: string | null | undefined): MapCity | null {
  if (name === null || name === undefined) {
    return null
  }
  const trimmed = name.trim()
  if (trimmed === '') {
    return null
  }
  return MAP_CITIES.find((city) => trimmed.includes(city.name)) ?? null
}
