# Ctrlable Pro — Supervisor fork-tracking harness

**Goal (owner directive, 2026-11-18):** Ctrlable Pro must behave **as close to HAOS as
possible, with no feature loss.** Where that needs Supervisor changes, we make them — but our
Supervisor stays **continuously rebaseable on vanilla** so we keep shipping at Home Assistant's
own release cadence. See `SPEC-incus-migration.md` §6a.

**Design rule:** every patched line is merge-conflict surface against a weekly upstream, so the
stack is kept **minimal and isolated** — ideally *empty*. We clear the Supervised-in-LXC
add-on-install gates with **environment parity first** (no fork), and patch Supervisor only for
what no LXC environment can satisfy.

## Layout
- `UPSTREAM`            — pinned vanilla Supervisor tag we track (+ repo URL).
- `patches/`           — the `git format-patch` stack applied on top of that tag. **Empty is
                         the goal.** `CANDIDATES.md` lists the only patches we expect to need
                         (connectivity fallback, platform-supported/branding) and their exact
                         upstream targets — add them only if the box proves the environment
                         can't clear the gate.
- `env/`               — **Layer-0, zero-fork** container config that makes a *stock*
                         Supervisor pass (NetworkManager owns the NIC, etc.). Consumed by the
                         appliance build, not by this harness; documents the contract.
- `scripts/`, `Makefile` — the harness: `sync` / `apply` / `report` / `build`.
- `ci/`                — scheduled upstream-release watch (runs `report`).

## Workflow
```
make sync       # fetch vanilla, show commits + merge-risk files since our pin (read-only)
make apply      # apply our stack on the pinned tag; names any patch that won't apply
make report     # CI/human: newest release, how far behind, does the stack still apply
make build      # apply stack + build the Ctrlable Pro Supervisor image (needs docker)
```
On each upstream release: `make report` → if clean, bump `UPSTREAM_TAG`, `make build`, smoke
test on the box, ship. If conflicting, resolve exactly the named hunk, regenerate that patch.

## Status
- `UPSTREAM_TAG=2026.11.0` — pinned to the Supervisor the PoC box runs.
- `patches/` — **one patch.** Add-on parity needs ZERO fork (Layer-0 env cleared it;
  `core_mosquitto` installed on a stock Supervisor, 2026-11-18). The single patch,
  `0001-ctrlable-platform-supported-gate-*`, is the OPTIONAL supported-banner branding (gates the
  `os`/`network_manager` unsupported reasons behind `CTRLABLE_PLATFORM=lxc`, inert on HAOS). C1 is
  not needed; see `patches/CANDIDATES.md`. Applies clean + compiles against 2026.11.0; behavioural
  validation needs `make build` of the forked image.
