<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { MIN_PASSWORD_LENGTH, useAuth } from '../composables/useAuth'
import { useWorkspace } from '../composables/useWorkspace'
import AccountSessions from './AccountSessions.vue'
const auth = useAuth()
const { state, emailUnverified } = auth
const workspace = useWorkspace()
const busy = computed(() => state.pending !== null)
const showClaim = computed(
  () => workspace.project.value !== null && auth.projectScope.value !== 'account',
)
const card = ref<HTMLElement | null>(null)
let returnFocus: HTMLElement | null = null
watch(
  () => state.open,
  async (open) => {
    if (open) {
      returnFocus = document.activeElement as HTMLElement
      await nextTick()
      card.value?.focus()
    } else {
      await nextTick()
      if (returnFocus?.isConnected) returnFocus.focus()
    }
  },
)
function trap(event: KeyboardEvent) {
  if (event.key !== 'Tab' || !card.value) return
  const items = [
    ...card.value.querySelectorAll<HTMLElement>(
      'button:not(:disabled), input:not(:disabled), summary, a[href]',
    ),
  ].filter((e) => e.offsetParent !== null)
  const first = items[0],
    last = items.at(-1)
  if (
    event.shiftKey &&
    (document.activeElement === first || document.activeElement === card.value)
  ) {
    event.preventDefault()
    last?.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first?.focus()
  }
}
</script>
<template>
  <div v-if="state.open" class="account-backdrop" @click.self="auth.closePanel()">
    <section
      ref="card"
      class="account-dialog"
      role="dialog"
      aria-modal="true"
      aria-labelledby="account-title"
      tabindex="-1"
      @keydown.esc="auth.closePanel()"
      @keydown="trap"
    >
      <button class="icon-button dialog-close" aria-label="关闭账号窗口" @click="auth.closePanel()">
        ×
      </button>
      <span class="brand-symbol">↗</span>
      <h2 id="account-title">
        {{
          state.status === 'authenticated'
            ? '我的账号'
            : state.form === 'register'
              ? '把旅行留在这里'
              : state.form === 'forgot'
                ? '找回密码'
                : state.form === 'reset'
                  ? '设置新密码'
                  : '欢迎回来'
        }}
      </h2>
      <p class="account-subtitle">
        {{
          state.status === 'authenticated' ? state.user?.email : '保存喜欢的地方，下次接着出发。'
        }}
      </p>
      <p v-if="state.status === 'unknown'" role="status">
        {{ state.meError ? '暂时读不到登录状态。' : '正在读取账号…'
        }}<button v-if="state.meError" class="text-button" @click="auth.bootstrap()">重试</button>
      </p>
      <template v-else-if="state.status === 'authenticated'">
        <div v-if="showClaim" class="account-save-callout">
          <p>将这次旅行保存到账号，换设备也能继续。</p>
          <button class="btn btn--primary" :disabled="busy" @click="auth.claimCurrentProject()">
            保存这次旅行
          </button>
          <p v-if="state.claimError" class="field__error">{{ state.claimError }}</p>
          <p v-if="state.claimNotice">{{ state.claimNotice }}</p>
        </div>
        <div v-if="emailUnverified" class="gentle-note">
          邮箱还未验证。<button
            class="text-button"
            :disabled="busy"
            @click="auth.resendVerification()"
          >
            重发验证邮件
          </button>
        </div>
        <p v-if="state.resendNotice || state.verifyNotice" role="status">
          {{ state.resendNotice || state.verifyNotice }}
        </p>
        <p v-if="state.resendError || state.verifyError" class="field__error">
          {{ state.resendError || state.verifyError }}
        </p>
        <button
          v-if="state.verifyToken"
          class="btn"
          :disabled="busy"
          @click="auth.submitVerifyEmail()"
        >
          确认验证邮箱
        </button>
        <details class="account-settings">
          <summary>修改密码</summary>
          <form @submit.prevent="auth.submitChangePassword()">
            <label class="field"
              >当前密码<input
                v-model="state.currentPassword"
                class="field__input"
                type="password"
                autocomplete="current-password"
                required /></label
            ><label class="field"
              >新密码<input
                v-model="state.newPassword"
                class="field__input"
                type="password"
                autocomplete="new-password"
                :minlength="MIN_PASSWORD_LENGTH"
                required /></label
            ><label class="field"
              >再次输入新密码<input
                v-model="state.newPasswordConfirm"
                class="field__input"
                type="password"
                autocomplete="new-password"
                required
            /></label>
            <p v-if="state.changeError" class="field__error">{{ state.changeError }}</p>
            <p v-if="state.changeNotice">{{ state.changeNotice }}</p>
            <button class="btn" :disabled="busy">修改密码</button>
          </form>
        </details>
        <details class="account-settings">
          <summary>登录设备</summary>
          <AccountSessions />
        </details>
        <button class="btn btn--block" :disabled="busy" @click="auth.logout()">退出登录</button>
        <details class="account-settings account-danger">
          <summary>停用账号</summary>
          <p>停用后将退出所有设备，无法再登录。旅行数据保留，但需要管理员协助恢复访问。</p>
          <button
            v-if="!state.deactivateConfirm"
            class="text-button danger"
            @click="auth.beginDeactivate()"
          >
            停用这个账号</button
          ><template v-else
            ><p>确定停用？此操作无法在页面撤销。</p>
            <button class="btn btn--danger" :disabled="busy" @click="auth.deactivateAccount()">
              确认停用</button
            ><button class="btn btn--ghost" @click="auth.cancelDeactivate()">取消</button></template
          >
          <p v-if="state.deactivateError" class="field__error">{{ state.deactivateError }}</p>
        </details>
      </template>
      <template v-else>
        <p v-if="state.formError" class="field__error" role="alert">{{ state.formError }}</p>
        <p v-if="state.formNotice" class="gentle-note" role="status">{{ state.formNotice }}</p>
        <form
          v-if="state.form === 'login' || state.form === 'register'"
          @submit.prevent="state.form === 'register' ? auth.register() : auth.login()"
        >
          <label class="field"
            >邮箱<input
              v-model="state.email"
              class="field__input"
              type="email"
              autocomplete="email"
              placeholder="你的邮箱地址"
              required
          /></label>
          <label class="field"
            >密码<input
              v-model="state.password"
              class="field__input"
              type="password"
              :autocomplete="state.form === 'login' ? 'current-password' : 'new-password'"
              :placeholder="
                state.form === 'register' ? `至少 ${MIN_PASSWORD_LENGTH} 位` : '输入密码'
              "
              required
          /></label>
          <label v-if="state.form === 'register'" class="field"
            >确认密码<input
              v-model="state.passwordConfirm"
              class="field__input"
              type="password"
              autocomplete="new-password"
              required
          /></label>
          <label v-if="showClaim" class="check"
            ><input v-model="state.claimOnSubmit" type="checkbox" />同时保存这次旅行</label
          >
          <button class="btn btn--primary btn--block" :disabled="busy">
            {{ busy ? '请稍候…' : state.form === 'register' ? '注册并登录' : '登录' }}
          </button>
          <div class="auth-switch">
            <button
              type="button"
              class="text-button"
              @click="auth.setForm(state.form === 'register' ? 'login' : 'register')"
            >
              {{ state.form === 'register' ? '已有账号？登录' : '没有账号？注册' }}</button
            ><button
              v-if="state.form === 'login'"
              type="button"
              class="text-button muted"
              @click="auth.setForm('forgot')"
            >
              忘记密码
            </button>
          </div>
        </form>
        <form v-else-if="state.form === 'forgot'" @submit.prevent="auth.submitForgot()">
          <label class="field"
            >注册邮箱<input
              v-model="state.email"
              class="field__input"
              type="email"
              autocomplete="email"
              required /></label
          ><button class="btn btn--primary btn--block" :disabled="busy">发送重置邮件</button
          ><button type="button" class="text-button" @click="auth.setForm('login')">
            返回登录
          </button>
        </form>
        <form v-else @submit.prevent="auth.submitReset()">
          <p v-if="!state.resetToken" class="gentle-note">请从重置邮件中的链接打开此页面。</p>
          <template v-else
            ><label class="field"
              >新密码<input
                v-model="state.newPassword"
                class="field__input"
                type="password"
                autocomplete="new-password"
                :minlength="MIN_PASSWORD_LENGTH"
                required /></label
            ><label class="field"
              >再次输入新密码<input
                v-model="state.newPasswordConfirm"
                class="field__input"
                type="password"
                autocomplete="new-password"
                required /></label
            ><button class="btn btn--primary btn--block" :disabled="busy">
              保存新密码
            </button></template
          ><button type="button" class="text-button" @click="auth.setForm('forgot')">
            重新获取邮件
          </button>
        </form>
      </template>
    </section>
  </div>
</template>
