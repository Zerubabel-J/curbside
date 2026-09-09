import React, { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api.js'

// Verified against the live pipeline - each of these returns neighbours and
// produces at least one postcard. Indiana only: that is where the 3in CC0
// imagery is sharp enough to resolve a driveway.
const EXAMPLES = [
  { address: '5410 N Illinois St, Indianapolis, IN 46208', note: 'Meridian-Kessler · 4 of 6 qualify' },
  { address: '8102 Talliho Dr, Indianapolis, IN 46256',    note: 'Geist suburb · 3 of 6 qualify' },
  { address: '5802 Central Ave, Indianapolis, IN 46220',   note: 'Broad Ripple · 3 of 6 qualify' },
  { address: '4920 N Pennsylvania St, Indianapolis, IN 46205', note: 'Historic North · 2 of 6' },
  { address: '1240 Fairfield Ave, Indianapolis, IN 46205', note: 'Mid-century grid · 2 of 6' },
  { address: '6301 N Keystone Ave, Indianapolis, IN 46220', note: 'Keystone · 1 of 6' },
]

function StepRow({ step, elapsed }) {
  const icon = { done: '✓', running: '', error: '!' }[step.state] ?? ''
  return (
    <li className={`step ${step.state}`}>
      <span className="dot">{step.state === 'running' ? <i className="spin" /> : icon}</span>
      <span className="label">{step.label}</span>
      <span className="detail">
        {step.detail || (step.state === 'running' && elapsed > 2 ? `${elapsed}s` : '')}
      </span>
    </li>
  )
}

function ResultCard({ lead, onSend, busy }) {
  const q = lead.qualification || {}
  const qc = lead.qc || {}
  const ready = lead.state === 'composed' || lead.state === 'approved'
  const sent = lead.state === 'mailed'
  const qcFailed = lead.state === 'failed'
  const userRejected = lead.state === 'rejected' && (lead.qualification || {}).qualified

  return (
    <article className={`result ${ready ? 'ready' : 'skip'}`}>
      {lead.has_after ? (
        <div className="ba">
          <figure><img src={api.imageUrl(lead.id, 'before')} alt="" loading="lazy" />
            <figcaption>Before</figcaption></figure>
          <figure><img src={api.imageUrl(lead.id, 'after')} alt="" loading="lazy" />
            <figcaption className="hi">After</figcaption></figure>
        </div>
      ) : lead.has_before ? (
        <div className="ba single">
          <figure><img src={api.imageUrl(lead.id, 'before')} alt="" loading="lazy" />
            <figcaption>
              {qcFailed ? 'Retry needed' : userRejected ? 'Rejected' : 'Not a fit'}
            </figcaption></figure>
        </div>
      ) : <div className="ba placeholder" />}

      <div className={`body ${ready || sent ? 'compact' : ''}`}>
        <h4>{lead.address}</h4>
        {ready || sent ? (
          <p className="tags">
            <span className="tag">{q.surface}</span>
            <span className="tag">condition {q.condition_score}/10</span>
            {qc.mask?.mode && <span className="tag ghost">{qc.mask.mode}</span>}
          </p>
        ) : userRejected ? (
          <p className="muted">You rejected this one.</p>
        ) : qcFailed ? (
          <p className="muted">
            Driveway found, but the render did not pass quality checks.
            Worth a retry.
          </p>
        ) : (
          <p className="muted">{q.reason || 'Not a candidate'}</p>
        )}

        {sent ? (
          <div className="sent">✓ Postcard sent</div>
        ) : ready ? (
          <div className="actions">
            <button className="send" disabled={busy} onClick={() => onSend(lead.id)}>
              Send postcard
            </button>
            <a className="link" href={api.imageUrl(lead.id, 'postcard')}
               target="_blank" rel="noreferrer">Preview</a>
          </div>
        ) : null}
      </div>
    </article>
  )
}

export default function Scan({ onDone, config }) {
  const [address, setAddress] = useState('')
  const [recentOnly, setRecentOnly] = useState(false)
  const [job, setJob] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const timer = useRef(null)
  const [elapsed, setElapsed] = useState(0)
  const stepStart = useRef(Date.now())
  const lastStepCount = useRef(0)

  const poll = useCallback(async (id) => {
    try {
      const j = await api.scanStatus(id)
      setJob(j)
      if (j.status === 'running') {
        timer.current = setTimeout(() => poll(id), 2000)
      } else {
        setBusy(false)
        onDone?.()
      }
    } catch (e) { setError(e.message); setBusy(false) }
  }, [onDone])

  useEffect(() => () => clearTimeout(timer.current), [])

  // A long step should look like progress, not a stall - tick a counter while
  // one is running, resetting whenever the pipeline moves on.
  useEffect(() => {
    if (job?.status !== 'running') { setElapsed(0); return }
    const n = job.steps?.length || 0
    if (n !== lastStepCount.current) {
      lastStepCount.current = n
      stepStart.current = Date.now()
      setElapsed(0)
    }
    const t = setInterval(
      () => setElapsed(Math.round((Date.now() - stepStart.current) / 1000)), 1000)
    return () => clearInterval(t)
  }, [job])

  // Deep links: ?job=<id> reopens a scan, ?address=... runs one. Handy for
  // sharing a specific result without re-running it.
  useEffect(() => {
    const p = new URLSearchParams(window.location.search)
    const jobId = p.get('job')
    const addr = p.get('address')
    if (jobId) { setBusy(true); poll(jobId) }
    else if (addr) { setAddress(addr) }
  }, [poll])

  const start = async (e, override) => {
    e?.preventDefault()
    const addr = (override ?? address).trim()
    if (!addr) return
    setBusy(true); setError(null); setJob(null)
    try {
      const { job_id } = await api.scan({
        address: addr, radius_m: 200, limit: 6,
        sold_within_months: recentOnly ? 12 : null,
      })
      poll(job_id)
    } catch (e) { setError(e.message); setBusy(false) }
  }

  const send = async (id) => {
    setBusy(true)
    try {
      await api.send(id, 'sent from scan results')
      if (job?.id) await poll(job.id)
    } catch (e) { setError(e.message) } finally { setBusy(false) }
  }

  const leads = job?.leads || []
  const ready = leads.filter(l => ['composed', 'approved', 'mailed'].includes(l.state))
  const skipped = leads.filter(l => !['composed', 'approved', 'mailed'].includes(l.state))
  // A QC failure is a different story from "no driveway here" - one is worth
  // retrying, the other never will be.
  const retryable = leads.filter(l => l.state === 'failed')

  return (
    <section className="scan">
      {!job && (
        <div className="hero">
          <h1>Find every driveway on the block.</h1>
          <p>Enter a job site address. We'll find the neighbours, spot the worn
             driveways, and generate a ready-to-mail postcard for each one.</p>

          <form className="searchbar" onSubmit={start}>
            <input value={address} onChange={e => setAddress(e.target.value)}
                   placeholder="Enter a job site address…" disabled={busy} />
            <button type="submit" disabled={busy || !address.trim()}>
              {busy ? 'Scanning…' : 'Scan block'}
            </button>
          </form>
          <div className="examples">
            {config?.supports_sale_date && (
            <label className="toggle">
              <input type="checkbox" checked={recentOnly} disabled={busy}
                     onChange={e => setRecentOnly(e.target.checked)} />
              <span>Only homes sold in the last 12 months</span>
            </label>
          )}

          <p className="try">Or try a verified block:</p>
            <div className="chips">
              {EXAMPLES.map(ex => (
                <button key={ex.address} className="example" disabled={busy}
                        onClick={() => { setAddress(ex.address); start(null, ex.address) }}>
                  <span className="ex-addr">{ex.address.split(',')[0]}</span>
                  <span className="ex-note">{ex.note}</span>
                </button>
              ))}
            </div>
            <p className="scope">
              Indiana addresses only — that is where the public imagery is
              3 inch/pixel, sharp enough to resolve a driveway.
            </p>
          </div>
        </div>
      )}

      {error && <div className="banner err">{error}</div>}

      {job && (
        <div className="progress">
          <div className="phead">
            <span className={`chip ${job.status}`}>
              {job.status === 'running' ? 'Scanning block' :
               job.status === 'failed' ? 'Scan failed' : 'Scan complete'}
            </span>
            {job.status === 'running' && (
              <p className="hint">Renders take about 15 seconds each and run in
                 parallel — a full block is usually under a minute.</p>
            )}
            <h2>{job.address}</h2>
          </div>
          <ul className="steps">
            {(job.steps || []).map((s, i) => (
              <StepRow key={i} step={s}
                       elapsed={i === (job.steps.length - 1) ? elapsed : 0} />
            ))}
          </ul>
          {job.error && <div className="banner err">{job.error}</div>}
        </div>
      )}

      {job?.status !== 'running' && leads.length > 0 && (
        <>
          {ready.length === 0 && retryable.length > 0 && (
            <div className="banner note">
              Found {retryable.length} driveway{retryable.length > 1 ? 's' : ''} here,
              but the render did not pass quality checks. Scan again to retry —
              the models vary between runs.
            </div>
          )}

          {ready.length === 0 && retryable.length === 0 && (
            <div className="banner note">
              No candidates on this block — the scan worked, but none of these
              properties has a driveway we can upgrade. Commercial lots,
              shared parking and homes without a visible driveway are all
              skipped. Try a residential street.
            </div>
          )}

          <div className="summary">
            <b>{leads.length}</b> homes scanned
            <span className="sep" />
            <b className="good">{ready.length}</b> need a new driveway
            <span className="sep" />
            <b className="dim">{skipped.length}</b> skipped
            <button className="asLink right" onClick={() => setJob(null)}>Scan another block</button>
          </div>

          <div className="grid-results">
            {ready.map(l => <ResultCard key={l.id} lead={l} onSend={send} busy={busy} />)}
            {skipped.map(l => <ResultCard key={l.id} lead={l} onSend={send} busy={busy} />)}
          </div>
        </>
      )}
    </section>
  )
}
