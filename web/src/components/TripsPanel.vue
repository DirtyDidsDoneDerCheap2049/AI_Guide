<script setup lang="ts">
import { useAuth } from '../composables/useAuth'
import { formatDateTime } from '../utils/format'
const auth = useAuth()
</script>
<template>
  <section v-if="auth.state.trips.length || auth.state.tripsError" class="recent-trips">
    <div class="section-heading">
      <h2>我的旅行</h2>
      <button class="text-button" @click="auth.loadTrips()">刷新</button>
    </div>
    <p v-if="auth.state.tripsError" class="field__error">旅行列表暂时没有加载出来，请重试。</p>
    <div class="trip-grid">
      <button
        v-for="trip in auth.state.trips"
        :key="trip.id"
        class="trip-tile"
        :disabled="auth.state.openingTripId !== null"
        @click="auth.openTrip(trip)"
      >
        <span class="trip-icon">↗</span
        ><span
          ><strong>{{ trip.title }}</strong
          ><small
            >{{ trip.city_hint || '我的旅行' }} · {{ formatDateTime(trip.updated_at) }}</small
          ></span
        ><span>→</span>
      </button>
    </div>
  </section>
</template>
