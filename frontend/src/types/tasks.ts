export type TaskRow = {
  id: string
  type: string
  status: string
  error?: string
  result?: {
    path?: string
    delivery_status?: string
    artifact_deleted?: boolean
  }
}

export type Confirmation = {
  token: string
  action: string
  payload: Record<string, unknown>
  status: string
  expires_at: string
}
