import React, { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api.js'

// The lead sheet is the working view: thousands of rows, worked through in
// batches. The scan view answers "what does this block look like"; this one
// answers "what is in my pipeline and what do I do next", which is the
// question a contractor actually opens the tool with.

const STATES = [
  { key: '', label: 'All' },
  { key: 'discovered', label: 'New' },
  { key: 'composed', label: 'Postcard ready' },
  { key: 'mailed', label: 'Mailed' },
  { key: 'rejected', label: 'Skipped' },
  { key: 'failed', label: 'Failed' },
]

// A page holds what a person can actually scan without scrolling past the
// action cards. The table is for working through leads one at a time, not for
// admiring the size of the list.
const PAGE = 10

const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
                'August', 'September', 'October', 'November', 'December']

// `lead_source` holds the county for anything pulled from the records. Older
// leads carry the route they arrived by - `sold_list`, `block_scan` - which is
// not a county and should not be printed as one.
const COUNTIES = {
  miami_dade: 'Miami-Dade', palm_beach: 'Palm Beach', broward: 'Broward',
}

function county(source) {
  return COUNTIES[source] || '—'
}

function money(n) {
  if (n == null) return '—'
  return '$' + Math.round(n).toLocaleString()
}

function StatusChip({ state }) {
  const label = {
    discovered: 'New', imaged: 'Photographed', qualified: 'Candidate',
    rendered: 'Rendered', composed: 'Ready', approved: 'Approved',
    mailed: 'Mailed', rejected: 'Skipped', failed: 'Failed',
    blocked: 'Blocked', suppressed: 'Suppressed',
  }[state] || state
  return <span className={`chip-${state}`}>{label}</span>
}

export default function Sheet({ onOpenScan }) {
  const [data, setData] = useState(null)
  const [state, setState] = useState('')
  const [search, setSearch] = useState('')
  const [busy, setBusy] = useState(false)
  const [job, setJob] = useState(null)
  const [error, setError] = useState(null)
  const [offset, setOffset] = useState(0)

  const now = new Date()
  const [year, setYear] = useState(now.getFullYear())
  const [month, setMonth] = useState(now.getMonth())   // 0-based; last month
  const [minPrice, setMinPrice] = useState(700000)
  const [batch, setBatch] = useState(25)
  const [templates, setTemplates] = useState([])
  const [template, setTemplate] = useState('')
  const timer = useRef(null)

  const load = useCallback(async () => {
    try {
      const d = await api.sheet({ state, q: search, limit: PAGE, offset })
      // A running job changes the counts under you - generating 10 postcards
      // moves ten leads out of "New" while you are reading page 4 of it. Step
      // back rather than showing an empty table on a filter that still has
      // rows in it.
      if (d.rows.length === 0 && d.total > 0 && offset > 0) {
        setOffset(Math.max(0, Math.floor((d.total - 1) / PAGE) * PAGE))
        return
      }
      setData(d); setError(null)
    } catch (e) { setError(e.message) }
  }, [state, search, offset])

  useEffect(() => { load() }, [load])

  useEffect(() => {
    api.templates()
       .then(d => { setTemplates(d.templates || []); setTemplate(d.default || '') })
       .catch(() => { /* the picker is optional; the default still applies */ })
  }, [])

  // While a pull or a generate runs, keep the table and counters live so the
  // numbers move as work completes rather than after it.
  useEffect(() => {
    if (!job || job.status !== 'running') return
    timer.current = setInterval(async () => {
      try {
        const j = await api.scanStatus(job.job_id || job.id)
        setJob(j)
        load()
        if (j.status !== 'running') { setBusy(false); clearInterval(timer.current) }
      } catch { /* keep polling */ }
    }, 2500)
    return () => clearInterval(timer.current)
  }, [job, load])

  const start = async (fn, body) => {
    setBusy(true); setError(null)
    try {
      const j = await fn(body)
      setJob({ ...j, status: 'running' })
    } catch (e) { setError(e.message); setBusy(false) }
  }

  const counts = data?.counts || {}
  const ready = counts.composed || 0
  const rows = data?.rows || []

  return (
    <section className="sheet">
      <header className="sheet-head">
        <div>
          <h1>Leads</h1>
          <p>Single-family homes sold in Miami-Dade, Broward and Palm Beach,
             from county property records. Pull a month, generate postcards,
             review each one, then mail.</p>
        </div>
        <button className="asLink" onClick={onOpenScan}>Scan a single block →</button>
      </header>

      <div className="counters">
        <button className={state === '' ? 'on' : ''} onClick={() => { setState(''); setOffset(0) }}>
          All <b>{data?.all ?? 0}</b>
        </button>
        {STATES.slice(1).map(s => (
          <button key={s.key} className={state === s.key ? 'on' : ''}
                  onClick={() => { setState(s.key); setOffset(0) }}>
            {s.label} <b>{counts[s.key] ?? 0}</b>
          </button>
        ))}
      </div>

      <div className="actions-row">
        <div className="action-card">
          <h4>1 · Pull sales</h4>
          <p>Homes sold in one month, above your price floor.</p>
          <div className="controls">
            <select value={month} disabled={busy}
                    onChange={e => setMonth(Number(e.target.value))}>
              {MONTHS.map((m, i) => <option key={m} value={i}>{m}</option>)}
            </select>
            <select value={year} disabled={busy}
                    onChange={e => setYear(Number(e.target.value))}>
              {[0, 1, 2].map(d => {
                const y = now.getFullYear() - d
                return <option key={y} value={y}>{y}</option>
              })}
            </select>
            <select value={minPrice} disabled={busy}
                    onChange={e => setMinPrice(Number(e.target.value))}>
              <option value={400000}>$400k+</option>
              <option value={700000}>$700k+</option>
              <option value={1000000}>$1M+</option>
              <option value={2000000}>$2M+</option>
            </select>
            <button className="primary" disabled={busy}
                    onClick={() => start(api.pull,
                      { year, month: month + 1, min_price: minPrice })}>
              Pull sales
            </button>
          </div>
        </div>

        <div className="action-card">
          <h4>2 · Generate postcards</h4>
          <p>Photographs each home, decides what driveway suits it, renders it
             and lays out the card. <b>{counts.discovered ?? 0}</b> waiting.</p>
          <div className="controls">
            <select value={batch} disabled={busy}
                    onChange={e => setBatch(Number(e.target.value))}>
              {[5, 10, 25, 50].map(n => <option key={n} value={n}>{n} homes</option>)}
            </select>
            {templates.length > 0 && (
              <select value={template} disabled={busy} title="Postcard copy"
                      onChange={e => setTemplate(e.target.value)}>
                {templates.map(t => (
                  <option key={t.key} value={t.key} title={t.note}>{t.name}</option>
                ))}
              </select>
            )}
            <button className="primary" disabled={busy || !(counts.discovered)}
                    onClick={() => start(api.generate,
                      { limit: batch, template: template || null })}>
              Generate
            </button>
          </div>
        </div>

        <div className="action-card">
          <h4>3 · Review and mail</h4>
          <p><b>{ready}</b> postcard{ready === 1 ? '' : 's'} ready for your
             approval. Nothing is mailed until you approve it.</p>
          <div className="controls">
            <button disabled={!ready}
                    onClick={() => { setState('composed'); setOffset(0) }}>
              Review {ready > 0 ? ready : ''}
            </button>
          </div>
        </div>
      </div>

      {job && (
        <div className={`job-strip ${job.status}`}>
          {(job.steps || []).map((s, i) => (
            <span key={i} className={`job-step ${s.state}`}>
              {s.state === 'done' ? '✓' : s.state === 'error' ? '!' : '…'} {s.label}
              {s.detail ? <em> — {s.detail}</em> : null}
            </span>
          ))}
          {job.error && <span className="job-step error">{job.error}</span>}
        </div>
      )}

      {error && <div className="banner err">{error}</div>}

      <div className="sheet-filter">
        <input value={search} placeholder="Find an address…"
               onChange={e => { setSearch(e.target.value); setOffset(0) }} />
        <span className="showing">
          Showing {rows.length} of {data?.total ?? 0}
        </span>
      </div>

      <div className="table-wrap">
        <table className="leads">
          <thead>
            <tr>
              <th>Sold</th><th>Address</th><th>County</th><th className="num">Price</th>
              <th>Owner</th><th>Status</th><th>Driveway</th><th>Postcard</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(l => {
              const q = l.qualification || {}
              const ok = l.state === 'composed' || l.state === 'approved'
                      || l.state === 'mailed'
              return (
                <tr key={l.id} className={ok ? 'has-card' : ''}>
                  <td className="dim">{l.sale_date || '—'}</td>
                  <td className="addr">{l.address}</td>
                  <td className="dim">{county(l.lead_source)}</td>
                  <td className="num">{money(l.sale_price)}</td>
                  <td className="dim owner">{l.owner || '—'}</td>
                  <td><StatusChip state={l.state} /></td>
                  <td className="dim">{q.best_shape || '—'}</td>
                  <td>
                    {ok
                      ? <a href={api.imageUrl(l.id, 'postcard')} target="_blank"
                           rel="noreferrer">View</a>
                      : <span className="dim">—</span>}
                  </td>
                </tr>
              )
            })}
            {rows.length === 0 && (
              <tr><td colSpan={8} className="empty">
                No leads yet — pull a month of sales to begin.
              </td></tr>
            )}
          </tbody>
        </table>
      </div>

      {(data?.total ?? 0) > PAGE && (
        <div className="pager">
          <button disabled={offset === 0}
                  onClick={() => setOffset(Math.max(0, offset - PAGE))}>Previous</button>
          <span>
            {rows.length ? offset + 1 : 0}–{offset + rows.length} of {data.total}
            <em> · page {Math.floor(offset / PAGE) + 1} of {Math.ceil(data.total / PAGE)}</em>
          </span>
          <button disabled={offset + rows.length >= data.total}
                  onClick={() => setOffset(offset + PAGE)}>Next</button>
        </div>
      )}
    </section>
  )
}
