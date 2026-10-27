<script setup lang="ts">
import { computed, onMounted, ref, reactive } from 'vue'
import { useRouter } from 'vue-router'
import { useAuth } from '../stores/auth'
import { request, mutate, money, errorText } from '../lib/api'
import type { Product, Cart } from '../lib/types'
import ProductArt from '../components/ProductArt.vue'
const auth = useAuth(),
  router = useRouter(),
  products = ref<Product[]>([]),
  error = ref(''),
  message = ref(''),
  loading = ref(false),
  busy = ref(''),
  category = ref('全部好物')
const selected = reactive<Record<string, string>>({}),
  quantities = reactive<Record<string, number>>({})
const categories = computed(() => ['全部好物', ...new Set(products.value.map((p) => p.category))])
const visible = computed(() =>
  products.value.filter((p) => category.value === '全部好物' || p.category === category.value),
)
function sku(product: Product) {
  return product.skus.find((s) => s.sku_id === selected[product.product_id]) ?? product.skus[0]
}
async function load() {
  loading.value = true
  error.value = ''
  try {
    products.value = (await request<{ items: Product[] }>('/products')).items
    for (const p of products.value) {
      selected[p.product_id] = p.skus[0]?.sku_id
      quantities[p.product_id] = 1
    }
  } catch (e) {
    error.value = errorText(e)
  } finally {
    loading.value = false
  }
}
async function add(product: Product) {
  if (!auth.session) {
    await router.push({ path: '/login', query: { next: '/' } })
    return
  }
  error.value = ''
  message.value = ''
  busy.value = product.product_id
  try {
    const cart = await request<Cart>('/me/cart'),
      chosen = sku(product)
    const existing = cart.items.find((i) => i.sku_id === chosen.sku_id)
    await mutate(`/me/cart/items/${chosen.sku_id}`, 'PUT', {
      qty: (existing?.qty ?? 0) + Number(quantities[product.product_id]),
      selected: true,
      expected_version: existing?.version ?? 0,
    })
    message.value = product.name + '已加入购物袋'
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = ''
  }
}
onMounted(load)
</script>
<template>
  <section class="hero">
    <div class="hero-copy">
      <span class="eyebrow">THE EVERYDAY EDIT / 日常精选</span>
      <h1>把日常，<br />过成喜欢的样子。</h1>
      <p class="muted">一只顺手的杯，一束柔和的光。<br />为平凡的每一天，添一点温度。</p>
      <a href="#products" class="link">挑选今日好物 ↗</a>
    </div>
    <div class="hero-art">
      <ProductArt kind="cup" name="日常陶瓷杯插画" /><span class="art-caption"
        >SMALL THINGS. GOOD DAYS.</span
      >
    </div>
  </section>
  <section id="products" aria-label="商品列表">
    <div class="section-heading">
      <div>
        <span class="eyebrow">CURATED FOR YOUR DAY</span>
        <h2>日常好物</h2>
        <span class="muted">当前店铺商品 · 模拟包邮政策</span>
      </div>
      <div class="filters" aria-label="商品分类">
        <button
          v-for="item in categories"
          :key="item"
          :class="{ active: category === item }"
          :aria-pressed="category === item"
          @click="category = item"
        >
          {{ item }}
        </button>
      </div>
    </div>
    <p v-if="loading" role="status">正在取回商品与库存…</p>
    <div v-if="error" class="error" role="alert">
      {{ error }} <button @click="load">刷新商品</button>
    </div>
    <p v-if="message" class="success" role="status">
      {{ message }} <RouterLink to="/cart" class="link">查看购物袋</RouterLink>
    </p>
    <div class="product-grid">
      <article v-for="product in visible" :key="product.product_id" class="product-card">
        <div class="product-image">
          <ProductArt :kind="product.image_slug" :name="product.name + '示意插画'" />
        </div>
        <div class="product-meta">
          <span class="eyebrow">{{ product.category }}</span>
          <h3>{{ product.name }}</h3>
          <p class="product-note">{{ product.description }}</p>
          <template v-if="sku(product)"
            ><div class="price"><small>¥</small>{{ money(sku(product).price_minor) }}</div>
            <label
              ><span class="sr-only">{{ product.name }}规格</span
              ><select v-model="selected[product.product_id]" :aria-label="product.name + '规格'">
                <option v-for="s in product.skus" :key="s.sku_id" :value="s.sku_id">
                  {{ Object.values(s.specifications).join(' / ') }}
                </option>
              </select></label
            >
            <div class="product-actions">
              <input
                v-model.number="quantities[product.product_id]"
                type="number"
                min="1"
                step="1"
                class="qty"
                :aria-label="product.name + '数量'"
              /><button
                :disabled="
                  busy !== '' ||
                  sku(product).available_qty < 1 ||
                  (!!auth.session && auth.session.active_role !== 'customer')
                "
                @click="add(product)"
              >
                {{
                  busy === product.product_id
                    ? '正在加入…'
                    : sku(product).available_qty < 1
                      ? '暂时售罄'
                      : '加入购物袋'
                }}
              </button>
            </div>
            <p class="muted">可售 {{ sku(product).available_qty }} 件</p></template
          >
          <p v-else class="muted">暂无可售规格</p>
        </div>
      </article>
    </div>
    <p v-if="!loading && !error && !visible.length" class="empty">当前分类暂无商品</p>
  </section>
</template>
