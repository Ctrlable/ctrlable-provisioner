# Layer-0 environment parity (ZERO fork)

These artifacts make the Ctrlable Pro **container** look like HAOS to a *stock* Supervisor,
so the add-on-install gates clear without patching Supervisor. They are consumed by the
appliance build (`poc/incus-bootstrap.sh` partB / the image's Ctrlable-Pro profile), **not**
by the fork harness. Keeping them here documents the contract the fork depends on.

Apply order inside the container (after the Supervised install, before first add-on install):
1. Container is **privileged** (`incus config set <ct> security.privileged true`) — clears
   `healthy:false → [privileged]`. Set on the Incus side, not in the container.
2. **NetworkManager owns the NIC** — install `network-manager`, drop
   `10-globally-managed-devices.conf`, **MASK systemd-networkd** for the NIC, and bring `eth0`
   up under a persistent NM DHCP connection. **VALIDATED 2026-11-18:** `connectivity=full`,
   `host_internet:true`, add-on installed on a stock Supervisor, survived reboot. Zero fork.
   The critical detail: networkd and NM must NOT both touch `eth0` — the fight breaks DHCP and
   NM then reports `limited`. `apply-net-parity.sh` encodes the validated clean sequence.
3. **os-agent** running + `/var/lib/homeassistant/audio/external` present (bootstrap already
   does the audio path) — clears the os-agent/audio evaluations.

> The Supervisor **supported-gate** (os/network_manager) is NOT here — it lives in
> rebrand-tools `patch_supervisor.py::_patch_lxc_supported_gate`, gated on `CTRLABLE_PLATFORM=lxc`
> (set by incus-bootstrap partB). This dir is only the Layer-0 network parity + the flag wiring.
