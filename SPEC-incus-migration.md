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
  Debian 13 (trixie) host
    └─ Incus (system containers + QEMU VMs)
         ├─ ctrlable-orchestrator      (system container — our provisioner, this repo)
         ├─ ctrlable-pro               (QEMU VM — rebranded HAOS; add-on store + RAUC OTA)
         ├─ zwavejs / zigbee2mqtt / …  (system containers — services)
         └─ incusbr-site / incusbr-internal  (networks; or adopt existing vmbr0/vmbr1)
  Provisioner UI/API  ──Incus REST API (unix socket / https+token)──▶ host
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

**Recommendation (updated): coordinator-driven, not fixed.** For the **current USB-only fleet,
Option B (Supervised-in-LXC)** is now the leaning choice — trivial USB passthrough, equivalent
per-component rollback via LXC snapshots, and a "supported" status we control. **Option A (HAOS
VM)** remains preferred for sites on **network-attached coordinators**, where its clean `.img`
deploy and intact RAUC path win with zero USB passthrough. The PoC validates both before any
commit. (C is excluded by the store req.) **Host-OS update safety is a separate, higher-value
axis — see §14.**

## 7. The builder problem (the long pole)

`community-scripts/ProxmoxVE` recipes are Proxmox-native (`pct`, Proxmox storage, whiptail) and
**do not run on Incus**. We must own a **build recipe per service** (zwavejs, zigbee2mqtt,
frigate, freepbx, hardware-manager, dali-bridge). Approach: distro-agnostic install scripts run
inside a base Incus container, then `incus publish` to a local image — reusing the *pinned app
version + `var_*`* intent we already encode, minus the Proxmox plumbing. This is new code **and**
permanent maintenance we currently get upstream for free — the single biggest cost line.

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

> **Net:** fleet is USB-only now → **Option B leans ahead** at those sites (trivial USB, snapshot ≈ RAUC
> per-component, a "supported" status we control). As sites adopt **network coordinators**, **Option A
> (HAOS VM)** regains the edge (clean `.img`, intact RAUC, zero passthrough). **Host-OS atomic rollback
> (§14) is the higher-value, orthogonal prize either way.**

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

## 15. Open decisions

- [ ] Ctrlable Pro host: **Option B (Supervised-LXC)** for USB-only sites vs **Option A (HAOS VM)** for network-coordinator sites — PoC both (§6, §13).
- [ ] Host OS atomic rollback (§14): btrfs+snapper+grub-btrfs vs ZFS boot envs vs image-based A/B — and whether to wire boot-counting auto-rollback.
- [ ] Per-site: USB-local (tilts B) vs commit to **network coordinators** (keeps A) — drives the host choice (§13).
- [ ] Networks: **adopt existing `vmbrN`** vs Incus-managed networks.
- [ ] Storage backend: zfs vs btrfs.
- [ ] Builders: fork/adapt community-scripts logic vs write clean distro-agnostic recipes.
- [ ] Target Debian base: 13 (trixie) + Incus from Debian repo vs Zabbly packages.
