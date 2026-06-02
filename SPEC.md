# Ctrlable Provisioner — SPEC.md

> Handoff spec for Claude Code. One-click, dealer-facing provisioning of Ctrlable
> appliance stacks on Proxmox VE, built on `community-scripts/ProxmoxVE` as the
> template **builder** and the Proxmox API as the runtime control plane.

---

## 1. Purpose

A single **orchestrator LXC** that lets a dealer stand up a complete Ctrlable
appliance stack on a fresh Proxmox host with one click, and add more instances
later — always running the **latest validated release**, never bleeding-edge
upstream. The orchestrator also reports each site's installed release and
instances up to the central Ctrlable WireGuard management platform.

**Non-goals:** this is not a fork of community-scripts, not a config-management
system for already-deployed sites (that's the management platform), and not a
replacement for HAOS's own update mechanism.

---

## 2. Core concepts

### 2.1 Release manifest = source of truth
A versioned, pinned manifest (e.g. `2026.06`) defines a known-good combination of
component versions you have validated *together*. "Latest validated" always means
"whichever release the templates on this host were built from." Nothing pulls
`main` at deploy time.

### 2.2 Two phases — build once, clone many
- **Build** (rare, per release, on the Proxmox host): run each community-script
  with pinned `var_*` + pinned app version, then convert the result to a Proxmox
  **template** (`pct template` / `qm template`). The build is the only time we
  touch upstream/network.
- **Deploy** (frequent, per project/instance, offline-fast): `clone` a template
  and let it self-personalize on first boot. Seconds, not minutes; works on flaky
  or not-yet-provisioned site internet.

### 2.3 Three planes
| Plane | Where it runs | What it does |
|---|---|---|
| **Control / monitor** | Orchestrator LXC → Proxmox **API** (scoped token) | Dashboard, inventory, clone-to-deploy, start/stop/reboot, metrics |
| **Build** | Orchestrator → **host trigger** (privileged) | Run community-scripts, bake Ctrlable config, snapshot to template — per release only |
| **First-boot** | Inside each cloned guest | Identity reset + pull service-wiring assignment from orchestrator |

Day-to-day dealer operation is pure API. The host trigger is reached only when a
new release is built. Deploys clone via the API; per-guest personalization is
self-service on first boot (§8), so deploys do **not** require host shell access.

---

## 3. Appliance catalog

Each template is built from a pinned community-script. `frigate` and `freepbx`
placement are configurable; defaults below follow what each script natively
produces.

| Template | Kind | Builder script | Notes |
|---|---|---|---|
| `ctrlable-pro` | VM | `vm/haos-vm.sh` | HAOS, rebranded, base config baked. Special deploy flow (§9). |
| `zwavejs` | LXC | `ct/zwave-js-ui.sh` | Z-Wave JS UI. Coordinator over IP (§10). |
| `zigbee2mqtt` | LXC | `ct/zigbee2mqtt.sh` | Z2M. Coordinator over IP (§10). |
| `docker-portainer` | LXC | `ct/docker.sh` | Docker + Portainer. `docker.sh` supports Portainer + `-s update`. |
| `frigate` | LXC | `ct/frigate.sh` | Native Frigate LXC. **Decision:** keep separate (default) or fold into the docker LXC — see §18. |
| `freepbx` | LXC | `ct/freepbx.sh` | LXC per the script. **Decision:** VM is an option for stronger isolation — see §18. |

Pin the **script source** to a git tag/SHA, not `main`, so the builder is
reproducible: fetch `build.func` and each `ct/*.sh` / `vm/*.sh` from a pinned ref.

---

## 4. Release manifest

YAML, versioned in the repo, one file per release under `releases/`.

```yaml
release: "2026.06"
community_scripts_ref: "v2026.05.3"   # git tag/SHA — NOT main
proxmox_min_version: "8.2"
templates:
  ctrlable-pro:
    kind: vm
    builder: vm/haos-vm.sh
    haos_version: "15.1"
    rebrand: true
    base_backup: "ctrlable-base-2026.06.tar"
    vmid_base: 9000
  zwavejs:
    kind: lxc
    builder: ct/zwave-js-ui.sh
    app_version: "9.x"
    os: { distro: debian, version: "12" }
    resources: { cpu: 2, ram: 2048, disk: 8 }
    unprivileged: true
  zigbee2mqtt:
    kind: lxc
    builder: ct/zigbee2mqtt.sh
    app_version: "1.x"
    os: { distro: debian, version: "12" }
    resources: { cpu: 2, ram: 2048, disk: 8 }
    unprivileged: true
  docker-portainer:
    kind: lxc
    builder: ct/docker.sh
    portainer: true
    os: { distro: debian, version: "12" }
    resources: { cpu: 4, ram: 4096, disk: 20 }
    unprivileged: false
  frigate:
    kind: lxc
    builder: ct/frigate.sh
    app_version: "0.14.x"
    resources: { cpu: 4, ram: 4096, disk: 32 }
    unprivileged: false
  freepbx:
    kind: lxc
    builder: ct/freepbx.sh
    resources: { cpu: 2, ram: 2048, disk: 16 }
```

---

## 5. Build pipeline (per release, on host)

Triggered when a new release is tagged. Runs through the **host trigger** (§13).

For each template in the manifest:
1. Export pinned `var_*` env vars from the manifest and run the builder with `METHOD=default`
2. Apply Ctrlable post-build steps inside the new guest (install firstboot service, pin versions, rebrand)
3. Stop the guest, clear instance state (`/etc/machine-id`, SSH host keys)
4. Convert to a template: `pct template <vmid>` / `qm template <vmid>`
5. Record `(release, template_name, template_vmid, builder_ref, app_version)` in the state DB

---

## 6. Deploy pipeline (per project / per instance, via API)

1. Resolve target template VMID for the requested type + active release
2. **Clone**: `POST /nodes/{node}/{qemu|lxc}/{tmpl_vmid}/clone`
3. **Network identity**: set a fresh MAC on the clone via `POST .../config`
4. Record a **pending assignment** keyed by hostname in the state DB
5. **Start**: `POST .../status/start`
6. Guest firstboot service self-personalizes and pulls assignment from orchestrator
7. On firstboot callback success, mark instance `active` and report to platform

---

## 7. First-boot personalization contract

`ctrlable-firstboot.service` (oneshot) is baked into every Debian-based template.

1. `rm /etc/machine-id` → `systemd-machine-id-setup`
2. Regenerate SSH host keys
3. Confirm hostname
4. `GET /api/provision/assignment?hostname=<self>&secret=<release-secret>`
5. Apply service wiring (Z2M config, ZwaveJS settings, Frigate config, etc.), restart app
6. `POST /api/provision/complete`
7. `systemctl disable ctrlable-firstboot.service`

---

## 8. HAOS special case (`ctrlable-pro`)

1. Clone rebranded HAOS template via API; set fresh MAC + hostname
2. Start; HAOS runs its own onboarding
3. Apply Ctrlable base via HA backup restore
4. Service wiring + platform enrollment via HA REST/WebSocket API

---

## 9. Radio / coordinator strategy

Default: **network coordinators** (Ethernet/PoE for Zigbee, ser2net bridge for Z-Wave).
Fallback: USB passthrough requires manual per-host LXC config — flagged in UI as "needs binding".

---

## 10. State database (SQLite, per host)

See `backend/app/state.py` for full schema.

Tables: `releases`, `templates`, `projects`, `instances`, `builds`

---

## 11. Proxmox API surface

Token-scoped. Uses `proxmoxer` (Python).

| Purpose | Method / path |
|---|---|
| Host health | `GET /nodes/{node}/status` |
| Guest inventory | `GET /nodes/{node}/qemu`, `GET /nodes/{node}/lxc` |
| Clone template | `POST /nodes/{node}/{qemu\|lxc}/{tmpl}/clone` |
| Set MAC / hostname | `POST /nodes/{node}/{qemu\|lxc}/{vmid}/config` |
| Lifecycle | `POST .../status/{start\|stop\|reboot}` |

---

## 12. Host-trigger contract (build plane only)

- SSH key restricted via `command=` in `authorized_keys` to `ctrlable-build <release>`
- Wrapper lives on the host, fetches manifest from orchestrator, runs §5
- Triggered exclusively by "Build release" in UI

---

## 13. Web UI

FastAPI (backend) + React/Vite (frontend), served from the orchestrator LXC.

- **Dashboard**: host health (CPU/RAM/storage), guest cards grouped by project
- **Projects / Deploy**: stack deploy wizard, add-instance
- **Releases**: build status, per-template versions, trigger build

---

## 14. Security

- Proxmox API: scoped token (`CtrlableProvisioner` role)
- Host trigger: `command=`-restricted SSH key
- Firstboot secret: per-release, LAN+TLS only, rotated each release
- Secrets delivered via assignment callback — never baked into images

---

## 15. Repo layout

```
ctrlable-provisioner/
├── SPEC.md
├── install.sh                  # M0: bootstrap — creates orchestrator LXC on bare PVE host
├── releases/
│   └── 2026.06.yaml
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   ├── config.py
│   │   ├── manifest.py
│   │   ├── proxmox.py
│   │   ├── deploy.py           # M5
│   │   ├── provision.py
│   │   ├── build.py
│   │   ├── state.py
│   │   └── platform.py         # M8
│   └── requirements.txt
├── frontend/
├── deploy/
│   └── ctrlable-provisioner.service
├── host/
│   ├── ctrlable-build
│   ├── ctrlable-build.conf.example
│   └── firstboot/
│       ├── ctrlable-firstboot.service
│       └── ctrlable-firstboot.sh
└── .env.example
```

---

## 16. Milestones

0. **Bootstrap installer** — `install.sh` creates the orchestrator LXC on a bare PVE host one-shot.
1. **Manifest + state DB** — schema, loader, validation. ✓
2. **Proxmox API layer** — read-only dashboard (host health + guest inventory). ✓
3. **Host trigger + build pipeline** — build `zwavejs` template end-to-end. ✓
4. **Firstboot contract** — machine-id/SSH/hostname reset + assignment callback. ✓ (smoke test pending)
5. **Deploy pipeline** — clone + MAC/hostname + start + firstboot → `active`, all LXC types.
6. **HAOS flow** — clone + onboarding + base-backup restore + platform enrollment.
7. **Full UI** — deploy wizard, add-instance, releases tab, lifecycle controls.
8. **Platform report-up** — installed release + instances in central management platform.

---

## 17. Open decisions

- **Frigate placement:** separate LXC vs. folded into `docker-portainer`
- **FreePBX kind:** LXC (default) vs. VM for stronger isolation
- **HAOS instance UUID:** regenerate per site vs. accept restored base UUID
- **Single host vs. cluster:** spec assumes single-node; cluster needs a pass on `{node}` resolution
