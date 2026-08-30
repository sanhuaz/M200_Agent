import { describe, expect, it } from 'vitest'

import { buildMemoryCenterQuery, type MemoryFilters } from './memory'

const base: MemoryFilters = {
  memoryType: 'all',
  scopeType: 'all',
  status: 'active',
  scopeId: '',
  personaKey: '',
  keyword: '',
}

describe('buildMemoryCenterQuery', () => {
  it('关系记忆保留人格筛选并清理文本空白', () => {
    const params = buildMemoryCenterQuery({
      ...base,
      memoryType: 'relationship',
      scopeType: 'user',
      scopeId: ' 10001 ',
      personaKey: 'persona-a',
      keyword: ' 朋友 ',
    })

    expect(params.get('scope_id')).toBe('10001')
    expect(params.get('persona_key')).toBe('persona-a')
    expect(params.get('keyword')).toBe('朋友')
  })

  it('事实记忆不会发送关系人格筛选', () => {
    const params = buildMemoryCenterQuery({ ...base, memoryType: 'fact', personaKey: 'persona-a' })

    expect(params.get('memory_type')).toBe('fact')
    expect(params.has('persona_key')).toBe(false)
    expect(params.has('scope_id')).toBe(false)
  })
})
