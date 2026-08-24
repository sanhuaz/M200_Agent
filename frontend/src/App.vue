<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import {
  ChatDotRound, Collection, DataAnalysis, Delete, Expand, Fold, MagicStick, Memo, Menu,
  Monitor, Moon, PictureFilled, Setting, Sunny, Tools, UserFilled,
} from '@element-plus/icons-vue'
import { API, api, streamChat } from './services/api'

type Conversation = { id: string; title: string; platform: string; model_alias: string; persona_id?: string | null }
type ReasoningEffort = 'low' | 'medium' | 'high' | null
type ModelProfile = {
  alias: string
  model: string
  base_url: string
  configured: boolean
  has_api_key: boolean
  in_use: boolean
  can_delete: boolean
  is_default: boolean
  reasoning_effort: ReasoningEffort
  streaming: boolean
  temperature: number | null
  context_window: number
  input_soft_limit: number
  max_output_tokens: number
  timeout_seconds: number
}
type ModelProfileDraft = Omit<ModelProfile, 'configured' | 'has_api_key' | 'in_use' | 'can_delete' | 'is_default'> & { api_key: string }
type Message = { id?: string; role: string; content: string }
type KnowledgeBase = { id: string; name: string; embedding_profile: string }
type DocumentRow = { id: string; knowledge_base_id: string; filename: string; status: string; error?: string }
type MemoryRow = { id: string; fact_key: string; content: string; status: string }
type TaskRow = { id: string; type: string; status: string; error?: string; result?: { path?: string; delivery_status?: string; artifact_deleted?: boolean } }
type Confirmation = { token: string; action: string; payload: Record<string, unknown>; status: string; expires_at: string }
type ExtensionRow = { id: string; kind: string; name: string; version: string; description: string; enabled: boolean; builtin: boolean; status: string; access_policy: string; error?: string }
type PersonaRow = { id: string; name: string; raw_prompt: string; created_at?: string; updated_at?: string }
type AdminRow = { id: string; external_id: string; display_name?: string; platform: string; enabled: boolean }
type HealthItem = { alias?: string; model?: string; configured?: boolean }
type HealthData = {
  status?: string
  database?: string
  chroma?: string
  worker?: string
  onebot?: string
  reranker?: string
  python_executable?: string
  models?: HealthItem[]
  embedding_profiles?: HealthItem[]
  [key: string]: unknown
}
type ThemeName = 'light' | 'dark'

const activeTab = ref('chat')
const conversations = ref<Conversation[]>([])
const models = ref<ModelProfile[]>([])
const modelForm = ref<ModelProfileDraft>(newModelDraft())
const editingModelAlias = ref('')
const modelSaving = ref(false)
const modelTesting = ref(false)
const currentConversationId = ref('')
const messages = ref<Message[]>([])
const messagesContainer = ref<HTMLElement | null>(null)
const messagesAutoFollow = ref(true)
const input = ref('')
const sending = ref(false)
const health = ref<HealthData>({})
const knowledgeBases = ref<KnowledgeBase[]>([])
const documents = ref<DocumentRow[]>([])
const memories = ref<MemoryRow[]>([])
const tasks = ref<TaskRow[]>([])
const confirmations = ref<Confirmation[]>([])
const selectedTaskIds = ref<string[]>([])
const selectedConfirmationTokens = ref<string[]>([])
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
const sidebarCollapsed = ref(false)
const mobileSidebarOpen = ref(false)
const isNarrow = ref(window.innerWidth < 1024)
const conversationListOpen = ref(true)
const extensionExpanded = ref(true)
const systemExpanded = ref(true)
const theme = ref<ThemeName>('light')
let followsSystemTheme = false
let systemThemeQuery: MediaQueryList | undefined
const currentConversation = computed(() => conversations.value.find((item) => item.id === currentConversationId.value))
const hasIndexingDocuments = computed(() => documents.value.some((item) => ['queued', 'indexing'].includes(item.status)))
const DOCUMENT_REFRESH_INTERVAL_MS = 1000
let documentRefreshTimer: number | undefined
const routePaths: Record<string, string> = {
  chat: '/chat', models: '/models', knowledge: '/knowledge', tools: '/tools', skills: '/skills',
  personas: '/personas', memory: '/memories', admin: '/admin', tasks: '/tasks', status: '/status',
}
const pageDetails: Record<string, { title: string; description: string }> = {
  chat: { title: '对话', description: '与 M200 Agent 对话并管理会话模型和人格' },
  models: { title: '模型管理', description: '配置主聊天 LLM，并在会话中快捷切换' },
  knowledge: { title: '知识库', description: '管理文档、Embedding 配置和索引状态' },
  memory: { title: '长期记忆', description: '查看和维护全局、用户与群组记忆' },
  personas: { title: '人格管理', description: '创建可按会话切换的原始提示词人格' },
  tools: { title: '工具管理', description: '管理工具扩展并发起漫画搜索与下载' },
  skills: { title: '技能管理', description: '导入、启用和维护 Agent Skills' },
  tasks: { title: '任务中心', description: '处理待确认操作并跟踪后台任务' },
  admin: { title: '管理员', description: '维护拥有高权限操作能力的 QQ Owner' },
  status: { title: '系统状态', description: '查看本地服务、模型和检索组件状态' },
}
const currentPage = computed(() => pageDetails[activeTab.value] || pageDetails.chat)
const sidebarToggleLabel = computed(() => isNarrow.value
  ? mobileSidebarOpen.value ? '关闭导航菜单' : '打开导航菜单'
  : sidebarCollapsed.value ? '展开侧边栏' : '折叠侧边栏')
const statusLabels: Record<string, string> = {
  ok: '正常', degraded: '降级', connected: '已连接', unavailable: '不可用', installed: '已安装',
  running: '运行中', stopped: '已停止', configured_disconnected: '已配置未连接',
  needs_configuration: '需要配置', enabled: '已启用', disabled: '已停用',
}

function applyTheme(value: ThemeName) {
  theme.value = value
  document.documentElement.dataset.theme = value
}

function handleSystemThemeChange(event: MediaQueryListEvent) {
  if (followsSystemTheme) applyTheme(event.matches ? 'dark' : 'light')
}

function initializeTheme() {
  const stored = window.localStorage.getItem('m200-theme')
  systemThemeQuery = window.matchMedia('(prefers-color-scheme: dark)')
  followsSystemTheme = stored !== 'light' && stored !== 'dark'
  applyTheme(followsSystemTheme && systemThemeQuery.matches ? 'dark' : stored === 'dark' ? 'dark' : 'light')
  systemThemeQuery.addEventListener('change', handleSystemThemeChange)
}

function toggleTheme() {
  followsSystemTheme = false
  const nextTheme: ThemeName = theme.value === 'dark' ? 'light' : 'dark'
  window.localStorage.setItem('m200-theme', nextTheme)
  applyTheme(nextTheme)
}

function toggleSidebar() {
  if (window.innerWidth < 1024) mobileSidebarOpen.value = !mobileSidebarOpen.value
  else sidebarCollapsed.value = !sidebarCollapsed.value
}

function handleResize() {
  isNarrow.value = window.innerWidth < 1024
  if (!isNarrow.value) mobileSidebarOpen.value = false
}

function statusLabel(value: unknown) {
  const key = String(value || '')
  return statusLabels[key] || key || '未知'
}

function statusType(value: unknown): '' | 'success' | 'warning' | 'danger' | 'info' {
  const key = String(value || '')
  if (['ok', 'connected', 'installed', 'running', 'enabled'].includes(key)) return 'success'
  if (['degraded', 'configured_disconnected', 'needs_configuration', 'stopped', 'disabled'].includes(key)) return 'warning'
  if (key === 'unavailable') return 'danger'
  return 'info'
}

function newModelDraft(): ModelProfileDraft {
  return {
    alias: '',
    model: '',
    base_url: '',
    reasoning_effort: null,
    streaming: true,
    temperature: null,
    context_window: 1_000_000,
    input_soft_limit: 131_072,
    max_output_tokens: 16_384,
    timeout_seconds: 120,
    api_key: '',
  }
}

function modelDraftFrom(item: ModelProfile): ModelProfileDraft {
  return {
    alias: item.alias,
    model: item.model,
    base_url: item.base_url,
    reasoning_effort: item.reasoning_effort,
    streaming: item.streaming,
    temperature: item.temperature,
    context_window: item.context_window,
    input_soft_limit: item.input_soft_limit,
    max_output_tokens: item.max_output_tokens,
    timeout_seconds: item.timeout_seconds,
    api_key: '',
  }
}

const editingModel = computed(() => models.value.find((item) => item.alias === editingModelAlias.value))

function selectModel(item: ModelProfile) {
  editingModelAlias.value = item.alias
  modelForm.value = modelDraftFrom(item)
}

async function makeDefaultModel(item: ModelProfile) {
  if (item.is_default || !item.configured) return
  if (!window.confirm(`确定将“${item.alias}”设为主默认模型？现有网页和 QQ 会话都会切换。`)) return
  try {
    await api(`/models/${item.alias}/default`, { method: 'PUT' })
    await loadBase()
    ElMessage.success(`已将 ${item.alias} 设为主默认模型`)
  } catch (error) {
    ElMessage.error((error as Error).message)
  }
}

function startNewModel() {
  editingModelAlias.value = ''
  modelForm.value = newModelDraft()
}

function modelPayload() {
  return {
    alias: modelForm.value.alias,
    model: modelForm.value.model,
    base_url: modelForm.value.base_url,
    ...(modelForm.value.api_key ? { api_key: modelForm.value.api_key } : {}),
    reasoning_effort: modelForm.value.reasoning_effort,
    streaming: modelForm.value.streaming,
    temperature: modelForm.value.temperature,
    context_window: modelForm.value.context_window,
    input_soft_limit: modelForm.value.input_soft_limit,
    max_output_tokens: modelForm.value.max_output_tokens,
    timeout_seconds: modelForm.value.timeout_seconds,
  }
}

async function saveModel() {
  if (!modelForm.value.model.trim() || !modelForm.value.base_url.trim()) {
    ElMessage.warning('请填写模型名称和 base_url')
    return
  }
  modelSaving.value = true
  try {
    if (editingModelAlias.value) {
      const payload = modelPayload()
      await api(`/models/${editingModelAlias.value}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
      })
      ElMessage.success('模型配置已保存')
    } else {
      if (!modelForm.value.alias.trim()) {
        ElMessage.warning('请填写模型别名')
        return
      }
      const saved = await api<ModelProfile>('/models', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(modelPayload()),
      })
      editingModelAlias.value = saved.alias
      ElMessage.success('模型配置已创建')
    }
    await loadBase()
    const current = models.value.find((item) => item.alias === editingModelAlias.value)
    if (current) selectModel(current)
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    modelSaving.value = false
  }
}

async function testModel() {
  if (!modelForm.value.model.trim() || !modelForm.value.base_url.trim()) {
    ElMessage.warning('请先填写模型名称和 base_url')
    return
  }
  if (!window.confirm('测试连接会发送一次极短的模型请求，可能产生少量调用费用。继续吗？')) return
  modelTesting.value = true
  try {
    await api('/models/test-connection', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(modelPayload()),
    })
    ElMessage.success('连接测试成功')
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    modelTesting.value = false
  }
}

async function clearModelKey() {
  if (!editingModelAlias.value || !editingModel.value?.has_api_key) return
  if (!window.confirm(`确定清除“${editingModelAlias.value}”的 API Key？`)) return
  try {
    await api(`/models/${editingModelAlias.value}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ clear_api_key: true }),
    })
    await loadBase()
    const current = models.value.find((item) => item.alias === editingModelAlias.value)
    if (current) selectModel(current)
    ElMessage.success('API Key 已清除')
  } catch (error) {
    ElMessage.error((error as Error).message)
  }
}

async function deleteModel() {
  const item = editingModel.value
  if (!item || !item.can_delete) return
  if (!window.confirm(`确定删除模型配置“${item.alias}”？`)) return
  try {
    await api(`/models/${item.alias}`, { method: 'DELETE' })
    await loadBase()
    startNewModel()
    ElMessage.success('模型配置已删除')
  } catch (error) {
    ElMessage.error((error as Error).message)
  }
}

initializeTheme()

function syncRoute() {
  const route = Object.entries(routePaths).find(([, path]) => window.location.pathname === path)?.[0]
  if (route) activeTab.value = route
  mobileSidebarOpen.value = false
}

function changeTab(tab: string | number) {
  const name = String(tab)
  activeTab.value = name
  const path = routePaths[name] || '/chat'
  if (window.location.pathname !== path) window.history.pushState({}, '', path)
  mobileSidebarOpen.value = false
  if (name === 'memory' || name === 'tasks') loadMemoryTasks()
  if (name === 'models' && !editingModelAlias.value && models.value.length) {
    selectModel(models.value.find((item) => item.is_default) || models.value[0])
  }
}

function isMessagesNearBottom(element: HTMLElement) {
  return element.scrollHeight - element.scrollTop - element.clientHeight < 56
}

function handleMessagesScroll(event: Event) {
  messagesAutoFollow.value = isMessagesNearBottom(event.currentTarget as HTMLElement)
}

async function scrollMessagesToBottom(force = false) {
  await nextTick()
  const element = messagesContainer.value
  if (!element || (!force && !messagesAutoFollow.value)) return
  element.scrollTop = element.scrollHeight
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
  if (activeTab.value === 'models' && !editingModelAlias.value && models.value.length) {
    selectModel(models.value.find((item) => item.is_default) || models.value[0])
  }
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
    body: JSON.stringify({ title: `会话 ${conversations.value.length + 1}` }),
  })
  conversations.value.unshift(item)
  currentConversationId.value = item.id
  messages.value = []
  messagesAutoFollow.value = true
  await scrollMessagesToBottom(true)
}

async function loadMessages(force = true) {
  if (!currentConversationId.value) return
  messages.value = await api(`/conversations/${currentConversationId.value}/messages`)
  if (force) messagesAutoFollow.value = true
  await scrollMessagesToBottom(force)
}

async function switchModel(alias: string) {
  if (!currentConversationId.value) return
  await api(`/conversations/${currentConversationId.value}/model`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ model_alias: alias }),
  })
  await loadBase()
  ElMessage.success(`已切换为 ${alias}`)
}

async function switchPersona(personaId: string) {
  if (!currentConversationId.value) return
  await api(`/conversations/${currentConversationId.value}/persona`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ persona_id: personaId || null }),
  })
  await loadBase()
  ElMessage.success(personaId ? '已切换人格' : '已关闭人格')
}

async function deleteConversation(item: Conversation) {
  if (item.platform !== 'web' || !window.confirm(`确定删除网页会话“${item.title}”？消息和工作流记录将被删除。`)) return
  try {
    await api(`/conversations/${item.id}`, { method: 'DELETE' })
    const wasCurrent = currentConversationId.value === item.id
    conversations.value = conversations.value.filter((conversation) => conversation.id !== item.id)
    if (wasCurrent) {
      const next = conversations.value[0]
      currentConversationId.value = next?.id || ''
      messages.value = []
      messagesAutoFollow.value = true
      if (next) await loadMessages()
    }
    ElMessage.success('网页会话已删除')
  } catch (error) {
    ElMessage.error((error as Error).message)
  }
}

async function send() {
  const text = input.value.trim()
  if (!text || sending.value) return
  if (!currentConversationId.value) await createConversation()
  input.value = ''
  messages.value.push({ role: 'user', content: text }, { role: 'assistant', content: '' })
  const target = messages.value[messages.value.length - 1]
  messagesAutoFollow.value = true
  await scrollMessagesToBottom(true)
  sending.value = true
  try {
    await streamChat({ conversation_id: currentConversationId.value, message: text }, (event, data) => {
      if (event === 'token') target.content += String(data.text || '')
      if (event === 'error') target.content = `错误：${data.message}`
      if (event === 'token') void scrollMessagesToBottom()
    })
    await loadMessages(false)
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
    api<Confirmation[]>('/confirmations?status=all'),
  ])
  memories.value = memoryData
  tasks.value = taskData
  confirmations.value = confirmationData
  selectedTaskIds.value = selectedTaskIds.value.filter((id) => tasks.value.some((item) => item.id === id))
  selectedConfirmationTokens.value = selectedConfirmationTokens.value.filter((token) => confirmations.value.some((item) => item.token === token))
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

function taskSelectable(item: TaskRow) {
  return ['succeeded', 'failed', 'cancelled'].includes(item.status)
}

function taskSelectionChange(rows: TaskRow[]) {
  selectedTaskIds.value = rows.map((item) => item.id)
}

async function deleteSelectedConfirmations() {
  if (!selectedConfirmationTokens.value.length) return
  if (!window.confirm(`确定删除选中的 ${selectedConfirmationTokens.value.length} 条确认记录？待确认请求将立即失效。`)) return
  await api('/confirmations/bulk-delete', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tokens: selectedConfirmationTokens.value }),
  })
  selectedConfirmationTokens.value = []
  await loadMemoryTasks()
  ElMessage.success('确认记录已删除')
}

async function deleteSelectedTasks() {
  if (!selectedTaskIds.value.length) return
  if (!window.confirm(`确定删除选中的 ${selectedTaskIds.value.length} 条任务记录？本地下载文件会保留。`)) return
  await api('/tasks/bulk-delete', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids: selectedTaskIds.value }),
  })
  selectedTaskIds.value = []
  await loadMemoryTasks()
  ElMessage.success('任务记录已删除，本地文件仍保留')
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
  window.addEventListener('resize', handleResize)
  try { await loadBase(); await loadDocuments(); await loadMemoryTasks(); await loadManagement() }
  catch (error) { ElMessage.error((error as Error).message) }
})

onUnmounted(() => {
  window.removeEventListener('popstate', syncRoute)
  window.removeEventListener('resize', handleResize)
  systemThemeQuery?.removeEventListener('change', handleSystemThemeChange)
  stopDocumentRefresh()
})
</script>

<template>
  <div class="app-shell" :class="{ 'sidebar-collapsed': sidebarCollapsed, 'sidebar-open': mobileSidebarOpen }">
    <button v-if="mobileSidebarOpen" class="sidebar-backdrop" aria-label="关闭导航菜单" @click="mobileSidebarOpen = false" />

    <aside class="app-sidebar">
      <div class="brand">
        <span class="brand-mark"><el-icon><Monitor /></el-icon></span>
        <div class="brand-copy"><strong>M200 Agent</strong><small>本地智能工作台</small></div>
      </div>

      <nav class="side-nav" aria-label="功能导航">
        <span class="nav-section-label">核心功能</span>
        <button class="nav-item" aria-label="对话" title="对话" :class="{ active: activeTab === 'chat' }" @click="changeTab('chat')"><el-icon><ChatDotRound /></el-icon><span>对话</span></button>
        <button class="nav-item" aria-label="模型管理" title="模型管理" :class="{ active: activeTab === 'models' }" @click="changeTab('models')"><el-icon><Setting /></el-icon><span>模型管理</span></button>
        <button class="nav-item" aria-label="知识库" title="知识库" :class="{ active: activeTab === 'knowledge' }" @click="changeTab('knowledge')"><el-icon><Collection /></el-icon><span>知识库</span></button>
        <button class="nav-item" aria-label="长期记忆" title="长期记忆" :class="{ active: activeTab === 'memory' }" @click="changeTab('memory')"><el-icon><Memo /></el-icon><span>长期记忆</span></button>
        <button class="nav-item" aria-label="人格管理" title="人格管理" :class="{ active: activeTab === 'personas' }" @click="changeTab('personas')"><el-icon><UserFilled /></el-icon><span>人格管理</span></button>

        <button class="nav-group" :aria-expanded="extensionExpanded" @click="extensionExpanded = !extensionExpanded">
          <span><el-icon><MagicStick /></el-icon><span>扩展管理</span></span>
          <el-icon class="group-arrow" :class="{ expanded: extensionExpanded }"><Expand /></el-icon>
        </button>
        <div v-show="sidebarCollapsed || extensionExpanded" class="nav-children">
          <button class="nav-item" aria-label="工具管理" title="工具管理" :class="{ active: activeTab === 'tools' }" @click="changeTab('tools')"><el-icon><Tools /></el-icon><span>工具管理</span></button>
          <button class="nav-item" aria-label="技能管理" title="技能管理" :class="{ active: activeTab === 'skills' }" @click="changeTab('skills')"><el-icon><MagicStick /></el-icon><span>技能管理</span></button>
        </div>

        <button class="nav-item nav-standalone" aria-label="任务中心" title="任务中心" :class="{ active: activeTab === 'tasks' }" @click="changeTab('tasks')"><el-icon><PictureFilled /></el-icon><span>任务中心</span></button>

        <button class="nav-group" :aria-expanded="systemExpanded" @click="systemExpanded = !systemExpanded">
          <span><el-icon><Setting /></el-icon><span>系统管理</span></span>
          <el-icon class="group-arrow" :class="{ expanded: systemExpanded }"><Expand /></el-icon>
        </button>
        <div v-show="sidebarCollapsed || systemExpanded" class="nav-children">
          <button class="nav-item" aria-label="管理员" title="管理员" :class="{ active: activeTab === 'admin' }" @click="changeTab('admin')"><el-icon><UserFilled /></el-icon><span>管理员</span></button>
          <button class="nav-item" aria-label="系统状态" title="系统状态" :class="{ active: activeTab === 'status' }" @click="changeTab('status')"><el-icon><DataAnalysis /></el-icon><span>系统状态</span></button>
        </div>
      </nav>

      <div class="sidebar-footer">
        <span class="version-dot" />
        <div><strong>本机模式</strong><small>仅监听回环地址</small></div>
      </div>
    </aside>

    <div class="app-main">
      <header class="topbar">
        <div class="topbar-left">
          <button class="icon-button" :aria-label="sidebarToggleLabel" @click="toggleSidebar">
            <el-icon><Menu v-if="isNarrow" /><Expand v-else-if="sidebarCollapsed" /><Fold v-else /></el-icon>
          </button>
          <div><span class="topbar-kicker">M200 AGENT</span><strong>{{ currentPage.title }}</strong></div>
        </div>
        <div class="topbar-actions">
          <el-tag round effect="light" :type="statusType(health.status)"><span class="status-pulse" />{{ statusLabel(health.status) }}</el-tag>
          <button class="icon-button" :aria-label="theme === 'dark' ? '切换到浅色主题' : '切换到深色主题'" @click="toggleTheme">
            <el-icon><Sunny v-if="theme === 'dark'" /><Moon v-else /></el-icon>
          </button>
        </div>
      </header>

      <main class="content-area">
        <div class="page-heading">
          <div><span class="eyebrow">本地智能工作台</span><h1>{{ currentPage.title }}</h1><p>{{ currentPage.description }}</p></div>
        </div>

        <section v-if="activeTab === 'chat'" class="chat-grid" :class="{ 'conversation-hidden': !conversationListOpen }">
          <aside class="panel conversation-panel">
            <div class="panel-heading"><div><span class="section-kicker">会话</span><h2>最近对话</h2></div><el-button type="primary" @click="createConversation">新建</el-button></div>
            <div class="conversation-list">
              <div v-for="item in conversations" :key="item.id" class="conversation-row">
                <button class="conversation" :class="{ active: item.id === currentConversationId }" @click="currentConversationId = item.id; loadMessages()">
                  <span class="conversation-icon"><el-icon><ChatDotRound /></el-icon></span><span><strong>{{ item.title }}</strong><small>{{ item.model_alias }} · {{ item.platform === 'web' ? '网页' : 'QQ' }}</small></span>
                </button>
                <button v-if="item.platform === 'web'" class="conversation-delete" aria-label="删除网页会话" title="删除网页会话" :disabled="sending && item.id === currentConversationId" @click="deleteConversation(item)"><el-icon><Delete /></el-icon></button>
              </div>
              <div v-if="!conversations.length" class="empty-copy">还没有会话，点击“新建”开始对话。</div>
            </div>
          </aside>
          <section class="panel chat-panel">
            <div class="chat-toolbar">
              <div class="chat-title"><button class="icon-button conversation-toggle" aria-label="显示或隐藏会话列表" @click="conversationListOpen = !conversationListOpen"><el-icon><Menu /></el-icon></button><span><span class="section-kicker">当前会话</span><strong>{{ currentConversation?.title || '未选择会话' }}</strong></span></div>
              <div class="toolbar-selects">
                <el-select :model-value="currentConversation?.model_alias" placeholder="选择模型" @change="switchModel"><el-option v-for="model in models" :key="model.alias" :label="`${model.alias}${model.configured ? '' : '（未配置）'}`" :value="model.alias" /></el-select>
                <el-select :model-value="currentConversation?.persona_id || ''" placeholder="选择人格" @change="switchPersona"><el-option label="关闭人格" value="" /><el-option v-for="item in personas" :key="item.id" :label="item.name" :value="item.id" /></el-select>
              </div>
            </div>
            <div ref="messagesContainer" class="messages" @scroll="handleMessagesScroll">
              <div v-if="!messages.length" class="chat-empty"><span class="empty-orb"><el-icon><ChatDotRound /></el-icon></span><h3>开始一段新对话</h3><p>选择模型和人格，然后输入你的问题。</p></div>
              <article v-for="(message, index) in messages" :key="message.id || index" :class="['message', message.role]"><span>{{ message.role === 'user' ? '你' : 'M200 Agent' }}</span><p>{{ message.content }}</p></article>
            </div>
            <div class="composer"><el-input v-model="input" type="textarea" :rows="3" resize="none" placeholder="输入消息，Ctrl+Enter 发送" @keydown.ctrl.enter.prevent="send" /><el-button type="primary" :loading="sending" @click="send">发送</el-button></div>
          </section>
        </section>

        <template v-else-if="activeTab === 'models'">
          <div class="model-management">
            <section class="panel stack model-catalog">
              <div class="panel-heading">
                <div><span class="section-kicker">配置列表</span><h2>主聊天模型</h2></div>
                <el-button type="primary" @click="startNewModel">新增配置</el-button>
              </div>
              <p class="hint">模型配置只用于主聊天链路及其摘要、记忆处理；API Key 不会在列表中显示。</p>
              <div class="model-list">
                <div
                  v-for="item in models"
                  :key="item.alias"
                  type="button"
                  class="model-list-item"
                  role="button"
                  tabindex="0"
                  :class="{ active: item.alias === editingModelAlias }"
                  @click="selectModel(item)"
                  @keydown.enter="selectModel(item)"
                >
                  <span class="model-list-icon"><el-icon><Setting /></el-icon></span>
                  <span class="model-list-copy"><strong>{{ item.alias }}</strong><small>{{ item.model }}</small><small>{{ item.base_url }}</small></span>
                  <span class="model-list-state"><el-tag size="small" :type="item.configured ? 'success' : 'warning'">{{ item.configured ? '已配置' : '未配置' }}</el-tag><el-tag v-if="item.is_default" size="small" type="primary">主默认</el-tag><el-tag v-else-if="item.in_use" size="small" type="info">使用中</el-tag><el-button v-if="!item.is_default" size="small" text type="primary" :disabled="!item.configured" @click.stop="makeDefaultModel(item)">设为默认</el-button></span>
                </div>
                <div v-if="!models.length" class="empty-copy">还没有模型配置。</div>
              </div>
            </section>

            <section class="panel stack model-editor">
              <div class="panel-heading">
                <div><span class="section-kicker">参数编辑</span><h2>{{ editingModelAlias ? `编辑 ${editingModelAlias}` : '新增模型配置' }}</h2></div>
                <el-tag v-if="editingModel?.has_api_key" type="success" effect="plain">密钥已配置</el-tag>
              </div>
              <div class="model-form-grid">
                <label class="field-control"><span>模型别名</span><el-input v-model="modelForm.alias" placeholder="例如 default" :disabled="Boolean(editingModelAlias)" /></label>
                <label class="field-control"><span>模型名称</span><el-input v-model="modelForm.model" placeholder="服务商提供的 model 名称" /></label>
                <label class="field-control field-wide"><span>base_url</span><el-input v-model="modelForm.base_url" placeholder="https://provider.example/v1" /></label>
                <label class="field-control field-wide"><span>API Key</span><el-input v-model="modelForm.api_key" type="password" show-password autocomplete="new-password" :placeholder="editingModel?.has_api_key ? '已配置，留空表示不修改' : '只在本机 .env 保存'" /></label>
                <div class="field-control"><span>思考强度</span><el-select v-model="modelForm.reasoning_effort" clearable placeholder="关闭"><el-option label="关闭" :value="null" /><el-option label="低" value="low" /><el-option label="中" value="medium" /><el-option label="高" value="high" /></el-select></div>
                <div class="field-control"><span>流式输出</span><el-switch v-model="modelForm.streaming" active-text="开启" inactive-text="关闭" /></div>
                <div class="field-control"><span>温度</span><el-input-number v-model="modelForm.temperature" :min="0" :max="2" :step="0.1" :precision="2" controls-position="right" placeholder="服务默认" /></div>
              </div>
              <div class="advanced-heading"><span>高级参数</span><small>用于上下文和请求限制</small></div>
              <div class="model-form-grid advanced-fields">
                <label class="field-control"><span>上下文窗口</span><el-input-number v-model="modelForm.context_window" :min="1" controls-position="right" /></label>
                <label class="field-control"><span>输入软限制</span><el-input-number v-model="modelForm.input_soft_limit" :min="1" controls-position="right" /></label>
                <label class="field-control"><span>最大输出 Token</span><el-input-number v-model="modelForm.max_output_tokens" :min="1" controls-position="right" /></label>
                <label class="field-control"><span>超时时间（秒）</span><el-input-number v-model="modelForm.timeout_seconds" :min="1" :step="1" controls-position="right" /></label>
              </div>
              <p class="model-security-note">保存后配置立即对下一次请求生效。API Key 只写入本机被忽略的 <code>.env</code>，不会返回到页面或写入数据库。</p>
              <div class="form-row model-actions"><el-button type="primary" :loading="modelSaving" @click="saveModel">保存配置</el-button><el-button :loading="modelTesting" @click="testModel">测试连接</el-button><el-button v-if="editingModelAlias" :disabled="!editingModel?.has_api_key" @click="clearModelKey">清除密钥</el-button><el-button v-if="editingModelAlias" type="danger" plain :disabled="!editingModel?.can_delete" @click="deleteModel">删除配置</el-button><el-button v-if="!editingModelAlias" @click="startNewModel">重置</el-button></div>
            </section>
          </div>
        </template>

        <template v-else-if="activeTab === 'knowledge'">
          <section class="panel stack action-panel"><div class="panel-heading"><div><span class="section-kicker">创建</span><h2>知识库配置</h2></div></div><div class="form-row"><el-input v-model="kbName" placeholder="知识库名称" /><el-select v-model="kbEmbedding"><el-option label="本地 BGE" value="local-bge" /><el-option label="在线 Embedding" value="online" /></el-select><el-button type="primary" @click="createKb">创建知识库</el-button></div></section>
          <section class="panel stack"><div class="panel-heading"><div><span class="section-kicker">文档</span><h2>索引管理</h2></div><el-tag v-if="hasIndexingDocuments" type="warning" effect="plain">索引状态自动刷新中</el-tag></div><div class="form-row"><el-select v-model="selectedKb" placeholder="选择知识库" @change="loadDocuments"><el-option v-for="kb in knowledgeBases" :key="kb.id" :label="`${kb.name} · ${kb.embedding_profile}`" :value="kb.id" /></el-select><label class="file-picker"><input type="file" accept=".txt,.md,.markdown,.html,.htm,.pdf,.docx" @change="uploadFile = ($event.target as HTMLInputElement).files?.[0] || null" /><span>{{ uploadFile?.name || '选择文档' }}</span></label><el-button :disabled="!uploadFile || !selectedKb" @click="upload">上传并索引</el-button><el-button :disabled="!selectedKb" @click="rebuildKb">重建索引</el-button><el-button type="danger" plain :disabled="!selectedKb" @click="deleteKb">删除知识库</el-button></div><div class="table-wrap"><el-table :data="documents"><el-table-column prop="filename" label="文件" min-width="220" /><el-table-column label="状态" width="120"><template #default="scope"><el-tag :type="scope.row.status === 'ready' ? 'success' : scope.row.status === 'failed' ? 'danger' : 'warning'">{{ scope.row.status }}</el-tag></template></el-table-column><el-table-column prop="error" label="错误" min-width="220" /><el-table-column label="操作" width="190"><template #default="scope"><el-button size="small" :loading="reindexingDocumentIds.includes(scope.row.id)" :disabled="['queued', 'indexing'].includes(scope.row.status)" @click="reindexDocument(scope.row.id)">重新索引</el-button><el-button size="small" type="danger" plain @click="deleteDocument(scope.row.id)">删除</el-button></template></el-table-column></el-table></div></section>
        </template>

        <template v-else-if="activeTab === 'memory'">
          <section class="panel stack"><div class="panel-heading"><div><span class="section-kicker">筛选</span><h2>长期记忆</h2></div></div><div class="form-row"><el-select v-model="memoryScope" @change="loadMemoryTasks"><el-option label="全部" value="all" /><el-option label="全局记忆" value="global" /><el-option label="指定用户" value="user" /></el-select><el-input v-model="memoryUserId" placeholder="QQ 用户 ID（可选）" @change="loadMemoryTasks" /><el-select v-model="memoryStatus" @change="loadMemoryTasks"><el-option label="有效" value="active" /><el-option label="已归档" value="archived" /><el-option label="全部状态" value="" /></el-select></div><div class="table-wrap"><el-table :data="memories"><el-table-column prop="fact_key" label="事实键" min-width="180" /><el-table-column prop="content" label="内容" min-width="320" /><el-table-column prop="status" label="状态" width="110" /><el-table-column label="操作" width="230"><template #default="scope"><el-button size="small" @click="editMemory(scope.row)">编辑</el-button><el-button size="small" @click="archiveMemory(scope.row.id)">归档</el-button><el-button size="small" type="danger" plain @click="deleteMemory(scope.row.id)">删除</el-button></template></el-table-column></el-table></div></section>
        </template>

        <template v-else-if="activeTab === 'personas'">
          <div class="two-column"><section class="panel stack"><div class="panel-heading"><div><span class="section-kicker">配置</span><h2>{{ editingPersonaId ? '编辑人格' : '新建人格' }}</h2></div></div><el-input v-model="personaName" placeholder="人格名称" /><el-input v-model="personaPrompt" type="textarea" :rows="10" maxlength="8000" show-word-limit placeholder="描述角色身份、语气、称呼、详细程度和格式；不能修改权限、工具或系统规则" /><div class="form-row"><el-button type="primary" @click="savePersona">保存人格</el-button><el-button @click="personaName = ''; personaPrompt = ''; editingPersonaId = ''">清空</el-button></div></section><section class="panel stack"><div class="panel-heading"><div><span class="section-kicker">列表</span><h2>已保存人格</h2></div><span class="count-badge">{{ personas.length }}</span></div><div class="card-list"><div v-for="item in personas" :key="item.id" class="result"><span class="list-icon"><el-icon><UserFilled /></el-icon></span><strong>{{ item.name }}</strong><div><el-button size="small" @click="editPersona(item)">编辑</el-button><el-button size="small" type="danger" plain @click="deletePersona(item)">删除</el-button></div></div><div v-if="!personas.length" class="empty-copy">还没有已保存人格。</div></div></section></div>
        </template>

        <template v-else-if="activeTab === 'tools'">
          <div class="two-column tools-layout"><section class="panel stack"><div class="panel-heading"><div><span class="section-kicker">扩展</span><h2>工具管理</h2></div><span class="count-badge">{{ tools.length }}</span></div><p class="hint">导入的 Python Tool 在后端进程内执行，拥有本机代码权限；默认停用且不会自动安装依赖。</p><label class="file-picker"><input type="file" accept=".zip" @change="importExtension('tools', $event)" /><span>导入工具 ZIP</span></label><div class="form-row"><el-input v-model="githubUrl" placeholder="公开 GitHub 仓库地址" /><el-button @click="importGithub('tools')">导入 GitHub 工具</el-button></div><div class="table-wrap"><el-table :data="tools"><el-table-column label="工具" min-width="220"><template #default="scope"><div class="table-primary"><strong>{{ scope.row.name }}</strong><small>{{ scope.row.description }}</small></div></template></el-table-column><el-table-column prop="status" label="状态" width="90" /><el-table-column label="操作" width="160"><template #default="scope"><el-button size="small" @click="setExtension('tools', scope.row, !scope.row.enabled)">{{ scope.row.enabled ? '停用' : '启用' }}</el-button><el-button size="small" type="danger" plain :disabled="scope.row.builtin" @click="deleteExtension('tools', scope.row)">删除</el-button></template></el-table-column></el-table></div></section><section class="panel stack manga-panel"><div class="panel-heading"><div><span class="section-kicker">内置工具</span><h2>漫画搜索</h2></div><span class="manga-mark">JM</span></div><p class="hint">Owner 点击后立即创建下载任务，无需二次确认。</p><div class="form-row"><el-input v-model="mangaQuery" placeholder="输入漫画关键词" /><el-button type="primary" @click="searchManga">搜索</el-button></div><div class="card-list"><div v-for="item in mangaResults" :key="item.album_id" class="result"><span><small>JM{{ item.album_id }}</small><strong>{{ item.title }}</strong></span><el-button size="small" @click="requestDownload(item.album_id)">立即下载</el-button></div><div v-if="!mangaResults.length" class="empty-copy">搜索结果会显示在这里。</div></div></section></div>
        </template>

        <template v-else-if="activeTab === 'skills'">
          <section class="panel stack"><div class="panel-heading"><div><span class="section-kicker">扩展</span><h2>技能管理</h2></div><span class="count-badge">{{ skills.length }}</span></div><p class="hint">只读取 SKILL.md 和根目录内的 references/assets；scripts 仅展示，不执行。每轮最多加载 3 个。</p><div class="form-row"><label class="file-picker"><input type="file" accept=".zip" @change="importExtension('skills', $event)" /><span>导入技能 ZIP</span></label><el-input v-model="githubUrl" placeholder="公开 GitHub Skill 地址" /><el-button @click="importGithub('skills')">导入 GitHub 技能</el-button></div><div class="table-wrap"><el-table :data="skills"><el-table-column prop="name" label="名称" min-width="160" /><el-table-column prop="description" label="说明" min-width="320" /><el-table-column prop="status" label="状态" width="110" /><el-table-column label="操作" width="190"><template #default="scope"><el-button size="small" @click="setExtension('skills', scope.row, !scope.row.enabled)">{{ scope.row.enabled ? '停用' : '启用' }}</el-button><el-button size="small" type="danger" plain @click="deleteExtension('skills', scope.row)">删除</el-button></template></el-table-column></el-table></div></section>
        </template>

        <template v-else-if="activeTab === 'tasks'">
          <section class="panel stack">
            <div class="panel-heading">
              <div><span class="section-kicker">授权记录</span><h2>确认操作</h2></div>
              <div class="heading-actions"><span class="count-badge">{{ confirmations.length }}</span><el-button size="small" type="danger" plain :disabled="!selectedConfirmationTokens.length" @click="deleteSelectedConfirmations">删除选中</el-button></div>
            </div>
            <p class="hint">待确认请求可以直接删除使其失效；已处理记录也可批量清理。</p>
            <el-checkbox-group v-model="selectedConfirmationTokens" class="record-list">
              <div v-for="item in confirmations" :key="item.token" class="result record-row">
                <el-checkbox :label="item.token"><code>{{ item.token }}</code></el-checkbox>
                <span>{{ item.action }}</span>
                <el-tag size="small" :type="item.status === 'pending' ? 'warning' : 'info'">{{ item.status === 'pending' ? '待确认' : item.status }}</el-tag>
                <div><el-button v-if="item.status === 'pending'" size="small" type="primary" @click="resolve(item.token, true)">确认</el-button><el-button v-if="item.status === 'pending'" size="small" @click="resolve(item.token, false)">拒绝</el-button></div>
              </div>
            </el-checkbox-group>
            <div v-if="!confirmations.length" class="empty-copy compact">当前没有确认记录。</div>
          </section>
          <section class="panel stack">
            <div class="panel-heading">
              <div><span class="section-kicker">后台任务</span><h2>任务列表</h2></div>
              <div class="heading-actions"><span class="count-badge">{{ tasks.length }}</span><el-button size="small" type="danger" plain :disabled="!selectedTaskIds.length" @click="deleteSelectedTasks">删除选中</el-button></div>
            </div>
            <p class="hint">仅成功、失败、已取消任务可删除；排队中和运行中任务必须先完成或取消。本地下载文件不会因删除记录而删除。</p>
            <div class="table-wrap"><el-table :data="tasks" @selection-change="taskSelectionChange"><el-table-column type="selection" width="48" :selectable="taskSelectable" /><el-table-column prop="id" label="ID" min-width="210" /><el-table-column prop="type" label="类型" min-width="150" /><el-table-column prop="status" label="状态" width="110" /><el-table-column prop="result.delivery_status" label="QQ 发送" width="120" /><el-table-column prop="error" label="错误" min-width="200" /><el-table-column label="操作" width="250"><template #default="scope"><el-button v-if="['queued', 'running'].includes(scope.row.status)" size="small" @click="cancelTask(scope.row.id)">取消</el-button><el-link v-if="scope.row.status === 'succeeded' && !scope.row.result?.artifact_deleted" :href="`${API}/tasks/${scope.row.id}/artifact`" target="_blank" type="primary">下载产物</el-link><el-button v-if="scope.row.status === 'succeeded' && scope.row.type === 'manga_download' && !scope.row.result?.artifact_deleted" size="small" type="danger" plain @click="deleteTaskArtifact(scope.row)">删除本地文件</el-button><el-tag v-if="scope.row.result?.artifact_deleted" type="info">已删除</el-tag></template></el-table-column></el-table></div>
          </section>
        </template>

        <template v-else-if="activeTab === 'admin'">
          <section class="panel stack"><div class="panel-heading"><div><span class="section-kicker">权限</span><h2>QQ Owner</h2></div><span class="count-badge">{{ admins.length }}</span></div><p class="hint">local-owner 永久存在且不可删除；这里的变更会立即生效。</p><div class="form-row"><el-input v-model="adminQq" placeholder="QQ 号" /><el-input v-model="adminName" placeholder="备注（可选）" /><el-button type="primary" @click="addAdmin">添加 Owner</el-button></div><div class="table-wrap"><el-table :data="admins"><el-table-column prop="external_id" label="身份" min-width="180" /><el-table-column prop="display_name" label="备注" min-width="180" /><el-table-column prop="platform" label="平台" width="130" /><el-table-column label="操作" width="120"><template #default="scope"><el-button size="small" type="danger" plain :disabled="scope.row.external_id === 'local-owner'" @click="removeAdmin(scope.row)">删除</el-button></template></el-table-column></el-table></div></section>
        </template>

        <template v-else-if="activeTab === 'status'">
          <div class="status-grid"><article class="status-card"><span>服务状态</span><el-icon><Monitor /></el-icon><strong>{{ statusLabel(health.status) }}</strong><el-tag :type="statusType(health.status)">{{ health.status || 'loading' }}</el-tag></article><article class="status-card"><span>数据库</span><el-icon><DataAnalysis /></el-icon><strong>{{ statusLabel(health.database) }}</strong><el-tag :type="statusType(health.database)">{{ health.database || 'unknown' }}</el-tag></article><article class="status-card"><span>向量索引</span><el-icon><Collection /></el-icon><strong>{{ statusLabel(health.chroma) }}</strong><el-tag :type="statusType(health.chroma)">{{ health.chroma || 'unknown' }}</el-tag></article><article class="status-card"><span>任务 Worker</span><el-icon><Setting /></el-icon><strong>{{ statusLabel(health.worker) }}</strong><el-tag :type="statusType(health.worker)">{{ health.worker || 'unknown' }}</el-tag></article><article class="status-card"><span>OneBot</span><el-icon><ChatDotRound /></el-icon><strong>{{ statusLabel(health.onebot) }}</strong><el-tag :type="statusType(health.onebot)">{{ health.onebot || 'unknown' }}</el-tag></article><article class="status-card"><span>Reranker</span><el-icon><MagicStick /></el-icon><strong>{{ statusLabel(health.reranker) }}</strong><el-tag :type="statusType(health.reranker)">{{ health.reranker || 'unknown' }}</el-tag></article></div>
          <div class="two-column status-detail-grid"><section class="panel stack"><div class="panel-heading"><div><span class="section-kicker">模型</span><h2>模型配置</h2></div></div><div class="card-list"><div v-for="item in health.models || []" :key="item.alias" class="result"><span><strong>{{ item.alias }}</strong><small>{{ item.model }}</small></span><el-tag :type="item.configured ? 'success' : 'warning'">{{ item.configured ? '已配置' : '未配置' }}</el-tag></div></div></section><section class="panel stack"><div class="panel-heading"><div><span class="section-kicker">检索</span><h2>Embedding 配置</h2></div></div><div class="card-list"><div v-for="item in health.embedding_profiles || []" :key="item.alias" class="result"><span><strong>{{ item.alias }}</strong><small>{{ item.model }}</small></span><el-tag :type="item.configured ? 'success' : 'warning'">{{ item.configured ? '已配置' : '未配置' }}</el-tag></div></div></section></div>
          <section class="panel raw-status"><el-collapse><el-collapse-item title="查看原始运行详情" name="raw"><pre>{{ JSON.stringify(health, null, 2) }}</pre></el-collapse-item></el-collapse></section>
        </template>
      </main>
    </div>
  </div>
</template>
