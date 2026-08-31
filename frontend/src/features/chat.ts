import { API, SseEventParser, api } from '../services/api'
import type { ChatStreamEvent, ChatStreamRequest, Conversation, Message } from '../types/chat'

export const CHAT_IMAGE_MAX_COUNT = 4
export const CHAT_IMAGE_MAX_BYTES = 32 * 1024 * 1024
const CHAT_IMAGE_TYPES = new Set(['image/jpeg', 'image/png', 'image/gif', 'image/webp'])

export function validateImageFiles(files: readonly File[], existing: readonly File[] = []): string | null {
  if (existing.length + files.length > CHAT_IMAGE_MAX_COUNT) return '每条消息最多选择 4 张图片。'
  if (files.some((file) => {
    const mime = file.type.toLowerCase()
    return !CHAT_IMAGE_TYPES.has(mime) && !/\.(jpe?g|png|gif|webp)$/i.test(file.name)
  })) return '仅支持 JPEG、PNG、GIF 和 WebP 图片。'
  const total = [...existing, ...files].reduce((sum, file) => sum + file.size, 0)
  if (total > CHAT_IMAGE_MAX_BYTES) return '每条消息图片总大小不能超过 32 MiB。'
  return null
}

export function listConversations() {
  return api<Conversation[]>('/conversations')
}

export function listMessages(conversationId: string) {
  return api<Message[]>(`/conversations/${encodeURIComponent(conversationId)}/messages`)
}

export function createConversation(title: string) {
  return api<Conversation>('/conversations', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title }),
  })
}

export function updateConversationModel(conversationId: string, modelAlias: string) {
  return api(`/conversations/${encodeURIComponent(conversationId)}/model`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ model_alias: modelAlias }),
  })
}

export function updateConversationPersona(conversationId: string, personaId: string | null) {
  return api(`/conversations/${encodeURIComponent(conversationId)}/persona`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ persona_id: personaId }),
  })
}

export function removeConversation(conversationId: string) {
  return api(`/conversations/${encodeURIComponent(conversationId)}`, { method: 'DELETE' })
}

export function removeAttachment(messageId: string, attachmentId: string) {
  return api<{ deleted: boolean; message_id: string; attachment_id: string }>(
    `/messages/${encodeURIComponent(messageId)}/attachments/${encodeURIComponent(attachmentId)}`,
    { method: 'DELETE' },
  )
}

export function attachmentUrl(downloadUrl: string) {
  return downloadUrl.startsWith('/api/') ? downloadUrl : `${API}${downloadUrl}`
}

function emitEvents(parser: SseEventParser, text: string, onEvent: (event: ChatStreamEvent) => void) {
  for (const item of parser.feed(text)) onEvent(item)
}

export async function streamChatTurn(
  request: ChatStreamRequest,
  onEvent: (event: ChatStreamEvent) => void,
): Promise<void> {
  const hasImages = request.images.length > 0
  let response: Response
  if (hasImages) {
    const form = new FormData()
    if (request.conversationId) form.append('conversation_id', request.conversationId)
    form.append('content', request.text)
    for (const image of request.images) form.append('images', image, image.name)
    response = await fetch(`${API}/chat/stream/multimodal`, {
      method: 'POST',
      body: form,
      signal: request.signal,
    })
  } else {
    response = await fetch(`${API}/chat/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ conversation_id: request.conversationId, message: request.text }),
      signal: request.signal,
    })
  }
  if (!response.ok || !response.body) throw new Error(`聊天请求失败：${response.status}`)
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  const parser = new SseEventParser()
  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    emitEvents(parser, decoder.decode(value, { stream: true }), onEvent)
  }
  emitEvents(parser, decoder.decode(), onEvent)
  for (const item of parser.finish()) onEvent(item)
}
