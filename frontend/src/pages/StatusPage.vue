<script setup lang="ts">
import {
  ChatDotRound,
  Collection,
  DataAnalysis,
  MagicStick,
  Monitor,
  Setting,
} from '@element-plus/icons-vue'
import type { HealthData } from '../types/management'

defineProps<{ health: HealthData }>()

const labels: Record<string, string> = {
  ok: '正常',
  degraded: '降级',
  connected: '已连接',
  unavailable: '不可用',
  installed: '已安装',
  running: '运行中',
  stopped: '已停止',
  configured_disconnected: '已配置未连接',
  needs_configuration: '需要配置',
  enabled: '已启用',
  disabled: '已停用',
  online: 'QQ 在线',
  offline: 'QQ 离线',
  not_configured: '未配置',
  connecting: '连接中',
  auth_failed: '认证失败',
  disconnected: '未连接',
  error: '错误',
  unknown: '未知',
}

function statusLabel(value: unknown) {
  const key = String(value || 'unknown')
  return labels[key] || key
}

function statusType(value: unknown): 'success' | 'warning' | 'danger' {
  const key = String(value || 'unknown')
  if (['ok', 'connected', 'running', 'enabled', 'online', 'installed'].includes(key)) return 'success'
  if (['degraded', 'configured_disconnected', 'connecting', 'unknown'].includes(key)) return 'warning'
  return 'danger'
}
</script>

<template>
  <div class="status-grid">
    <article class="status-card"><span>服务状态</span><el-icon><Monitor /></el-icon><strong>{{ statusLabel(health.status) }}</strong><el-tag :type="statusType(health.status)">{{ health.status || 'loading' }}</el-tag></article>
    <article class="status-card"><span>数据库</span><el-icon><DataAnalysis /></el-icon><strong>{{ statusLabel(health.database) }}</strong><el-tag :type="statusType(health.database)">{{ health.database || 'unknown' }}</el-tag></article>
    <article class="status-card"><span>向量索引</span><el-icon><Collection /></el-icon><strong>{{ statusLabel(health.chroma) }}</strong><el-tag :type="statusType(health.chroma)">{{ health.chroma || 'unknown' }}</el-tag></article>
    <article class="status-card"><span>任务 Worker</span><el-icon><Setting /></el-icon><strong>{{ statusLabel(health.worker) }}</strong><el-tag :type="statusType(health.worker)">{{ health.worker || 'unknown' }}</el-tag></article>
    <article class="status-card"><span>OneBot</span><el-icon><ChatDotRound /></el-icon><strong>{{ statusLabel(health.onebot) }}</strong><el-tag :type="statusType(health.onebot)">{{ health.onebot || 'unknown' }}</el-tag></article>
    <article class="status-card"><span>Reranker</span><el-icon><MagicStick /></el-icon><strong>{{ statusLabel(health.reranker) }}</strong><el-tag :type="statusType(health.reranker)">{{ health.reranker || 'unknown' }}</el-tag></article>
  </div>
  <div class="two-column status-detail-grid">
    <section class="panel stack">
      <div class="panel-heading"><div><span class="section-kicker">模型</span><h2>模型配置</h2></div></div>
      <div class="card-list"><div v-for="item in health.models || []" :key="item.alias" class="result"><span><strong>{{ item.alias }}</strong><small>{{ item.model }}</small></span><el-tag :type="item.configured ? 'success' : 'warning'">{{ item.configured ? '已配置' : '未配置' }}</el-tag></div></div>
    </section>
    <section class="panel stack">
      <div class="panel-heading"><div><span class="section-kicker">检索</span><h2>Embedding 配置</h2></div></div>
      <div class="card-list"><div v-for="item in health.embedding_profiles || []" :key="item.alias" class="result"><span><strong>{{ item.alias }}</strong><small>{{ item.model }}</small></span><el-tag :type="item.configured ? 'success' : 'warning'">{{ item.configured ? '已配置' : '未配置' }}</el-tag></div></div>
    </section>
  </div>
  <section class="panel raw-status">
    <el-collapse><el-collapse-item title="查看原始运行详情" name="raw"><pre>{{ JSON.stringify(health, null, 2) }}</pre></el-collapse-item></el-collapse>
  </section>
</template>
