<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElTable, ElTableColumn } from 'element-plus'
import { request, mutate, money, yuanToMinor, errorText } from '../lib/api'
import type { Wallet } from '../lib/types'
interface Entry {
  entry_id: string
  kind: string
  amount_minor: string
  balance_after_minor: string
  occurred_at: string
  sequence: number
}
const wallet = ref<Wallet | null>(null),
  entries = ref<Entry[]>([]),
  amount = ref(''),
  error = ref(''),
  message = ref(''),
  busy = ref(false),
  offset = ref(0)
const names: Record<string, string> = {
  initialization: '开户',
  topup: '模拟充值',
  payment: '购物支付',
  refund: '退款',
}
async function load() {
  try {
    ;[wallet.value, entries.value] = await Promise.all([
      request<Wallet>('/me/wallet'),
      request<{ items: Entry[] }>(`/me/wallet/entries?offset=${offset.value}`).then((r) => r.items),
    ])
  } catch (e) {
    error.value = errorText(e)
  }
}
async function topup() {
  busy.value = true
  error.value = ''
  message.value = ''
  try {
    await mutate('/me/topups', 'POST', { currency: 'CNY', amount_minor: yuanToMinor(amount.value) })
    amount.value = ''
    offset.value = 0
    await load()
    message.value = '模拟充值已入账，可用于店内购物。'
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}
async function page(delta: number) {
  offset.value += delta
  await load()
}
onMounted(load)
</script>
<template>
  <div class="page-heading">
    <div>
      <span class="eyebrow">YOUR ACCOUNT</span>
      <h1>账户余额</h1>
      <p class="muted">模拟店内支付账户，不涉及真实资金。</p>
    </div>
    <RouterLink to="/orders" class="link">查看订单</RouterLink>
  </div>
  <p v-if="error" class="error" role="alert">{{ error }}</p>
  <p v-if="message" class="success" role="status">{{ message }}</p>
  <div class="split">
    <section class="panel">
      <span class="muted">可用余额 · 人民币</span>
      <p class="wallet-amount" data-testid="wallet-balance">
        {{ wallet ? '¥' + money(wallet.balance_minor) : '正在读取…' }}
      </p>
      <p class="muted">购物从余额扣款，获准退款回到本人账户。<br />暂不支持提现和转账。</p>
    </section>
    <form class="panel" @submit.prevent="topup">
      <h2>模拟充值</h2>
      <label
        >充值金额（元）<input
          v-model="amount"
          :disabled="busy"
          required
          inputmode="decimal"
          placeholder="如 100.00"
          aria-label="充值金额"
      /></label>
      <div class="actions form-actions">
        <button
          v-for="value in ['20', '50', '100']"
          :key="value"
          type="button"
          :disabled="busy"
          @click="amount = value"
        >
          ¥{{ value }}
        </button>
      </div>
      <p class="muted" style="margin-top: 20px">点击后增加演示余额，不会发起外部支付。</p>
      <button class="primary full" :disabled="busy">
        {{ busy ? '正在核对…' : '确认模拟充值' }}
      </button>
    </form>
  </div>
  <section class="panel" style="margin-top: 32px">
    <h2>账户流水</h2>
    <ElTable :data="entries" empty-text="暂无流水"
      ><ElTableColumn label="时间" min-width="190"
        ><template #default="{ row }">{{
          new Date(row.occurred_at).toLocaleString('zh-CN')
        }}</template></ElTableColumn
      ><ElTableColumn label="类型"
        ><template #default="{ row }">{{ names[row.kind] || row.kind }}</template></ElTableColumn
      ><ElTableColumn label="变动金额"
        ><template #default="{ row }"
          ><span class="ledger-amount"
            >{{ row.kind === 'payment' ? '-' : '+' }}¥{{ money(row.amount_minor) }}</span
          ></template
        ></ElTableColumn
      ><ElTableColumn label="变动后余额"
        ><template #default="{ row }"
          >¥{{ money(row.balance_after_minor) }}</template
        ></ElTableColumn
      ></ElTable
    >
    <div class="actions form-actions">
      <button :disabled="offset === 0" @click="page(-20)">上一页</button
      ><span class="muted">第{{ offset / 20 + 1 }}页</span
      ><button :disabled="entries.length < 20" @click="page(20)">下一页</button>
    </div>
  </section>
</template>
