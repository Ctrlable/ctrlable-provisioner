# Ctrlable Provisioner — Incus Migration SPEC (Proxmox → plain Debian + Incus)

> **Status:** DRAFT / exploratory. This is a companion to `SPEC.md` (which describes
> the current Proxmox-based system). Nothing here is committed to a schedule; the
> purpose is to decide *whether* to migrate, scope the work, and define a PoC that
> retires the two real unknowns before anyone commits.

## 1. Goal & non-goals

**Goal:** run the Ctrlable appliance on a **plain Debian host + Incus**, with our own
provisioner web UI as the only control surface — removing the dependency on Proxmox VE
(its enterprise-repo friction, its release coupling, and the `pct`/`qm`/`pvesh` glue we
currently shell out to) while keeping everything an operator actually sees: one-click
deploy, the service catalog, the internal LAN, and Ctrlable Pro with its official add-on
store and our OTA.

**Non-goals:** clustering / HA failover (we don't use Proxmox's today); changing the
manifest/release model; changing the firstboot personalization contract; re-theming the UI.

## 2. Why Incus specifically

Incus (the Debian-native LXD fork) is the only option that replaces **both** things we use
Proxmox for under **one** programmatic API: **system containers** (full-systemd LXC, exactly
our service model) **and** **KVM/QEMU VMs** (Ctrlable Pro). libvirt+native-lxc splits this
across two weaker toolchains; Docker/Podman don't give us system containers at all. Incus's
REST API fits our "own web UI" model far better than scraping `pct`/`qm`/`pvesh`.

## 3. What we depend on Proxmox for today (coupling inventory)

Measured against the current tree (`host/ctrlable-build`, `backend/app`):

| Area | Proxmox surface | Rough size |
|---|---|---|
| LXC lifecycle/exec | `pct create/exec/push/config/set/list/template` | ~18 call sites |
| VM (Ctrlable Pro) | `qm create/clone/config/start/template`, `build_vm`, `clone_vm` | ~16 call sites |
| Control/monitor plane | backend Proxmox **API client** (`:8006` + scoped token): inventory, start/stop/reboot, metrics | 1 client module |
| **Template builder** | **`community-scripts/ProxmoxVE` `ct/*.sh`** + the pin-trap wrappers + `build.func` | pervasive — the long pole |
| Networking | `vmbrN` Linux bridges + dnsmasq (incl. the internal-LAN §20 work) | ~28 refs |
| Storage | `local-lvm` (container rootfs), `local` (templates), `pvesm` | handful |
| Image path | HAOS `.img.xz` import → `qm` VM | the riskiest unknown |

The `pct`/`qm` call sites are **mostly mechanical 1:1 swaps**. The builder ecosystem and the
HAOS VM are where the real work and risk live.

## 4. Target architecture

```
  Debian 13 (trixie) host  — image-based A/B root (§14), one persistent Incus data pool
    └─ Incus (system containers; QEMU VMs only as a documented exception)
         ├─ ctrlable-orchestrator      (system container — our provisioner, this repo)
         ├─ ctrlable-pro               (system container — rebranded HA Supervised; add-on store + our OTA)
         ├─ zwavejs / zigbee2mqtt / …  (system containers — services)
         └─ incusbr-site / incusbr-internal  (networks; or adopt existing vmbr0/vmbr1)
  Provisioner UI/API  ──Incus REST API (unix socket / https+token)──▶ host

**Design intent (decided):** run everything — Ctrlable Pro included — as a **system
container (LXC)**, and avoid VMs wherever we can. A QEMU VM is now the *exception* (a
specific site whose radios are network-attached, see §6/§13), not the default.
```

The orchestrator talks to the **Incus API** instead of `pct`/`qm`/`pvesh`. The manifest,
release, deploy-once/clone-many, firstboot, and internal-LAN concepts all survive; only the
*host driver* underneath changes.

## 5. Proxmox → Incus mapping

| Proxmox | Incus |
|---|---|
| `pct exec <id> -- cmd` | `incus exec <name> -- cmd` |
| `pct push <id> src dst` | `incus file push src <name>/dst` |
| `pct set <id> -net1 name=eth1,bridge=vmbr1,ip=…` | `incus config device add <name> eth1 nic nictype=bridged parent=vmbr1` (+ static IP via cloud-init / inside) |
| `pct create` from template | `incus launch <image> <name> --profile …` |
| `pct template <id>` | `incus publish <name> --alias <image>` (local image) |
| `qm` VM + `.img.xz` import | `incus init <name> --vm --empty` + import disk, or an Incus VM image |
| Proxmox API: list/start/stop/reboot/metrics | Incus API: `GET /1.0/instances`, `PUT …/state`, metrics endpoint |
| `local-lvm` / `local` storage | Incus storage pool (zfs/btrfs/lvm/dir) |
| `vmbr0`/`vmbr1` + dnsmasq | adopt existing bridges via `nictype=bridged`, **or** Incus-managed networks (Incus runs its own dnsmasq) |

**Build-token/firstboot/call-home**: unchanged — the orchestrator still serves the manifest
and bakes `firstboot.conf`; only the push mechanism changes (`pct push` → `incus file push`).
The self-heal `ORCHESTRATOR_URL` auto-detect (commit `363ee62`) carries over as-is.

## 6. DECISION POINT — Ctrlable Pro: VM vs Supervised-in-LXC

The add-on store requires **Supervisor**, which exists only in **HAOS** or **Supervised**.

- **Option A — keep HAOS as an Incus VM.** Preserves the add-on store, our
  **Supervisor fork**, **RAUC OS OTA** (rebrand-tools / Core+OS tiles), the supported/healthy
  status, and the clean `.img` deploy. Cost: a VM (RAM/boot overhead) and the HAOS-on-Incus
  image work (the PoC's job). **Lowest risk; keeps the fleet OTA intact.**
- **Option B — Ctrlable Pro as Supervised-in-LXC.** Run HA **Supervised** (Supervisor +
  Docker) in a container with `security.nesting=true` (privileged or idmapped). Keeps the add-on
  store, drops the VM, and makes **USB coordinator passthrough trivial** (host `/dev/serial/by-id`
  bind vs a virtualized VM controller — see the USB note below). The usual objections are
  **smaller than they first look**:
    - *"Officially unsupported" banner* — **ours to remove.** We ship our **own Supervisor fork**
      and **bake our own images**, so *we* define "supported"; the unhealthy banner and the
      supported-system checks are under our control, not Home Assistant's.
    - *"Loses RAUC OS-OTA"* — **loses nothing stack-wide.** RAUC only ever covered the HA
      appliance (§14), never the host. An **LXC snapshot before each update** gives that one
      component the *same* atomic rollback, and Supervisor still updates Core/add-ons.
  The **real** residual cost: we now own keeping the **Debian+Docker base matched to what
  Supervisor expects** across HA releases (OS-agent, Docker version, AppArmor profile) — the
  compatibility HAOS otherwise guarantees — plus Docker-in-LXC's AppArmor/device edge cases.
- **Option C — HA Container / Core in an LXC.** Lightest, but **loses the add-on store** →
  off the table given the requirement.

**USB coordinators tilt this.** Today every site is **USB-radio only** (network-attached
coordinators come later, per site/budget). USB **passthrough to a VM** is the fragile path we
want to avoid — virtualized controller, device re-enumeration, the stick vanishing across
reboots/migrations (our own USB-enumeration history is a monument to this). USB **device
passthrough to an LXC** is just the host kernel exposing `/dev/serial/by-id/*` into the
container (cgroup allow + bind): shared kernel, stable `by-id` names, no re-attach dance — so
Option B's host is genuinely better *for a USB-stick site* — and the usual objections to B are
weaker than they appear (the banner is ours to remove; an LXC snapshot before update ≈ RAUC for the
HA component — see Option B above), so for the **current USB-only fleet the balance now leans B**.
The clean escape that keeps Option A **and** kills the USB problem is **network-attached
coordinators** (TCP/PoE Zigbee, Z-Wave-over-IP, ser2net) — no passthrough of any kind — which is
why A regains the edge as sites move that way. See §13; host-OS update safety is separate (§14).

**Decision (2026-11-17): Option B (Supervised-in-LXC) is the default — avoid VMs.**
Ctrlable Pro runs as a **system container**, not a VM. The reasons above hold for the
whole current fleet (USB-only): trivial `/dev/serial/by-id` passthrough, per-component
rollback via LXC snapshots, a "supported" status we control, and no VM RAM/boot overhead.
Beyond those, a **single-container-model host is simpler to provision, snapshot, back up,
and move** than one that mixes containers and a VM disk image — which is the direction we
want for an automated appliance build.

> **PoC-validated 2026-11-17** (`SPEC-incus-poc.md` §3): HA Supervised came up in an
> **unprivileged** LXC on Debian 13 — Supervisor + a **97-add-on store** + Core on `:8123`,
> no privileged container, no AppArmor disable. The only caveats are the `supported:false`
> banner (ours to flip via the Supervisor fork) and three small container accommodations
> (Debian-13 base, a host `kernel.dmesg_restrict` sysctl, a benign cgroup-v1 warning). The
> "own the Debian+Docker base compat" cost is therefore **real but small**.

**Option A (HAOS VM) is retained only as a documented exception** for a site whose radios
are **network-attached** (no USB to pass through), where the clean `.img` deploy and intact
in-image RAUC are a genuine win and the VM cost is justified. Even there, prefer B unless
that specific win applies. The PoC still boots A once to keep the escape hatch proven, but
B is what the bootstrap builds by default. (C is excluded by the store req.) **Host-OS
update safety is a separate, higher-value axis — see §14; the host's own A/B is where the
"A/B model" lives now, since Ctrlable Pro no longer carries HAOS's A/B.**

## 7. The builder problem (the long pole)

`community-scripts/ProxmoxVE` recipes are Proxmox-native (`pct`, Proxmox storage, whiptail) and
**do not run on Incus**. We must own a **build recipe per service** (zwavejs, zigbee2mqtt,
frigate, freepbx, hardware-manager, dali-bridge). Approach: distro-agnostic install scripts run
inside a base Incus container, then `incus publish` to a local image — reusing the *pinned app
version + `var_*`* intent we already encode, minus the Proxmox plumbing. This is new code **and**
permanent maintenance we currently get upstream for free — the single biggest cost line.

### 7a. Service inventory & porting difficulty (so P2's scope is explicit)

The PoC built **one** service (zwavejs) to prove the path + size this cost — not the catalog.
What actually has to be ported, and how hard each is:

| Service | Source today | Hardware / nesting | Incus difficulty | PoC status |
|---|---|---|---|---|
| zwavejs (zwave-js-ui) | community-scripts | USB coordinator | **easy** (static binary) | ✅ built + deployed |
| zigbee2mqtt (z2m) | community-scripts | **USB coordinator** | easy–moderate | path ✅; **USB passthrough ✅** (SONOFF MG24, B3) |
| mosquitto / MQTT | community-scripts | none | easy | follows the recipe |
| matter-server | community-scripts | none (host net) | easy–moderate | follows the recipe |
| go2rtc | community-scripts | none | easy | follows the recipe |
| dali-bridge | `host/builders/dali-bridge.sh` | USB (hasseb DALI) | moderate | USB path ✅ (B3-style) |
| hardware-manager | `host/builders/hardware-manager.sh` | host introspection | moderate | — |
| freepbx | `host/builders/freepbx.sh` | SIP/RTP networking | moderate–hard | — (networking quirks) |
| **frigate** | `host/builders/frigate-docker.sh` | **privileged + Docker-in-LXC + `/dev/dri` iGPU VAAPI** | **hard** | ✅ VAAPI **H264 dec+enc** in a *privileged* Incus CT (render node + i965) |
| Ctrlable Pro | HAOS → Supervised-LXC | Docker-in-LXC + USB | hardest overall | ✅ Part B (store + Core) |

**Net:** the zwavejs-likes all share one recipe shape (proven) and z2m's scary part — USB
coordinator passthrough — is already retired by the B3 test. The genuine remaining service-level
risk is **frigate**: iGPU `/dev/dri` passthrough + VAAPI transcode inside an Incus container,
which, like Supervised, is where Incus semantics could fight. That is why it gets its own PoC
item rather than being assumed mechanical. freepbx's SIP/RTP is a lesser, networking-shaped risk.

**Frigate PoC result (2026-11-17), the one surprise worth pinning:** iGPU VAAPI transcode works
in an Incus container — the box's Braswell N3700 render node passed through, `vainfo` enumerating
H264 `VAEntrypointVLD`+`VAEntrypointEncSlice` both directly and from the real frigate image. But it
**requires a *privileged* container**: the unprivileged `/dev/dri` node mount fails at Incus's
forkmount layer, where privileged succeeds (Supervised, by contrast, ran unprivileged). This matches
`frigate-docker.sh`'s existing privileged model, and only the **render node** (`renderD128`) is
needed — `card0` is display-only and its mount is what broke the `gpu`-device attempt. So the
service-container split is: **everything unprivileged except frigate, which is privileged for the
GPU.** Driver is **i965** on this Gen8 Braswell (iHD is Broadwell+), selected the same way the
builder already probes.

## 8. Networking

Adopt the existing `vmbr0` (site/real LAN) and `vmbr1` (internal `198.18.0.0/24`, SPEC §20)
Linux bridges via `nictype=bridged, parent=vmbrN` so the internal-LAN work we just shipped is
preserved verbatim (host still `.1`, dnsmasq still ours). Alternatively move to Incus-managed
networks (Incus runs dnsmasq) — more "native" but re-plumbs the internal LAN; **prefer adopting
the existing bridges** to minimize churn and keep the §20 guarantees.

## 9. Storage & backup

Incus storage pool (zfs or btrfs recommended for snapshots/send). We inherit backup/restore
ourselves (Proxmox's backup UI goes away): snapshots via `incus snapshot`, export via
`incus export`, scheduled off-box copy is **our** responsibility to wire — net-new work.

## 10. Risks & unknowns

1. **HAOS-on-Incus VM** — image import, OVMF/secureboot, boot order, our rebrand/firstboot
   pipeline. *The* unknown; PoC item #2.
2. **Builder re-implementation** — effort + ongoing maintenance; PoC item #1 sizes it.
3. **Field support** — Incus less familiar than Proxmox for debugging a sick box (mitigated by
   our UI, but real).
4. **Blast radius** — this is the control plane for live customer appliances. Migrate per-box,
   never in place; keep a Proxmox rollback until Incus is proven on that box.

## 11. PoC (do this BEFORE committing — answers "how much work / how stable")

Spare Debian box, time-boxed. Success = both land cleanly:

1. **Service container path.** Incus on Debian → build + deploy **one** service LXC (zwavejs)
   with our firstboot + attach it to a `vmbr1`-style internal network. *Proves the container
   path and sizes the builder work.*
2. **Ctrlable Pro VM path (Option A).** Boot **HAOS as an Incus VM** from our `.img`, confirm
   Supervisor + add-on store + our rebrand come up. *Retires the single biggest unknown.*

If #2 fights us, that materially changes the plan (and reopens Option B as a fallback).

## 12. Phased migration (only if the PoC passes)

- **P0 — host-driver seam.** Put a `HostDriver` interface in front of every `pct`/`qm`/`pvesh`
  call (Proxmox impl today), so the codebase compiles against one abstraction. *No behavior
  change; makes the rest incremental.*
- **P1 — Incus driver: containers.** Implement `HostDriver` on the Incus API for service LXCs
  (exec/push/config/device/deploy). Internal-LAN device wiring included.
- **P2 — Builders.** Incus-native build recipe per service; `incus publish` to local images.
- **P3 — Ctrlable Pro.** HAOS as an Incus VM **or** Supervised-in-LXC per the §6/§13 outcome; carry over rebrand + firstboot (+ RAUC OTA if VM, LXC-snapshot rollback if LXC).
- **P4 — Networking & storage.** Adopt `vmbr0/1`; storage pool; backup/restore.
- **P5 — Bootstrap.** `bootstrap-appliance.sh` installs Incus on plain Debian instead of
  standing up Proxmox. Per-box cutover with Proxmox rollback retained.

## 13. Radio coordinators & service topology (what actually drives the host choice)

Two decisions people conflate are independent; the migration spec keeps them apart:

**(a) Coordinator transport — the real driver.**

| Sites are… | Best host | Keeps / gives up |
|---|---|---|
| **network-attached** coordinators (future, per site/budget) | **HAOS VM (A)** | keeps store + atomic A/B RAUC OTA; **no USB passthrough at all** |
| **USB sticks, kept local** (every site *today*) | **Supervised-LXC (B)** — now the leaning choice | trivial USB device passthrough; supported-banner is **ours to remove** (own Supervisor+images); per-component rollback via **LXC snapshot ≈ RAUC**; cost = owning the Debian+Docker base compat |

The deciding question is *not* VM-vs-LXC in the abstract — it's **where the radio lives**. As sites
move to network coordinators the USB argument for B evaporates and A wins outright. The PoC should
therefore test **both** Option A (HAOS VM) **and** an Option-B-alt (Supervised-LXC with a real USB
coordinator passed in) so we can measure the USB path on each before committing per-site.

**(b) Service topology — consolidated vs distributed.** Independent of host: z2m and zwavejs can run
as **HA add-ons co-located inside Ctrlable Pro** (one box to patch, radios adjacent to HA) *or* as
**separate service containers** (today's model; blast-radius isolation, independent restarts). Add-on
co-location already works in the current HAOS **VM** — it is **not** an LXC-only capability; the only
thing LXC specifically buys is the easier USB passthrough in (a). Consolidation trades isolation for
simpler maintenance; it does not require choosing Option B.

> **Net (decided §6):** **Option B (Supervised-LXC) is the default** — Ctrlable Pro is a container, no VM.
> It fits the whole USB-only fleet (trivial USB, snapshot ≈ RAUC per-component, a "supported" status we
> control) **and** keeps the host a uniform all-container box that is simpler to provision/snapshot/move.
> **Option A (HAOS VM)** is kept only as a per-site exception for **network-attached coordinators** (clean
> `.img`, intact in-image RAUC, zero passthrough). **Host-OS atomic rollback (§14) is now where the A/B
> model lives — the higher-value prize either way.**

## 14. Host OS update safety (atomic rollback) — orthogonal, highest-value

RAUC's A/B only ever protected the **HA appliance**, never the host. The host OS has **no atomic
rollback today on Proxmox** (plain `pve`/`apt`; vzdump guards guests only), and **most appliances
ship without IPMI/BMC** (cost), so a bad host `apt dist-upgrade` on a fielded box is a **truck roll
right now**. This gap is **independent of VM-vs-LXC** and exists before *and* after the migration —
closing it is arguably a bigger reliability win than the host choice itself.

**What we'd add (a spectrum, cheapest → closest-to-RAUC):**
- **btrfs + snapper + grub-btrfs** — snapshot-on-`apt`, boot a known-good snapshot from GRUB.
  Moderate effort; boot-selectable recovery.
- **root-on-ZFS + zfsbootmenu** — same idea via ZFS boot environments.
- **image-based A/B Debian** (mkosi + systemd-boot + verity, or RAUC proper on a purpose-built
  image) — closest to true RAUC; largest effort (you are effectively building an appliance OS).

**The part that actually saves a no-IPMI box — automatic rollback on a failed boot — needs
boot-counting integration in *all* of these; it is not free in the snapshot-only variants.**

**Honesty bound:** this protects against **bad deliberate updates** only. It does **not** address the
runtime wedges in our history (memory-reclaim livelock, USB-URB storms) — those are not update
failures and need watchdog/kdump, not A/B. Do not let host A/B be sold as a cure for boxes going
unreachable in general.

### 14a. Disk & partition layout — disposable OS, one persistent data pool

Now that Ctrlable Pro is a **container** (§6) and no longer ships HAOS's own A/B, the A/B
model moves **to the Debian host**. The layout that makes an appliance easy to **update,
snapshot, back up, and move** separates two things that today live on one root:

```
  /dev/sdX
   ├─ ESP (FAT32, ~512M)            boot loader + both kernels
   ├─ root-A  (OS slot A)   ─┐      the Debian host OS — disposable, swappable
   ├─ root-B  (OS slot B)   ─┘      (image-based A/B, or snapshot pair)
   └─ data    (the rest)           ONE persistent pool: the Incus storage pool
                                    (all containers incl. ctrlable-pro + their
                                    /config, service data, snapshots)
```

- **The host OS is disposable.** An update writes the *inactive* slot and flips a boot
  pointer; a failed boot falls back to the known-good slot (boot-counting, §14). Nothing
  an operator cares about lives on the OS slot, so re-imaging it is safe.
- **All state lives in the `data` pool**, which is **never touched by an OS update**. This
  is the single artifact to back up and the single artifact to move: `incus export` of each
  instance, or a pool-level `btrfs send` / `zfs send` snapshot stream, carries the *entire*
  appliance (Ctrlable Pro + every service, with their data) to another box regardless of
  the host OS version on either side. "Move the appliance" becomes "move the data pool."
- **Why this beats HAOS-style A/B for the whole disk:** HAOS keeps OS *and* `/config` on
  the same A/B image, so a restore couples OS version to data. Splitting them lets us roll
  the OS back and forward independently of the data, which is exactly what multi-service
  appliance migration needs.

**DECIDED 2026-11-17 — image-based A/B via RAUC-for-host.** We build a purpose-built Debian
appliance OS image with RAUC A/B slots, the same way we already build the HAOS `ctrlable-pro`
image (rebrand-tools pipeline, RAUC OTA) — so this **reuses existing RAUC/image muscle** rather
than inventing a new rollback scheme. The host root becomes immutable and swappable; the
`data`/Incus pool is a separate btrfs filesystem the OS image never touches. (We considered a
btrfs-subvolume `@`/`@_prev` + grub-btrfs approach as a ship-fast alternative; rejected in favor
of true image A/B because it matches our pipeline and gives real slot-level rollback, not just a
GRUB snapshot pick.)

### 14b. RAUC-for-host build stack (x86 Debian) — concrete shape

```
  Build pipeline (CI, mirrors rebrand-tools HAOS build):
    mmdebstrap/mkosi → sealed Debian rootfs (Incus + orchestrator key baked in)
      → RAUC bundle (.raucb, signed) → OTA via our existing release channel
  On-box slots:
    ESP (FAT32) ──GRUB + grubenv (RAUC `grub` backend: ORDER + boot-try counter)
    root-A / root-B  ── RAUC writes the inactive slot, flips grubenv, reboots
    data (btrfs)     ── Incus pool; untouched by slot writes; holds all state
```

- **Bootloader slot-select backend is the one real wrinkle.** RAUC on x86 Debian typically
  drives **GRUB via `grubenv`** (`rauc` flips `ORDER`/`_TRY`, GRUB counts boot attempts and
  falls back) — this is the proven path and gives the **automatic rollback on failed boot** a
  no-IPMI box needs. The alternative is a systemd-boot/`bootctl` backend. **Leaning GRUB+grubenv**
  (best-documented RAUC x86 story, boot-counting built in). This is the next sub-decision (§15).
- **Reuse, don't reinvent:** the RAUC key/signing, the bundle format, and the OTA delivery
  channel already exist for the HAOS appliance — the host bundle is a *second* RAUC target on the
  same machinery, not a parallel system.
- **First-boot contract:** the image bakes the orchestrator pubkey into `/root/.ssh/authorized_keys`
  and ships Incus pre-installed; first boot creates/attaches the `data` btrfs Incus pool if absent,
  then registers with the orchestrator — **zero hand-typed setup**, which is the whole point.
- **The `data`/Incus pool being a separate filesystem from the OS is the non-negotiable invariant**
  — it is what lets an OS slot be wiped/rolled without touching appliance state, and what makes
  "move the appliance" = "`btrfs send` the data pool."

### 14c. Host identity / branding (GRUB + console + SSH)

A fielded Ctrlable appliance should *look* like a Ctrlable appliance everywhere an operator or
field tech lands, not like a stock Debian box. In scope for the image build / first-boot
(implemented in `poc/branding/apply-branding.sh`, idempotent):

- **GRUB** — `GRUB_DISTRIBUTOR="Ctrlable Pro"` so the boot menu entries read *Ctrlable Pro
  GNU/Linux* (plus room for a GRUB theme/background later). Applied only where grub exists.
- **Console (getty) `/etc/issue`** — a Ctrlable ASCII banner shown at the local login prompt.
  Uses a `#`-block art because getty runs backslash escapes (`\n`=host, `\l`=tty, `\m`, `\t`) —
  a slash-based art would be mangled; the `#` art is escape-safe, with the dynamic host/tty line
  appended after it.
- **SSH** — pre-auth `Banner /etc/ctrlable/banner` (via an `sshd_config.d` drop-in, validated with
  `sshd -t` before any reload so a bad edit can never lock the box out) **and** a post-login
  `/etc/motd`, both carrying the Ctrlable ASCII.
- **OS identity** — `PRETTY_NAME` in `/etc/os-release` set to "Ctrlable Pro (Debian 13)".

Note this is **host** branding, distinct from the Ctrlable Pro *application* rebrand (the HA
frontend "Home Assistant"→"Ctrlable Pro" work) and from flipping Supervisor's supported/healthy
banner — those live in the app layer / Supervisor fork, not here.

## 15. Open decisions

- [x] **DECIDED 2026-11-17 — Ctrlable Pro host: Option B (Supervised-in-LXC), avoid VMs.** Container by default; Option A (HAOS VM) retained only as a per-site exception for network-attached-coordinator sites (§6, §13).
- [x] **DECIDED 2026-11-17 — disk layout: disposable OS slot(s) + ONE persistent Incus data pool on a separate filesystem (§14a).** The data pool is the single backup/move artifact and is never touched by an OS update.
- [x] **DECIDED 2026-11-17 — OS A/B mechanism: image-based A/B via RAUC-for-host (§14a/§14b)**, reusing the HAOS/rebrand-tools RAUC+image pipeline. btrfs-subvolume alternative rejected.
- [ ] **OPEN — RAUC bootloader slot-select backend (§14b): GRUB + grubenv** (leaning; best x86 RAUC story + built-in boot-counting auto-rollback) **vs systemd-boot/`bootctl`**.
- [x] **DECIDED 2026-11-17 — data-pool / storage backend: btrfs** (in-tree, no out-of-tree DKMS to break on a fielded no-IPMI box; native `send`/`receive` for off-box backup/move).
- [ ] Per-site: USB-local (B, the default) vs **network coordinators** (the only case that re-opens A) — drives the per-site exception (§13).
- [ ] Networks: **adopt existing `vmbrN`** vs Incus-managed networks.
- [ ] Builders: fork/adapt community-scripts logic vs write clean distro-agnostic recipes.
- [ ] Target Debian base: 13 (trixie) + Incus from Debian repo vs Zabbly packages.
- [ ] **Management UI (§16): build-into-our-orchestrator vs adopt a generic Incus tool (Incus UI / LXConsole) vs hybrid.** Constraint: provisioning/fleet/OTA/Ctrlable flows have no off-the-shelf equivalent — generic tools can only own day-2 instance management. Evaluate hands-on against the PoC box (P6).

## 16. Provisioner / orchestrator web UI — management roadmap

Today the provisioner UI (`frontend/`, `backend/`) is largely a **provisioning/deploy** surface. On
Incus we lose the Proxmox web console entirely, so the orchestrator UI must grow into the day-2
**management** surface operators would otherwise get from Proxmox. Scope to add (post-migration,
tracked here so it isn't forgotten):

**Per-appliance (what Proxmox's node/guest views gave us):**
- Instance lifecycle: start / stop / restart / freeze, **snapshot + restore** (LXC snapshot ≈ our
  per-component rollback), clone, delete — across containers *and* any VM exception.
- **Console/terminal** access in-browser (Incus exposes an exec/console websocket).
- Live **metrics** (CPU/mem/disk/net per instance + host) — Incus has a metrics endpoint
  (Prometheus format) we can scrape instead of Proxmox's rrd.
- **Storage** view: the `data` btrfs pool usage, per-instance volumes, snapshots.
- **Network** view: `incusbr-*` / adopted bridges, the internal backplane, per-instance NICs + the
  USB/`by-id` device bindings (coordinators).
- **Host OS / RAUC** panel: current A/B slot, health, pending OTA bundle, trigger update + rollback.

**Fleet (what Proxmox never gave us — our differentiator):**
- Multi-appliance roster, health, versions, "update N boxes", per-site grouping.

**The decision: build vs adopt vs hybrid.** Either we keep investing in our own orchestrator UI, or
we adopt a tool better suited to Incus — but note the hard constraint: **our provisioning, firstboot,
manifest/release, fleet, call-home, and RAUC-OTA flows, plus the Ctrlable-specific surfaces, have no
off-the-shelf equivalent.** A generic Incus tool can own **day-2 instance management**; it cannot own
what makes us us. So full replacement is unlikely — the realistic axis is how much day-2 to reuse.

Candidates to evaluate (all speak the same Incus REST API our `HostDriver` targets):
- **Incus built-in UI** (`incus-ui-canonical`; daemon serves it at `core.https_address`, usually
  `:8443`) — modern SPA (LXD-UI lineage): instances, snapshots, console, metrics, storage, networks.
  *PoC note (2026-11-17): the HTTPS API serves (`:8443`→200) but the built-in UI is **version-
  coupled to Zabbly's Incus**. `incus-ui-canonical` is not in Debian's repo; Zabbly ships it (even
  for trixie), but the assets are built for Zabbly's Incus **7.6** and **Debian's Incus 6.0.4 does
  not serve them** (`/ui/`→404 even with the assets dropped at `/opt/incus/ui`). So the built-in UI
  effectively requires **adopting Zabbly's Incus wholesale** — which is exactly the §15 Debian-repo-
  vs-Zabbly decision, now with teeth: "want the native Incus UI" ⇒ "run Zabbly's Incus."*
- **LXConsole** — open-source multi-server web UI for LXD/Incus; closer to a Proxmox-style fleet view.
- **Our provisioner UI, extended** — build the day-2 panels in, for one branded, coherent surface.
- **Proxmox UI** — the familiarity benchmark for field techs (node tree, guest tabs, console, task log).

Likely answer = **hybrid**: reuse a generic UI (Incus UI / LXConsole) for raw instance day-2 while we
keep + grow our orchestrator for provisioning, fleet, OTA, and Ctrlable flows — single sign-on / link
between them. Decide after hands-on evaluation.

> **Action:** stand up `incus-ui-canonical` (and try LXConsole) against the PoC box to evaluate
> reuse-vs-build-vs-hybrid, then scope the chosen management surface as a **P6** phase.
