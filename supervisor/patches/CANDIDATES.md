# Candidate Layer-1 patches (DO NOT add until Layer 0 is proven insufficient)

These are the *only* Supervisor modifications we anticipate needing for Supervised-in-LXC,
and each exists **only** because an LXC cannot present the exact thing stock Supervisor checks
for. Before turning any of these into a real `patches/NNNN-*.patch`, prove on the box that
Layer-0 environment parity (§6a: privileged CT + NetworkManager-managed NIC + os-agent) does
**not** already clear it. Empty stack is the win.

Each candidate is written to be **inert on real HAOS** (gated on an env/config flag or a
fallback that only triggers when the HAOS-shaped signal is absent), so upstream merges stay cheap.

---

## C1 — connectivity fallback — NOT NEEDED (2026-11-18)
**Resolved by Layer-0, zero fork.** Masking networkd for the NIC + NM-alone DHCP gave
`connectivity=full` / `host_internet:true` and add-ons install on a stock Supervisor. No patch.
Kept here only as a reserve shape if a future upstream changes connectivity detection.
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

## C2 — platform "supported" for the Ctrlable LXC environment — IMPLEMENTED (2026-11-18)
**Shipped as `patches/0001-ctrlable-platform-supported-gate-for-Supervised-in-L.patch`** (pinned
tag 2026.11.0). Gates the `os` and `network_manager` unsupported evaluations behind
`CTRLABLE_PLATFORM=lxc` (set only by our image; inert on HAOS). Rationale: both are structurally
unsatisfiable in an Incus LXC (os_agent is not HAOS; the NM-managed veth is not enumerable over
Supervisor's NM D-Bus) yet neither blocks a feature — so this clears the "unsupported" banner for
HAOS parity. CAVEAT: silencing `network_manager` does not ADD the NETWORK host-feature, so the
Supervisor Network UI stays inert until the veth is exposed over D-Bus (follow-up); host/Incus owns
networking meanwhile. Behavioural validation (supported→true) needs a build of the forked
Supervisor image (`make build`); the patch applies clean + compiles against 2026.11.0.
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
