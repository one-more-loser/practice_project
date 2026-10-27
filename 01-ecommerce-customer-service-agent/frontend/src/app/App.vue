<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElDialog } from 'element-plus'
import ChatPanel from '../features/support/ChatPanel.vue'
import { useAuth } from '../stores/auth'
import { errorText } from '../lib/api'
const auth = useAuth(),
  router = useRouter(),
  chat = ref(false),
  error = ref('')
async function logout() {
  try {
    await auth.logout()
    await router.push('/')
  } catch (e) {
    error.value = errorText(e)
  }
}
async function switchRole(event: Event) {
  try {
    await auth.switchRole((event.target as HTMLSelectElement).value)
    await router.push('/')
  } catch (e) {
    error.value = errorText(e)
  }
}
</script>
<template>
  <a class="skip" href="#content">跳到主要内容</a>
  <div class="notice-bar">模拟生活方式商店 · 余额充值与支付不涉及真实资金</div>
  <header class="site-header">
    <div class="header-inner">
      <RouterLink to="/" class="brand" aria-label="橙日生活首页"
        ><span class="brand-mark">◒</span
        ><span>橙日<span class="brand-small">ORANGE DAYS</span></span></RouterLink
      >
      <nav aria-label="主导航">
        <a v-if="auth.session?.capabilities?.includes('metrics.read')" href="/operations/">经营工作台</a>
        <RouterLink
          v-if="['support', 'supervisor'].includes(auth.session?.active_role || '')"
          to="/workbench"
          >客服工作台</RouterLink
        >
        <RouterLink
          v-if="auth.session && auth.session.active_role !== 'knowledge_admin'"
          to="/service"
          >服务记录</RouterLink
        >
        <RouterLink to="/">发现好物</RouterLink
        ><template v-if="auth.session?.active_role === 'customer'"
          ><RouterLink to="/orders">我的订单</RouterLink
          ><RouterLink to="/wallet">账户余额</RouterLink></template
        ><RouterLink v-if="auth.session?.active_role === 'shop_admin'" to="/shop"
          >店铺工作台</RouterLink
        >
        <RouterLink v-if="auth.session?.active_role === 'knowledge_admin'" to="/knowledge"
          >知识管理</RouterLink
        >
      </nav>
      <div class="account-nav">
        <RouterLink v-if="auth.session?.active_role === 'customer'" to="/cart" class="cart-link"
          >购物袋</RouterLink
        >
        <template v-if="auth.session"
          ><label v-if="auth.session.roles.length > 1" class="role-label"
            >当前角色<select :value="auth.session.active_role" @change="switchRole">
              <option v-for="role in auth.session.roles" :key="role">{{ role }}</option>
            </select></label
          ><span class="account-name">{{ auth.session.subject_id }}</span
          ><button class="text-button" @click="logout">退出</button></template
        ><RouterLink v-else to="/login">登录 / 账户</RouterLink>
      </div>
    </div>
  </header>
  <main id="content" tabindex="-1">
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <RouterView
      :key="
        (auth.session?.subject_id || 'guest') +
        ':' +
        (auth.session?.active_role || 'guest') +
        $route.path
      "
    />
  </main>
  <footer class="site-footer">
    <div>
      <strong>橙日生活</strong>
      <p>日常所需，刚好喜欢。</p>
    </div>
    <p>模拟店铺 · 商品、物流与账户均用于业务演示<br />电脑浏览器服务</p>
  </footer>
  <button class="service-launcher" @click="chat = true">店铺客服</button>
  <ElDialog v-model="chat" title="店铺客服" width="480" destroy-on-close
    ><ChatPanel v-if="chat"
  /></ElDialog>
</template>
