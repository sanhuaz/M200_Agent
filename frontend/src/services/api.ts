export const API = '/api/v1'

export type StreamEvent = {
  event: string
  data: Record<string, unknown>
}

export class SseEventParser {
  private buffer = ''

  feed(text: string): StreamEvent[] {
    this.buffer += text
    const blocks = this.buffer.split('\n\n')
    this.buffer = blocks.pop() || ''
    return blocks.flatMap(parseEventBlock)
  }

  finish(): StreamEvent[] {
    const block = this.buffer
    this.buffer = ''
    return block ? parseEventBlock(block) : []
  }
}

function parseEventBlock(block: string): StreamEvent[] {
  const event = block.match(/^event: (.+)$/m)?.[1]
  const data = block.match(/^data: (.+)$/m)?.[1]
  if (!event || !data) return []
  return [{ event, data: JSON.parse(data) as Record<string, unknown> }]
}

export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, options)
  if (!response.ok) {
    const payload = await response.json().catch(() => ({ detail: response.statusText }))
    throw new Error(payload.detail || response.statusText)
  }
  return response.json() as Promise<T>
}

export async function streamChat(
  payload: Record<string, unknown>,
  onEvent: (event: string, data: Record<string, unknown>) => void,
): Promise<void> {
  const response = await fetch(`${API}/chat/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  if (!response.ok || !response.body) throw new Error(`聊天请求失败：${response.status}`)
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  const parser = new SseEventParser()
  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    for (const item of parser.feed(decoder.decode(value, { stream: true }))) {
      onEvent(item.event, item.data)
    }
  }
  for (const item of parser.feed(decoder.decode())) {
    onEvent(item.event, item.data)
  }
  for (const item of parser.finish()) {
    onEvent(item.event, item.data)
  }
}
