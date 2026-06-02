import { useEffect, useState, useCallback } from 'react'
import './App.css'

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------
function fmtBytes(bytes) {
  if (!bytes) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0, v = bytes
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++ }
  return `${v.toFixed(1)} ${units[i]}`
}

function fmtUptime(s) {
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600)
  const m = Math.floor((s % 3600) / 60)
  if (d > 0) return `${d}d ${h}h`
  if (h > 0) return `${h}h ${m}m`
  return `${m}m`
}

// ---------------------------------------------------------------------------
// Shared components
// ---------------------------------------------------------------------------
function StatusDot({ status }) {
  const colors = { running: '#22c55e', stopped: '#6b7280', active: '#22c55e',
                   pending: '#f59e0b', provisioning: '#6366f1', error: '#ef4444' }
  return <span className="status-dot" style={{ background: colors[status] ?? '#64748b' }} />
}

function HealthBar({ label, pct, sublabel }) {
  const p = Math.min(Math.round(pct ?? 0), 100)
  const color = p > 90 ? '#ef4444' : p > 70 ? '#f59e0b' : '#22c55e'
  return (
    <div className="metric">
      <div className="metric-header"><span>{label}</span><span>{p}%</span></div>
      <div className="bar-bg"><div className="bar-fill" style={{ width: `${p}%`, background: color }} /></div>
      {sublabel && <div className="metric-sub">{sublabel}</div>}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Dashboard tab
// ---------------------------------------------------------------------------
function GuestCard({ guest, onAction }) {
  return (
    <div className="guest-card">
      <div className="guest-header">
        <StatusDot status={guest.status} />
        <span className="guest-hostname" title={guest.hostname}>{guest.hostname}</span>
        <span className="guest-kind">{guest.kind?.toUpperCase()}</span>
      </div>
      <div className="guest-meta">
        {guest.type   && <span className="tag">{guest.type}</span>}
        {guest.release && <span className="tag accent">{guest.release}</span>}
        <span className="tag">{guest.status}</span>
      </div>
      {guest.status === 'running' && (
        <div className="guest-stats">
          <span>CPU {(guest.cpu * 100).toFixed(1)}%</span>
          <span>{fmtBytes(guest.mem)} / {fmtBytes(guest.maxmem)}</span>
        </div>
      )}
      <div className="guest-actions">
        {guest.status === 'stopped' && <button className="btn-xs" onClick={() => onAction(guest.vmid, 'start')}>Start</button>}
        {guest.status === 'running' && <button className="btn-xs danger" onClick={() => onAction(guest.vmid, 'stop')}>Stop</button>}
        {guest.status === 'running' && <button className="btn-xs" onClick={() => onAction(guest.vmid, 'reboot')}>Reboot</button>}
      </div>
    </div>
  )
}

function groupBy(guests, key) {
  const map = {}
  for (const g of guests) { const k = g[key] ?? '(untracked)'; (map[k] ??= []).push(g) }
  return Object.entries(map).sort(([a], [b]) => a.localeCompare(b))
}

function DashboardTab({ data, onAction }) {
  if (!data) return <div className="loading">Loading…</div>
  if (!data.configured) return (
    <div className="unconfigured">
      <h2>Proxmox not configured</h2>
      <p>Set <code>PVE_HOST</code>, <code>PVE_TOKEN_ID</code>, and <code>PVE_TOKEN_SECRET</code> in <code>backend/.env</code>.</p>
    </div>
  )
  if (data.error) return <div className="banner error">Proxmox error: {data.error}</div>

  const { host, guests } = data
  const cpuPct  = (host.cpu ?? 0) * 100
  const memPct  = host.mem_total  ? (host.mem_used  / host.mem_total)  * 100 : 0
  const diskPct = host.disk_total ? (host.disk_used / host.disk_total) * 100 : 0

  return (
    <>
      <section className="host-health">
        <HealthBar label="CPU" pct={cpuPct} />
        <HealthBar label="RAM" pct={memPct}
          sublabel={`${fmtBytes(host.mem_used)} / ${fmtBytes(host.mem_total)}`} />
        <HealthBar label="Disk" pct={diskPct}
          sublabel={`${fmtBytes(host.disk_used)} / ${fmtBytes(host.disk_total)}`} />
      </section>
      <section className="guests">
        {groupBy(guests, 'project').length === 0 && <p className="empty">No guests on this node.</p>}
        {groupBy(guests, 'project').map(([project, items]) => (
          <div key={project} className="project-group">
            <h2 className="project-name">{project}</h2>
            <div className="guest-grid">
              {items.map(g => <GuestCard key={g.vmid} guest={g} onAction={onAction} />)}
            </div>
          </div>
        ))}
      </section>
    </>
  )
}

// ---------------------------------------------------------------------------
// Deploy tab
// ---------------------------------------------------------------------------
const EMPTY_FORM = {
  site_name: '', release: '',
  mqtt_host: '', mqtt_port: 1883, mqtt_user: '', mqtt_pass: '',
  zigbee_coordinator: '', zwave_coordinator: '',
}

function Field({ label, value, onChange, type = 'text', placeholder = '' }) {
  return (
    <label className="field">
      <span>{label}</span>
      <input type={type} value={value} placeholder={placeholder}
        onChange={e => onChange(e.target.value)} />
    </label>
  )
}

function InstanceRow({ inst }) {
  return (
    <div className="instance-row">
      <StatusDot status={inst.status} />
      <span className="inst-host">{inst.hostname}</span>
      <span className="tag">{inst.type}</span>
      <span className={`tag status-${inst.status}`}>{inst.status}</span>
    </div>
  )
}

function DeployTab({ releases }) {
  const [form, setForm] = useState({ ...EMPTY_FORM, release: releases[0]?.release ?? '' })
  const [deploying, setDeploying] = useState(false)
  const [error, setError] = useState(null)
  const [projects, setProjects] = useState([])

  const set = (key) => (val) => setForm(f => ({ ...f, [key]: val }))

  const loadProjects = useCallback(() => {
    fetch('/api/projects').then(r => r.json()).then(setProjects).catch(() => {})
  }, [])

  useEffect(() => {
    loadProjects()
    const id = setInterval(loadProjects, 5000)
    return () => clearInterval(id)
  }, [loadProjects])

  const deploy = async () => {
    if (!form.site_name.trim()) return
    setDeploying(true); setError(null)
    try {
      const res = await fetch('/api/projects', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...form, mqtt_port: Number(form.mqtt_port) }),
      })
      const data = await res.json()
      if (!res.ok) { setError(data.detail ?? 'Deploy failed'); return }
      setForm({ ...EMPTY_FORM, release: releases[0]?.release ?? '' })
      loadProjects()
    } catch (e) {
      setError(e.message)
    } finally {
      setDeploying(false)
    }
  }

  return (
    <div className="deploy-tab">
      <section className="deploy-form card">
        <h2>New Stack</h2>
        {error && <div className="banner error">{error}</div>}
        <div className="form-grid">
          <Field label="Site name" value={form.site_name} onChange={set('site_name')} placeholder="e.g. hampton-inn" />
          <label className="field">
            <span>Release</span>
            <select value={form.release} onChange={e => set('release')(e.target.value)}>
              {releases.map(r => <option key={r.release} value={r.release}>{r.release}</option>)}
            </select>
          </label>
          <Field label="MQTT host" value={form.mqtt_host} onChange={set('mqtt_host')} placeholder="192.168.1.10" />
          <Field label="MQTT port" value={form.mqtt_port} onChange={set('mqtt_port')} type="number" />
          <Field label="MQTT user" value={form.mqtt_user} onChange={set('mqtt_user')} />
          <Field label="MQTT password" value={form.mqtt_pass} onChange={set('mqtt_pass')} type="password" />
          <Field label="Zigbee coordinator" value={form.zigbee_coordinator} onChange={set('zigbee_coordinator')} placeholder="tcp://192.168.1.20:6638" />
          <Field label="Z-Wave coordinator" value={form.zwave_coordinator} onChange={set('zwave_coordinator')} placeholder="tcp://192.168.1.21:8888" />
        </div>
        <button className="btn-primary" onClick={deploy}
          disabled={deploying || !form.site_name.trim() || !form.release}>
          {deploying ? 'Deploying…' : 'Deploy Stack'}
        </button>
        <p className="form-note">Deploys all LXC templates for the selected release. HAOS (ctrlable-pro) is handled separately (M6).</p>
      </section>

      {projects.length > 0 && (
        <section className="projects-list">
          <h2 className="section-title">Deployed Stacks</h2>
          {projects.map(p => (
            <div key={p.id} className="card project-card">
              <div className="project-card-header">
                <strong>{p.site_name}</strong>
                <span className="tag accent">{p.release}</span>
              </div>
              {p.instances.length === 0
                ? <p className="empty small">Cloning in progress…</p>
                : p.instances.map(i => <InstanceRow key={i.id} inst={i} />)
              }
            </div>
          ))}
        </section>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Releases tab (stub — build trigger is M7)
// ---------------------------------------------------------------------------
function ReleasesTab({ releases }) {
  return (
    <div className="releases-tab">
      {releases.map(r => (
        <div key={r.release} className="card">
          <div className="release-row">
            <strong>{r.release}</strong>
            {r.active ? <span className="tag accent">active</span> : <span className="tag">inactive</span>}
            <span className="muted">{r.community_ref}</span>
          </div>
          <p className="form-note">Build pipeline trigger coming in M7.</p>
        </div>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Root app
// ---------------------------------------------------------------------------
export default function App() {
  const [tab, setTab] = useState('dashboard')
  const [dashboard, setDashboard] = useState(null)
  const [releases, setReleases] = useState([])
  const [fetchErr, setFetchErr] = useState(null)

  const loadDashboard = useCallback(() => {
    fetch('/api/dashboard')
      .then(r => r.json()).then(d => { setDashboard(d); setFetchErr(null) })
      .catch(e => setFetchErr(e.message))
  }, [])

  useEffect(() => {
    loadDashboard()
    fetch('/api/releases').then(r => r.json()).then(setReleases).catch(() => {})
    const id = setInterval(loadDashboard, 30_000)
    return () => clearInterval(id)
  }, [loadDashboard])

  const handleAction = useCallback(async (vmid, action) => {
    await fetch(`/api/guests/${vmid}/${action}`, { method: 'POST' })
    setTimeout(loadDashboard, 1500)
  }, [loadDashboard])

  const node = dashboard?.host?.node ?? '…'
  const uptime = dashboard?.host?.uptime

  if (fetchErr) return <div className="banner error">Cannot reach API: {fetchErr}</div>

  return (
    <div className="app">
      <header className="app-header">
        <h1>Ctrlable Provisioner</h1>
        <div className="header-right">
          <span className="chip">{node}</span>
          {uptime > 0 && <span className="muted">up {fmtUptime(uptime)}</span>}
        </div>
      </header>

      <nav className="tabs">
        {['dashboard', 'deploy', 'releases'].map(t => (
          <button key={t} className={`tab ${tab === t ? 'active' : ''}`}
            onClick={() => setTab(t)}>
            {t.charAt(0).toUpperCase() + t.slice(1)}
          </button>
        ))}
      </nav>

      <main>
        {tab === 'dashboard' && <DashboardTab data={dashboard} onAction={handleAction} />}
        {tab === 'deploy'    && <DeployTab releases={releases} />}
        {tab === 'releases'  && <ReleasesTab releases={releases} />}
      </main>
    </div>
  )
}
