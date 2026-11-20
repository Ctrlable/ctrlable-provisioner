# Ctrlable Pro appliance OS image — build orchestrator (§14b).
# Run on a DEBIAN 13 build host as root (needs mmdebstrap, loop devices, grub-efi, rauc).
SHELL := /bin/bash

WORK       ?= work
SUITE      ?= trixie
COMPATIBLE ?= ctrlable-pro-x86-64
VERSION    ?= $(shell date +%Y.%m.%d)
# Signing (bundle) vs keyring (baked trust anchor) are SEPARATE in prod:
#   RAUC_KEYRING = cert baked into the image to VERIFY every future OTA (the permanent trust anchor;
#                  changing it = reflash the fleet). Dev: the self-signed cert. Prod: the CA cert.
#   RAUC_CERT/_KEY = the cert+key that SIGN a bundle. Dev: same self-signed pair. Prod: a short-lived
#                  LEAF (chain file) + its key from CI secrets, which chains to RAUC_KEYRING.
# Dev (one box, one pair):  make keys && make all
# Prod (rotatable signer):  scripts/gen-prod-ca.sh keys/prod   (once; custody the keys per its notes)
#                           make all RAUC_KEYRING=keys/prod/ca-cert.pem \
#                                    RAUC_CERT=keys/prod/signing-cert.pem RAUC_KEY=keys/prod/signing-key.pem
RAUC_KEYRING ?= keys/dev-cert.pem
RAUC_CERT  ?= keys/dev-cert.pem
RAUC_KEY   ?= keys/dev-key.pem

export WORK SUITE COMPATIBLE VERSION RAUC_KEYRING RAUC_CERT RAUC_KEY

STAMP := $(WORK)/.rootfs.stamp

.PHONY: all keys prod-ca rootfs image bundle netboot clean help
help:
	@echo "targets: keys prod-ca rootfs image bundle all clean   (run as root on Debian 13)"
all: image bundle

keys:        ## one-time DEV signing keys (single self-signed pair; local builds only)
	scripts/gen-dev-keys.sh keys

prod-ca:     ## one-time PROD CA + rotatable leaf signer (custody the keys — see script output)
	scripts/gen-prod-ca.sh keys/prod

# rootfs is tracked by a stamp so dependent targets don't rebuild it every time.
rootfs: $(STAMP)
$(STAMP): scripts/build-rootfs.sh config/mmdebstrap/packages ../poc/incus-bootstrap.sh ../poc/branding/apply-branding.sh
	scripts/build-rootfs.sh
	@touch $(STAMP)

image: $(STAMP) ## assemble the flashable GPT A/B image -> $(WORK)/ctrlable-pro-$(VERSION).img
	scripts/pack-image.sh

bundle: $(STAMP) ## build the signed RAUC OTA bundle -> $(WORK)/ctrlable-pro-$(VERSION).raucb
	scripts/make-bundle.sh

netboot: image ## stage network-install (zstd image + Alpine writer + iPXE) under $(WORK)/netboot; pass BASE_URL=http://<ip>:<port> [SERVE=<port>]
	scripts/make-netboot.sh "$(BASE_URL)" $(if $(SERVE),--serve $(SERVE),)

clean:
	rm -rf $(WORK)
