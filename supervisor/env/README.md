# Layer-0 environment parity (ZERO fork)

These artifacts make the Ctrlable Pro **container** look like HAOS to a *stock* Supervisor,
so the add-on-install gates clear without patching Supervisor. They are consumed by the
appliance build (`poc/incus-bootstrap.sh` partB / the image's Ctrlable-Pro profile), **not**
by the fork harness. Keeping them here documents the contract the fork depends on.

Apply order inside the container (after the Supervised install, before first add-on install):
1. Container is **privileged** (`incus config set <ct> security.privileged true`) — clears
   `healthy:false → [privileged]`. Set on the Incus side, not in the container.
2. **NetworkManager owns the NIC** — install `network-manager`, drop
   `10-globally-managed-devices.conf`, remove the systemd-networkd `.network` that claims
   `eth0`, then bring `eth0` up under an NM connection. Clears `host_internet:false` — the
   HAOS-faithful fix for the connectivity gate. **VALIDATE ON BOX** (the PoC live hack didn't
   stick; this image-level config is the real test — SPEC §15 open item).
3. **os-agent** running + `/var/lib/homeassistant/audio/external` present (bootstrap already
   does the audio path) — clears the os-agent/audio evaluations.
