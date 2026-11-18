#!/usr/bin/env bash
#
# apply-branding.sh — stamp Ctrlable identity on GRUB, the console, and SSH.
#
# Runs as root on the Debian appliance host; part of the RAUC-for-host image build and/or
# first-boot (SPEC-incus-migration.md §14c). Idempotent and safe to re-run.
#
# Contexts and the escaping gotcha:
#   * sshd `Banner` and /etc/motd print a file VERBATIM  -> use the pretty "standard" art.
#   * getty /etc/issue runs backslash ESCAPES (\n=host, \l=tty, ...) -> a slash-art would be
#     mangled, so /etc/issue uses the "#"-block art (no backslashes) + escape lines after it.
#
set -euo pipefail
PRETTY="Ctrlable Pro"
install -d -m755 /etc/ctrlable

# ---- verbatim art (SSH pre-auth banner + post-login MOTD) -------------------
cat > /etc/ctrlable/banner <<'ART'

  ____ _        _       _     _        ____
 / ___| |_ _ __| | __ _| |__ | | ___  |  _ \ _ __ ___
| |   | __| '__| |/ _` | '_ \| |/ _ \ | |_) | '__/ _ \
| |___| |_| |  | | (_| | |_) | |  __/ |  __/| | | (_) |
 \____|\__|_|  |_|\__,_|_.__/|_|\___| |_|   |_|  \___/

        Ctrlable Pro appliance  -  authorized use only

ART
cp /etc/ctrlable/banner /etc/motd

# ---- console /etc/issue (escape-safe "#" art + dynamic host/tty line) -------
cat > /etc/issue <<'ART'

 #####  ####### ######  #          #    ######  #       #######
#     #    #    #     # #         # #   #     # #       #
#          #    #     # #        #   #  #     # #       #
#          #    ######  #       #     # ######  #       #####
#          #    #   #   #       ####### #     # #       #
#     #    #    #    #  #       #     # #     # #       #
 #####     #    #     # ####### #     # ######  ####### #######
ART
# getty escapes (no backslashes exist in the art above, so these are unambiguous):
printf ' Ctrlable Pro \\n  tty:\\l  arch:\\m  \\t\n\n' >> /etc/issue
cp /etc/issue /etc/issue.net

# ---- SSH pre-auth banner (validated before any reload — never lock ourselves out) --
install -d -m755 /etc/ssh/sshd_config.d
echo 'Banner /etc/ctrlable/banner' > /etc/ssh/sshd_config.d/99-ctrlable-banner.conf
if sshd -t 2>/dev/null; then
  systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
else
  echo "WARN: sshd -t failed; left banner drop-in in place but did NOT reload"
fi

# ---- OS identity ------------------------------------------------------------
if [ -f /etc/os-release ]; then
  sed -i 's/^PRETTY_NAME=.*/PRETTY_NAME="Ctrlable Pro (Debian 13)"/' /etc/os-release || true
fi

# ---- GRUB branding (only where grub is actually installed) ------------------
if [ -f /etc/default/grub ] && command -v update-grub >/dev/null 2>&1; then
  if grep -q '^GRUB_DISTRIBUTOR=' /etc/default/grub; then
    sed -i 's/^GRUB_DISTRIBUTOR=.*/GRUB_DISTRIBUTOR="Ctrlable Pro"/' /etc/default/grub
  else
    echo 'GRUB_DISTRIBUTOR="Ctrlable Pro"' >> /etc/default/grub
  fi
  update-grub
  echo "grub: menu entries now read 'Ctrlable Pro GNU/Linux'"
else
  echo "grub: not present here (e.g. inside a container) — skipped"
fi

echo "branding applied: $PRETTY"
