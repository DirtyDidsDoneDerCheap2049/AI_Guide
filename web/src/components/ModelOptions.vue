<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { levelLabel, useModelOptions } from '../composables/useModelOptions'
const options = useModelOptions()
const { state } = options
const open = ref(false)
const root = ref<HTMLElement | null>(null)
const trigger = ref<HTMLButtonElement | null>(null)
const levels = computed(options.levels)
const index = computed(() => Math.max(0, levels.value.indexOf(state.thinkingLevel)))
const label = (level: string) => levelLabel(level, levels.value.length === 2)
function choose(value: number) {
  state.thinkingLevel = levels.value[value] ?? 'off'
  options.save()
}
function outside(event: PointerEvent) {
  if (!root.value?.contains(event.target as Node)) open.value = false
}
function escape() {
  open.value = false
  trigger.value?.focus()
}
function changeModel() {
  options.save()
  open.value = false
}
onMounted(() => {
  void options.load()
  document.addEventListener('pointerdown', outside)
})
onBeforeUnmount(() => document.removeEventListener('pointerdown', outside))
</script>
<template>
  <div ref="root" class="model-options" @keydown.esc.stop="escape">
    <template v-if="state.models.length">
      <select v-model="state.modelId" aria-label="对话模型" @change="changeModel">
        <option v-for="model in state.models" :key="model.id" :value="model.id">
          {{ model.label }}
        </option>
      </select>
      <div class="effort-control">
        <button
          ref="trigger"
          class="effort-trigger"
          type="button"
          :aria-expanded="open"
          :disabled="!options.supportsThinking()"
          :class="{ active: state.thinking }"
          :title="options.supportsThinking() ? '调整思考强度' : '此模型未配置可切换的思考模式'"
          @click="open = !open"
        >
          {{ label(state.thinkingLevel) }} <span aria-hidden="true">⌄</span>
        </button>
        <div v-if="open" class="effort-popover" role="group" aria-label="选择思考档位">
          <input
            type="range"
            aria-label="思考档位"
            :aria-valuetext="label(state.thinkingLevel)"
            min="0"
            :max="levels.length - 1"
            step="1"
            :value="index"
            @input="choose(Number(($event.target as HTMLInputElement).value))"
          />
          <div class="effort-labels">
            <button
              v-for="(level, i) in levels"
              :key="level"
              type="button"
              :aria-pressed="state.thinkingLevel === level"
              @click="choose(i)"
            >
              {{ label(level) }}
            </button>
          </div>
          <p>更深入的思考可能需要更长时间</p>
        </div>
      </div>
    </template>
    <button v-else-if="state.error" type="button" @click="options.load()">
      {{ state.error }} · 重试
    </button>
    <span v-else>正在加载模型…</span>
  </div>
</template>
<style scoped>
.model-options {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 4px 0 8px;
  font-size: 12px;
  color: #62736a;
}
select,
button {
  font: inherit;
  color: inherit;
  background: transparent;
  border: 1px solid #dce4d6;
  border-radius: 16px;
  padding: 6px 10px;
  max-width: 240px;
}
button {
  cursor: pointer;
}
.effort-control {
  position: relative;
}
.effort-trigger.active {
  background: #e6eee7;
  color: #315c49;
  border-color: #73937e;
}
button:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.effort-popover {
  position: absolute;
  bottom: calc(100% + 10px);
  left: 0;
  z-index: 40;
  width: 240px;
  box-sizing: border-box;
  padding: 20px 16px 12px;
  border: 1px solid #dce4d6;
  border-radius: 14px;
  background: #fff;
  box-shadow: 0 8px 28px #263e3520;
}
input[type='range'] {
  display: block;
  width: 100%;
  margin: 0 0 10px;
  accent-color: #315c49;
  cursor: pointer;
}
.effort-labels {
  display: flex;
  justify-content: space-between;
  gap: 4px;
}
.effort-labels button {
  padding: 4px;
  border: 0;
  border-radius: 5px;
}
.effort-labels button[aria-pressed='true'] {
  color: #315c49;
  background: #e6eee7;
  font-weight: 600;
}
.effort-popover p {
  margin: 12px 0 0;
  font-size: 11px;
  color: #879288;
}
</style>
