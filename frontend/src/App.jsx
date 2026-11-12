import { useEffect, useState, useCallback, useRef, Fragment } from 'react'
import './App.css'

// ---------------------------------------------------------------------------
// Auth helpers
// ---------------------------------------------------------------------------
const TOKEN_KEY = 'ctrlable_provisioner_token'
function getToken() { return localStorage.getItem(TOKEN_KEY) }
function setToken(t) { localStorage.setItem(TOKEN_KEY, t) }
function clearToken() { localStorage.removeItem(TOKEN_KEY) }

// Resolve an app path against the document base so the app works both at the
// server root (direct, baseURI = "/") and under a dynamic prefix (HA ingress,
// where the proxy injects <base href=".../">). A leading slash is absolute and
// would escape the ingress prefix, so strip it and let the browser resolve it
// relative to <base>.
const rel = (p) => (typeof p === 'string' && p[0] === '/') ? p.slice(1) : p

async function apiFetch(path, opts = {}) {
  const headers = { ...opts.headers }
  const token = getToken()
  if (token) headers['Authorization'] = `Bearer ${token}`
  if (opts.body && typeof opts.body === 'string') headers['Content-Type'] = 'application/json'
  const res = await fetch(rel(path), { ...opts, headers })
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
      const res = await fetch(rel('/api/auth/login'), {
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
      const res = await fetch(rel('/api/auth/setup'), {
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
        <div className="banner">Welcome! Set an admin password to get started.</div>
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
        <h2>Change password</h2>
        {ok
          ? <div className="banner success">Password updated. <button className="btn-xs" onClick={onClose}>Close</button></div>
          : <form onSubmit={submit} style={{display:'flex',flexDirection:'column',gap:'14px'}}>
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
              <div style={{display:'flex',gap:'8px'}}>
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
  const [serialDevs, setSerialDevs] = useState([])
  const [serialBusy, setSerialBusy] = useState(false)
  const [restartNote, setRestartNote] = useState(null)
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
    else
      apiFetch('/api/host/devices/serial').then(r => r.json()).then(d => setSerialDevs(d.devices ?? [])).catch(() => {})
  }, [guest.vmid, guest.kind])

  const refetchSerial = () =>
    apiFetch('/api/host/devices/serial').then(r => r.json()).then(d => setSerialDevs(d.devices ?? [])).catch(() => {})

  const serialOp = async (path, by_id, doneMsg) => {
    setSerialBusy(true); setError(null); setRestartNote(null)
    try {
      const res = await apiFetch(path, { method: 'POST', body: JSON.stringify({ by_id }) })
      const d = await res.json()
      if (!res.ok) { setError(d.detail ?? 'Failed'); return }
      if (d.restart_required) setRestartNote(doneMsg)
      refetchSerial(); onRefresh()
    } catch (e) { setError(e.message) }
    finally { setSerialBusy(false) }
  }
  const attachSerial = (by_id) =>
    serialOp(`/api/guests/${guest.vmid}/serial-device`, by_id,
             `Attached. Restart ${guest.hostname} to activate the device — not done automatically.`)
  const detachSerial = (by_id) =>
    serialOp(`/api/guests/${guest.vmid}/serial-device/detach`, by_id,
             `Removed. Restart ${guest.hostname} to release the device.`)

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
          <button className="btn-icon" style={{marginLeft:'auto'}} onClick={onClose}>✕</button>
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

          {guest.kind === 'qemu' ? (
          <section className="cfg-section">
            <h3>USB passthrough</h3>
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
                    const dup = usbDevs.filter(x => x.vendid === d.vendid && x.prodid === d.prodid).length > 1
                    return (
                      <div key={i} className="device-row">
                        <span className="muted">{label}</span>
                        {dup && <span className="tag" title="Two identical devices share this ID — vendor:product passthrough is ambiguous. Bind by serial via an LXC, or use a network coordinator.">dup</span>}
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
          ) : (
          <section className="cfg-section">
            <h3>USB radios <span className="muted small">— bound by serial, survives port moves</span></h3>
            {restartNote && <div className="banner">{restartNote}</div>}
            {serialDevs.length === 0
              ? <p className="empty small">No USB serial devices detected on the host</p>
              : serialDevs.map((d) => {
                  const mine  = d.bound_to && d.bound_to.vmid === guest.vmid
                  const other = d.bound_to && d.bound_to.vmid !== guest.vmid
                  const dup   = d.vendor_id && serialDevs.filter(x =>
                                  x.vendor_id === d.vendor_id && x.product_id === d.product_id).length > 1
                  const name  = (d.vendor || d.product)
                    ? `${d.vendor ?? ''}${d.vendor && d.product ? ' — ' : ''}${d.product ?? ''}`.trim()
                    : d.by_id
                  return (
                    <div key={d.by_id} className="device-row">
                      <span className="muted" title={d.by_id}>{name}</span>
                      {d.serial && <span className="tag">sn {d.serial}</span>}
                      {d.node && <span className="tag">{d.node.replace('/dev/', '')}</span>}
                      {dup && <span className="tag" title="Two identical models are plugged in — the serial keeps them distinct.">dup model</span>}
                      {mine
                        ? <button className="btn-xs danger" disabled={serialBusy}
                            onClick={() => detachSerial(d.by_id)}>Remove</button>
                        : other
                          ? <span className="tag" title={`Bound to CT ${d.bound_to.vmid}`}>on CT {d.bound_to.vmid}</span>
                          : <button className="btn-xs" disabled={serialBusy}
                              onClick={() => attachSerial(d.by_id)}>Attach</button>}
                    </div>
                  )
                })
            }
            <p className="empty small">Binds the stick at <code>/dev/serial/by-id/…</code> into the container; takes effect on the next container start.</p>
          </section>
          )}

          {guest.kind === 'qemu' && (
            <section className="cfg-section">
              <h3>PCI / GPU passthrough</h3>
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
        <h2>Add instance — {project.site_name}</h2>
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
            <div style={{display:'flex',gap:'8px',marginTop:'14px'}}>
              <button className="btn-primary" onClick={submit} disabled={loading || !selected}>
                {loading ? 'Deploying…' : 'Deploy instance'}
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
// Utilities + shared presentational helpers
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

// status -> a theme variant shared by the dot and the pill
const STATUS_VARIANT = { running:'ok', active:'ok', stopped:'idle', pending:'warn', provisioning:'warn', error:'crit' }
const variantFor = (s) => STATUS_VARIANT[s] ?? 'idle'

// glyph per service template, matching the design mockup
const SVC_ICON = {
  'ctrlable-pro':'⌂', 'zwavejs':'⚡', 'zwave-js':'⚡', 'zigbee2mqtt':'◆',
  'frigate':'▶', 'freepbx':'☎', 'hardware-manager':'⚙', 'dali-bridge':'✦',
}
const svcIcon = (type) => SVC_ICON[type] ?? '●'

// internal-LAN address for a template, from the plan's per-template octets
function internalIp(lan, type) {
  const v = lan?.assignments?.[type]
  if (v == null || !lan?.subnet) return null
  // assignments may arrive as full IPs ("198.18.0.20") or bare octets ("20")
  const s = String(v)
  if (s.includes('.')) return s
  const base = lan.subnet.split('/')[0].split('.').slice(0, 3).join('.')
  return `${base}.${s}`
}

function Dot({ status }) { return <span className={`dot ${variantFor(status)}`} /> }
function Pill({ status, children }) {
  return <span className={`pill ${variantFor(status)}`}><span className={`dot ${variantFor(status)}`} />{children ?? status}</span>
}

function guestActions(g, onAction) {
  return (
    <>
      {g.status === 'stopped' && <button className="btn-xs" onClick={() => onAction(g.vmid,'start')}>Start</button>}
      {g.status === 'running' && <button className="btn-xs danger" onClick={() => onAction(g.vmid,'stop')}>Stop</button>}
      {g.status === 'running' && <button className="btn-xs" onClick={() => onAction(g.vmid,'reboot')}>Reboot</button>}
    </>
  )
}

// ---------------------------------------------------------------------------
// Map tab — the appliance as a map (brain → internal backplane → service ports)
// ---------------------------------------------------------------------------
function Tile({ k, icon, value, unit, sub, pct }) {
  return (
    <div className="tile">
      <div className="k">{icon}{k}</div>
      <div className="v">{value}{unit && <small> {unit}</small>}</div>
      {sub && <div className="sub">{sub}</div>}
      {pct != null && <div className="track"><i style={{ width: `${Math.min(Math.round(pct),100)}%` }} /></div>}
    </div>
  )
}

function MapTab({ data, lan, onAction, onConfigure, goTo }) {
  const [view, setView] = useState('map')
  if (!data) return <div className="loading">Loading…</div>
  if (!data.configured) return (
    <div className="card unconfigured"><h2>Proxmox not configured</h2>
      <p className="muted">Set <code>PVE_HOST</code>, <code>PVE_TOKEN_ID</code>, and <code>PVE_TOKEN_SECRET</code> in <code>backend/.env</code>.</p>
    </div>
  )
  if (data.error) return <div className="banner error">Proxmox error: {data.error}</div>

  const host = data.host ?? {}
  const guests = data.guests ?? []
  const brain = guests.find(g => g.type === 'ctrlable-pro') || guests.find(g => g.kind === 'qemu') || null
  const services = guests.filter(g => g !== brain)
  const lanOn = !!(lan && lan.configured)
  const brokerIp = lan?.broker_ip || internalIp(lan, 'ctrlable-pro')

  // services declared in the address plan but not deployed yet -> ghost ports
  const deployedTypes = new Set(services.map(g => g.type).filter(Boolean))
  const ghosts = Object.keys(lan?.assignments ?? {})
    .filter(t => t !== 'ctrlable-pro' && !deployedTypes.has(t))
    .map(t => ({ type: t, ip: internalIp(lan, t) }))

  const ramPct  = host.mem_total ? (host.mem_used / host.mem_total) * 100 : 0
  const diskPct = host.disk_total ? (host.disk_used / host.disk_total) * 100 : 0

  return (
    <>
      <div className="head">
        <div>
          <h1>Appliance map</h1>
          <p>Everything on this box and how it's wired. Services share one private backplane, so a site-LAN change never breaks them.</p>
        </div>
        <div className="right">
          <div className="seg">
            <button className={view==='map'?'on':''} onClick={() => setView('map')}>◈ Map</button>
            <button className={view==='list'?'on':''} onClick={() => setView('list')}>☰ List</button>
          </div>
        </div>
      </div>

      <div className="tiles">
        <Tile k="CPU" icon={<span className="dot ok" />} value={`${Math.round((host.cpu ?? 0)*100)}`} unit="%" pct={(host.cpu ?? 0)*100} />
        <Tile k="RAM" icon={<span className="dot ok" />} value={`${Math.round(ramPct)}`} unit="%"
          sub={`${fmtBytes(host.mem_used)} / ${fmtBytes(host.mem_total)}`} pct={ramPct} />
        <Tile k="Disk" icon={<span className="dot ok" />} value={`${Math.round(diskPct)}`} unit="%"
          sub={`${fmtBytes(host.disk_used)} / ${fmtBytes(host.disk_total)}`} pct={diskPct} />
      </div>

      {view === 'map' ? (
        <>
          <div className="map">
            <div className="brainwrap">
              <div className="node brain">
                <div className="r1">
                  <span className="gi">⌂</span>
                  <div>
                    <div className="nm">{brain ? (brain.hostname || 'Ctrlable Pro') : 'Ctrlable Pro'}</div>
                    <div className="tag2">Home Assistant brain{brain ? ` · ${brain.kind?.toUpperCase()} ${brain.vmid}` : ' · not deployed'}</div>
                  </div>
                  {brain && <span style={{marginLeft:'auto'}}><Pill status={brain.status} /></span>}
                </div>
                <div className="r2">
                  {brain?.release && <span>release <b>{brain.release}</b></span>}
                  {brain && brain.status === 'running' && <span>CPU <b>{(brain.cpu*100).toFixed(1)}%</b></span>}
                  {brokerIp && <span>internal <b className="brand mono">{brokerIp}</b></span>}
                </div>
                {brain && (
                  <div className="r3">
                    {guestActions(brain, onAction)}
                    <button className="btn-xs" onClick={() => onConfigure(brain)}>⚙ Configure</button>
                  </div>
                )}
              </div>
            </div>

            <div className="drop" />
            <div className="bus">
              <span className="ic">⇄</span>
              <div>
                <div className="t">Internal LAN</div>
                <div className="sub mono">{lanOn ? `${lan.subnet} · ${lan.bridge} · dnsmasq` : 'not enabled on this appliance'}</div>
              </div>
              <span className={lanOn ? 'on' : 'off'}>{lanOn ? 'ON' : 'OFF'}</span>
            </div>

            <div className="ports">
              {services.map(g => {
                const ip = internalIp(lan, g.type)
                return (
                  <div className="port" key={g.vmid}>
                    <div className="drop" />
                    <div className="node svc">
                      <div className="top">
                        <span className="gi">{svcIcon(g.type)}</span>
                        <div style={{minWidth:0}}>
                          <div className="nm">{g.type || g.hostname}</div>
                          <div className="vr mono">{g.release || g.kind?.toUpperCase()}</div>
                        </div>
                        <button className="btn-icon cfg" title="Configure" onClick={() => onConfigure(g)}>⚙</button>
                      </div>
                      <div className="mid">
                        <Pill status={g.status} />
                        {ip && <span className="ip mono">{ip}</span>}
                      </div>
                      <div className="act">{guestActions(g, onAction)}</div>
                    </div>
                  </div>
                )
              })}

              {ghosts.map(gh => (
                <div className="port" key={`ghost-${gh.type}`}>
                  <div className="drop" />
                  <div className="node svc ghost">
                    <div className="top">
                      <span className="gi">{svcIcon(gh.type)}</span>
                      <div style={{minWidth:0}}>
                        <div className="nm">{gh.type}</div>
                        <div className="vr">not deployed</div>
                      </div>
                    </div>
                    <div className="mid">
                      <Pill status="pending">not deployed</Pill>
                      {gh.ip && <span className="ip pend mono">{gh.ip}</span>}
                    </div>
                    <div className="act"><button className="btn-xs primary" onClick={() => goTo('deploy')}>Deploy</button></div>
                  </div>
                </div>
              ))}

              <div className="port">
                <div className="node svc ghost">
                  <div className="top">
                    <span className="gi">＋</span>
                    <div><div className="nm">Add a service</div><div className="vr">from a release</div></div>
                  </div>
                  <div className="act"><button className="btn-xs ghost" onClick={() => goTo('deploy')}>Open Deploy</button></div>
                </div>
              </div>
            </div>

            <div className="legend">
              <span className="it"><span className="dot ok" /> running</span>
              <span className="it"><span className="dot idle" /> stopped / not deployed</span>
              <span className="it"><span className="dot warn" /> needs attention</span>
              <span className="it"><span className="wire-ex" /> on the internal backplane</span>
            </div>
          </div>

          {lanOn && (
            <div className="tip" style={{marginTop:16}}>
              <span style={{color:'var(--brand)'}}>⇄</span>
              <div>Services on the backplane reach each other over fixed <span className="mono">{lan.subnet?.split('/')[0]?.replace(/\.0$/, '.x')}</span> addresses.
                See <a href="#network" onClick={e => { e.preventDefault(); goTo('network') }}>Internal network</a> for the full plan.</div>
            </div>
          )}
        </>
      ) : (
        <div className="card">
          <div className="hd"><h2>Services</h2>
            <span className="sub">{guests.filter(g=>g.status==='running').length} running · {guests.length} total</span>
          </div>
          <table>
            <thead><tr><th>Service</th><th>Status</th><th>Kind</th><th>VMID</th><th>Internal</th><th></th></tr></thead>
            <tbody>
              {guests.map(g => (
                <tr key={g.vmid}>
                  <td><div className="svccell"><span className="gi">{svcIcon(g.type)}</span>
                    <b>{g.type || g.hostname}</b></div></td>
                  <td><Pill status={g.status} /></td>
                  <td className="muted mono">{g.kind?.toUpperCase()}</td>
                  <td className="mono">{g.vmid}</td>
                  <td className="mono" style={{color: internalIp(lan,g.type) ? 'var(--brand)' : 'var(--faint)'}}>
                    {internalIp(lan, g.type) || '—'}</td>
                  <td className="row-actions">
                    {guestActions(g, onAction)}
                    <button className="btn-icon" title="Configure" onClick={() => onConfigure(g)}>⚙</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}

// ---------------------------------------------------------------------------
// Internal network tab
// ---------------------------------------------------------------------------
function InternalNetworkTab({ lan, data, reloadLan }) {
  const [enabling, setEnabling] = useState(false)
  const [result, setResult]     = useState(null)
  const [error, setError]       = useState(null)
  const [migrating, setMigrating]   = useState(null)
  const [migResults, setMigResults] = useState({})

  const migrate = async (vmid, template) => {
    setMigrating(template)
    try {
      const res = await apiFetch('/api/lan/internal/migrate', { method: 'POST', body: JSON.stringify({ vmid, template }) })
      const d = await res.json()
      setMigResults(m => ({ ...m, [template]: res.ok ? d : { ok: false, output: d.detail || 'failed' } }))
      if (res.ok) reloadLan()
    } catch (e) { setMigResults(m => ({ ...m, [template]: { ok: false, output: e.message } })) }
    finally { setMigrating(null) }
  }

  const enable = async () => {
    setEnabling(true); setError(null); setResult(null)
    try {
      const res = await apiFetch('/api/lan/internal/enable', { method: 'POST', body: JSON.stringify({}) })
      const data = await res.json()
      if (!res.ok) { setError(data.detail ?? 'Enable failed'); return }
      setResult(data); reloadLan()
    } catch (e) { setError(e.message) }
    finally { setEnabling(false) }
  }

  if (!lan) return <div className="loading">Loading…</div>

  const configured = lan.configured
  const plan = Object.entries(lan.assignments ?? {})
    .map(([type, octet]) => ({ type, ip: internalIp(lan, type), octet }))
    .sort((a, b) => a.octet - b.octet)

  return (
    <>
      <div className="head"><div>
        <h1>Internal network</h1>
        <p>A private bridge only this appliance's services share. They reach each other over fixed addresses here, so changing the site LAN — or moving the box to a new site — never breaks Z-Wave, Zigbee, cameras, or the PBX.</p>
      </div></div>

      {error && <div className="banner error">{error}</div>}

      <div className="card">
        <div className="hd">
          <span style={{color:'var(--brand)',fontSize:17}}>⇄</span>
          <div><h2>Internal management LAN</h2>
            <span className="sub mono">
              {configured ? `${lan.bridge} · ${lan.subnet} · gateway ${internalIp(lan,'__gw__') ?? (lan.subnet?.split('/')[0]?.replace(/\.0$/,'.1'))} · dnsmasq` : 'not declared by this release'}
            </span></div>
          <div className="right">
            <span className={`toggle ${configured ? 'on' : ''}`}>
              <span style={{fontSize:12,fontWeight:700,color:configured?'var(--brand)':'var(--muted)'}}>{configured?'On':'Off'}</span>
              <span className="switch" />
            </span>
          </div>
        </div>
        {configured && (
          <div className="bd">
            <p className="form-note" style={{marginBottom:12}}>
              Enabling brings up the fabric and attaches the Ctrlable Pro VM (hot-plug, no reboot).
              Existing services keep running and are re-wired on their next deploy.
            </p>
            <button className="btn-primary" onClick={enable} disabled={enabling || !lan.broker_known}>
              {enabling ? 'Enabling…' : 'Enable internal LAN'}
            </button>
            {!lan.broker_known && <p className="form-note" style={{marginTop:6}}>No deployed Ctrlable Pro VM found to attach.</p>}
            {result && (
              <div style={{marginTop:14}}>
                {result.ok
                  ? (result.broker_reachable
                      ? <div className="banner success">Enabled — broker reachable at {lan.broker_ip}.</div>
                      : <div className="banner">Fabric up and NIC attached, but the broker isn't answering yet.
                          {result.reboot_suggested && ` Reboot VM #${result.broker_vmid} to pick up the NIC.`}</div>)
                  : <div className="banner error">Enable failed — see the log.</div>}
                {result.output && <pre className="build-log">{result.output}</pre>}
              </div>
            )}
          </div>
        )}
      </div>

      {configured && (
        <div className="netgrid">
          <div className="card" style={{margin:0}}>
            <div className="hd"><h2>Address plan</h2><span className="sub">deterministic, per service — move one onto the backplane in place</span></div>
            <table>
              <thead><tr><th>Service</th><th>Internal IP</th><th>Mode</th><th>Status</th><th></th></tr></thead>
              <tbody>
                <tr><td>Ctrlable Pro</td><td className="mono" style={{color:'var(--brand)'}}>{lan.broker_ip || internalIp(lan,'ctrlable-pro')}</td>
                  <td className="muted small">DHCP-reserved</td>
                  <td>{lan.broker_known ? <Pill status="running">reserved</Pill> : <Pill status="pending">pending</Pill>}</td><td /></tr>
                {plan.filter(p => p.type !== 'ctrlable-pro').map(p => {
                  const guest = data?.guests?.find(g => g.type === p.type)
                  const r = migResults[p.type]
                  return (
                    <Fragment key={p.type}>
                      <tr>
                        <td>{p.type}</td>
                        <td className="mono">{p.ip}</td>
                        <td className="muted small">static</td>
                        <td>{guest ? <Pill status={guest.status} /> : <Pill status="pending">not deployed</Pill>}</td>
                        <td className="row-actions">
                          {guest && <button className="btn-xs" onClick={() => migrate(guest.vmid, p.type)} disabled={migrating === p.type}>
                            {migrating === p.type ? 'Moving…' : 'Move to internal LAN'}</button>}
                        </td>
                      </tr>
                      {r && (
                        <tr><td colSpan={5} style={{background:'var(--surface-2)'}}>
                          {r.ok
                            ? <div className="banner success" style={{margin:0}}>
                                {r.done
                                  ? `${p.type} is on the internal LAN at ${r.internal_ip}.${r.reconnected ? ' Reconnected to the broker.' : ''}`
                                  : `${p.type}: NIC attached at ${r.internal_ip} — see the log for the next step.`}
                              </div>
                            : <div className="banner error" style={{margin:0}}>Move failed — see the log.</div>}
                          {r.output && <pre className="build-log">{r.output}</pre>}
                        </td></tr>
                      )}
                    </Fragment>
                  )
                })}
              </tbody>
            </table>
          </div>

          <div style={{display:'flex',flexDirection:'column',gap:16}}>
            <div className="card" style={{margin:0}}>
              <div className="hd"><h2>Isolation</h2><span className="pill ok">isolated L2</span></div>
              <div className="bd">
                <div className="checkrow"><span className="ck">✓</span><span>No physical ports on <b>{lan.bridge}</b> — cannot disturb the site LAN</span></div>
                <div className="checkrow"><span className="ck">✓</span><span>Benchmark space <span className="mono">198.18.0.0/15</span> — won't collide with site LANs or the WG fabric</span></div>
                <div className="checkrow"><span className="ck">✓</span><span>Falls back to a free subnet if the default is already in use</span></div>
              </div>
            </div>
            <div className="card" style={{margin:0}}>
              <div className="hd"><h2>Wiring</h2><span className="sub">applied on each deploy</span></div>
              <div className="bd" style={{paddingTop:4}}>
                <div className="wire"><span className="src">Zigbee2MQTT</span><span className="arrow">→</span>
                  <span className="dst mono">mqtt://{lan.broker_ip || internalIp(lan,'ctrlable-pro')}:1883</span></div>
                <div className="wire"><span className="src">Ctrlable Pro</span><span className="arrow">→</span>
                  <span className="dst mono">zwave {internalIp(lan,'zwavejs') || '—'}:3000</span></div>
                <div className="wire"><span className="src">Ctrlable Pro</span><span className="arrow">→</span>
                  <span className="dst mono">frigate {internalIp(lan,'frigate') || '—'}:5000</span></div>
              </div>
            </div>
          </div>
        </div>
      )}
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
  const pct = total ? Math.round((idx / total) * 100) : 0

  return (
    <div className={`instance-row${busy ? ' instance-row-busy' : ''}`}>
      <div className="instance-row-main">
        <Dot status={inst.status} />
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

const SEV_COLOR = { OUTAGE: 'var(--crit)', DRIFT: 'var(--warn)', UNKNOWN: 'var(--muted)', OK: 'var(--ok)' }

function SevBadge({ sev }) {
  return <span className="tag" style={{
    background: 'color-mix(in srgb, ' + (SEV_COLOR[sev] ?? 'var(--muted)') + ' 16%, transparent)',
    color: SEV_COLOR[sev] ?? 'var(--muted)',
  }}>{sev}</span>
}

function DriftPanel({ projectId }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [open, setOpen] = useState(false)

  const check = async () => {
    setLoading(true); setOpen(true)
    try {
      const res = await apiFetch(`/api/projects/${projectId}/drift`)
      setData(await res.json())
    } catch { setData({ error: 'could not reach the orchestrator' }) }
    finally { setLoading(false) }
  }

  return (
    <div className="drift-panel">
      <div className="drift-panel-head">
        <button className="btn-xs" onClick={check} disabled={loading}>
          {loading ? 'Checking…' : (open ? 'Re-check drift' : 'Check drift')}
        </button>
        {data && !data.error && <SevBadge sev={data.severity} />}
        {data && !data.manifest_present && <span className="small muted">no manifest for {data.release}</span>}
      </div>
      {open && data && (data.error
        ? <p className="small" style={{ color: 'var(--crit)' }}>{data.error}</p>
        : (
          <table className="drift-table">
            <thead>
              <tr><th></th><th>component</th><th>live</th><th>pinned</th><th>schema</th><th>where</th><th>detail</th></tr>
            </thead>
            <tbody>
              {data.rows.map(r => (
                <tr key={r.instance_id}>
                  <td><SevBadge sev={r.severity} /></td>
                  <td className="mono">{r.template}</td>
                  <td className="mono">{r.live_version || '—'}</td>
                  <td className="mono">{r.pinned_version || '—'}</td>
                  <td className="mono">
                    {r.schema_required == null ? '—'
                      : `${r.schema_live == null ? '?' : r.schema_live}/${r.schema_required}`}
                  </td>
                  <td className="small muted">
                    {r.address ? `${r.address}` : '—'}
                    {r.address_source && r.address !== null ? <span className="muted"> ({r.address_source})</span> : null}
                  </td>
                  <td className="small">
                    {r.findings.length === 0
                      ? <span className="muted">matches manifest</span>
                      : r.findings.map((f, i) => (
                          <div key={i} style={{ color: SEV_COLOR[f.severity] }}>{f.detail}</div>
                        ))}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ))}
    </div>
  )
}

function DeployTab({ releases }) {
  const [form, setForm]           = useState({ ...EMPTY_FORM, release: releases[0]?.release ?? '' })
  const [deploying, setDeploying] = useState(false)
  const [error, setError]         = useState(null)
  const [projects, setProjects]   = useState([])
  const [addInstProject, setAddInstProject] = useState(null)
  const set = key => val => setForm(f => ({ ...f, [key]: val }))

  const loadProjects = useCallback(() => {
    apiFetch('/api/projects').then(r => r.json()).then(setProjects).catch(() => {})
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

  return (
    <>
      <div className="head"><div>
        <h1>Deploy</h1>
        <p>Provision a full stack for a site, or add a service to an existing one.</p>
      </div></div>

      <section className="deploy-form card">
        <h2>New stack</h2>
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
          {deploying ? 'Deploying…' : 'Deploy stack'}
        </button>
      </section>

      {projects.length > 0 && (
        <section className="projects-list">
          <h2 className="section-title">Deployed stacks</h2>
          {projects.map(p => (
            <div key={p.id} className="card project-card">
              <div className="project-card-header">
                <strong>{p.site_name}</strong>
                <span className="tag accent">{p.release}</span>
                <button className="btn-xs" style={{marginLeft:'auto'}} onClick={() => setAddInstProject(p)}>+ Add instance</button>
                <button className="btn-xs danger" onClick={() => deleteProject(p)}>Delete project</button>
              </div>
              {p.instances.length === 0
                ? <p className="empty small muted">No instances yet — deploy is running or failed.</p>
                : p.instances.map(i => <InstanceRow key={i.id} inst={i} onDelete={inst => deleteInstance(p.id, inst)} />)}
              {p.instances.length > 0 && <DriftPanel projectId={p.id} />}
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
    </>
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
  const statusColor = build.status === 'success' ? 'var(--ok)' : build.status === 'failed' ? 'var(--crit)' : 'var(--warn)'

  return (
    <div className="build-log-wrap">
      <div className="build-log-header">
        <span>Build #{build.id} — {build.release}</span>
        <span className="pill" style={{background:'transparent',color:statusColor}}>{build.status}</span>
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
      const res = await apiFetch('/api/builds', { method: 'POST', body: JSON.stringify({ release: release.release }) })
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
        <span className="muted mono">{release.community_ref}</span>
        <button className="btn-xs" style={{marginLeft:'auto'}} onClick={triggerBuild} disabled={building || !!activeBuildId}>
          {building ? 'Starting…' : 'Build'}
        </button>
      </div>

      {error && <div className="banner error" style={{marginTop:10}}>{error}</div>}

      {activeBuildId && (
        <BuildLogViewer buildId={activeBuildId} onDone={() => {
          apiFetch(`/api/releases/${release.release}/built-templates`)
            .then(r => r.json()).then(setTemplates).catch(() => {})
        }} />
      )}

      {templates.length > 0 && (
        <table className="cfg-table" style={{marginTop:12}}>
          <thead><tr><th>Template</th><th>Kind</th><th>VMID</th><th>Built</th></tr></thead>
          <tbody>
            {templates.map(t => (
              <tr key={t.name}>
                <td>{t.name}</td>
                <td><span className="tag">{t.kind}</span></td>
                <td className="muted mono">{t.template_vmid}</td>
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
    <>
      <div className="head"><div>
        <h1>Releases</h1>
        <p>The validated software combination this appliance builds from — one pin per component, moved together.</p>
      </div></div>

      {releases.map(r => <ReleaseCard key={r.release} release={r} onBuildStarted={loadBuilds} />)}

      {builds.length > 0 && (
        <div className="card">
          <div className="hd"><h2>Build history</h2></div>
          <table>
            <thead><tr><th>#</th><th>Release</th><th>Status</th><th>Started</th></tr></thead>
            <tbody>
              {builds.map(b => (
                <tr key={b.id}>
                  <td className="muted mono">{b.id}</td>
                  <td>{b.release}</td>
                  <td><Pill status={b.status === 'success' ? 'running' : b.status === 'failed' ? 'error' : 'provisioning'}>{b.status}</Pill></td>
                  <td className="muted">{new Date(b.started_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}

// ---------------------------------------------------------------------------
// Provisioner self-update card (shown on the Platform page)
// ---------------------------------------------------------------------------
function ProvisionerCard() {
  const [version, setVersion] = useState(null)
  const [updateInfo, setUpdateInfo] = useState(null)   // /api/system/update-check
  const [updating, setUpdating] = useState(false)
  const [restarting, setRestarting] = useState(false)
  const [updateResult, setUpdateResult] = useState(null)
  const [error, setError] = useState(null)

  const loadVersion = useCallback(() =>
    apiFetch('/api/version').then(r => r.json()).then(setVersion).catch(() => {}), [])
  const checkUpdate = useCallback(() =>
    apiFetch('/api/system/update-check').then(r => r.json()).then(setUpdateInfo).catch(() => setUpdateInfo(null)), [])
  useEffect(() => { loadVersion(); checkUpdate() }, [loadVersion, checkUpdate])

  // Poll /api/version across the restart window, then re-check for updates.
  const pollVersion = (fromRev) => {
    let tries = 0
    const poll = setInterval(async () => {
      tries += 1
      try {
        const v = await (await fetch(rel('/api/version'))).json()
        setVersion(v)
        if (v.revision && v.revision !== fromRev) { clearInterval(poll); setRestarting(false); checkUpdate() }
      } catch { /* restart window \u2014 keep polling */ }
      if (tries > 20) { clearInterval(poll); setRestarting(false) }
    }, 3000)
  }

  const selfUpdate = async () => {
    setUpdating(true); setError(null); setUpdateResult(null)
    const fromRev = version?.revision
    try {
      const res = await apiFetch('/api/system/self-update', { method: 'POST', body: JSON.stringify({}) })
      let data
      try { data = await res.json() }
      catch {
        // Update runs synchronously then restarts; behind the ingress proxy the
        // long request can time out to an HTML error page (not JSON) even though
        // the update is underway. Treat as dispatched and poll for the new rev.
        setRestarting(true); pollVersion(fromRev); return
      }
      if (res.status === 409) { setError(data.detail ?? 'Busy'); return }
      setUpdateResult(data)
      if (data.ok && data.restarting) { setRestarting(true); pollVersion(data.from) }
      else if (data.ok) { checkUpdate() }
    } catch {
      setRestarting(true); pollVersion(fromRev)
    } finally { setUpdating(false) }
  }

  const avail = updateInfo?.update_available
  return (
    <div className="card">
      <div className="hd">
        <span className="status-dot" style={{background:'var(--ok)'}} />
        <h2>Orchestrator software</h2>
        <div className="right">
          {avail
            ? <button className="btn-primary" onClick={selfUpdate} disabled={updating || restarting}>
                {updating ? 'Updating\u2026' : restarting ? 'Restarting\u2026' : 'Update now'}
              </button>
            : <span className="sub mono" style={{opacity:.75}}>
                {restarting ? 'Restarting\u2026' : updateInfo ? 'Up to date \u2713' : '\u2026'}
              </span>}
        </div>
      </div>
      <div className="bd">
        {error && <div className="banner error">{error}</div>}
        {restarting && !updateResult &&
          <div className="banner success">{'Update is running \u2014 the orchestrator will restart and the version will refresh shortly.'}</div>}
        {avail && !restarting &&
          <div className="banner" style={{marginBottom:10}}>
            {'Update available'}{updateInfo?.behind ? ` \u2014 ${updateInfo.behind} commit${updateInfo.behind === 1 ? '' : 's'} behind` : ''}
            {updateInfo?.latest && <>{' \u2192 '}<code>{String(updateInfo.latest).slice(0,7)}</code></>}
          </div>}
        <dl className="kv">
          <dt>Version</dt><dd><code>{version?.version ?? '\u2026'}</code></dd>
          <dt>Revision</dt><dd><code>{version?.revision ?? '\u2026'}</code></dd>
          {version?.ref && <><dt>Ref</dt><dd><code>{version.ref}</code></dd></>}
        </dl>
        <p className="form-note" style={{marginTop:10}}>
          Fast-forwards the orchestrator to the latest build, refreshes the host tools, verifies, then
          restarts. Refused while a build or deploy is running; the checkout is rolled back if a step fails.
        </p>
        {updateResult && (
          <div style={{marginTop:12}}>
            {updateResult.ok
              ? <div className="banner success">
                  {updateResult.unchanged ? 'Already up to date.' : `Updated ${String(updateResult.from).slice(0,7)} \u2192 ${String(updateResult.to).slice(0,7)}.`}
                  {updateResult.restarting && ' Restarting \u2014 version will refresh shortly.'}
                  {updateResult.host_tools?.attempted && !updateResult.host_tools?.ok &&
                    ' (Host tools not refreshed \u2014 the host may need a manual reinstall on this first update.)'}
                </div>
              : <div className="banner error">{`Update failed at \u201c${updateResult.stage}\u201d.`}{updateResult.rolled_back ? ' Rolled back.' : ''}</div>}
            {updateResult.steps && (
              <pre className="build-log">
                {updateResult.steps.map(st => `[${st.rc === 0 ? 'ok' : 'FAIL'}] ${st.step}\n${st.output || ''}`).join('\n')}
              </pre>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Platform tab
// ---------------------------------------------------------------------------
function PlatformTab() {
  const [status, setStatus]   = useState(null)
  const [lan, setLan]         = useState(null)
  const [token, setTokenVal]  = useState('')
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
      const res = await apiFetch('/api/platform/enroll', { method: 'POST', body: JSON.stringify({ token: token.trim() }) })
      const data = await res.json()
      if (!res.ok) { setError(data.detail ?? 'Enrollment failed'); return }
      setTokenVal(''); loadStatus()
    } catch (e) { setError(e.message) }
    finally { setEnrolling(false) }
  }

  return (
    <>
      <div className="head"><div>
        <h1>Platform</h1>
        <p>This appliance's own software, and how it reaches the Ctrlable portal and the site LAN.</p>
      </div></div>

      <ProvisionerCard />

      {!status ? <div className="loading">Loading…</div> : status.enrolled ? (
        <>
          <div className="tiles">
            <Tile k="Enrollment" icon={<span className="dot ok" />} value="Active" sub="portal.ctrlable.com" />
            <Tile k="Tunnel" icon={<span className="dot ok" />} value={<span className="mono" style={{fontSize:17}}>{status.tunnel_ip}</span>}
              sub={`WireGuard · ${status.wg_iface}`} />
            <Tile k="LAN access" icon={<span className={`dot ${lan?.configured ? 'ok':'warn'}`} />}
              value={lan?.configured ? 'Routed' : 'Pending'} sub={lan?.configured ? lan.lan_subnet : 'auto-detect'} />
          </div>

          <div className="card">
            <div className="hd"><h2>Portal enrollment</h2>
              <div className="right"><a className="btn-xs" href={status.portal_url} target="_blank" rel="noreferrer">View in portal →</a></div>
            </div>
            <div className="bd">
              <dl className="kv">
                <dt>Device ID</dt><dd><code>{status.device_id}</code></dd>
                <dt>Tunnel IP</dt><dd><code>{status.tunnel_ip}</code></dd>
                <dt>WG interface</dt><dd><code>{status.wg_iface}</code></dd>
                <dt>Enrolled</dt><dd>{new Date(status.enrolled_at).toLocaleString()}</dd>
              </dl>
            </div>
          </div>

          <div className="card">
            <div className="hd"><h2>Site LAN</h2>
              <span className={`pill ${lan?.configured ? 'ok' : 'warn'}`}>{lan?.configured ? 'routed' : 'pending'}</span></div>
            <div className="bd">
              {lan?.configured ? (
                <dl className="kv">
                  <dt>Interface</dt><dd><code>{lan.lan_iface}</code></dd>
                  <dt>LAN subnet</dt><dd><code>{lan.lan_subnet}</code></dd>
                  <dt>Proxy subnet</dt><dd>{lan.proxy_subnet ? <code>{lan.proxy_subnet}</code> : <span className="muted">pending portal allocation…</span>}</dd>
                  <dt>NAT / NETMAP</dt><dd><span className="pill ok">active</span></dd>
                </dl>
              ) : <p className="form-note">LAN not yet detected — will configure automatically.</p>}
            </div>
          </div>
        </>
      ) : (
        <div className="card"><div className="bd">
          {status.pending_token ? (
            <>
              <div className="platform-pending"><span className="status-dot" style={{background:'var(--warn)'}} />
                <strong>Waiting for internet — auto-enrollment pending</strong></div>
              <p className="form-note" style={{marginTop:10}}>
                An enrollment token was provided during installation. The orchestrator will connect to
                portal.ctrlable.com automatically once internet access is detected.
              </p>
            </>
          ) : (
            <>
              <h2 style={{fontSize:15,marginBottom:8}}>Enroll in Ctrlable Portal</h2>
              <p className="form-note" style={{marginBottom:12}}>
                Enrolling connects this orchestrator to portal.ctrlable.com via WireGuard, enabling remote
                management and monitoring. Generate an enrollment token under <strong>Devices → Add Device</strong>.
              </p>
            </>
          )}
          {error && <div className="banner error">{error}</div>}
          <label className="field" style={{marginTop: status.pending_token ? 10 : 0}}>
            <span>{status.pending_token ? 'Re-enroll with a new token' : 'Enrollment token'}</span>
            <input value={token} onChange={e => setTokenVal(e.target.value)} placeholder="Paste token from portal.ctrlable.com" />
          </label>
          <button className="btn-primary" style={{marginTop:12}} onClick={enroll} disabled={enrolling || !token.trim()}>
            {enrolling ? 'Enrolling…' : status.pending_token ? 'Re-enroll' : 'Enroll'}
          </button>
        </div></div>
      )}
    </>
  )
}

// ---------------------------------------------------------------------------
// Theme
// ---------------------------------------------------------------------------
function useTheme() {
  const [theme, setThemeState] = useState(() => {
    try { return localStorage.getItem('prov_theme') } catch { return null }
  })
  useEffect(() => {
    const root = document.documentElement
    if (theme) root.setAttribute('data-theme', theme)
    else root.removeAttribute('data-theme')
  }, [theme])
  const sysDark = () => { try { return matchMedia('(prefers-color-scheme: dark)').matches } catch { return false } }
  const current = theme || (sysDark() ? 'dark' : 'light')
  const toggle = () => {
    const next = current === 'dark' ? 'light' : 'dark'
    setThemeState(next)
    try { localStorage.setItem('prov_theme', next) } catch { /* ignore */ }
  }
  return { current, toggle }
}

// ---------------------------------------------------------------------------
// Root app
// ---------------------------------------------------------------------------
const NAV = [
  { group: 'Appliance', items: [
    { id: 'map',     label: 'Map',              ic: '◈' },
    { id: 'network', label: 'Internal network', ic: '⇄' },
  ]},
  { group: 'Fleet', items: [
    { id: 'deploy',   label: 'Deploy',   ic: '＋' },
    { id: 'releases', label: 'Releases', ic: '⤓' },
    { id: 'platform', label: 'Platform', ic: '◉' },
  ]},
]

export default function App() {
  const [authReady, setAuthReady]           = useState(false)
  const [mustChangePassword, setMustChange] = useState(false)
  const [loggedIn, setLoggedIn]             = useState(!!getToken())
  const [tab, setTab]                       = useState('map')
  const [dashboard, setDashboard]           = useState(null)
  const [releases, setReleases]             = useState([])
  const [lan, setLan]                       = useState(null)
  const [fetchErr, setFetchErr]             = useState(null)
  const [showChangePwd, setShowChangePwd]   = useState(false)
  const [configGuest, setConfigGuest]       = useState(null)
  const { current: theme, toggle: toggleTheme } = useTheme()

  useEffect(() => {
    fetch(rel('/api/auth/status')).then(r => r.json())
      .then(d => { setMustChange(d.must_change_password); setAuthReady(true) })
      .catch(() => setAuthReady(true))
  }, [])

  const loadDashboard = useCallback(() => {
    apiFetch('/api/dashboard')
      .then(r => r.json()).then(d => { setDashboard(d); setFetchErr(null) })
      .catch(e => setFetchErr(e.message))
  }, [])
  const loadLan = useCallback(() => {
    apiFetch('/api/lan/internal').then(r => r.json()).then(setLan).catch(() => {})
  }, [])

  useEffect(() => {
    if (!authReady || !loggedIn) return
    loadDashboard(); loadLan()
    apiFetch('/api/releases').then(r => r.json()).then(setReleases).catch(() => {})
    const id = setInterval(loadDashboard, 30_000)
    return () => clearInterval(id)
  }, [authReady, loggedIn, loadDashboard, loadLan])

  const handleAction = useCallback(async (vmid, action) => {
    await apiFetch(`/api/guests/${vmid}/${action}`, { method: 'POST' })
    setTimeout(loadDashboard, 1500)
  }, [loadDashboard])

  const logout = () => { clearToken(); setLoggedIn(false); setDashboard(null) }

  if (!authReady) return null
  if (mustChangePassword) return <SetupPasswordScreen onDone={() => { setMustChange(false); setLoggedIn(true) }} />
  if (!loggedIn) return <LoginScreen onLogin={({ mustChange }) => {
    if (mustChange) setMustChange(true); else setLoggedIn(true)
  }} />
  if (fetchErr) return <div className="banner error" style={{margin:20}}>Cannot reach API: {fetchErr}</div>

  const node   = dashboard?.host?.node ?? '…'
  const uptime = dashboard?.host?.uptime
  const activeRelease = releases.find(r => r.active)?.release
  const lanOn = !!(lan && lan.configured)

  return (
    <>
      <div className="topbar">
        <div className="logo"><span className="mark" /> Ctrlable Provisioner</div>
        <div className="ctx">
          <span className="dot ok" />
          <span className="nm">{node}</span>
          <span className="meta mono">
            {[uptime > 0 ? `up ${fmtUptime(uptime)}` : null, activeRelease].filter(Boolean).join(' · ')}
          </span>
        </div>
        <div className="spacer" />
        <button className="icon-btn" title="Toggle theme" onClick={toggleTheme}>{theme === 'dark' ? '☀' : '☾'}</button>
        <button className="btn-xs" onClick={() => setShowChangePwd(true)}>Password</button>
        <button className="btn-xs" onClick={logout}>Sign out</button>
        <div className="avatar">A</div>
      </div>

      <div className="layout">
        <nav className="rail">
          {NAV.map(sec => (
            <div key={sec.group}>
              <div className="grp">{sec.group}</div>
              {sec.items.map(it => (
                <button key={it.id} className={`navitem ${tab === it.id ? 'active' : ''}`} onClick={() => setTab(it.id)}>
                  <span className="ic">{it.ic}</span> {it.label}
                  {it.id === 'network' && lanOn && <span className="nav-badge brand">on</span>}
                </button>
              ))}
            </div>
          ))}
        </nav>

        <main>
          {tab === 'map'      && <MapTab data={dashboard} lan={lan} onAction={handleAction} onConfigure={setConfigGuest} goTo={setTab} />}
          {tab === 'network'  && <InternalNetworkTab lan={lan} data={dashboard} reloadLan={loadLan} />}
          {tab === 'deploy'   && <DeployTab releases={releases} />}
          {tab === 'releases' && <ReleasesTab releases={releases} />}
          {tab === 'platform' && <PlatformTab />}
        </main>
      </div>

      {showChangePwd && <ChangePasswordModal onClose={() => setShowChangePwd(false)} />}
      {configGuest   && <GuestConfigDrawer guest={configGuest} onClose={() => setConfigGuest(null)} onRefresh={loadDashboard} />}
    </>
  )
}
