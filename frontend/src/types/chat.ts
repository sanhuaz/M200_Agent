export type ReasoningEffort = 'low' | 'medium' | 'high' | null

export type ModelProfile = {
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
  supports_vision: boolean
}

export type ChatPersona = {
  id: string
  name: string
  status: 'active' | 'invalid'
}

export type Conversation = {
  id: string
  title: string
  platform: string
  model_alias: string
  persona_id?: string | null
}

export type MessageAttachment = {
  id: string
  message_id: string
  original_filename: string
  mime_type: string
  byte_size: number
  width: number
  height: number
  source: 'web' | 'onebot' | string
  created_at: string
  download_url: string
  preview_url?: string
}

export type ChatArtifact = {
  id: string
  filename: string
  size: number
  content_type?: string
  download_url: string
}

export type Message = {
  id?: string
  role: string
  content: string
  created_at?: string
  attachments?: MessageAttachment[]
  artifacts?: ChatArtifact[]
}

export type ChatStreamEvent = {
  event: string
  data: Record<string, unknown>
}

export type ChatStreamRequest = {
  conversationId: string | null
  text: string
  images: File[]
  signal?: AbortSignal
}
