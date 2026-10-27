<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { request, mutate, money, errorText } from '../lib/api'
import type { Cart, CartItem } from '../lib/types'
import ProductArt from '../components/ProductArt.vue'
const cart = ref<Cart | null>(null),
  error = ref(''),
  busy = ref(false)
const selected = computed(() => cart.value?.items.filter((i) => i.selected) ?? [])
const total = computed(() =>
  selected.value
    .reduce((sum, item) => sum + BigInt(item.price_minor) * BigInt(item.qty), 0n)
    .toString(),
)
async function load() {
  try {
    cart.value = await request<Cart>('/me/cart')
  } catch (e) {
    error.value = errorText(e)
  }
}
async function update(item: CartItem, remove = false) {
  busy.value = true
  error.value = ''
  try {
    await mutate(
      `/me/cart/items/${item.sku_id}` + (remove ? `?expected_version=${item.version}` : ''),
      remove ? 'DELETE' : 'PUT',
      remove
        ? undefined
        : { qty: Number(item.qty), selected: item.selected, expected_version: item.version },
    )
    await load()
  } catch (e) {
    error.value = errorText(e)
    await load()
  } finally {
    busy.value = false
  }
}
onMounted(load)
</script>
<template>
  <div class="page-heading">
    <div>
      <span class="eyebrow">YOUR EVERYDAY FINDS</span>
      <h1>购物袋</h1>
      <p class="muted">下单前会核对价格、库存和收货信息。</p>
    </div>
    <RouterLink to="/" class="link">继续挑选</RouterLink>
  </div>
  <p v-if="error" class="error" role="alert">{{ error }}</p>
  <div v-if="cart?.items.length" class="split">
    <section class="panel">
      <article v-for="item in cart.items" :key="item.item_id" class="list-row">
        <input
          v-model="item.selected"
          :disabled="busy"
          type="checkbox"
          :aria-label="'选择' + item.name"
          @change="update(item)"
        /><ProductArt :kind="item.image_slug" :name="item.name" />
        <div class="grow">
          <h3>{{ item.name }}</h3>
          <p class="muted">{{ Object.values(item.specifications).join(' / ') }}</p>
          <span>¥{{ money(item.price_minor) }}</span>
        </div>
        <label
          ><span class="sr-only">{{ item.name }}数量</span
          ><input
            v-model.number="item.qty"
            :disabled="busy"
            type="number"
            min="1"
            step="1"
            class="qty"
            :aria-label="item.name + '数量'"
            @change="update(item)" /></label
        ><button
          class="text-button"
          :disabled="busy"
          :aria-label="'移除' + item.name"
          @click="update(item, true)"
        >
          移除
        </button>
      </article>
    </section>
    <aside class="panel summary">
      <h2>本次选购</h2>
      <div class="total-row">
        <span>已选规格</span><span>{{ selected.length }} 种</span>
      </div>
      <div class="total-row">
        <span>商品预估</span><strong>¥{{ money(total) }}</strong>
      </div>
      <p class="muted">实际应付以结算报价为准。当前模拟政策包邮。</p>
      <RouterLink v-if="selected.length && !busy" to="/checkout" class="button primary full"
        >去结算</RouterLink
      ><button v-else class="primary full" disabled>请选择商品</button>
    </aside>
  </div>
  <div v-else class="empty">
    <h2>购物袋还是空的</h2>
    <RouterLink to="/" class="link">去发现喜欢的好物</RouterLink>
  </div>
</template>
