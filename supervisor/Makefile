# Ctrlable Pro — Supervisor fork-tracking harness.
# Goal: ship Supervisor that behaves like HAOS inside an LXC while staying
# continuously rebaseable on vanilla (§6a of SPEC-incus-migration.md). The whole
# point is a MINIMAL, isolated patch stack — ideally empty (Layer-0 env parity).
.PHONY: all sync apply report build clean help
help:
	@echo "sync    - fetch vanilla upstream, show diff since our pinned tag (read-only)"
	@echo "apply   - apply our patch stack onto the pinned tag; report any patch that won't apply"
	@echo "report  - CI/human report: newest release + does our stack still apply"
	@echo "build   - apply stack + build the Ctrlable Pro Supervisor image"
	@echo "clean   - remove the scratch upstream clone (.work/)"
sync:   ; @bash scripts/sync.sh
apply:  ; @bash scripts/apply.sh
report: ; @bash scripts/report.sh
build:  ; @bash scripts/build.sh
clean:  ; @rm -rf .work && echo "removed .work/"
