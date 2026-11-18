# Candidate Layer-1 patches (DO NOT add until Layer 0 is proven insufficient)

These are the *only* Supervisor modifications we anticipate needing for Supervised-in-LXC,
and each exists **only** because an LXC cannot present the exact thing stock Supervisor checks
for. Before turning any of these into a real `patches/NNNN-*.patch`, prove on the box that
Layer-0 environment parity (§6a: privileged CT + NetworkManager-managed NIC + os-agent) does
**not** already clear it. Empty stack is the win.

Each candidate is written to be **inert on real HAOS** (gated on an env/config flag or a
fallback that only triggers when the HAOS-shaped signal is absent), so upstream merges stay cheap.

---

## C1 — connectivity fallback when no NM-managed device is present
**Only if** Layer-0 NetworkManager-manages-the-veth does NOT clear `host_internet:false`.
- **Symptom:** add-on install/store refresh blocked by `JobCondition.INTERNET_HOST`;
  `ha network info` shows `host_internet: false` though the container clearly has egress.
- **Root cause:** Supervisor derives host connectivity from NetworkManager's connectivity
  state; with no NM-managed interface it reports no connectivity.
- **Target (confirm line #s against the pinned tag):**
  - `supervisor/host/network.py` — the `connectivity` property / `check_connectivity()`.
  - and/or `supervisor/jobs/decorator.py` — `JobCondition.INTERNET_HOST` evaluation.
- **Surgical change:** when NM reports *no managed device* (not merely "no connectivity"),
  fall back to a direct outbound probe (the same check Supervisor already uses elsewhere) and
  treat success as connected. Inert on HAOS (there, NM always manages a device, so the
  fallback never runs).

## C2 — platform "supported" for the Ctrlable LXC environment + branding
**Only if** the "unsupported" surface actually impedes a feature (it does **not** block
installs by itself) OR for owned branding of the supported/healthy state.
- **Target:** `supervisor/resolution/evaluations/*.py` (the bare-metal-only evaluations, e.g.
  `systemd`, `os_agent`, `docker_configuration`) and the resolution manager that aggregates
  `UnsupportedReason`.
- **Surgical change:** behind a single env flag (e.g. `CTRLABLE_PLATFORM=lxc`), suppress the
  evaluations that are structurally impossible in our container and are features we own.
  Keep every *functional* evaluation live. Inert on HAOS (flag unset).

## C3 — (reserve) any kernel/boot evaluation that assumes bare-metal HAOS
Only if one surfaces as a hard install blocker in the LXC. Same pattern: env-flag-gated,
documented, minimal. Prefer fixing the environment over adding this.

---

### Turning a candidate into a tracked patch
1. `make sync` then `make apply` to get a clean `.work/supervisor` at the pinned tag.
2. Edit the target file(s) in `.work/supervisor`, commit with a message explaining *why LXC
   needs this*, then `git -C .work/supervisor format-patch -1 -o ../../patches`.
3. Renumber as `NNNN-<concern>.patch`. Re-run `make apply` (must be clean) + `make build`.
4. Record the patch in this file with its upstream target so the next rebase knows its intent.
