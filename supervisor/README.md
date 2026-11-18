# Ctrlable Pro Supervisor on Incus — delegates to rebrand-tools

**The Ctrlable Supervisor is built and tracked in ONE place: `rebrand-tools`** —
`server/patch_supervisor.py` + `.github/workflows/build-supervisor.yml`. That pipeline
pulls the **official upstream Supervisor (STABLE releases only** — enforced there, never
dev/beta/`:latest`), applies the Ctrlable brand patches, and publishes
`ghcr.io/ctrlable/<arch>-hassio-supervisor`. **The whole fleet runs that same branded image** —
the Proxmox HAOS-in-VM appliances *and* this Incus LXC appliance — pinned via the version
endpoint's `images.supervisor` (`stable.json`).

We deliberately do **NOT** fork or track Supervisor a second time here — that would duplicate
rebrand-tools' upstream tracking and break the "track upstream once, at HA's cadence" rule.
(An earlier draft of this dir carried a parallel `format-patch` harness; removed 2026-11-18 once
we confirmed rebrand-tools already owns Supervisor.)

## The only LXC-specific pieces (what this directory is for)

1. **The supported-gate lives in rebrand-tools**, not here:
   `patch_supervisor.py::_patch_lxc_supported_gate` clears the two unsupported reasons that are
   structurally unsatisfiable in an LXC but block no feature — `os` (os_agent is not HAOS) and
   `network_manager` (the NM-managed Incus veth is not enumerable over Supervisor's NM D-Bus).
   It is gated at runtime on **`CTRLABLE_PLATFORM=lxc`**, so the one branded image stays inert on
   the VM path (which never sets the flag, and is genuinely supported anyway). Validated on
   ctrlable-pro-1 2026-11-18: with the flag, `supported:true`, `unsupported:[]`.

2. **Setting the flag** is the appliance's job: `poc/incus-bootstrap.sh partB` wires
   `-e CTRLABLE_PLATFORM=lxc` into `hassio-supervisor.service`. The flag is NEVER baked into the
   image (fail-safe: a missing flag = vanilla behaviour, never a false "supported").

3. **`env/` — Layer-0 network parity** (the zero-fork part): hand `eth0` to NetworkManager so a
   stock Supervisor reports `host_internet:true` and add-ons install (SPEC §6a). `apply-net-parity.sh`
   masks systemd-networkd for the NIC and lets NM alone DHCP the Incus-bridge veth. Applied by
   `incus-bootstrap partB`. This is pure deployment config — correctly a provisioner concern.

## Production pin
A rebrand-tools build of the branded Supervisor carries the gate automatically. To put it on the
fleet, point `stable.json`'s `images.supervisor` at that build — a **deliberate fleet-wide deploy**,
done from rebrand-tools, not here.
