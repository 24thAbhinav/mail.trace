// api.ts - typed API client for the FastAPI backend

import axios from 'axios'
import type {
  Application,
  ApplicationEvent,
  ApplicationListResponse,
  ApplicationStats,
  ApplicationStatus,
} from '../types'

// In production VITE_API_URL points to the Render backend (e.g. https://my-app.onrender.com).
// In local dev it is unset and the Vite proxy forwards /api → localhost:8000.
const BASE_URL = import.meta.env.VITE_API_URL
  ? `${import.meta.env.VITE_API_URL}`
  : '/api'

const api = axios.create({
  baseURL: BASE_URL,
  headers: { 'Content-Type': 'application/json' },
  withCredentials: true,
})

export interface ApplicationFilters {
  status?: string
  company?: string
  search?: string
  limit?: number
  offset?: number
  include_closed?: boolean
}

export async function getApplicationStats(): Promise<ApplicationStats> {
  const { data } = await api.get<ApplicationStats>('/applications/stats')
  return data
}

export async function getApplications(
  filters: ApplicationFilters = {},
): Promise<ApplicationListResponse> {
  const { data } = await api.get<ApplicationListResponse>('/applications', {
    params: filters,
  })
  return data
}

export async function getApplication(id: number): Promise<Application> {
  const { data } = await api.get<Application>(`/applications/${id}`)
  return data
}

export async function getApplicationEvents(
  id: number,
): Promise<ApplicationEvent[]> {
  const { data } = await api.get<ApplicationEvent[]>(`/applications/${id}/events`)
  return data
}

export async function updateApplication(
  id: number,
  updates: {
    status?: ApplicationStatus
    is_closed?: boolean
    notes?: string
  },
): Promise<Application> {
  const { data } = await api.patch<Application>(`/applications/${id}`, updates)
  return data
}

export async function setApplicationClosed(
  id: number,
  isClosed: boolean,
): Promise<Application> {
  return updateApplication(id, { is_closed: isClosed })
}

export default api

// Gmail & auth endpoints
export async function getAuthUrl(): Promise<string> {
  // In production, redirect to the Render backend's /auth/google endpoint.
  const base = import.meta.env.VITE_API_URL ?? ''
  return `${base}/auth/google`
}

export async function registerWatch(): Promise<{ history_id: string | null; expiration: string | null }> {
  const { data } = await api.post('/gmail/watch')
  return data
}

export interface SyncResult {
  fetched: number
  processed: unknown[]
}

export async function triggerFullSync(): Promise<SyncResult> {
  const { data } = await api.post<SyncResult>('/gmail/sync')
  return data
}
