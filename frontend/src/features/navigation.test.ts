import { describe, expect, it } from 'vitest'

import { normalizeTab, pathForTab, resolveRoute } from './navigation'

describe('navigation', () => {
  it('将旧关系页路由迁移到记忆中心', () => {
    expect(resolveRoute('/relationships')).toEqual({
      tab: 'memory',
      memoryType: 'relationship',
      replacePath: '/memories?memory_type=relationship',
    })
  })

  it('同步记忆筛选参数并忽略非法值', () => {
    expect(resolveRoute('/memories', '?memory_type=fact')).toEqual({ tab: 'memory', memoryType: 'fact' })
    expect(resolveRoute('/memories', '?memory_type=invalid')).toEqual({ tab: 'memory', memoryType: undefined })
  })

  it('标准化标签并生成可分享路径', () => {
    expect(normalizeTab('relationships')).toEqual({ tab: 'memory', memoryType: 'relationship' })
    expect(pathForTab('memory', 'relationship')).toBe('/memories?memory_type=relationship')
    expect(pathForTab('missing', 'all')).toBe('/chat')
  })
})
