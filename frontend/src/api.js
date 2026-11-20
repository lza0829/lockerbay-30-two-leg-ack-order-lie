async function req(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export const api = {
  health: () => req('/api/health'),
  overview: () => req('/api/overview'),
  bay: () => req('/api/bay'),
  occupancies: () => req('/api/occupancies'),
  events: () => req('/api/events'),
  quarantine: () => req('/api/quarantine'),
  proofs: () => req('/api/occupancy-proofs'),
  closeLegs: () => req('/api/close-legs'),
  closeCandidates: () => req('/api/close-candidates'),
  twoLegQuarantine: () => req('/api/two-leg/quarantine'),
  verifyTwoLeg: (occupancyId) => req(`/api/two-leg/verify/${encodeURIComponent(occupancyId)}`),
  auditTwoLeg: () => req('/api/two-leg/audit'),
  sweepTwoLeg: () => req('/api/two-leg/sweep', { method: 'POST', body: '{}' }),
  settings: () => req('/api/settings'),
  setOverdue: (overdue_hours) =>
    req('/api/settings/overdue-hours', { method: 'PUT', body: JSON.stringify({ overdue_hours }) }),
  setPairTimeout: (pair_timeout_minutes) =>
    req('/api/settings/pair-timeout', {
      method: 'PUT',
      body: JSON.stringify({ pair_timeout_minutes }),
    }),
  ingest: () => req('/api/ingest/run', { method: 'POST', body: '{}' }),
  twoLegAck: (id) => req(`/api/two-leg-ack/${id}`),
}
