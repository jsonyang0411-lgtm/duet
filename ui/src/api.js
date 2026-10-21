// All calls go to the same origin as the page (/api/...). The desktop app and
// the tests serve the UI from the bot's own web server; `npm run dev` proxies
// /api to 127.0.0.1:7327 (vite.config.js).

export class ApiError extends Error {
  constructor(message, { status = 0, data = null } = {}) {
    super(message)
    this.status = status
    this.data = data
  }
}

export async function api(path, { method = 'GET', body, signal, query } = {}) {
  let url = path
  if (query) {
    const qs = new URLSearchParams()
    for (const [k, v] of Object.entries(query)) {
      if (v !== undefined && v !== null && v !== '') qs.set(k, String(v))
    }
    const s = qs.toString()
    if (s) url += (url.includes('?') ? '&' : '?') + s
  }
  let res
  try {
    res = await fetch(url, {
      method,
      signal,
      cache: 'no-store',
      headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    })
  } catch (e) {
    if (e.name === 'AbortError') throw e
    throw new ApiError('连不上控制台服务（webot 可能已经退出）')
  }
  let data = null
  try { data = await res.json() } catch { /* non-JSON body */ }
  if (!res.ok) {
    throw new ApiError((data && data.error) || `请求失败（HTTP ${res.status}）`, { status: res.status, data })
  }
  return data ?? {}
}

/** Like api(), but also treats `{ok: false}` as an error. */
export async function apiOk(path, opts) {
  const data = await api(path, opts)
  if (data && data.ok === false) {
    throw new ApiError(data.error || data.message || '操作失败', { status: 200, data })
  }
  return data
}

export const get = (path, query) => apiOk(path, { query })
export const post = (path, body = {}) => apiOk(path, { method: 'POST', body })
