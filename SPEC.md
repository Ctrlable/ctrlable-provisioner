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

## 16. Component drift and self-update

### 16.1 Why

2026-11-07, Pinecrest. `zwave_js` had been unloaded for over a day, taking the
keymaster service-entrance lock with it. Three separate things had to be true:

1. Core 2026.11 pins `zwave_js_server` MIN == MAX == schema 51. Server 3.10.0
   advertises 50, so the integration does not load at all — there is no
   degraded mode. **Being behind is as breaking as being ahead.**
2. This manifest already knew the answer. § 4 pinned `11.25.2` and declared
   `requires.zwave_js_api_schema: 51`, naming Pinecrest and the keymaster lock
   in the comment. **Nothing ever compared that to what was running.**
3. The dealer's manual update failed, and would have failed forever: the
   `ct/zwave-js-ui.sh` copy INSIDE the container was months old and built its
   download URL by stripping the `v` off the tag. GitHub 404s, `wget &>
   /dev/null` swallows it, and the trap prints `exit code 0`. Upstream had
   already fixed this — current community scripts glob the asset via
   `fetch_and_deploy_gh_release` instead of reconstructing the filename — but
   **nothing refreshes the updater inside a container that already exists.**

The outage was found by a human noticing dead locks. Every site is exposed to
the same class of failure on every core release that moves a pinned schema.

### 16.2 Three capabilities, deliberately separate

| | What it answers | Where it runs | Failure if absent |
|---|---|---|---|
| **Probe** | what version is actually installed | orchestrator → `pct exec` | we only know intent, never reality |
| **Drift** | installed vs manifest pin and `requires` | orchestrator, on a timer | a site is broken and nobody knows |
| **Apply** | bring a guest to the pinned version | orchestrator → guest | we can see the problem and not fix it |

Keep them separate. Probe is read-only and safe to run often; apply restarts a
service a live site depends on and is never automatic (§ 16.5).

### 16.3 Probe: declared, not coded

Each template declares how to read its installed version, so adding a
component is a manifest edit rather than a code change:

```yaml
  zwavejs:
    app_version: "11.25.2"
    version_probe:
      kind: exec                       # exec | file | http
      command: "/opt/zwave-js-ui/zwave-js-ui-linux --version"
      parse: '([0-9]+\.[0-9]+\.[0-9]+)'
    requires:
      zwave_js_server: ">=3.11.0"
      zwave_js_api_schema: 51
```

`requires` keys are **capability assertions, not version strings**, and are
checked against the live service where one exposes them. For `zwavejs` the
schema is authoritative and the version is a proxy: connect to the Z-Wave JS
websocket and read `maxSchemaVersion` off the server's first frame. That is
the number Core actually rejects on, so it is the number to assert. A site can
be on the right version and still wrong (wrong binary, failed restart); the
schema probe catches that, a version string does not.

### 16.4 Refresh the updater — the part that actually bit us

A container carries the update script it was born with, forever. Before any
apply, re-fetch the component's current community script. This is also the
answer to "do we own the builders?": **no.** Upstream fixed the URL bug
structurally and maintains it for free (§ 16.7); what we were missing was
freshness, not ownership.

Two freshness questions, often confused, with different answers:

- **Build time** — which ct script a NEW guest is built from. Governed by
  `community_scripts_ref`. Pinned for reproducibility.
- **Update time** — which update script an EXISTING guest still carries.
  Governed by nothing today. Should track upstream, because that is where
  upstream's fixes live.

Note the standing hazard: pinned ct scripts fetch `build.func` from `main`
(§ 4). That mismatch is what broke every pinned script on 2026-10-31 when
`misc/` was deleted. Pinning is not free either.

### 16.5 Apply: gated by default

- **Dealer-initiated.** Surfaced as a row with a button, never a timer.
- **Gated crossings stay gated.** `gated_across: "2.0.0"` on `zigbee2mqtt`
  exists because crossing z2m 2.0 rewrites a working site's configuration.
  The monitor may flag it; applying it is a human's decision.
- **Snapshot first.** `POST .../snapshot` before apply, named for the drift
  row, so rollback is one call.
- **Verify after, by capability.** Re-run the probe AND the `requires`
  assertion. "The service started" is not success; "the schema is 51" is.

### 16.6 Provisioner self-update

The orchestrator is the one component that can update itself, and the only one
whose failure is silent — a stalled orchestrator keeps answering `/health`
while reporting drift for nobody.

- Poll a Ctrlable release channel for a provisioner version, not the fleet's
  component manifest. **Separate cadence from the appliance release.**
- Stage, verify, switch, and keep the previous version for one-command
  rollback. Never update while a build or apply is in flight.
- Report `provisioner_version` on the existing 60 s heartbeat (§ platform), so
  the portal can see a host that has stopped updating. A host that never
  checks in is the failure this is meant to catch; the heartbeat is the only
  thing that can distinguish "no drift" from "nothing is looking".

### 16.7 Three classes of component, three update paths

The catalog is NOT uniform, and a single "update" button would be wrong for
it. Of seven templates, only two run community `ct/` scripts:

| Class | Templates | Who fixes a broken installer | Update path |
|---|---|---|---|
| **Community `ct/`** | `zwavejs`, `zigbee2mqtt` | upstream, for free | refresh the in-guest updater (§ 16.4), then run it |
| **Ctrlable builder, third-party app** | `frigate`, `freepbx` | us — we already own these | re-run our builder at the pinned version |
| **Ctrlable product** | `hardware-manager`, `dali-bridge` | us | our own release channel, same cadence as the Store |
| **Appliance image** | `ctrlable-pro` | us | not a builder re-run at all — a new OS/Core image via rebrand-tools, offered through the version endpoint (§ 8) |

This matters for drift in two ways:

- **Only the community class has the stale-updater problem.** Our builders are
  fetched fresh from this repo at apply time, so they cannot carry a
  months-old bug the way a container-resident `ct/` script can. § 16.4 is
  scoped to `zwavejs` and `zigbee2mqtt`.
- **Only the community class floats at build time.** Our builders already take
  an explicit version, so `app_version` is enforced for five of seven
  templates today. The two that document rather than enforce it are exactly
  the two running community scripts — which is why `zwavejs` drifted two
  majors stale without anyone noticing.

A Ctrlable product being behind is also a different severity from a
third-party one: we control when it ships, so drift there means our own
release never landed, not that upstream moved.

`ctrlable-pro` is listed for completeness but is governed by § 8, not by this
section: it updates by being offered a new image, and the appliance decides.
Its drift row is still worth showing beside the others — "this site is three
Core releases behind" is the same question a dealer is asking — but the apply
button belongs to the version endpoint, not here.

### 16.8 Non-goals

- **Not in the Ctrlable Pro UI.** These guests are siblings of the HAOS VM,
  not something Home Assistant owns. Infrastructure updates belong in a
  dealer-facing surface; a homeowner clicking "update Z-Wave" takes their own
  locks offline.
- **Not owning the two remaining community builders.** `frigate`, `freepbx`,
  `hardware-manager` and `dali-bridge` already have Ctrlable builders; this
  says nothing about them. It says we should NOT take over
  `ct/zwave-js-ui.sh` and `ct/zigbee2mqtt.sh`: upstream maintains them for
  free and fixed this exact URL bug structurally (globbing the release asset
  instead of reconstructing its filename). Taking them on means owning that
  forever to buy nothing. If `app_version` must be enforced for those two, the
  cheaper lever is rewriting the `"latest"` argument in the fetched script via
  the existing `ctrlable-build` fetch wrappers — one token, not a builder.
- **Not automatic for gated crossings.** § 16.5.

---

## 17. Appliance reporter add-on

### 17.1 Why — the compatibility fact is split across two machines

Neither side currently holds both halves of the question § 16 exists to answer:

| | knows | does not know |
|---|---|---|
| appliance | what Core DEMANDS | what is actually running on the radios |
| provisioner | what is RUNNING | what this appliance's Core demands |
| manifest | a hand-copied `51` | whether that is still true |

`requires.zwave_js_api_schema: 51` was read off Core once, by a human, and
written down. It is correct today and silently wrong the day Core moves to 52
— the same "nobody noticed the number changed" failure as the outage itself,
one level up.

It does not have to be maintained, because **the appliance can report it**.
The constant ships in the `zwave-js-server-python` library that
`homeassistant/components/zwave_js/manifest.json` already requires, so it is
installed on every appliance. Read live from Pinecrest, 2026-11-07:

```
MIN_SERVER_SCHEMA_VERSION: 51
MAX_SERVER_SCHEMA_VERSION: 51      # equal — an equality, not a floor
```

An add-on turns `requires` from something we maintain into something we
observe. That is the point of it; the UI is a bonus.

### 17.2 Boundary: a reporter and a trigger, NEVER an actor

The add-on has no Proxmox token, no host access and no ability to change a
guest. It reports, it displays, and it ASKS. Every privileged action — the
snapshot, the apply, the gate (§ 16.5) — stays in the provisioner.

This is not only least-privilege hygiene. It is what makes the add-on
acceptable in the Home Assistant UI at all. § 16.8 rules that infrastructure
updates do not belong in the end-customer's interface, because a homeowner
clicking "update Z-Wave" takes their own locks offline. An add-on that only
REQUESTS does not put that button in the HA UI: the decision still happens in
a dealer-facing surface that owns the gating. Visibility moves to where the
dealer already is; authority does not move at all.

### 17.3 What it reports

A single document, pushed on change and on a slow heartbeat. The add-on is
the only thing that can observe most of this:

```jsonc
{
  "appliance_id":   "<machine id>",
  "core_version":   "2026.11.0",
  "requires": {                      // OBSERVED, not configured
    "zwave_js_api_schema": {"min": 51, "max": 51}
  },
  "integrations": [                  // WHICH instance is actually live
    {"domain": "zwave_js",       "endpoint": "ws://10.1.8.14:3000", "loaded": true},
    {"domain": "ctrlable_zigbee", "transport": "mqtt", "loaded": true}
  ],
  "addons": [                        // including STOPPED ones, and said so
    {"slug": "45df7312_zigbee2mqtt", "version": "2.12.1-1", "state": "stopped"}
  ]
}
```

`integrations[].endpoint` is the field that matters most and the one nothing
else can supply. On 2026-11-07 this appliance ran a STOPPED Zigbee2MQTT
add-on at 2.12.1-1 beside a live instance at 2.14.3, and a Z-Wave JS add-on
that Core does not use at all — its `zwave_js` entry points at an LXC. A
survey that trusted the add-on list would have reported drift that did not
exist and missed the component that was actually broken. The appliance is the
only party that knows which instance Core is talking to.

`addons[]` is reported WITH state, never filtered. A stopped add-on is a fact
about the site, not noise — see § 17.6.

### 17.4 Direction, discovery and trust

- **The appliance initiates.** It sits behind the site's NAT, it is the party
  that knows when its own Core changed, and an outbound call needs no inbound
  path. The provisioner never dials the appliance.
- **Discovery is configured, with a fallback.** The provisioner's address is
  set at deploy time, because the appliance is deployed BY the provisioner and
  the value is known then. mDNS is a convenience for a re-homed appliance, not
  the primary path — a monitoring channel that depends on multicast is a
  monitoring channel that disappears on a VLAN boundary.
- **Trust is per-appliance and issued, not shared.** The firstboot secret
  (§ 7) is per-release and proves "you are one of this release's guests",
  which is the wrong grain: a reporter speaks for ONE appliance for its whole
  life. Issue a token when the instance is created.
- **Reports are data, never instructions.** A report sets no versions and
  starts no actions; the provisioner compares it against the manifest and
  decides. A compromised appliance can lie about itself and nothing else.

### 17.5 What the dealer sees

The provisioner's drift row, rendered in the add-on's ingress panel, with the
three columns that mean three different things:

```
zwavejs      live 11.25.2   pinned 11.25.2   upstream 11.25.2   schema 51   OK
zigbee2mqtt  live  2.14.3   pinned  2.14.3   upstream  2.14.3               OK
```

*is it working* / *is it what we intended* / *has the world moved*. An
"Update" button posts a request; the provisioner applies the § 16.5 rules and
may refuse (gated crossing, build in flight, no snapshot).

### 17.6 What this replaces

Today the appliance carries Z-Wave JS UI and Zigbee2MQTT add-ons installed
but STOPPED, as version beacons: Supervisor tracks `version` vs
`version_latest` for any installed add-on, so a stopped one still surfaces
"update available" in the HA UI. It is a reasonable hack and was the only
signal available.

It answers a different question from the one that matters. The beacon
compares the ADD-ON's frozen version against upstream's latest; it never
looks at the running instance. On 2026-11-07 it would have reported "Z-Wave
JS: update available" — true, and equally true for months — while the actual
fault was that the LIVE server's schema had become incompatible with Core.
Updating the add-on would not have fixed it, and the fix did not change the
beacon.

So the beacon can be retired once this ships, and `addons[]` keeps reporting
it in the meantime: a stopped add-on at 2.12.1-1 beside a live 2.14.3 is
exactly the kind of thing a dealer should be able to see.

### 17.7 Non-goals

- **Not an actor.** § 17.2. No Proxmox access, no guest changes, no applying
  its own updates.
- **Not the gate.** The provisioner enforces `gated_across` and snapshots;
  the add-on cannot override either, and a request is not an authorization.
- **Not required.** Drift must still work for an appliance without it, using
  the manifest's declared `requires`. The add-on makes the requirement
  observed instead of maintained; it does not become a dependency for
  monitoring to function at all.

---

## 18. Milestones

0. **Bootstrap installer** — `install.sh` creates the orchestrator LXC on a bare PVE host one-shot.
1. **Manifest + state DB** — schema, loader, validation. ✓
2. **Proxmox API layer** — read-only dashboard (host health + guest inventory). ✓
3. **Host trigger + build pipeline** — build `zwavejs` template end-to-end. ✓
4. **Firstboot contract** — machine-id/SSH/hostname reset + assignment callback. ✓ (smoke test pending)
5. **Deploy pipeline** — clone + MAC/hostname + start + firstboot → `active`, all LXC types.
6. **HAOS flow** — clone + onboarding + base-backup restore + platform enrollment.
7. **Full UI** — deploy wizard, add-instance, releases tab, lifecycle controls.
8. **Platform report-up** — installed release + instances in central management platform.
9. **Drift monitor** (§ 16) — probe + compare against manifest pins, surfaced in the UI. ✓
   `drift_scan.scan_project` joins `drift.evaluate` + `addressing.resolve` across a
   project's instances; `GET /api/projects/{id}/drift` returns a row each (severity,
   live/pinned/schema, where), rendered as the § 17.5 panel per project card. The
   **schema** capability — the one that unloaded `zwave_js` — is read live over TCP
   and fully determinable. The **version** dimension needs an in-guest exec transport
   (`version_probe`, kind: exec) that is NOT wired to the restricted host key yet
   (§ 18), so it is injected into the scanner and reports UNKNOWN rather than a false
   OK until M10 lands it. Transport is injected, so the scan is tested with no PVE
   host and no live socket (`tests/test_drift_scan.py`).
10. **Apply + updater refresh** (§ 16.4–16.5) — gated, snapshot-first, verified by capability.
11. **Provisioner self-update** (§ 16.6) — staged, rollback-capable, reported on the heartbeat.
12. **Appliance reporter add-on** (§ 17) — observed `requires`, live endpoints, drift panel, apply requests.

---

## 19. Open decisions

- **Frigate placement:** separate LXC vs. folded into `docker-portainer`
- **FreePBX kind:** LXC (default) vs. VM for stronger isolation
- **HAOS instance UUID:** regenerate per site vs. accept restored base UUID
- **Single host vs. cluster:** spec assumes single-node; cluster needs a pass on `{node}` resolution
- **Update-time script freshness** (§ 16.4): always track upstream, or pin and accept that upstream's fixes never reach existing guests?
- **Probe transport:** `pct exec` from the orchestrator (simple, needs host trigger for LXC) vs. an agent inside each guest (no trigger, one more thing to keep alive).
- **Who owns `requires` assertions** that need a live protocol handshake, like the Z-Wave schema read — the orchestrator, or the component's own health endpoint?
- **Reporter token lifecycle** (§ 17.4): issued at instance creation — but what re-issues it after a restore-from-backup, which brings back an appliance's identity along with a token that may have been revoked?

---

## 20. Internal management LAN (`vmbr1`)

Inter-service wiring must survive the site LAN changing its DHCP range, or the
whole appliance being moved to a different site. Not hypothetical: the
174.48.139.185 appliance was built on a 172.16.x LAN and shipped to a 10.0.0.x
one, and zigbee2mqtt had been pointed at the HA broker by its then-current DHCP
address.

Every guest joins a private bridge `vmbr1` on a **fixed** address, and the
inter-service wiring uses those, never DHCP ones. The manifest's
`network.internal` block is the single source of truth (`subnet`, `bridge`,
`assignments` = host octets within `subnet`, so a collision fallback to another
/24 re-maps every address for free). `app/internal_net.py` resolves a template
to its address (`ip_for`), gives the broker host (`mqtt_host`, the HA VM), and
checks a subnet against what is already on the host (`overlaps` / `choose_subnet`).

**Subnet choice.** `vmbr1` is isolated L2, so the only collision that matters is
with another network on the SAME host. `198.18.0.0/24` is RFC 2544 benchmarking
space: never a site LAN (10/172.16/192.168), never Tailscale (100.64/10), never
the WireGuard fabric (10.10.16.x / 10.20.x). The bootstrap still checks it
against the host's live interfaces/routes and refuses (or falls back) on overlap,
so a surprise overlay cannot make it collide silently.

**Address mechanism.** LXCs take a static `net1` (`ip_for(template)/24`), which
`addressing.resolve` already reads (§ 16). The HAOS VM cannot take a static IP
from Proxmox (no cloud-init), so it gets `net1` on `vmbr1` and DHCP-reserves its
address off a host **dnsmasq** keyed by the MAC ctrlable-build assigns.

**Phasing.** Phase 1 (done): the manifest block, `internal_net.py`, and the
orchestrator deriving `mqtt_host` from the internal map in `deploy.py`
(`_with_internal_net`; an explicitly-supplied `mqtt_host` still wins), with
`tests/test_internal_net.py`. Phase 2 (host side): `install.sh` creates `vmbr1` +
dnsmasq with the collision check, and `ctrlable-build` adds each guest's `net1` +
the VM's dnsmasq reservation.
