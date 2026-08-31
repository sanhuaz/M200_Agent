export type MemoryRouteType = 'all' | 'fact' | 'relationship'

export const routePaths: Record<string, string> = {
  chat: '/chat',
  models: '/models',
  knowledge: '/knowledge',
  tools: '/tools',
  skills: '/skills',
  mcp: '/mcp',
  personas: '/personas',
  memory: '/memories',
  companion: '/companion',
  strategyGuides: '/strategy-guides',
  emotionRecords: '/emotion-records',
  privacy: '/privacy',
  admin: '/admin',
  tasks: '/tasks',
  status: '/status',
  logs: '/logs',
}

export function resolveRoute(pathname: string, search = ''): {
  tab: string | undefined
  memoryType?: MemoryRouteType
  replacePath?: string
} {
  if (pathname === '/relationships') {
    return {
      tab: 'memory',
      memoryType: 'relationship',
      replacePath: '/memories?memory_type=relationship',
    }
  }
  const tab = Object.entries(routePaths).find(([, path]) => pathname === path)?.[0]
  if (tab !== 'memory') return { tab }
  const requestedType = new URLSearchParams(search).get('memory_type')
  const memoryType = ['all', 'fact', 'relationship'].includes(requestedType || '')
    ? requestedType as MemoryRouteType
    : undefined
  return { tab, memoryType }
}

export function normalizeTab(tab: string | number): {
  tab: string
  memoryType?: MemoryRouteType
} {
  const name = String(tab)
  return name === 'relationships'
    ? { tab: 'memory', memoryType: 'relationship' }
    : { tab: name }
}

export function pathForTab(tab: string, memoryType: MemoryRouteType): string {
  if (tab === 'memory' && memoryType === 'relationship') {
    return '/memories?memory_type=relationship'
  }
  return routePaths[tab] || '/chat'
}
