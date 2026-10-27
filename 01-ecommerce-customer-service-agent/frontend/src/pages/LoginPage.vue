<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuth } from '../stores/auth'
import { errorText } from '../lib/api'
import { loginTarget } from '../lib/login-target'
const auth = useAuth(),
  route = useRoute(),
  router = useRouter(),
  name = ref(''),
  password = ref(''),
  error = ref(''),
  busy = ref(false)
async function login() {
  busy.value = true
  error.value = ''
  try {
    await auth.login(name.value, password.value)
    password.value = ''
    const target = loginTarget(String(route.query.next || ''))
    if (target?.startsWith('/operations/')) {
      window.location.assign(target)
      return
    }
    await router.push(
      target
        ? target
        : auth.session?.active_role === 'shop_admin'
          ? '/shop'
          : '/',
    )
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}
</script>
<template>
  <section class="panel auth-box">
    <span class="eyebrow">WELCOME BACK</span>
    <h1>欢迎回来</h1>
    <p class="muted">登录后查看购物袋、本人订单和账户余额。</p>
    <p v-if="$route.query.forbidden" class="error" role="alert">
      当前角色没有该页面权限，请使用对应账户。
    </p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <form @submit.prevent="login">
      <label
        >账户<input
          v-model="name"
          name="username"
          autocomplete="username"
          required
          maxlength="80" /></label
      ><label
        >密码<input
          v-model="password"
          name="password"
          type="password"
          autocomplete="current-password"
          required
          maxlength="256" /></label
      ><button class="primary" :disabled="busy">{{ busy ? '正在登录…' : '登录' }}</button>
    </form>
    <p class="muted" style="margin-top: 24px">演示账号由店铺负责人提供，页面不展示预置密码。</p>
  </section>
</template>
