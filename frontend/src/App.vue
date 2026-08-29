<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import {
  ChatDotRound, Collection, DataAnalysis, Delete, Expand, Fold, MagicStick, Memo, Menu,
  Monitor, Moon, PictureFilled, Setting, Sunny, Tools, UserFilled, Bell, CircleCheck, Refresh,
  VideoPause, VideoPlay, Warning,
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
type CompanionPreferenceRow = {
  scope_id: string
  companion_enabled: boolean
  support_mode: 'auto' | 'listen' | 'reflect' | 'advice'
  memory_enabled: boolean
  safety_mode: 'standard' | 'unfiltered'
  analyzer_model_alias: string | null
  boundaries: Record<string, unknown>
  updated_at?: string | null
}
type RelationshipRow = {
  id?: string
  scope_id: string
  persona_key: string
  persona_id?: string | null
  nickname: string | null
  shared_summary: string
  boundaries: Record<string, unknown>
  version: number
  updated_at?: string
}
type EmotionAssessmentRow = {
  id: string
  user_message_id: string
  candidate_emotions: string[]
  candidate_emotions_display: string[]
  primary_emotion: string | null
  primary_emotion_display: string | null
  effective_candidate_emotions?: string[]
  effective_candidate_emotions_display?: string[]
  effective_primary_emotion?: string | null
  effective_primary_emotion_display?: string | null
  intensity: string | null
  support_need: string | null
  support_need_display: string | null
  effective_support_need?: string | null
  effective_support_need_display?: string | null
  confidence: number | null
  risk_level: string
  next_action: string
  next_action_display: string
  model_alias?: string | null
  schema_valid: boolean
  analysis_status: 'valid' | 'retrying' | 'failed' | 'safety_redirected'
  correction: { emotions?: string[]; support_need?: string | null; note?: string | null }
  created_at: string
}
type CompanionFeedbackRow = { id: string; assistant_message_id: string; feedback: string; correction: { note?: string }; created_at: string; updated_at: string }
type CompanionSafetyRow = { id: string; message_id: string; risk_level: string; action: string; detector_version: string; details: { rules?: string[]; source?: string; safety_mode?: string; response_source?: string; violation_codes?: string[] }; created_at: string }
type HealthItem = { alias?: string; model?: string; configured?: boolean }
type LogProgress = { current?: number; total?: number; percent?: number; unit?: string }
type LogEvent = {
  id: string; timestamp: string; session_id: string; source: string; level: string; kind: string
  title: string; message: string; details?: unknown; operation_id?: string; trace_id?: string
  parent_operation_id?: string; progress?: LogProgress
}
type NapcatStatus = { url: string; configured: boolean; status: string; two_factor: boolean; last_error?: string | null; last_log_at?: string | null }
type ActiveLogResponse = { session_id: string; operations: LogEvent[]; napcat: NapcatStatus; onebot: { connection: string; qq: string; self_id?: string | null; nickname?: string | null } }
type HealthData = {
  status?: string
  database?: string
  chroma?: string
  worker?: string
  onebot?: string
  reranker?: string
  qq?: string
  napcat?: NapcatStatus
  logs?: { session_id?: string; events?: number }
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
const companionOwnerId = ref('')
const companionPreferences = ref<CompanionPreferenceRow | null>(null)
const companionRelationships = ref<RelationshipRow[]>([])
const companionPersonaKey = ref('default')
const companionAssessments = ref<EmotionAssessmentRow[]>([])
const companionFeedback = ref<CompanionFeedbackRow[]>([])
const companionSafetyEvents = ref<CompanionSafetyRow[]>([])
const companionCorrectionDrafts = ref<Record<string, { emotions: string[]; support_need: string; note: string }>>({})
const companionBoundaryText = ref('{}')
const companionLoading = ref(false)
const companionSaving = ref(false)
const companionSavedSafetyMode = ref<'standard' | 'unfiltered'>('standard')
const companionPrivacyDeleting = ref(false)
const companionDeleteCategories = ref<string[]>(['relationships', 'assessments', 'feedback', 'safety', 'preferences'])
const companionDeleteConfirm = ref('')
const companionExpanded = ref(true)
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
const logTab = ref<'operation' | 'napcat'>('operation')
const logEvents = ref<LogEvent[]>([])
const activeLogOperations = ref<LogEvent[]>([])
const logSourceFilter = ref('')
const logLevelFilter = ref('')
const logQuery = ref('')
const logAutoScroll = ref(true)
const logDisplayPaused = ref(false)
const expandedLogIds = ref<string[]>([])
const logsContainer = ref<HTMLElement | null>(null)
const napcatConfig = ref<NapcatStatus>({ url: 'http://127.0.0.1:6099', configured: false, status: 'not_configured', two_factor: false })
const napcatToken = ref('')
const napcatSaving = ref(false)
const napcatTesting = ref(false)
let logEventSource: EventSource | null = null
const theme = ref<ThemeName>('light')
let followsSystemTheme = false
let systemThemeQuery: MediaQueryList | undefined
const currentConversation = computed(() => conversations.value.find((item) => item.id === currentConversationId.value))
const hasIndexingDocuments = computed(() => documents.value.some((item) => ['queued', 'indexing'].includes(item.status)))
const companionOwners = computed(() => admins.value.filter((item) => item.platform === 'qq' && item.enabled && /^\d{5,20}$/.test(item.external_id)))
const companionPersonas = computed(() => [{ id: 'default', name: '默认人格' }, ...personas.value.map((item) => ({ id: item.id, name: item.name }))])
const companionAnalyzerFallbackAlias = computed(() => {
  const selected = companionPreferences.value?.analyzer_model_alias
  if (!selected) return ''
  const profile = models.value.find((item) => item.alias === selected)
  if (profile?.configured) return ''
  return models.value.find((item) => item.is_default)?.alias || '当前会话主模型'
})
const companionEmotionLabels: Record<string, string> = {
  joy: '开心', excitement: '兴奋', relief: '如释重负', gratitude: '感激', pride: '自豪', hope: '希望',
  sadness: '难过', loneliness: '孤独', anxiety: '焦虑', fear: '害怕', anger: '生气', frustration: '挫败',
  disappointment: '失望', guilt: '内疚', shame: '羞愧', helplessness: '无力', exhaustion: '疲惫', confusion: '困惑',
  calm: '平静', neutral: '平淡',
}
const companionEmotionOptions = Object.entries(companionEmotionLabels).map(([value, label]) => ({ value, label }))
const companionSupportNeedLabels: Record<string, string> = { listen: '倾听', comfort: '安慰', reflect: '一起梳理', advice: '建议', celebrate: '庆祝', space: '留一点空间', unknown: '尚不确定' }
const companionAnalysisStatusLabels: Record<EmotionAssessmentRow['analysis_status'], string> = { valid: '有效', retrying: '重试中', failed: '分析失败', safety_redirected: '安全转向' }
const companionAnalysisStatusTypes: Record<EmotionAssessmentRow['analysis_status'], 'success' | 'warning' | 'danger'> = { valid: 'success', retrying: 'warning', failed: 'danger', safety_redirected: 'warning' }
function companionAnalysisStatusLabel(status: string) { return companionAnalysisStatusLabels[status as EmotionAssessmentRow['analysis_status']] || '未知' }
function companionAnalysisStatusType(status: string) { return companionAnalysisStatusTypes[status as EmotionAssessmentRow['analysis_status']] || 'danger' }
const DOCUMENT_REFRESH_INTERVAL_MS = 1000
let documentRefreshTimer: number | undefined
const routePaths: Record<string, string> = {
  chat: '/chat', models: '/models', knowledge: '/knowledge', tools: '/tools', skills: '/skills',
  personas: '/personas', memory: '/memories', companion: '/companion', relationships: '/relationships',
  emotionRecords: '/emotion-records', privacy: '/privacy', admin: '/admin', tasks: '/tasks', status: '/status', logs: '/logs',
}
const pageDetails: Record<string, { title: string; description: string }> = {
  chat: { title: '对话', description: '与 M200 Agent 对话并管理会话模型和人格' },
  models: { title: '模型管理', description: '配置主聊天 LLM，并在会话中快捷切换' },
  knowledge: { title: '知识库', description: '管理文档、Embedding 配置和索引状态' },
  memory: { title: '长期记忆', description: '查看和维护全局、用户与群组记忆' },
  companion: { title: '陪伴设置', description: '为已启用的 QQ Owner 管理陪伴流程与记忆授权' },
  relationships: { title: '关系资料', description: '按人格查看、修改或删除已授权的低敏感关系资料' },
  emotionRecords: { title: '情绪记录', description: '查看陪伴分析、支持需求、策略与用户纠正' },
  privacy: { title: '陪伴隐私', description: '导出或按分类删除陪伴数据' },
  personas: { title: '人格管理', description: '创建可按会话切换的原始提示词人格' },
  tools: { title: '工具管理', description: '管理工具扩展并发起漫画搜索与下载' },
  skills: { title: '技能管理', description: '导入、启用和维护 Agent Skills' },
  tasks: { title: '任务中心', description: '处理待确认操作并跟踪后台任务' },
  admin: { title: '管理员', description: '维护拥有高权限操作能力的 QQ Owner' },
  status: { title: '系统状态', description: '查看本地服务、模型和检索组件状态' },
  logs: { title: '实时日志', description: '查看 M200 操作事件与 NapCat 原生日志流' },
}
const currentPage = computed(() => pageDetails[activeTab.value] || pageDetails.chat)
const sidebarToggleLabel = computed(() => isNarrow.value
  ? mobileSidebarOpen.value ? '关闭导航菜单' : '打开导航菜单'
  : sidebarCollapsed.value ? '展开侧边栏' : '折叠侧边栏')
const statusLabels: Record<string, string> = {
  ok: '正常', degraded: '降级', connected: '已连接', unavailable: '不可用', installed: '已安装',
  running: '运行中', stopped: '已停止', configured_disconnected: '已配置未连接',
  needs_configuration: '需要配置', enabled: '已启用', disabled: '已停用', online: 'QQ 在线',
  offline: 'QQ 离线', not_configured: '未配置', connecting: '连接中', auth_failed: '认证失败',
  disconnected: '未连接', error: '错误', unknown: '未知',
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
  if (['degraded', 'configured_disconnected', 'needs_configuration', 'stopped', 'disabled', 'offline', 'not_configured', 'connecting', 'disconnected', 'unknown'].includes(key)) return 'warning'
  if (['unavailable', 'auth_failed', 'error'].includes(key)) return 'danger'
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

const filteredLogEvents = computed(() => {
  const query = logQuery.value.trim().toLocaleLowerCase()
  return logEvents.value.filter((item) => {
    if (logTab.value === 'napcat' ? item.source !== 'napcat' : item.source === 'napcat') return false
    if (logSourceFilter.value && item.source !== logSourceFilter.value) return false
    if (logLevelFilter.value && item.level !== logLevelFilter.value) return false
    if (query && !JSON.stringify(item).toLocaleLowerCase().includes(query)) return false
    return true
  })
})

const logSources = computed(() => Array.from(new Set(logEvents.value.filter((item) => item.source !== 'napcat').map((item) => item.source))).sort())

function logLevelLabel(level: string) {
  return ({ info: '信息', warn: '警告', error: '错误', debug: '调试' } as Record<string, string>)[level] || level
}

function logKindLabel(kind: string) {
  return ({ started: '开始', progress: '进度', succeeded: '成功', failed: '失败', cancelled: '取消', message: '消息', queued: '排队' } as Record<string, string>)[kind] || kind
}

function logTime(value: string) {
  try { return new Date(value).toLocaleTimeString('zh-CN', { hour12: false }) } catch { return value }
}

function logLevelType(level: string): '' | 'success' | 'warning' | 'danger' | 'info' {
  if (level === 'error') return 'danger'
  if (level === 'warn') return 'warning'
  if (level === 'info') return 'success'
  return 'info'
}

function logOperationPercent(item: LogEvent) {
  const percent = Number(item.progress?.percent)
  return Number.isFinite(percent) ? Math.max(0, Math.min(100, percent)) : 0
}

async function scrollLogsToBottom(force = false) {
  await nextTick()
  const element = logsContainer.value
  if (!element || (!force && (!logAutoScroll.value || logDisplayPaused.value))) return
  element.scrollTop = element.scrollHeight
}

function appendLogEvent(item: LogEvent) {
  const index = logEvents.value.findIndex((entry) => entry.id === item.id)
  if (index >= 0) logEvents.value[index] = item
  else logEvents.value.push(item)
  if (logEvents.value.length > 2000) logEvents.value.splice(0, logEvents.value.length - 2000)
  if (item.operation_id && ['started', 'progress'].includes(item.kind)) {
    const operationIndex = activeLogOperations.value.findIndex((entry) => entry.operation_id === item.operation_id)
    if (operationIndex >= 0) activeLogOperations.value[operationIndex] = item
    else activeLogOperations.value.push(item)
  } else if (item.operation_id && ['succeeded', 'failed', 'cancelled', 'finished'].includes(item.kind)) {
    activeLogOperations.value = activeLogOperations.value.filter((entry) => entry.operation_id !== item.operation_id)
  }
  void scrollLogsToBottom()
}

function closeLogStream() {
  logEventSource?.close()
  logEventSource = null
}

function openLogStream() {
  closeLogStream()
  const stream = new EventSource(`${API}/logs/stream`)
  logEventSource = stream
  const receive = (event: MessageEvent<string>) => {
    try { appendLogEvent(JSON.parse(event.data) as LogEvent) } catch { /* 忽略异常 SSE 帧 */ }
  }
  stream.addEventListener('log', receive)
  stream.addEventListener('operation', receive)
  stream.addEventListener('status', (event) => {
    try {
      const data = JSON.parse((event as MessageEvent<string>).data) as { napcat?: NapcatStatus; onebot?: { qq?: string } }
      if (data.napcat) napcatConfig.value = data.napcat
      if (data.onebot?.qq) health.value = { ...health.value, qq: data.onebot.qq }
    } catch { /* 忽略状态帧 */ }
  })
  stream.onerror = () => {
    if (activeTab.value === 'logs') ElMessage.warning('日志流暂时断开，浏览器会自动重连')
  }
}

async function loadLogs() {
  const [history, active, config] = await Promise.all([
    api<{ session_id: string; events: LogEvent[] }>('/logs?limit=2000'),
    api<ActiveLogResponse>('/logs/active'),
    api<{ napcat: NapcatStatus }>('/logs/config'),
  ])
  logEvents.value = history.events || []
  activeLogOperations.value = active.operations || []
  napcatConfig.value = config.napcat
  await scrollLogsToBottom(true)
}

async function enterLogsPage() {
  try {
    await loadLogs()
    openLogStream()
  } catch (error) {
    ElMessage.error(`日志加载失败：${(error as Error).message}`)
  }
}

function toggleLogDetails(id: string) {
  expandedLogIds.value = expandedLogIds.value.includes(id)
    ? expandedLogIds.value.filter((item) => item !== id)
    : [...expandedLogIds.value, id]
}

async function saveNapcatConfig() {
  napcatSaving.value = true
  try {
    const payload: Record<string, unknown> = { url: napcatConfig.value.url }
    if (napcatToken.value.trim()) payload.token = napcatToken.value.trim()
    const result = await api<{ napcat: NapcatStatus }>('/logs/config', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
    napcatConfig.value = result.napcat
    napcatToken.value = ''
    ElMessage.success('NapCat 配置已保存并开始连接')
  } catch (error) {
    ElMessage.error(`NapCat 配置保存失败：${(error as Error).message}`)
  } finally { napcatSaving.value = false }
}

async function testNapcatConfig() {
  napcatTesting.value = true
  try {
    const payload: Record<string, unknown> = { url: napcatConfig.value.url }
    if (napcatToken.value.trim()) payload.token = napcatToken.value.trim()
    await api('/logs/test-connection', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
    ElMessage.success('NapCat 登录和实时日志接口测试成功')
  } catch (error) {
    ElMessage.error(`NapCat 测试失败：${(error as Error).message}`)
  } finally { napcatTesting.value = false }
}

async function clearNapcatToken() {
  if (!window.confirm('确定清除本机保存的 NapCat Token？日志流将断开。')) return
  try {
    const result = await api<{ napcat: NapcatStatus }>('/logs/config', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ clear_token: true }) })
    napcatConfig.value = result.napcat
    napcatToken.value = ''
    ElMessage.success('NapCat Token 已清除')
  } catch (error) { ElMessage.error(`清除 Token 失败：${(error as Error).message}`) }
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
  const previous = activeTab.value
  const route = Object.entries(routePaths).find(([, path]) => window.location.pathname === path)?.[0]
  if (route) activeTab.value = route
  mobileSidebarOpen.value = false
  if (previous === 'logs' && activeTab.value !== 'logs') closeLogStream()
  if (previous !== 'logs' && activeTab.value === 'logs') void enterLogsPage()
}

function changeTab(tab: string | number) {
  const name = String(tab)
  const previous = activeTab.value
  activeTab.value = name
  const path = routePaths[name] || '/chat'
  if (window.location.pathname !== path) window.history.pushState({}, '', path)
  mobileSidebarOpen.value = false
  if (previous === 'logs' && name !== 'logs') closeLogStream()
  if (name === 'logs') void enterLogsPage()
  if (name === 'memory' || name === 'tasks') loadMemoryTasks()
  if (['companion', 'relationships', 'emotionRecords', 'privacy'].includes(name)) void loadCompanionData()
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
  if (!companionOwnerId.value || !companionOwners.value.some((item) => item.external_id === companionOwnerId.value)) {
    companionOwnerId.value = companionOwners.value[0]?.external_id || ''
  }
}

function companionRequestId() {
  return companionOwnerId.value ? `?qq_user_id=${encodeURIComponent(companionOwnerId.value)}` : ''
}

function initializeCompanionCorrections(items: EmotionAssessmentRow[]) {
  companionCorrectionDrafts.value = Object.fromEntries(
    items.map((item) => [
      item.user_message_id,
      {
        emotions: item.correction?.emotions?.length ? item.correction.emotions : item.candidate_emotions,
        support_need: item.correction?.support_need || item.support_need || 'unknown',
        note: item.correction?.note || '',
      },
    ]),
  )
}

async function loadCompanionData() {
  if (!companionOwnerId.value) return
  companionLoading.value = true
  try {
    const scope = companionRequestId()
    const [preference, relationships, assessments, exported] = await Promise.all([
      api<CompanionPreferenceRow>(`/companion/preferences${scope}`),
      api<RelationshipRow[]>(`/companion/relationships${scope}`),
      api<EmotionAssessmentRow[]>(`/companion/assessments${scope}&limit=100`),
      api<{ feedback: CompanionFeedbackRow[]; safety_events: CompanionSafetyRow[] }>(`/companion/privacy/export${scope}`),
    ])
    companionPreferences.value = preference
    companionSavedSafetyMode.value = preference.safety_mode || 'standard'
    companionBoundaryText.value = JSON.stringify(preference.boundaries || {}, null, 2)
    companionRelationships.value = relationships
    companionAssessments.value = assessments
    initializeCompanionCorrections(assessments)
    companionFeedback.value = exported.feedback || []
    companionSafetyEvents.value = exported.safety_events || []
  } catch (error) {
    ElMessage.error(`陪伴数据加载失败：${(error as Error).message}`)
  } finally {
    companionLoading.value = false
  }
}

async function saveCompanionPreferences() {
  if (!companionOwnerId.value || !companionPreferences.value) return
  const requestedSafetyMode = companionPreferences.value.safety_mode || 'standard'
  if (requestedSafetyMode === 'unfiltered' && companionSavedSafetyMode.value !== 'unfiltered') {
    const confirmation = window.prompt('无过滤模式仅关闭本机陪伴内容拦截，仍受权限、工具确认、文件隔离、密钥脱敏和模型服务商规则约束。请输入“启用无过滤模式”确认：')
    if (confirmation !== '启用无过滤模式') {
      companionPreferences.value.safety_mode = companionSavedSafetyMode.value
      ElMessage.warning('未确认，无过滤模式保持关闭')
      return
    }
  }
  let boundaries: Record<string, unknown> = {}
  try {
    boundaries = JSON.parse(companionBoundaryText.value || '{}') as Record<string, unknown>
  } catch {
    ElMessage.warning('边界配置必须是合法 JSON')
    return
  }
  companionSaving.value = true
  try {
    companionPreferences.value = await api<CompanionPreferenceRow>('/companion/preferences', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ qq_user_id: companionOwnerId.value, ...companionPreferences.value, boundaries }),
    })
    companionSavedSafetyMode.value = companionPreferences.value.safety_mode || 'standard'
    companionBoundaryText.value = JSON.stringify(companionPreferences.value.boundaries || {}, null, 2)
    ElMessage.success('陪伴设置已保存')
  } catch (error) {
    ElMessage.error(`陪伴设置保存失败：${(error as Error).message}`)
  } finally {
    companionSaving.value = false
  }
}

function newCompanionRelationship() {
  if (!companionOwnerId.value) return
  if (companionRelationships.value.some((item) => item.persona_key === companionPersonaKey.value)) return
  const persona = companionPersonas.value.find((item) => item.id === companionPersonaKey.value)
  companionRelationships.value.push({
    scope_id: companionOwnerId.value,
    persona_key: companionPersonaKey.value,
    persona_id: companionPersonaKey.value === 'default' ? null : companionPersonaKey.value,
    nickname: null,
    shared_summary: '',
    boundaries: {},
    version: 0,
  })
  ElMessage.info(`已添加${persona?.name || '人格'}资料草稿，保存后生效`)
}

function updateRelationshipBoundaries(item: RelationshipRow, value: string | number | boolean) {
  try {
    item.boundaries = JSON.parse(String(value || '{}')) as Record<string, unknown>
  } catch {
    ElMessage.warning('边界必须是合法 JSON')
  }
}

async function saveCompanionRelationship(item: RelationshipRow) {
  if (!companionOwnerId.value) return
  try {
    const saved = await api<RelationshipRow>(`/companion/relationships/${encodeURIComponent(item.persona_key)}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ qq_user_id: companionOwnerId.value, nickname: item.nickname, shared_summary: item.shared_summary, boundaries: item.boundaries, version: item.version }),
    })
    companionRelationships.value = companionRelationships.value.map((row) => row.persona_key === saved.persona_key ? saved : row)
    ElMessage.success('关系资料已保存')
  } catch (error) {
    ElMessage.error(`关系资料保存失败：${(error as Error).message}`)
    if ((error as Error).message.includes('版本')) await loadCompanionData()
  }
}

async function deleteCompanionRelationship(item: RelationshipRow) {
  if (!companionOwnerId.value || !window.confirm(`确定删除“${item.persona_key === 'default' ? '默认人格' : item.persona_key}”的关系资料？`)) return
  try {
    await api(`/companion/relationships/${encodeURIComponent(item.persona_key)}${companionRequestId()}&version=${item.version}`, { method: 'DELETE' })
    companionRelationships.value = companionRelationships.value.filter((row) => row.persona_key !== item.persona_key)
    ElMessage.success('关系资料已删除')
  } catch (error) {
    ElMessage.error(`关系资料删除失败：${(error as Error).message}`)
  }
}

async function submitCompanionCorrection(item: EmotionAssessmentRow) {
  if (!companionOwnerId.value) return
  const draft = companionCorrectionDrafts.value[item.user_message_id]
  if (!draft?.emotions.length) {
    ElMessage.warning('至少选择一个情绪标签')
    return
  }
  try {
    const updated = await api<EmotionAssessmentRow>(`/companion/assessments/${item.user_message_id}/correction`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ qq_user_id: companionOwnerId.value, emotions: draft.emotions, support_need: draft.support_need, note: draft.note || null }),
    })
    companionAssessments.value = companionAssessments.value.map((row) => row.user_message_id === updated.user_message_id ? updated : row)
    ElMessage.success('情绪纠正已保存，原始分析仍会保留')
  } catch (error) {
    ElMessage.error(`情绪纠正失败：${(error as Error).message}`)
  }
}

async function exportCompanionData() {
  if (!companionOwnerId.value) return
  try {
    const data = await api<Record<string, unknown>>(`/companion/privacy/export${companionRequestId()}`)
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json;charset=utf-8' })
    const link = document.createElement('a')
    link.href = URL.createObjectURL(blob)
    link.download = `companion-${companionOwnerId.value}.json`
    link.click()
    URL.revokeObjectURL(link.href)
    ElMessage.success('陪伴数据已导出')
  } catch (error) {
    ElMessage.error(`数据导出失败：${(error as Error).message}`)
  }
}

async function deleteCompanionData() {
  if (!companionOwnerId.value || !companionDeleteCategories.value.length) return
  if (companionDeleteConfirm.value !== '删除陪伴数据') {
    ElMessage.warning('请输入“删除陪伴数据”确认删除')
    return
  }
  if (!window.confirm('删除后不可恢复，确定继续？')) return
  companionPrivacyDeleting.value = true
  try {
    const result = await api<{ counts: Record<string, number>; results: Record<string, { success: boolean; count: number; error?: string }> }>('/companion/privacy/data', {
      method: 'DELETE', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ qq_user_id: companionOwnerId.value, confirm_text: companionDeleteConfirm.value, categories: companionDeleteCategories.value }),
    })
    companionDeleteConfirm.value = ''
    await loadCompanionData()
    const failed = Object.entries(result.results || {}).filter(([, item]) => !item.success)
    if (failed.length) ElMessage.warning(`已部分删除，${failed.length} 项失败，请重试`)
    else ElMessage.success('所选陪伴数据已删除')
  } catch (error) {
    ElMessage.error(`数据删除失败：${(error as Error).message}`)
  } finally {
    companionPrivacyDeleting.value = false
  }
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
  if (['companion', 'relationships', 'emotionRecords', 'privacy'].includes(activeTab.value)) await loadCompanionData()
  if (activeTab.value === 'logs') await enterLogsPage()
})

onUnmounted(() => {
  window.removeEventListener('popstate', syncRoute)
  window.removeEventListener('resize', handleResize)
  systemThemeQuery?.removeEventListener('change', handleSystemThemeChange)
  stopDocumentRefresh()
  closeLogStream()
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

        <button class="nav-group" :aria-expanded="companionExpanded" @click="companionExpanded = !companionExpanded">
          <span><el-icon><ChatDotRound /></el-icon><span>陪伴管理</span></span>
          <el-icon class="group-arrow" :class="{ expanded: companionExpanded }"><Expand /></el-icon>
        </button>
        <div v-show="sidebarCollapsed || companionExpanded" class="nav-children">
          <button class="nav-item" aria-label="陪伴设置" title="陪伴设置" :class="{ active: activeTab === 'companion' }" @click="changeTab('companion')"><el-icon><Setting /></el-icon><span>陪伴设置</span></button>
          <button class="nav-item" aria-label="关系资料" title="关系资料" :class="{ active: activeTab === 'relationships' }" @click="changeTab('relationships')"><el-icon><UserFilled /></el-icon><span>关系资料</span></button>
          <button class="nav-item" aria-label="情绪记录" title="情绪记录" :class="{ active: activeTab === 'emotionRecords' }" @click="changeTab('emotionRecords')"><el-icon><DataAnalysis /></el-icon><span>情绪记录</span></button>
          <button class="nav-item" aria-label="陪伴隐私" title="陪伴隐私" :class="{ active: activeTab === 'privacy' }" @click="changeTab('privacy')"><el-icon><Delete /></el-icon><span>陪伴隐私</span></button>
        </div>

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
          <button class="nav-item" aria-label="实时日志" title="实时日志" :class="{ active: activeTab === 'logs' }" @click="changeTab('logs')"><el-icon><Bell /></el-icon><span>实时日志</span></button>
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

        <template v-else-if="activeTab === 'companion'">
          <section v-if="!companionOwners.length" class="panel stack">
            <div class="panel-heading"><div><span class="section-kicker">Owner</span><h2>暂无可用 QQ Owner</h2></div></div>
            <p class="hint">请先在“管理员”中添加并启用 QQ Owner；陪伴设置不接受任意输入的 QQ 号。</p>
            <el-button type="primary" @click="changeTab('admin')">前往管理员</el-button>
          </section>
          <div v-else class="two-column">
            <section class="panel stack">
              <div class="panel-heading"><div><span class="section-kicker">身份范围</span><h2>QQ Owner 陪伴</h2></div><div class="form-row"><el-tag :type="companionPreferences?.companion_enabled ? 'success' : 'warning'">{{ companionPreferences?.companion_enabled ? '已启用' : '已暂停' }}</el-tag><el-tag v-if="companionPreferences?.safety_mode === 'unfiltered'" type="danger">无过滤模式</el-tag></div></div>
              <label class="field-control"><span>选择已启用 QQ Owner</span><el-select v-model="companionOwnerId" @change="loadCompanionData"><el-option v-for="item in companionOwners" :key="item.external_id" :label="`${item.display_name || 'QQ Owner'} · ${item.external_id}`" :value="item.external_id" /></el-select></label>
              <el-alert title="仅 Owner 私聊进入陪伴流程；非 Owner 私聊和所有群聊仍使用通用 Agent。" type="info" :closable="false" />
              <div v-if="companionPreferences" class="stack companion-settings">
                <div class="form-row"><span class="setting-label">陪伴流程</span><el-switch v-model="companionPreferences.companion_enabled" active-text="启用" inactive-text="暂停" /></div>
                <label class="field-control"><span>默认支持方式</span><el-select v-model="companionPreferences.support_mode"><el-option label="自动判断" value="auto" /><el-option label="倾听" value="listen" /><el-option label="一起梳理" value="reflect" /><el-option label="建议" value="advice" /></el-select></label>
                <label class="field-control"><span>安全模式</span><el-select v-model="companionPreferences.safety_mode"><el-option label="标准防护" value="standard" /><el-option label="无过滤模式（仅本机拦截关闭）" value="unfiltered" /></el-select></label>
                <el-alert v-if="companionPreferences.safety_mode === 'unfiltered'" type="error" :closable="false" title="无过滤模式已选择" description="仅关闭本机陪伴内容拦截；Owner 权限、Tool 确认、文件隔离、密钥脱敏和模型服务商自身规则仍然有效。保存时需要输入确认文本。" />
                <label class="field-control"><span>情绪分析模型 alias</span><el-select v-model="companionPreferences.analyzer_model_alias" clearable placeholder="跟随当前会话主模型"><el-option v-for="item in models" :key="item.alias" :label="`${item.alias}${item.configured ? '' : '（不可用）'}`" :value="item.alias" /></el-select><small v-if="companionAnalyzerFallbackAlias" class="hint-inline">所选 alias 不可用，当前请求会回退到：{{ companionAnalyzerFallbackAlias }}</small></label>
                <div class="form-row"><span class="setting-label">关系记忆授权</span><el-switch v-model="companionPreferences.memory_enabled" active-text="已授权" inactive-text="关闭" /><small class="hint-inline">关闭时不召回、不新增关系资料。</small></div>
                <label class="field-control"><span>沟通边界（JSON）</span><el-input v-model="companionBoundaryText" type="textarea" :rows="5" placeholder='例如：{"items":["不想被催着给建议"]}' /></label>
                <el-button type="primary" :loading="companionSaving" @click="saveCompanionPreferences">保存陪伴设置</el-button>
              </div>
            </section>
            <section class="panel stack">
              <div class="panel-heading"><div><span class="section-kicker">使用边界</span><h2>当前安全说明</h2></div><el-icon><Warning /></el-icon></div>
              <p class="hint">情绪标签是可纠正的候选分类，不是心理诊断。<template v-if="companionPreferences?.safety_mode === 'unfiltered'">当前无过滤模式只做风险审计，高风险内容仍进入普通 Agent；本机权限和模型服务商规则不变。</template><template v-else>标准模式下，高风险内容会停止普通角色扮演和工具调用，转入安全支持提示。</template></p>
              <div class="card-list"><div class="result"><span><strong>记忆默认状态</strong><small>首次授权前，历史资料保留但当前回合不召回、不写入。</small></span><el-tag type="warning">默认关闭</el-tag></div><div class="result"><span><strong>分析模型不可用</strong><small>自动回退当前会话主模型；仍不可用时使用澄清降级。</small></span><el-tag type="info">自动回退</el-tag></div><div class="result"><span><strong>数据控制</strong><small>可在关系资料和隐私页面修改、导出或分类删除。</small></span><el-button size="small" @click="changeTab('privacy')">管理隐私</el-button></div></div>
            </section>
          </div>
        </template>

        <template v-else-if="activeTab === 'relationships'">
          <section v-if="!companionOwners.length" class="panel stack"><h2>暂无可用 QQ Owner</h2><p class="hint">请先启用 QQ Owner。</p></section>
          <section v-else class="panel stack">
            <div class="panel-heading"><div><span class="section-kicker">关系范围</span><h2>已授权关系资料</h2></div><span class="count-badge">{{ companionRelationships.length }}</span></div>
            <div class="form-row"><el-select v-model="companionOwnerId" @change="loadCompanionData"><el-option v-for="item in companionOwners" :key="item.external_id" :label="`${item.display_name || 'QQ Owner'} · ${item.external_id}`" :value="item.external_id" /></el-select><el-select v-model="companionPersonaKey"><el-option v-for="item in companionPersonas" :key="item.id" :label="item.name" :value="item.id" /></el-select><el-button @click="newCompanionRelationship">添加人格资料</el-button></div>
            <el-alert title="只保存用户明确表达的低敏感事实；诊断、危机细节、凭据、精确位置和第三方隐私不会自动写入。" type="info" :closable="false" />
            <div v-for="item in companionRelationships" :key="item.persona_key" class="relationship-editor">
              <div class="panel-heading"><div><span class="section-kicker">人格</span><h3>{{ companionPersonas.find((persona) => persona.id === item.persona_key)?.name || item.persona_key }}</h3></div><el-tag size="small" type="info">版本 {{ item.version }}</el-tag></div>
              <div class="model-form-grid"><label class="field-control"><span>称呼</span><el-input v-model="item.nickname" placeholder="用户希望的称呼" /></label><label class="field-control field-wide"><span>共同经历摘要</span><el-input v-model="item.shared_summary" type="textarea" :rows="3" maxlength="4000" /></label><label class="field-control field-wide"><span>边界（JSON）</span><el-input :model-value="JSON.stringify(item.boundaries || {}, null, 2)" type="textarea" :rows="3" @change="updateRelationshipBoundaries(item, $event)" /></label></div>
              <div class="form-row"><el-button type="primary" @click="saveCompanionRelationship(item)">保存</el-button><el-button type="danger" plain @click="deleteCompanionRelationship(item)">删除资料</el-button></div>
            </div>
            <div v-if="!companionRelationships.length" class="empty-copy">当前没有关系资料。选择人格后点击“添加人格资料”开始编辑。</div>
          </section>
        </template>

        <template v-else-if="activeTab === 'emotionRecords'">
          <section v-if="!companionOwners.length" class="panel stack"><h2>暂无可用 QQ Owner</h2><p class="hint">请先启用 QQ Owner。</p></section>
          <section v-else class="panel stack">
            <div class="panel-heading"><div><span class="section-kicker">可纠正分类</span><h2>情绪分析记录</h2></div><span class="count-badge">{{ companionAssessments.length }}</span></div>
            <div class="form-row"><el-select v-model="companionOwnerId" @change="loadCompanionData"><el-option v-for="item in companionOwners" :key="item.external_id" :label="`${item.display_name || 'QQ Owner'} · ${item.external_id}`" :value="item.external_id" /></el-select><el-button @click="loadCompanionData" :loading="companionLoading">刷新</el-button></div>
            <el-alert title="页面不展示用户消息原文；候选标签仅用于回顾和纠正。高风险记录只显示风险级别和已执行动作。" type="warning" :closable="false" />
            <p class="hint">安全转向记录会显示“安全转向，未执行情绪分类”，不把中性占位值当作真实情绪。</p>
            <div class="table-wrap"><el-table :data="companionAssessments" empty-text="还没有陪伴分析记录"><el-table-column label="状态" width="110"><template #default="scope"><el-tag :type="companionAnalysisStatusType(scope.row.analysis_status)">{{ companionAnalysisStatusLabel(scope.row.analysis_status) }}</el-tag><small v-if="scope.row.analysis_status !== 'valid'">不展示占位分类</small></template></el-table-column><el-table-column label="情绪候选" min-width="190"><template #default="scope"><template v-if="scope.row.effective_candidate_emotions_display?.length || scope.row.analysis_status === 'valid'"><el-tag v-for="emotion in (scope.row.effective_candidate_emotions_display?.length ? scope.row.effective_candidate_emotions_display : scope.row.candidate_emotions_display)" :key="emotion" size="small" class="tag-gap">{{ emotion }}</el-tag><small v-if="scope.row.effective_primary_emotion_display || scope.row.primary_emotion_display">主：{{ scope.row.effective_primary_emotion_display || scope.row.primary_emotion_display }}</small></template><span v-else>—</span></template></el-table-column><el-table-column label="支持需要" width="120"><template #default="scope">{{ scope.row.effective_support_need_display || (scope.row.analysis_status === 'valid' ? scope.row.support_need_display : '—') }}</template></el-table-column><el-table-column label="置信度" width="100"><template #default="scope">{{ scope.row.confidence == null ? '—' : `${Math.round(scope.row.confidence * 100)}%` }}</template></el-table-column><el-table-column prop="next_action_display" label="策略" width="120" /><el-table-column label="风险" width="100"><template #default="scope"><el-tag :type="scope.row.risk_level === 'low' ? 'success' : scope.row.risk_level === 'medium' ? 'warning' : 'danger'">{{ scope.row.risk_level }}</el-tag></template></el-table-column><el-table-column label="纠正" min-width="340"><template #default="scope"><div v-if="companionCorrectionDrafts[scope.row.user_message_id]" class="correction-cell"><el-select v-model="companionCorrectionDrafts[scope.row.user_message_id].emotions" multiple collapse-tags placeholder="选择 1-3 个情绪"><el-option v-for="emotion in companionEmotionOptions" :key="emotion.value" :label="emotion.label" :value="emotion.value" /></el-select><el-select v-model="companionCorrectionDrafts[scope.row.user_message_id].support_need" placeholder="支持需要"><el-option v-for="(label, value) in companionSupportNeedLabels" :key="value" :label="label" :value="value" /></el-select><el-button size="small" type="primary" @click="submitCompanionCorrection(scope.row)">保存纠正</el-button></div></template></el-table-column></el-table></div>
            <div v-if="companionSafetyEvents.length" class="card-list"><div v-for="item in companionSafetyEvents" :key="item.id" class="result"><span><strong>风险事件 · {{ item.risk_level }}</strong><small>动作：{{ item.action }} · 检测版本：{{ item.detector_version }}</small></span><el-tag type="warning">已脱敏</el-tag></div></div>
          </section>
        </template>

        <template v-else-if="activeTab === 'privacy'">
          <section v-if="!companionOwners.length" class="panel stack"><h2>暂无可用 QQ Owner</h2><p class="hint">请先启用 QQ Owner。</p></section>
          <div v-else class="two-column">
            <section class="panel stack"><div class="panel-heading"><div><span class="section-kicker">数据可携带</span><h2>导出陪伴数据</h2></div><el-icon><DataAnalysis /></el-icon></div><div class="form-row"><el-select v-model="companionOwnerId" @change="loadCompanionData"><el-option v-for="item in companionOwners" :key="item.external_id" :label="`${item.display_name || 'QQ Owner'} · ${item.external_id}`" :value="item.external_id" /></el-select><el-button type="primary" @click="exportCompanionData">导出 JSON</el-button></div><p class="hint">导出包含偏好、关系资料、情绪元数据、反馈和脱敏安全事件，不包含聊天正文、系统提示词、模型推理、密钥或日志凭据。</p><div class="card-list"><div class="result"><span><strong>关系资料</strong><small>{{ companionRelationships.length }} 条</small></span><el-tag type="info">可管理</el-tag></div><div class="result"><span><strong>情绪记录</strong><small>{{ companionAssessments.length }} 条</small></span><el-tag type="info">可纠正</el-tag></div><div class="result"><span><strong>反馈记录</strong><small>{{ companionFeedback.length }} 条</small></span><el-tag type="info">可更新</el-tag></div></div></section>
            <section class="panel stack"><div class="panel-heading"><div><span class="section-kicker">不可逆操作</span><h2>分类删除</h2></div><el-icon><Warning /></el-icon></div><el-checkbox-group v-model="companionDeleteCategories"><el-checkbox label="relationships">关系资料</el-checkbox><el-checkbox label="assessments">情绪分析</el-checkbox><el-checkbox label="feedback">回复反馈</el-checkbox><el-checkbox label="safety">安全记录</el-checkbox><el-checkbox label="preferences">陪伴偏好</el-checkbox></el-checkbox-group><el-input v-model="companionDeleteConfirm" placeholder="输入：删除陪伴数据" /><el-button type="danger" :loading="companionPrivacyDeleting" :disabled="!companionDeleteCategories.length" @click="deleteCompanionData">确认分类删除</el-button><p class="hint">删除接口幂等，并逐项返回成功/失败结果；删除关系资料后立即停止上下文注入。</p></section>
          </div>
        </template>

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

        <template v-else-if="activeTab === 'logs'">
          <div class="status-grid log-status-grid">
            <article class="status-card"><span>日志中心</span><el-icon><Bell /></el-icon><strong>{{ health.logs?.events ?? logEvents.length }}</strong><small>当前会话事件</small><el-tag type="success">运行中</el-tag></article>
            <article class="status-card"><span>NapCat 日志流</span><el-icon><Refresh /></el-icon><strong>{{ statusLabel(napcatConfig.status) }}</strong><small>{{ napcatConfig.configured ? napcatConfig.url : '尚未配置 Token' }}</small><el-tag :type="statusType(napcatConfig.status)">{{ napcatConfig.status }}</el-tag></article>
            <article class="status-card"><span>QQ 状态</span><el-icon><CircleCheck /></el-icon><strong>{{ statusLabel(health.qq) }}</strong><small>与 NapCat 进程、OneBot 分开显示</small><el-tag :type="statusType(health.qq)">{{ health.qq || 'unknown' }}</el-tag></article>
            <article class="status-card"><span>OneBot 状态</span><el-icon><ChatDotRound /></el-icon><strong>{{ statusLabel(health.onebot) }}</strong><small>反向 WebSocket</small><el-tag :type="statusType(health.onebot)">{{ health.onebot || 'unknown' }}</el-tag></article>
          </div>

          <section class="panel stack log-config-panel">
            <div class="panel-heading"><div><span class="section-kicker">本机连接</span><h2>NapCat WebUI 日志配置</h2></div><el-tag :type="napcatConfig.configured ? 'success' : 'warning'">{{ napcatConfig.configured ? 'Token 已配置' : '未配置' }}</el-tag></div>
            <p class="hint">仅允许回环地址。Token 只在后端内存和本机被忽略的 <code>.env</code> 中使用，不会回填、返回或写入日志；NapCat 启用 2FA 时不支持自动续期。</p>
            <div class="form-row log-config-row"><el-input v-model="napcatConfig.url" placeholder="http://127.0.0.1:6099" /><el-input v-model="napcatToken" type="password" show-password autocomplete="new-password" placeholder="输入 Token；留空表示保留已有值" /><el-button :loading="napcatTesting" @click="testNapcatConfig">测试连接</el-button><el-button type="primary" :loading="napcatSaving" @click="saveNapcatConfig">保存并连接</el-button><el-button type="danger" plain :disabled="!napcatConfig.configured" @click="clearNapcatToken">清除 Token</el-button></div>
            <p v-if="napcatConfig.last_error" class="log-error-note">最近连接提示：{{ napcatConfig.last_error }}</p>
          </section>

          <section class="panel stack logs-panel">
            <div class="log-tabs" role="tablist" aria-label="日志类型">
              <button class="log-tab" :class="{ active: logTab === 'operation' }" role="tab" :aria-selected="logTab === 'operation'" @click="logTab = 'operation'; logSourceFilter = ''"><el-icon><Setting /></el-icon>操作日志</button>
              <button class="log-tab" :class="{ active: logTab === 'napcat' }" role="tab" :aria-selected="logTab === 'napcat'" @click="logTab = 'napcat'; logSourceFilter = ''"><el-icon><Refresh /></el-icon>NapCat 日志</button>
            </div>
            <template v-if="logTab === 'operation'">
              <div v-if="activeLogOperations.length" class="active-operations"><div class="panel-heading"><div><span class="section-kicker">进行中</span><h2>当前正在执行</h2></div><span class="count-badge">{{ activeLogOperations.length }}</span></div><div class="active-operation-list"><article v-for="item in activeLogOperations" :key="item.operation_id" class="active-operation"><div class="active-operation-heading"><strong>{{ item.title }}</strong><span>{{ item.message }}</span><el-tag size="small" type="warning">{{ logKindLabel(item.kind) }}</el-tag></div><el-progress v-if="item.progress" :percentage="logOperationPercent(item)" :stroke-width="8" :format="() => `${logOperationPercent(item)}%${item.progress?.unit ? ` · ${item.progress.unit}` : ''}`" /><small v-if="item.operation_id">操作 ID：{{ item.operation_id }}</small></article></div></div>
              <div class="log-filters"><el-select v-model="logSourceFilter" clearable placeholder="全部来源"><el-option v-for="source in logSources" :key="source" :label="source" :value="source" /></el-select><el-select v-model="logLevelFilter" clearable placeholder="全部级别"><el-option label="信息" value="info" /><el-option label="警告" value="warn" /><el-option label="错误" value="error" /><el-option label="调试" value="debug" /></el-select><el-input v-model="logQuery" clearable placeholder="搜索标题、内容或详情" /><el-button :type="logDisplayPaused ? 'warning' : 'default'" @click="logDisplayPaused = !logDisplayPaused"><el-icon><VideoPlay v-if="logDisplayPaused" /><VideoPause v-else /></el-icon>{{ logDisplayPaused ? '继续显示' : '暂停显示' }}</el-button><el-switch v-model="logAutoScroll" active-text="自动滚动" /></div>
            </template>
            <div ref="logsContainer" class="log-list" :class="{ 'log-list-paused': logDisplayPaused }" aria-live="polite">
              <article v-for="item in filteredLogEvents" :key="item.id" class="log-entry" :class="[`log-level-${item.level}`, { expanded: expandedLogIds.includes(item.id) }]">
                <div class="log-entry-main"><time>{{ logTime(item.timestamp) }}</time><el-tag size="small" effect="plain" :type="logLevelType(item.level)">{{ logLevelLabel(item.level) }}</el-tag><span class="log-source">{{ item.source }}</span><span class="log-kind">{{ logKindLabel(item.kind) }}</span><strong>{{ item.title }}</strong><p>{{ item.message }}</p><button class="log-detail-toggle" :aria-expanded="expandedLogIds.includes(item.id)" @click="toggleLogDetails(item.id)">{{ expandedLogIds.includes(item.id) ? '收起详情' : '详情' }}</button></div>
                <div v-if="item.progress" class="log-entry-progress"><el-progress :percentage="logOperationPercent(item)" :stroke-width="6" :show-text="false" /><small>{{ logOperationPercent(item) }}%<template v-if="item.progress.unit"> · {{ item.progress.unit }}</template></small></div>
                <pre v-if="expandedLogIds.includes(item.id)" class="log-details">{{ JSON.stringify(item.details, null, 2) }}</pre>
              </article>
              <div v-if="!filteredLogEvents.length" class="empty-copy">当前筛选条件下没有日志。</div>
            </div>
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
