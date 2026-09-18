<script setup lang="ts">
/**
 * 登录设备列表（GET /auth/sessions）。
 *
 * 这里列的是**账号的登录会话**（auth_sessions），与匿名访客会话（GET /session）不是一回事：
 * 前者可以单独撤销、能在别的设备上出现，后者只是这台浏览器的匿名身份。
 *
 * 撤销「当前会话」等价于退出登录：撤销成功后 useAuth 会立刻切回未登录并清空本地私有数据。
 * 撤销别的会话要先确认（window.confirm），因为那台设备会被登出。
 */
import { computed } from 'vue'

import { useAuth } from '../composables/useAuth'
import { formatDateTime } from '../utils/format'

const auth = useAuth()
const { state, otherSessions } = auth

const busy = computed(() => state.pending !== null || state.revokingId !== null)
</script>

<template>
  <section class="account__block">
    <div class="account__rowhead">
      <h3 class="account__title">登录会话</h3>
      <button
        class="btn btn--ghost btn--sm"
        type="button"
        :disabled="busy"
        @click="auth.loadSessions()"
      >
        {{ state.sessionsStatus === 'loading' ? '读取中…' : '刷新列表' }}
      </button>
    </div>

    <p class="field__note">查看最近登录的设备。不认识的设备可以退出。</p>

    <p
      v-if="state.sessionsStatus === 'loading' && state.sessions.length === 0"
      class="panel__placeholder"
    >
      正在读取会话列表…
    </p>

    <p v-else-if="state.sessionsStatus === 'error'" class="field__error" role="alert">
      读取会话列表失败：{{ state.sessionsError }}
      <button class="btn btn--sm" type="button" :disabled="busy" @click="auth.loadSessions()">
        重试
      </button>
    </p>

    <template v-else>
      <p
        v-if="state.sessionsStatus === 'ready' && state.sessions.length === 0"
        class="panel__placeholder"
      >
        服务器返回的会话列表是空的（正常情况下至少应该有当前这个会话）。
      </p>

      <ul v-else class="sessions">
        <li v-for="session in state.sessions" :key="session.id" class="session">
          <div class="session__head">
            <span v-if="session.current" class="badge badge--ok">当前会话</span>
            <span v-else class="tag tag--muted">其它设备</span>
            <span class="session__ua">{{ session.user_agent ?? '未知设备' }}</span>
          </div>
          <div class="session__meta">
            登录于 {{ formatDateTime(session.created_at) }} · 最近活动
            {{ session.last_seen_at === null ? '未提供' : formatDateTime(session.last_seen_at) }} ·
            到期
            {{ formatDateTime(session.expires_at) }}
          </div>

          <button
            class="btn btn--sm"
            :class="{ 'btn--danger': session.current }"
            type="button"
            :disabled="busy"
            @click="auth.revokeSession(session)"
          >
            {{
              state.revokingId === session.id
                ? '撤销中…'
                : session.current
                  ? '退出此设备'
                  : '退出此设备'
            }}
          </button>
        </li>
      </ul>

      <p v-if="state.sessionsNotice !== null" class="account__notice" role="status">
        {{ state.sessionsNotice }}
      </p>
      <p class="field__note">
        共 {{ state.sessions.length }} 个会话，其中其它设备
        {{ otherSessions.length }} 个。撤销前会再问一次。
      </p>
    </template>
  </section>
</template>
