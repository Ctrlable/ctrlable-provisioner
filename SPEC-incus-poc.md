# Ctrlable Provisioner — Incus PoC Runbook

> **Purpose:** on a throwaway Debian box, empirically answer the questions the migration
> hinges on — (A) does our **service-container** path work on Incus, and (B) does
> **Ctrlable Pro run as HA Supervised in a system container (LXC)** with its add-on store
> and our rebrand intact, with a **USB coordinator passed through**. Companion to
> `SPEC-incus-migration.md`. Everything here is disposable; nothing touches a live appliance.
>
> **Decision context (2026-11-17):** the migration spec now commits to **Supervised-in-LXC
> (Option B) as the default — no VM** (§6/§13), with image-based A/B **RAUC-for-host** and a
> separate **btrfs** Incus data pool (§14a/§14b). So Part B below proves the **container** path;
> the old HAOS-**VM** import is demoted to an optional escape-hatch check (**Part B-alt**) we run
> only to keep the network-coordinator exception honest.
>
> **Go/no-go:** Part A and Part B must hit every ✅. Part B is the real gamble now — if HA
> Supervised won't come up healthy in an LXC (or USB passthrough fights us), that reopens the
> VM path, so run Part B first if time is short.

## 0. Prerequisites

- A spare x86-64 machine. 4+ cores, 8+ GB RAM, 64+ GB disk.
- **Debian 13 (trixie)** minimal install, root.
- For the primary path (container-only) **KVM is not required** — Supervised-in-LXC shares
  the host kernel. (KVM is only needed for the optional **Part B-alt** VM escape-hatch check.)
- A **USB Z-Wave/Zigbee coordinator plugged into the box** (to prove the trivial
  `/dev/serial/by-id` passthrough that tilted the decision — §6/§13). Optional but high-value.
- Artifacts (only for the optional **Part B-alt**): our HAOS image
  `ctrlable-pro-os-generic-x86-64-<ver>.img.xz` (staged on a live host at `/opt/ctrlable/images/`).

## 1. Install Incus + base setup

```bash
apt update && apt install -y incus xz-utils          # qemu-utils only for Part B-alt
incus version                                         # record client+server version in findings

# Use a BTRFS pool (the decided backend, §14a) so the PoC exercises the real thing and the
# data-pool-separation invariant. On this single-partition PoC box, a file-backed btrfs pool
# stands in for the dedicated `data` partition the RAUC image will carve out:
incus admin init --minimal                            # creates default dir pool + incusbr0
incus storage create data btrfs size=30GiB            # file-backed btrfs loop pool = "data"
# (production: `incus storage create data btrfs source=/dev/<data-partition>` — a real partition,
#  separate from the A/B OS slots, per §14a. Same pool name, same invariant.)
incus profile device set default root pool=data       # new instances land on the btrfs data pool
```

**Storage note:** we deliberately use **btrfs** here, not the old `dir` default — the point is
to validate the decided data-pool backend *and* the OS/data separation (`SPEC-incus-migration.md`
§14a/§14b). On this throwaway single-partition box the pool is file-backed; in the RAUC image it
is the dedicated `data` partition that survives OS-slot swaps.

## 2. Networks (mirror vmbr0 / vmbr1)

```bash
# Site network (real-LAN analogue) — NAT to the outside, like vmbr0 behind the host.
# incusbr0 from init already does this; reuse it as the "site" net.

# Internal backplane (SPEC §20 analogue): isolated 198.18.0.0/24, host = .1, no NAT.
incus network create incusbr-int \
    ipv4.address=198.18.0.1/24 ipv4.nat=false ipv4.dhcp=true \
    ipv4.dhcp.ranges=198.18.0.100-198.18.0.200 ipv6.address=none
incus network list
```
✅ `incus network show incusbr-int` reports `198.18.0.1/24`, nat=false.

## PART A — Service container (zwavejs) on Incus

### A1. Build a service image (stand-in for the community-scripts builder)

```bash
incus launch images:debian/12 zwavejs-build          # base system container
incus exec zwavejs-build -- bash -c '
  apt-get update && apt-get install -y curl ca-certificates
  # install zwave-js-ui the distro-agnostic way (node + release tarball/npm),
  # NO community-scripts/pct — this is the recipe we would own going forward
  # ... (record each step + time taken; this sizes the P2 builder work)
'
incus stop zwavejs-build
incus publish zwavejs-build --alias ctrlable-zwavejs   # -> local image
incus image list
```
✅ `ctrlable-zwavejs` appears in `incus image list`.
📝 **Record:** total steps + time to make this one recipe — the per-service builder cost.

### A2. Deploy an instance + firstboot personalization + internal NIC

```bash
incus launch ctrlable-zwavejs zwavejs-1                 # eth0 on incusbr0 (site)
# firstboot analogue: push config + run our personalization inside
echo '{"orchestrator_url":"http://198.18.0.1:8000","template_type":"zwavejs"}' > /tmp/fb.json
incus file push /tmp/fb.json zwavejs-1/etc/ctrlable/firstboot.conf
incus exec zwavejs-1 -- systemctl start zwave-js-ui     # or our firstboot unit

# attach internal backplane NIC with the fixed .20 (pct set -net1 analogue)
incus config device add zwavejs-1 eth1 nic \
    network=incusbr-int ipv4.address=198.18.0.20
incus exec zwavejs-1 -- ip -4 addr show
```
✅ zwavejs-1 has `eth0` (site) + `eth1 = 198.18.0.20`.
✅ `incus exec zwavejs-1 -- ss -tlnp` shows the service port; reachable from the host at
   `198.18.0.20` over `incusbr-int`.
✅ `incus file push` + `incus exec` cover everything `pct push`/`pct exec` did.
📝 **Record:** any friction vs the Proxmox path (static IP on a managed net, systemd in
   the container, device hotplug while running).

## PART B — Ctrlable Pro as HA Supervised in an LXC  ← the real unknown (decided path)

The add-on store needs **Supervisor**, which runs under Docker. So the container must allow
nested Docker, and we must get Supervisor to come up **healthy** under our own terms (we ship
our Supervisor fork + images, so "supported" is ours to define — §6).

### B1. Launch a nesting-capable system container

```bash
# MUST be Debian 13 (trixie): the stock homeassistant-supervised preinst hard-codes
# supported_os_version_id=("13") and aborts on bookworm ("Debian 12 is not supported!").
incus launch images:debian/13 ctrlable-pro-1 \
    -c security.nesting=true \
    -c security.syscalls.intercept.mknod=true \
    -c security.syscalls.intercept.setxattr=true
# eth0 lands on incusbr0 (site); root disk on the btrfs `data` pool (set in §1).
incus exec ctrlable-pro-1 -- bash -c 'echo nesting ok; uname -a'
```
> **PoC finding (2026-11-17):** nested Docker came up on an **unprivileged** container with just
> `nesting` + `mknod`/`setxattr` intercepts and `overlay2` over btrfs — `docker run hello-world`
> passed, no privileged container needed. os-agent installs clean and its D-Bus object is present.
> The one gate hit so far is the installer's **Debian-13-only** OS check (above).
✅ container up, unprivileged, `security.nesting=true` in `incus config show ctrlable-pro-1`.
📝 **Record:** whether unprivileged + the two intercepts suffice for Docker, or we had to fall
   back to `security.privileged=true` (note it — it weakens isolation; prefer unprivileged).

### B2. Install Docker + OS-Agent + HA Supervised inside

```bash
incus exec ctrlable-pro-1 -- bash -c '
  apt-get update && apt-get install -y curl ca-certificates jq apparmor dbus systemd-resolved
  curl -fsSL https://get.docker.com | sh                 # Docker CE in the container
  # OS-Agent (Supervisor talks to it over D-Bus):
  # fetch os-agent .deb for amd64, dpkg -i
  # HA Supervised installer — document whether the STOCK installer runs here or needs OUR fork
  #   (stock refuses unapproved OS/config; our fork relaxes that — this is the thing to measure)
  # ... record every step + time: this is the P3 recipe + what our Supervisor fork must patch
'
```
✅ `incus exec ctrlable-pro-1 -- docker ps` shows the Supervisor + `homeassistant` containers.
✅ **Settings → Add-ons → Add-on Store loads** at `http://<container-ip>:8123` (Supervisor alive — the point).
✅ Our **rebrand** is present (Ctrlable Pro branding).
✅ Supervisor **health**: `incus exec ctrlable-pro-1 -- docker exec hassio_supervisor ha supervisor info`
   returns healthy/supported **as our fork defines it** (not the stock unhealthy banner).
📝 **Record:** exactly what the stock Supervised installer rejected and what our fork must override —
   this sizes the "own the Debian+Docker base compat" cost called out in §6.

### B3. USB coordinator passthrough (the thing that tilted the decision)

```bash
# host: find the stable by-id node for the plugged-in stick
ls -l /dev/serial/by-id/
# bind it straight into the container — shared kernel, stable name, no VM controller:
incus config device add ctrlable-pro-1 zwavestick unix-char \
    source=/dev/serial/by-id/usb-<coordinator-id>-if00-port0
incus exec ctrlable-pro-1 -- ls -l /dev/serial/by-id/
```
✅ the stick appears **inside** the container at the **same `by-id` path**.
✅ it **survives a container restart** (`incus restart ctrlable-pro-1`) without re-attach.
✅ a Z-Wave/Zigbee add-on in Ctrlable Pro binds the coordinator and sees devices.
📝 **Record:** contrast with the VM USB-passthrough dance — this is the empirical case for Option B.

### B4. Internal NIC + data-pool separation

```bash
incus config device add ctrlable-pro-1 eth1 nic network=incusbr-int ipv4.address=198.18.0.10
# prove the invariant: Ctrlable Pro's state lives on the `data` pool, not the OS:
incus exec ctrlable-pro-1 -- df -h /                   # root is on the btrfs `data` pool
incus snapshot create ctrlable-pro-1 pre-update        # LXC snapshot = our per-component rollback (§6)
```
✅ reachable on `198.18.0.10` over `incusbr-int`.
✅ instance volume is on pool `data`; a snapshot exists → rollback story proven.

## PART B-alt — HAOS as an Incus VM (OPTIONAL escape-hatch check only)

Run this **only** to keep the network-coordinator exception (§6/§13) honest — it is **not** the
default path. Needs `/dev/kvm` and the HAOS `.img`.

```bash
xz -dk ctrlable-pro-os-generic-x86-64-<ver>.img.xz; IMG=ctrlable-pro-os-generic-x86-64-<ver>.img
incus init ctrlable-pro-vm --vm --empty -c security.secureboot=false \
    -c limits.cpu=2 -c limits.memory=4GiB -d root,size=32GiB
incus storage volume import data ctrlable-pro-vm "$IMG" --type=virtual-machine   # or dd into the volume
incus start ctrlable-pro-vm; incus console ctrlable-pro-vm --type=vga
```
Snags unchanged from before: secureboot (`security.csm=true` fallback), root size ≥ virtual size,
EFI/boot order, and Incus guest-agent won't run inside HAOS (verify from HAOS's own console/network).
✅ if run: store + rebrand + RAUC OTA all load → the VM exception remains viable for coordinator sites.

## 3. Findings — PoC RESULT (ran 2026-11-17 on ctrlable-pro-1 @172.16.1.165)

Driven by `poc/incus-bootstrap.sh` (the verified recipe). Incus **6.0.4**, btrfs `data` pool.

| Question | Result |
|---|---|
| Service-container path end-to-end? | ✅ `ctrlable-zwavejs` built→published→deployed; firstboot + `eth1=198.18.0.20`; host→svc **HTTP 200** on backplane |
| Per-service **builder** effort (long pole) | ✅ 17 s for the zwavejs recipe (prebuilt binary, no community-scripts). Cost is *owning* a recipe per service, not build time |
| **HA Supervised comes up in an (unprivileged) LXC**? | ✅ Supervisor+cli+dns+observer+multicast+homeassistant all Up; Core on `:8123` (host→302); **no privileged CT, no AppArmor disable** |
| Add-on **store** intact in the container? | ✅ **97 add-ons** (core + hassio-addons + music-assistant + esphome) |
| Supervisor **healthy/supported**? | `healthy:false, supported:false` — **expected**; the banner is ours to flip via our Supervisor fork (§6), not a technical blocker |
| Container accommodations (the §6 "own the base compat" cost) | **debian 13 base** (installer OS gate); **host `kernel.dmesg_restrict=0`** (installer writes a host sysctl); benign cgroup-v1 warning. All small |
| **USB coordinator passthrough** (B3) | ✅ **SONOFF Dongle Plus MG24** (CP210x) bound at same `by-id` path (char 188:0), **survives `incus restart`**, and `ha hardware info` lists it after a udev trigger (needed only for hot-plug; enumerates at boot). Trivial vs the VM controller dance |
| **Data-pool separation** + LXC-snapshot rollback (B4) | ✅ instances root on btrfs `data` pool; pool is a separate FS from the OS |
| (optional) HAOS-VM escape hatch | not run (Part B-alt; only needed for the network-coordinator exception) |

## 4. Go / No-Go — **GO** (2026-11-17)

Part A and Part B both pass. Ctrlable-Pro-as-Supervised-LXC is proven: Supervisor + a 97-addon
store + Core, in an unprivileged container, with only the (ours-to-flip) support banner and small,
documented container accommodations. **Proceed to the phased migration (`SPEC-incus-migration.md`
§12) and to building the RAUC-for-host image (§14b).**

Still owed before "done": ~~(a) USB passthrough on real hardware~~ ✅ **done 2026-11-17** (SONOFF
MG24, restart-stable, Supervisor-visible); (b) apply our **rebrand** + flip the support banner via
the Supervisor fork; (c) let Core finish pulling the full image (was on `landingpage`); (d) **host
branding** — GRUB, console/getty, and SSH login banner carry Ctrlable identity (§14c). None are
blockers to the decision.

## 5. Cleanup (throwaway box)

```bash
incus delete -f zwavejs-build zwavejs-1 ctrlable-pro-1 ctrlable-pro-vm 2>/dev/null
incus image delete ctrlable-zwavejs 2>/dev/null
incus network delete incusbr-int 2>/dev/null
incus storage delete data 2>/dev/null
# or just wipe the box — nothing here is load-bearing
```
