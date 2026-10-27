<script setup lang="ts">
import { formatMoney } from '../format'
defineProps<{ value?: string | null }>()
</script>
<template><span class="money" :class="{ unknown: value == null }">{{ formatMoney(value) }}</span></template>
