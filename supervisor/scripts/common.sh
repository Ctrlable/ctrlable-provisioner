#!/usr/bin/env bash
# shared helpers for the Supervisor fork-tracking harness
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
. "$HERE/UPSTREAM"
: "${WORK:=$HERE/.work}"          # scratch clone of vanilla supervisor (gitignored)
: "${PATCHDIR:=$HERE/patches}"
VENDOR="$WORK/supervisor"
log(){ echo "[supervisor-fork] $*" >&2; }

ensure_clone(){
  mkdir -p "$WORK"
  if [ ! -d "$VENDOR/.git" ]; then
    log "clone vanilla upstream -> $VENDOR"
    git clone --quiet "$UPSTREAM_REPO" "$VENDOR"
  fi
  git -C "$VENDOR" fetch --quiet --tags origin
}

newest_tag(){ git -C "$VENDOR" tag --sort=-creatordate | grep -E '^[0-9]{4}\.[0-9]+' | head -1; }

patch_list(){ ls "$PATCHDIR"/*.patch 2>/dev/null | sort || true; }
