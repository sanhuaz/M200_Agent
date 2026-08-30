export type MemoryType = 'all' | 'fact' | 'relationship'
export type MemoryScope = 'all' | 'global' | 'user' | 'group'
export type MemoryStatus = 'active' | 'archived' | 'all'

export type MemoryFilters = {
  memoryType: MemoryType
  scopeType: MemoryScope
  status: MemoryStatus
  scopeId: string
  personaKey: string
  keyword: string
}

export function buildMemoryCenterQuery(filters: MemoryFilters): URLSearchParams {
  const params = new URLSearchParams({
    memory_type: filters.memoryType,
    scope_type: filters.scopeType,
    status: filters.status,
  })
  if (filters.scopeId.trim()) params.set('scope_id', filters.scopeId.trim())
  if (filters.personaKey && filters.memoryType === 'relationship') {
    params.set('persona_key', filters.personaKey)
  }
  if (filters.keyword.trim()) params.set('keyword', filters.keyword.trim())
  return params
}
