#!/usr/bin/env bash
# build.sh — produce the Ctrlable Pro Supervisor image: the OFFICIAL Supervisor for
# the pinned tag, with our (tiny) patch stack layered on + CTRLABLE_PLATFORM=lxc.
#
# PROVEN on ctrlable-pro-1 (2026-11-18): this image reports supported:true in an
# Incus LXC (both `os` and `network_manager` unsupported reasons cleared), add-ons
# install, Core healthy. Because the stack touches only a few files, we LAYER them
# onto the official image (COPY) rather than rebuilding all of Supervisor from
# source — far faster, reproducible, and robust to the code dir being read-only at
# runtime (which is why an in-container edit fails; a build-time COPY does not).
# (A full source build is still possible: apply.sh patches $VENDOR, then
# `docker build` its own Dockerfile — use that only if a patch ever needs it.)
. "$(dirname "$0")/common.sh"
TARGET="${1:-$UPSTREAM_TAG}"
: "${ARCH:=amd64}"
: "${BASE_IMAGE:=ghcr.io/home-assistant/${ARCH}-hassio-supervisor:${TARGET}}"
: "${IMAGE:=ctrlable/${ARCH}-hassio-supervisor:${TARGET}}"
: "${PLATFORM_FLAG:=lxc}"

[ "$TARGET" = UNPINNED ] && { log "pin UPSTREAM_TAG first (run sync.sh)"; exit 1; }
command -v docker >/dev/null || { log "docker required to build"; exit 1; }

# Patch the vendored tree, then layer ONLY the files the stack touches.
bash "$(dirname "$0")/apply.sh" "$TARGET"
mapfile -t files < <(for p in $(patch_list); do awk '/^\+\+\+ b\//{sub(/^\+\+\+ b\//,"");print}' "$p"; done | sort -u)
if [ "${#files[@]}" -eq 0 ]; then
  log "no patched files — the official image already is our image (only ENV differs)"
fi

ctx="$WORK/ctx"; rm -rf "$ctx"; mkdir -p "$ctx"
{
  echo "FROM $BASE_IMAGE"
  echo "ENV CTRLABLE_PLATFORM=$PLATFORM_FLAG"
  for f in "${files[@]}"; do
    mkdir -p "$ctx/$(dirname "$f")"
    cp "$VENDOR/$f" "$ctx/$f"
    echo "COPY $f /usr/src/supervisor/$f"
  done
} > "$ctx/Dockerfile"

log "build $IMAGE  (FROM $BASE_IMAGE + ${#files[@]} patched file(s) + CTRLABLE_PLATFORM=$PLATFORM_FLAG)"
docker build -t "$IMAGE" "$ctx"
log "built $IMAGE"
log "next: push to the Ctrlable registry and PIN it as the supervisor image in the"
log "  appliance's Supervised config, so a Supervisor self-update never reverts to vanilla."
