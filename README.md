# cmux-session-manager

Snapshot and restore [cmux](https://cmux.com/) workspaces with Claude Code session resumption.

When cmux crashes or is restarted, this tool recreates your workspace layout — workspace colors, splits, every tab in a multi-tab pane, tab order, the focused tab, user-renamed tab titles, working directories, Claude sessions, and (optionally) running terminal commands — and resumes everything where it left off.

## How It Works

1. **Snapshot** reads cmux's session state file to capture windows, workspaces, panels, their layout (splits, orientations, tab order), and per-workspace metadata (`customColor`, `description`, `focusedPanelId`). Per-panel it captures the working directory, `customTitle` (only when the user renamed the tab), and `ttyName`.

2. It cross-references running Claude processes (`ps -eww`) by **TTY match against panel.ttyName** — so renamed Claude tabs are still detected. For each Claude panel, the session ID comes from the process's argv (`--session-id` / `--resume`) or, as a fallback, the newest `.jsonl` in `~/.claude/projects/<encoded-cwd>/`. For non-Claude terminal panels, it detects running foreground commands via process inspection.

3. **Restore** recreates the workspace structure using `cmux` CLI commands. Steps carry argv lists (no shell-string `.split()` pitfalls) and resolve workspace/surface/pane refs from the `OK ...` stdout of each `new-workspace` / `new-split` / `new-surface` call. The restore applies the workspace color via `workspace-action set-color`, creates additional tabs via `new-surface --pane <ref> --focus true`, restores user-renamed titles via `rename-tab` (sticky — overrides subsequent OSC title escapes), and ends with `focus-panel` on the originally-focused tab. Claude sessions resume with `claude --resume <session-id>`; terminal panels `cd` to their original directories. With `--run-commands` / `RC=1`, captured terminal commands are re-launched automatically.

## Quick Start

```bash
# Take a snapshot of all workspaces
make snapshot

# List active Claude sessions
make list-active

# Show detailed info for a workspace
make show W=myproject

# After a crash — preview the restore plan
make restore-dry-run W=myproject

# Restore a workspace (auto-executes inside cmux, prompts for confirmation)
make restore W=myproject

# Respawn: snapshot + kill + restore in one step
make respawn W=myproject
```

## Common Workflows

### Recover after a cmux crash

```bash
# See what you had running
make list-snapshots

# Inspect a specific snapshot to verify it has what you need
make show F=cmux-20260405-161401

# Preview the restore plan
make restore-dry-run

# Restore everything (requires typing 'yes' for multi-workspace)
make restore
```

### Respawn a misbehaving workspace

```bash
# Check current state
make show W=devops-work

# Snapshot, kill, and restore in one step
make respawn W=devops-work
```

### Restore a single workspace from an older snapshot

```bash
# List available snapshots
make list-snapshots

# Preview what it would do
make restore-dry-run W=devops-work F=cmux-20260405-161401

# Restore it
make restore W=devops-work F=cmux-20260405-161401
```

### Restore only closed workspaces from a snapshot

```bash
# See which workspaces from the snapshot are active vs closed
make diff

# Restore only the ones that aren't currently running
make restore SA=1

# Combine with a specific snapshot
make restore SA=1 F=cmux-20260405-161401
```

### Restore with dev servers and watchers

```bash
# Take a snapshot (captures running terminal commands)
make snapshot W=vibefocus

# Check what commands were captured
make show W=vibefocus F=latest

# Restore with commands auto-launched
make restore W=vibefocus RC=1
```

### Audit what's running across all workspaces

```bash
# Quick overview — Claude sessions and panel counts
make list-active

# Deep dive into a specific workspace
make show W=vibefocus
```

### Safe teardown before OS restart

```bash
# Snapshot everything
make snapshot

# Verify it captured your workspaces
make list-snapshots

# After reboot, restore from inside cmux
make restore
```

## Make Targets

```
make help               Show all available targets
make list-active        List active Claude sessions with git branches
make show               Show detailed workspace info (W= workspace, F= snapshot)
make snapshot           Capture state (W= workspace, N= name)
make list-snapshots     List all saved snapshots with workspace names
make diff               Compare snapshot vs live workspaces (F= snapshot)
make validate           Check snapshot health before restoring (F= snapshot, W= workspace)
make prune              Delete old snapshots, keep last N (KEEP=10)
make restore-dry-run    Preview restore (W= workspace, F= snapshot, RC=1, SA=1)
make restore            Restore from snapshot (W= workspace, F= snapshot, RC=1, SA=1)
make kill               Close a workspace with confirmation (requires W=)
make respawn            Snapshot, kill, and restore a workspace (requires W=)
make install            Symlink cmux-sessions into ~/bin
make install-watch      Install launchd agent for auto-snapshot on state change (THROTTLE=30 KEEP=50)
make uninstall-watch    Remove the auto-snapshot agent
make watch-status       Show agent state and recent snapshots
```

### Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `W=`     | Workspace name filter (case-insensitive substring) | `W=devops-work` |
| `F=`     | Snapshot file (bare name, with .json, or full path) | `F=cmux-20260405-161401` |
| `N=`     | Named snapshot (instead of timestamp) | `N=before-refactor` |
| `RC=1`   | Re-run captured terminal commands on restore | `RC=1` |
| `SA=1`   | Skip already-active workspaces on restore | `SA=1` |
| `KEEP=`  | Number of snapshots to keep when pruning (default: 10) | `KEEP=5` |
| `THROTTLE=` | Min seconds between auto-snapshots (default: 30) | `THROTTLE=60` |

## CLI Commands

```
cmux-sessions list                            Show Claude sessions with git branches
cmux-sessions show -w myproject               Show detailed workspace info (live)
cmux-sessions show -w myproject -f snap.json  Show detailed workspace info (snapshot)
cmux-sessions snapshot                        Save current state (all workspaces)
cmux-sessions snapshot -w myproject           Snapshot only matching workspace
cmux-sessions snapshot -n before-refactor     Save with a name instead of timestamp
cmux-sessions snapshots                       List all saved snapshots
cmux-sessions diff                            Compare snapshot vs live workspaces
cmux-sessions diff -f snap.json               Compare specific snapshot vs live
cmux-sessions validate                        Check snapshot health before restoring
cmux-sessions validate -f snap.json -w proj   Validate specific snapshot/workspace
cmux-sessions prune                           Delete old snapshots (keep last 10)
cmux-sessions prune --keep 5                  Keep last 5 snapshots
cmux-sessions restore --dry-run               Preview what would be restored
cmux-sessions restore -w myproject            Restore only matching workspace
cmux-sessions restore --skip-active           Restore only closed workspaces
cmux-sessions restore --run-commands          Re-run saved terminal commands
cmux-sessions kill -w myproject               Close a workspace (with confirmation)
cmux-sessions kill -w myproject -y            Close without confirmation
cmux-sessions respawn -w myproject            Snapshot, kill, and restore in one step
cmux-sessions install-watch                   Install launchd auto-snapshot agent
cmux-sessions install-watch --throttle 60     Min seconds between snapshots (default 30)
cmux-sessions install-watch --auto-prune 100  Keep last N snapshots (default 50)
cmux-sessions uninstall-watch                 Remove the auto-snapshot agent
cmux-sessions watch-status                    Show agent state and recent snapshots
cmux-sessions snapshot --auto-prune 50        After snapshotting, prune to last 50
```

## Features

### What gets preserved

| Attribute | Source | How it's restored |
|---|---|---|
| Workspace name | `customTitle` (or focused tab / cwd basename) | `cmux new-workspace --name` |
| Workspace cwd | `currentDirectory` | `cmux new-workspace --cwd` |
| Workspace description | `description` | `cmux new-workspace --description` |
| **Workspace color** | `customColor` | `cmux workspace-action --action set-color` |
| **Tab order in a pane** | `layout.panelIds` order | `cmux new-surface --pane <ref> --focus true` per tab |
| **Every tab in a multi-tab pane** | all of `layout.panelIds` | one `new-surface` per panel |
| **Focused tab** | workspace `focusedPanelId` | `cmux focus-panel` at end of restore |
| **User-renamed tab titles** | panel `customTitle` (only when user renamed) | `cmux rename-tab` — sticky, overrides Claude's OSC status updates |
| Claude session | TTY of running `claude` proc → session id from argv or `~/.claude/projects/<cwd>/<id>.jsonl` | `claude --resume <id>` typed into the tab |
| Saved terminal command | foreground child of the panel's shell | typed into the tab when `--run-commands` / `RC=1` |
| Split topology | `layout` split tree | sequential `cmux new-split` calls (best-effort — see Limitations) |

### Auto-snapshot on state change

`make install-watch` installs a macOS launchd agent that snapshots automatically whenever cmux writes its session state (new workspace, rename, color change, tab reorder, split, etc.). No long-running process needed.

```bash
make install-watch                 # 30s throttle, keep last 50 snapshots
make install-watch THROTTLE=60     # at most one snapshot per 60s
make install-watch KEEP=100        # auto-prune to last 100 after each snapshot
make watch-status                  # show agent state + recent snapshots
make uninstall-watch
```

How it works:

- Installs `~/Library/LaunchAgents/com.cmux-sessions.auto-snapshot.plist` with a `WatchPaths` entry on cmux's session file.
- launchd fires `cmux-sessions snapshot --auto-prune <N>` on every write.
- `ThrottleInterval` debounces: at most one snapshot per N seconds no matter how many writes occur in that window.
- Logs to `~/.cmux-snapshots/auto-watch.log`.
- The script is copied to `~/.cmux-snapshots/cmux-sessions-watch.py` at install time. Reason: macOS TCC blocks launchd-spawned processes from reading `~/Documents`, `~/Desktop`, and `~/Downloads` without Full Disk Access. After editing the source, re-run `make install-watch` to refresh the copy.
- Survives reboot — launch agents load at user session start.

### Workspace Show

`make show W=myproject` displays every panel with its type, working directory, and session/command info:

```
Workspace: myproject
Directory: ~/Development/myproject
Panels:    3

  Panel 1: [claude] Implement auth middleware
    cwd: ~/Development/myproject
    session: a1b2c3d4-5678-90ab-cdef-1234567890ab
    pid: 12345
    status: running

  Panel 2: [terminal] Terminal
    cwd: ~/Development/myproject
    command: npm run dev

  Panel 3: [terminal] Terminal
    cwd: ~/Development/myproject/docs
```

Works with `-f` to inspect snapshot contents: `make show W=myproject F=cmux-20260405-161401`

### Terminal Command Capture

Snapshots detect foreground processes running in non-Claude terminal panels (dev servers, watchers, REPLs, etc.) and save them as `lastCommand`. On restore:

- **Default**: commands are shown as hints but not executed — panels just `cd` to the correct directory
- **`RC=1`** / `--run-commands`: commands are re-launched automatically

### Smart Restore

Restore auto-detects whether you're inside cmux:

- **Inside cmux**: shows the plan, prompts for confirmation, then executes directly
- **Outside cmux**: generates a restore shell script to run from a cmux terminal

Safety checks before restoring:

- **Duplicate detection**: warns if target workspaces are already open, requires typing `force` to continue
- **Multi-workspace guard**: restoring all workspaces (no `W=` filter) requires typing `yes`
- **Single workspace**: standard `y/N` confirmation

### Respawn

`make respawn W=myproject` is the all-in-one workflow:

1. Snapshots the workspace
2. Prompts for confirmation
3. Kills it via `cmux close-workspace`
4. Restores from the fresh snapshot

### Claude Detection

A panel is classified as Claude if any of the following hold:

1. **TTY match** — a running `claude` process is attached to the panel's `ttyName`. Most reliable signal; catches manually-renamed tabs that title heuristics miss.
2. Title starts with `✳` (Claude idle) or a braille spinner glyph in `U+2800–U+28FF` (Claude working — `⠂`, `⠐`, etc.).
3. Title contains the literal word `Claude`.

Session IDs are then resolved in priority order:

1. Running process's argv `--session-id <id>` or `--resume <id>`
2. `sessions-index.json` in `~/.claude/projects/<encoded-cwd>/`
3. Most recent `.jsonl` session file by modification time (fallback when the index is missing)

### Named Snapshots

Give snapshots meaningful names instead of timestamps:

```bash
make snapshot W=devops N=before-refactor
# Saves as ~/.cmux-snapshots/cmux-before-refactor.json

# Restore from it later
make restore W=devops F=before-refactor
```

### Pre-restore Validation

Check that a snapshot is still valid before restoring:

```bash
make validate F=cmux-20260405-161401
```

Output shows PASS/FAIL for each panel's directory and Claude session:

```
WORKSPACE  TYPE        DIRECTORY                   DIR   SESSION
---------  ----------  --------------------------  ----  -------
myproject  [claude]    ~/Development/myproject      PASS  PASS
myproject  [terminal]  ~/Development/myproject      PASS  -
myproject  [terminal]  ~/Development/myproject/api  PASS  -
```

### Snapshot Pruning

Snapshots accumulate over time. Clean up old ones:

```bash
make prune              # Keep last 10
make prune KEEP=5       # Keep last 5
```

### Git Branch Display

`make list-active` includes a BRANCH column showing the current git branch for each Claude session's working directory.

## Installation

```bash
make install    # Symlinks to ~/bin/cmux-sessions
```

Or add to your PATH manually:
```bash
export PATH="$HOME/Development/cmux-sessions:$PATH"
```

## File Locations

| Path | Purpose |
|------|---------|
| `~/.cmux-snapshots/` | Snapshot storage directory |
| `~/.cmux-snapshots/latest.json` | Most recent snapshot (used by default restore) |
| `~/.cmux-snapshots/restore.sh` | Generated restore script (outside cmux fallback) |
| `~/.cmux-snapshots/cmux-sessions-watch.py` | Copy of the script the launchd agent invokes (created by `install-watch`) |
| `~/.cmux-snapshots/auto-watch.log` | Stdout/stderr from each auto-snapshot run |
| `~/Library/LaunchAgents/com.cmux-sessions.auto-snapshot.plist` | launchd agent that fires on cmux state writes |
| `~/Library/Application Support/cmux/session-com.cmuxterm.app.json` | cmux's live session state (read-only) |
| `~/.claude/projects/` | Claude Code session index and history files |

## Automating Snapshots

Preferred: `make install-watch` (see [Auto-snapshot on state change](#auto-snapshot-on-state-change)) — fires whenever cmux writes its state, throttled and auto-pruned.

If you want a fixed cadence instead, a cron entry still works as a backstop:

```bash
# Every 30 minutes
*/30 * * * * python3 ~/Development/cmux-sessions/cmux-sessions.py snapshot --auto-prune 50 2>/dev/null
```

## Limitations

- **Kill/respawn must be run from inside cmux** — `cmux close-workspace` requires a socket connection only available to cmux child processes. Snapshot, list, and show work from anywhere.
- **Nested splits are best-effort** — cmux's `new-split` can only target a single pane, so arbitrary tree topologies like `(A|B)/C` can't be reconstructed exactly. Each sibling subtree splits off the most recently created leaf, giving a close approximation (e.g. `A | (B/C)`). Single-pane and single-axis layouts reconstruct exactly. Divider positions may shift either way.
- **Tab title restoration only when explicitly renamed** — cmux's `rename-tab` is sticky and overrides process OSC title updates. Restoring a tab's last-displayed title would freeze it on stale process state, so the restorer only re-applies titles where `panel.customTitle` is set in the cmux session file (i.e. the user manually renamed the tab). Process-derived titles (Claude's `✳ Reading ...`, cwd defaults) reconstruct naturally from the running process.
- **Terminal command capture is best-effort** — it detects foreground child processes of shell sessions. Idle shells (at a prompt) have no command to capture. Background jobs and piped commands may not be detected. In-memory state (vim buffers, REPL history) is never recovered — only Claude sessions, which have an on-disk `.jsonl` log, fully resume.
- **Session IDs are point-in-time** — if a Claude session is ended and a new one started between snapshot and restore, the old session ID will be used. Use `make install-watch` to keep snapshots fresh automatically.

## Requirements

- Python 3.8+ (no third-party packages)
- cmux (macOS)
- Claude Code CLI (optional — only needed for `claude --resume` on restore)
- `launchctl` (macOS, used by `install-watch`; not required for manual snapshots)
