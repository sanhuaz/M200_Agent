<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { API, api, streamChat } from './services/api'

type Conversation = { id: string; title: string; model_alias: string }
type ModelProfile = { alias: string; model: string; configured: boolean }
type Message = { id?: string; role: string; content: string }
type KnowledgeBase = { id: string; name: string; embedding_profile: string }
type DocumentRow = { id: string; knowledge_base_id: string; filename: string; status: string; error?: string }
type MemoryRow = { id: string; fact_key: string; content: string; status: string }
type TaskRow = { id: string; type: string; status: string; error?: string; result?: { path?: string } }
type Confirmation = { token: string; action: string; payload: Record<string, unknown>; expires_at: string }

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
const mangaQuery = ref('')
const mangaResults = ref<Array<{ album_id: string; title: string }>>([])
const currentConversation = computed(() => conversations.value.find((item) => item.id === currentConversationId.value))

async function loadBase() {
  const [healthData, modelData, conversationData, knowledgeData] = await Promise.all([
    api<Record<string, unknown>>('/health'),
    api<ModelProfile[]>('/models'),
    api<Conversation[]>('/conversations'),
    api<KnowledgeBase[]>('/knowledge-bases'),
  ])
  health.value = healthData
  models.value = modelData
  conversations.value = conversationData
  knowledgeBases.value = knowledgeData
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
  documents.value = await api(`/documents${selectedKb.value ? `?knowledge_base_id=${selectedKb.value}` : ''}`)
}

async function loadMemoryTasks() {
  const [memoryData, taskData, confirmationData] = await Promise.all([
    api<MemoryRow[]>('/memories'),
    api<TaskRow[]>('/tasks'),
    api<Confirmation[]>('/confirmations'),
  ])
  memories.value = memoryData
  tasks.value = taskData
  confirmations.value = confirmationData
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
  await api('/manga/download', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ album_id: albumId, requester_id: 'local-owner', conversation_id: currentConversationId.value || null }),
  })
  await loadMemoryTasks()
}

async function resolve(token: string, approve: boolean) {
  await api(`/confirmations/${token}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ requester_id: 'local-owner', approve }),
  })
  await loadMemoryTasks()
}

onMounted(async () => {
  try { await loadBase(); await loadDocuments(); await loadMemoryTasks() }
  catch (error) { ElMessage.error((error as Error).message) }
})
</script>

<template>
  <div class="shell">
    <header>
      <div><span class="eyebrow">LOCAL AI WORKSPACE</span><h1>PersonalAgent</h1></div>
      <el-tag :type="health.status === 'ok' ? 'success' : 'warning'">{{ health.status || 'loading' }}</el-tag>
    </header>
    <el-tabs v-model="activeTab" class="tabs" @tab-change="() => loadMemoryTasks()">
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
              <el-select :model-value="currentConversation?.model_alias" placeholder="模型" @change="switchModel">
                <el-option v-for="model in models" :key="model.alias" :label="`${model.alias}${model.configured ? '' : '（未配置）'}`" :value="model.alias" />
              </el-select>
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
          <el-table :data="documents"><el-table-column prop="filename" label="文件" /><el-table-column prop="status" label="状态" /><el-table-column prop="error" label="错误" /><el-table-column label="操作"><template #default="scope"><el-button size="small" type="danger" plain @click="deleteDocument(scope.row.id)">删除</el-button></template></el-table-column></el-table>
        </section>
      </el-tab-pane>

      <el-tab-pane label="记忆" name="memory">
        <section class="panel stack"><h2>长期记忆</h2>
          <el-table :data="memories"><el-table-column prop="fact_key" label="事实键" /><el-table-column prop="content" label="内容" /><el-table-column prop="status" label="状态" /><el-table-column label="操作" width="230"><template #default="scope"><el-button size="small" @click="editMemory(scope.row)">编辑</el-button><el-button size="small" @click="archiveMemory(scope.row.id)">归档</el-button><el-button size="small" type="danger" plain @click="deleteMemory(scope.row.id)">删除</el-button></template></el-table-column></el-table>
        </section>
      </el-tab-pane>

      <el-tab-pane label="漫画与任务" name="tasks">
        <div class="two-column">
          <section class="panel stack"><h2>漫画搜索</h2><div class="row"><el-input v-model="mangaQuery" placeholder="关键词" /><el-button type="primary" @click="searchManga">搜索</el-button></div>
            <div v-for="item in mangaResults" :key="item.album_id" class="result"><span>JM{{ item.album_id }} · {{ item.title }}</span><el-button size="small" @click="requestDownload(item.album_id)">请求下载</el-button></div>
          </section>
          <section class="panel stack"><h2>待确认</h2><div v-for="item in confirmations" :key="item.token" class="result"><code>{{ item.token }}</code><span>{{ item.action }}</span><div><el-button size="small" type="primary" @click="resolve(item.token, true)">确认</el-button><el-button size="small" @click="resolve(item.token, false)">拒绝</el-button></div></div></section>
        </div>
        <section class="panel stack task-list"><h2>任务</h2><el-table :data="tasks"><el-table-column prop="id" label="ID" /><el-table-column prop="type" label="类型" /><el-table-column prop="status" label="状态" /><el-table-column prop="error" label="错误" /><el-table-column label="操作" width="180"><template #default="scope"><el-button v-if="['queued', 'running'].includes(scope.row.status)" size="small" @click="cancelTask(scope.row.id)">取消</el-button><el-link v-if="scope.row.status === 'succeeded'" :href="`${API}/tasks/${scope.row.id}/artifact`" target="_blank" type="primary">下载产物</el-link></template></el-table-column></el-table></section>
      </el-tab-pane>

      <el-tab-pane label="状态" name="status"><pre class="panel status">{{ JSON.stringify(health, null, 2) }}</pre></el-tab-pane>
    </el-tabs>
  </div>
</template>
