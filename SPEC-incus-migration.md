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

- **Option A — keep HAOS as an Incus VM (RECOMMENDED).** Preserves the add-on store, our
  **Supervisor fork**, **RAUC OS OTA** (rebrand-tools / Core+OS tiles), the supported/healthy
  status, and the clean `.img` deploy. Cost: a VM (RAM/boot overhead) and the HAOS-on-Incus
  image work (the PoC's job). **Lowest risk; keeps the fleet OTA intact.**
- **Option B — Ctrlable Pro as Supervised-in-LXC.** Run HA **Supervised** (Supervisor +
  Docker) in a **privileged / `security.nesting=true`** container. Keeps the add-on store and
  drops the VM, but: it is **officially unsupported** (unhealthy banner, failing supported-
  system checks); **removes HAOS → no RAUC → our OS-OTA fleet mechanism must be rebuilt**;
  Docker-in-LXC adds fragility for USB/AppArmor-sensitive add-ons. **Not recommended as the
  default — it trades a bounded VM-image problem for dismantling our update story.**
- **Option C — HA Container / Core in an LXC.** Lightest, but **loses the add-on store** →
  off the table given the requirement.

**Recommendation: Option A.** Revisit B only if VM overhead proves unacceptable on target
hardware *and* we're willing to replace the OS-OTA pipeline. (C is excluded by the store req.)

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
- **P3 — Ctrlable Pro VM.** HAOS as an Incus VM; carry over rebrand + firstboot + RAUC OTA.
- **P4 — Networking & storage.** Adopt `vmbr0/1`; storage pool; backup/restore.
- **P5 — Bootstrap.** `bootstrap-appliance.sh` installs Incus on plain Debian instead of
  standing up Proxmox. Per-box cutover with Proxmox rollback retained.

## 13. Open decisions

- [ ] Ctrlable Pro: confirm **Option A (HAOS VM)** vs accept Option B's OTA cost.
- [ ] Networks: **adopt existing `vmbrN`** vs Incus-managed networks.
- [ ] Storage backend: zfs vs btrfs.
- [ ] Builders: fork/adapt community-scripts logic vs write clean distro-agnostic recipes.
- [ ] Target Debian base: 13 (trixie) + Incus from Debian repo vs Zabbly packages.
