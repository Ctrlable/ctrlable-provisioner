#!/usr/bin/env bash
# apply.sh — check out TARGET (default: pinned tag) and apply our patch stack,
# reporting exactly which patch (if any) no longer applies cleanly. Non-destructive
# to the repo; works in the scratch clone only.
. "$(dirname "$0")/common.sh"
TARGET="${1:-$UPSTREAM_TAG}"
ensure_clone
[ "$TARGET" = "UNPINNED" ] && { log "set UPSTREAM_TAG first (run sync.sh)"; exit 1; }
log "checkout $TARGET + reset to clean tree"
git -C "$VENDOR" checkout --quiet -f "$TARGET"
git -C "$VENDOR" clean -qfdx
n=0; fail=0
for p in $(patch_list); do
  n=$((n+1))
  if git -C "$VENDOR" apply --check "$p" 2>/dev/null; then
    git -C "$VENDOR" apply "$p"; log "✓ applied $(basename "$p")"
  else
    log "✗ DOES NOT APPLY: $(basename "$p") — resolve this hunk against $TARGET"
    git -C "$VENDOR" apply --3way "$p" 2>&1 | sed 's/^/    /' || true
    fail=$((fail+1))
  fi
done
log "stack: $n patch(es), $fail failed on $TARGET"
[ "$n" = 0 ] && log "NOTE: zero patches — this is the goal (Layer-0 environment parity, no fork)."
exit "$fail"
