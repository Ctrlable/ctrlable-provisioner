import { useEffect, useState } from 'react'
import './App.css'

function fmtBytes(bytes) {
  if (!bytes) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  let v = bytes
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++ }
  return `${v.toFixed(1)} ${units[i]}`
}

function fmtUptime(s) {
  const d = Math.floor(s / 86400)
  const h = Math.floor((s % 86400) / 3600)
  const m = Math.floor((s % 3600) / 60)
  if (d > 0) return `${d}d ${h}h`
  if (h > 0) return `${h}h ${m}m`
  return `${m}m`
}

function HealthBar({ label, pct, sublabel }) {
  const p = Math.min(Math.round(pct), 100)
  const color = p > 90 ? '#ef4444' : p > 70 ? '#f59e0b' : '#22c55e'
  return (
    <div className="metric">
      <div className="metric-header">
        <span>{label}</span>
        <span>{p}%</span>
      </div>
      <div className="bar-bg">
        <div className="bar-fill" style={{ width: `${p}%`, background: color }} />
      </div>
      {sublabel && <div className="metric-sub">{sublabel}</div>}
    </div>
  )
}

function StatusDot({ status }) {
  const colors = { running: '#22c55e', stopped: '#6b7280', error: '#ef4444' }
  return <span className="status-dot" style={{ background: colors[status] ?? '#f59e0b' }} />
}

function GuestCard({ guest }) {
  const cpuPct = (guest.cpu * 100).toFixed(1)
  return (
    <div className="guest-card">
      <div className="guest-header">
        <StatusDot status={guest.status} />
        <span className="guest-hostname" title={guest.hostname}>{guest.hostname}</span>
        <span className="guest-kind">{guest.kind.toUpperCase()}</span>
      </div>
      <div className="guest-meta">
        {guest.type   && <span className="tag">{guest.type}</span>}
        {guest.release && <span className="tag accent">{guest.release}</span>}
        <span className="tag">{guest.status}</span>
      </div>
      {guest.status === 'running' && (
        <div className="guest-stats">
          <span>CPU {cpuPct}%</span>
          <span>{fmtBytes(guest.mem)} / {fmtBytes(guest.maxmem)}</span>
        </div>
      )}
    </div>
  )
}

function groupByProject(guests) {
  const map = {}
  for (const g of guests) {
    const key = g.project ?? '(untracked)'
    ;(map[key] ??= []).push(g)
  }
  return Object.entries(map).sort(([a], [b]) => a.localeCompare(b))
}

export default function App() {
  const [data, setData] = useState(null)
  const [fetchErr, setFetchErr] = useState(null)

  function load() {
    fetch('/api/dashboard')
      .then(r => r.json())
      .then(d => { setData(d); setFetchErr(null) })
      .catch(e => setFetchErr(e.message))
  }

  useEffect(() => {
    load()
    const id = setInterval(load, 30_000)
    return () => clearInterval(id)
  }, [])

  if (fetchErr) return (
    <div className="banner error">Cannot reach API: {fetchErr}</div>
  )
  if (!data) return <div className="loading">Loading…</div>
  if (!data.configured) return (
    <div className="unconfigured">
      <h2>Proxmox not configured</h2>
      <p>Set <code>PVE_HOST</code>, <code>PVE_TOKEN_ID</code>, and <code>PVE_TOKEN_SECRET</code> in the backend <code>.env</code> file.</p>
    </div>
  )
  if (data.error) return (
    <div className="banner error">Proxmox error: {data.error}</div>
  )

  const { host, guests } = data
  const cpuPct  = (host.cpu ?? 0) * 100
  const memPct  = host.mem_total  ? (host.mem_used  / host.mem_total)  * 100 : 0
  const diskPct = host.disk_total ? (host.disk_used / host.disk_total) * 100 : 0
  const grouped = groupByProject(guests)

  return (
    <div className="app">
      <header className="app-header">
        <h1>Ctrlable Provisioner</h1>
        <div className="header-right">
          <span className="chip">{host.node}</span>
          {host.uptime > 0 && <span className="muted">up {fmtUptime(host.uptime)}</span>}
        </div>
      </header>

      <section className="host-health">
        <HealthBar label="CPU" pct={cpuPct} />
        <HealthBar
          label="RAM"
          pct={memPct}
          sublabel={`${fmtBytes(host.mem_used)} / ${fmtBytes(host.mem_total)}`}
        />
        <HealthBar
          label="Disk"
          pct={diskPct}
          sublabel={`${fmtBytes(host.disk_used)} / ${fmtBytes(host.disk_total)}`}
        />
      </section>

      <section className="guests">
        {grouped.length === 0 && (
          <p className="empty">No guests found on {host.node}.</p>
        )}
        {grouped.map(([project, items]) => (
          <div key={project} className="project-group">
            <h2 className="project-name">{project}</h2>
            <div className="guest-grid">
              {items.map(g => <GuestCard key={g.vmid} guest={g} />)}
            </div>
          </div>
        ))}
      </section>
    </div>
  )
}
