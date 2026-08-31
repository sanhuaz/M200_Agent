import { api } from '../services/api'
import type {
  McpAccessPolicy,
  McpCatalogResponse,
  McpGrantKind,
  McpServer,
  McpServerDraft,
  McpTestResponse,
} from '../types/mcp'

function parseObject(text: string, label: string): Record<string, unknown> {
  const value = JSON.parse(text || '{}') as unknown
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`${label}必须是 JSON 对象`)
  }
  return value as Record<string, unknown>
}

function parsePrivateUsers(text: string): string[] {
  return [...new Set(text.split(/[,\n]/).map((item) => item.trim()).filter(Boolean))]
}

export function draftToPayload(draft: McpServerDraft) {
  return {
    name: draft.name.trim(),
    slug: draft.slug.trim() || undefined,
    transport: draft.transport,
    config: parseObject(draft.configText, '连接配置'),
    secret_values: parseObject(draft.secretValuesText, '密钥配置') as Record<string, string>,
    access_policy: draft.accessPolicy,
    private_users: parsePrivateUsers(draft.privateUsersText),
  }
}

export function serverToDraft(server: McpServer): McpServerDraft {
  return {
    name: server.name,
    slug: server.slug,
    transport: server.transport,
    configText: JSON.stringify(server.config || {}, null, 2),
    secretValuesText: '{}',
    accessPolicy: server.access_policy,
    privateUsersText: server.private_users.join('\n'),
  }
}

export function listMcpServers(): Promise<McpServer[]> {
  return api<McpServer[]>('/mcp/servers')
}

export function createMcpServer(draft: McpServerDraft): Promise<McpServer> {
  return api<McpServer>('/mcp/servers', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(draftToPayload(draft)),
  })
}

export function updateMcpServer(id: string, draft: McpServerDraft): Promise<McpServer> {
  const payload = draftToPayload(draft)
  delete payload.slug
  return api<McpServer>(`/mcp/servers/${encodeURIComponent(id)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}

export function setMcpServerEnabled(id: string, enabled: boolean): Promise<McpServer> {
  return api<McpServer>(`/mcp/servers/${encodeURIComponent(id)}/enabled`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled }),
  })
}

export function deleteMcpServer(id: string): Promise<{ deleted: boolean }> {
  return api<{ deleted: boolean }>(`/mcp/servers/${encodeURIComponent(id)}`, { method: 'DELETE' })
}

export function testMcpServer(id: string): Promise<McpTestResponse> {
  return api<McpTestResponse>(`/mcp/servers/${encodeURIComponent(id)}/test`, { method: 'POST' })
}

export function refreshMcpServer(id: string): Promise<McpServer> {
  return api<McpServer>(`/mcp/servers/${encodeURIComponent(id)}/refresh`, { method: 'POST' })
}

export function getMcpCatalog(id: string): Promise<McpCatalogResponse> {
  return api<McpCatalogResponse>(`/mcp/servers/${encodeURIComponent(id)}/catalog`)
}

export function setMcpGrants(id: string, kind: McpGrantKind, allowedKeys: string[]): Promise<{ kind: string; allowed_keys: string[] }> {
  return api<{ kind: string; allowed_keys: string[] }>(`/mcp/servers/${encodeURIComponent(id)}/grants/${kind}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ allowed_keys: allowedKeys }),
  })
}

export const policyLabels: Record<McpAccessPolicy, string> = {
  owner_only: '仅管理员私聊',
  private_users: '指定私聊白名单',
}

