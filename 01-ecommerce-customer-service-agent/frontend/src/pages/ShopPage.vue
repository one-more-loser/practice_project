<script setup lang="ts">
import { onMounted, ref, computed, reactive } from 'vue'
import { ElTable, ElTableColumn, ElDialog } from 'element-plus'
import { request, mutate, money, yuanToMinor, errorText } from '../lib/api'
import type { Product, Sku, Order } from '../lib/types'
const products = ref<Product[]>([]),
  orders = ref<Order[]>([]),
  error = ref(''),
  message = ref(''),
  busy = ref(false),
  tab = ref('orders'),
  chosen = ref<Order | null>(null),
  edit = ref<Product | null>(null),
  newProduct = ref(false),
  offset = ref(0)
const price = reactive<Record<string, string>>({}),
  delta = reactive<Record<string, number>>({})
const fields = reactive({
  name: '',
  description: '',
  category: '',
  specification: '默认款',
  price: '',
  status: 'active',
})
const rows = computed(() =>
  products.value.flatMap((p) => p.skus.map((s) => ({ ...s, product: p }))),
)
async function load() {
  busy.value = true
  error.value = ''
  try {
    ;[products.value, orders.value] = await Promise.all([
      request<{ items: Product[] }>('/shop/products').then((r) => r.items),
      request<{ items: Order[] }>(`/shop/orders?offset=${offset.value}`).then((r) => r.items),
    ])
    for (const p of products.value)
      for (const s of p.skus) {
        price[s.sku_id] = money(s.price_minor)
        delta[s.sku_id] = 0
      }
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}
async function run(work: () => Promise<unknown>, success: string) {
  busy.value = true
  error.value = ''
  message.value = ''
  try {
    await work()
    await load()
    message.value = success
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}
function savePrice(s: Sku) {
  return run(
    () =>
      mutate(`/shop/skus/${s.sku_id}`, 'PATCH', {
        price_minor: yuanToMinor(price[s.sku_id]),
        expected_version: s.version,
      }),
    '售价已更新，新报价采用当前售价。',
  )
}
function stock(s: Sku) {
  return run(
    () =>
      mutate(`/shop/skus/${s.sku_id}/inventory-adjustments`, 'POST', {
        delta: Number(delta[s.sku_id]),
        expected_version: s.inventory_version,
        reason: '店铺工作台模拟盘点',
      }),
    '库存调整已记录。',
  )
}
function toggle(s: Sku) {
  return run(
    () =>
      mutate(`/shop/skus/${s.sku_id}`, 'PATCH', {
        status: s.status === 'active' ? 'inactive' : 'active',
        expected_version: s.version,
      }),
    '规格状态已更新。',
  )
}
function openEdit(product?: Product) {
  edit.value = product ?? null
  newProduct.value = !product
  Object.assign(
    fields,
    product
      ? {
          name: product.name,
          description: product.description,
          category: product.category,
          status: product.status,
        }
      : {
          name: '',
          description: '',
          category: '',
          status: 'active',
          specification: '默认款',
          price: '',
        },
  )
}
async function save() {
  await run(async () => {
    if (edit.value)
      await mutate(`/shop/products/${edit.value.product_id}`, 'PATCH', {
        name: fields.name,
        description: fields.description,
        category: fields.category,
        status: fields.status,
        expected_version: edit.value.version,
      })
    else
      await mutate('/shop/products', 'POST', {
        name: fields.name,
        description: fields.description,
        category: fields.category,
        specifications: { 款式: fields.specification },
        price_minor: yuanToMinor(fields.price),
      })
    edit.value = null
    newProduct.value = false
  }, '商品资料已保存。新建商品需调整库存后可售。')
}
async function inspect(row: Order) {
  await run(async () => {
    chosen.value = await request<Order>('/shop/orders/' + row.order_id)
  }, '')
}
async function ship() {
  if (!chosen.value) return
  const row = chosen.value
  await run(async () => {
    await mutate(`/shop/orders/${row.order_id}/shipments`, 'POST', {
      expected_version: row.version,
      quantities: Object.fromEntries(row.items.map((i) => [i.order_item_id, i.ordered_qty])),
    })
    chosen.value = await request<Order>('/shop/orders/' + row.order_id)
  }, '整单发货成功，订单进入运输中。')
}
async function page(step: number) {
  offset.value += step
  await load()
}
async function carrierSigned() {
  const row = chosen.value,
    shipment = row?.shipments[0]
  if (!row || !shipment) return
  await run(async () => {
    await mutate(
      `/shop/orders/${row.order_id}/shipments/${shipment.shipment_id}/tracking`,
      'POST',
      { status: 'carrier_signed', expected_version: shipment.version },
    )
    chosen.value = await request<Order>('/shop/orders/' + row.order_id)
  }, '已记录承运签收，等待顾客本人确认收货。')
}
onMounted(load)
function closeEditor() {
  edit.value = null
  newProduct.value = false
}
</script>
<template>
  <div class="page-heading">
    <div>
      <span class="eyebrow">STORE WORKSPACE</span>
      <h1>店铺工作台</h1>
      <p class="muted">维护商品和库存，按订单记录完成整单发货。</p>
    </div>
    <button :disabled="busy" @click="load">刷新数据</button>
  </div>
  <div class="admin-toolbar">
    <button :class="{ primary: tab === 'orders' }" @click="tab = 'orders'">订单履约</button
    ><button :class="{ primary: tab === 'products' }" @click="tab = 'products'">商品与库存</button>
  </div>
  <p v-if="error" class="error" role="alert">{{ error }}</p>
  <p v-if="message" class="success" role="status">{{ message }}</p>
  <section v-if="tab === 'orders'" class="panel">
    <h2>本店订单</h2>
    <ElTable :data="orders" empty-text="暂无订单"
      ><ElTableColumn prop="display_no" label="订单号" min-width="190" /><ElTableColumn
        label="应付 / 已支付"
        min-width="150"
        ><template #default="{ row }"
          >¥{{ money(row.payable_minor) }} / ¥{{ money(row.paid_minor) }}</template
        ></ElTableColumn
      ><ElTableColumn label="履约状态" min-width="140"
        ><template #default="{ row }">{{
          row.fulfillment_status === 'unshipped'
            ? '待发货'
            : row.fulfillment_status === 'in_transit'
              ? '运输中'
              : '本人已收货'
        }}</template></ElTableColumn
      ><ElTableColumn label="收货地区" min-width="200"
        ><template #default="{ row }"
          >{{ row.shipping.address_masked }}<br />{{ row.shipping.phone_masked }}</template
        ></ElTableColumn
      ><ElTableColumn label="处理" width="100"
        ><template #default="{ row }"
          ><button class="text-button" :disabled="busy" @click="inspect(row as Order)">
            查看订单
          </button></template
        ></ElTableColumn
      ></ElTable
    >
    <div class="actions form-actions">
      <button :disabled="busy || offset === 0" @click="page(-20)">上一页</button
      ><span class="muted">第{{ offset / 20 + 1 }}页</span
      ><button :disabled="busy || orders.length < 20" @click="page(20)">下一页</button>
    </div>
  </section>
  <section v-else class="panel">
    <div class="page-heading" style="margin: 0 0 24px">
      <h2>商品、售价与可售库存</h2>
      <button class="primary" :disabled="busy" @click="openEdit()">新增商品</button>
    </div>
    <div class="table-wrap">
      <ElTable :data="rows" empty-text="暂无商品" min-width="1100"
        ><ElTableColumn label="商品 / 规格" min-width="180"
          ><template #default="{ row }"
            ><strong>{{ row.product.name }}</strong>
            <p class="muted">{{ Object.values(row.specifications).join(' / ') }}</p>
            <button class="text-button" :disabled="busy" @click="openEdit(row.product)">
              编辑商品
            </button></template
          ></ElTableColumn
        ><ElTableColumn label="售价（元）" min-width="160"
          ><template #default="{ row }"
            ><input
              v-model="price[row.sku_id]"
              :aria-label="row.sku_id + '售价'"
              :disabled="busy"
              inputmode="decimal"
            /><button class="text-button" :disabled="busy" @click="savePrice(row as Sku)">
              保存售价
            </button></template
          ></ElTableColumn
        ><ElTableColumn label="库存 / 预留 / 可售" min-width="150"
          ><template #default="{ row }"
            >{{ row.stock_qty }} / {{ row.reserved_qty }} / {{ row.available_qty }}</template
          ></ElTableColumn
        ><ElTableColumn label="库存变更量" min-width="160"
          ><template #default="{ row }"
            ><input
              v-model.number="delta[row.sku_id]"
              :aria-label="row.sku_id + '库存变更量'"
              :disabled="busy"
              type="number"
              step="1"
            /><button class="text-button" :disabled="busy" @click="stock(row as Sku)">
              记录调整
            </button></template
          ></ElTableColumn
        ><ElTableColumn label="状态" min-width="120"
          ><template #default="{ row }"
            ><span>{{ row.status === 'active' ? '已上架' : '已下架' }}</span
            ><button class="text-button" :disabled="busy" @click="toggle(row as Sku)">
              {{ row.status === 'active' ? '下架' : '上架' }}
            </button></template
          ></ElTableColumn
        ></ElTable
      >
    </div>
  </section>
  <ElDialog
    :model-value="!!chosen"
    title="核对订单并发货"
    width="640"
    :close-on-click-modal="false"
    :show-close="!busy"
    @close="chosen = null"
    ><template v-if="chosen"
      ><p class="order-number">{{ chosen.display_no }}</p>
      <p>{{ chosen.shipping.address_masked }} · {{ chosen.shipping.phone_masked }}</p>
      <p v-for="item in chosen.items" :key="item.order_item_id">
        {{ item.product_name }} × {{ item.ordered_qty }}
      </p>
      <p class="muted">发货后禁止改址。此处只支持整单发货，物流记录由模拟服务生成。</p>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
      <p v-if="!chosen.allowed_actions.ship" class="muted">当前状态不可发货。</p>
      <button
        v-if="chosen.shipments[0]?.status === 'in_transit'"
        :disabled="busy"
        @click="carrierSigned"
      >
        记录承运签收
      </button>
      <p v-if="chosen.shipments[0]?.status === 'carrier_signed'" class="muted">
        承运方已签收，该状态不代替顾客本人确认收货。
      </p></template
    ><template #footer
      ><button :disabled="busy" @click="chosen = null">关闭</button
      ><button class="primary" :disabled="busy || !chosen?.allowed_actions.ship" @click="ship">
        {{ busy ? '正在核对…' : '确认整单发货' }}
      </button></template
    ></ElDialog
  >
  <ElDialog
    :model-value="!!edit || newProduct"
    :title="edit ? '编辑商品' : '新增商品'"
    width="640"
    :close-on-click-modal="false"
    :show-close="!busy"
    @close="closeEditor"
    ><form @submit.prevent="save">
      <div class="shipping-form">
        <label
          >商品名称<input v-model="fields.name" required maxlength="160" :disabled="busy" /></label
        ><label
          >分类<input v-model="fields.category" required maxlength="80" :disabled="busy" /></label
        ><label class="wide"
          >商品描述<textarea
            v-model="fields.description"
            required
            maxlength="2000"
            :disabled="busy"
          /></label
        ><template v-if="newProduct"
          ><label>首个规格<input v-model="fields.specification" required :disabled="busy" /></label
          ><label
            >售价（元）<input
              v-model="fields.price"
              inputmode="decimal"
              required
              :disabled="busy" /></label></template
        ><label v-else
          >商品状态<select v-model="fields.status" :disabled="busy">
            <option value="active">上架</option>
            <option value="inactive">下架</option>
            <option value="draft">草稿</option>
          </select></label
        >
      </div>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
      <div class="actions form-actions">
        <button class="primary" :disabled="busy">{{ busy ? '正在保存…' : '保存商品' }}</button>
      </div>
    </form></ElDialog
  >
</template>
