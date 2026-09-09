const BASE = import.meta.env.VITE_API_BASE || '/api'

async function req(path, opts = {}) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  })
  if (!res.ok) {
    const body = await res.text()
    throw new Error(`${res.status}: ${body.slice(0, 200)}`)
  }
  return res.json()
}

export const api = {
  config: () => req('/config'),
  stats: () => req('/stats'),
  leads: (state, limit = 100) =>
    req(`/leads?${state ? `state=${state}&` : ''}limit=${limit}`),
  lead: (id) => req(`/leads/${id}`),
  events: (id) => req(`/events/${id}`),
  approve: (id, note) =>
    req(`/leads/${id}/approve`, { method: 'POST', body: JSON.stringify({ note }) }),
  reject: (id, note) =>
    req(`/leads/${id}/reject`, { method: 'POST', body: JSON.stringify({ note }) }),
  run: (body) => req('/run', { method: 'POST', body: JSON.stringify(body || {}) }),
  runStatus: (jobId) => req(`/run/${jobId}`),
  scan: (body) => req('/scan', { method: 'POST', body: JSON.stringify(body) }),
  scanStatus: (jobId) => req(`/scan/${jobId}`),
  scans: () => req('/scans'),
  send: (id, note) =>
    req(`/leads/${id}/send`, { method: 'POST', body: JSON.stringify({ note }) }),
  mail: (body) => req('/mail', { method: 'POST', body: JSON.stringify(body || {}) }),
  imageUrl: (id, kind) => `${BASE}/leads/${id}/image/${kind}`,
}
