#!/bin/bash
# Set up a fold-back marker for the current fork session.
# When Claude finishes its next turn, the Stop hook reads this marker,
# routes the assistant's last message to the parent's iTerm2 pane, and
# closes this fork.
set -e

PY="${CLAUDE_FOLD_BACK_PYTHON:-${CLAUDE_FORK_SPLIT_PYTHON:-python3}}"

"$PY" - "$ITERM_SESSION_ID" <<'PYEOF'
import json, os, re, subprocess, sys, time
from pathlib import Path


def parent_claude_command(start_pid):
    """Walk up the process tree, find the nearest `claude --resume X --fork-session` ancestor."""
    pid = start_pid
    seen = set()
    for _ in range(20):
        if pid in seen or pid in (0, 1):
            break
        seen.add(pid)
        try:
            r = subprocess.run(
                ["ps", "-p", str(pid), "-o", "ppid=,command="],
                capture_output=True, text=True, timeout=2,
            )
        except Exception:
            return ""
        if r.returncode != 0 or not r.stdout.strip():
            return ""
        parts = r.stdout.strip().split(None, 1)
        try:
            ppid = int(parts[0].strip())
        except ValueError:
            return ""
        cmd = parts[1] if len(parts) > 1 else ""
        if re.search(r"(^|[/ ])claude(\.js)?(\s|$)", cmd):
            return cmd
        pid = ppid
    return ""


def find_fork_jsonl_and_sid():
    """Look up our own session via ~/.claude/sessions/<pid>.json — pid here is the
    nearest claude ancestor's pid. Returns (sid, jsonl_path, fork_pid_tty)."""
    pid = os.getpid()
    sessions_dir = Path.home() / ".claude/sessions"
    seen = set()
    for _ in range(20):
        if pid in seen or pid in (0, 1):
            break
        seen.add(pid)
        try:
            r = subprocess.run(
                ["ps", "-p", str(pid), "-o", "ppid=,tty=,command="],
                capture_output=True, text=True, timeout=2,
            )
        except Exception:
            return None, None, None
        if r.returncode != 0 or not r.stdout.strip():
            return None, None, None
        parts = r.stdout.strip().split(None, 2)
        try:
            ppid = int(parts[0].strip())
        except ValueError:
            return None, None, None
        tty = parts[1].strip() if len(parts) > 1 else ""
        cmd = parts[2] if len(parts) > 2 else ""
        if re.search(r"(^|[/ ])claude(\.js)?(\s|$)", cmd):
            pf = sessions_dir / f"{pid}.json"
            if pf.exists():
                try:
                    data = json.loads(pf.read_text(encoding="utf-8"))
                    sid = data.get("sessionId")
                    cwd = data.get("cwd") or ""
                    if sid and cwd:
                        enc = cwd.replace("/", "-").replace(".", "-").replace(" ", "-")
                        jf = Path.home() / ".claude/projects" / enc / f"{sid}.jsonl"
                        return sid, jf, ("/dev/" + tty if tty and not tty.startswith("/dev/") else tty)
                except Exception:
                    pass
        pid = ppid
    return None, None, None


def find_pane_tty_for_sid(target_sid):
    """Find the iTerm2 pane TTY currently running the parent session."""
    sessions_dir = Path.home() / ".claude/sessions"
    if not sessions_dir.exists():
        return None
    for pf in sessions_dir.iterdir():
        if pf.suffix != ".json":
            continue
        try:
            data = json.loads(pf.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("sessionId") != target_sid:
            continue
        try:
            pid = int(pf.stem)
        except ValueError:
            continue
        try:
            r = subprocess.run(
                ["ps", "-p", str(pid), "-o", "tty="],
                capture_output=True, text=True, timeout=2,
            )
        except Exception:
            continue
        tty = r.stdout.strip()
        if not tty or tty == "??":
            return None
        return tty if tty.startswith("/dev/") else f"/dev/{tty}"
    return None


fork_sid, fork_jsonl, fork_tty = find_fork_jsonl_and_sid()
if not fork_sid or not fork_jsonl:
    print("fold-back: could not identify the current fork session", file=sys.stderr)
    sys.exit(1)

parent_cmd = parent_claude_command(os.getpid())
m = re.search(r"--resume[=\s]+(\S+)", parent_cmd or "")
fork_flag = "--fork-session" in (parent_cmd or "")
if not m or not fork_flag:
    print(
        "fold-back: this session doesn't look like a /fork-split fork — refusing.\n"
        "(no --resume … --fork-session in the ancestor claude process)",
        file=sys.stderr,
    )
    sys.exit(1)
parent_sid = m.group(1)

parent_tty = find_pane_tty_for_sid(parent_sid)
if not parent_tty:
    print(
        f"fold-back: parent session {parent_sid[:8]} isn't running anywhere reachable. "
        "Resume it in a pane, then retry.",
        file=sys.stderr,
    )
    sys.exit(1)

marker_dir = Path.home() / ".claude/fold-back-pending"
marker_dir.mkdir(parents=True, exist_ok=True)
marker = marker_dir / f"{fork_sid}.json"
marker.write_text(
    json.dumps(
        {
            "parent_sid": parent_sid,
            "parent_tty": parent_tty,
            "fork_sid": fork_sid,
            "fork_tty": fork_tty,
            "fork_jsonl": str(fork_jsonl),
            "created_at": time.time(),
        },
        indent=2,
    )
)

print(f"fold-back marker set: parent={parent_sid[:8]} ({parent_tty}). "
      f"Write your digest now; the Stop hook will route it.")
PYEOF
