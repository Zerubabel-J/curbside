import React, { useCallback, useEffect, useState } from 'react'
import { api } from './api.js'
import { Stat, Lead, LeadRow } from './components.jsx'

const TABS = [
  { key: 'composed', label: 'Review' },
  { key: 'approved', label: 'Approved' },
  { key: 'mailed', label: 'Mailed' },
  { key: 'rejected', label: 'Rejected' },
  { key: 'failed', label: 'Failed' },
  { key: '', label: 'All' },
]

export default function App() {
  const [config, setConfig] = useState(null)
  const [stats, setStats] = useState(null)
  const [tab, setTab] = useState('composed')
  const [leads, setLeads] = useState([])
  const [busy, setBusy] = useState(false)
  const [job, setJob] = useState(null)
  const [error, setError] = useState(null)

  const refresh = useCallback(async () => {
    try {
      const [s, l] = await Promise.all([api.stats(), api.leads(tab)])
      setStats(s); setLeads(l); setError(null)
    } catch (e) { setError(e.message) }
  }, [tab])

  useEffect(() => { api.config().then(setConfig).catch(e => setError(e.message)) }, [])
  useEffect(() => { refresh() }, [refresh])

  // poll a running pipeline job
  useEffect(() => {
    if (!job || job.status !== 'running') return
    const t = setInterval(async () => {
      try {
        const j = await api.runStatus(job.job_id || job.id)
        setJob({ ...j, job_id: j.id })
        if (j.status !== 'running') { refresh(); }
      } catch { clearInterval(t) }
    }, 2000)
    return () => clearInterval(t)
  }, [job, refresh])

  const decide = async (fn, id) => {
    setBusy(true)
    try { await fn(id); await refresh() }
    catch (e) { setError(e.message) }
    finally { setBusy(false) }
  }

  const startRun = async () => {
    setBusy(true); setError(null)
    try { const j = await api.run({}); setJob(j) }
    catch (e) { setError(e.message) }
    finally { setBusy(false) }
  }

  const sendMail = async () => {
    setBusy(true); setError(null)
    try {
      const r = await api.mail({})
      setJob({ status: 'completed', stage: 'mail', log: r.log, id: 'mail' })
      await refresh()
    } catch (e) { setError(e.message) }
    finally { setBusy(false) }
  }

  const spend = stats?.total_spend ?? 0
  const budget = stats?.budget ?? 0
  const counts = stats?.counts || {}
  const pending = counts.composed || 0

  return (
    <>
      <header className="top">
        <div className="brand">Curb<span>side</span></div>
        {config && (
          <span className="pill" title={config.source.name}>
            {config.source.name} · {config.source.license}
          </span>
        )}
        <div className="spacer" />
        {config && !config.checks.gemini_key && <span className="pill">no API key</span>}
        <span className="pill">${spend.toFixed(4)} / ${budget.toFixed(2)}</span>
        <button onClick={startRun} disabled={busy || job?.status === 'running'}>
          {job?.status === 'running' ? `Running… ${job.stage}` : 'Run pipeline'}
        </button>
        <button className="primary" onClick={sendMail}
                disabled={busy || !(counts.approved > 0)}>
          Send {counts.approved || 0}
        </button>
      </header>

      <main className="wrap">
        {error && <div className="banner err" style={{ marginBottom: '1rem' }}>{error}</div>}

        <div className="grid">
          <Stat k="Awaiting review" v={pending}
                s={pending ? 'needs your approval' : 'all clear'} />
          <Stat k="Mailed" v={counts.mailed || 0}
                s={config?.checks.live_mail_enabled ? 'live' : 'dry run'} />
          <Stat k="Rejected" v={counts.rejected || 0} s="by the qualifier" />
          <Stat k="Spend" v={`$${spend.toFixed(2)}`}
                s={stats?.per_piece != null ? `$${stats.per_piece.toFixed(3)} / piece` : '—'} />
        </div>

        {budget > 0 && (
          <div style={{ marginTop: '.75rem' }}>
            <div className="bar">
              <i style={{ width: `${Math.min(100, (spend / budget) * 100)}%` }} />
            </div>
          </div>
        )}

        {job?.log?.length > 0 && (
          <div className="card" style={{ marginTop: '1rem' }}><div className="pad">
            <div className="meta" style={{ marginBottom: '.5rem' }}>
              <b>{job.stage}</b><span className="pill">{job.status}</span>
            </div>
            <div className="log">{job.log.slice(-40).join('\n')}</div>
            {job.error && <div className="banner err" style={{ marginTop: '.5rem' }}>{job.error}</div>}
          </div></div>
        )}

        <nav className="tabs" role="tablist">
          {TABS.map(t => (
            <button key={t.key} className="tab" role="tab"
                    aria-selected={tab === t.key} onClick={() => setTab(t.key)}>
              {t.label}{counts[t.key] ? ` (${counts[t.key]})` : ''}
            </button>
          ))}
        </nav>

        {leads.length === 0 ? (
          <div className="empty">
            Nothing here. {tab === 'composed' && 'Run the pipeline to generate postcards.'}
          </div>
        ) : (
          <div style={{ display: 'grid', gap: '.75rem' }}>
            {leads.map(l => (
              l.state === 'composed' && l.has_after
                ? <Lead key={l.id} lead={l} busy={busy}
                        onApprove={id => decide(api.approve, id)}
                        onReject={id => decide(api.reject, id)} />
                : <LeadRow key={l.id} lead={l} />
            ))}
          </div>
        )}

        <div className="foot">
          {stats?.source?.attribution}
        </div>
      </main>
    </>
  )
}
