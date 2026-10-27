<script setup lang="ts">
import { computed, useId } from 'vue'
const props = defineProps<{ capabilities: string[]; capability: string; label: string; blockedReason?: string; busy?: boolean; primary?: boolean }>()
const emit = defineEmits<{ activate: [] }>()
const id = useId()
const reason = computed(() => !props.capabilities.includes(props.capability) ? '没有此操作权限' : props.blockedReason ?? '')
</script>
<template>
  <div class="action-gate">
    <button type="button" :class="{ primary }" :disabled="!!reason || busy" :aria-describedby="reason ? id : undefined" :aria-busy="busy" @click="!reason && !busy && emit('activate')"><span v-if="busy" class="spinner" aria-hidden="true"></span>{{ label }}</button>
    <span v-if="reason" :id="id" class="blocked-reason">{{ reason }}</span>
  </div>
</template>
