# Ctrlable Pro appliance OS image — RAUC-for-host A/B (§14b)

The build pipeline for the **plain-Debian + Incus** appliance OS: an image-based **A/B** host
with RAUC OTA + automatic rollback, a single persistent **btrfs `data`** Incus pool, and
**zero-touch first boot**. This is the productionization of `../poc/incus-bootstrap.sh` — the
logic proven in the PoC is baked into the image and runs from a first-boot unit.

> **Status: SCAFFOLD.** Every design decision from `../SPEC-incus-migration.md` §14a/§14b/§14c is
> encoded here and the scripts are real, but the image has **not** been build-tested yet — building
> needs a **Debian 13 build host** with root + loop devices (not macOS). TODOs mark the spots that
> need a build host or real signing keys. Treat this as the skeleton to iterate on, not a release.

## Disk layout it produces (§14a)

```
  GPT /dev/sdX
   ├─ esp     (FAT32, 512M)   GRUB + grubenv (slot select + boot counting) + both kernels
   ├─ root-a  (ext4,  ~4G)    OS slot A   ─┐ immutable, RAUC-managed, swappable
   ├─ root-b  (ext4,  ~4G)    OS slot B   ─┘ (B empty at flash; first OTA fills it)
   └─ data    (btrfs, rest)   the ONE persistent Incus pool — NEVER touched by an OS update
```

## Pipeline

```
  scripts/build-rootfs.sh   mmdebstrap -> work/rootfs/   (Debian 13 + Incus + first-boot + branding + orchestrator key)
  scripts/pack-image.sh     work/rootfs -> work/ctrlable-pro.img   (GPT: esp+root-a+root-b+data, GRUB installed, root-a populated)
  scripts/make-bundle.sh    work/rootfs -> work/ctrlable-pro.raucb  (signed RAUC bundle for OTA)
  scripts/gen-dev-keys.sh   dev signing cert/key  (DEV ONLY — prod keys live in CI secrets)
```

Orchestrated by the `Makefile`:

```
make keys      # one-time dev signing keys  (prod: supply via RAUC_CERT/RAUC_KEY)
make rootfs    # build the Debian rootfs
make image     # flashable .img  (first appliance of a release / factory)
make bundle    # .raucb OTA bundle  (every subsequent update)
make all
make clean
```

## How it maps to the two deliverables

- **New appliance from bare metal** → flash `ctrlable-pro.img` (USB installer / netboot / dup),
  power on. First boot creates the `data` pool, registers with the orchestrator, launches Incus
  containers (Ctrlable Pro Supervised-LXC + services). No human input — the orchestrator key is
  baked in. (`SPEC-incus-migration.md` §P5 / zero-touch chain.)
- **Fleet update** → `ctrlable-pro.raucb` over our existing OTA channel. RAUC writes the inactive
  slot, flips the GRUB slot pointer, reboots; boot-counting rolls back automatically on a failed
  boot. The `data` pool is untouched.

## Key files

| Path | Role |
|---|---|
| `config/mmdebstrap/packages` | package set baked into the rootfs |
| `config/rauc/system.conf` | RAUC slot definitions + grub boot backend |
| `config/grub/grub.cfg.in` | boot-counting A/B slot selection |
| `config/first-boot/ctrlable-firstboot.sh` + `.service` | zero-touch first boot |
| `config/orchestrator/authorized_keys` | **orchestrator pubkey baked into `/root/.ssh`** (replace placeholder) |
| `scripts/*` | build/pack/bundle/keys |

## Open sub-decision still live (§15)

RAUC bootloader slot-select backend: **GRUB + grubenv** (scaffolded here; built-in boot counting)
vs systemd-boot. If we switch, `config/rauc/system.conf` `bootloader=` and `config/grub/` change.
