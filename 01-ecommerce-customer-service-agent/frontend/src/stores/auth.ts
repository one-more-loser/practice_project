import { defineStore } from 'pinia'
import { ref } from 'vue'
import { request, setSession, ApiError } from '../lib/api'
import type { SessionView } from '../lib/types'
export const useAuth = defineStore('auth', () => {
  const session = ref<SessionView | null>(null)
  let loaded = false
  function apply(value: SessionView | null) {
    session.value = value
    setSession(
      value?.csrf_token ?? '',
      value ? `${value.subject_id}:${value.active_role}:${value.auth_version}` : 'guest',
    )
  }
  async function restore() {
    if (loaded) return
    try {
      apply(await request<SessionView>('/session'))
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 401) throw error
      apply(null)
    }
    loaded = true
  }
  async function login(name: string, password: string) {
    const value = await request<SessionView>('/session', 'POST', { login_name: name, password })
    apply(value)
    loaded = true
  }
  async function logout() {
    await request('/session', 'DELETE')
    apply(null)
    loaded = true
  }
  async function switchRole(role: string) {
    const value = await request<SessionView>('/session/role', 'PATCH', { role })
    apply(value)
  }
  return { session, restore, login, logout, switchRole }
})
