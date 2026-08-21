<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { API, api, streamChat } from './services/api'

type Conversation = { id: string; title: string; model_alias: string; persona_id?: string | null }
type ModelProfile = { alias: string; model: string; configured: boolean }
type Message = { id?: string; role: string; content: string }
type KnowledgeBase = { id: string; name: string; embedding_profile: string }
type DocumentRow = { id: string; knowledge_base_id: string; filename: string; status: string; error?: string }
type MemoryRow = { id: string; fact_key: string; content: string; status: string }
type TaskRow = { id: string; type: string; status: string; error?: string; result?: { path?: string; delivery_status?: string; artifact_deleted?: boolean } }
type Confirmation = { token: string; action: string; payload: Record<string, unknown>; expires_at: string }
type ExtensionRow = { id: string; kind: string; name: string; version: string; description: string; enabled: boolean; builtin: boolean; status: string; access_policy: string; error?: string }
type PersonaRow = { id: string; name: string; raw_prompt: string; created_at?: string; updated_at?: string }
type AdminRow = { id: string; external_id: string; display_name?: string; platform: string; enabled: boolean }

const activeTab = ref('chat')
const conversations = ref<Conversation[]>([])
const models = ref<ModelProfile[]>([])
const currentConversationId = ref('')
const messages = ref<Message[]>([])
const input = ref('')
const sending = ref(false)
const health = ref<Record<string, unknown>>({})
const knowledgeBases = ref<KnowledgeBase[]>([])
const documents = ref<DocumentRow[]>([])
const memories = ref<MemoryRow[]>([])
const tasks = ref<TaskRow[]>([])
const confirmations = ref<Confirmation[]>([])
const kbName = ref('')
const kbEmbedding = ref('local-bge')
const selectedKb = ref('')
const uploadFile = ref<File | null>(null)
const reindexingDocumentIds = ref<string[]>([])
const mangaQuery = ref('')
const mangaResults = ref<Array<{ album_id: string; title: string }>>([])
const tools = ref<ExtensionRow[]>([])
const skills = ref<ExtensionRow[]>([])
const personas = ref<PersonaRow[]>([])
const admins = ref<AdminRow[]>([])
const personaName = ref('')
const personaPrompt = ref('')
const editingPersonaId = ref('')
const adminQq = ref('')
const adminName = ref('')
const githubUrl = ref('')
const memoryScope = ref('all')
const memoryUserId = ref('')
const memoryStatus = ref('active')
const currentConversation = computed(() => conversations.value.find((item) => item.id === currentConversationId.value))
const hasIndexingDocuments = computed(() => documents.value.some((item) => ['queued', 'indexing'].includes(item.status)))
const DOCUMENT_REFRESH_INTERVAL_MS = 1000
let documentRefreshTimer: number | undefined
const routePaths: Record<string, string> = {
  chat: '/chat', knowledge: '/knowledge', tools: '/tools', skills: '/skills',
  personas: '/personas', memory: '/memories', admin: '/admin', tasks: '/tasks', status: '/status',
}

function syncRoute() {
  const route = Object.entries(routePaths).find(([, path]) => window.location.pathname === path)?.[0]
  if (route) activeTab.value = route
}

function changeTab(tab: string | number) {
  const name = String(tab)
  activeTab.value = name
  const path = routePaths[name] || '/chat'
  if (window.location.pathname !== path) window.history.pushState({}, '', path)
  if (name === 'memory' || name === 'tasks') loadMemoryTasks()
}

async function loadBase() {
  const [healthData, modelData, conversationData, knowledgeData, personaData] = await Promise.all([
    api<Record<string, unknown>>('/health'),
    api<ModelProfile[]>('/models'),
    api<Conversation[]>('/conversations'),
    api<KnowledgeBase[]>('/knowledge-bases'),
    api<PersonaRow[]>('/personas'),
  ])
  health.value = healthData
  models.value = modelData
  conversations.value = conversationData
  knowledgeBases.value = knowledgeData
  personas.value = personaData
  if (!currentConversationId.value && conversations.value.length) {
    currentConversationId.value = conversations.value[0].id
    await loadMessages()
  }
  if (!selectedKb.value && knowledgeBases.value.length) selectedKb.value = knowledgeBases.value[0].id
}

async function createConversation() {
  const item = await api<Conversation>('/conversations', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title: `会话 ${conversations.value.length + 1}`, model_alias: models.value[0]?.alias || 'default' }),
  })
  conversations.value.unshift(item)
  currentConversationId.value = item.id
  messages.value = []
}

async function loadMessages() {
  if (!currentConversationId.value) return
  messages.value = await api(`/conversations/${currentConversationId.value}/messages`)
}

async function switchModel(alias: string) {
  if (!currentConversationId.value) return
  await api(`/conversations/${currentConversationId.value}/model`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ model_alias: alias }),
  })
  await loadBase()
  ElMessage.success(`已切换为 ${alias}`)
}

async function switchPersona(personaId: string | null) {
  if (!currentConversationId.value) return
  await api(`/conversations/${currentConversationId.value}/persona`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ persona_id: personaId }),
  })
  await loadBase()
  ElMessage.success(personaId ? '已切换人格' : '已关闭人格')
}

async function send() {
  const text = input.value.trim()
  if (!text || sending.value) return
  if (!currentConversationId.value) await createConversation()
  input.value = ''
  messages.value.push({ role: 'user', content: text }, { role: 'assistant', content: '' })
  const target = messages.value[messages.value.length - 1]
  sending.value = true
  try {
    await streamChat({ conversation_id: currentConversationId.value, message: text }, (event, data) => {
      if (event === 'token') target.content += String(data.text || '')
      if (event === 'error') target.content = `错误：${data.message}`
    })
    await loadMessages()
  } catch (error) {
    target.content = `错误：${(error as Error).message}`
  } finally {
    sending.value = false
  }
}

async function createKb() {
  if (!kbName.value.trim()) return
  await api('/knowledge-bases', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: kbName.value, embedding_profile: kbEmbedding.value }),
  })
  kbName.value = ''
  await loadBase()
}

async function upload() {
  if (!selectedKb.value || !uploadFile.value) return
  const form = new FormData()
  form.append('file', uploadFile.value)
  await api(`/documents/${selectedKb.value}`, { method: 'POST', body: form })
  uploadFile.value = null
  await loadDocuments()
  ElMessage.success('文件已进入索引队列')
}

async function loadDocuments() {
  const knowledgeBaseId = selectedKb.value
  const data = await api<DocumentRow[]>(`/documents${knowledgeBaseId ? `?knowledge_base_id=${knowledgeBaseId}` : ''}`)
  if (knowledgeBaseId !== selectedKb.value) return
  documents.value = data
  scheduleDocumentRefresh()
}

function stopDocumentRefresh() {
  if (documentRefreshTimer !== undefined) window.clearTimeout(documentRefreshTimer)
  documentRefreshTimer = undefined
}

function scheduleDocumentRefresh() {
  if (!hasIndexingDocuments.value) {
    stopDocumentRefresh()
    return
  }
  if (documentRefreshTimer !== undefined) return
  documentRefreshTimer = window.setTimeout(async () => {
    documentRefreshTimer = undefined
    try {
      await loadDocuments()
    } catch (error) {
      ElMessage.error(`索引状态刷新失败：${(error as Error).message}`)
    }
  }, DOCUMENT_REFRESH_INTERVAL_MS)
}

async function loadMemoryTasks() {
  const [memoryData, taskData, confirmationData] = await Promise.all([
    api<MemoryRow[]>(`/memories?scope=${memoryScope.value}${memoryUserId.value ? `&user_id=${encodeURIComponent(memoryUserId.value)}` : ''}${memoryStatus.value ? `&status=${memoryStatus.value}` : ''}`),
    api<TaskRow[]>('/tasks'),
    api<Confirmation[]>('/confirmations'),
  ])
  memories.value = memoryData
  tasks.value = taskData
  confirmations.value = confirmationData
}

async function loadManagement() {
  const [toolData, skillData, personaData, adminData] = await Promise.all([
    api<ExtensionRow[]>('/tools'), api<ExtensionRow[]>('/skills'), api<PersonaRow[]>('/personas'), api<AdminRow[]>('/admins'),
  ])
  tools.value = toolData
  skills.value = skillData
  personas.value = personaData
  admins.value = adminData
}

async function setExtension(kind: 'tools' | 'skills', item: ExtensionRow, enabled: boolean) {
  await api(`/${kind}/${item.name}/${enabled ? 'enable' : 'disable'}`, { method: 'POST' })
  await loadManagement()
}

async function deleteExtension(kind: 'tools' | 'skills', item: ExtensionRow) {
  if (item.builtin || !window.confirm(`确定删除 ${item.name}？`)) return
  await api(`/${kind}/${item.name}`, { method: 'DELETE' })
  await loadManagement()
}

async function importExtension(kind: 'tools' | 'skills', event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (!file) return
  const form = new FormData(); form.append('file', file)
  await api(`/${kind}/import`, { method: 'POST', body: form })
  await loadManagement()
  ElMessage.success(`${kind === 'tools' ? 'Tool' : 'Skill'} 已导入，默认停用`)
}

async function importGithub(kind: 'tools' | 'skills') {
  if (!githubUrl.value.trim()) return
  await api(`/${kind}/import/github`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url: githubUrl.value.trim() }) })
  githubUrl.value = ''; await loadManagement(); ElMessage.success('GitHub 扩展已导入，默认停用')
}

async function savePersona() {
  if (!personaName.value.trim() || !personaPrompt.value.trim()) return
  if (editingPersonaId.value) {
    await api(`/personas/${editingPersonaId.value}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: personaName.value, raw_prompt: personaPrompt.value }) })
  } else {
    await api('/personas', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: personaName.value, raw_prompt: personaPrompt.value }) })
  }
  personaName.value = ''; personaPrompt.value = ''; editingPersonaId.value = ''
  await loadManagement()
}

function editPersona(item: PersonaRow) {
  editingPersonaId.value = item.id; personaName.value = item.name; personaPrompt.value = item.raw_prompt
}

async function deletePersona(item: PersonaRow) {
  if (!window.confirm(`确定删除人格“${item.name}”？使用它的会话会自动关闭人格。`)) return
  await api(`/personas/${item.id}`, { method: 'DELETE' })
  await loadBase()
  ElMessage.success(`已删除人格：${item.name}`)
}

async function addAdmin() {
  if (!/^\d{5,20}$/.test(adminQq.value)) return
  await api('/admins', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ external_id: adminQq.value, display_name: adminName.value || null }) })
  adminQq.value = ''; adminName.value = ''; await loadManagement()
}

async function removeAdmin(item: AdminRow) {
  if (item.external_id === 'local-owner' || !window.confirm(`确定移除 ${item.external_id} 的 Owner 权限？`)) return
  await api(`/admins/${item.external_id}`, { method: 'DELETE' }); await loadManagement()
}

async function archiveMemory(id: string) {
  await api(`/memories/${id}/archive`, { method: 'POST' })
  await loadMemoryTasks()
}

async function editMemory(item: MemoryRow) {
  const content = window.prompt('修改记忆内容', item.content)?.trim()
  if (!content || content === item.content) return
  await api(`/memories/${item.id}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ content }),
  })
  await loadMemoryTasks()
}

async function deleteMemory(id: string) {
  if (!window.confirm('确定删除这条记忆？')) return
  await api(`/memories/${id}`, { method: 'DELETE' })
  await loadMemoryTasks()
}

async function deleteDocument(id: string) {
  if (!window.confirm('确定删除这个文档及其索引？')) return
  await api(`/documents/${id}`, { method: 'DELETE' })
  await loadDocuments()
}

async function reindexDocument(id: string) {
  if (reindexingDocumentIds.value.includes(id)) return
  reindexingDocumentIds.value.push(id)
  try {
    const result = await api<{ task_id: string }>(`/documents/${id}/reindex`, { method: 'POST' })
    ElMessage.success(`已创建重新索引任务 ${result.task_id}`)
    await Promise.all([loadDocuments(), loadMemoryTasks()])
  } finally {
    reindexingDocumentIds.value = reindexingDocumentIds.value.filter((item) => item !== id)
  }
}

async function deleteKb() {
  if (!selectedKb.value || !window.confirm('确定删除当前知识库及全部文档？')) return
  await api(`/knowledge-bases/${selectedKb.value}`, { method: 'DELETE' })
  selectedKb.value = ''
  documents.value = []
  await loadBase()
}

async function rebuildKb() {
  if (!selectedKb.value) return
  const result = await api<{ task_id: string }> (`/knowledge-bases/${selectedKb.value}/embedding`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ embedding_profile: kbEmbedding.value }),
  })
  ElMessage.success(`已创建重建任务 ${result.task_id}`)
  await loadMemoryTasks()
}

async function cancelTask(id: string) {
  await api(`/tasks/${id}/cancel`, { method: 'POST' })
  await loadMemoryTasks()
}

async function searchManga() {
  const result = await api<{ results: Array<{ album_id: string; title: string }> }>('/manga/search', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ query: mangaQuery.value }),
  })
  mangaResults.value = result.results
}

async function requestDownload(albumId: string) {
  const result = await api<{ task: TaskRow }>('/manga/download', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ album_id: albumId, requester_id: 'local-owner', conversation_id: currentConversationId.value || null }),
  })
  await loadMemoryTasks()
  ElMessage.success(`已创建下载任务 ${result.task.id}，无需二次确认`)
}

async function deleteTaskArtifact(item: TaskRow) {
  if (!window.confirm(`确定删除任务 ${item.id} 的本地漫画文件？任务记录会保留。`)) return
  await api(`/tasks/${item.id}/artifact`, { method: 'DELETE' })
  await loadMemoryTasks()
  ElMessage.success('本地漫画文件已删除，任务记录已保留')
}

async function resolve(token: string, approve: boolean) {
  await api(`/confirmations/${token}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ requester_id: 'local-owner', approve }),
  })
  await loadMemoryTasks()
}

onMounted(async () => {
  syncRoute()
  window.addEventListener('popstate', syncRoute)
  try { await loadBase(); await loadDocuments(); await loadMemoryTasks(); await loadManagement() }
  catch (error) { ElMessage.error((error as Error).message) }
})

onUnmounted(() => {
  window.removeEventListener('popstate', syncRoute)
  stopDocumentRefresh()
})
</script>

<template>
  <div class="shell">
    <header>
      <div><span class="eyebrow">LOCAL AI WORKSPACE</span><h1>PersonalAgent</h1></div>
      <el-tag :type="health.status === 'ok' ? 'success' : 'warning'">{{ health.status || 'loading' }}</el-tag>
    </header>
    <el-tabs v-model="activeTab" class="tabs" @tab-change="changeTab">
      <el-tab-pane label="对话" name="chat">
        <div class="chat-grid">
          <aside class="panel sidebar">
            <el-button type="primary" class="full" @click="createConversation">新建会话</el-button>
            <button v-for="item in conversations" :key="item.id" class="conversation" :class="{ active: item.id === currentConversationId }" @click="currentConversationId = item.id; loadMessages()">
              <strong>{{ item.title }}</strong><small>{{ item.model_alias }}</small>
            </button>
          </aside>
          <main class="panel chat-panel">
            <div class="toolbar">
              <span>{{ currentConversation?.title || '未选择会话' }}</span>
              <div class="row">
                <el-select :model-value="currentConversation?.model_alias" placeholder="模型" @change="switchModel">
                  <el-option v-for="model in models" :key="model.alias" :label="`${model.alias}${model.configured ? '' : '（未配置）'}`" :value="model.alias" />
                </el-select>
                <el-select :model-value="currentConversation?.persona_id || null" placeholder="人格" @change="switchPersona">
                  <el-option label="关闭人格" :value="null" />
                  <el-option v-for="item in personas" :key="item.id" :label="item.name" :value="item.id" />
                </el-select>
              </div>
            </div>
            <div class="messages">
              <article v-for="(message, index) in messages" :key="message.id || index" :class="['message', message.role]">
                <span>{{ message.role === 'user' ? '你' : 'Agent' }}</span><p>{{ message.content }}</p>
              </article>
            </div>
            <div class="composer">
              <el-input v-model="input" type="textarea" :rows="3" placeholder="输入消息，Ctrl+Enter 发送" @keydown.ctrl.enter.prevent="send" />
              <el-button type="primary" :loading="sending" @click="send">发送</el-button>
            </div>
          </main>
        </div>
      </el-tab-pane>

      <el-tab-pane label="知识库" name="knowledge">
        <section class="panel stack">
          <h2>知识库与文档</h2>
          <div class="row">
            <el-input v-model="kbName" placeholder="知识库名称" />
            <el-select v-model="kbEmbedding"><el-option label="本地 BGE" value="local-bge" /><el-option label="在线 Embedding" value="online" /></el-select>
            <el-button type="primary" @click="createKb">创建</el-button>
          </div>
          <div class="row">
            <el-select v-model="selectedKb" placeholder="选择知识库" @change="loadDocuments"><el-option v-for="kb in knowledgeBases" :key="kb.id" :label="`${kb.name} · ${kb.embedding_profile}`" :value="kb.id" /></el-select>
            <input type="file" accept=".txt,.md,.markdown,.html,.htm,.pdf,.docx" @change="uploadFile = ($event.target as HTMLInputElement).files?.[0] || null" />
            <el-button :disabled="!uploadFile || !selectedKb" @click="upload">上传并索引</el-button>
            <el-button :disabled="!selectedKb" @click="rebuildKb">按所选 Embedding 重建</el-button>
            <el-button type="danger" plain :disabled="!selectedKb" @click="deleteKb">删除知识库</el-button>
          </div>
          <el-tag v-if="hasIndexingDocuments" type="warning" effect="plain">索引状态自动刷新中</el-tag>
          <el-table :data="documents"><el-table-column prop="filename" label="文件" /><el-table-column label="状态"><template #default="scope"><el-tag :type="scope.row.status === 'ready' ? 'success' : scope.row.status === 'failed' ? 'danger' : 'warning'">{{ scope.row.status }}</el-tag></template></el-table-column><el-table-column prop="error" label="错误" /><el-table-column label="操作" width="190"><template #default="scope"><el-button size="small" :loading="reindexingDocumentIds.includes(scope.row.id)" :disabled="['queued', 'indexing'].includes(scope.row.status)" @click="reindexDocument(scope.row.id)">重新索引</el-button><el-button size="small" type="danger" plain @click="deleteDocument(scope.row.id)">删除</el-button></template></el-table-column></el-table>
        </section>
      </el-tab-pane>

      <el-tab-pane label="记忆" name="memory">
        <section class="panel stack"><h2>长期记忆</h2>
          <div class="row"><el-select v-model="memoryScope" @change="loadMemoryTasks"><el-option label="全部" value="all" /><el-option label="全局记忆" value="global" /><el-option label="指定用户" value="user" /></el-select><el-input v-model="memoryUserId" placeholder="QQ 用户 ID（可选）" @change="loadMemoryTasks" /><el-select v-model="memoryStatus" @change="loadMemoryTasks"><el-option label="有效" value="active" /><el-option label="已归档" value="archived" /><el-option label="全部状态" value="" /></el-select></div>
          <el-table :data="memories"><el-table-column prop="fact_key" label="事实键" /><el-table-column prop="content" label="内容" /><el-table-column prop="status" label="状态" /><el-table-column label="操作" width="230"><template #default="scope"><el-button size="small" @click="editMemory(scope.row)">编辑</el-button><el-button size="small" @click="archiveMemory(scope.row.id)">归档</el-button><el-button size="small" type="danger" plain @click="deleteMemory(scope.row.id)">删除</el-button></template></el-table-column></el-table>
        </section>
      </el-tab-pane>

      <el-tab-pane label="Tools" name="tools">
        <div class="two-column">
          <section class="panel stack"><h2>Tool 管理</h2><p class="hint">导入的 Python Tool 在后端进程内执行，拥有本机代码权限；默认停用且不会自动安装依赖。</p><input type="file" accept=".zip" @change="importExtension('tools', $event)" /><div class="row"><el-input v-model="githubUrl" placeholder="公开 GitHub 仓库地址" /><el-button @click="importGithub('tools')">导入 GitHub Tool</el-button></div><el-table :data="tools"><el-table-column prop="name" label="名称" /><el-table-column prop="description" label="说明" /><el-table-column prop="status" label="状态" /><el-table-column label="操作" width="220"><template #default="scope"><el-button size="small" @click="setExtension('tools', scope.row, !scope.row.enabled)">{{ scope.row.enabled ? '停用' : '启用' }}</el-button><el-button size="small" type="danger" plain :disabled="scope.row.builtin" @click="deleteExtension('tools', scope.row)">删除</el-button></template></el-table-column></el-table></section>
          <section class="panel stack"><h2>漫画工具</h2><p class="hint">Owner 点击后立即创建任务，无需二次确认。</p><div class="row"><el-input v-model="mangaQuery" placeholder="关键词" /><el-button type="primary" @click="searchManga">搜索</el-button></div><div v-for="item in mangaResults" :key="item.album_id" class="result"><span>JM{{ item.album_id }} · {{ item.title }}</span><el-button size="small" @click="requestDownload(item.album_id)">立即下载</el-button></div></section>
        </div>
      </el-tab-pane>

      <el-tab-pane label="Skills" name="skills">
        <section class="panel stack"><h2>Skills</h2><p class="hint">只读取 SKILL.md 和根目录内的 references/assets；scripts 仅展示，不执行。每轮最多加载 3 个。</p><input type="file" accept=".zip" @change="importExtension('skills', $event)" /><div class="row"><el-input v-model="githubUrl" placeholder="公开 GitHub Skill 地址" /><el-button @click="importGithub('skills')">导入 GitHub Skill</el-button></div><el-table :data="skills"><el-table-column prop="name" label="名称" /><el-table-column prop="description" label="说明" /><el-table-column prop="status" label="状态" /><el-table-column label="操作" width="220"><template #default="scope"><el-button size="small" @click="setExtension('skills', scope.row, !scope.row.enabled)">{{ scope.row.enabled ? '停用' : '启用' }}</el-button><el-button size="small" type="danger" plain @click="deleteExtension('skills', scope.row)">删除</el-button></template></el-table-column></el-table></section>
      </el-tab-pane>

      <el-tab-pane label="人格" name="personas">
        <div class="two-column"><section class="panel stack"><h2>{{ editingPersonaId ? '编辑人格' : '新建人格' }}</h2><el-input v-model="personaName" placeholder="人格名称" /><el-input v-model="personaPrompt" type="textarea" :rows="8" maxlength="8000" show-word-limit placeholder="描述角色身份、语气、称呼、详细程度和格式；不能修改权限、工具或系统规则" /><div class="row"><el-button type="primary" @click="savePersona">保存</el-button><el-button @click="personaName = ''; personaPrompt = ''; editingPersonaId = ''">清空</el-button></div></section><section class="panel stack"><h2>已保存人格</h2><div v-for="item in personas" :key="item.id" class="result"><strong>{{ item.name }}</strong><div><el-button size="small" @click="editPersona(item)">编辑</el-button><el-button size="small" type="danger" plain @click="deletePersona(item)">删除</el-button></div></div></section></div>
      </el-tab-pane>

      <el-tab-pane label="管理员" name="admin">
        <section class="panel stack"><h2>QQ Owner</h2><p class="hint">local-owner 永久存在且不可删除；这里的变更会立即生效。</p><div class="row"><el-input v-model="adminQq" placeholder="QQ 号" /><el-input v-model="adminName" placeholder="备注（可选）" /><el-button type="primary" @click="addAdmin">添加 Owner</el-button></div><el-table :data="admins"><el-table-column prop="external_id" label="身份" /><el-table-column prop="display_name" label="备注" /><el-table-column prop="platform" label="平台" /><el-table-column label="操作"><template #default="scope"><el-button size="small" type="danger" plain :disabled="scope.row.external_id === 'local-owner'" @click="removeAdmin(scope.row)">删除</el-button></template></el-table-column></el-table></section>
      </el-tab-pane>

      <el-tab-pane label="漫画与任务" name="tasks">
        <div class="two-column">
          <section class="panel stack"><h2>漫画搜索</h2><div class="row"><el-input v-model="mangaQuery" placeholder="关键词" /><el-button type="primary" @click="searchManga">搜索</el-button></div>
            <div v-for="item in mangaResults" :key="item.album_id" class="result"><span>JM{{ item.album_id }} · {{ item.title }}</span><el-button size="small" @click="requestDownload(item.album_id)">立即下载</el-button></div>
          </section>
          <section class="panel stack"><h2>待确认</h2><div v-for="item in confirmations" :key="item.token" class="result"><code>{{ item.token }}</code><span>{{ item.action }}</span><div><el-button size="small" type="primary" @click="resolve(item.token, true)">确认</el-button><el-button size="small" @click="resolve(item.token, false)">拒绝</el-button></div></div></section>
        </div>
        <section class="panel stack task-list"><h2>任务</h2><el-table :data="tasks"><el-table-column prop="id" label="ID" /><el-table-column prop="type" label="类型" /><el-table-column prop="status" label="状态" /><el-table-column prop="result.delivery_status" label="QQ发送" /><el-table-column prop="error" label="错误" /><el-table-column label="操作" width="250"><template #default="scope"><el-button v-if="['queued', 'running'].includes(scope.row.status)" size="small" @click="cancelTask(scope.row.id)">取消</el-button><el-link v-if="scope.row.status === 'succeeded' && !scope.row.result?.artifact_deleted" :href="`${API}/tasks/${scope.row.id}/artifact`" target="_blank" type="primary">下载产物</el-link><el-button v-if="scope.row.status === 'succeeded' && scope.row.type === 'manga_download' && !scope.row.result?.artifact_deleted" size="small" type="danger" plain @click="deleteTaskArtifact(scope.row)">删除本地文件</el-button><el-tag v-if="scope.row.result?.artifact_deleted" type="info">已删除</el-tag></template></el-table-column></el-table></section>
      </el-tab-pane>

      <el-tab-pane label="状态" name="status"><pre class="panel status">{{ JSON.stringify(health, null, 2) }}</pre></el-tab-pane>
    </el-tabs>
  </div>
</template>
