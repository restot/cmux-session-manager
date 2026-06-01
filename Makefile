.PHONY: help list-active show snapshot list-snapshots diff validate prune restore restore-dry-run kill respawn install install-watch uninstall-watch watch-status

SCRIPT := python3 $(CURDIR)/cmux-sessions.py
SNAP_DIR := $(HOME)/.cmux-snapshots

# Resolve snapshot: full path passes through, bare name gets SNAP_DIR prefix
resolve_snap = $(if $(wildcard $(1)),$(1),$(if $(wildcard $(SNAP_DIR)/$(1)),$(SNAP_DIR)/$(1),$(if $(wildcard $(SNAP_DIR)/$(1).json),$(SNAP_DIR)/$(1).json,$(1))))

help: ## Show this help
	@echo "Usage: make <target>"
	@echo ""
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

list-active: ## List active Claude sessions across cmux workspaces
	@$(SCRIPT) list

# Double-quote (not single-quote) variable substitutions so values
# containing apostrophes (e.g. W="Bob's app") pass through cleanly.
# Double quotes still preserve whitespace; the only chars needing
# escaping inside "..." are $, `, ", and \ — none of which are
# expected in workspace or snapshot names.

show: ## Show detailed workspace info (W= workspace, F= snapshot file or name)
	@$(SCRIPT) show $(if $(W),-w "$(W)") $(if $(F),-f "$(call resolve_snap,$(F))")

snapshot: ## Capture state (W= workspace, N= name, e.g. make snapshot W=devops N=before-refactor)
	@$(SCRIPT) snapshot $(if $(W),-w "$(W)") $(if $(N),-n "$(N)")

list-snapshots: ## List all saved snapshots
	@$(SCRIPT) snapshots

diff: ## Compare snapshot vs live workspaces (F= snapshot)
	@$(SCRIPT) diff $(if $(F),-f "$(call resolve_snap,$(F))")

validate: ## Check snapshot health before restoring (F= snapshot, W= workspace)
	@$(SCRIPT) validate $(if $(F),-f "$(call resolve_snap,$(F))") $(if $(W),-w "$(W)")

prune: ## Delete old snapshots, keep last N (KEEP=10)
	@$(SCRIPT) prune --keep "$(or $(KEEP),10)"

restore-dry-run: ## Preview restore (W= workspace, F= snapshot, RC=1 commands, SA=1 skip active)
	@$(SCRIPT) restore --dry-run $(if $(W),-w "$(W)") $(if $(F),-f "$(call resolve_snap,$(F))") $(if $(RC),--run-commands) $(if $(SA),--skip-active)

restore: ## Restore from snapshot (W= workspace, F= snapshot, RC=1 commands, SA=1 skip active)
	@$(SCRIPT) restore $(if $(W),-w "$(W)") $(if $(F),-f "$(call resolve_snap,$(F))") $(if $(RC),--run-commands) $(if $(SA),--skip-active)

kill: ## Close a workspace with confirmation (requires W=)
	@test -n "$(W)" || (echo "Usage: make kill W=<workspace>"; exit 1)
	@$(SCRIPT) kill -w "$(W)"

respawn: ## Snapshot, kill, and restore a workspace (requires W=)
	@test -n "$(W)" || (echo "Usage: make respawn W=<workspace>"; exit 1)
	@$(SCRIPT) respawn -w "$(W)"

install: ## Symlink cmux-sessions into ~/bin
	@mkdir -p ~/bin
	@ln -sf $(CURDIR)/cmux-sessions.py ~/bin/cmux-sessions
	@echo "Installed: ~/bin/cmux-sessions"
	@echo "Make sure ~/bin is in your PATH"

install-watch: ## Install launchd agent that snapshots on every cmux state change (THROTTLE=30 KEEP=50)
	@$(SCRIPT) install-watch --throttle $(or $(THROTTLE),30) --auto-prune $(or $(KEEP),50)

uninstall-watch: ## Remove the launchd auto-snapshot agent
	@$(SCRIPT) uninstall-watch

watch-status: ## Show auto-snapshot agent state and recent snapshots
	@$(SCRIPT) watch-status
