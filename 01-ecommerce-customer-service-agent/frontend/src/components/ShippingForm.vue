<script setup lang="ts">
import { reactive } from 'vue'
const props = defineProps<{ disabled?: boolean }>()
const fields = reactive({
  recipient: '',
  phone: '',
  province: '',
  city: '',
  district: '',
  street: '',
  postal_code: '',
})
defineExpose({ fields })
</script>
<template>
  <fieldset :disabled="props.disabled" class="shipping-form">
    <legend>收货信息</legend>
    <label
      >收件人<input
        v-model="fields.recipient"
        name="recipient"
        required
        maxlength="100"
        autocomplete="name"
    /></label>
    <label
      >手机号<input
        v-model="fields.phone"
        name="phone"
        required
        pattern="1[3-9][0-9]{9}"
        inputmode="tel"
        maxlength="11"
        autocomplete="tel"
    /></label>
    <label
      >省 / 直辖市<input v-model="fields.province" required placeholder="如：浙江省" maxlength="80"
    /></label>
    <label
      >市<input v-model="fields.city" required placeholder="如：杭州市" maxlength="80"
    /></label>
    <label>区 / 县<input v-model="fields.district" required maxlength="80" /></label>
    <label
      >邮政编码（选填）<input
        v-model="fields.postal_code"
        maxlength="16"
        autocomplete="postal-code"
    /></label>
    <label class="wide"
      >详细地址<input
        v-model="fields.street"
        required
        maxlength="300"
        autocomplete="street-address"
    /></label>
  </fieldset>
</template>
