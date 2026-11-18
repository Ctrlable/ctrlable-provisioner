#!/usr/bin/env bash
# sync.sh — fetch upstream and show what changed since our pinned tag.
# Read-only: never bumps UPSTREAM_TAG or touches patches. A human bumps the pin
# only after `apply.sh` + `build.sh` pass on the new tag.
. "$(dirname "$0")/common.sh"
ensure_clone
NEW="$(newest_tag)"
log "pinned=$UPSTREAM_TAG  newest-upstream=$NEW"
if [ "$UPSTREAM_TAG" = "UNPINNED" ]; then
  log "no pin yet — set UPSTREAM_TAG=$NEW in UPSTREAM to start tracking"; exit 0
fi
if [ "$UPSTREAM_TAG" = "$NEW" ]; then log "up to date."; exit 0; fi
log "=== upstream commits $UPSTREAM_TAG..$NEW (summary) ==="
git -C "$VENDOR" log --oneline --no-merges "$UPSTREAM_TAG..$NEW" | sed 's/^/  /'
log "=== files our patch stack touches that ALSO changed upstream (merge-risk) ==="
touched="$(for p in $(patch_list); do lsdiff "$p" 2>/dev/null || git -C "$VENDOR" apply --numstat "$p" 2>/dev/null | awk '{print $3}'; done | sort -u)"
changed="$(git -C "$VENDOR" diff --name-only "$UPSTREAM_TAG..$NEW" | sort -u)"
comm -12 <(printf '%s\n' "$touched") <(printf '%s\n' "$changed") | sed 's/^/  ⚠ /' || true
log "(review the above, then run apply.sh to test the stack on $NEW)"
