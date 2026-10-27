<script setup lang="ts">
import { ref } from 'vue'
import { ElButton, ElCard } from 'element-plus'

const message = ref('尚未检查')
const loading = ref(false)
async function check() {
  loading.value = true
  try {
    const response = await fetch('/health/ready', { credentials: 'same-origin' })
    const result = await response.json()
    message.value =
      response.ok && result.data?.database === 'ready'
        ? '数据库基础可用；客服模型与业务功能待后续批次验证'
        : '数据库尚未就绪；请检查本项目本地环境'
  } catch {
    message.value = '服务连接失败，请稍后重试'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <el-card>
    <p>模拟店铺 · B0 基础验证</p>
    <h1>店铺服务</h1>
    <p>本页用于检查工程入口。商城、嵌入式客服和员工工作台按开发计划分批接入。</p>
    <el-button type="primary" :loading="loading" @click="check">检查服务状态</el-button>
    <p role="status" aria-live="polite">{{ message }}</p>
  </el-card>
</template>
