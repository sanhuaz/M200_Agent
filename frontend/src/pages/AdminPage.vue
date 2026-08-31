<script setup lang="ts">
import type { AdminRow } from '../types/management'

defineProps<{ admins: AdminRow[]; qq: string; name: string }>()
const emit = defineEmits<{
  'update:qq': [value: string]
  'update:name': [value: string]
  add: []
  remove: [item: AdminRow]
}>()
</script>

<template>
  <section class="panel stack">
    <div class="panel-heading">
      <div><span class="section-kicker">权限</span><h2>QQ 管理员</h2></div>
      <span class="count-badge">{{ admins.length }}</span>
    </div>
    <p class="hint">本机管理员（内部标识 local-owner）永久存在且不可删除；这里的变更会立即生效。</p>
    <div class="form-row">
      <el-input :model-value="qq" placeholder="QQ 号" @update:model-value="emit('update:qq', String($event))" />
      <el-input :model-value="name" placeholder="备注（可选）" @update:model-value="emit('update:name', String($event))" />
      <el-button type="primary" @click="emit('add')">添加管理员</el-button>
    </div>
    <div class="table-wrap">
      <el-table :data="admins">
        <el-table-column label="身份" min-width="180">
          <template #default="scope">
            {{ scope.row.external_id === 'local-owner' ? '本机管理员（内部标识 local-owner）' : scope.row.external_id }}
          </template>
        </el-table-column>
        <el-table-column prop="display_name" label="备注" min-width="180" />
        <el-table-column prop="platform" label="平台" width="130" />
        <el-table-column label="操作" width="120">
          <template #default="scope">
            <el-button
              size="small"
              type="danger"
              plain
              :disabled="scope.row.external_id === 'local-owner'"
              @click="emit('remove', scope.row)"
            >
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>
  </section>
</template>
