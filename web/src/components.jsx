import React from 'react'
import { api } from './api.js'

export function Stat({ k, v, s }) {
  return (
    <div className="card stat"><div className="pad">
      <div className="k">{k}</div>
      <div className="v">{v}</div>
      {s && <div className="s">{s}</div>}
    </div></div>
  )
}

export function Lead({ lead, onApprove, onReject, busy }) {
  const q = lead.qualification || {}
  const qc = lead.qc || {}
  const blocked = lead.compliance && !lead.compliance.allowed
  const pct = (x) => (x == null ? '—' : `${(x * 100).toFixed(1)}%`)

  return (
    <div className="card lead">
      <div className="pair">
        <figure className="before">
          <img src={api.imageUrl(lead.id, 'before')} alt="current property" loading="lazy" />
          <figcaption>TODAY</figcaption>
        </figure>
        <figure className="after">
          <img src={api.imageUrl(lead.id, 'after')} alt="with new driveway" loading="lazy" />
          <figcaption>AFTER</figcaption>
        </figure>
      </div>

      <div className="side">
        <div className="addr">{lead.address}</div>

        <div className="meta">
          <span><b>{q.surface || '—'}</b> surface</span>
          <span>condition <b>{q.condition_score ?? '—'}/10</b></span>
          {q.obstruction && q.obstruction !== 'none' && <span>obstruction <b>{q.obstruction}</b></span>}
        </div>

        <div className="meta">
          <span>mask <b>{pct(qc.mask_frac)}</b></span>
          <span>drift <b>{pct(qc.outside_drift_frac)}</b></span>
          {qc.mask?.mode && <span>via <b>{qc.mask.mode}</b></span>}
          {qc.semantic?.highlighted_object && <span>region <b>{qc.semantic.highlighted_object}</b></span>}
        </div>

        {q.reason && <div style={{ fontSize: '.8rem', color: 'var(--muted)' }}>{q.reason}</div>}

        {blocked && (
          <div className="banner warn">
            Blocked: {lead.compliance.reasons[0]}
          </div>
        )}

        <div className="btns">
          <button className="primary" disabled={busy || blocked}
                  onClick={() => onApprove(lead.id)}>
            Approve
          </button>
          <button className="danger" disabled={busy} onClick={() => onReject(lead.id)}>
            Reject
          </button>
          <a className="pill" style={{ alignSelf: 'center', textDecoration: 'none' }}
             href={api.imageUrl(lead.id, 'postcard')} target="_blank" rel="noreferrer">
            postcard ↗
          </a>
        </div>
      </div>
    </div>
  )
}

export function LeadRow({ lead }) {
  const q = lead.qualification || {}
  return (
    <div className="card"><div className="pad" style={{ display: 'flex', gap: '1rem', alignItems: 'center' }}>
      <div style={{ flex: 1 }}>
        <div style={{ fontWeight: 600 }}>{lead.address}</div>
        <div className="meta">
          <span className="pill">{lead.state}</span>
          {q.surface && <span>{q.surface} · {q.condition_score}/10</span>}
          {lead.fail_error && <span style={{ color: 'var(--accent)' }}>{lead.fail_error.slice(0, 70)}</span>}
          {q.reason && !lead.fail_error && <span>{q.reason.slice(0, 80)}</span>}
        </div>
      </div>
      {lead.has_before && (
        <img src={api.imageUrl(lead.id, 'before')} alt="" loading="lazy"
             style={{ width: 56, height: 56, borderRadius: 6, objectFit: 'cover' }} />
      )}
    </div></div>
  )
}
