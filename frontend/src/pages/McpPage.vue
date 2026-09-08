<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CircleCheck, Connection, Delete, Plus, Refresh, VideoPause, VideoPlay } from '@element-plus/icons-vue'
import {
  createMcpServer,
  deleteMcpServer,
  getMcpCatalog,
  getMcpGlobalIntents,
  getMcpServerIntents,
  installAnySearch,
  listMcpPresets,
  listMcpServers,
  policyLabels,
  refreshMcpServer,
  serverToDraft,
  setMcpGrants,
  setMcpGlobalIntents,
  setMcpServerIntents,
  setMcpServerEnabled,
  testMcpServer,
  updateMcpServer,
} from '../features/mcp'
import type {
  McpCatalog,
  McpCatalogEntry,
  McpGrantKind,
  McpGlobalIntents,
  McpIntentPhrase,
  McpIntentRule,
  AnySearchAuthMode,
  McpPreset,
  McpServer,
  McpServerDraft,
  McpServerIntents,
  McpTransport,
} from '../types/mcp'

const transports: Array<{ value: McpTransport; label: string }> = [
  { value: 'stdio', label: 'stdio（本机进程）' },
  { value: 'sse', label: 'SSE（HTTP）' },
  { value: 'streamable_http', label: 'Streamable HTTP' },
]

const emptyCatalog = (): McpCatalog => ({ tools: [], resources: [], resource_templates: [], prompts: [] })
const newDraft = (): McpServerDraft => ({
  name: '',
  slug: '',
  transport: 'stdio',
  configText: JSON.stringify({ command: '', args: [] }, null, 2),
  secretValuesText: '{}',
  accessPolicy: 'owner_only',
  privateUsersText: '',
})

const servers = ref<McpServer[]>([])
const presets = ref<McpPreset[]>([])
const selectedId = ref('')
const draft = reactive<McpServerDraft>(newDraft())
const catalog = ref<McpCatalog>(emptyCatalog())
const catalogStatus = ref('')
const loading = ref(false)
const saving = ref(false)
const testing = ref(false)
const refreshing = ref(false)
const togglingId = ref('')
const deletingId = ref('')
const catalogLoading = ref(false)
const anySearchDialogVisible = ref(false)
const anySearchMode = ref<AnySearchAuthMode>('anonymous')
const anySearchApiKey = ref('')
const anySearchInstalling = ref(false)
const grantSaving = reactive<Record<McpGrantKind, boolean>>({ tool: false, resource: false, prompt: false })
const grantSelections = reactive<Record<McpGrantKind, string[]>>({ tool: [], resource: [], prompt: [] })
const globalIntents = ref<McpGlobalIntents | null>(null)
const serverIntents = ref<McpServerIntents | null>(null)
const globalIntentLoading = ref(false)
const globalIntentSaving = ref(false)
const serverIntentLoading = ref(false)
const serverIntentSaving = ref(false)
let serverRequest = 0
let catalogRequest = 0
let intentRequest = 0

const selectedServer = computed(() => servers.value.find((item) => item.id === selectedId.value) || null)
const anySearchPreset = computed(() => presets.value.find((item) => item.slug === 'anysearch') || null)
const isEditing = computed(() => Boolean(selectedId.value))
const catalogGroups = computed(() => [
  { kind: 'tool' as const, label: 'Tools', items: catalog.value.tools, hint: '可被 Agent 直接调用' },
  { kind: 'resource' as const, label: 'Resources', items: catalog.value.resources, hint: '按需读取文本或 Artifact' },
  { kind: 'resource' as const, label: 'Resource Templates', items: catalog.value.resource_templates, hint: '按 URI 参数按需读取' },
  { kind: 'prompt' as const, label: 'Prompts', items: catalog.value.prompts, hint: '按名称和参数按需读取' },
])

function statusType(status: string): '' | 'success' | 'warning' | 'danger' | 'info' {
  if (status === 'ready') return 'success'
  if (status === 'error') return 'danger'
  if (status === 'connecting') return 'warning'
  return 'info'
}

function statusLabel(status: string): string {
  return ({ ready: '已连接', error: '连接失败', connecting: '连接中', disabled: '已停用' } as Record<string, string>)[status] || status || '未知'
}

function transportLabel(transport: McpTransport): string {
  return transports.find((item) => item.value === transport)?.label || transport
}

function entryTitle(entry: McpCatalogEntry): string {
  return entry.title || entry.name || entry.uri || entry.uri_template || entry.key
}

function entrySubtitle(entry: McpCatalogEntry): string {
  return [entry.description, entry.uri || entry.uri_template, `稳定键：${entry.key}`]
    .filter(Boolean)
    .join(' · ')
}

function newIntentId(): string {
  return typeof crypto !== 'undefined' && crypto.randomUUID
    ? crypto.randomUUID()
    : `intent-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

function newPhrase(): McpIntentPhrase {
  return { id: newIntentId(), phrase: '', enabled: true }
}

function newGlobalRule(serverId: string): McpIntentRule {
  return { id: newIntentId(), phrase: '', server_id: serverId, tool_key: null, enabled: true }
}

function selectServer(item: McpServer) {
  selectedId.value = item.id
  Object.assign(draft, serverToDraft(item))
  void loadCatalog(item.id)
  void loadServerIntents(item.id)
}

function startNew() {
  selectedId.value = ''
  Object.assign(draft, newDraft())
  catalog.value = emptyCatalog()
  catalogStatus.value = ''
  serverIntents.value = null
  resetGrants()
}

function resetGrants() {
  grantSelections.tool = []
  grantSelections.resource = []
  grantSelections.prompt = []
}

async function loadServers() {
  const request = ++serverRequest
  loading.value = true
  try {
    const result = await listMcpServers()
    if (request !== serverRequest) return
    servers.value = result
    if (selectedId.value && !result.some((item) => item.id === selectedId.value)) startNew()
    if (!selectedId.value && result.length) selectServer(result[0])
  } catch (error) {
    if (request === serverRequest) ElMessage.error((error as Error).message)
  } finally {
    if (request === serverRequest) loading.value = false
  }
}

async function loadPresets() {
  try {
    presets.value = await listMcpPresets()
  } catch (error) {
    ElMessage.error((error as Error).message)
  }
}

async function loadGlobalIntents() {
  globalIntentLoading.value = true
  try {
    globalIntents.value = await getMcpGlobalIntents()
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    globalIntentLoading.value = false
  }
}

async function loadServerIntents(id = selectedId.value) {
  if (!id) {
    serverIntents.value = null
    return
  }
  const request = ++intentRequest
  serverIntentLoading.value = true
  try {
    const result = await getMcpServerIntents(id)
    if (request !== intentRequest || id !== selectedId.value) return
    serverIntents.value = result
  } catch (error) {
    if (request === intentRequest) ElMessage.error((error as Error).message)
  } finally {
    if (request === intentRequest) serverIntentLoading.value = false
  }
}

function addGlobalRule() {
  if (!globalIntents.value || !servers.value.length) {
    ElMessage.warning('请先创建 MCP Server')
    return
  }
  globalIntents.value.custom_rules.push(newGlobalRule(servers.value[0].id))
}

function removeGlobalRule(index: number) {
  globalIntents.value?.custom_rules.splice(index, 1)
}

function setGlobalToolKey(rule: McpIntentRule, value: string | number) {
  const normalized = String(value).trim()
  rule.tool_key = normalized || null
}

async function saveGlobalIntentRules() {
  if (!globalIntents.value) return
  globalIntentSaving.value = true
  try {
    globalIntents.value = await setMcpGlobalIntents(globalIntents.value.custom_rules)
    ElMessage.success('全局 MCP 意图已保存')
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    globalIntentSaving.value = false
  }
}

function addServerPhrase() {
  serverIntents.value?.server_phrases.push(newPhrase())
}

function removeServerPhrase(index: number) {
  serverIntents.value?.server_phrases.splice(index, 1)
}

function addToolPhrase(tool: McpServerIntents['tools'][number]) {
  tool.phrases.push(newPhrase())
}

function removeToolPhrase(tool: McpServerIntents['tools'][number], index: number) {
  tool.phrases.splice(index, 1)
}

async function saveServerIntentRules() {
  if (!serverIntents.value || !selectedId.value) return
  serverIntentSaving.value = true
  try {
    serverIntents.value = await setMcpServerIntents(selectedId.value, {
      server_phrases: serverIntents.value.server_phrases,
      tools: serverIntents.value.tools.map((tool) => ({ item_key: tool.item_key, phrases: tool.phrases })),
    })
    ElMessage.success('Server 与 Tool 意图已保存')
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    serverIntentSaving.value = false
  }
}

function openAnySearchDialog() {
  anySearchMode.value = 'anonymous'
  anySearchApiKey.value = ''
  anySearchDialogVisible.value = true
}

async function installAnySearchPreset() {
  const apiKey = anySearchApiKey.value.trim()
  if (anySearchMode.value === 'api_key' && !apiKey) {
    ElMessage.warning('请输入 AnySearch API Key，或切换为匿名访问')
    return
  }
  anySearchInstalling.value = true
  try {
    const item = await installAnySearch(anySearchMode.value === 'api_key' ? apiKey : undefined)
    updateServerInList(item)
    selectedId.value = item.id
    Object.assign(draft, serverToDraft(item))
    draft.secretValuesText = '{}'
    catalog.value = emptyCatalog()
    catalogStatus.value = item.status
    anySearchDialogVisible.value = false
    anySearchApiKey.value = ''
    await loadPresets()
    await loadCatalog(item.id)
    ElMessage.success('AnySearch 已添加，当前停用且未授权任何工具')
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    anySearchInstalling.value = false
  }
}

async function loadCatalog(id = selectedId.value) {
  if (!id) return
  const request = ++catalogRequest
  catalogLoading.value = true
  try {
    const result = await getMcpCatalog(id)
    if (request !== catalogRequest || id !== selectedId.value) return
    catalog.value = result.catalog
    catalogStatus.value = result.status
    grantSelections.tool = result.catalog.tools.filter((item) => item.allowed).map((item) => item.key)
    grantSelections.resource = [
      ...result.catalog.resources,
      ...result.catalog.resource_templates,
    ].filter((item) => item.allowed).map((item) => item.key)
    grantSelections.prompt = result.catalog.prompts.filter((item) => item.allowed).map((item) => item.key)
  } catch (error) {
    if (request === catalogRequest) ElMessage.error((error as Error).message)
  } finally {
    if (request === catalogRequest) catalogLoading.value = false
  }
}

function updateServerInList(item: McpServer) {
  const index = servers.value.findIndex((current) => current.id === item.id)
  if (index >= 0) servers.value[index] = item
  else servers.value.push(item)
}

async function save() {
  if (!draft.name.trim()) {
    ElMessage.warning('请填写 Server 名称')
    return
  }
  saving.value = true
  try {
    const item = isEditing.value ? await updateMcpServer(selectedId.value, draft) : await createMcpServer(draft)
    updateServerInList(item)
    selectedId.value = item.id
    Object.assign(draft, serverToDraft(item))
    draft.secretValuesText = '{}'
    ElMessage.success(isEditing.value ? 'MCP Server 已保存' : 'MCP Server 已创建，默认停用')
    await loadCatalog(item.id)
    await loadServerIntents(item.id)
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    saving.value = false
  }
}

async function testConnection() {
  if (!selectedId.value) {
    ElMessage.warning('请先保存 Server')
    return
  }
  testing.value = true
  try {
    const result = await testMcpServer(selectedId.value)
    catalog.value = result.catalog
    catalogStatus.value = 'tested'
    ElMessage.success('连接测试通过；目录尚未写入，刷新或启用后会保存目录')
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    testing.value = false
  }
}

async function refreshCatalog() {
  if (!selectedId.value) return
  refreshing.value = true
  try {
    const item = await refreshMcpServer(selectedId.value)
    updateServerInList(item)
    await loadCatalog(item.id)
    ElMessage.success('目录已刷新')
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    refreshing.value = false
  }
}

async function toggle(item: McpServer) {
  togglingId.value = item.id
  try {
    const updated = await setMcpServerEnabled(item.id, !item.enabled)
    updateServerInList(updated)
    if (updated.id === selectedId.value) {
      catalogStatus.value = updated.status
      await loadCatalog(updated.id)
    }
    ElMessage.success(updated.enabled ? 'MCP Server 已启用' : 'MCP Server 已停用')
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    togglingId.value = ''
  }
}

async function remove(item: McpServer) {
  try {
    await ElMessageBox.confirm(`确定删除 MCP Server“${item.name}”？目录授权和配置会一并删除。`, '删除确认', { type: 'warning' })
  } catch {
    return
  }
  deletingId.value = item.id
  try {
    await deleteMcpServer(item.id)
    servers.value = servers.value.filter((current) => current.id !== item.id)
    if (selectedId.value === item.id) {
      const next = servers.value[0]
      if (next) selectServer(next)
      else startNew()
    }
    ElMessage.success('MCP Server 已删除')
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    deletingId.value = ''
  }
}

function toggleGrant(kind: McpGrantKind, key: string, value: boolean | string | number) {
  const selected = grantSelections[kind]
  const enabled = Boolean(value)
  if (enabled && !selected.includes(key)) selected.push(key)
  if (!enabled) grantSelections[kind] = selected.filter((item) => item !== key)
}

function handleGrantChange(kind: McpGrantKind, key: string, value: unknown) {
  toggleGrant(kind, key, Boolean(value))
}

async function saveGrant(kind: McpGrantKind) {
  if (!selectedId.value) return
  grantSaving[kind] = true
  try {
    await setMcpGrants(selectedId.value, kind, grantSelections[kind])
    await loadCatalog(selectedId.value)
    ElMessage.success(`${kind === 'tool' ? 'Tools' : kind === 'prompt' ? 'Prompts' : 'Resources'} 授权已保存`)
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    grantSaving[kind] = false
  }
}

onMounted(() => { void loadServers(); void loadPresets(); void loadGlobalIntents() })
</script>

<template>
  <section class="panel stack mcp-intent-panel">
    <div class="panel-heading">
      <div><span class="section-kicker">意图路由</span><h2>全局 MCP 意图</h2></div>
      <div class="form-row">
        <el-tag type="info">追问继承 {{ globalIntents?.inheritance_ttl_seconds || 600 }} 秒</el-tag>
        <el-button :loading="globalIntentSaving" @click="saveGlobalIntentRules">保存全局规则</el-button>
        <el-button type="primary" plain :disabled="!servers.length" @click="addGlobalRule"><Plus />新增规则</el-button>
      </div>
    </div>
    <p class="hint">全局规则可以把自然表达绑定到整个 Server 或指定 Tool。内置规则只读；短语会在后端统一做 Unicode 规范化和同级冲突校验。</p>
    <div v-loading="globalIntentLoading" class="mcp-intent-stack">
      <div v-if="globalIntents?.builtin_rules.length" class="mcp-intent-readonly">
        <div v-for="rule in globalIntents.builtin_rules" :key="rule.id" class="mcp-intent-readonly-row">
          <el-tag size="small" type="info">内置</el-tag>
          <span><strong>{{ rule.server_slug }}</strong> · {{ rule.description }}</span>
        </div>
      </div>
      <div v-if="globalIntents?.custom_rules.length" class="mcp-intent-rules">
        <div v-for="(rule, index) in globalIntents.custom_rules" :key="rule.id" class="mcp-intent-rule-row">
          <el-input v-model="rule.phrase" placeholder="例如：帮我看看今天的热搜" />
          <el-select v-model="rule.server_id" placeholder="目标 Server">
            <el-option v-for="item in servers" :key="item.id" :label="item.name" :value="item.id" />
          </el-select>
          <el-input :model-value="rule.tool_key || ''" placeholder="Tool key（留空为整个 Server）" @update:model-value="setGlobalToolKey(rule, $event)" />
          <el-tag v-if="rule.orphaned" size="small" type="warning">孤儿目标</el-tag>
          <el-checkbox v-model="rule.enabled">启用</el-checkbox>
          <el-button text type="danger" @click="removeGlobalRule(index)"><Delete /></el-button>
        </div>
      </div>
      <div v-else class="empty-copy">还没有自定义全局规则。</div>
    </div>
  </section>

  <div class="mcp-layout">
    <section class="panel stack mcp-editor">
      <div class="panel-heading">
        <div><span class="section-kicker">MCP</span><h2>{{ isEditing ? '编辑 Server' : '新增 Server' }}</h2></div>
        <el-button v-if="isEditing" text @click="startNew"><Plus />新增</el-button>
      </div>
      <p class="hint">通过官方 MCP SDK 接入外部工具与内容能力。默认停用、授权为空；保存后需单独启用并逐项授权。</p>
      <label class="field-control"><span>显示名称</span><el-input v-model="draft.name" maxlength="160" show-word-limit placeholder="例如：本地文件工具" /></label>
      <label class="field-control"><span>稳定 slug（创建后不随名称变化）</span><el-input v-model="draft.slug" :disabled="isEditing" maxlength="80" placeholder="留空则按名称生成" /></label>
      <label class="field-control"><span>Transport</span><el-select v-model="draft.transport"><el-option v-for="item in transports" :key="item.value" :label="item.label" :value="item.value" /></el-select></label>
      <label class="field-control"><span>{{ draft.transport === 'stdio' ? 'stdio 进程配置 JSON' : `${transportLabel(draft.transport)} 连接配置 JSON` }}</span><el-input v-model="draft.configText" type="textarea" :rows="8" spellcheck="false" :placeholder="draft.transport === 'stdio' ? '{ command, args: [], cwd }' : '{ url, headers: {} }'" /></label>
      <p v-if="draft.transport === 'stdio'" class="hint mcp-config-help">stdio 需要 command、字符串数组 args 和可选 cwd；环境变量密钥请放到下方敏感值 JSON。</p>
      <p v-else class="hint mcp-config-help">{{ transportLabel(draft.transport) }} 需要 http(s) url 和非敏感 headers；Authorization、Token、密码或 API Key 必须放到下方敏感值 JSON。</p>
      <label class="field-control"><span>敏感值 JSON（只写入本机 .env，不会回显）</span><el-input v-model="draft.secretValuesText" type="textarea" :rows="3" spellcheck="false" placeholder='例如：{"Authorization":"Bearer …"}' /></label>
      <label class="field-control"><span>访问策略</span><el-select v-model="draft.accessPolicy"><el-option v-for="(label, value) in policyLabels" :key="value" :label="label" :value="value" /></el-select></label>
      <label v-if="draft.accessPolicy === 'private_users'" class="field-control"><span>私聊白名单（QQ 号，每行一个）</span><el-input v-model="draft.privateUsersText" type="textarea" :rows="3" placeholder="10001" /></label>
      <div class="form-row">
        <el-button type="primary" :loading="saving" @click="save">{{ isEditing ? '保存配置' : '创建 Server' }}</el-button>
        <el-button v-if="isEditing" :loading="testing" @click="testConnection">测试连接</el-button>
        <el-button v-if="isEditing" :loading="refreshing" @click="refreshCatalog"><Refresh />刷新目录</el-button>
      </div>
      <p v-if="selectedServer && Object.keys(selectedServer.secret_refs).length" class="hint">已配置密钥字段：{{ Object.keys(selectedServer.secret_refs).join('、') }}（原始值不会显示）</p>
    </section>

    <section class="panel stack mcp-server-list">
      <div class="panel-heading"><div><span class="section-kicker">已配置</span><h2>MCP Server</h2></div><div class="form-row"><el-button v-if="anySearchPreset && !anySearchPreset.installed" type="primary" plain @click="openAnySearchDialog"><Plus />添加 AnySearch</el-button><span class="count-badge">{{ servers.length }}</span></div></div>
      <div v-loading="loading" class="card-list">
        <button v-for="item in servers" :key="item.id" class="mcp-server-card" :class="{ active: item.id === selectedId }" @click="selectServer(item)">
          <span class="mcp-server-icon"><Connection /></span>
          <span class="mcp-server-copy"><strong>{{ item.name }}</strong><small>{{ item.slug }} · {{ transportLabel(item.transport) }}</small><small>{{ item.catalog_summary.tools }} Tools · {{ item.catalog_summary.resources }} Resources · {{ item.catalog_summary.resource_templates }} Templates · {{ item.catalog_summary.prompts }} Prompts</small></span>
          <span class="mcp-server-actions" @click.stop>
            <el-tag size="small" :type="statusType(item.status)">{{ statusLabel(item.status) }}</el-tag>
            <el-button size="small" :loading="togglingId === item.id" @click="toggle(item)"><VideoPause v-if="item.enabled" /><VideoPlay v-else />{{ item.enabled ? '停用' : '启用' }}</el-button>
            <el-button size="small" type="danger" plain :loading="deletingId === item.id" @click="remove(item)"><Delete />删除</el-button>
          </span>
        </button>
        <div v-if="!servers.length && !loading" class="empty-copy">还没有 MCP Server，先在左侧创建一个。</div>
      </div>
    </section>
  </div>

  <section v-if="selectedServer" class="panel stack mcp-catalog-panel">
    <div class="panel-heading"><div><span class="section-kicker">目录与权限</span><h2>{{ selectedServer.name }}</h2></div><div class="form-row"><el-tag :type="statusType(catalogStatus || selectedServer.status)">{{ statusLabel(catalogStatus || selectedServer.status) }}</el-tag><el-button :loading="catalogLoading" @click="loadCatalog()">重新读取目录</el-button></div></div>
    <p v-if="selectedServer.last_error" class="mcp-error-note">最近连接提示：{{ selectedServer.last_error }}</p>
    <p class="hint">目录只保存名称、说明和 Schema 等元数据。勾选后点击对应保存按钮，授权才会进入 Agent 注册范围；Resources、Templates、Prompts 不会自动注入系统提示词。</p>
    <div class="mcp-catalog-grid" v-loading="catalogLoading">
      <article v-for="(group, index) in catalogGroups" :key="`${group.label}-${index}`" class="mcp-catalog-group">
        <div class="panel-heading"><div><h3>{{ group.label }}</h3><small>{{ group.hint }}</small></div><el-button size="small" :loading="grantSaving[group.kind]" @click="saveGrant(group.kind)">保存授权</el-button></div>
        <div v-if="group.items.length" class="mcp-catalog-items">
          <label v-for="entry in group.items" :key="entry.key" class="mcp-catalog-item">
            <el-checkbox :model-value="grantSelections[group.kind].includes(entry.key)" @change="handleGrantChange(group.kind, entry.key, $event)" />
            <span><strong>{{ entryTitle(entry) }}</strong><small>{{ entrySubtitle(entry) }}</small></span>
          </label>
        </div>
        <div v-else class="empty-copy">目录中没有此类能力。</div>
      </article>
    </div>
  </section>

  <section v-if="selectedServer && serverIntents" v-loading="serverIntentLoading" class="panel stack mcp-intent-panel">
    <div class="panel-heading">
      <div><span class="section-kicker">意图路由</span><h2>{{ selectedServer.name }} 的 Server / Tool 规则</h2></div>
      <div class="form-row">
        <el-button :loading="serverIntentSaving" @click="saveServerIntentRules">保存 Server / Tool 规则</el-button>
        <el-button type="primary" plain @click="addServerPhrase"><Plus />新增 Server 短语</el-button>
      </div>
    </div>
    <p class="hint">Server 短语作用于该 Server；Tool 短语只作用于对应目录项。目录刷新后消失的 Tool 会保留为孤儿规则，但不会在运行时命中。</p>
    <div class="mcp-intent-section">
      <div class="panel-heading"><div><h3>Server 短语</h3><small>用于表达“使用这个 Server”或该 Server 的整体能力</small></div></div>
      <div v-if="serverIntents.server_phrases.length" class="mcp-intent-rules">
        <div v-for="(phrase, index) in serverIntents.server_phrases" :key="phrase.id" class="mcp-intent-rule-row mcp-intent-rule-row-phrase">
          <el-input v-model="phrase.phrase" placeholder="例如：使用 AnySearch" />
          <el-checkbox v-model="phrase.enabled">启用</el-checkbox>
          <el-button text type="danger" @click="removeServerPhrase(index)"><Delete /></el-button>
        </div>
      </div>
      <div v-else class="empty-copy">还没有 Server 自定义短语。</div>
    </div>
    <div class="mcp-intent-section">
      <div class="panel-heading"><div><h3>Tool 短语</h3><small>只对当前目录中的 Tool 产生意图</small></div></div>
      <div class="mcp-tool-intent-list">
        <article v-for="tool in serverIntents.tools" :key="tool.item_key" class="mcp-tool-intent-card">
          <div class="panel-heading">
            <div><strong>{{ tool.title || tool.name }}</strong><small>{{ tool.item_key }}</small></div>
            <div class="form-row"><el-tag v-if="!tool.catalog_present" size="small" type="warning">目录已不存在</el-tag><el-button size="small" plain @click="addToolPhrase(tool)"><Plus />新增短语</el-button></div>
          </div>
          <div v-if="tool.phrases.length" class="mcp-intent-rules">
            <div v-for="(phrase, index) in tool.phrases" :key="phrase.id" class="mcp-intent-rule-row mcp-intent-rule-row-phrase">
              <el-input v-model="phrase.phrase" placeholder="例如：把搜索结果列出来" />
              <el-checkbox v-model="phrase.enabled">启用</el-checkbox>
              <el-button text type="danger" @click="removeToolPhrase(tool, index)"><Delete /></el-button>
            </div>
          </div>
          <div v-else class="empty-copy">还没有 Tool 自定义短语。</div>
        </article>
      </div>
    </div>
  </section>

  <section class="panel stack mcp-risk-note">
    <div class="panel-heading"><div><span class="section-kicker">安全边界</span><h2><CircleCheck /> 授权前请确认</h2></div></div>
    <p>第三方 MCP Server 的实际稳定性取决于其自身实现，并非模型本身。MCP Server 运行在本机 Agent 进程可访问的环境中；只授权你理解且需要的目录项，停用或断线不会影响其他 Server。</p>
    <p>本版本不实施 OAuth 交互登录、Sampling、Elicitation、Roots 和订阅。密钥只通过本机环境变量引用传递，页面不会回填原始值。</p>
  </section>

  <el-dialog v-model="anySearchDialogVisible" title="添加 AnySearch" width="520px">
    <p class="hint">AnySearch 使用官方远程 MCP 端点。添加只保存连接配置，不会自动连接、授权或启用。</p>
    <p class="hint">官方地址：{{ anySearchPreset?.url }}</p>
    <el-radio-group v-model="anySearchMode">
      <el-radio value="anonymous">匿名访问</el-radio>
      <el-radio value="api_key">使用 API Key</el-radio>
    </el-radio-group>
    <el-input v-if="anySearchMode === 'api_key'" v-model="anySearchApiKey" class="dialog-field" type="password" show-password autocomplete="new-password" placeholder="只在本次请求中提交，页面不会回填" />
    <template #footer>
      <el-button @click="anySearchDialogVisible = false">取消</el-button>
      <el-button type="primary" :loading="anySearchInstalling" @click="installAnySearchPreset">保存并停用</el-button>
    </template>
  </el-dialog>
</template>
