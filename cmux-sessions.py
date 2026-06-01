#!/usr/bin/env python3
"""
Snapshot and restore cmux workspaces with Claude Code sessions.

Captures the full cmux workspace layout (from cmux's session file) and
cross-references running Claude processes to map session IDs. On restore,
recreates workspaces and resumes Claude sessions.

Usage:
  cmux-sessions snapshot                  # Save current state (all workspaces)
  cmux-sessions snapshot -w myproject     # Snapshot only matching workspace
  cmux-sessions snapshot -o state.json    # Save to specific file
  cmux-sessions list                      # Show active Claude sessions
  cmux-sessions diff                      # Compare snapshot vs live workspaces
  cmux-sessions restore                   # Restore from latest snapshot
  cmux-sessions restore -w myproject      # Restore only matching workspace
  cmux-sessions restore -f state.json     # Restore from specific file
  cmux-sessions restore --skip-active     # Restore only closed workspaces
  cmux-sessions restore --dry-run         # Preview what would be restored
"""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

CMUX_SESSION_FILE = os.path.expanduser(
    "~/Library/Application Support/cmux/session-com.cmuxterm.app.json"
)
CLAUDE_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
SNAPSHOT_DIR = os.path.expanduser("~/.cmux-snapshots")

WATCH_LABEL = "com.cmux-sessions.auto-snapshot"
WATCH_PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{WATCH_LABEL}.plist")
WATCH_LOG = os.path.join(SNAPSHOT_DIR, "auto-watch.log")


# ── Process discovery ────────────────────────────────────────


_CLAUDE_BIN_RE = re.compile(r"(^|/)claude(\s|$)")


def get_claude_processes():
    """Get all running Claude processes.

    Returns a list of dicts: {pid, tty, session_id, cwd}.
    session_id is None when --session-id/--resume is absent (fresh sessions).
    cwd is resolved lazily — only callers that need it call get_process_cwd().
    """
    try:
        # -ww: don't truncate the command field. Default BSD ps truncates to
        # terminal width, which loses argv past column ~80.
        result = subprocess.run(
            ["ps", "-eww", "-o", "pid,tty,command"],
            capture_output=True, text=True, timeout=5
        )
    except Exception:
        return []

    processes = []
    for line in result.stdout.splitlines()[1:]:
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        pid, tty, cmd = parts[0], parts[1], parts[2]

        # Filter to the claude binary specifically. The literal substring
        # "claude" is too loose — Claude's own system prompt embeds the word
        # "grep" (and others), so prior heuristics misfired both ways.
        if not _CLAUDE_BIN_RE.search(cmd):
            continue
        # Skip processes without a controlling TTY — those are shell snapshots,
        # eval wrappers, the agent harness itself, etc.
        if tty in ("?", "??"):
            continue

        session_id = None
        for flag in ("--session-id", "--resume"):
            m = re.search(rf"{flag}\s+(\S+)", cmd)
            if m:
                session_id = m.group(1)
                break

        processes.append({
            "pid": int(pid),
            "tty": tty,
            "session_id": session_id,
            "cwd": None,  # lazy
        })

    return processes


def get_process_cwd(pid):
    """Get the working directory of a process."""
    try:
        result = subprocess.run(
            ["lsof", "-p", str(pid)],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 9 and parts[3] == "cwd":
                return parts[-1]
    except Exception:
        pass
    return None


def get_git_branch(directory):
    """Get the current git branch for a directory, walking up to 5 parent levels."""
    d = directory
    for _ in range(6):
        head = os.path.join(d, ".git", "HEAD")
        if os.path.isfile(head):
            try:
                with open(head) as f:
                    content = f.read().strip()
                if content.startswith("ref: refs/heads/"):
                    return content[16:]
                return content[:12]  # detached HEAD — show short hash
            except Exception:
                return None
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


SHELL_NAMES = {"bash", "zsh", "fish", "login", "sshd", "sh"}
NOISE_CMDS = {"sleep", "ps", "head", "tail", "read", "cat", "grep", "awk", "sed"}


def _is_claude_title(title):
    """Decide if a panel title is owned by a Claude session.

    Claude rewrites its tab title to:
      - "✳ <summary>" (U+2733 sparkle) when idle/ready
      - "<braille spinner> <summary>" (U+2800-U+28FF) when working
      - Or contains the literal "Claude" (e.g. "Claude Code")
    """
    if not title:
        return False
    if "Claude" in title:
        return True
    c = title[0]
    if c == "✳":
        return True
    if 0x2800 <= ord(c) <= 0x28FF:
        return True
    return False


def is_claude_panel(panel):
    return _is_claude_title(panel.get("title", "") if panel else "")


def _clean_panel_title(title):
    """Strip leading Claude-status glyphs from a panel title for display."""
    if not title:
        return ""
    s = title
    while s and (s[0] == "✳" or 0x2800 <= ord(s[0]) <= 0x28FF):
        s = s[1:].lstrip()
    return s or title  # if we stripped everything, return original


def get_terminal_commands():
    """Discover foreground commands running in terminal shells.

    Returns a dict: cwd -> [command_string, ...] for non-Claude, non-shell processes
    whose parent is a shell.
    """
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid,ppid,comm"],
            capture_output=True, text=True, timeout=5
        )
    except Exception:
        return {}

    # Build parent lookup
    children = {}  # ppid -> [(pid, comm)]
    for line in result.stdout.splitlines()[1:]:
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        pid, ppid, comm = parts[0], parts[1], parts[2]
        children.setdefault(ppid, []).append((pid, comm))

    # Find shell processes and their foreground children
    commands_by_cwd = {}
    for line in result.stdout.splitlines()[1:]:
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        shell_pid, _, comm = parts[0], parts[1], parts[2]

        # Only look at shells
        base_comm = os.path.basename(comm).lstrip("-")
        if base_comm not in SHELL_NAMES:
            continue

        # Check children of this shell
        for child_pid, child_comm in children.get(shell_pid, []):
            base_child = os.path.basename(child_comm)
            # Skip noise and other shells
            if base_child in NOISE_CMDS or base_child in SHELL_NAMES:
                continue
            # Skip Claude processes (handled separately)
            if "claude" in child_comm.lower():
                continue

            # Get the full command line
            try:
                args_result = subprocess.run(
                    ["ps", "-p", child_pid, "-o", "args="],
                    capture_output=True, text=True, timeout=3
                )
                full_cmd = args_result.stdout.strip()
            except Exception:
                full_cmd = child_comm

            if not full_cmd:
                continue

            # Get parent shell's cwd
            cwd = get_process_cwd(shell_pid)
            if cwd:
                commands_by_cwd.setdefault(cwd, []).append(full_cmd)

    return commands_by_cwd


# ── Claude session index ─────────────────────────────────────


def get_claude_session_info(project_path, session_id):
    """Look up Claude session metadata from the sessions index."""
    encoded = project_path.replace("/", "-")
    index_path = os.path.join(CLAUDE_PROJECTS_DIR, encoded, "sessions-index.json")

    if os.path.exists(index_path):
        try:
            with open(index_path) as f:
                data = json.load(f)
            for entry in data.get("entries", []):
                if entry.get("sessionId") == session_id:
                    return {
                        "summary": entry.get("summary", ""),
                        "firstPrompt": entry.get("firstPrompt", ""),
                        "modified": entry.get("modified", ""),
                        "messageCount": entry.get("messageCount", 0),
                        "gitBranch": entry.get("gitBranch", ""),
                    }
        except Exception:
            pass

    # Fallback: check if the session .jsonl file exists directly
    project_dir = os.path.join(CLAUDE_PROJECTS_DIR, encoded)
    jsonl_path = os.path.join(project_dir, f"{session_id}.jsonl")
    if os.path.exists(jsonl_path):
        try:
            mtime = os.path.getmtime(jsonl_path)
            modified = datetime.fromtimestamp(mtime).isoformat()
            return {
                "summary": "",
                "firstPrompt": "",
                "modified": modified,
                "messageCount": 0,
                "gitBranch": "",
                "note": "from-file",
            }
        except Exception:
            pass

    return None


def find_latest_claude_session(project_path):
    """Find the most recent Claude session for a given project path."""
    encoded = project_path.replace("/", "-")
    index_path = os.path.join(CLAUDE_PROJECTS_DIR, encoded, "sessions-index.json")

    if os.path.exists(index_path):
        try:
            with open(index_path) as f:
                data = json.load(f)
            entries = data.get("entries", [])
            if entries:
                entries.sort(key=lambda e: e.get("modified", ""), reverse=True)
                return entries[0]
        except Exception:
            pass

    # Fallback: scan .jsonl files by modification time
    project_dir = os.path.join(CLAUDE_PROJECTS_DIR, encoded)
    if not os.path.isdir(project_dir):
        return None

    try:
        jsonl_files = sorted(
            Path(project_dir).glob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if jsonl_files:
            newest = jsonl_files[0]
            mtime = newest.stat().st_mtime
            return {
                "sessionId": newest.stem,
                "summary": "",
                "modified": datetime.fromtimestamp(mtime).isoformat(),
                "messageCount": 0,
                "gitBranch": "",
                "note": "from-file",
            }
    except Exception:
        pass

    return None


# ── cmux state ───────────────────────────────────────────────


def load_cmux_session():
    """Load the cmux session state file."""
    if not os.path.exists(CMUX_SESSION_FILE):
        print(f"Error: cmux session file not found at {CMUX_SESSION_FILE}", file=sys.stderr)
        sys.exit(1)

    with open(CMUX_SESSION_FILE) as f:
        return json.load(f)


def parse_layout(layout):
    """Recursively parse a cmux layout tree into a serializable description."""
    layout_type = layout.get("type")
    if layout_type == "pane":
        pane = layout.get("pane", {})
        return {
            "type": "pane",
            "panelIds": pane.get("panelIds", []),
            "selectedPanelId": pane.get("selectedPanelId"),
        }
    elif layout_type == "split":
        split = layout.get("split", {})
        return {
            "type": "split",
            "orientation": split.get("orientation", "vertical"),
            "dividerPosition": split.get("dividerPosition", 0.5),
            "first": parse_layout(split.get("first", {})),
            "second": parse_layout(split.get("second", {})),
        }
    return {"type": "unknown"}


def collect_layout_pane_ids(layout):
    """Walk a parsed layout tree and return panel IDs in left-to-right / top-to-bottom order."""
    if layout.get("type") == "pane":
        return layout.get("panelIds", [])
    elif layout.get("type") == "split":
        return collect_layout_pane_ids(layout.get("first", {})) + collect_layout_pane_ids(layout.get("second", {}))
    return []


def layout_to_splits(layout):
    """Convert a layout tree into a sequence of split operations.

    Returns a list of dicts:
      {"panelIds": [...], "direction": "right"|"down", "position": float}
    The first entry is the initial pane (no split needed).
    """
    result = []
    _walk_layout(layout, result, is_root=True)
    return result


def _walk_layout(layout, result, is_root=False):
    if layout.get("type") == "pane":
        result.append({
            "panelIds": layout.get("panelIds", []),
            "direction": None,  # filled in by parent split
        })
    elif layout.get("type") == "split":
        orientation = layout.get("orientation", "vertical")
        direction = "right" if orientation == "vertical" else "down"

        _walk_layout(layout.get("first", {}), result, is_root=False)
        # Mark the next entry as needing a split
        start = len(result)
        _walk_layout(layout.get("second", {}), result, is_root=False)
        if start < len(result):
            result[start]["direction"] = direction


# ── Commands ─────────────────────────────────────────────────


def _match_workspace(ws, ws_idx, filter_name):
    """Check if a workspace matches the given filter (title or index)."""
    if filter_name is None:
        return True
    # Match by index
    if filter_name.isdigit() and int(filter_name) == ws_idx:
        return True
    # Match by title (case-insensitive substring)
    cwd = ws.get("currentDirectory", "")
    title = ws.get("customTitle") or ws.get("title") or (os.path.basename(cwd) if cwd else f"workspace-{ws_idx}")
    return filter_name.lower() in title.lower()


def cmd_snapshot(args):
    """Capture current cmux + Claude state."""
    cmux_data = load_cmux_session()
    claude_procs = get_claude_processes()
    terminal_cmds = get_terminal_commands()

    # Index by TTY (precise 1:1 with cmux panels via panel.ttyName) and by
    # cwd (fallback when ttyName is missing/unmapped).
    claude_by_tty = {p["tty"]: p for p in claude_procs if p.get("tty")}
    claude_by_cwd = {}
    for proc in claude_procs:
        # Resolve cwd lazily — only needed for the fallback path.
        if proc.get("cwd") is None:
            proc["cwd"] = get_process_cwd(proc["pid"])
        if proc.get("cwd"):
            claude_by_cwd.setdefault(proc["cwd"], []).append(proc)

    ws_filter = getattr(args, "workspace", None)

    snapshot_data = {
        "version": 3,
        "timestamp": datetime.now().isoformat(),
        "windows": [],
    }

    matched_any = False

    for win_idx, win in enumerate(cmux_data.get("windows", [])):
        window = {"index": win_idx, "workspaces": []}

        tm = win.get("tabManager", {})
        selected_idx = tm.get("selectedWorkspaceIndex", 0)

        for ws_idx, ws in enumerate(tm.get("workspaces", [])):
            if not _match_workspace(ws, ws_idx, ws_filter):
                continue
            matched_any = True
            cwd = ws.get("currentDirectory", "")
            title = ws.get("customTitle") or ws.get("title") or (os.path.basename(cwd) if cwd else f"workspace-{ws_idx}")
            layout = parse_layout(ws.get("layout", {}))

            workspace = {
                "index": ws_idx,
                "title": title,
                "color": ws.get("customColor", "") or "",
                "description": ws.get("description", "") or "",
                "gitBranch": ws.get("gitBranch", "") or "",
                "cwd": cwd,
                "isSelected": ws_idx == selected_idx,
                "isPinned": ws.get("isPinned", False),
                "focusedPanelId": ws.get("focusedPanelId") or "",
                "layout": layout,
                "panels": [],
            }

            # Iterate panels in layout (tab-display) order, falling back to raw
            # panels order for any panel not referenced in the layout.
            raw_panels = ws.get("panels", [])
            by_id = {p.get("id"): p for p in raw_panels}
            ordered_ids = collect_layout_pane_ids(layout)
            seen = set()
            iter_panels = []
            for pid in ordered_ids:
                p = by_id.get(pid)
                if p is not None and pid not in seen:
                    iter_panels.append(p)
                    seen.add(pid)
            for p in raw_panels:
                pid = p.get("id")
                if pid not in seen:
                    iter_panels.append(p)
                    if pid:
                        seen.add(pid)

            for panel in iter_panels:
                panel_id = panel.get("id", "")
                panel_dir = panel.get("directory", cwd)
                panel_title = panel.get("title", "")
                panel_type = panel.get("type", "terminal")
                is_pinned = panel.get("isPinned", False)
                terminal = panel.get("terminal", {})
                terminal_cwd = terminal.get("workingDirectory", panel_dir)
                tty_name = panel.get("ttyName") or ""

                # Detect Claude via running process on the panel's TTY first —
                # this catches sessions whose tab was manually renamed and
                # is more reliable than title-glyph heuristics.
                proc = claude_by_tty.get(tty_name)
                is_claude = proc is not None or _is_claude_title(panel_title)
                detection = None
                claude_session = None

                if proc is not None:
                    detection = "tty"
                    sid = proc.get("session_id")
                    if not sid and terminal_cwd:
                        # Fresh session (no --resume in argv): resolve via
                        # the .jsonl index in the panel's directory.
                        latest = find_latest_claude_session(terminal_cwd)
                        if latest:
                            sid = latest.get("sessionId")
                    claude_session = {
                        "session_id": sid,
                        "pid": proc["pid"],
                    }
                    if sid and terminal_cwd:
                        meta = get_claude_session_info(terminal_cwd, sid)
                        if meta:
                            claude_session["summary"] = meta.get("summary", "")
                            claude_session["gitBranch"] = meta.get("gitBranch", "")
                elif is_claude and terminal_cwd:
                    # Title-glyph detection without a running TTY-matched proc.
                    # Try cwd-based match (older heuristic), then fall back to
                    # the filesystem index.
                    matches = claude_by_cwd.get(terminal_cwd, [])
                    if matches:
                        proc2 = matches.pop(0)
                        detection = "cwd"
                        claude_session = {
                            "session_id": proc2.get("session_id"),
                            "pid": proc2["pid"],
                        }
                        sid = proc2.get("session_id")
                        if not sid:
                            latest = find_latest_claude_session(terminal_cwd)
                            if latest:
                                sid = latest.get("sessionId")
                                claude_session["session_id"] = sid
                        if sid:
                            meta = get_claude_session_info(terminal_cwd, sid)
                            if meta:
                                claude_session["summary"] = meta.get("summary", "")
                                claude_session["gitBranch"] = meta.get("gitBranch", "")
                    else:
                        latest = find_latest_claude_session(terminal_cwd)
                        if latest:
                            detection = "index"
                            claude_session = {
                                "session_id": latest["sessionId"],
                                "pid": None,
                                "summary": latest.get("summary", ""),
                                "gitBranch": latest.get("gitBranch", ""),
                                "note": "from-index",
                            }

                # Capture running command for non-Claude terminal panels
                last_command = None
                if not is_claude and terminal_cwd:
                    cmds = terminal_cmds.get(terminal_cwd, [])
                    if cmds:
                        last_command = cmds.pop(0)  # consume to avoid duplicates

                # customTitle is set by cmux ONLY when the user explicitly
                # renamed the tab. Process-derived titles (Claude OSC status,
                # cwd-default "…/some/dir") leave it absent. We restore only
                # this — anything else would stomp on Claude's live status.
                custom_title = (panel.get("customTitle") or "").strip()

                panel_data = {
                    "id": panel_id,
                    "title": panel_title,
                    "type": panel_type,
                    "directory": terminal_cwd,
                    "isPinned": is_pinned,
                    "isClaude": is_claude,
                }
                if custom_title:
                    panel_data["customTitle"] = custom_title
                if tty_name:
                    panel_data["ttyName"] = tty_name
                if claude_session:
                    if detection:
                        claude_session["detectedVia"] = detection
                    panel_data["claudeSession"] = claude_session
                if last_command:
                    panel_data["lastCommand"] = last_command

                workspace["panels"].append(panel_data)

            window["workspaces"].append(workspace)

        if window["workspaces"]:
            snapshot_data["windows"].append(window)

    if ws_filter and not matched_any:
        print(f"Error: No workspace matching '{ws_filter}' found.", file=sys.stderr)
        sys.exit(1)

    # Save snapshot
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    if args.output:
        out_path = args.output
    elif getattr(args, "name", None):
        out_path = os.path.join(SNAPSHOT_DIR, f"cmux-{args.name}.json")
    else:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_path = os.path.join(SNAPSHOT_DIR, f"cmux-{ts}.json")

    with open(out_path, "w") as f:
        json.dump(snapshot_data, f, indent=2)

    # Also symlink as "latest"
    latest_path = os.path.join(SNAPSHOT_DIR, "latest.json")
    with open(latest_path, "w") as f:
        json.dump(snapshot_data, f, indent=2)

    # Summary
    total_ws = sum(len(w["workspaces"]) for w in snapshot_data["windows"])
    total_panels = sum(
        len(ws["panels"])
        for w in snapshot_data["windows"]
        for ws in w["workspaces"]
    )
    total_claude = sum(
        1 for w in snapshot_data["windows"]
        for ws in w["workspaces"]
        for p in ws["panels"] if p.get("isClaude")
    )
    claude_with_session = sum(
        1 for w in snapshot_data["windows"]
        for ws in w["workspaces"]
        for p in ws["panels"] if p.get("claudeSession")
    )

    print(f"Snapshot saved: {out_path}")
    if ws_filter:
        print(f"  Filter:           '{ws_filter}'")
    print(f"  Windows:          {len(snapshot_data['windows'])}")
    print(f"  Workspaces:       {total_ws}")
    print(f"  Panels:           {total_panels}")
    print(f"  Claude panels:    {total_claude} ({claude_with_session} with session IDs)")

    # Auto-prune after snapshotting (used by the launchd watcher to avoid
    # unbounded snapshot accumulation).
    auto_prune = getattr(args, "auto_prune", None)
    if auto_prune is not None and auto_prune > 0:
        prune_args = argparse.Namespace(keep=auto_prune)
        cmd_prune(prune_args)


def cmd_list(args):
    """List active Claude sessions across all cmux workspaces."""
    cmux_data = load_cmux_session()
    claude_procs = get_claude_processes()
    claude_by_tty = {p["tty"]: p for p in claude_procs if p.get("tty")}

    rows = []
    for win in cmux_data.get("windows", []):
        for ws in win.get("tabManager", {}).get("workspaces", []):
            ws_title = ws.get("customTitle") or ws.get("title") or os.path.basename(ws.get("currentDirectory", ""))
            if len(ws_title) > 25:
                ws_title = ws_title[:22] + "..."

            all_panels = ws.get("panels", [])
            total_panels = len(all_panels)
            # Claude detection: title glyph OR Claude proc on the panel's TTY.
            def _panel_is_claude(p):
                if _is_claude_title(p.get("title", "")):
                    return True
                tty = p.get("ttyName") or ""
                return tty in claude_by_tty
            claude_count = sum(1 for p in all_panels if _panel_is_claude(p))
            non_claude_count = total_panels - claude_count
            panels_str = f"{total_panels} ({claude_count}C/{non_claude_count}T)"

            for panel in all_panels:
                panel_title = panel.get("title", "")
                tty_name = panel.get("ttyName") or ""
                proc = claude_by_tty.get(tty_name)
                is_claude = proc is not None or _is_claude_title(panel_title)
                if not is_claude:
                    continue

                panel_dir = panel.get("terminal", {}).get(
                    "workingDirectory", panel.get("directory", "")
                )

                session_id = "-"
                status = "stopped"
                sid = None
                if proc is not None:
                    status = "running"
                    sid = proc.get("session_id")
                    if not sid and panel_dir:
                        latest = find_latest_claude_session(panel_dir)
                        if latest:
                            sid = latest.get("sessionId")
                    if sid:
                        session_id = sid[:12] + "..." if len(sid) > 15 else sid

                short_dir = panel_dir.replace(os.path.expanduser("~"), "~")
                if len(short_dir) > 45:
                    short_dir = "..." + short_dir[-42:]

                clean_title = _clean_panel_title(panel_title)
                if len(clean_title) > 35:
                    clean_title = clean_title[:32] + "..."

                # Git branch — try session metadata first, fall back to .git/HEAD
                branch = "-"
                if panel_dir:
                    meta = None
                    if sid and status == "running":
                        meta = get_claude_session_info(panel_dir, sid)
                    if meta and meta.get("gitBranch"):
                        branch = meta["gitBranch"]
                    else:
                        branch = get_git_branch(panel_dir) or "-"
                    if len(branch) > 20:
                        branch = branch[:17] + "..."

                rows.append((ws_title, panels_str, clean_title, short_dir, branch, session_id, status))

    if not rows:
        print("No Claude sessions found in cmux workspaces.")
        return

    headers = ("WORKSPACE", "PANELS", "CLAUDE SESSION", "DIRECTORY", "BRANCH", "SESSION ID", "STATUS")
    widths = [max(len(h), max(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)

    print(fmt.format(*headers))
    print(fmt.format(*("-" * w for w in widths)))
    for row in rows:
        print(fmt.format(*row))

    running = sum(1 for r in rows if r[6] == "running")
    print(f"\n{len(rows)} Claude panels, {running} running")


def cmd_show(args):
    """Show detailed info for a workspace (active or from a snapshot)."""
    home = os.path.expanduser("~")

    if args.file:
        # Show from snapshot
        snap_path = args.file
        if not os.path.exists(snap_path):
            print(f"Error: Snapshot not found at {snap_path}", file=sys.stderr)
            sys.exit(1)

        with open(snap_path) as f:
            snap = json.load(f)

        found = False
        for win in snap.get("windows", []):
            for ws in win.get("workspaces", []):
                title = ws.get("title", "untitled")
                if args.workspace and args.workspace.lower() not in title.lower():
                    continue
                found = True
                _show_snapshot_workspace(ws, snap_path, snap.get("timestamp", "unknown"), home)

        if not found:
            print(f"Error: No workspace matching '{args.workspace}' in snapshot.", file=sys.stderr)
            sys.exit(1)
    else:
        # Show from live cmux session
        cmux_data = load_cmux_session()
        claude_procs = get_claude_processes()
        terminal_cmds = get_terminal_commands()
        claude_by_tty = {p["tty"]: p for p in claude_procs if p.get("tty")}

        found = False
        for win in cmux_data.get("windows", []):
            for ws in win.get("tabManager", {}).get("workspaces", []):
                ws_title = ws.get("customTitle") or ws.get("title") or os.path.basename(ws.get("currentDirectory", ""))
                if args.workspace and args.workspace.lower() not in ws_title.lower():
                    continue
                found = True
                _show_live_workspace(ws, claude_by_tty, terminal_cmds, home)

        if not found:
            if args.workspace:
                print(f"Error: No workspace matching '{args.workspace}' found.", file=sys.stderr)
            else:
                print("No workspaces found.", file=sys.stderr)
            sys.exit(1)


def _show_live_workspace(ws, claude_by_tty, terminal_cmds, home):
    """Print detailed info for a live workspace."""
    ws_title = ws.get("customTitle") or ws.get("title") or os.path.basename(ws.get("currentDirectory", ""))
    ws_cwd = ws.get("currentDirectory", "")
    panels = ws.get("panels", [])

    print(f"Workspace: {ws_title}")
    print(f"Directory: {ws_cwd.replace(home, '~')}")
    print(f"Panels:    {len(panels)}")
    print()

    for i, panel in enumerate(panels):
        panel_title = panel.get("title", "")
        tty_name = panel.get("ttyName") or ""
        proc = claude_by_tty.get(tty_name)
        is_claude = proc is not None or _is_claude_title(panel_title)
        terminal = panel.get("terminal", {})
        panel_cwd = terminal.get("workingDirectory", panel.get("directory", ""))
        short_cwd = panel_cwd.replace(home, "~")

        kind = "claude" if is_claude else "terminal"
        clean_title = _clean_panel_title(panel_title).strip()
        if not clean_title:
            clean_title = "(untitled)"

        print(f"  Panel {i + 1}: [{kind}] {clean_title}")
        print(f"    cwd: {short_cwd}")
        if tty_name:
            print(f"    tty: {tty_name}")

        if is_claude:
            if proc is not None:
                sid = proc.get("session_id")
                if not sid and panel_cwd:
                    latest = find_latest_claude_session(panel_cwd)
                    if latest:
                        sid = latest.get("sessionId")
                print(f"    session: {sid or '(unknown)'}")
                print(f"    pid: {proc['pid']}")
                print(f"    status: running (matched by tty)")
                if sid:
                    meta = get_claude_session_info(panel_cwd, sid)
                    if meta:
                        if meta.get("summary"):
                            print(f"    summary: {meta['summary']}")
                        if meta.get("gitBranch"):
                            print(f"    branch: {meta['gitBranch']}")
            else:
                latest = find_latest_claude_session(panel_cwd)
                if latest:
                    print(f"    session: {latest['sessionId']}")
                    print(f"    status: stopped (from index)")
                    if latest.get("summary"):
                        print(f"    summary: {latest['summary']}")
                else:
                    print(f"    status: stopped (no session found)")
        else:
            # Show running command for terminal panels
            cmds = terminal_cmds.get(panel_cwd, [])
            if cmds:
                cmd = cmds.pop(0)
                print(f"    command: {cmd}")
        print()


def _show_snapshot_workspace(ws, snap_path, timestamp, home):
    """Print detailed info for a snapshot workspace."""
    title = ws.get("title", "untitled")
    cwd = ws.get("cwd", "")

    print(f"Workspace: {title}")
    print(f"Directory: {cwd.replace(home, '~')}")
    print(f"Snapshot:  {os.path.basename(snap_path)} ({timestamp})")
    print(f"Panels:    {len(ws.get('panels', []))}")
    print()

    for i, panel in enumerate(ws.get("panels", [])):
        is_claude = panel.get("isClaude", False)
        panel_dir = panel.get("directory", "")
        short_dir = panel_dir.replace(home, "~")
        panel_title = panel.get("title", "")

        kind = "claude" if is_claude else "terminal"
        clean_title = _clean_panel_title(panel_title).strip()
        if not clean_title:
            clean_title = "(untitled)"

        print(f"  Panel {i + 1}: [{kind}] {clean_title}")
        print(f"    cwd: {short_dir}")

        if is_claude:
            session = panel.get("claudeSession", {})
            if session.get("session_id"):
                print(f"    session: {session['session_id']}")
            if session.get("pid"):
                print(f"    pid: {session['pid']} (at snapshot time)")
            if session.get("summary"):
                print(f"    summary: {session['summary']}")
            if session.get("gitBranch"):
                print(f"    branch: {session['gitBranch']}")
            if session.get("note"):
                print(f"    note: {session['note']}")
        else:
            if panel.get("lastCommand"):
                print(f"    command: {panel['lastCommand']}")
        print()


def cmd_restore(args):
    """Restore cmux workspaces and Claude sessions from a snapshot."""
    if args.file:
        snap_path = args.file
    else:
        snap_path = os.path.join(SNAPSHOT_DIR, "latest.json")

    if not os.path.exists(snap_path):
        print(f"Error: Snapshot not found at {snap_path}", file=sys.stderr)
        print("Run 'cmux-sessions snapshot' first to create one.", file=sys.stderr)
        sys.exit(1)

    with open(snap_path) as f:
        snap = json.load(f)

    ws_filter = getattr(args, "workspace", None)
    run_commands = getattr(args, "run_commands", False)
    skip_active = getattr(args, "skip_active", False)
    home = os.path.expanduser("~")

    print(f"Restoring from: {snap_path}")
    print(f"Snapshot taken:  {snap.get('timestamp', 'unknown')}")
    if ws_filter:
        print(f"Workspace filter: {ws_filter}")
    print()

    # When --skip-active, determine which workspaces are already open
    active_titles = set()
    if skip_active:
        try:
            live_workspaces = _get_live_workspaces()
            active_titles = {ws["title"].lower() for ws in live_workspaces}
        except SystemExit:
            pass  # can't query cmux; proceed without filtering

    # Build ordered restore plan
    steps = []
    total_workspaces = 0
    total_panels = 0
    total_claude = 0
    skipped = []
    skipped_active = []
    matched_any = False

    for win in snap.get("windows", []):
        for ws_idx, ws in enumerate(win.get("workspaces", [])):
            title = ws.get("title", "untitled")

            # Apply workspace filter
            if ws_filter is not None:
                match = False
                if ws_filter.isdigit() and int(ws_filter) == ws.get("index", ws_idx):
                    match = True
                elif ws_filter.lower() in title.lower():
                    match = True
                if not match:
                    continue
            matched_any = True

            # Skip workspaces that are already open
            if skip_active and title.lower() in active_titles:
                skipped_active.append(title)
                continue

            cwd = ws.get("cwd", "")

            if not cwd or not os.path.isdir(cwd):
                skipped.append((title, cwd))
                continue

            total_workspaces += 1
            ws_steps, ws_panel_count, ws_claude_count = _build_workspace_steps(
                ws, total_workspaces, run_commands, home
            )
            steps.extend(ws_steps)
            total_panels += ws_panel_count
            total_claude += ws_claude_count

    if ws_filter and not matched_any:
        print(f"Error: No workspace matching '{ws_filter}' found in snapshot.", file=sys.stderr)
        # List available workspaces to help the user
        print("Available workspaces:", file=sys.stderr)
        for w in snap.get("windows", []):
            for ws in w.get("workspaces", []):
                print(f"  [{ws.get('index', '?')}] {ws.get('title', 'untitled')}", file=sys.stderr)
        sys.exit(1)

    # Print plan
    if skipped_active:
        print(f"Skipped (already open): {len(skipped_active)} workspace(s)")
        for t in skipped_active:
            print(f"  - {t}")
        print()

    if skipped:
        print("Skipped (directory not found):")
        for title, cwd in skipped:
            print(f"  {title}: {cwd}")
        print()

    non_claude = total_panels - total_claude

    # Collect workspace titles for display (exclude skipped-active)
    skipped_active_lower = {t.lower() for t in skipped_active}
    ws_titles = [
        ws.get("title", "untitled")
        for win in snap.get("windows", [])
        for ws in win.get("workspaces", [])
        if _snap_ws_matches(ws, ws_filter)
        and ws.get("title", "untitled").lower() not in skipped_active_lower
    ]

    if total_workspaces == 0:
        if skipped_active:
            print("All matching workspaces are already open. Nothing to restore.")
        else:
            print("No workspaces to restore.")
        return

    print(f"Plan: {total_workspaces} workspaces, {total_panels} panels ({total_claude} Claude, {non_claude} terminal)")
    for t in ws_titles:
        print(f"  - {t}")
    print()

    # Show saved commands as hints
    saved_cmds = []
    for win in snap.get("windows", []):
        for ws in win.get("workspaces", []):
            if not _snap_ws_matches(ws, ws_filter):
                continue
            for p in ws.get("panels", []):
                if p.get("lastCommand") and not p.get("isClaude"):
                    short_dir = p.get("directory", "").replace(home, "~")
                    saved_cmds.append((ws.get("title", ""), short_dir, p["lastCommand"]))

    if saved_cmds and not run_commands:
        print("Saved terminal commands (not restored by default):")
        for ws_title, pdir, cmd in saved_cmds:
            print(f"  {pdir}: {cmd}")
        print(f"  Use --run-commands to auto-run these on restore.")
        print()
    elif saved_cmds and run_commands:
        print("Terminal commands will be re-run:")
        for ws_title, pdir, cmd in saved_cmds:
            print(f"  {pdir}: {cmd}")
        print()

    if args.dry_run:
        _print_dry_run(steps)
        return

    if _inside_cmux():
        # Check for already-open workspaces that would conflict
        already_open = []
        try:
            live_workspaces = _get_live_workspaces()
            live_titles = {ws["title"].lower() for ws in live_workspaces}
            already_open = [t for t in ws_titles if t.lower() in live_titles]
        except SystemExit:
            pass  # _get_live_workspaces calls sys.exit on failure; ignore here

        if already_open:
            print(f"WARNING: {len(already_open)} workspace(s) already open:")
            for t in already_open:
                print(f"  - {t}")
            print()
            print("Restoring would create duplicates and conflict with active Claude sessions.")
            print("Kill them first with: make kill W=<name>")
            print("Or use respawn:       make respawn W=<name>")
            print()
            try:
                answer = input("Restore anyway? Type 'force' to continue: ")
                if answer.strip().lower() != "force":
                    print("Aborted.")
                    return
            except (EOFError, KeyboardInterrupt):
                print("\nAborted.")
                return
        elif ws_filter is None and total_workspaces > 1:
            # Stronger warning when restoring all workspaces
            print("WARNING: No workspace filter specified — this will restore ALL workspaces.")
            try:
                answer = input(f"Restore all {total_workspaces} workspaces? Type 'yes' to confirm: ")
                if answer.strip().lower() != "yes":
                    print("Aborted.")
                    return
            except (EOFError, KeyboardInterrupt):
                print("\nAborted.")
                return
        else:
            try:
                answer = input("Restore now? [y/N] ")
                if answer.strip().lower() not in ("y", "yes"):
                    print("Aborted.")
                    return
            except (EOFError, KeyboardInterrupt):
                print("\nAborted.")
                return

        _execute_restore(steps, total_workspaces, total_panels, total_claude, non_claude)
    else:
        _generate_restore_script(
            steps, snap_path, total_workspaces, total_panels, total_claude, non_claude
        )


# ── Restore step engine ─────────────────────────────────────
#
# Each restore step is an argv list (`argv`) — never a shell string —
# plus an optional `captures` list that names regex patterns to extract
# refs (workspace:N, surface:N, pane:N) from the command's stdout.
#
# Tokens of the form `${VAR}` inside argv are placeholders. They are
# resolved at execute time from previously-captured refs. Bash output
# preserves them as `"$VAR"`; everything else is shlex.quote'd.

_VAR_TOKEN_RE = re.compile(r"\$\{(\w+)\}")
_BARE_VAR_RE = re.compile(r"^\$\{(\w+)\}$")


def _resolve_argv(argv, env):
    """Substitute ${VAR} references. Returns (resolved_argv, missing_vars).

    A missing var is one that's referenced as ${X} but not in env. Callers
    must treat any missing var as fatal — otherwise the ${X} literal gets
    passed to cmux, which falls back to "current/focused" context and the
    command lands on the caller's pane instead of the target workspace.
    """
    out = []
    missing = []
    for tok in argv:
        def sub(m):
            v = env.get(m.group(1))
            if v is None:
                missing.append(m.group(1))
                return m.group(0)
            return v
        out.append(_VAR_TOKEN_RE.sub(sub, tok))
    return out, missing


def _bash_token(tok):
    """Render one argv token for a bash script.

    Bare ${VAR} tokens become "$VAR" (so cmux refs expand naturally).
    Everything else is shlex.quote'd.
    """
    m = _BARE_VAR_RE.match(tok)
    if m:
        return f'"${m.group(1)}"'
    return shlex.quote(tok)


def _run_cmux(args, timeout=10):
    """Run a cmux command and return (success, stdout, stderr)."""
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout
        )
        return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
    except Exception as e:
        return False, "", str(e)


def _sh_escape(s):
    """Escape single quotes for shell single-quoted embedding."""
    return s.replace("'", "'\\''")


# ── Workspace step builder ──────────────────────────────────


def _claude_launch_cmd(panel):
    """Return the `claude ...` command for a Claude panel, or None."""
    sess = panel.get("claudeSession") or {}
    sid = sess.get("session_id")
    if sid:
        return f"claude --resume {shlex.quote(sid)}"
    return "claude -c"


def _panel_launch_text(panel, panel_dir, run_commands):
    """Build the shell text to send to a panel's surface.

    Always starts with `cd <dir>` (so the new terminal lands in the right
    place even if cmux's --cwd was inherited). Appends the Claude resume
    or saved command when applicable.
    """
    parts = []
    if panel_dir:
        parts.append(f"cd {shlex.quote(panel_dir)}")
    if panel.get("isClaude"):
        parts.append(_claude_launch_cmd(panel))
    else:
        last_cmd = (panel.get("lastCommand") or "").strip()
        if run_commands and last_cmd:
            parts.append(last_cmd)
    return " && ".join(parts) if parts else ""


def _build_workspace_steps(ws, ws_num, run_commands, home):
    """Build restore steps for one workspace.

    Returns (steps, panel_count, claude_count).
    """
    title = ws.get("title", "untitled")
    cwd = ws.get("cwd", "")
    color = (ws.get("color") or "").strip()
    description = (ws.get("description") or "").strip()
    panels = ws.get("panels", [])
    layout = ws.get("layout", {}) or {}
    focused_id = (ws.get("focusedPanelId") or "").strip()

    panels_by_id = {p.get("id"): p for p in panels}
    ws_var = f"WS{ws_num}"

    steps = []

    # 1. Create workspace.
    new_argv = ["cmux", "new-workspace"]
    if cwd:
        new_argv += ["--cwd", cwd]
    if title and title != "untitled":
        new_argv += ["--name", title]
    if description:
        new_argv += ["--description", description]
    steps.append({
        "type": "new-workspace",
        "desc": f"Create workspace: {title}",
        "argv": new_argv,
        "captures": [{"var": ws_var, "pattern": r"workspace:[0-9]+"}],
    })

    # 2. Apply color via workspace-action (new-workspace has no --color flag).
    if color:
        steps.append({
            "type": "set-color",
            "desc": f"  Set color: {color}",
            "argv": ["cmux", "workspace-action",
                     "--action", "set-color",
                     "--workspace", f"${{{ws_var}}}",
                     "--color", color],
        })

    # 3. Walk layout tree. At each leaf pane, emit one step per panelId.
    surf_counter = [0]
    selected_surface_var = [None]
    panel_count = [0]
    claude_count = [0]
    first_pane_var = [None]  # remember the workspace's initial pane for orphans

    def emit_panel(panel, surf_var, pane_var):
        """Emit send + rename-tab steps for a single restored panel."""
        panel_dir = (panel.get("directory") or cwd or "").strip()
        launch = _panel_launch_text(panel, panel_dir, run_commands)
        if launch:
            steps.append({
                "type": "send",
                "desc": f"    Launch: {launch[:70]}",
                "argv": ["cmux", "send",
                         "--workspace", f"${{{ws_var}}}",
                         "--surface", f"${{{surf_var}}}",
                         launch],
                "post_enter": True,
                "ws_var": ws_var,
                "surf_var": surf_var,
            })

        # Only restore the tab title if the user explicitly renamed it.
        # cmux makes `tab-action rename` sticky — it overrides subsequent
        # OSC title escapes from the process. Re-applying a process-
        # derived title (e.g. Claude's "✳ Reading file") would freeze the
        # tab on that stale status instead of letting Claude update it.
        custom_title = (panel.get("customTitle") or "").strip()
        if custom_title:
            steps.append({
                "type": "rename-tab",
                "desc": f"    Rename tab: {custom_title[:60]}",
                "argv": ["cmux", "rename-tab",
                         "--workspace", f"${{{ws_var}}}",
                         "--surface", f"${{{surf_var}}}",
                         custom_title],
            })

        panel_count[0] += 1
        if panel.get("isClaude"):
            claude_count[0] += 1

    def emit_pane(pane_node, is_first_pane, split_dir, parent_pane_var):
        """Emit steps for one pane node. Returns this pane's pane_var."""
        panel_ids = pane_node.get("panelIds") or []
        pane_selected = (pane_node.get("selectedPanelId") or "").strip()

        pane_var = f"P{ws_num}_{surf_counter[0]}"
        pane_ref_known = False

        for i, pid in enumerate(panel_ids):
            panel = panels_by_id.get(pid)
            if panel is None:
                continue

            surf_counter[0] += 1
            surf_var = f"S{ws_num}_{surf_counter[0]}"

            if i == 0 and is_first_pane:
                steps.append({
                    "type": "lookup-initial",
                    "desc": f"  Initial tab: {(panel.get('title') or '')[:60]}",
                    "ws_var": ws_var,
                    "surf_var": surf_var,
                    "pane_var": pane_var,
                })
                pane_ref_known = True
                if first_pane_var[0] is None:
                    first_pane_var[0] = pane_var
            elif i == 0:
                # First panel of a non-initial pane => split off the parent.
                # Targeting --panel <parent> is essential for nested layouts;
                # without it cmux uses its current/focused pane heuristic and
                # deeper splits land in unpredictable places.
                split_argv = ["cmux", "new-split", split_dir,
                              "--workspace", f"${{{ws_var}}}"]
                if parent_pane_var:
                    split_argv += ["--panel", f"${{{parent_pane_var}}}"]
                steps.append({
                    "type": "new-split",
                    "desc": f"  Split {split_dir}: {(panel.get('title') or '')[:50]}",
                    "argv": split_argv,
                    "captures": [
                        {"var": surf_var, "pattern": r"surface:[0-9]+"},
                        {"var": pane_var, "pattern": r"pane:[0-9]+"},
                    ],
                })
                pane_ref_known = True
            else:
                # Additional tab in the same pane.
                # --focus true is essential: cmux inserts new surfaces after
                # the currently focused one, so without it every new tab lands
                # at position 1, reversing the intended order.
                new_surf_argv = ["cmux", "new-surface",
                                 "--workspace", f"${{{ws_var}}}",
                                 "--focus", "true"]
                if pane_ref_known:
                    new_surf_argv += ["--pane", f"${{{pane_var}}}"]
                steps.append({
                    "type": "new-surface",
                    "desc": f"  + Tab: {(panel.get('title') or '')[:60]}",
                    "argv": new_surf_argv,
                    "captures": [{"var": surf_var, "pattern": r"surface:[0-9]+"}],
                })

            emit_panel(panel, surf_var, pane_var)

            # Remember which surface should be focused at the end.
            if focused_id and pid == focused_id:
                selected_surface_var[0] = surf_var
            elif not focused_id and pane_selected and pid == pane_selected:
                selected_surface_var[0] = surf_var

        return pane_var if pane_ref_known else None

    def walk(node, is_first_pane, split_dir, parent_pane_var):
        """Walk the layout tree. Returns (still_first, last_leaf_pane_var).

        Returning the *last* leaf (not the first) means each sibling split
        attaches to the most-recently-created pane in the subtree above it.
        This is the closest approximation of the original topology that
        cmux's per-pane new-split CLI allows — cmux can't split a multi-
        pane region as a unit, so arbitrary nested trees can't be perfectly
        reconstructed.
        """
        ntype = node.get("type")
        if ntype == "pane":
            pv = emit_pane(node, is_first_pane, split_dir, parent_pane_var)
            return False, pv
        if ntype == "split":
            orientation = node.get("orientation", "vertical")
            direction = "right" if orientation == "vertical" else "down"
            _, first_leaf = walk(
                node.get("first", {}) or {}, is_first_pane, None, parent_pane_var)
            _, second_leaf = walk(
                node.get("second", {}) or {}, False, direction, first_leaf)
            return False, second_leaf
        return is_first_pane, parent_pane_var

    # If layout is empty/unknown, synthesize a single pane from all panels
    # so we don't silently drop them.
    if layout.get("type") not in ("pane", "split"):
        all_ids = [p.get("id") for p in panels if p.get("id")]
        layout = {
            "type": "pane",
            "panelIds": all_ids,
            "selectedPanelId": focused_id or "",
        }

    walk(layout, True, None, None)

    # Catch panels not referenced by the layout tree — append them as extra
    # tabs in the initial pane. Otherwise they're silently dropped.
    referenced = set()
    def _collect_referenced(node):
        ntype = node.get("type")
        if ntype == "pane":
            for pid in (node.get("panelIds") or []):
                if pid:
                    referenced.add(pid)
        elif ntype == "split":
            _collect_referenced(node.get("first") or {})
            _collect_referenced(node.get("second") or {})
    _collect_referenced(layout)

    orphans = [p for p in panels if p.get("id") and p["id"] not in referenced]
    for panel in orphans:
        surf_counter[0] += 1
        surf_var = f"S{ws_num}_{surf_counter[0]}"
        new_surf_argv = ["cmux", "new-surface",
                         "--workspace", f"${{{ws_var}}}",
                         "--focus", "true"]
        if first_pane_var[0]:
            new_surf_argv += ["--pane", f"${{{first_pane_var[0]}}}"]
        steps.append({
            "type": "new-surface",
            "desc": f"  + Orphan tab: {(panel.get('title') or '')[:50]}",
            "argv": new_surf_argv,
            "captures": [{"var": surf_var, "pattern": r"surface:[0-9]+"}],
        })
        emit_panel(panel, surf_var, first_pane_var[0])
        if focused_id and panel.get("id") == focused_id:
            selected_surface_var[0] = surf_var

    # 4. Focus the originally-focused tab.
    if selected_surface_var[0]:
        steps.append({
            "type": "focus-panel",
            "desc": f"  Focus selected tab",
            "argv": ["cmux", "focus-panel",
                     "--workspace", f"${{{ws_var}}}",
                     "--panel", f"${{{selected_surface_var[0]}}}"],
        })

    return steps, panel_count[0], claude_count[0]


# ── Restore step rendering ──────────────────────────────────


def _step_argv_for_display(s):
    """Return the argv tokens for a step, or None if it has no argv."""
    return s.get("argv")


def _format_bash_command(argv):
    """Join argv into a bash-safe command string."""
    return " ".join(_bash_token(t) for t in argv)


def _print_dry_run(steps):
    """Print a dry-run preview of restore steps."""
    print("Steps:")
    print()
    for s in steps:
        step_type = s["type"]
        print(f"  [{step_type:>18}] {s['desc']}")

        if step_type == "lookup-initial":
            ws_var = s["ws_var"]
            surf_var = s["surf_var"]
            pane_var = s["pane_var"]
            print(f"             $ {surf_var}=$(cmux list-pane-surfaces --workspace \"${ws_var}\" "
                  f"| grep -oE 'surface:[0-9]+' | head -1)")
            print(f"             $ {pane_var}=$(cmux list-panes --workspace \"${ws_var}\" "
                  f"| grep -oE 'pane:[0-9]+' | head -1)")
            print()
            continue

        argv = _step_argv_for_display(s)
        captures = s.get("captures", [])
        if argv:
            cmd_str = _format_bash_command(argv)
            if captures:
                # Show first capture as `VAR=$(... | grep ...)`
                first = captures[0]
                grep_pattern = first["pattern"]
                print(f"             $ {first['var']}=$({cmd_str} | grep -oE '{grep_pattern}' | head -1)")
                for cap in captures[1:]:
                    pat = cap["pattern"]
                    print(f"             $ {cap['var']}=$(cmux {'list-panes' if 'pane' in pat else 'list-pane-surfaces'} "
                          f"--workspace \"${s.get('ws_var', '')}\" | grep -oE '{pat}' | head -1)")
            else:
                print(f"             $ {cmd_str}")

        if s.get("post_enter"):
            ws_var = s.get("ws_var", "")
            surf_var = s.get("surf_var", "")
            print(f"             $ cmux send-key --workspace \"${ws_var}\" --surface \"${surf_var}\" Enter")
        print()
    print("(dry run — no changes made)")


def _execute_step(s, env):
    """Run one step against the live cmux socket. Mutates env with captured refs.

    Returns True on success, None on hard failure (caller must exit).

    Anything that would leave a downstream step running without a resolved
    workspace/surface/pane ref is treated as fatal — otherwise the command
    silently falls back to cmux's current/focused context and the restore
    lands on the caller's pane.
    """
    step_type = s["type"]
    ws_var = s.get("ws_var", "")
    surf_var = s.get("surf_var", "")

    if step_type == "lookup-initial":
        ws_ref = env.get(s["ws_var"], "")
        if not ws_ref:
            print(f"    ERROR: no workspace ref for {s['ws_var']} — refusing to continue")
            return None
        surfaces = _list_pane_surfaces(ws_ref)
        if not surfaces:
            print(f"    ERROR: no surfaces found in {ws_ref}")
            return None
        env[s["surf_var"]] = surfaces[0]["ref"]
        ok, out, _ = _run_cmux(["cmux", "list-panes", "--workspace", ws_ref])
        pane_m = re.search(r"pane:[0-9]+", out or "") if ok else None
        if not pane_m:
            print(f"    ERROR: could not resolve initial pane ref in {ws_ref}")
            return None
        env[s["pane_var"]] = pane_m.group(0)
        print(f"             surface={env[s['surf_var']]} pane={env[s['pane_var']]}")
        return True

    argv = s.get("argv")
    if not argv:
        return True

    resolved, missing = _resolve_argv(argv, env)
    if missing:
        print(f"    ERROR: unresolved refs {sorted(set(missing))} — refusing to run with caller's cmux context")
        return None

    ok, out, err = _run_cmux(resolved)
    if not ok:
        # Steps that produce a ref later commands depend on are fatal.
        # Side-effect-only steps (set-color, rename-tab, focus-panel,
        # send) just warn — losing those degrades the restore but does
        # not redirect future commands to the wrong workspace.
        side_effect_only = {"set-color", "set-desc", "rename-tab",
                            "focus-panel", "send"}
        if step_type in side_effect_only:
            print(f"    WARNING: {err or '(failed)'}")
            return True
        print(f"    ERROR: {err or '(failed)'}")
        return None

    # Capture refs from stdout. A capture miss is fatal — downstream
    # placeholders for this var would silently expand to the current pane.
    for cap in s.get("captures", []):
        m = re.search(cap["pattern"], out or "")
        if not m:
            print(f"    ERROR: could not capture {cap['var']} (pattern {cap['pattern']}) "
                  f"from output: {out!r}")
            return None
        env[cap["var"]] = m.group(0)
        print(f"             {cap['var']}={m.group(0)}")

    # Optional Enter after send. Both workspace and surface refs must be
    # known — otherwise send-key would land on the caller's pane.
    if s.get("post_enter"):
        ws_ref = env.get(ws_var) if ws_var else None
        surf_ref = env.get(surf_var) if surf_var else None
        if not ws_ref or not surf_ref:
            print(f"    ERROR: post_enter missing ws/surf refs (ws={ws_ref!r}, surf={surf_ref!r})")
            return None
        _run_cmux(["cmux", "send-key", "--workspace", ws_ref,
                   "--surface", surf_ref, "Enter"])

    return True


def _execute_restore(steps, total_workspaces, total_panels, total_claude, non_claude):
    """Execute restore steps directly via cmux CLI."""
    env = {}  # var name -> resolved cmux ref

    for s in steps:
        step_type = s["type"]
        if step_type != "lookup-initial":
            print(f"  [{step_type:>18}] {s['desc']}")
        else:
            print(f"  [{step_type:>18}] {s['desc']}")

        rv = _execute_step(s, env)
        if rv is None:
            sys.exit(1)

        # Pacing.
        if step_type == "new-workspace":
            time.sleep(0.6)
        elif step_type == "new-split":
            time.sleep(0.3)
        elif step_type == "new-surface":
            time.sleep(0.25)
        elif step_type == "send":
            time.sleep(0.3)

    print()
    print(f"Restored {total_workspaces} workspaces, {total_panels} panels "
          f"({total_claude} Claude, {non_claude} terminal).")


def _generate_restore_script(steps, snap_path, total_workspaces, total_panels, total_claude, non_claude):
    """Generate a restore shell script for running outside cmux."""
    script_path = os.path.join(SNAPSHOT_DIR, "restore.sh")
    with open(script_path, "w") as f:
        f.write("#!/bin/bash\n")
        f.write("# cmux workspace restore script\n")
        f.write(f"# Generated: {datetime.now().isoformat()}\n")
        f.write(f"# Source: {snap_path}\n")
        f.write(f"# Workspaces: {total_workspaces}, Panels: {total_panels} "
                f"({total_claude} Claude, {non_claude} terminal)\n")
        f.write("#\n")
        f.write("# Run this from inside a cmux terminal.\n")
        f.write("# Review with --dry-run first: cmux-sessions restore --dry-run\n\n")
        f.write("set -e\n\n")

        for s in steps:
            step_type = s["type"]
            f.write(f"# {s['desc']}\n")

            if step_type == "lookup-initial":
                ws_var = s["ws_var"]
                surf_var = s["surf_var"]
                pane_var = s["pane_var"]
                f.write(f"{surf_var}=$(cmux list-pane-surfaces --workspace \"${ws_var}\" "
                        f"| grep -oE 'surface:[0-9]+' | head -1)\n")
                f.write(f"{pane_var}=$(cmux list-panes --workspace \"${ws_var}\" "
                        f"| grep -oE 'pane:[0-9]+' | head -1)\n")
                f.write("\n")
                continue

            argv = s.get("argv")
            captures = s.get("captures", [])
            if argv:
                cmd_str = _format_bash_command(argv)
                if captures:
                    first = captures[0]
                    f.write(f"_OUT=$({cmd_str})\n")
                    f.write(f"echo \"$_OUT\"\n")
                    for cap in captures:
                        f.write(f"{cap['var']}=$(echo \"$_OUT\" | grep -oE '{cap['pattern']}' | head -1)\n")
                else:
                    f.write(f"{cmd_str}\n")

            if s.get("post_enter"):
                ws_var = s.get("ws_var", "")
                surf_var = s.get("surf_var", "")
                f.write(f"cmux send-key --workspace \"${ws_var}\" --surface \"${surf_var}\" Enter\n")

            if step_type == "new-workspace":
                f.write("sleep 0.6\n")
            elif step_type == "new-split":
                f.write("sleep 0.3\n")
            elif step_type == "new-surface":
                f.write("sleep 0.25\n")
            elif step_type == "send":
                f.write("sleep 0.3\n")

            f.write("\n")

        f.write(f'echo "Restored {total_workspaces} workspaces, {total_panels} panels '
                f'({total_claude} Claude, {non_claude} terminal)."\n')

    os.chmod(script_path, 0o755)

    print("Not running inside cmux — generated restore script instead.")
    print()
    print(f"  {script_path}")
    print()
    print("Run it from inside a cmux terminal, or review first with:")
    print("  cmux-sessions restore --dry-run")


def cmd_snapshots(args):
    """List available snapshots."""
    if not os.path.isdir(SNAPSHOT_DIR):
        print("No snapshots found.")
        return

    files = sorted(Path(SNAPSHOT_DIR).glob("cmux-*.json"), reverse=True)
    if not files:
        print("No snapshots found.")
        return

    print(f"{'SNAPSHOT':<35} {'WORKSPACES':<35} {'CLAUDE':>6} {'PANELS':>6}")
    print(f"{'-'*35} {'-'*35} {'-'*6} {'-'*6}")

    for fp in files:
        try:
            with open(fp) as f:
                data = json.load(f)
            ws_names = [
                ws.get("title", "untitled")
                for w in data.get("windows", [])
                for ws in w["workspaces"]
            ]
            names_str = ", ".join(ws_names) if ws_names else "(none)"
            if len(names_str) > 33:
                names_str = names_str[:30] + "..."
            n_panels = sum(
                len(ws["panels"])
                for w in data.get("windows", [])
                for ws in w["workspaces"]
            )
            n_claude = sum(
                1 for w in data.get("windows", [])
                for ws in w["workspaces"]
                for p in ws["panels"] if p.get("isClaude")
            )
            print(f"{fp.name:<35} {names_str:<35} {n_claude:>6} {n_panels:>6}")
        except Exception:
            print(f"{fp.name:<35} {'(corrupt)':<35}")


def cmd_validate(args):
    """Check if a snapshot is still valid for restoring."""
    home = os.path.expanduser("~")

    if args.file:
        snap_path = args.file
    else:
        snap_path = os.path.join(SNAPSHOT_DIR, "latest.json")

    if not os.path.exists(snap_path):
        print(f"Error: Snapshot not found at {snap_path}", file=sys.stderr)
        sys.exit(1)

    with open(snap_path) as f:
        snap = json.load(f)

    ws_filter = getattr(args, "workspace", None)

    print(f"Validating: {snap_path}")
    print(f"Snapshot:   {snap.get('timestamp', 'unknown')}")
    if ws_filter:
        print(f"Filter:     {ws_filter}")
    print()

    all_pass = True
    rows = []

    for win in snap.get("windows", []):
        for ws in win.get("workspaces", []):
            if not _snap_ws_matches(ws, ws_filter):
                continue

            ws_title = ws.get("title", "untitled")
            ws_cwd = ws.get("cwd", "")

            # Check workspace directory
            ws_dir_ok = os.path.isdir(ws_cwd) if ws_cwd else False
            if not ws_dir_ok:
                all_pass = False

            for i, panel in enumerate(ws.get("panels", [])):
                panel_dir = panel.get("directory", "")
                short_dir = panel_dir.replace(home, "~")
                is_claude = panel.get("isClaude", False)

                # Directory check
                dir_ok = os.path.isdir(panel_dir) if panel_dir else False
                if not dir_ok:
                    all_pass = False

                # Session check (Claude only)
                session_ok = None
                if is_claude:
                    session = panel.get("claudeSession", {})
                    sid = session.get("session_id", "")
                    if sid and panel_dir:
                        encoded = panel_dir.replace("/", "-")
                        jsonl = os.path.join(CLAUDE_PROJECTS_DIR, encoded, f"{sid}.jsonl")
                        session_ok = os.path.exists(jsonl)
                        if not session_ok:
                            all_pass = False
                    else:
                        session_ok = False
                        all_pass = False

                kind = "claude" if is_claude else "terminal"
                dir_str = "\033[32mPASS\033[0m" if dir_ok else "\033[31mFAIL\033[0m"
                if session_ok is None:
                    sess_str = "-"
                elif session_ok:
                    sess_str = "\033[32mPASS\033[0m"
                else:
                    sess_str = "\033[31mFAIL\033[0m"

                rows.append((ws_title, f"[{kind}]", short_dir, dir_str, sess_str))

    if not rows:
        if ws_filter:
            print(f"No workspace matching '{ws_filter}' in snapshot.")
        else:
            print("No panels found in snapshot.")
        sys.exit(1)

    headers = ("WORKSPACE", "TYPE", "DIRECTORY", "DIR", "SESSION")
    # Calculate widths without ANSI codes
    def strip_ansi(s):
        return re.sub(r'\033\[[0-9;]*m', '', s)

    widths = [max(len(h), max(len(strip_ansi(r[i])) for r in rows)) for i, h in enumerate(headers)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)

    print(fmt.format(*headers))
    print(fmt.format(*("-" * w for w in widths)))
    for row in rows:
        # Pad with ANSI-aware widths
        parts = []
        for i, val in enumerate(row):
            visible_len = len(strip_ansi(val))
            padding = widths[i] - visible_len
            parts.append(val + " " * padding)
        print("  ".join(parts))

    print()
    if all_pass:
        print("\033[32mAll checks passed.\033[0m")
    else:
        print("\033[31mSome checks failed.\033[0m Review before restoring.")
        sys.exit(1)


def cmd_diff(args):
    """Compare a snapshot against live cmux workspaces."""
    if args.file:
        snap_path = args.file
    else:
        snap_path = os.path.join(SNAPSHOT_DIR, "latest.json")

    if not os.path.exists(snap_path):
        print(f"Error: Snapshot not found at {snap_path}", file=sys.stderr)
        sys.exit(1)

    with open(snap_path) as f:
        snap = json.load(f)

    # Get snapshot workspace titles
    snap_workspaces = {}
    for win in snap.get("windows", []):
        for ws in win.get("workspaces", []):
            title = ws.get("title", "untitled")
            panels = ws.get("panels", [])
            claude_count = sum(1 for p in panels if p.get("isClaude"))
            snap_workspaces[title] = {
                "panels": len(panels),
                "claude": claude_count,
                "terminal": len(panels) - claude_count,
            }

    # Get live workspace titles
    try:
        live_workspaces = _get_live_workspaces()
        live_titles = {ws["title"] for ws in live_workspaces}
    except SystemExit:
        print("Error: Cannot query live workspaces. Are you inside cmux?", file=sys.stderr)
        sys.exit(1)

    # Categorize
    snap_titles = set(snap_workspaces.keys())
    active = snap_titles & live_titles
    closed = snap_titles - live_titles
    extra = live_titles - snap_titles

    print(f"Snapshot:  {os.path.basename(snap_path)} ({snap.get('timestamp', 'unknown')})")
    print(f"Total:     {len(snap_titles)} in snapshot, {len(live_titles)} live")
    print()

    if closed:
        print(f"Closed ({len(closed)}) — in snapshot but not running:")
        for t in sorted(closed):
            info = snap_workspaces[t]
            print(f"  - {t}  ({info['panels']} panels: {info['claude']}C/{info['terminal']}T)")
        print()

    if active:
        print(f"Active ({len(active)}) — in snapshot and currently running:")
        for t in sorted(active):
            info = snap_workspaces[t]
            print(f"  - {t}  ({info['panels']} panels: {info['claude']}C/{info['terminal']}T)")
        print()

    if extra:
        print(f"New ({len(extra)}) — running but not in snapshot:")
        for t in sorted(extra):
            print(f"  - {t}")
        print()

    if not closed:
        print("All snapshot workspaces are currently active.")
    else:
        print(f"To restore closed workspaces: make restore SA=1" + (f" F={os.path.basename(snap_path).replace('.json', '')}" if args.file else ""))


def cmd_prune(args):
    """Delete old snapshots, keeping the most recent N."""
    if not os.path.isdir(SNAPSHOT_DIR):
        print("No snapshots found.")
        return

    files = sorted(Path(SNAPSHOT_DIR).glob("cmux-*.json"), reverse=True)
    if not files:
        print("No snapshots found.")
        return

    keep = args.keep
    to_delete = files[keep:]

    if not to_delete:
        print(f"Nothing to prune ({len(files)} snapshots, keeping {keep}).")
        return

    print(f"Snapshots: {len(files)} total, keeping {keep}, deleting {len(to_delete)}")
    for fp in to_delete:
        print(f"  delete: {fp.name}")
        os.remove(fp)

    print(f"\nPruned {len(to_delete)} snapshots.")


def _snap_ws_matches(ws, ws_filter):
    """Check if a snapshot workspace entry matches the filter."""
    if ws_filter is None:
        return True
    title = ws.get("title", "untitled")
    idx = ws.get("index", -1)
    if ws_filter.isdigit() and int(ws_filter) == idx:
        return True
    return ws_filter.lower() in title.lower()


def _inside_cmux():
    """Check if we're running inside a cmux terminal."""
    return bool(os.environ.get("CMUX_WORKSPACE_ID"))


# Matches lines like:
#   "  workspace:1  Some Title"
#   "* workspace:2  Other Title  [selected]"
_WS_LINE_RE = re.compile(r"^\s*(\*\s+)?(workspace:\d+)\s+(.+?)(?:\s+\[selected\])?\s*$")

# Matches lines like:
#   "  surface:8  terminal  \"title\""
#   "* surface:9  terminal  [focused]  \"title\""
#   "  surface:8  some title with spaces"
#   "* surface:9  some title  [selected]"
_SURFACE_LINE_RE = re.compile(
    r"^\s*(?P<sel>\*\s+)?(?P<ref>surface:\d+)\s+(?P<rest>.+?)\s*$"
)


def _get_live_workspaces():
    """Query cmux for currently open workspaces. Returns list of dicts with ref, title, selected."""
    try:
        result = subprocess.run(
            ["cmux", "list-workspaces"],
            capture_output=True, text=True, timeout=5
        )
    except Exception as e:
        print(f"Error: Cannot talk to cmux ({e}). Are you inside a cmux terminal?", file=sys.stderr)
        sys.exit(1)

    if result.returncode != 0:
        print(f"Error: cmux list-workspaces failed: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)

    workspaces = []
    for line in result.stdout.splitlines():
        m = _WS_LINE_RE.match(line)
        if not m:
            continue
        workspaces.append({
            "ref": m.group(2),
            "title": m.group(3).strip(),
            "selected": bool(m.group(1)) or "[selected]" in line,
        })
    return workspaces


def _list_pane_surfaces(ws_ref, pane_ref=None):
    """List tabs (surfaces) in a workspace pane. Returns list of dicts: ref, title, selected."""
    args = ["cmux", "list-pane-surfaces", "--workspace", ws_ref]
    if pane_ref:
        args += ["--pane", pane_ref]
    ok, out, _ = _run_cmux(args)
    if not ok or not out:
        return []
    result = []
    for line in out.splitlines():
        m = _SURFACE_LINE_RE.match(line)
        if not m:
            continue
        rest = m.group("rest")
        # Trim trailing "[selected]"
        is_selected = bool(m.group("sel")) or "[selected]" in rest
        title = re.sub(r"\s*\[selected\]\s*$", "", rest).strip()
        result.append({"ref": m.group("ref"), "title": title, "selected": is_selected})
    return result


def _find_workspace_ref(ws_filter):
    """Find a live workspace ref matching the filter. Returns (ref, title) or exits with error."""
    workspaces = _get_live_workspaces()
    matches = []
    for ws in workspaces:
        if ws_filter.lower() in ws["title"].lower():
            matches.append(ws)
        elif ws["ref"] == ws_filter:
            matches.append(ws)

    if not matches:
        print(f"Error: No open workspace matching '{ws_filter}'.", file=sys.stderr)
        print("Open workspaces:", file=sys.stderr)
        for ws in workspaces:
            print(f"  {ws['ref']}  {ws['title']}", file=sys.stderr)
        sys.exit(1)

    if len(matches) > 1:
        print(f"Error: Multiple workspaces match '{ws_filter}':", file=sys.stderr)
        for ws in matches:
            print(f"  {ws['ref']}  {ws['title']}", file=sys.stderr)
        print("Be more specific.", file=sys.stderr)
        sys.exit(1)

    return matches[0]["ref"], matches[0]["title"]


def cmd_kill(args):
    """Close a cmux workspace after confirmation."""
    if not args.workspace:
        print("Error: -w/--workspace is required for kill.", file=sys.stderr)
        sys.exit(1)

    ref, title = _find_workspace_ref(args.workspace)

    # Show what will be killed
    print(f"Workspace:  {title}")
    print(f"Ref:        {ref}")

    # Count panels from cmux session data
    cmux_data = load_cmux_session()
    panel_count = 0
    claude_count = 0
    for win in cmux_data.get("windows", []):
        for ws in win.get("tabManager", {}).get("workspaces", []):
            ws_title = ws.get("customTitle") or ws.get("title") or os.path.basename(ws.get("currentDirectory", ""))
            if args.workspace.lower() in ws_title.lower():
                panels = ws.get("panels", [])
                panel_count = len(panels)
                claude_count = sum(
                    1 for p in panels
                    if _is_claude_title(p.get("title", ""))
                )
                break

    print(f"Panels:     {panel_count} ({claude_count} Claude, {panel_count - claude_count} terminal)")
    print()

    if args.yes:
        confirmed = True
    else:
        try:
            answer = input(f"Kill workspace '{title}'? This will close all panels. [y/N] ")
            confirmed = answer.strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(1)

    if not confirmed:
        print("Aborted.")
        return

    try:
        result = subprocess.run(
            ["cmux", "close-workspace", "--workspace", ref],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            print(f"Error: cmux close-workspace failed: {result.stderr.strip()}", file=sys.stderr)
            sys.exit(1)
    except Exception as e:
        print(f"Error closing workspace: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Killed workspace '{title}'.")


def cmd_respawn(args):
    """Snapshot, kill, and restore a workspace in one step."""
    if not args.workspace:
        print("Error: -w/--workspace is required for respawn.", file=sys.stderr)
        sys.exit(1)

    ws_filter = args.workspace

    # Step 1: Verify workspace exists before we start
    ref, title = _find_workspace_ref(ws_filter)

    # Show what will happen
    cmux_data = load_cmux_session()
    panel_count = 0
    claude_count = 0
    for win in cmux_data.get("windows", []):
        for ws in win.get("tabManager", {}).get("workspaces", []):
            ws_title = ws.get("customTitle") or ws.get("title") or os.path.basename(ws.get("currentDirectory", ""))
            if ws_filter.lower() in ws_title.lower():
                panels = ws.get("panels", [])
                panel_count = len(panels)
                claude_count = sum(
                    1 for p in panels
                    if _is_claude_title(p.get("title", ""))
                )
                break

    print(f"Respawn workspace: {title}")
    print(f"  Panels: {panel_count} ({claude_count} Claude, {panel_count - claude_count} terminal)")
    print(f"  This will: snapshot → kill → restore")
    print()

    if args.yes:
        confirmed = True
    else:
        try:
            answer = input(f"Respawn '{title}'? [y/N] ")
            confirmed = answer.strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(1)

    if not confirmed:
        print("Aborted.")
        return

    # Step 1: Snapshot
    print(f"\n[1/3] Snapshotting '{title}'...")
    snap_args = argparse.Namespace(workspace=ws_filter, output=None)
    cmd_snapshot(snap_args)

    # Step 2: Kill
    print(f"\n[2/3] Killing '{title}'...")
    kill_args = argparse.Namespace(workspace=ws_filter, yes=True)
    cmd_kill(kill_args)

    # Step 3: Restore
    print(f"\n[3/3] Restoring '{title}'...")
    restore_args = argparse.Namespace(workspace=ws_filter, file=None, dry_run=False)
    cmd_restore(restore_args)


# ── launchd auto-snapshot watcher ────────────────────────────


def _launchctl(*args):
    """Run launchctl; return (rc, stdout+stderr)."""
    try:
        r = subprocess.run(
            ["launchctl", *args], capture_output=True, text=True, timeout=10
        )
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:
        return 1, str(e)


def _watch_plist_xml(script_path, python_bin, throttle, auto_prune):
    """Build the LaunchAgent plist content."""
    args_xml = "\n".join(
        f"        <string>{x}</string>"
        for x in [python_bin, script_path, "snapshot", "--auto-prune", str(auto_prune)]
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{WATCH_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
{args_xml}
    </array>
    <key>WatchPaths</key>
    <array>
        <string>{CMUX_SESSION_FILE}</string>
    </array>
    <key>ThrottleInterval</key>
    <integer>{throttle}</integer>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>{WATCH_LOG}</string>
    <key>StandardErrorPath</key>
    <string>{WATCH_LOG}</string>
</dict>
</plist>
"""


def cmd_install_watch(args):
    """Install a launchd agent that snapshots on every cmux state file change."""
    # Launchd agents can't read ~/Documents, ~/Desktop, or ~/Downloads under
    # macOS TCC unless the user grants Full Disk Access. Sidestep by copying
    # the script into SNAPSHOT_DIR (home root, no TCC restriction).
    import shutil
    source_path = os.path.realpath(__file__)
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    script_path = os.path.join(SNAPSHOT_DIR, "cmux-sessions-watch.py")
    shutil.copy2(source_path, script_path)
    os.chmod(script_path, 0o755)
    python_bin = sys.executable

    if not os.path.exists(CMUX_SESSION_FILE):
        print(f"Warning: cmux session file not found at {CMUX_SESSION_FILE}", file=sys.stderr)
        print("        The agent will install but won't fire until cmux creates the file.", file=sys.stderr)

    os.makedirs(os.path.dirname(WATCH_PLIST), exist_ok=True)

    # If already installed, bootout first so the new plist takes effect.
    uid = os.getuid()
    if os.path.exists(WATCH_PLIST):
        _launchctl("bootout", f"gui/{uid}", WATCH_PLIST)

    plist_xml = _watch_plist_xml(script_path, python_bin, args.throttle, args.auto_prune)
    with open(WATCH_PLIST, "w") as f:
        f.write(plist_xml)

    rc, out = _launchctl("bootstrap", f"gui/{uid}", WATCH_PLIST)
    if rc != 0:
        print(f"Error: launchctl bootstrap failed: {out}", file=sys.stderr)
        sys.exit(1)

    print(f"Installed: {WATCH_PLIST}")
    print(f"  Label:         {WATCH_LABEL}")
    print(f"  Script:        {script_path}  (copy of source — re-run install-watch after edits)")
    print(f"  Throttle:      {args.throttle}s (max one snapshot per interval)")
    print(f"  Auto-prune:    keep last {args.auto_prune} snapshots")
    print(f"  Log:           {WATCH_LOG}")
    print(f"  Watches:       {CMUX_SESSION_FILE}")
    print()
    print("The agent fires whenever cmux writes its session state. To test:")
    print("  - Open or close a workspace in cmux.")
    print(f"  - Within {args.throttle}s, a new snapshot should appear in {SNAPSHOT_DIR}.")
    print("  - Run `cmux-sessions watch-status` to inspect.")


def cmd_uninstall_watch(args):
    """Remove the launchd auto-snapshot agent."""
    if not os.path.exists(WATCH_PLIST):
        print(f"Not installed: {WATCH_PLIST}")
        return

    uid = os.getuid()
    rc, out = _launchctl("bootout", f"gui/{uid}", WATCH_PLIST)
    # bootout returns nonzero if the agent isn't loaded; that's OK.
    if rc != 0 and "Could not find" not in out and "No such process" not in out:
        print(f"Warning: launchctl bootout: {out}", file=sys.stderr)

    os.remove(WATCH_PLIST)
    # Also remove the script copy (kept the source untouched).
    script_copy = os.path.join(SNAPSHOT_DIR, "cmux-sessions-watch.py")
    if os.path.exists(script_copy):
        os.remove(script_copy)
    print(f"Removed:   {WATCH_PLIST}")
    print(f"Removed:   {script_copy}")
    print(f"Log retained: {WATCH_LOG}")


def cmd_watch_status(args):
    """Show the auto-snapshot agent's state and recent activity."""
    print(f"Plist:         {WATCH_PLIST}")
    if not os.path.exists(WATCH_PLIST):
        print("Status:        not installed")
        print()
        print("Install with:  cmux-sessions install-watch")
        return

    uid = os.getuid()
    rc, out = _launchctl("print", f"gui/{uid}/{WATCH_LABEL}")
    loaded = rc == 0
    print(f"Status:        {'loaded' if loaded else 'plist exists but not loaded'}")

    # Last few key lines from launchctl print
    if loaded:
        for line in out.splitlines():
            ln = line.strip()
            for k in ("state =", "last exit code =", "throttle interval =", "runs ="):
                if ln.startswith(k):
                    print(f"  {ln}")
                    break

    # Most recent snapshots
    snaps = sorted(Path(SNAPSHOT_DIR).glob("cmux-*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)[:5]
    if snaps:
        print()
        print("Recent snapshots:")
        for p in snaps:
            ts = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            print(f"  {ts}  {p.name}")

    # Tail of the log
    if os.path.exists(WATCH_LOG):
        print()
        print(f"Log tail ({WATCH_LOG}):")
        try:
            with open(WATCH_LOG) as f:
                lines = f.readlines()
            for line in lines[-10:]:
                print(f"  {line.rstrip()}")
        except Exception as e:
            print(f"  (could not read: {e})")


# ── Main ─────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Snapshot and restore cmux workspaces with Claude Code sessions",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  %(prog)s list                            Show Claude sessions across workspaces
  %(prog)s show -w myproject               Show detailed workspace info (live)
  %(prog)s show -w myproject -f snap.json  Show detailed workspace info (snapshot)
  %(prog)s snapshot                        Save current state (all workspaces)
  %(prog)s snapshot -w myproject           Snapshot only matching workspace
  %(prog)s snapshot -n before-refactor     Save with a name instead of timestamp
  %(prog)s snapshots                       List all saved snapshots
  %(prog)s diff                            Compare snapshot vs live workspaces
  %(prog)s validate                        Check snapshot health before restoring
  %(prog)s validate -f snap.json -w proj   Validate specific snapshot/workspace
  %(prog)s prune                           Delete old snapshots (keep last 10)
  %(prog)s prune --keep 5                  Keep last 5 snapshots
  %(prog)s restore --dry-run               Preview what would be restored
  %(prog)s restore -w myproject            Restore only matching workspace
  %(prog)s restore --skip-active           Restore only closed workspaces
  %(prog)s restore --run-commands          Re-run captured terminal commands
  %(prog)s kill -w myproject               Close a workspace (with confirmation)
  %(prog)s respawn -w myproject            Snapshot, kill, and restore a workspace
""",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # list
    sub.add_parser("list", help="List active Claude sessions in cmux")

    # snapshot
    p_snap = sub.add_parser("snapshot", help="Capture current cmux + Claude state")
    p_snap.add_argument("-o", "--output", help="Output file (default: ~/.cmux-snapshots/cmux-<timestamp>.json)")
    p_snap.add_argument("-w", "--workspace", help="Snapshot only this workspace (name substring or index)")
    p_snap.add_argument("-n", "--name", help="Give the snapshot a name (e.g. before-refactor)")
    p_snap.add_argument("--auto-prune", type=int, default=None,
                        help="After snapshotting, prune to keep last N (used by auto-watch)")

    # show
    p_show = sub.add_parser("show", help="Show detailed workspace info")
    p_show.add_argument("-w", "--workspace", help="Workspace name (substring match)")
    p_show.add_argument("-f", "--file", help="Show from snapshot file instead of live state")

    # snapshots
    sub.add_parser("snapshots", help="List available snapshots")

    # restore
    p_restore = sub.add_parser("restore", help="Restore workspaces from a snapshot")
    p_restore.add_argument("-f", "--file", help="Snapshot file (default: latest)")
    p_restore.add_argument("-w", "--workspace", help="Restore only this workspace (name substring or index)")
    p_restore.add_argument("--dry-run", action="store_true", help="Preview without executing")
    p_restore.add_argument("--run-commands", action="store_true", help="Re-run captured terminal commands (use with caution)")
    p_restore.add_argument("--skip-active", action="store_true", help="Skip workspaces that are already open (restore only closed ones)")

    # diff
    p_diff = sub.add_parser("diff", help="Compare snapshot against live workspaces")
    p_diff.add_argument("-f", "--file", help="Snapshot file (default: latest)")

    # validate
    p_val = sub.add_parser("validate", help="Check snapshot health before restoring")
    p_val.add_argument("-f", "--file", help="Snapshot file (default: latest)")
    p_val.add_argument("-w", "--workspace", help="Validate only this workspace")

    # prune
    p_prune = sub.add_parser("prune", help="Delete old snapshots, keep last N")
    p_prune.add_argument("--keep", type=int, default=10, help="Number of snapshots to keep (default: 10)")

    # kill
    p_kill = sub.add_parser("kill", help="Close a workspace (with confirmation)")
    p_kill.add_argument("-w", "--workspace", required=True, help="Workspace to close (name substring or ref)")
    p_kill.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")

    # respawn
    p_respawn = sub.add_parser("respawn", help="Snapshot, kill, and restore a workspace")
    p_respawn.add_argument("-w", "--workspace", required=True, help="Workspace to respawn (name substring)")
    p_respawn.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")

    # install-watch
    p_iw = sub.add_parser("install-watch",
                          help="Install a launchd agent that auto-snapshots on cmux state changes")
    p_iw.add_argument("--throttle", type=int, default=30,
                      help="Min seconds between snapshots (default: 30)")
    p_iw.add_argument("--auto-prune", type=int, default=50,
                      help="Keep last N snapshots after each auto-snapshot (default: 50)")

    # uninstall-watch
    sub.add_parser("uninstall-watch", help="Remove the launchd auto-snapshot agent")

    # watch-status
    sub.add_parser("watch-status", help="Show auto-snapshot agent state and recent snapshots")

    args = parser.parse_args()
    commands = {
        "list": cmd_list,
        "show": cmd_show,
        "snapshot": cmd_snapshot,
        "snapshots": cmd_snapshots,
        "diff": cmd_diff,
        "validate": cmd_validate,
        "prune": cmd_prune,
        "restore": cmd_restore,
        "kill": cmd_kill,
        "respawn": cmd_respawn,
        "install-watch": cmd_install_watch,
        "uninstall-watch": cmd_uninstall_watch,
        "watch-status": cmd_watch_status,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
