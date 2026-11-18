#!/usr/bin/env bash
# report.sh — one-shot human/CI report: newest upstream release, whether our pin
# is behind, and whether the patch stack still applies to the newest tag.
. "$(dirname "$0")/common.sh"
ensure_clone
NEW="$(newest_tag)"
echo "## Ctrlable Pro Supervisor — upstream tracking report"
echo "- pinned tag:  $UPSTREAM_TAG"
echo "- newest tag:  $NEW"
echo "- patches:     $(patch_list | wc -l | tr -d ' ') in stack"
if [ "$UPSTREAM_TAG" != "UNPINNED" ] && [ "$UPSTREAM_TAG" != "$NEW" ]; then
  echo "- behind by:   $(git -C "$VENDOR" rev-list --count "$UPSTREAM_TAG..$NEW" 2>/dev/null || echo '?') commits"
  echo ""
  echo "### Does our stack still apply to $NEW?"
  if bash "$(dirname "$0")/apply.sh" "$NEW" >/tmp/sv-apply.$$  2>&1; then
    echo "✅ clean — safe to bump the pin after a build+smoke test"
  else
    echo "⚠️ CONFLICTS — a human must resolve:"; sed 's/^/    /' /tmp/sv-apply.$$
  fi
  rm -f /tmp/sv-apply.$$
else
  echo "- status:      up to date (or unpinned)"
fi
