<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElDialog } from 'element-plus'
import { request, mutate, money, errorText } from '../lib/api'
import type { Order, Operation, Proposal } from '../lib/types'
import ActionConfirm from '../components/ActionConfirm.vue'
import ShippingForm from '../components/ShippingForm.vue'
const route = useRoute(),
  rows = ref<Order[]>([]),
  detail = ref<Order | null>(null),
  error = ref(''),
  busy = ref(false),
  proposal = ref<Proposal | null>(null),
  title = ref(''),
  addressOpen = ref(false),
  shipping = ref<InstanceType<typeof ShippingForm> | null>(null),
  offset = ref(0)
const serviceOpen = ref(false),
  reason = ref('ordinary'),
  requestFreight = ref(false),
  description = ref(''),
  quantities = ref<Record<string, number>>({}),
  transportKind = ref('inquiry')
function openService() {
  quantities.value = Object.fromEntries(
    (detail.value?.items || []).map((item) => [item.order_item_id, 0]),
  )
  serviceOpen.value = true
}
async function service() {
  if (!detail.value) return
  busy.value = true
  error.value = ''
  try {
    const formal = detail.value.allowed_actions.aftersale
    proposal.value = await request<Proposal>('/action-proposals', 'POST', {
      action: formal ? 'aftersale.apply' : 'transport.incident.create',
      resource_ref: detail.value.order_id,
      expected_version: detail.value.version,
      parameters: formal
        ? {
            quantities: Object.fromEntries(
              Object.entries(quantities.value).filter(([, qty]) => qty > 0),
            ),
            reason: reason.value,
            description: description.value,
            request_original_freight: reason.value !== 'ordinary' && requestFreight.value,
          }
        : { kind: transportKind.value, description: description.value },
    })
    title.value = formal ? '退货退款申请' : '运输异常工单'
    serviceOpen.value = false
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}
const statuses: Record<string, string> = {
  awaiting_payment: '待付款',
  paid: '已付款',
  completed: '已完成',
  cancelled_unpaid: '已取消',
  cancelled_refunded: '已取消并退款',
  closed_refunded: '已关闭并退款',
  unshipped: '待发货',
  in_transit: '运输中',
  received_by_customer: '本人已确认收货',
}
async function load() {
  busy.value = true
  try {
    if (route.params.id) detail.value = await request<Order>('/me/orders/' + route.params.id)
    else rows.value = (await request<{ items: Order[] }>(`/me/orders?offset=${offset.value}`)).items
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}
async function propose(action: string, label: string, parameters: Record<string, string> = {}) {
  if (!detail.value) return
  busy.value = true
  error.value = ''
  try {
    proposal.value = await request<Proposal>('/action-proposals', 'POST', {
      action,
      resource_ref: detail.value.order_id,
      expected_version: detail.value.version,
      parameters,
    })
    title.value = label
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}
async function address() {
  busy.value = true
  error.value = ''
  try {
    const input = await mutate<Operation>('/me/shipping-inputs', 'POST', {
      ...shipping.value!.fields,
      purpose: 'address_change',
    })
    await propose('order.address.change', '修改地址', { shipping_input_ref: input.business_ref })
    if (proposal.value) addressOpen.value = false
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}
async function done() {
  proposal.value = null
  await load()
}
async function page(delta: number) {
  offset.value += delta
  await load()
}
function refresh() {
  error.value = ''
  return load()
}
onMounted(load)
</script>
<template>
  <div class="page-heading">
    <div>
      <span class="eyebrow">YOUR ORDERS</span>
      <h1>{{ detail ? '订单详情' : '我的订单' }}</h1>
      <p class="muted">订单、付款和物流状态以店铺记录为准。</p>
    </div>
    <RouterLink :to="detail ? '/orders' : '/'" class="link">{{
      detail ? '返回订单列表' : '继续挑选'
    }}</RouterLink>
  </div>
  <p v-if="error" class="error" role="alert">
    {{ error }}
    <button :disabled="busy" @click="refresh">刷新</button>
  </p>
  <template v-if="detail"
    ><section class="panel" style="margin-bottom: 24px">
      <div class="page-heading" style="margin: 0">
        <div>
          <span class="badge">{{ statuses[detail.trade_status] }}</span>
          <span class="badge">{{ statuses[detail.fulfillment_status] }}</span>
          <p class="order-number" style="margin: 16px 0 0">{{ detail.display_no }}</p>
        </div>
        <div class="actions">
          <button
            v-if="detail.allowed_actions.pay"
            class="primary"
            :disabled="busy"
            @click="propose('order.pay', '余额支付')"
          >
            余额支付 ¥{{ money(detail.payable_minor) }}</button
          ><button
            v-if="detail.allowed_actions.cancel"
            :disabled="busy"
            @click="propose('order.cancel', '取消订单')"
          >
            取消订单</button
          ><button
            v-if="detail.allowed_actions.confirm_receipt"
            class="primary"
            :disabled="busy"
            @click="propose('order.receipt.confirm', '确认收货')"
          >
            确认收货
          </button>
        </div>
      </div>
      <p v-if="detail.allowed_actions.pay" class="muted" style="margin-top: 16px">
        付款截止 {{ new Date(detail.payment_expires_at).toLocaleString('zh-CN') }} ·
        <RouterLink to="/wallet" class="link">余额不足可模拟充值</RouterLink>
      </p>
      <p
        v-if="detail.cancellation_requires_eligibility_review"
        class="muted"
        style="margin-top: 16px"
      >
        已付款取消须经资格审核。<RouterLink to="/service" class="link">查看审核结果</RouterLink>
      </p>
    </section>
    <div class="order-detail-grid">
      <section class="panel">
        <h2>商品与金额</h2>
        <div v-for="item in detail.items" :key="item.order_item_id" class="list-row">
          <div class="grow">
            <h3>{{ item.product_name }} × {{ item.ordered_qty }}</h3>
            <span class="muted">{{ Object.values(item.specifications).join(' / ') }}</span>
          </div>
          <span>¥{{ money(item.allocated_minor) }}</span>
        </div>
        <div class="total-row">
          <span>商品金额</span><span>¥{{ money(detail.goods_minor) }}</span>
        </div>
        <div class="total-row">
          <span>运费</span><span>¥{{ money(detail.freight_minor) }}</span>
        </div>
        <div class="total-row">
          <span>应付</span><strong>¥{{ money(detail.payable_minor) }}</strong>
        </div>
        <div class="total-row">
          <span>已支付</span><span>¥{{ money(detail.paid_minor) }}</span>
        </div>
        <div class="total-row">
          <span>已退款</span><span>¥{{ money(detail.refunded_minor) }}</span>
        </div>
      </section>
      <div class="stack">
        <section class="panel">
          <h2>收货信息</h2>
          <p class="address">
            {{ detail.shipping.recipient }}　{{ detail.shipping.phone }}<br />{{
              detail.shipping.province
            }}{{ detail.shipping.city }}{{ detail.shipping.district }}<br />{{
              detail.shipping.street
            }}
          </p>
          <button
            :disabled="busy || !detail.allowed_actions.change_address"
            @click="addressOpen = true"
          >
            {{ detail.change_count === 1 ? '地址已修改' : '修改收货地址' }}
          </button>
          <p class="muted" style="margin-top: 12px">每单在发货前只可成功改址一次。</p>
        </section>
        <section class="panel">
          <h2>物流记录</h2>
          <template v-for="shipment in detail.shipments" :key="shipment.shipment_id"
            ><p>{{ shipment.carrier }} · {{ shipment.tracking_no }}</p>
            <p v-for="(event, index) in shipment.events" :key="index" class="muted">
              {{ event.description }}<br />{{ new Date(event.occurred_at).toLocaleString('zh-CN') }}
            </p></template
          >
          <p v-if="!detail.shipments.length" class="muted">店铺尚未发货。</p>
          <p v-if="detail.fulfillment_status === 'in_transit'" class="muted">
            请在收到全部商品后由本人确认收货。
          </p>
          <p v-if="detail.allowed_actions.aftersale" class="muted">
            本人收货时间
            {{ new Date(detail.received_at!).toLocaleString('zh-CN') }}。
          </p>
          <button
            v-if="
              detail.allowed_actions.aftersale ||
              (detail.trade_status === 'paid' && detail.fulfillment_status === 'in_transit')
            "
            class="primary"
            :disabled="busy"
            @click="openService"
          >
            {{ detail.allowed_actions.aftersale ? '申请退货退款' : '运输异常 / 未收到货' }}
          </button>
          <RouterLink to="/service" class="link">服务记录</RouterLink>
        </section>
      </div>
    </div></template
  >
  <template v-else
    ><div class="stack">
      <article v-for="row in rows" :key="row.order_id" class="panel">
        <div class="page-heading" style="margin: 0">
          <div>
            <span class="badge">{{ statuses[row.trade_status] }}</span>
            <span class="badge">{{ statuses[row.fulfillment_status] }}</span>
            <p class="order-number" style="margin: 16px 0 8px">{{ row.display_no }}</p>
            <p>
              {{ row.items.map((item) => item.product_name + ' × ' + item.ordered_qty).join('、') }}
            </p>
          </div>
          <div>
            <p class="price">¥{{ money(row.payable_minor) }}</p>
            <RouterLink :to="'/orders/' + row.order_id" class="link">查看订单 →</RouterLink>
          </div>
        </div>
      </article>
      <p v-if="!rows.length" class="empty">暂无订单，去挑选喜欢的商品吧。</p>
    </div>
    <div class="actions form-actions">
      <button :disabled="offset === 0 || busy" @click="page(-20)">上一页</button
      ><span class="muted">第{{ offset / 20 + 1 }}页</span
      ><button :disabled="rows.length < 20 || busy" @click="page(20)">下一页</button>
    </div></template
  >
  <ActionConfirm :proposal="proposal" :title="title" @done="done" @close="proposal = null" />
  <ElDialog
    v-model="serviceOpen"
    :title="detail?.allowed_actions.aftersale ? '退货退款' : '运输异常'"
    width="560"
    :close-on-click-modal="false"
  >
    <form @submit.prevent="service">
      <template v-if="detail?.allowed_actions.aftersale"
        ><label v-for="item in detail.items" :key="item.order_item_id" class="service-field"
          >{{ item.product_name }} · 原实付 ¥{{ money(item.paid_goods_minor)
          }}<input
            v-model.number="quantities[item.order_item_id]"
            type="number"
            min="0"
            :max="item.ordered_qty"
            aria-label="退货数量" /></label
        ><label class="service-field"
          >申请原因<select v-model="reason">
            <option value="ordinary">普通退货</option>
            <option value="quality">质量问题</option>
            <option value="wrong_item">错发</option>
          </select></label
        ><label v-if="reason !== 'ordinary' && detail?.freight_minor !== '0'" class="service-field"
          ><span
            ><input v-model="requestFreight" type="checkbox" />
            同时申请退原订单运费（按适用政策核验）</span
          ></label
        ></template
      ><label v-else class="service-field"
        >诉求<select v-model="transportKind">
          <option value="inquiry">查询运输</option>
          <option value="expedite">催件</option>
          <option value="intercept">拦截</option>
          <option value="lost">疑似丢件</option>
        </select></label
      ><label class="service-field"
        >情况说明<textarea v-model="description" maxlength="1000" rows="3" required />
      </label>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
      <button class="primary" :disabled="busy">核对申请</button>
    </form>
  </ElDialog>
  <ElDialog
    v-model="addressOpen"
    title="修改收货地址"
    width="640"
    :close-on-click-modal="false"
    :close-on-press-escape="!busy"
    :show-close="!busy"
    ><p>本单只可成功修改一次地址。发货后无法修改。</p>
    <form @submit.prevent="address">
      <ShippingForm ref="shipping" :disabled="busy" />
      <p v-if="error" class="error" role="alert">{{ error }}</p>
      <div class="actions form-actions">
        <button type="button" :disabled="busy" @click="addressOpen = false">返回</button
        ><button class="primary" :disabled="busy">{{ busy ? '正在核对…' : '核对新地址' }}</button>
      </div>
    </form></ElDialog
  >
</template>
<style scoped>
.service-field {
  display: grid;
  gap: 8px;
  margin: 16px 0;
}
</style>
