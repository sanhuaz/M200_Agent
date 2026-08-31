<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { ChatDotRound, Delete, Menu } from '@element-plus/icons-vue'
import { LatestRequestGate } from '../features/requestGate'
import {
  attachmentUrl,
  createConversation as createConversationApi,
  listConversations,
  listMessages,
  removeAttachment,
  removeConversation,
  streamChatTurn,
  validateImageFiles,
  updateConversationModel,
  updateConversationPersona,
} from '../features/chat'
import type {
  ChatPersona,
  Conversation,
  Message,
  MessageAttachment,
  ModelProfile,
} from '../types/chat'

const props = defineProps<{
  models: ModelProfile[]
  personas: ChatPersona[]
}>()
const emit = defineEmits<{ changed: [] }>()

type PendingImage = {
  file: File
  previewUrl: string
}

const conversations = ref<Conversation[]>([])
const currentConversationId = ref('')
const messages = ref<Message[]>([])
const messagesContainer = ref<HTMLElement | null>(null)
const messagesAutoFollow = ref(true)
const messagesLoading = ref(false)
const conversationsLoading = ref(false)
const conversationListOpen = ref(true)
const input = ref('')
const sending = ref(false)
const pendingImages = ref<PendingImage[]>([])
const failedAttachmentUrls = ref(new Set<string>())
const conversationRequests = new LatestRequestGate<string>()
const messagesRequests = new LatestRequestGate<string>()
let streamController: AbortController | null = null
let streamSequence = 0

const currentConversation = computed(() =>
  conversations.value.find((item) => item.id === currentConversationId.value),
)
const currentModel = computed(() =>
  props.models.find((item) => item.alias === currentConversation.value?.model_alias),
)
const visionEnabled = computed(() => currentModel.value?.supports_vision === true)

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

async function loadMessages(force = true) {
  const conversationId = currentConversationId.value
  const request = messagesRequests.begin(conversationId)
  if (!conversationId) {
    messages.value = []
    messagesLoading.value = false
    return
  }
  messages.value = []
  messagesLoading.value = true
  try {
    const data = await listMessages(conversationId)
    if (!messagesRequests.isCurrent(request, currentConversationId.value)) return
    messages.value = data
    if (force) messagesAutoFollow.value = true
    await scrollMessagesToBottom(force)
  } catch (error) {
    if (messagesRequests.isCurrent(request, currentConversationId.value)) {
      ElMessage.error(`消息加载失败：${(error as Error).message}`)
    }
  } finally {
    if (messagesRequests.isCurrent(request, currentConversationId.value)) messagesLoading.value = false
  }
}

async function loadConversations() {
  const request = conversationRequests.begin('all')
  conversationsLoading.value = true
  try {
    const data = await listConversations()
    if (!conversationRequests.isCurrent(request, 'all')) return
    conversations.value = data
    if (!currentConversationId.value && data.length) {
      currentConversationId.value = data[0].id
      await loadMessages()
    } else if (currentConversationId.value && !data.some((item) => item.id === currentConversationId.value)) {
      currentConversationId.value = data[0]?.id || ''
      await loadMessages()
    }
  } catch (error) {
    ElMessage.error(`会话加载失败：${(error as Error).message}`)
  } finally {
    if (conversationRequests.isCurrent(request, 'all')) conversationsLoading.value = false
  }
}

async function createConversation() {
  const item = await createConversationApi(`会话 ${conversations.value.length + 1}`)
  conversations.value.unshift(item)
  currentConversationId.value = item.id
  messages.value = []
  messagesAutoFollow.value = true
  await scrollMessagesToBottom(true)
}

function cancelStream() {
  streamSequence += 1
  streamController?.abort()
  streamController = null
  sending.value = false
}

async function selectConversation(conversationId: string) {
  if (currentConversationId.value === conversationId && messagesLoading.value) return
  if (sending.value) cancelStream()
  currentConversationId.value = conversationId
  messages.value = []
  messagesAutoFollow.value = true
  await loadMessages()
}

async function switchModel(alias: string) {
  if (!currentConversationId.value || sending.value) return
  try {
    await updateConversationModel(currentConversationId.value, alias)
    await loadConversations()
    emit('changed')
    ElMessage.success(`已切换为 ${alias}`)
  } catch (error) {
    ElMessage.error(`模型切换失败：${(error as Error).message}`)
  }
}

async function switchPersona(personaId: string) {
  if (!currentConversationId.value || sending.value) return
  try {
    await updateConversationPersona(currentConversationId.value, personaId || null)
    await loadConversations()
    emit('changed')
    ElMessage.success(personaId ? '已切换人格' : '已关闭人格')
  } catch (error) {
    ElMessage.error(`人格切换失败：${(error as Error).message}`)
  }
}

async function deleteConversation(item: Conversation) {
  if (item.platform !== 'web' || !window.confirm(`确定删除网页会话“${item.title}”？消息和工作流记录将被删除。`)) return
  if (sending.value && currentConversationId.value === item.id) cancelStream()
  try {
    await removeConversation(item.id)
    const wasCurrent = currentConversationId.value === item.id
    conversations.value = conversations.value.filter((conversation) => conversation.id !== item.id)
    if (wasCurrent) {
      currentConversationId.value = conversations.value[0]?.id || ''
      await loadMessages()
    }
    ElMessage.success('网页会话已删除')
  } catch (error) {
    ElMessage.error(`会话删除失败：${(error as Error).message}`)
  }
}

function formatBytes(size: number) {
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KiB`
  return `${(size / 1024 / 1024).toFixed(1)} MiB`
}

function addImages(event: Event) {
  const inputElement = event.target as HTMLInputElement
  const files = Array.from(inputElement.files || [])
  inputElement.value = ''
  if (!visionEnabled.value) {
    ElMessage.warning('当前模型未启用识图能力，请先在模型管理中开启。')
    return
  }
  const validationError = validateImageFiles(files, pendingImages.value.map((item) => item.file))
  if (validationError) {
    ElMessage.warning(validationError)
    return
  }
  pendingImages.value.push(...files.map((file) => ({ file, previewUrl: URL.createObjectURL(file) })))
}

function removeImage(index: number) {
  const [item] = pendingImages.value.splice(index, 1)
  if (item) URL.revokeObjectURL(item.previewUrl)
}

function clearPendingImages() {
  for (const item of pendingImages.value) URL.revokeObjectURL(item.previewUrl)
  pendingImages.value = []
}

function releasePreviewUrls(urls: Iterable<string>) {
  for (const url of urls) URL.revokeObjectURL(url)
}

function optimisticAttachment(item: PendingImage, messageId: string): MessageAttachment {
  return {
    id: `local-${messageId}-${item.file.name}`,
    message_id: messageId,
    original_filename: item.file.name,
    mime_type: item.file.type || 'image/*',
    byte_size: item.file.size,
    width: 0,
    height: 0,
    source: 'web',
    created_at: new Date().toISOString(),
    download_url: '',
    preview_url: item.previewUrl,
  }
}

async function send() {
  const text = input.value.trim()
  if ((!text && !pendingImages.value.length) || sending.value) return
  if (pendingImages.value.length && !visionEnabled.value) {
    ElMessage.warning('当前模型未启用识图能力，请先在模型管理中开启。')
    return
  }
  if (!currentConversationId.value) {
    try {
      await createConversation()
    } catch (error) {
      ElMessage.error(`新建会话失败：${(error as Error).message}`)
      return
    }
  }
  const targetConversationId = currentConversationId.value
  const requestSequence = ++streamSequence
  const images = pendingImages.value.map((item) => item.file)
  const localMessageId = `local-${requestSequence}`
  const optimisticImages = pendingImages.value.map((item) => optimisticAttachment(item, localMessageId))
  const previewUrls = pendingImages.value.map((item) => item.previewUrl)
  input.value = ''
  pendingImages.value = []
  messages.value.push(
    { id: localMessageId, role: 'user', content: text || '[图片]', attachments: optimisticImages },
    { id: `assistant-${localMessageId}`, role: 'assistant', content: '' },
  )
  const target = messages.value[messages.value.length - 1]
  messagesAutoFollow.value = true
  await scrollMessagesToBottom(true)
  sending.value = true
  const controller = new AbortController()
  streamController = controller
  try {
    await streamChatTurn(
      { conversationId: targetConversationId, text, images, signal: controller.signal },
      ({ event, data }) => {
        if (requestSequence !== streamSequence || currentConversationId.value !== targetConversationId) return
        if (event === 'token') target.content += String(data.text || '')
        if (event === 'error') target.content = `错误：${String(data.message || '聊天请求失败')}`
        if (event === 'token') void scrollMessagesToBottom()
      },
    )
    if (requestSequence === streamSequence && currentConversationId.value === targetConversationId) await loadMessages(false)
  } catch (error) {
    if (requestSequence === streamSequence && currentConversationId.value === targetConversationId) {
      if ((error as Error).name !== 'AbortError') target.content = `错误：${(error as Error).message}`
      try { await loadMessages(false) } catch { /* 保留本地错误消息，等待用户重试 */ }
    }
  } finally {
    releasePreviewUrls(previewUrls)
    if (requestSequence === streamSequence) {
      sending.value = false
      streamController = null
    }
  }
}

async function deleteHistoryAttachment(message: Message, attachment: MessageAttachment) {
  if (!message.id || attachment.id.startsWith('local-')) return
  try {
    await removeAttachment(message.id, attachment.id)
    message.attachments = (message.attachments || []).filter((item) => item.id !== attachment.id)
    ElMessage.success('附件已删除')
  } catch (error) {
    ElMessage.error(`附件删除失败：${(error as Error).message}`)
  }
}

function displayAttachmentUrl(attachment: MessageAttachment) {
  return attachment.preview_url || attachmentUrl(attachment.download_url)
}

function markAttachmentFailed(url: string) {
  failedAttachmentUrls.value = new Set(failedAttachmentUrls.value).add(url)
}

onMounted(() => {
  void loadConversations()
})

onUnmounted(() => {
  cancelStream()
  clearPendingImages()
})
</script>

<template>
  <section class="chat-grid" :class="{ 'conversation-hidden': !conversationListOpen }">
    <aside class="panel conversation-panel">
      <div class="panel-heading"><div><span class="section-kicker">会话</span><h2>最近对话</h2></div><el-button type="primary" @click="createConversation">新建</el-button></div>
      <div v-loading="conversationsLoading" class="conversation-list">
        <div v-for="item in conversations" :key="item.id" class="conversation-row">
          <button class="conversation" :class="{ active: item.id === currentConversationId }" @click="selectConversation(item.id)">
            <span class="conversation-icon"><el-icon><ChatDotRound /></el-icon></span><span><strong>{{ item.title }}</strong><small>{{ item.model_alias }} · {{ item.platform === 'web' ? '网页' : 'QQ' }}</small></span>
          </button>
          <button v-if="item.platform === 'web'" class="conversation-delete" aria-label="删除网页会话" title="删除网页会话" :disabled="sending && item.id === currentConversationId" @click="deleteConversation(item)"><el-icon><Delete /></el-icon></button>
        </div>
        <div v-if="!conversations.length && !conversationsLoading" class="empty-copy">还没有会话，点击“新建”开始对话。</div>
      </div>
    </aside>
    <section class="panel chat-panel">
      <div class="chat-toolbar">
        <div class="chat-title"><button class="icon-button conversation-toggle" aria-label="显示或隐藏会话列表" @click="conversationListOpen = !conversationListOpen"><el-icon><Menu /></el-icon></button><span><span class="section-kicker">当前会话</span><strong>{{ currentConversation?.title || '未选择会话' }}</strong></span></div>
        <div class="toolbar-selects">
          <el-select :model-value="currentConversation?.model_alias" placeholder="选择模型" :disabled="sending" @change="switchModel"><el-option v-for="model in models" :key="model.alias" :label="`${model.alias}${model.configured ? '' : '（未配置）'}`" :value="model.alias" /></el-select>
          <el-select :model-value="currentConversation?.persona_id || ''" placeholder="选择人格" :disabled="sending" @change="switchPersona"><el-option label="关闭人格" value="" /><el-option v-for="item in personas" :key="item.id" :label="`${item.name}${item.status === 'active' ? '' : '（文件无效）'}`" :value="item.id" :disabled="item.status !== 'active'" /></el-select>
        </div>
      </div>
      <div ref="messagesContainer" class="messages" v-loading="messagesLoading" @scroll="handleMessagesScroll">
        <div v-if="!messages.length && !messagesLoading" class="chat-empty"><span class="empty-orb"><el-icon><ChatDotRound /></el-icon></span><h3>开始一段新对话</h3><p>选择模型和人格，然后输入你的问题。</p></div>
        <article v-for="(message, index) in messages" :key="message.id || index" :class="['message', message.role]">
          <span>{{ message.role === 'user' ? '你' : 'M200 Agent' }}</span>
          <p>{{ message.content }}</p>
          <div v-if="message.attachments?.length" class="message-attachments">
            <figure v-for="attachment in message.attachments" :key="attachment.id" class="message-attachment">
              <template v-if="!failedAttachmentUrls.has(displayAttachmentUrl(attachment))">
                <img :src="displayAttachmentUrl(attachment)" :alt="attachment.original_filename" loading="lazy" @error="markAttachmentFailed(displayAttachmentUrl(attachment))" />
              </template>
              <div v-else class="attachment-placeholder">图片不可用</div>
              <figcaption><span>{{ attachment.original_filename }} · {{ formatBytes(attachment.byte_size) }}</span><button v-if="!attachment.id.startsWith('local-')" type="button" aria-label="删除附件" title="删除附件" @click="deleteHistoryAttachment(message, attachment)"><el-icon><Delete /></el-icon></button></figcaption>
            </figure>
          </div>
        </article>
      </div>
      <div v-if="pendingImages.length" class="pending-attachments">
        <figure v-for="(item, index) in pendingImages" :key="item.previewUrl" class="pending-attachment">
          <img :src="item.previewUrl" :alt="item.file.name" />
          <figcaption><span>{{ item.file.name }} · {{ formatBytes(item.file.size) }}</span><button type="button" @click="removeImage(index)">移除</button></figcaption>
        </figure>
      </div>
      <div class="composer">
        <el-input v-model="input" type="textarea" :rows="3" resize="none" placeholder="输入消息，Ctrl+Enter 发送" @keydown.ctrl.enter.prevent="send" />
        <div class="composer-actions">
          <label class="file-picker chat-file-picker" :class="{ disabled: !visionEnabled || sending }"><input type="file" accept="image/jpeg,image/png,image/gif,image/webp" multiple :disabled="!visionEnabled || sending" @change="addImages" /><span>{{ visionEnabled ? '添加图片' : '模型未启用识图' }}</span></label>
          <el-button v-if="sending" @click="cancelStream">取消</el-button>
          <el-button type="primary" :loading="sending" :disabled="!input.trim() && !pendingImages.length" @click="send">发送</el-button>
        </div>
      </div>
    </section>
  </section>
</template>
