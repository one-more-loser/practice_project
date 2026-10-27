<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { request, mutate, money, errorText } from '../lib/api'
import type { Cart, Quote, Operation, Proposal } from '../lib/types'
import ShippingForm from '../components/ShippingForm.vue'
import ActionConfirm from '../components/ActionConfirm.vue'
const router = useRouter(),
  cart = ref<Cart | null>(null),
  shipping = ref<InstanceType<typeof ShippingForm> | null>(null),
  quote = ref<Quote | null>(null),
  proposal = ref<Proposal | null>(null),
  busy = ref(false),
  error = ref('')
async function load() {
  try {
    cart.value = await request<Cart>('/me/cart')
  } catch (e) {
    error.value = errorText(e)
  }
}
async function quoteOrder() {
  busy.value = true
  error.value = ''
  quote.value = null
  try {
    const input = await mutate<Operation>('/me/shipping-inputs', 'POST', {
      ...shipping.value!.fields,
      purpose: 'checkout',
    })
    quote.value = await mutate<Quote>('/me/quotes', 'POST', {
      cart_item_ids: cart.value!.items.filter((i) => i.selected).map((i) => i.item_id),
      shipping_input_ref: input.business_ref,
    })
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}
async function propose() {
  if (!quote.value) return
  busy.value = true
  error.value = ''
  try {
    proposal.value = await request<Proposal>('/action-proposals', 'POST', {
      action: 'order.create',
      resource_ref: quote.value.quote_id,
      expected_version: quote.value.version,
      parameters: { quote_ref: quote.value.quote_id },
    })
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}
async function done(result: Operation) {
  proposal.value = null
  await router.push('/orders/' + result.business_ref)
}
onMounted(load)
</script>
<template>
  <div class="page-heading">
    <div>
      <span class="eyebrow">ONE STEP CLOSER</span>
      <h1>确认选购</h1>
      <p class="muted">订单创建后请在15分钟内完成余额支付。</p>
    </div>
    <RouterLink to="/cart" class="link">返回购物袋</RouterLink>
  </div>
  <p v-if="error" class="error" role="alert">{{ error }}</p>
  <div v-if="cart?.items.some((i) => i.selected)" class="split">
    <div class="stack">
      <section class="panel">
        <h2>选购明细</h2>
        <div
          v-for="item in cart.items.filter((i) => i.selected)"
          :key="item.item_id"
          class="total-row"
        >
          <span>{{ item.name }} × {{ item.qty }}</span
          ><span>¥{{ money((BigInt(item.price_minor) * BigInt(item.qty)).toString()) }}</span>
        </div>
      </section>
      <form class="panel" @input="quote = null" @submit.prevent="quoteOrder">
        <ShippingForm ref="shipping" :disabled="busy || !!proposal" />
        <div class="form-actions">
          <button :disabled="busy || !!proposal">
            {{ busy ? '正在核对…' : quote ? '重新取得报价' : '核对价格与库存' }}
          </button>
        </div>
      </form>
    </div>
    <aside class="panel summary">
      <h2>订单金额</h2>
      <template v-if="quote"
        ><div class="total-row">
          <span>商品金额</span><span>¥{{ money(quote.goods_minor) }}</span>
        </div>
        <div class="total-row">
          <span>优惠</span><span>¥{{ money(quote.discount_minor) }}</span>
        </div>
        <div class="total-row">
          <span>运费</span><span>¥{{ money(quote.freight_minor) }}</span>
        </div>
        <div class="total-row">
          <span>应付</span><strong>¥{{ money(quote.payable_minor) }}</strong>
        </div>
        <p class="muted">
          报价有效至
          {{
            new Date(quote.expires_at).toLocaleTimeString('zh-CN')
          }}。重新填写地址后，请重新取得报价。
        </p>
        <button class="primary full" :disabled="busy" @click="propose">提交订单</button></template
      >
      <p v-else class="muted">请填写收货信息并取得报价。</p>
    </aside>
  </div>
  <p v-else class="empty">
    没有已选商品。<RouterLink to="/cart" class="link">返回购物袋</RouterLink>
  </p>
  <ActionConfirm :proposal="proposal" title="提交订单" @done="done" @close="proposal = null" />
</template>
