import { useEffect, useState, useCallback, useRef } from 'react'
import './App.css'

// ---------------------------------------------------------------------------
// Auth helpers
// ---------------------------------------------------------------------------
const TOKEN_KEY = 'ctrlable_provisioner_token'
function getToken() { return localStorage.getItem(TOKEN_KEY) }
function setToken(t) { localStorage.setItem(TOKEN_KEY, t) }
function clearToken() { localStorage.removeItem(TOKEN_KEY) }

async function apiFetch(path, opts = {}) {
  const headers = { ...opts.headers }
  const token = getToken()
  if (token) headers['Authorization'] = `Bearer ${token}`
  if (opts.body && typeof opts.body === 'string') headers['Content-Type'] = 'application/json'
  const res = await fetch(path, { ...opts, headers })
  if (res.status === 401) { clearToken(); window.location.reload() }
  return res
}

// ---------------------------------------------------------------------------
// Login screen
// ---------------------------------------------------------------------------
function LoginScreen({ onLogin }) {
  const [username, setUsername] = useState('admin')
  const [password, setPassword] = useState('')
  const [error, setError]       = useState(null)
  const [loading, setLoading]   = useState(false)

  const submit = async (e) => {
    e.preventDefault(); setLoading(true); setError(null)
    try {
      const res = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      })
      const data = await res.json()
      if (!res.ok) { setError(data.detail ?? 'Login failed'); return }
      setToken(data.token)
      onLogin({ mustChange: data.must_change_password })
    } catch { setError('Cannot reach server') }
    finally { setLoading(false) }
  }

  return (
    <div className="login-wrap">
      <form className="login-card card" onSubmit={submit}>
        <h1 className="login-title">Ctrlable Provisioner</h1>
        {error && <div className="banner error">{error}</div>}
        <label className="field"><span>Username</span>
          <input value={username} onChange={e => setUsername(e.target.value)} autoComplete="username" />
        </label>
        <label className="field"><span>Password</span>
          <input type="password" value={password} onChange={e => setPassword(e.target.value)}
            autoComplete="current-password" autoFocus />
        </label>
        <button className="btn-primary" disabled={loading || !password}>
          {loading ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </div>
  )
}

// ---------------------------------------------------------------------------
// First-time setup screen (shown when must_change_password is true)
// ---------------------------------------------------------------------------
function SetupPasswordScreen({ onDone }) {
  const [pw, setPw]           = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError]     = useState(null)
  const [loading, setLoading] = useState(false)

  const submit = async (e) => {
    e.preventDefault()
    if (pw !== confirm) { setError('Passwords do not match'); return }
    if (pw.length < 8)  { setError('Minimum 8 characters'); return }
    setLoading(true); setError(null)
    try {
      const res = await fetch('/api/auth/setup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ new_password: pw }),
      })
      const data = await res.json()
      if (!res.ok) { setError(data.detail ?? 'Failed'); return }
      setToken(data.token)
      onDone()
    } catch { setError('Cannot reach server') }
    finally { setLoading(false) }
  }

  return (
    <div className="login-wrap">
      <form className="login-card card" onSubmit={submit}>
        <h1 className="login-title">Ctrlable Provisioner</h1>
        <div className="banner" style={{background:'var(--accent-subtle,#1a2a3a)',marginBottom:'.5rem'}}>
          Welcome! Set an admin password to get started.
        </div>
        {error && <div className="banner error">{error}</div>}
        <label className="field"><span>New password</span>
          <input type="password" value={pw} onChange={e => setPw(e.target.value)}
            autoComplete="new-password" autoFocus placeholder="Min. 8 characters" />
        </label>
        <label className="field"><span>Confirm password</span>
          <input type="password" value={confirm} onChange={e => setConfirm(e.target.value)}
            autoComplete="new-password" placeholder="Repeat password" />
        </label>
        <button className="btn-primary" disabled={loading || !pw || !confirm}>
          {loading ? 'Saving…' : 'Set password & continue'}
        </button>
      </form>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Change password modal
// ---------------------------------------------------------------------------
function ChangePasswordModal({ onClose }) {
  const [current, setCurrent]   = useState('')
  const [next, setNext]         = useState('')
  const [confirm, setConfirm]   = useState('')
  const [error, setError]       = useState(null)
  const [ok, setOk]             = useState(false)
  const [loading, setLoading]   = useState(false)

  const submit = async (e) => {
    e.preventDefault()
    if (next !== confirm) { setError('Passwords do not match'); return }
    if (next.length < 8)  { setError('Minimum 8 characters'); return }
    setLoading(true); setError(null)
    try {
      const res = await apiFetch('/api/auth/change-password', {
        method: 'POST',
        body: JSON.stringify({ current_password: current, new_password: next }),
      })
      const data = await res.json()
      if (!res.ok) { setError(data.detail ?? 'Failed'); return }
      setOk(true)
    } catch { setError('Request failed') }
    finally { setLoading(false) }
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-card card" onClick={e => e.stopPropagation()}>
        <h2>Change Password</h2>
        {ok
          ? <div className="banner success">Password updated. <button className="btn-xs" onClick={onClose}>Close</button></div>
          : <form onSubmit={submit} style={{display:'flex',flexDirection:'column',gap:'1rem'}}>
              {error && <div className="banner error">{error}</div>}
              <label className="field"><span>Current password</span>
                <input type="password" value={current} onChange={e => setCurrent(e.target.value)} autoFocus />
              </label>
              <label className="field"><span>New password</span>
                <input type="password" value={next} onChange={e => setNext(e.target.value)} />
              </label>
              <label className="field"><span>Confirm new password</span>
                <input type="password" value={confirm} onChange={e => setConfirm(e.target.value)} />
              </label>
              <div style={{display:'flex',gap:'.5rem'}}>
                <button className="btn-primary" disabled={loading || !current || !next || !confirm}>
                  {loading ? 'Saving…' : 'Update'}
                </button>
                <button type="button" className="btn-xs" onClick={onClose}>Cancel</button>
              </div>
            </form>
        }
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Guest config drawer
// ---------------------------------------------------------------------------
function GuestConfigDrawer({ guest, onClose, onRefresh }) {
  const [cfg, setCfg]           = useState(null)
  const [usbDevs, setUsbDevs]   = useState([])
  const [pciDevs, setPciDevs]   = useState([])
  const [error, setError]       = useState(null)
  const [saving, setSaving]     = useState(false)
  const [resizeVal, setResizeVal] = useState('')
  const [resizeDisk, setResizeDisk] = useState('')
  const kind = useRef(null)

  useEffect(() => {
    apiFetch(`/api/guests/${guest.vmid}/config`)
      .then(r => r.json()).then(d => { kind.current = d.kind; setCfg(d.config) }).catch(e => setError(e.message))
    apiFetch('/api/host/devices/usb').then(r => r.json()).then(setUsbDevs).catch(() => {})
    if (guest.kind === 'qemu')
      apiFetch('/api/host/devices/pci').then(r => r.json()).then(setPciDevs).catch(() => {})
  }, [guest.vmid, guest.kind])

  const patch = async (body) => {
    setSaving(true); setError(null)
    try {
      const res = await apiFetch(`/api/guests/${guest.vmid}/config`, {
        method: 'PUT', body: JSON.stringify(body),
      })
      if (!res.ok) { const d = await res.json(); setError(d.detail ?? 'Failed'); return }
      const r = await apiFetch(`/api/guests/${guest.vmid}/config`)
      const d = await r.json(); setCfg(d.config); onRefresh()
    } catch (e) { setError(e.message) }
    finally { setSaving(false) }
  }

  const resize = async () => {
    if (!resizeDisk || !resizeVal) return
    setSaving(true); setError(null)
    try {
      const res = await apiFetch(`/api/guests/${guest.vmid}/resize`, {
        method: 'POST', body: JSON.stringify({ disk: resizeDisk, size: resizeVal }),
      })
      if (!res.ok) { const d = await res.json(); setError(d.detail ?? 'Failed'); return }
      const r = await apiFetch(`/api/guests/${guest.vmid}/config`)
      const d = await r.json(); setCfg(d.config); setResizeVal(''); setResizeDisk('')
    } catch (e) { setError(e.message) }
    finally { setSaving(false) }
  }

  const usbSlots  = cfg ? Object.entries(cfg).filter(([k]) => /^usb\d+$/.test(k)) : []
  const pciSlots  = cfg ? Object.entries(cfg).filter(([k]) => /^hostpci\d+$/.test(k)) : []
  const diskSlots = cfg ? Object.entries(cfg).filter(([k]) =>
    /^(scsi|virtio|ide|sata)\d+$/.test(k) || k === 'rootfs'
  ) : []

  return (
    <div className="drawer-overlay" onClick={onClose}>
      <div className="drawer card" onClick={e => e.stopPropagation()}>
        <div className="drawer-header">
          <h2>{guest.hostname} <span className="tag">{guest.kind?.toUpperCase()}</span></h2>
          <button className="btn-xs" onClick={onClose}>✕</button>
        </div>
        {error && <div className="banner error">{error}</div>}
        {!cfg ? <div className="loading">Loading config…</div> : <>

          <section className="cfg-section">
            <h3>General</h3>
            <label className="toggle-row">
              <span>Start on boot</span>
              <input type="checkbox" checked={!!cfg.onboot} onChange={e => patch({ onboot: e.target.checked })} disabled={saving} />
            </label>
          </section>

          <section className="cfg-section">
            <h3>Disks</h3>
            <table className="cfg-table">
              <tbody>
                {diskSlots.map(([k, v]) => (
                  <tr key={k}><td className="muted">{k}</td><td>{v.split(',').find(p => p.startsWith('size='))?.slice(5) ?? '—'}</td></tr>
                ))}
              </tbody>
            </table>
            <div className="inline-form">
              <select value={resizeDisk} onChange={e => setResizeDisk(e.target.value)} className="select-sm">
                <option value="">Disk…</option>
                {diskSlots.map(([k]) => <option key={k} value={k}>{k}</option>)}
              </select>
              <input className="input-sm" placeholder="32G or +10G" value={resizeVal}
                onChange={e => setResizeVal(e.target.value)} />
              <button className="btn-xs" onClick={resize} disabled={saving || !resizeDisk || !resizeVal}>
                Resize
              </button>
            </div>
          </section>

          <section className="cfg-section">
            <h3>USB Passthrough</h3>
            {usbSlots.length === 0
              ? <p className="empty small">None configured</p>
              : usbSlots.map(([slot, val]) => (
                <div key={slot} className="device-row">
                  <span className="tag">{slot}</span>
                  <span className="muted">{val}</span>
                  <button className="btn-xs danger" disabled={saving}
                    onClick={() => patch({ usb_del: slot })}>Remove</button>
                </div>
              ))
            }
            {usbDevs.length > 0 && (
              <details className="add-device">
                <summary>Add USB device</summary>
                <div className="device-list">
                  {usbDevs.map((d, i) => {
                    const id = `${d.vendid}:${d.prodid}`
                    const label = d.manufacturer ? `${d.manufacturer} — ${d.product ?? id}` : id
                    return (
                      <div key={i} className="device-row">
                        <span className="muted">{label}</span>
                        <span className="tag">{id}</span>
                        <button className="btn-xs" disabled={saving}
                          onClick={() => patch({ usb_add: `host=${id}` })}>Add</button>
                      </div>
                    )
                  })}
                </div>
              </details>
            )}
          </section>

          {guest.kind === 'qemu' && (
            <section className="cfg-section">
              <h3>PCI / GPU Passthrough</h3>
              {pciSlots.length === 0
                ? <p className="empty small">None configured</p>
                : pciSlots.map(([slot, val]) => (
                  <div key={slot} className="device-row">
                    <span className="tag">{slot}</span>
                    <span className="muted">{val}</span>
                    <button className="btn-xs danger" disabled={saving}
                      onClick={() => patch({ pci_del: slot })}>Remove</button>
                  </div>
                ))
              }
              {pciDevs.length > 0 && (
                <details className="add-device">
                  <summary>Add PCI device</summary>
                  <div className="device-list">
                    {pciDevs.map((d, i) => {
                      const isVga = (d.class ?? '').toLowerCase().includes('vga') || (d.class ?? '').toLowerCase().includes('display')
                      return (
                        <div key={i} className="device-row">
                          <span className="muted">{d.device_name ?? d.pciid}</span>
                          {isVga && <span className="tag accent">VGA</span>}
                          <span className="tag">{d.pciid}</span>
                          <button className="btn-xs" disabled={saving}
                            onClick={() => patch({ pci_add: `${d.pciid},pcie=1${isVga ? ',x-vga=1' : ''}` })}>
                            Add
                          </button>
                        </div>
                      )
                    })}
                  </div>
                </details>
              )}
            </section>
          )}
        </>}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Add Instance modal
// ---------------------------------------------------------------------------
function AddInstanceModal({ project, onClose, onDone }) {
  const [templates, setTemplates] = useState([])
  const [selected, setSelected]  = useState('')
  const [error, setError]        = useState(null)
  const [loading, setLoading]    = useState(false)

  useEffect(() => {
    apiFetch(`/api/releases/${project.release}/built-templates`)
      .then(r => r.json()).then(rows => {
        setTemplates(rows)
        if (rows.length) setSelected(rows[0].name)
      }).catch(() => {})
  }, [project.release])

  const submit = async () => {
    setLoading(true); setError(null)
    try {
      const res = await apiFetch(`/api/projects/${project.id}/instances`, {
        method: 'POST',
        body: JSON.stringify({ template_name: selected, wire_to: {} }),
      })
      const data = await res.json()
      if (!res.ok) { setError(data.detail ?? 'Failed'); return }
      onDone()
    } catch (e) { setError(e.message) }
    finally { setLoading(false) }
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-card card" onClick={e => e.stopPropagation()}>
        <h2>Add Instance — {project.site_name}</h2>
        {error && <div className="banner error">{error}</div>}
        {templates.length === 0
          ? <p className="empty">No built templates for release {project.release}.</p>
          : <>
            <label className="field"><span>Template</span>
              <select value={selected} onChange={e => setSelected(e.target.value)}>
                {templates.map(t => (
                  <option key={t.name} value={t.name}>{t.name} ({t.kind})</option>
                ))}
              </select>
            </label>
            <div style={{display:'flex',gap:'.5rem',marginTop:'1rem'}}>
              <button className="btn-primary" onClick={submit} disabled={loading || !selected}>
                {loading ? 'Deploying…' : 'Deploy Instance'}
              </button>
              <button className="btn-xs" onClick={onClose}>Cancel</button>
            </div>
          </>
        }
      </div>
    </div>
  )
}

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
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60)
  if (d > 0) return `${d}d ${h}h`; if (h > 0) return `${h}h ${m}m`; return `${m}m`
}

// ---------------------------------------------------------------------------
// Dashboard tab
// ---------------------------------------------------------------------------
function StatusDot({ status }) {
  const colors = { running:'#22c55e', stopped:'#6b7280', active:'#22c55e', pending:'#f59e0b', provisioning:'#6366f1', error:'#ef4444' }
  return <span className="status-dot" style={{ background: colors[status] ?? '#64748b' }} />
}
function HealthBar({ label, pct, sublabel }) {
  const p = Math.min(Math.round(pct ?? 0), 100)
  const color = p > 90 ? '#ef4444' : p > 70 ? '#f59e0b' : '#22c55e'
  return (
    <div className="metric">
      <div className="metric-header"><span>{label}</span><span>{p}%</span></div>
      <div className="bar-bg"><div className="bar-fill" style={{ width:`${p}%`, background:color }} /></div>
      {sublabel && <div className="metric-sub">{sublabel}</div>}
    </div>
  )
}

function GuestCard({ guest, onAction, onConfigure }) {
  return (
    <div className="guest-card">
      <div className="guest-header">
        <StatusDot status={guest.status} />
        <span className="guest-hostname" title={guest.hostname}>{guest.hostname}</span>
        <span className="guest-kind">{guest.kind?.toUpperCase()}</span>
        <button className="btn-icon" title="Configure" onClick={() => onConfigure(guest)}>⚙</button>
      </div>
      <div className="guest-meta">
        {guest.type    && <span className="tag">{guest.type}</span>}
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
        {guest.status === 'stopped'  && <button className="btn-xs" onClick={() => onAction(guest.vmid,'start')}>Start</button>}
        {guest.status === 'running'  && <button className="btn-xs danger" onClick={() => onAction(guest.vmid,'stop')}>Stop</button>}
        {guest.status === 'running'  && <button className="btn-xs" onClick={() => onAction(guest.vmid,'reboot')}>Reboot</button>}
      </div>
    </div>
  )
}

function groupBy(guests, key) {
  const map = {}
  for (const g of guests) { const k = g[key] ?? '(untracked)'; (map[k] ??= []).push(g) }
  return Object.entries(map).sort(([a],[b]) => a.localeCompare(b))
}

function DashboardTab({ data, onAction, onConfigure }) {
  if (!data) return <div className="loading">Loading…</div>
  if (!data.configured) return (
    <div className="unconfigured"><h2>Proxmox not configured</h2>
      <p>Set <code>PVE_HOST</code>, <code>PVE_TOKEN_ID</code>, and <code>PVE_TOKEN_SECRET</code> in <code>backend/.env</code>.</p>
    </div>
  )
  if (data.error) return <div className="banner error">Proxmox error: {data.error}</div>
  const { host, guests } = data
  return (
    <>
      <section className="host-health">
        <HealthBar label="CPU" pct={(host.cpu ?? 0)*100} />
        <HealthBar label="RAM" pct={host.mem_total ? (host.mem_used/host.mem_total)*100 : 0}
          sublabel={`${fmtBytes(host.mem_used)} / ${fmtBytes(host.mem_total)}`} />
        <HealthBar label="Disk" pct={host.disk_total ? (host.disk_used/host.disk_total)*100 : 0}
          sublabel={`${fmtBytes(host.disk_used)} / ${fmtBytes(host.disk_total)}`} />
      </section>
      <section className="guests">
        {groupBy(guests,'project').length === 0 && <p className="empty">No guests on this node.</p>}
        {groupBy(guests,'project').map(([project, items]) => (
          <div key={project} className="project-group">
            <h2 className="project-name">{project}</h2>
            <div className="guest-grid">
              {items.map(g => <GuestCard key={g.vmid} guest={g} onAction={onAction} onConfigure={onConfigure} />)}
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
const EMPTY_FORM = { site_name:'', release:'', mqtt_host:'', mqtt_port:1883, mqtt_user:'', mqtt_pass:'', zigbee_coordinator:'', zwave_coordinator:'' }

function Field({ label, value, onChange, type='text', placeholder='' }) {
  return (
    <label className="field"><span>{label}</span>
      <input type={type} value={value} placeholder={placeholder} onChange={e => onChange(e.target.value)} />
    </label>
  )
}

function InstanceRow({ inst, onDelete }) {
  const [showLog, setShowLog] = useState(false)
  const busy  = inst.status === 'provisioning'
  const failed = inst.status === 'error'
  const total = inst.phase_total || 0
  const idx   = inst.phase_index || 0
  // Phases come from the host tool's [PHASE] n/m markers, so this tracks real
  // steps. Deliberately no time-based estimate: "running community script" can
  // take a minute or twenty, and a bar that invents progress is worse than one
  // that sits still and says what it is doing.
  const pct = total ? Math.round((idx / total) * 100) : 0

  return (
    <div className={`instance-row${busy ? ' instance-row-busy' : ''}`}>
      <div className="instance-row-main">
        <StatusDot status={inst.status} />
        <span className="inst-host">{inst.hostname}</span>
        <span className="tag">{inst.type}</span>
        <span className={`tag status-${inst.status}`}>{inst.status}</span>
        {inst.vmid > 0 && <span className="tag inst-vmid">vmid {inst.vmid}</span>}
        {(busy || failed) && inst.deploy_log && (
          <button className="btn-xs inst-log-toggle" onClick={() => setShowLog(v => !v)}>
            {showLog ? 'hide log' : 'log'}
          </button>
        )}
        <button className="btn-xs danger inst-del" title="Remove instance" onClick={() => onDelete(inst)}>✕</button>
      </div>

      {busy && (
        <div className="inst-progress">
          <div className="inst-progress-track">
            <div className="inst-progress-fill" style={{ width: `${pct}%` }} />
          </div>
          <span className="inst-progress-label">
            {total ? `${idx}/${total}` : '…'} {inst.phase_label || 'starting deploy'}
          </span>
        </div>
      )}

      {failed && <div className="inst-error">deploy failed — see log</div>}

      {showLog && inst.deploy_log && (
        <pre className="inst-log">{inst.deploy_log.split('\n').slice(-14).join('\n')}</pre>
      )}
    </div>
  )
}

function DeployTab({ releases }) {
  const [form, setForm]           = useState({ ...EMPTY_FORM, release: releases[0]?.release ?? '' })
  const [deploying, setDeploying] = useState(false)
  const [error, setError]         = useState(null)
  const [projects, setProjects]   = useState([])
  const [addInstProject, setAddInstProject] = useState(null)
  const [builtTemplates, setBuiltTemplates] = useState([])
  const set = key => val => setForm(f => ({ ...f, [key]: val }))

  const loadProjects = useCallback(() => {
    apiFetch('/api/projects').then(r => r.json()).then(setProjects).catch(() => {})
  }, [])

  useEffect(() => {
    loadProjects()
    const id = setInterval(loadProjects, 5000)
    return () => clearInterval(id)
  }, [loadProjects])

  useEffect(() => {
    if (!form.release) return
    apiFetch(`/api/releases/${form.release}/built-templates`)
      .then(r => r.json()).then(setBuiltTemplates).catch(() => setBuiltTemplates([]))
  }, [form.release])

  const deploy = async () => {
    if (!form.site_name.trim()) return
    setDeploying(true); setError(null)
    try {
      const res = await apiFetch('/api/projects', { method:'POST', body: JSON.stringify({ ...form, mqtt_port: Number(form.mqtt_port) }) })
      const data = await res.json()
      if (!res.ok) { setError(data.detail ?? 'Deploy failed'); return }
      setForm({ ...EMPTY_FORM, release: releases[0]?.release ?? '' }); loadProjects()
    } catch (e) { setError(e.message) } finally { setDeploying(false) }
  }

  const deleteInstance = async (projectId, inst) => {
    if (!confirm(`Remove ${inst.hostname} from the orchestrator?\n\nClick OK to remove DB record only.\nThe Proxmox guest will NOT be destroyed automatically — delete it manually in Proxmox if needed.`)) return
    await apiFetch(`/api/projects/${projectId}/instances/${inst.id}`, { method: 'DELETE' })
    loadProjects()
  }

  const deleteProject = async (p) => {
    const msg = p.instances.length > 0
      ? `Delete project "${p.site_name}" and remove all ${p.instances.length} instance record(s) from the orchestrator?\n\nProxmox guests will NOT be destroyed — delete them manually.`
      : `Delete empty project "${p.site_name}"?`
    if (!confirm(msg)) return
    await apiFetch(`/api/projects/${p.id}`, { method: 'DELETE' })
    loadProjects()
  }

  const releaseReady = true  // deploy no longer requires pre-built templates

  return (
    <div className="deploy-tab">
      <section className="deploy-form card">
        <h2>New Stack</h2>
        {error && <div className="banner error">{error}</div>}
        <div className="form-grid">
          <Field label="Site name" value={form.site_name} onChange={set('site_name')} placeholder="e.g. hampton-inn" />
          <label className="field"><span>Release</span>
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
        <button className="btn-primary" onClick={deploy} disabled={deploying || !form.site_name.trim() || !form.release}>
          {deploying ? 'Deploying…' : 'Deploy Stack'}
        </button>
      </section>

      {projects.length > 0 && (
        <section className="projects-list">
          <h2 className="section-title">Deployed Stacks</h2>
          {projects.map(p => (
            <div key={p.id} className="card project-card">
              <div className="project-card-header">
                <strong>{p.site_name}</strong>
                <span className="tag accent">{p.release}</span>
                <button className="btn-xs" onClick={() => setAddInstProject(p)}>+ Add Instance</button>
                <button className="btn-xs danger" onClick={() => deleteProject(p)}>Delete Project</button>
              </div>
              {p.instances.length === 0
                ? <p className="empty small muted">No instances yet — deploy is running or failed.</p>
                : p.instances.map(i => <InstanceRow key={i.id} inst={i} onDelete={inst => deleteInstance(p.id, inst)} />)}
            </div>
          ))}
        </section>
      )}

      {addInstProject && (
        <AddInstanceModal
          project={addInstProject}
          onClose={() => setAddInstProject(null)}
          onDone={() => { setAddInstProject(null); loadProjects() }}
        />
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Releases tab
// ---------------------------------------------------------------------------
function BuildLogViewer({ buildId, onDone }) {
  const [build, setBuild] = useState(null)
  const logRef = useRef(null)

  useEffect(() => {
    const poll = async () => {
      const res = await apiFetch(`/api/builds/${buildId}`)
      const data = await res.json()
      setBuild(data)
      if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight
      if (data.status === 'running') setTimeout(poll, 3000)
      else onDone?.()
    }
    poll()
  }, [buildId])

  if (!build) return <div className="loading">Loading build…</div>

  const statusColor = build.status === 'success' ? '#22c55e' : build.status === 'failed' ? '#ef4444' : '#f59e0b'

  return (
    <div className="build-log-wrap">
      <div className="build-log-header">
        <span>Build #{build.id} — {build.release}</span>
        <span className="tag" style={{background: statusColor, color:'#fff'}}>{build.status}</span>
      </div>
      <pre ref={logRef} className="build-log">{build.log || '(no output yet)'}</pre>
    </div>
  )
}

function ReleaseCard({ release, onBuildStarted }) {
  const [templates, setTemplates] = useState([])
  const [building, setBuilding]   = useState(false)
  const [error, setError]         = useState(null)
  const [activeBuildId, setActiveBuildId] = useState(null)

  useEffect(() => {
    apiFetch(`/api/releases/${release.release}/built-templates`)
      .then(r => r.json()).then(setTemplates).catch(() => {})
  }, [release.release, activeBuildId])

  const triggerBuild = async () => {
    setBuilding(true); setError(null)
    try {
      const res = await apiFetch('/api/builds', {
        method: 'POST',
        body: JSON.stringify({ release: release.release }),
      })
      const data = await res.json()
      if (!res.ok) { setError(data.detail ?? 'Build failed to start'); return }
      setActiveBuildId(data.build_id)
      onBuildStarted?.()
    } catch (e) { setError(e.message) }
    finally { setBuilding(false) }
  }

  return (
    <div className="card release-card">
      <div className="release-row">
        <strong>{release.release}</strong>
        {release.active ? <span className="tag accent">active</span> : <span className="tag">inactive</span>}
        <span className="muted">{release.community_ref}</span>
        <button className="btn-xs" onClick={triggerBuild} disabled={building || !!activeBuildId}>
          {building ? 'Starting…' : 'Build'}
        </button>
      </div>

      {error && <div className="banner error" style={{marginTop:'.5rem'}}>{error}</div>}

      {activeBuildId && (
        <BuildLogViewer buildId={activeBuildId} onDone={() => {
          // Reload templates after build completes
          apiFetch(`/api/releases/${release.release}/built-templates`)
            .then(r => r.json()).then(setTemplates).catch(() => {})
        }} />
      )}

      {templates.length > 0 && (
        <table className="cfg-table" style={{marginTop:'.75rem'}}>
          <thead><tr><th>Template</th><th>Kind</th><th>VMID</th><th>Built</th></tr></thead>
          <tbody>
            {templates.map(t => (
              <tr key={t.name}>
                <td>{t.name}</td>
                <td><span className="tag">{t.kind}</span></td>
                <td className="muted">{t.template_vmid}</td>
                <td className="muted">{t.built_at ? new Date(t.built_at).toLocaleDateString() : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

function ReleasesTab({ releases }) {
  const [builds, setBuilds] = useState([])

  const loadBuilds = useCallback(() => {
    apiFetch('/api/builds').then(r => r.json()).then(setBuilds).catch(() => {})
  }, [])

  useEffect(() => { loadBuilds() }, [loadBuilds])

  return (
    <div className="releases-tab">
      {releases.map(r => (
        <ReleaseCard key={r.release} release={r} onBuildStarted={loadBuilds} />
      ))}

      {builds.length > 0 && (
        <div className="card" style={{marginTop:'1.5rem'}}>
          <h3 style={{marginBottom:'.75rem'}}>Build History</h3>
          <table className="cfg-table">
            <thead><tr><th>#</th><th>Release</th><th>Status</th><th>Started</th></tr></thead>
            <tbody>
              {builds.map(b => (
                <tr key={b.id}>
                  <td className="muted">{b.id}</td>
                  <td>{b.release}</td>
                  <td>
                    <span className="tag" style={{
                      background: b.status === 'success' ? '#22c55e' : b.status === 'failed' ? '#ef4444' : '#f59e0b',
                      color: '#fff'
                    }}>{b.status}</span>
                  </td>
                  <td className="muted">{new Date(b.started_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Platform tab (M8)
// ---------------------------------------------------------------------------
function PlatformTab() {
  const [status, setStatus]   = useState(null)
  const [lan, setLan]         = useState(null)
  const [token, setToken]     = useState('')
  const [enrolling, setEnrolling] = useState(false)
  const [error, setError]     = useState(null)

  const loadStatus = useCallback(() => {
    apiFetch('/api/platform/status').then(r => r.json()).then(setStatus).catch(() => {})
    apiFetch('/api/lan/status').then(r => r.json()).then(setLan).catch(() => {})
  }, [])

  useEffect(() => { loadStatus() }, [loadStatus])

  const enroll = async () => {
    if (!token.trim()) return
    setEnrolling(true); setError(null)
    try {
      const res = await apiFetch('/api/platform/enroll', {
        method: 'POST',
        body: JSON.stringify({ token: token.trim() }),
      })
      const data = await res.json()
      if (!res.ok) { setError(data.detail ?? 'Enrollment failed'); return }
      setToken(''); loadStatus()
    } catch (e) { setError(e.message) }
    finally { setEnrolling(false) }
  }

  if (!status) return <div className="loading">Loading…</div>

  return (
    <div className="platform-tab">
      {status.enrolled ? (
        <>
          <div className="card">
            <div className="platform-enrolled">
              <span className="status-dot" style={{background:'#22c55e', display:'inline-block', marginRight:'.5rem'}} />
              <strong>Enrolled in Ctrlable Portal</strong>
            </div>
            <div className="platform-details">
              <div className="platform-row"><span className="muted">Device ID</span><code>{status.device_id}</code></div>
              <div className="platform-row"><span className="muted">Tunnel IP</span><code>{status.tunnel_ip}</code></div>
              <div className="platform-row"><span className="muted">WG Interface</span><code>{status.wg_iface}</code></div>
              <div className="platform-row"><span className="muted">Enrolled</span><span>{new Date(status.enrolled_at).toLocaleString()}</span></div>
            </div>
            <a className="btn-xs" href={status.portal_url} target="_blank" rel="noreferrer" style={{marginTop:'1rem',display:'inline-block'}}>
              View in Portal →
            </a>
          </div>

          {/* LAN access */}
          <div className="card" style={{marginTop:'1rem'}}>
            <div style={{display:'flex',alignItems:'center',gap:'.6rem',marginBottom:'1rem'}}>
              <span className="status-dot" style={{
                background: lan?.configured ? '#22c55e' : '#f59e0b', display:'inline-block'
              }} />
              <strong>LAN Access</strong>
            </div>
            {lan?.configured ? (
              <div className="platform-details">
                <div className="platform-row"><span className="muted">Interface</span><code>{lan.lan_iface}</code></div>
                <div className="platform-row"><span className="muted">LAN Subnet</span><code>{lan.lan_subnet}</code></div>
                <div className="platform-row">
                  <span className="muted">Proxy Subnet</span>
                  {lan.proxy_subnet
                    ? <code>{lan.proxy_subnet}</code>
                    : <span className="muted">Pending portal allocation…</span>}
                </div>
                <div className="platform-row"><span className="muted">NAT</span>
                  <span className="tag" style={{background:'#22c55e',color:'#fff'}}>active</span>
                </div>
              </div>
            ) : (
              <p className="form-note">LAN not yet detected — will configure automatically.</p>
            )}
          </div>
        </>
      ) : (
        <div className="card">
          {status.pending_token ? (
            <div className="platform-pending">
              <div style={{display:'flex',alignItems:'center',gap:'.6rem',marginBottom:'1rem'}}>
                <span className="status-dot" style={{background:'#f59e0b',display:'inline-block'}} />
                <strong>Waiting for internet — auto-enrollment pending</strong>
              </div>
              <p className="form-note">
                An enrollment token was provided during installation. The orchestrator will
                connect to portal.ctrlable.com automatically once internet access is detected.
              </p>
            </div>
          ) : (
            <>
              <h2>Enroll in Ctrlable Portal</h2>
              <p className="form-note" style={{marginBottom:'1rem'}}>
                Enrolling connects this orchestrator to portal.ctrlable.com via WireGuard,
                enabling remote management and monitoring.
                Generate an enrollment token in the portal under <strong>Devices → Add Device</strong>.
              </p>
            </>
          )}
          {error && <div className="banner error">{error}</div>}
          <label className="field" style={{marginTop: status.pending_token ? '1rem' : 0}}>
            <span>{status.pending_token ? 'Re-enroll with a new token' : 'Enrollment Token'}</span>
            <input
              value={token}
              onChange={e => setToken(e.target.value)}
              placeholder="Paste token from portal.ctrlable.com"
            />
          </label>
          <button className="btn-primary" style={{marginTop:'.75rem'}}
            onClick={enroll} disabled={enrolling || !token.trim()}>
            {enrolling ? 'Enrolling…' : status.pending_token ? 'Re-enroll' : 'Enroll'}
          </button>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Root app
// ---------------------------------------------------------------------------
export default function App() {
  const [authReady, setAuthReady]           = useState(false)
  const [mustChangePassword, setMustChange] = useState(false)
  const [loggedIn, setLoggedIn]             = useState(!!getToken())
  const [tab, setTab]                       = useState('dashboard')
  const [dashboard, setDashboard]           = useState(null)
  const [releases, setReleases]             = useState([])
  const [fetchErr, setFetchErr]             = useState(null)
  const [showChangePwd, setShowChangePwd]   = useState(false)
  const [configGuest, setConfigGuest]       = useState(null)

  useEffect(() => {
    fetch('/api/auth/status').then(r => r.json())
      .then(d => {
        setMustChange(d.must_change_password)
        setAuthReady(true)
      })
      .catch(() => setAuthReady(true))
  }, [])

  const loadDashboard = useCallback(() => {
    apiFetch('/api/dashboard')
      .then(r => r.json()).then(d => { setDashboard(d); setFetchErr(null) })
      .catch(e => setFetchErr(e.message))
  }, [])

  useEffect(() => {
    if (!authReady || !loggedIn) return
    loadDashboard()
    apiFetch('/api/releases').then(r => r.json()).then(setReleases).catch(() => {})
    const id = setInterval(loadDashboard, 30_000)
    return () => clearInterval(id)
  }, [authReady, loggedIn, loadDashboard])

  const handleAction = useCallback(async (vmid, action) => {
    await apiFetch(`/api/guests/${vmid}/${action}`, { method: 'POST' })
    setTimeout(loadDashboard, 1500)
  }, [loadDashboard])

  const logout = () => { clearToken(); setLoggedIn(false); setDashboard(null) }

  if (!authReady) return null
  // First-time setup: no login needed, just set the password
  if (mustChangePassword) return <SetupPasswordScreen onDone={() => { setMustChange(false); setLoggedIn(true) }} />
  if (!loggedIn) return <LoginScreen onLogin={({ mustChange }) => {
    if (mustChange) { setMustChange(true) } else { setLoggedIn(true) }
  }} />
  if (fetchErr) return <div className="banner error">Cannot reach API: {fetchErr}</div>

  const node   = dashboard?.host?.node ?? '…'
  const uptime = dashboard?.host?.uptime

  return (
    <div className="app">
      <header className="app-header">
        <h1>Ctrlable Provisioner</h1>
        <div className="header-right">
          <span className="chip">{node}</span>
          {uptime > 0 && <span className="muted">up {fmtUptime(uptime)}</span>}
          <button className="btn-xs" onClick={() => setShowChangePwd(true)}>Password</button>
          <button className="btn-xs" onClick={logout}>Sign out</button>
        </div>
      </header>

      <nav className="tabs">
        {['dashboard','deploy','releases','platform'].map(t => (
          <button key={t} className={`tab ${tab===t?'active':''}`} onClick={() => setTab(t)}>
            {t.charAt(0).toUpperCase()+t.slice(1)}
          </button>
        ))}
      </nav>

      <main>
        {tab==='dashboard' && <DashboardTab data={dashboard} onAction={handleAction} onConfigure={setConfigGuest} />}
        {tab==='deploy'    && <DeployTab releases={releases} />}
        {tab==='releases'  && <ReleasesTab releases={releases} />}
        {tab==='platform'  && <PlatformTab />}
      </main>

      {showChangePwd && <ChangePasswordModal onClose={() => setShowChangePwd(false)} />}
      {configGuest   && <GuestConfigDrawer guest={configGuest} onClose={() => setConfigGuest(null)} onRefresh={loadDashboard} />}
    </div>
  )
}
