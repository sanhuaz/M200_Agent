import { describe, expect, it } from 'vitest'

import { LatestRequestGate } from './requestGate'

describe('LatestRequestGate', () => {
  it('只接受同一资源最近一次请求', () => {
    const gate = new LatestRequestGate<string>()
    const first = gate.begin('conversation-a')
    const second = gate.begin('conversation-a')

    expect(gate.isCurrent(first, 'conversation-a')).toBe(false)
    expect(gate.isCurrent(second, 'conversation-a')).toBe(true)
  })

  it('拒绝已经切换资源的响应', () => {
    const gate = new LatestRequestGate<string>()
    const token = gate.begin('conversation-a')

    expect(gate.isCurrent(token, 'conversation-b')).toBe(false)
  })
})
