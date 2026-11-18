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
- `UPSTREAM_TAG=UNPINNED` — set it on the first `make sync` from a networked box.
- `patches/` empty — **intended.** First prove `env/apply-net-parity.sh` (Layer-0) clears
  `host_internet` on the box (SPEC §15 open item). Only then decide if C1/C2 are needed.
