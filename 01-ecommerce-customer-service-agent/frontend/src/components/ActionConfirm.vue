<script setup lang="ts">
import { ref, watch, nextTick } from 'vue'
import { ElDialog } from 'element-plus'
import type { Proposal, Operation } from '../lib/types'
import { mutate, errorText, money, ApiError } from '../lib/api'
const props = defineProps<{ proposal: Proposal | null; title: string }>()
const emit = defineEmits<{ done: [result: Operation]; close: [] }>()
const error = ref(''),
  busy = ref(false),
  uncertain = ref(false),
  terminal = ref(false)
watch(
  () => props.proposal,
  () => {
    error.value = ''
    uncertain.value = false
    terminal.value = false
  },
)
async function submit(decision: 'confirm' | 'cancel') {
  if (!props.proposal || busy.value) return
  busy.value = true
  error.value = ''
  try {
    const result = await mutate<Operation>(
      `/confirmations/${props.proposal.confirmation_id}/submit`,
      'POST',
      { decision },
    )
    if (decision === 'confirm') emit('done', result)
    else emit('close')
  } catch (cause) {
    error.value = errorText(cause)
    uncertain.value = !(cause instanceof ApiError) || cause.status === 0 || cause.status >= 500
    terminal.value = !uncertain.value
  } finally {
    busy.value = false
    await nextTick()
  }
}
</script>
<template>
  <ElDialog
    :model-value="!!proposal"
    :title="title"
    append-to-body
    width="480"
    :close-on-click-modal="false"
    :close-on-press-escape="!busy && !uncertain"
    :show-close="!busy && !uncertain"
    @close="emit('close')"
  >
    <p>请核对本次操作，确认后将提交处理。</p>
    <p v-if="proposal?.summary.display_no" class="order-number">
      {{ proposal.summary.display_no }}
    </p>
    <p v-for="(item, index) in proposal?.summary.items" :key="index">
      {{ item.name || item.product_name }} × {{ item.qty }}
    </p>
    <p v-if="proposal?.summary.previous_shipping" class="muted">
      原收货信息：{{ proposal.summary.previous_shipping.address_masked }} ·
      {{ proposal.summary.previous_shipping.phone_masked }}
    </p>
    <p v-if="proposal?.summary.shipping" class="muted">
      收货信息：{{ proposal.summary.shipping.address_masked }} ·
      {{ proposal.summary.shipping.phone_masked }}
    </p>
    <p v-if="proposal?.summary.payable_minor" class="confirm-amount">
      ¥{{ money(proposal.summary.payable_minor) }}
    </p>
    <p v-if="proposal?.summary.goods_minor" class="confirm-amount">
      申请退商品 ¥{{ money(proposal.summary.goods_minor)
      }}<span v-if="proposal.summary.original_freight_minor">
        · 原运费 ¥{{ money(proposal.summary.original_freight_minor) }}</span
      >
    </p>
    <p v-if="proposal?.summary.notice">{{ proposal.summary.notice }}</p>
    <p class="muted">
      确认有效期至 {{ proposal ? new Date(proposal.expires_at).toLocaleTimeString('zh-CN') : '' }}
    </p>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <template #footer
      ><button :disabled="busy || uncertain" @click="terminal ? emit('close') : submit('cancel')">
        {{ terminal ? '关闭并重新核对' : '暂不操作' }}</button
      ><button class="primary" :disabled="busy || terminal" @click="submit('confirm')">
        {{
          busy
            ? '正在核对…'
            : uncertain
              ? '核对并重试'
              : title.startsWith('确认')
                ? title
                : '确认' + title
        }}
      </button></template
    >
  </ElDialog>
</template>
