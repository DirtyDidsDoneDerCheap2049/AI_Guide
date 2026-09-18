<script setup lang="ts">
import { onMounted } from 'vue'
import { usePlacesRoutes } from '../composables/usePlacesRoutes'
import { useGuidePlaces } from '../composables/useGuidePlaces'
const places = usePlacesRoutes()
const guide = useGuidePlaces()
onMounted(() => places.ensureSavedPlaces())
</script>
<template>
  <section class="map-side-panel">
    <div class="section-heading">
      <h2>想去的地方</h2>
      <span>{{ places.state.saved.items.length }} 处</span>
    </div>
    <p v-if="places.state.saved.errorText" class="field__error" role="alert">
      {{ places.state.saved.errorText }}
      <button class="text-button" @click="places.loadSavedPlaces()">重试</button>
    </p>
    <div v-if="!places.state.saved.items.length" class="empty-state">
      <span>♡</span>
      <h3>先留住感兴趣的地方</h3>
      <p>搜索地点或点开导游推荐，点击收藏就会出现在这里。</p>
    </div>
    <article v-for="place in places.state.saved.items" :key="place.id" class="place-card">
      <button class="place-card-main" @click="guide.select(place)">
        <span class="place-pin">↗</span
        ><span
          ><strong>{{ place.name }}</strong
          ><small>{{ place.address || place.region || '位置待确认' }}</small></span
        >
      </button>
      <div class="place-actions">
        <button class="text-button" @click="guide.ask(place)">问导游</button
        ><button
          class="text-button"
          :disabled="guide.adding.value || place.latitude === null"
          @click="guide.add(place)"
        >
          加入路线</button
        ><button
          class="text-button muted"
          :disabled="places.state.saved.pending !== null"
          @click="places.removeSavedPlace(place.id)"
        >
          取消收藏
        </button>
      </div>
    </article>
  </section>
</template>
