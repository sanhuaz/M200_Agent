import { afterEach, describe, expect, it, vi } from 'vitest'

import { SseEventParser, streamChat } from './api'

describe('SseEventParser', () => {
  it('能跨网络分片解析事件并在结束时刷新尾部事件', () => {
    const parser = new SseEventParser()

    expect(parser.feed('event: token\ndata: {"content":"你')).toEqual([])
    expect(parser.feed('好"}\n\nevent: done\ndata: {"message_id":"m1"}')).toEqual([
      { event: 'token', data: { content: '你好' } },
    ])
    expect(parser.finish()).toEqual([{ event: 'done', data: { message_id: 'm1' } }])
  })
})

describe('streamChat', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('按顺序转发聊天流事件', async () => {
    const encoder = new TextEncoder()
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode('event: token\ndata: {"content":"A"}\n\n'))
        controller.enqueue(encoder.encode('event: done\ndata: {"message_id":"m1"}'))
        controller.close()
      },
    })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(body, { status: 200 })))
    const events: Array<[string, Record<string, unknown>]> = []

    await streamChat({ conversation_id: 'c1', content: 'hello' }, (event, data) => events.push([event, data]))

    expect(events).toEqual([
      ['token', { content: 'A' }],
      ['done', { message_id: 'm1' }],
    ])
  })
})
