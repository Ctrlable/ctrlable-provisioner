# Show the appliance claim code at interactive login (so a dealer/tech can read it off the host).
case $- in *i*)
  if [ -s /etc/ctrlable/claim-code ] && command -v ctrlable-claim >/dev/null 2>&1; then
    ctrlable-claim 2>/dev/null || true
  fi
;; esac
