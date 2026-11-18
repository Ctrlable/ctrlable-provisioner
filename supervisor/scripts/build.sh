#!/usr/bin/env bash
# build.sh — produce the Ctrlable Pro Supervisor artifact from TARGET tag + patch
# stack. Runs apply.sh first (fails if the stack doesn't apply), then builds the
# Supervisor container image the way HA builds it (Dockerfile in the repo).
# This is the fork's output; the appliance image/incus-bootstrap installs the
# Supervised stack and points it at this image (see env/ for the version wiring).
. "$(dirname "$0")/common.sh"
TARGET="${1:-$UPSTREAM_TAG}"
: "${IMAGE:=ctrlable/supervisor:local}"
bash "$(dirname "$0")/apply.sh" "$TARGET"
log "build $IMAGE from $VENDOR (vanilla $TARGET + $(patch_list | wc -l | tr -d ' ') patch(es))"
if [ -f "$VENDOR/Dockerfile" ]; then
  docker build -t "$IMAGE" --build-arg BUILD_ARCH="${ARCH:-amd64}" "$VENDOR"
else
  log "no Dockerfile at tag $TARGET — adjust for the upstream build layout"; exit 1
fi
log "built $IMAGE"
