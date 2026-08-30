export type AdminRow = {
  id: string
  external_id: string
  display_name?: string
  platform: string
  enabled: boolean
}

export type HealthItem = {
  alias?: string
  model?: string
  configured?: boolean
}

export type NapcatStatus = {
  url: string
  configured: boolean
  status: string
  two_factor: boolean
  last_error?: string | null
  last_log_at?: string | null
}

export type HealthData = {
  status?: string
  database?: string
  chroma?: string
  worker?: string
  onebot?: string
  reranker?: string
  qq?: string
  napcat?: NapcatStatus
  logs?: { session_id?: string; events?: number }
  python_executable?: string
  models?: HealthItem[]
  embedding_profiles?: HealthItem[]
  [key: string]: unknown
}
