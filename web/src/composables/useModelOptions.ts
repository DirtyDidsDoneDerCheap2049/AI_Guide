import { reactive } from 'vue'
import { getModelOptions } from '../api/client'

const state = reactive({
  models: [] as {
    id: string
    label: string
    supports_thinking: boolean
    thinking_levels: string[]
  }[],
  modelId: '',
  thinking: false,
  thinkingLevel: 'off',
  error: '',
})
let loading: Promise<void> | null = null

async function load(): Promise<void> {
  if (loading) return loading
  loading = (async () => {
    try {
      const data = await getModelOptions()
      state.models = data.models
      let saved: { modelId?: string; thinking?: boolean; thinkingLevel?: string } = {}
      try {
        saved = JSON.parse(localStorage.getItem('tripwright-model') || '{}')
      } catch {
        /* optional */
      }
      state.modelId = data.models.some((m) => m.id === saved?.modelId)
        ? saved.modelId!
        : data.default_id
      state.thinking = (saved?.thinking ?? data.default_thinking) && supportsThinking()
      state.thinkingLevel = saved?.thinkingLevel ?? (state.thinking ? 'medium' : 'off')
      normalizeLevel()
      state.error = ''
    } catch {
      state.error = '模型列表加载失败'
      loading = null
    }
  })()
  return loading
}
function supportsThinking(): boolean {
  return state.models.find((m) => m.id === state.modelId)?.supports_thinking ?? false
}
function save(): void {
  normalizeLevel()
  try {
    localStorage.setItem(
      'tripwright-model',
      JSON.stringify({
        modelId: state.modelId,
        thinking: state.thinking,
        thinkingLevel: state.thinkingLevel,
      }),
    )
  } catch {
    /* optional */
  }
}
export function useModelOptions() {
  return { state, load, save, supportsThinking, levels }
}

function levels(): string[] {
  return state.models.find((m) => m.id === state.modelId)?.thinking_levels ?? ['off']
}
function normalizeLevel(): void {
  if (!levels().includes(state.thinkingLevel))
    state.thinkingLevel = supportsThinking() && state.thinking ? levels().at(-1)! : 'off'
  state.thinking = state.thinkingLevel !== 'off'
}
export function levelLabel(level: string, binary = false): string {
  if (binary && level !== 'off') return '开启'
  return (
    ({ off: '关闭', low: '轻度', medium: '标准', high: '深入' } as Record<string, string>)[level] ??
    '关闭'
  )
}
