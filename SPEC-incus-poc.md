# Ctrlable Provisioner — Incus PoC Runbook

> **Purpose:** on a throwaway Debian box, empirically answer the two questions the
> migration hinges on — (A) does our **service-container** path work on Incus, and
> (B) does **Ctrlable Pro (HAOS)** boot as an **Incus VM** with its add-on store and
> our rebrand intact. Companion to `SPEC-incus-migration.md`. Everything here is
> disposable; nothing touches a live appliance.
>
> **Go/no-go:** both Part A and Part B must hit every ✅ check. Part B is the real
> gamble — if HAOS fights the VM import, that reopens Option B (Supervised-LXC) and
> changes the migration plan materially, so run Part B first if time is short.

## 0. Prerequisites

- A spare x86-64 machine (or nested VM with **nested virt enabled** — HAOS is a VM,
  so the host must expose KVM). 4+ cores, 8+ GB RAM, 64+ GB disk.
- **Debian 13 (trixie)** minimal install, root/sudo.
- Artifacts to copy onto the box:
  - our HAOS image — `ctrlable-pro-os-generic-x86-64-<ver>.img.xz`
    (staged on a live host at `/opt/ctrlable/images/`; grab the current one).
- Confirm KVM is available: `kvm-ok` or `ls -l /dev/kvm` (must exist, or Part B can't run).

## 1. Install Incus + base setup

```bash
apt update && apt install -y incus qemu-utils xz-utils
# non-root use (optional): usermod -aG incus-admin $USER ; newgrp incus-admin
incus admin init --minimal          # dir pool "default", managed bridge "incusbr0"
incus version                       # record client+server version in findings
```

**Storage note:** `--minimal` gives a **`dir`** pool — deliberately chosen for the PoC
because a VM's root disk is then a plain `root.img` file, making the HAOS import trivial
(§3). Production storage (zfs/btrfs) is a separate decision (`SPEC-incus-migration.md` §13).

## 2. Networks (mirror vmbr0 / vmbr1)

```bash
# Site network (real-LAN analogue) — NAT to the outside, like vmbr0 behind the host.
# incusbr0 from init already does this; reuse it as the "site" net.

# Internal backplane (SPEC §20 analogue): isolated 198.18.0.0/24, host = .1, no NAT.
incus network create incusbr-internal \
    ipv4.address=198.18.0.1/24 ipv4.nat=false ipv4.dhcp=true \
    ipv4.dhcp.ranges=198.18.0.100-198.18.0.200 ipv6.address=none
incus network list
```
✅ `incus network show incusbr-internal` reports `198.18.0.1/24`, nat=false.

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
    network=incusbr-internal ipv4.address=198.18.0.20
incus exec zwavejs-1 -- ip -4 addr show
```
✅ zwavejs-1 has `eth0` (site) + `eth1 = 198.18.0.20`.
✅ `incus exec zwavejs-1 -- ss -tlnp` shows the service port; reachable from the host at
   `198.18.0.20` over `incusbr-internal`.
✅ `incus file push` + `incus exec` cover everything `pct push`/`pct exec` did.
📝 **Record:** any friction vs the Proxmox path (static IP on a managed net, systemd in
   the container, device hotplug while running).

## PART B — Ctrlable Pro (HAOS) as an Incus VM  ← the real unknown

### B1. Create an empty VM and import the HAOS disk

```bash
xz -dk ctrlable-pro-os-generic-x86-64-<ver>.img.xz     # -> raw .img
IMG=ctrlable-pro-os-generic-x86-64-<ver>.img
qemu-img info "$IMG"                                    # note virtual size

incus init ctrlable-pro-1 --vm --empty \
    -c security.secureboot=false \
    -c limits.cpu=2 -c limits.memory=4GiB \
    -d root,size=32GiB                                 # >= HAOS virtual size
# dir pool: overwrite the VM's root disk file with the HAOS image
VOL=/var/lib/incus/storage-pools/default/virtual-machines/ctrlable-pro-1/root.img
incus list ctrlable-pro-1 -c ns                        # must be STOPPED
qemu-img convert -f raw -O raw "$IMG" "$VOL"           # or: dd if="$IMG" of="$VOL" bs=4M conv=notrunc
incus start ctrlable-pro-1
incus console ctrlable-pro-1 --type=vga                # watch it boot (OVMF UEFI)
```

**Known snags to work through (this is why the PoC exists):**
- **Secureboot** — HAOS generic may refuse under OVMF secureboot; `security.secureboot=false`
  set above. If it still won't boot, try `security.csm=true` (legacy BIOS) as a fallback.
- **Disk size** — the VM root volume must be ≥ the HAOS image's virtual size, else the
  convert truncates. Bump `-d root,size=`.
- **Boot order / EFI vars** — if GRUB/systemd-boot doesn't come up, inspect the EFI entry
  from `incus console`.
- **Agent** — Incus's guest agent won't run inside HAOS (locked appliance); that's expected.
  Verify HAOS from *inside* via its own console/network, not `incus exec`.

### B2. Verify the appliance is fully intact

```bash
incus list ctrlable-pro-1 -c ns4                       # gets a site-LAN IP
# browse to http://<that-ip>:8123
```
✅ HA onboarding/UI loads at `:8123`.
✅ **Settings → Add-ons → Add-on Store loads** (proves Supervisor is alive — the whole point).
✅ Our **rebrand** is present (Ctrlable Pro branding, not stock HA).
✅ **RAUC/OS OTA** surfaces (Settings → System → Updates shows the Core/OS tiles) — confirms
   our fleet update path survives the VM move.
✅ Attach the internal NIC too and confirm reachability:
   `incus config device add ctrlable-pro-1 eth1 nic network=incusbr-internal ipv4.address=198.18.0.10`
📝 **Record:** exact boot config that worked (secureboot/csm, disk size, any EFI fiddling) —
   this *is* the P3 recipe.

## 3. Findings to capture (what this PoC is for)

| Question | Evidence from this run |
|---|---|
| Does the service-container path work end-to-end? | Part A checklist |
| Per-service **builder** effort (the long pole) | A1 step count + time |
| Does **HAOS boot as an Incus VM** at all? | B1 console |
| Add-on **store** intact? | B2 ✅ |
| **OS-OTA / RAUC** intact in a VM? | B2 ✅ |
| Exact HAOS VM boot recipe | B1/B2 notes |
| Incus version / storage driver used | §1 |

## 4. Go / No-Go

- **GO** (proceed to the phased migration, `SPEC-incus-migration.md` §12) if every ✅ passed.
- **CONDITIONAL** if Part A is clean but Part B needed heavy fiddling — HAOS-VM is viable but
  size P3 generously.
- **NO-GO / rethink** if Part B can't keep the store **and** OTA — then either stay on Proxmox
  or re-open Option B (Supervised-LXC) with eyes open about losing RAUC.

## 5. Cleanup (throwaway box)

```bash
incus delete -f zwavejs-build zwavejs-1 ctrlable-pro-1
incus image delete ctrlable-zwavejs
incus network delete incusbr-internal
# or just wipe the box — nothing here is load-bearing
```
