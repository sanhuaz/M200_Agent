<script setup lang="ts">
import { API } from '../services/api'
import type { Confirmation, TaskRow } from '../types/tasks'

defineProps<{
  confirmations: Confirmation[]
  tasks: TaskRow[]
  selectedConfirmationTokens: string[]
  selectedTaskIds: string[]
}>()

const emit = defineEmits<{
  'update:selectedConfirmationTokens': [value: string[]]
  resolve: [token: string, approved: boolean]
  deleteConfirmations: []
  taskSelectionChange: [rows: TaskRow[]]
  deleteTasks: []
  cancelTask: [id: string]
  deleteTaskArtifact: [item: TaskRow]
}>()

function updateConfirmationSelection(value: unknown) {
  emit('update:selectedConfirmationTokens', Array.isArray(value) ? value.map(String) : [])
}

function taskSelectable(item: TaskRow) {
  return ['succeeded', 'failed', 'cancelled'].includes(item.status)
}
</script>

<template>
  <section class="panel stack">
    <div class="panel-heading">
      <div><span class="section-kicker">授权记录</span><h2>确认操作</h2></div>
      <div class="heading-actions"><span class="count-badge">{{ confirmations.length }}</span><el-button size="small" type="danger" plain :disabled="!selectedConfirmationTokens.length" @click="emit('deleteConfirmations')">删除选中</el-button></div>
    </div>
    <p class="hint">待确认请求可以直接删除使其失效；已处理记录也可批量清理。</p>
    <el-checkbox-group :model-value="selectedConfirmationTokens" class="record-list" @update:model-value="updateConfirmationSelection">
      <div v-for="item in confirmations" :key="item.token" class="result record-row">
        <el-checkbox :label="item.token"><code>{{ item.token }}</code></el-checkbox>
        <span>{{ item.action }}</span>
        <el-tag size="small" :type="item.status === 'pending' ? 'warning' : 'info'">{{ item.status === 'pending' ? '待确认' : item.status }}</el-tag>
        <div><el-button v-if="item.status === 'pending'" size="small" type="primary" @click="emit('resolve', item.token, true)">确认</el-button><el-button v-if="item.status === 'pending'" size="small" @click="emit('resolve', item.token, false)">拒绝</el-button></div>
      </div>
    </el-checkbox-group>
    <div v-if="!confirmations.length" class="empty-copy compact">当前没有确认记录。</div>
  </section>
  <section class="panel stack">
    <div class="panel-heading">
      <div><span class="section-kicker">后台任务</span><h2>任务列表</h2></div>
      <div class="heading-actions"><span class="count-badge">{{ tasks.length }}</span><el-button size="small" type="danger" plain :disabled="!selectedTaskIds.length" @click="emit('deleteTasks')">删除选中</el-button></div>
    </div>
    <p class="hint">仅成功、失败、已取消任务可删除；排队中和运行中任务必须先完成或取消。本地下载文件不会因删除记录而删除。</p>
    <div class="table-wrap">
      <el-table :data="tasks" @selection-change="emit('taskSelectionChange', $event)">
        <el-table-column type="selection" width="48" :selectable="taskSelectable" />
        <el-table-column prop="id" label="ID" min-width="210" />
        <el-table-column prop="type" label="类型" min-width="150" />
        <el-table-column prop="status" label="状态" width="110" />
        <el-table-column prop="result.delivery_status" label="QQ 发送" width="120" />
        <el-table-column prop="error" label="错误" min-width="200" />
        <el-table-column label="操作" width="250">
          <template #default="scope">
            <el-button v-if="['queued', 'running'].includes(scope.row.status)" size="small" @click="emit('cancelTask', scope.row.id)">取消</el-button>
            <el-link v-if="scope.row.status === 'succeeded' && !scope.row.result?.artifact_deleted" :href="`${API}/tasks/${scope.row.id}/artifact`" target="_blank" type="primary">下载产物</el-link>
            <el-button v-if="scope.row.status === 'succeeded' && scope.row.type === 'manga_download' && !scope.row.result?.artifact_deleted" size="small" type="danger" plain @click="emit('deleteTaskArtifact', scope.row)">删除本地文件</el-button>
            <el-tag v-if="scope.row.result?.artifact_deleted" type="info">已删除</el-tag>
          </template>
        </el-table-column>
      </el-table>
    </div>
  </section>
</template>
