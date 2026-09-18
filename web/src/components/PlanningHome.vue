<script setup lang="ts">
import { computed, ref } from 'vue'
import { useWorkspace } from '../composables/useWorkspace'
import { useAuth } from '../composables/useAuth'
import { MAP_CITIES } from '../map/cities'
import TripsPanel from './TripsPanel.vue'
import DiscoveryPanel from './DiscoveryPanel.vue'
const workspace = useWorkspace()
const { state, project } = workspace
const auth = useAuth()
const cityInput = ref('')
const choosing = ref(false)
function submitOnEnter(event: KeyboardEvent) {
  if (event.isComposing) return
  event.preventDefault()
  void workspace.startPlanning(state.draftQuestion)
}
const suggestions = computed(() => [
  `第一次去${state.destination}，两天怎么安排？`,
  `${state.destination}有哪些适合慢慢逛的地方？`,
  `帮我比较${state.destination}的住宿区域`,
])
function choose(city: string) {
  if (!city.trim()) return
  state.destination = city.trim().slice(0, 60)
  try {
    localStorage.setItem('guide-destination', state.destination)
  } catch {
    /* Browsing still works without local storage. */
  }
  choosing.value = false
}
try {
  const saved = localStorage.getItem('guide-destination')
  if (saved?.trim()) state.destination = saved.slice(0, 60)
} catch {
  /* default 杭州 */
}
</script>
<template>
  <div class="landing planning-home">
    <header class="landing-nav">
      <a class="brand" href="/" @click.prevent="workspace.openExplore()"
        ><span class="brand-symbol">↗</span> AI·Guide</a
      >
      <div class="nav-links">
        <span class="desktop-only">把想去的地方，安排成一次旅行</span
        ><button class="btn" @click="auth.openPanel()">
          {{ auth.state.user?.display_name || (auth.state.user ? '我的账号' : '登录 / 注册') }}
        </button>
      </div>
    </header>
    <main class="landing-main">
      <section class="landing-hero">
        <div class="destination-picker">
          <span>目的地</span
          ><button class="city-trigger" :aria-expanded="choosing" @click="choosing = !choosing">
            {{ state.destination }} ⌄</button
          ><span class="city-hint">可切换，无需定位</span>
          <div v-if="choosing" class="city-popover">
            <div class="section-heading">
              <strong>想去哪个城市？</strong
              ><button class="icon-button" aria-label="关闭城市选择" @click="choosing = false">
                ×
              </button>
            </div>
            <div class="city-options">
              <button
                v-for="city in MAP_CITIES"
                :key="city.key"
                :class="{ active: city.name === state.destination }"
                @click="choose(city.name)"
              >
                {{ city.name }}
              </button>
            </div>
            <form @submit.prevent="choose(cityInput)">
              <input
                v-model="cityInput"
                aria-label="其他目的地城市"
                placeholder="也可以输入其他城市"
                maxlength="60"
              /><button class="btn" :disabled="!cityInput.trim()">确定</button>
            </form>
          </div>
        </div>
        <h1>下一站，{{ state.destination }}。</h1>
        <p class="hero-subtitle">先看看想去的地方，再一起安排路线。</p>
        <form class="hero-composer" @submit.prevent="workspace.startPlanning(state.draftQuestion)">
          <textarea
            v-model="state.draftQuestion"
            aria-label="想去哪里，或者想了解什么"
            :placeholder="`比如：去${state.destination}玩两天，想逛景点，也想留点时间随便走走`"
            rows="2"
            maxlength="2000"
            @keydown.enter.exact="submitOnEnter"
          />
          <div class="hero-composer-actions">
            <button type="button" class="text-button" @click="workspace.startPlanning()">
              先看看地图和资料 ↗</button
            ><span class="composer-limit">无需登录，即可开始</span
            ><button
              type="submit"
              class="send-button"
              aria-label="开始规划"
              :disabled="
                state.pending !== null || state.sendingMessage || !state.draftQuestion.trim()
              "
            >
              ↑
            </button>
          </div>
        </form>
        <div class="planning-suggestions">
          <button
            v-for="question in suggestions"
            :key="question"
            class="suggestion-chip"
            :disabled="!!state.pending || state.sendingMessage"
            @click="workspace.startPlanning(question)"
          >
            {{ question }}
          </button>
        </div>
        <p
          v-if="state.notice"
          class="landing-notice"
          :class="{ field__error: state.notice.kind === 'error' }"
          role="status"
        >
          {{ state.notice.text }}
        </p>
        <button v-if="project" class="resume-link" @click="workspace.enterWork()">
          继续上次的旅行 <strong>{{ project.title }}</strong
          ><span>↗</span>
        </button>
      </section>
      <DiscoveryPanel :city="state.destination" home />
      <TripsPanel />
      <footer class="landing-footer">
        <span>AI·Guide</span>
        <p>地点与路线来自高德 · AI 建议仅供参考</p>
      </footer>
    </main>
  </div>
</template>
