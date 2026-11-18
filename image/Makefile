# Ctrlable Pro appliance OS image — build orchestrator (§14b).
# Run on a DEBIAN 13 build host as root (needs mmdebstrap, loop devices, grub-efi, rauc).
SHELL := /bin/bash

WORK       ?= work
SUITE      ?= trixie
COMPATIBLE ?= ctrlable-pro-x86-64
VERSION    ?= $(shell date +%Y.%m.%d)
# Prod: pass RAUC_CERT=/secrets/cert.pem RAUC_KEY=/secrets/key.pem (CI secrets). Dev: `make keys`.
RAUC_CERT  ?= keys/dev-cert.pem
RAUC_KEY   ?= keys/dev-key.pem

export WORK SUITE COMPATIBLE VERSION RAUC_CERT RAUC_KEY

STAMP := $(WORK)/.rootfs.stamp

.PHONY: all keys rootfs image bundle clean help
help:
	@echo "targets: keys rootfs image bundle all clean   (run as root on Debian 13)"
all: image bundle

keys:        ## one-time DEV signing keys (prod keys come from CI secrets)
	scripts/gen-dev-keys.sh keys

# rootfs is tracked by a stamp so dependent targets don't rebuild it every time.
rootfs: $(STAMP)
$(STAMP): scripts/build-rootfs.sh config/mmdebstrap/packages ../poc/incus-bootstrap.sh ../poc/branding/apply-branding.sh
	scripts/build-rootfs.sh
	@touch $(STAMP)

image: $(STAMP) ## assemble the flashable GPT A/B image -> $(WORK)/ctrlable-pro-$(VERSION).img
	scripts/pack-image.sh

bundle: $(STAMP) ## build the signed RAUC OTA bundle -> $(WORK)/ctrlable-pro-$(VERSION).raucb
	scripts/make-bundle.sh

clean:
	rm -rf $(WORK)
