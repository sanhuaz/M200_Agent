export type McpTransport = 'stdio' | 'sse' | 'streamable_http'
export type McpAccessPolicy = 'owner_only' | 'private_users'
export type McpGrantKind = 'tool' | 'resource' | 'prompt'

export type McpCatalogEntry = {
  key: string
  name?: string
  title?: string | null
  description?: string | null
  uri?: string
  uri_template?: string
  mime_type?: string | null
  size?: number | null
  input_schema?: Record<string, unknown>
  output_schema?: Record<string, unknown> | null
  arguments?: Array<Record<string, unknown>> | null
  allowed?: boolean
}

export type McpCatalog = {
  tools: McpCatalogEntry[]
  resources: McpCatalogEntry[]
  resource_templates: McpCatalogEntry[]
  prompts: McpCatalogEntry[]
}

export type McpServer = {
  id: string
  name: string
  slug: string
  transport: McpTransport
  config: Record<string, unknown>
  secret_refs: Record<string, string>
  secrets_configured: Record<string, boolean>
  access_policy: McpAccessPolicy
  private_users: string[]
  enabled: boolean
  status: string
  last_error?: string | null
  server_info: Record<string, unknown>
  catalog_summary: {
    tools: number
    resources: number
    resource_templates: number
    prompts: number
  }
  last_connected_at?: string | null
  last_refreshed_at?: string | null
  created_at: string
  updated_at: string
}

export type McpServerDraft = {
  name: string
  slug: string
  transport: McpTransport
  configText: string
  secretValuesText: string
  accessPolicy: McpAccessPolicy
  privateUsersText: string
}

export type McpCatalogResponse = {
  server_id: string
  status: string
  catalog: McpCatalog
}

export type McpTestResponse = {
  ok: boolean
  server_info: Record<string, unknown>
  catalog: McpCatalog
  catalog_summary: Record<string, number>
}
