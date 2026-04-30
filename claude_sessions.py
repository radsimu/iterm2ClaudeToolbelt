#!/usr/bin/env python3
"""
Claude Code Session Manager — iTerm2 Toolbelt Widget
"""

import asyncio
import datetime
import json
import os
import queue
import shlex
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Optional

import iterm2

# ── Constants ─────────────────────────────────────────────────────────────────

PORT = 9837
IDENTIFIER = "com.claude-code.session-manager"
CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
TEAMS_DIR = CLAUDE_DIR / "teams"
PID_TO_SESSION_FILE = CLAUDE_DIR / "pid-to-session.json"
SESSION_STATUS_FILE = CLAUDE_DIR / "session-status.json"
ARCHIVE_FILE = CLAUDE_DIR / "session-manager-archive.json"
ORDER_FILE  = CLAUDE_DIR / "session-manager-order.json"
TEAMS_INDEX_FILE = CLAUDE_DIR / "session-manager-teams.json"
WEIGHTS_FILE = CLAUDE_DIR / "session-manager-weights.json"

TEAMMATE_MARKER = '<teammate-message teammate_id="team-lead">'

# ── Shared state ──────────────────────────────────────────────────────────────

_connection: Optional[iterm2.Connection] = None
_event_loop: Optional[asyncio.AbstractEventLoop] = None

# SSE: event broadcaster — iTerm2 watchers / fs watcher put into each subscriber's queue.
_event_subscribers: list = []
_event_sub_lock = threading.Lock()

# Cache: (path_str, mtime) → _session_git_info result dict
# Keyed by mtime so it's automatically invalidated when the file changes.
_git_info_cache: dict = {}


def _broadcast_refresh(reason: str = "event") -> None:
    with _event_sub_lock:
        subs = list(_event_subscribers)
    for q in subs:
        try:
            q.put_nowait(reason)
        except Exception:
            pass


# ── Pure-Python data layer ────────────────────────────────────────────────────

def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_json(path: Path, data: dict) -> bool:
    try:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return True
    except Exception:
        return False


def _read_archive() -> set:
    try:
        raw = json.loads(ARCHIVE_FILE.read_text(encoding="utf-8"))
        return set(raw if isinstance(raw, list) else [])
    except Exception:
        return set()


def _write_archive(archived: set) -> bool:
    try:
        ARCHIVE_FILE.write_text(json.dumps(sorted(archived), indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def _read_order() -> dict:
    try:
        return json.loads(ORDER_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_order(data: dict) -> bool:
    try:
        ORDER_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return True
    except Exception:
        return False


def _read_teams_index() -> dict:
    raw = _read_json(TEAMS_INDEX_FILE)
    if not isinstance(raw, dict):
        return {"scanned": {}, "team_to_leader": {}}
    raw.setdefault("scanned", {})
    raw.setdefault("team_to_leader", {})
    return raw


def _scan_jsonl_for_team_creates(jf: Path) -> list:
    """Return list of team_names this session created via TeamCreate. Fast-path: byte search first."""
    try:
        raw = jf.read_bytes()
    except Exception:
        return []
    # Byte-level fast check — skip expensive JSON parse if the file never mentions TeamCreate
    if b'"TeamCreate"' not in raw:
        return []
    names: list = []
    try:
        text = raw.decode("utf-8", errors="ignore")
    except Exception:
        return []
    for line in text.splitlines():
        if '"TeamCreate"' not in line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") != "assistant":
            continue
        for blk in d.get("message", {}).get("content", []) or []:
            if not isinstance(blk, dict):
                continue
            if blk.get("type") == "tool_use" and blk.get("name") == "TeamCreate":
                tname = (blk.get("input") or {}).get("team_name")
                if tname and tname not in names:
                    names.append(tname)
    return names


def _refresh_teams_index(session_files: list) -> dict:
    """session_files: list of (sid, jsonl_path, enc, mtime, is_teammate).

    Incremental: only re-scans JSONLs whose mtime changed since last scan. Teammate
    sessions never call TeamCreate so are skipped. Active teams from ~/.claude/teams/
    overlay at the end so live data wins.
    """
    idx = _read_teams_index()
    scanned = idx["scanned"]
    team_to_leader = idx["team_to_leader"]
    live_sids = set()
    changed = False

    for sid, jf, enc, mtime, is_teammate in session_files:
        live_sids.add(sid)
        if is_teammate:
            continue
        prev = scanned.get(sid) or {}
        if prev.get("mtime") == mtime and prev.get("enc") == enc:
            continue
        # Remove this session's old team→leader entries before rescanning
        for t in prev.get("teams", []) or []:
            if team_to_leader.get(t, {}).get("lead_session_id") == sid:
                team_to_leader.pop(t, None)
        teams = _scan_jsonl_for_team_creates(jf)
        scanned[sid] = {"mtime": mtime, "enc": enc, "teams": teams}
        for t in teams:
            team_to_leader[t] = {"lead_session_id": sid, "lead_proj_enc": enc}
        changed = True

    # Drop entries for sessions that no longer exist
    stale = [sid for sid in scanned if sid not in live_sids]
    for sid in stale:
        for t in scanned[sid].get("teams", []) or []:
            if team_to_leader.get(t, {}).get("lead_session_id") == sid:
                team_to_leader.pop(t, None)
        scanned.pop(sid, None)
        changed = True

    # Overlay active team configs (authoritative while teams are alive)
    if TEAMS_DIR.exists():
        try:
            for td in TEAMS_DIR.iterdir():
                if not td.is_dir():
                    continue
                cfg = td / "config.json"
                if not cfg.exists():
                    continue
                try:
                    data = json.loads(cfg.read_text(encoding="utf-8"))
                except Exception:
                    continue
                tname = data.get("name") or td.name
                lsid = data.get("leadSessionId")
                if not (tname and lsid):
                    continue
                prev = team_to_leader.get(tname, {})
                if prev.get("lead_session_id") != lsid:
                    # Resolve enc from the scanned index (leader was scanned as a JSONL)
                    enc = (scanned.get(lsid) or {}).get("enc", "")
                    team_to_leader[tname] = {"lead_session_id": lsid, "lead_proj_enc": enc}
                    changed = True
        except Exception:
            pass

    if changed:
        _write_json(TEAMS_INDEX_FILE, idx)
    return team_to_leader


def _model_max_context(model: str, peak_input_tokens: int = 0) -> int:
    """Best-effort max-context-window for a Claude model.

    Most current Claude models default to 200K. The 1M-context variants carry a
    `[1m]` suffix in the id, but Claude Code strips it before writing to the
    JSONL — so the only reliable signal for 1M mode is observed usage. If the
    session ever exceeded 200K input tokens, it has to be running in 1M mode.
    """
    if "[1m]" in (model or ""):
        return 1_000_000
    if peak_input_tokens > 200_000:
        return 1_000_000
    return 200_000


def _refresh_weights_index(session_files: list) -> dict:
    """session_files: list of (sid, jsonl_path, mtime). Returns {sid: weight_dict}.

    Incremental: for each session, scan only bytes after last_offset. If the file
    shrunk (truncation or replacement), reset and rescan. Writes cache back to
    WEIGHTS_FILE when anything changed.
    """
    raw = _read_json(WEIGHTS_FILE)
    cache = raw if isinstance(raw, dict) else {}
    live_sids = set()
    changed = False

    for sid, jf, mtime in session_files:
        live_sids.add(sid)
        try:
            size = jf.stat().st_size
        except Exception:
            continue
        prev = cache.get(sid) or {}
        # Force a full rescan for entries written before we started tracking
        # current_input_tokens / model — those need to be backfilled.
        needs_backfill = ("current_input_tokens" not in prev) or ("model" not in prev)
        if not needs_backfill and prev.get("mtime") == mtime and prev.get("size") == size:
            continue

        if needs_backfill:
            start_offset = 0
            output_tokens = 0
            max_input_tokens = 0
            compactions = 0
            current_input_tokens = 0
            model = ""
        else:
            start_offset = prev.get("last_offset", 0)
            output_tokens = prev.get("output_tokens", 0)
            max_input_tokens = prev.get("max_input_tokens", 0)
            compactions = prev.get("compactions", 0)
            current_input_tokens = prev.get("current_input_tokens", 0)
            model = prev.get("model", "")

        # If the file is smaller than our last offset, it was truncated/replaced — restart.
        if size < start_offset:
            start_offset = 0
            output_tokens = 0
            max_input_tokens = 0
            compactions = 0
            current_input_tokens = 0
            model = ""

        try:
            with jf.open("rb") as fh:
                fh.seek(start_offset)
                # Read only the new portion
                data = fh.read(size - start_offset)
        except Exception:
            continue

        if data:
            try:
                text = data.decode("utf-8", errors="ignore")
            except Exception:
                text = ""
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = d.get("type")
                if t == "compact_boundary" or d.get("subtype") == "compact_boundary":
                    compactions += 1
                msg = d.get("message") or {}
                u = msg.get("usage") or {}
                if u:
                    output_tokens += int(u.get("output_tokens") or 0)
                    itok = (int(u.get("input_tokens") or 0)
                            + int(u.get("cache_read_input_tokens") or 0)
                            + int(u.get("cache_creation_input_tokens") or 0))
                    if itok > max_input_tokens:
                        max_input_tokens = itok
                    # current_input_tokens / model only update on real assistant
                    # records; <synthetic> ones (and tool wrappers) don't reflect
                    # the actual context size of the next user-facing turn.
                    if t == "assistant":
                        m = msg.get("model") or ""
                        if m and m != "<synthetic>":
                            current_input_tokens = itok
                            model = m

        cache[sid] = {
            "mtime": mtime,
            "size": size,
            "last_offset": size,
            "output_tokens": output_tokens,
            "max_input_tokens": max_input_tokens,
            "compactions": compactions,
            "current_input_tokens": current_input_tokens,
            "model": model,
        }
        changed = True

    # Drop entries for sessions that no longer exist
    stale = [sid for sid in list(cache) if sid not in live_sids]
    for sid in stale:
        cache.pop(sid, None)
        changed = True

    if changed:
        _write_json(WEIGHTS_FILE, cache)
    return cache


def _ids_in_order(nodes) -> set:
    ids: set = set()
    for node in nodes:
        if isinstance(node, str):
            ids.add(node)
        else:
            ids.add(node["id"])
            ids |= _ids_in_order(node.get("children", []))
    return ids


def _build_tree(order_nodes, sess_map) -> list:
    result = []
    for node in order_nodes:
        sid = node if isinstance(node, str) else node["id"]
        child_nodes = [] if isinstance(node, str) else node.get("children", [])
        if sid not in sess_map:
            continue
        s = sess_map[sid].copy()
        s["children"] = _build_tree(child_nodes, sess_map)
        result.append(s)
    return result


def _remove_from_tree(nodes, sid) -> list:
    result = []
    for node in nodes:
        node_id = node if isinstance(node, str) else node["id"]
        if node_id == sid:
            continue
        if isinstance(node, dict):
            node = {**node, "children": _remove_from_tree(node.get("children", []), sid)}
        result.append(node)
    return result


def _insert_as_child(nodes, parent_sid, child_sid):
    """Return a new tree with child_sid inserted as first child under parent_sid."""
    out = []
    for node in nodes:
        if isinstance(node, str):
            if node == parent_sid:
                out.append({"id": node, "children": [child_sid]})
            else:
                out.append(node)
        else:
            children = node.get("children") or []
            if node["id"] == parent_sid:
                out.append({**node, "children": [child_sid] + list(children)})
            else:
                out.append({**node, "children": _insert_as_child(children, parent_sid, child_sid)})
    return out


def _project_path_from_jsonl(proj_dir: Path) -> Optional[str]:
    """Scan JSONL files (oldest first) for a cwd entry — that's the launch directory."""
    try:
        files = sorted(
            (f for f in proj_dir.iterdir() if f.suffix == ".jsonl"),
            key=lambda f: f.stat().st_mtime,
        )
    except Exception:
        return None
    for jf in files:
        try:
            with jf.open(encoding="utf-8") as fh:
                for i, line in enumerate(fh):
                    if i >= 30:
                        break
                    try:
                        cwd = json.loads(line).get("cwd")
                        if cwd:
                            return cwd
                    except json.JSONDecodeError:
                        continue
        except Exception:
            continue
    return None


def _session_git_info(jf: Path) -> dict:
    """Return dict: branch, cwd, custom_title, recap, team_name, agent_name, is_teammate.

    custom_title: most recent custom-title entry (written by /rename or the widget).
    recap:        away_summary → first user prompt (tooltip; custom_title excluded).
    is_teammate:  first real user message starts with the teammate-message wrapper.
    team_name/agent_name: pulled from the teammate's own JSONL records (present on every message).

    Result is cached by mtime — the file is only re-read when it has changed.
    """
    try:
        mtime = jf.stat().st_mtime
    except Exception:
        mtime = None

    key = (str(jf), mtime)
    if key in _git_info_cache:
        return _git_info_cache[key]

    branch = cwd = custom_title = recap = None
    team_name = agent_name = None
    is_teammate = False
    working = False
    try:
        with jf.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()

            # Read enough from the start that we'll usually catch the first user
            # message. Some sessions (subagent / hook-launched ones) prepend
            # huge queue-operation records and push the first user record well
            # past 16KB.
            f.seek(0)
            head = f.read(min(size, 524288)).decode("utf-8", errors="ignore")

            f.seek(max(0, size - 8192))
            small_tail = f.read().decode("utf-8", errors="ignore")

            f.seek(max(0, size - 524288))
            large_tail = f.read().decode("utf-8", errors="ignore")

        # Capture branch/cwd from a small recent slice (cheap).
        for line in reversed(small_tail.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("gitBranch") and d.get("cwd"):
                branch, cwd = d["gitBranch"], d["cwd"]
                break

        # Activity classification scans large_tail (512KB) — small_tail can fall
        # entirely inside a single huge tool_use record, hiding the real signal.
        _meta_types = {
            "system", "custom-title", "permission-mode", "file-history-snapshot",
            "queue-operation", "last-prompt", "agent-name", "pr-link", "attachment",
        }
        # Wrappers that show up as `type:"user"` records but are harness-injected
        # notifications, not actual user input the model has to answer.
        _systemy_user_prefixes = (
            "<system-reminder>",
            "<local-command-caveat>",
            "<local-command-stdout>",
            "<local-command-stderr>",
            "<command-name>",
            "<command-message>",
            "<command-stdout>",
            "<command-stderr>",
        )
        for line in reversed(large_tail.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = d.get("type")
            if not t or t in _meta_types:
                continue
            if t == "assistant":
                stop = (d.get("message") or {}).get("stop_reason")
                working = stop not in ("end_turn", "stop_sequence", None)
                break
            if t == "user":
                content = (d.get("message") or {}).get("content")
                # tool_result → model is mid-turn, definitely working
                if isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in content
                ):
                    working = True
                    break
                # Extract text portion, if any
                txt = ""
                if isinstance(content, str):
                    txt = content
                elif isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "text":
                            txt = b.get("text") or ""
                            break
                # Pure harness-injected wrapper → keep walking back
                if txt.startswith(_systemy_user_prefixes):
                    continue
                working = True
                break
            break

        for line in reversed(large_tail.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                # custom-title: accept empty string as an intentional clear
                if custom_title is None and d.get("type") == "custom-title":
                    ct = d.get("customTitle")
                    if ct is not None:
                        custom_title = ct
                # recap: away_summary only (custom-title is now the label)
                if not recap and d.get("type") == "system" and d.get("subtype") == "away_summary":
                    recap = d.get("content") or None
                if custom_title is not None and recap:
                    break
            except json.JSONDecodeError:
                continue

        # First-pass over head: find first real user prompt + detect teammate + grab team/agent names
        for line in head.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("type") == "user":
                content = d.get("message", {}).get("content", "")
                if isinstance(content, str):
                    text = content.strip()
                elif isinstance(content, list):
                    text = next(
                        (c.get("text", "").strip() for c in content
                         if isinstance(c, dict) and c.get("type") == "text"
                         and c.get("text", "").strip()),
                        "",
                    )
                else:
                    text = ""
                # Skip slash-command echoes / system caveats / harness reminders — those aren't real prompts.
                _skip_prefixes = (
                    "<system-reminder>",
                    "<local-command-caveat>",
                    "<local-command-stdout>",
                    "<local-command-stderr>",
                    "<command-name>",
                    "<command-message>",
                    "<command-stdout>",
                    "<command-stderr>",
                )
                if text and not text.startswith(_skip_prefixes):
                    if text.startswith(TEAMMATE_MARKER):
                        is_teammate = True
                    if not recap:
                        snippet = text
                        if is_teammate:
                            # Strip the wrapper so the tooltip shows the actual task
                            snippet = snippet[len(TEAMMATE_MARKER):].lstrip()
                            end = snippet.find("</teammate-message>")
                            if end != -1:
                                snippet = snippet[:end].strip()
                        recap = snippet[:200].replace("\n", " ")
                    # Only need the first real user message
                    if is_teammate or recap:
                        pass  # continue to also collect team_name/agent_name below
                    break
            # teamName/agentName sit on user/assistant records
            if team_name is None and d.get("teamName"):
                team_name = d.get("teamName")
            if agent_name is None and d.get("agentName"):
                agent_name = d.get("agentName")

        # Second pass if we didn't get team_name/agent_name yet — scan a bit further
        if is_teammate and (not team_name or not agent_name):
            for line in head.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if team_name is None and d.get("teamName"):
                    team_name = d.get("teamName")
                if agent_name is None and d.get("agentName"):
                    agent_name = d.get("agentName")
                if team_name and agent_name:
                    break

    except Exception:
        pass

    result = {
        "branch": branch,
        "cwd": cwd,
        "custom_title": custom_title,
        "recap": recap,
        "team_name": team_name,
        "agent_name": agent_name,
        "is_teammate": is_teammate,
        "working": working,
    }
    if mtime is not None:
        _git_info_cache[key] = result
        # Evict stale entries for the same path (previous mtime values)
        for k in [k for k in _git_info_cache if k[0] == str(jf) and k[1] != mtime]:
            del _git_info_cache[k]
    return result


def _abbrev_path(path: str) -> str:
    """Shorten a path for display: ~/Work/foo/bar or last 3 components."""
    home = str(Path.home())
    if path.startswith(home):
        path = "~" + path[len(home):]
    parts = path.replace("\\", "/").split("/")
    if len(parts) > 4:
        return "/".join(parts[:2]) + "/…/" + "/".join(parts[-2:])
    return path


def _encode_project_path(path: str) -> str:
    """Inverse of _decode_project_path. Claude maps `/`, `.`, and ` ` all to `-`."""
    return path.replace("/", "-").replace(".", "-").replace(" ", "-")


def _decode_project_path(encoded: str) -> Optional[str]:
    """Resolve encoded dir name to a real filesystem path.

    Claude encodes `/`, `.`, and ` ` all as `-`. Reversing is ambiguous on the
    plain string, so we walk the filesystem: at each level, look for a real
    subdir whose encoded basename matches a prefix of the remaining parts.
    Greedy-longest wins on ties.
    """
    if not encoded.startswith("-"):
        return None
    parts = encoded[1:].split("-")
    current = "/"
    remaining = list(parts)
    while remaining:
        try:
            entries = os.listdir(current)
        except Exception:
            return None
        best_consumed = 0
        best_entry = None
        for entry in entries:
            entry_path = os.path.join(current, entry)
            if not os.path.isdir(entry_path):
                continue
            enc = entry.replace(".", "-").replace(" ", "-").replace("/", "-")
            enc_parts = enc.split("-")
            n = len(enc_parts)
            if n > len(remaining) or n <= best_consumed:
                continue
            if remaining[:n] == enc_parts:
                best_consumed = n
                best_entry = entry
        if best_entry is None:
            return None
        current = os.path.join(current, best_entry)
        remaining = remaining[best_consumed:]
    return current if current != "/" else None


def _arg_value(args: str, flag: str) -> Optional[str]:
    """Extract the value following --flag in a command-line args string."""
    parts = args.split()
    try:
        idx = parts.index(flag)
    except ValueError:
        return None
    return parts[idx + 1] if idx + 1 < len(parts) else None


def _scan_claude_processes() -> list:
    """Return list of (pid, tty_raw, args_str) for every claude CLI or teammate process."""
    try:
        r = subprocess.run(
            ["ps", "-axo", "pid=,tty=,command="],
            capture_output=True, text=True, timeout=3,
        )
    except Exception:
        return []
    out = []
    for line in r.stdout.splitlines():
        line = line.rstrip()
        if not line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid, tty, args = parts
        # Skip ourselves
        if "claude_sessions" in args:
            continue
        first = args.split()[0] if args else ""
        base = os.path.basename(first)
        is_claude = (
            base == "claude"
            or "/.local/share/claude/versions/" in first
            or "/claude.app/Contents/MacOS/claude" in first
        )
        if is_claude:
            out.append((pid, tty, args))
    return out


def _get_active_session_ttys(teammate_map: Optional[dict] = None) -> dict:
    """Return {session_id: tty} for every running claude process.

    Strategy, in priority order:
    1. ~/.claude/sessions/<pid>.json — Claude itself records the PID → sessionId
       mapping for interactive launches. Authoritative and unambiguous.
    2. --resume <sid> in args — direct mapping (covers teammates too, sometimes).
    3. --agent-name/--team-name — look up (team, agent) → sid via teammate_map.
    """
    teammate_map = teammate_map or {}
    procs = _scan_claude_processes()
    if not procs:
        return {}

    sessions_dir = CLAUDE_DIR / "sessions"
    result: dict = {}

    for pid, raw_tty, args in procs:
        tty = _normalize_tty(raw_tty or "")
        if not tty:
            continue

        # Claude's own per-PID session file — overwritten at startup, removed at exit.
        sf = sessions_dir / f"{pid}.json"
        if sf.exists():
            try:
                sid = json.loads(sf.read_text(encoding="utf-8")).get("sessionId")
            except Exception:
                sid = None
            if sid:
                result[sid] = tty
                continue

        if "--resume" in args:
            parts = args.split()
            try:
                sid = parts[parts.index("--resume") + 1]
                result[sid] = tty
                continue
            except (ValueError, IndexError):
                pass

        if "--agent-name" in args and "--team-name" in args:
            agent = _arg_value(args, "--agent-name")
            team = _arg_value(args, "--team-name")
            if agent and team:
                sid = teammate_map.get((team, agent))
                if sid:
                    result[sid] = tty

    return result


def _pid_tty(pid: str) -> Optional[str]:
    try:
        r = subprocess.run(["ps", "-o", "tty=", "-p", pid], capture_output=True, text=True, timeout=1)
        t = r.stdout.strip()
        return t if t and t != "??" else None
    except Exception:
        return None


def _normalize_tty(tty: str) -> str:
    t = (tty or "").strip()
    if not t or t == "??":
        return ""
    if t.startswith("/dev/"):
        return t
    if t.startswith("tty"):
        return "/dev/" + t
    return "/dev/tty" + t


_SHELL_BASENAMES = {"zsh", "bash", "fish", "sh", "tcsh", "csh", "ksh", "dash"}


def _tty_at_shell_prompt(tty: str) -> bool:
    """Return True if the foreground process on `tty` is a login shell.

    macOS marks the foreground process group with `+` in its `stat` column.
    """
    norm = (tty or "").replace("/dev/", "").strip()
    if not norm:
        return False
    try:
        r = subprocess.run(
            ["ps", "-t", norm, "-o", "stat=,command="],
            capture_output=True, text=True, timeout=2,
        )
    except Exception:
        return False
    for line in r.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) < 2:
            continue
        stat, cmd = parts
        if "+" not in stat:
            continue
        first = cmd.split()[0] if cmd else ""
        # Strip leading dash (login shell convention) and any path component.
        base = first.lstrip("-").rsplit("/", 1)[-1]
        if base in _SHELL_BASENAMES:
            return True
    return False


def _relative_time(mtime: float) -> str:
    delta = datetime.datetime.now() - datetime.datetime.fromtimestamp(mtime)
    if delta.days >= 1:
        return f"{delta.days}d ago"
    if delta.seconds >= 3600:
        return f"{delta.seconds // 3600}h ago"
    if delta.seconds >= 60:
        return f"{delta.seconds // 60}m ago"
    return "just now"


def build_projects() -> list:
    archived_set = _read_archive()
    order_data = _read_order()
    order_changed = False

    projects: list = []
    if not PROJECTS_DIR.exists():
        return projects

    # Phase 1 — collect flat session data for every project.
    phase1: list = []  # [(enc, project_path, jsonl_files, flat), ...]
    session_index_input: list = []  # for _refresh_teams_index
    weight_input: list = []  # (sid, jf, mtime) for _refresh_weights_index
    for proj_dir in PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        try:
            jsonl_files = [f for f in proj_dir.iterdir() if f.suffix == ".jsonl"]
        except PermissionError:
            continue
        if not jsonl_files:
            continue

        project_path = _decode_project_path(proj_dir.name) or _project_path_from_jsonl(proj_dir) or ""

        flat: list = []
        for jf in jsonl_files:
            sid = jf.stem
            try:
                mtime = jf.stat().st_mtime
            except Exception:
                continue
            info = _session_git_info(jf)
            branch = info["branch"]
            sess_cwd = info["cwd"]
            custom_title = info["custom_title"]
            recap = info["recap"]
            is_teammate = info["is_teammate"]
            team_name = info["team_name"] or ""
            agent_name = info["agent_name"] or ""
            working = bool(info.get("working"))
            is_worktree = False
            if sess_cwd:
                try:
                    is_worktree = Path(sess_cwd, ".git").is_file()
                except Exception:
                    pass
            # Fallback label when the user hasn't named the session.
            # Teammates → agentName. Everyone else → truncated away_summary or
            # first user prompt (already captured in `recap`).
            default_label = ""
            if is_teammate and agent_name:
                default_label = agent_name
            elif recap:
                snippet = recap.strip().replace("\n", " ")
                if len(snippet) > 60:
                    cut = snippet[:60]
                    sp = cut.rfind(" ")
                    if sp > 30:
                        cut = cut[:sp]
                    snippet = cut + "…"
                default_label = snippet
            flat.append({
                "id": sid,
                "label": custom_title or "",
                "default_label": default_label,
                "computed_title": recap or "",
                "_working_jsonl": working,
                "status": "idle",
                "mtime": mtime,
                "time_str": _relative_time(mtime),
                "is_open": False,
                "tty": "",
                "archived": sid in archived_set,
                "branch": branch or "",
                "is_worktree": is_worktree,
                "is_teammate": is_teammate,
                "team_name": team_name,
                "agent_name": agent_name,
                "leader_session_id": "",
                "leader_proj_enc": "",
                "output_tokens": 0,
                "max_input_tokens": 0,
                "compactions": 0,
                "current_input_tokens": 0,
                "model": "",
                "max_context": 200000,
                "children": [],
            })
            session_index_input.append((sid, jf, proj_dir.name, mtime, is_teammate))
            weight_input.append((sid, jf, mtime))

        if not flat:
            continue
        phase1.append((proj_dir.name, project_path, flat))

    # Phase 1b — surface freshly-started sessions that haven't written a JSONL yet
    # (e.g. a brand new claude pane, or `--fork-session` before its first message).
    # We discover them via ~/.claude/sessions/<pid>.json (Claude writes this at startup)
    # and synthesize a placeholder entry under the matching project.
    sessions_dir = CLAUDE_DIR / "sessions"
    if sessions_dir.exists():
        known_sids: set = {s["id"] for _, _, fl in phase1 for s in fl}
        enc_to_idx = {enc: i for i, (enc, _, _) in enumerate(phase1)}
        for pf in sessions_dir.iterdir():
            if pf.suffix != ".json":
                continue
            try:
                pdata = json.loads(pf.read_text(encoding="utf-8"))
            except Exception:
                continue
            sid = pdata.get("sessionId")
            cwd = pdata.get("cwd")
            if not sid or not cwd or sid in known_sids:
                continue
            target_enc = _encode_project_path(cwd)
            idx = enc_to_idx.get(target_enc)
            if idx is None:
                continue
            mtime = (pdata.get("startedAt") or 0) / 1000.0 or time.time()
            jf_stub = PROJECTS_DIR / target_enc / f"{sid}.jsonl"
            phase1[idx][2].append({
                "id": sid,
                "label": "",
                "default_label": pdata.get("name") or "",
                "computed_title": "",
                "_working_jsonl": False,
                "status": "idle",
                "mtime": mtime,
                "time_str": _relative_time(mtime),
                "is_open": False,
                "tty": "",
                "archived": False,
                "branch": "",
                "is_worktree": False,
                "is_teammate": False,
                "team_name": "",
                "agent_name": "",
                "leader_session_id": "",
                "leader_proj_enc": "",
                "output_tokens": 0,
                "max_input_tokens": 0,
                "compactions": 0,
                "current_input_tokens": 0,
                "model": "",
                "max_context": 200000,
                "children": [],
            })
            known_sids.add(sid)
            session_index_input.append((sid, jf_stub, target_enc, mtime, False))
            weight_input.append((sid, jf_stub, mtime))

    # Phase 2 — resolve team → leader and attach to teammate sessions.
    team_to_leader = _refresh_teams_index(session_index_input)
    weights = _refresh_weights_index(weight_input)
    for _, _, flat in phase1:
        for s in flat:
            w = weights.get(s["id"]) or {}
            s["output_tokens"] = int(w.get("output_tokens") or 0)
            s["max_input_tokens"] = int(w.get("max_input_tokens") or 0)
            s["compactions"] = int(w.get("compactions") or 0)
            s["current_input_tokens"] = int(w.get("current_input_tokens") or 0)
            s["model"] = w.get("model") or ""
            s["max_context"] = _model_max_context(s["model"], s["max_input_tokens"])

    # Teammate map (team_name, agent_name) → most-recent sid. Used by process scanner
    # to pin a running teammate process directly to its session without mtime heuristics.
    teammate_map: dict = {}
    teammate_mtime: dict = {}
    for _, _, flat in phase1:
        for s in flat:
            if s["is_teammate"] and s["team_name"] and s["agent_name"]:
                key = (s["team_name"], s["agent_name"])
                if s["mtime"] > teammate_mtime.get(key, 0):
                    teammate_mtime[key] = s["mtime"]
                    teammate_map[key] = s["id"]

    # Phase 2b — scan live claude processes and apply tty/status to each session.
    # Status: "inactive" (no claude process), "working" (something pending in JSONL),
    # or "idle" (process alive, last assistant turn ended).
    session_tty = _get_active_session_ttys(teammate_map)
    for _, _, flat in phase1:
        for s in flat:
            tty = session_tty.get(s["id"], "")
            s["tty"] = tty
            s["is_open"] = bool(tty)
            jsonl_working = s.pop("_working_jsonl", False)
            if not tty:
                s["status"] = "inactive"
            elif jsonl_working:
                s["status"] = "working"
            else:
                s["status"] = "idle"
    all_session_ids = {sid for _, _, flat in phase1 for sid in (s["id"] for s in flat)}
    for _, _, flat in phase1:
        for s in flat:
            if s["is_teammate"] and s["team_name"]:
                entry = team_to_leader.get(s["team_name"])
                if entry and entry.get("lead_session_id") in all_session_ids:
                    s["leader_session_id"] = entry["lead_session_id"]
                    s["leader_proj_enc"] = entry.get("lead_proj_enc", "")

    # One-time migration: pull existing top-level teammates under their same-project leader.
    migrate_teammates = order_data.get("__teammate_migration__") != 1

    # Phase 3 — build per-project trees; auto-nest new teammates under same-project leader.
    for enc, project_path, flat in phase1:
        active_map = {s["id"]: s for s in flat if not s["archived"]}
        archived_list = sorted([s for s in flat if s["archived"]], key=lambda s: s["mtime"], reverse=True)

        order_tree = order_data.get(enc, [])

        if migrate_teammates:
            # Move top-level teammates under same-project leader (anywhere in tree).
            tree_ids = _ids_in_order(order_tree)
            top_level_ids = {node if isinstance(node, str) else node["id"] for node in order_tree}
            moved = False
            for sid in list(top_level_ids):
                s = active_map.get(sid)
                if not s or not s["is_teammate"]:
                    continue
                leader_sid = s["leader_session_id"] if s["leader_proj_enc"] == enc else ""
                if not leader_sid or leader_sid not in tree_ids or leader_sid == sid:
                    continue
                order_tree = _remove_from_tree(order_tree, sid)
                order_tree = _insert_as_child(order_tree, leader_sid, sid)
                moved = True
            if moved:
                order_data[enc] = order_tree
                order_changed = True

        known = _ids_in_order(order_tree)
        new_ids = [sid for sid in sorted(active_map, key=lambda sid: active_map[sid]["mtime"], reverse=True)
                   if sid not in known]
        # Insert non-teammates first so that teammates can find their leader in the tree.
        non_tm_new = [sid for sid in new_ids if not active_map[sid]["is_teammate"]]
        tm_new = [sid for sid in new_ids if active_map[sid]["is_teammate"]]
        for sid in non_tm_new:
            order_tree = [sid] + order_tree
        for sid in tm_new:
            s = active_map[sid]
            leader_sid = s["leader_session_id"] if s["leader_proj_enc"] == enc else ""
            if leader_sid and leader_sid in _ids_in_order(order_tree):
                order_tree = _insert_as_child(order_tree, leader_sid, sid)
            else:
                order_tree = [sid] + order_tree
        if new_ids:
            order_data[enc] = order_tree
            order_changed = True

        active_tree = _build_tree(order_tree, active_map)
        last_active = max(s["mtime"] for s in flat)

        display = Path(project_path).name if project_path else enc
        projects.append({
            "encoded_name": enc,
            "path": project_path,
            "abbrev_path": _abbrev_path(project_path) if project_path else "",
            "display_name": display,
            "sessions": active_tree,
            "archived": archived_list,
            "last_active": last_active,
            "has_open": any(s["is_open"] for s in flat if not s["archived"]),
        })

    if migrate_teammates:
        order_data["__teammate_migration__"] = 1
        order_changed = True

    # Project order: stored as __project_order__ in the order file
    proj_order = order_data.get("__project_order__", [])
    known_proj = set(proj_order)
    new_encs = [p["encoded_name"] for p in projects if p["encoded_name"] not in known_proj]
    if new_encs:
        proj_order = new_encs + proj_order
        order_data["__project_order__"] = proj_order
        order_changed = True

    if order_changed:
        _write_order(order_data)

    enc_to_proj = {p["encoded_name"]: p for p in projects}
    projects = [enc_to_proj[enc] for enc in proj_order if enc in enc_to_proj]
    # Any projects not yet in proj_order (shouldn't happen, but safety net)
    projects += [p for p in enc_to_proj.values() if p["encoded_name"] not in set(proj_order)]

    from collections import Counter
    counts = Counter(p["display_name"] for p in projects)
    for p in projects:
        if counts[p["display_name"]] > 1 and p["path"]:
            parts = Path(p["path"]).parts
            p["display_name"] = "/".join(parts[-2:]) if len(parts) >= 2 else p["path"]

    return projects


# ── iTerm2 async operations ───────────────────────────────────────────────────

async def _refresh_app() -> iterm2.App:
    return await iterm2.async_get_app(_connection)


async def _do_get_window_context() -> dict:
    """Return {frontmost_ttys:[...], focused_tty:str} for the current iTerm2 window.

    focused_tty is the tty of the active session (i.e. the split pane the user is
    typing in). frontmost_ttys covers every session in the frontmost window.
    """
    app = await _refresh_app()
    win = app.current_terminal_window
    if not win:
        return {"frontmost_ttys": [], "focused_tty": ""}
    sessions = [s for t in win.tabs for s in t.sessions]
    if not sessions:
        return {"frontmost_ttys": [], "focused_tty": ""}
    focused_id = None
    try:
        cur_tab = win.current_tab
        cs = cur_tab.current_session if cur_tab else None
        if cs is not None:
            focused_id = cs.session_id
    except Exception:
        focused_id = None
    ttys = await asyncio.gather(
        *[s.async_get_variable("tty") for s in sessions],
        return_exceptions=True,
    )
    focused_tty = ""
    out: list = []
    for s, tty in zip(sessions, ttys):
        if isinstance(tty, Exception):
            continue
        norm = _normalize_tty(tty or "")
        if norm:
            out.append(norm)
            if focused_id and getattr(s, "session_id", None) == focused_id:
                focused_tty = norm
    return {"frontmost_ttys": out, "focused_tty": focused_tty}


async def _do_focus(tty: str) -> bool:
    norm = _normalize_tty(tty)
    if not norm:
        return False
    app = await _refresh_app()
    all_sessions = [
        s for w in app.windows for t in w.tabs for s in t.sessions
    ]
    ttys = await asyncio.gather(
        *[s.async_get_variable("tty") for s in all_sessions],
        return_exceptions=True,
    )
    for session, session_tty in zip(all_sessions, ttys):
        if isinstance(session_tty, Exception):
            continue
        if _normalize_tty(session_tty or "") == norm:
            await session.async_activate(select_tab=True, order_window_front=True)
            return True
    return False


async def _do_resume(session_id: str, project_path: str, mode: str = "tab") -> bool:
    app = await _refresh_app()
    cmd = f"cd {shlex.quote(project_path)} && claude --resume {session_id}\n"

    if mode == "here":
        win = app.current_terminal_window
        sess = win.current_tab.current_session if (win and win.current_tab) else None
        if not sess:
            return False
        await sess.async_send_text(cmd)
        await sess.async_activate(select_tab=True, order_window_front=True)
        return True

    if mode == "split":
        win = app.current_window
        src = win.current_tab.current_session if win and win.current_tab else None
        if src:
            new_sess = await src.async_split_pane(vertical=True)
            if new_sess:
                await asyncio.sleep(0.3)
                await new_sess.async_send_text(cmd)
                await new_sess.async_activate(select_tab=True, order_window_front=True)
                return True
        mode = "tab"  # fall back

    if mode == "window":
        win = await iterm2.Window.async_create(_connection)
        if not win:
            return False
        await asyncio.sleep(0.3)
        tab = win.current_tab
    else:  # "tab" (default)
        windows = app.windows
        if windows:
            tab = await windows[0].async_create_tab()
        else:
            win = await iterm2.Window.async_create(_connection)
            if not win:
                return False
            tab = win.current_tab

    if not tab:
        return False
    await asyncio.sleep(0.4)
    session = tab.current_session
    if not session:
        return False
    await session.async_send_text(cmd)
    await session.async_activate(select_tab=True, order_window_front=True)
    return True


def _run_iterm_op(coro) -> Optional[bool]:
    if _event_loop is None:
        return None
    future = asyncio.run_coroutine_threadsafe(coro, _event_loop)
    try:
        return future.result(timeout=10)
    except Exception as exc:
        print(f"[claude-sessions] iTerm2 op error: {exc}", file=sys.stderr)
        return None


# ── HTTP server ───────────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _send_json(self, data, status: int = 200) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def _handle_sse(self) -> None:
        q: queue.Queue = queue.Queue()
        with _event_sub_lock:
            _event_subscribers.append(q)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            # Initial ping so the client triggers its first load right away.
            self.wfile.write(b"data: hello\n\n")
            self.wfile.flush()
            while True:
                try:
                    reason = q.get(timeout=25)
                    self.wfile.write(f"data: {reason}\n\n".encode())
                    self.wfile.flush()
                except queue.Empty:
                    # Keepalive comment line prevents proxies / browsers from dropping the stream.
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except Exception:
            pass
        finally:
            with _event_sub_lock:
                if q in _event_subscribers:
                    _event_subscribers.remove(q)

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html"):
            self._send_html(MAIN_HTML)
        elif self.path == "/api/events":
            self._handle_sse()
        elif self.path == "/api/data":
            projects = build_projects()
            ctx = _run_iterm_op(_do_get_window_context()) or {}
            tty_set = set(ctx.get("frontmost_ttys") or [])
            focused_tty = ctx.get("focused_tty") or ""
            current_sids: list = []
            focused_sid = ""
            def _collect(nodes):
                nonlocal focused_sid
                for s in nodes:
                    t = s.get("tty") or ""
                    if t and t in tty_set:
                        current_sids.append(s["id"])
                    if t and focused_tty and t == focused_tty:
                        focused_sid = s["id"]
                    _collect(s.get("children") or [])
            for p in projects:
                _collect(p.get("sessions") or [])
                _collect(p.get("archived") or [])
            # The "Use focused pane" resume option is offered only when the
            # focused pane is empty (no claude session) AND its foreground
            # process is a shell waiting for input.
            focused_pane_available = bool(
                focused_tty and not focused_sid and _tty_at_shell_prompt(focused_tty)
            )
            self._send_json({
                "projects": projects,
                "current_window_session_ids": current_sids,
                "focused_session_id": focused_sid,
                "focused_pane_available": focused_pane_available,
            })
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        try:
            body = self._read_body()
            if self.path == "/api/focus":
                ok = _run_iterm_op(_do_focus(body.get("tty", "")))
                self._send_json({"ok": bool(ok)})

            elif self.path == "/api/resume":
                ok = _run_iterm_op(_do_resume(body.get("session_id", ""), body.get("project_path", ""), body.get("mode", "tab")))
                self._send_json({"ok": bool(ok)})

            elif self.path == "/api/rename":
                sid = body.get("session_id", "")
                enc = body.get("encoded_name", "")
                label = body.get("label", "")
                ok = False
                if sid and enc:
                    jf = PROJECTS_DIR / enc / f"{sid}.jsonl"
                    try:
                        entry = json.dumps({"type": "custom-title", "customTitle": label, "sessionId": sid})
                        with jf.open("a", encoding="utf-8") as fh:
                            fh.write(entry + "\n")
                        ok = True
                    except Exception:
                        pass
                self._send_json({"ok": ok})

            elif self.path == "/api/archive":
                sid = body.get("session_id", "")
                enc = body.get("encoded_name", "")
                archived = _read_archive()
                archived.add(sid)
                ok = _write_archive(archived)
                if enc:
                    od = _read_order()
                    if enc in od:
                        od[enc] = _remove_from_tree(od[enc], sid)
                        _write_order(od)
                self._send_json({"ok": ok})

            elif self.path == "/api/unarchive":
                sid = body.get("session_id", "")
                enc = body.get("encoded_name", "")
                archived = _read_archive()
                archived.discard(sid)
                ok = _write_archive(archived)
                if enc:
                    od = _read_order()
                    tree = od.get(enc, [])
                    if sid not in _ids_in_order(tree):
                        od[enc] = [sid] + tree
                        _write_order(od)
                self._send_json({"ok": ok})

            elif self.path == "/api/reorder":
                enc = body.get("encoded_name", "")
                order = body.get("order", [])
                od = _read_order()
                od[enc] = order
                self._send_json({"ok": _write_order(od)})

            elif self.path == "/api/delete":
                sid = body.get("session_id", "")
                enc = body.get("encoded_name", "")
                ok = False
                if sid and enc:
                    proj_dir = PROJECTS_DIR / enc
                    jf = proj_dir / f"{sid}.jsonl"
                    try:
                        if jf.exists():
                            jf.unlink()
                        # Clean up order
                        od = _read_order()
                        if enc in od:
                            od[enc] = _remove_from_tree(od[enc], sid)
                        _write_order(od)
                        # Clean up archive
                        archived = _read_archive()
                        archived.discard(sid)
                        _write_archive(archived)
                        ok = True
                    except Exception:
                        pass
                self._send_json({"ok": ok})

            elif self.path == "/api/reorder-projects":
                proj_order = body.get("order", [])
                od = _read_order()
                od["__project_order__"] = proj_order
                self._send_json({"ok": _write_order(od)})

            elif self.path == "/api/probe":
                # Log what the WebView can see, to help identify iTerm2 window context
                print(f"[probe] {json.dumps(body)[:4000]}", file=sys.stderr, flush=True)
                self._send_json({"ok": True})

            else:
                self.send_response(404)
                self.end_headers()
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)


class _Server(ThreadingMixIn, HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def _start_server() -> None:
    _Server(("127.0.0.1", PORT), _Handler).serve_forever()


# ── HTML ──────────────────────────────────────────────────────────────────────

MAIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Claude Sessions</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#1a1a1a;color:#ccc;font:12px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;padding:8px;overflow:hidden;display:flex;flex-direction:column;height:100vh}
#root{flex:1;overflow-y:auto;overflow-x:hidden}
#probe{font:10px/1.3 Menlo,monospace;color:#888;background:#181818;border:1px solid #333;border-radius:3px;padding:4px 6px;margin-bottom:6px;max-height:180px;overflow:auto;white-space:pre-wrap;word-break:break-all}
#probe .k{color:#9cdcfe}
#probe .v{color:#ce9178}
#probe .hit{color:#e48faa;font-weight:700}
#probe-toggle{background:none;border:none;color:#555;font-size:10px;cursor:pointer;padding:0 4px}
#probe-toggle:hover{color:#999}
#hdr{display:flex;align-items:center;gap:6px;margin-bottom:6px;padding-bottom:6px;border-bottom:1px solid #2a2a2a}
#hdr h1{font-size:11px;font-weight:600;color:#666;text-transform:uppercase;letter-spacing:.5px;flex:1}
#search{width:100%;background:#222;border:1px solid #333;color:#ccc;font-size:11px;padding:4px 8px;border-radius:3px;outline:none;margin-bottom:6px}
#search:focus{border-color:#4fc3f7}
#search::placeholder{color:#444}

.proj{margin-bottom:2px}
.phdr{display:flex;align-items:center;gap:4px;cursor:pointer;padding:3px 4px;border-radius:3px;user-select:none}
.phdr:hover{background:#222}
.pdh{cursor:grab;color:#333;font-size:11px;flex-shrink:0;padding:0 2px 0 0;user-select:none;line-height:1}
.pdh:hover{color:#666}
.pdh:active{cursor:grabbing}
.chv{color:#555;font-size:9px;width:10px;flex-shrink:0;transition:transform .1s}
.collapsed .chv{transform:rotate(-90deg)}
.pname{font-size:11px;font-weight:600;color:#9cdcfe;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pbadge{font-size:9px;color:#555;flex-shrink:0}
.plive{width:5px;height:5px;border-radius:50%;background:#4ec9b0;flex-shrink:0}
.ppath{font-size:10px;color:#555;padding:0 4px 3px 24px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

.sessions{padding-left:12px;min-height:2px}
.collapsed .sessions{display:none}

/* nested children container */
.children{padding-left:14px;margin-top:1px}

.sess{padding:3px 4px 3px 6px;border-left:2px solid #333;margin:1px 0;border-radius:0 2px 2px 0;position:relative}
.sess:hover{background:#1e1e1e}
.sess.open{border-left-color:#4ec9b0}
.sess.working{border-left-color:#e6b450}
.sess.teammate{border-left-color:#b480e6;border-left-width:2px;background:#1d1724}
.sess.teammate:hover{background:#231b2c}
.sess.teammate.open{border-left-color:#4ec9b0;box-shadow:inset 2px 0 #b480e6}
.sess.teammate.working{border-left-color:#e6b450;box-shadow:inset 2px 0 #b480e6}
.sess.focused{background:#1a2930 !important;border-left-color:#4fc3f7 !important;border-left-width:3px;padding-left:5px}
.sess.focused:hover{background:#1f3040 !important}
.sess.focused .lbl{color:#cfe8ff}
.sess.teammate.focused{background:#2a1d3d !important}
.sess.teammate.focused:hover{background:#331f4a !important}
.sess.teammate.focused .lbl{color:#e8d9f7}
/* The focus tint should highlight the session's own row only, not its
   nested children — paint the children container with the page bg so it
   covers the parent's tint behind it. */
.sess.focused > .children{background:#1a1a1a}
.sess.archived-item{opacity:.5;border-left-color:#333}
.sess.archived-item .lbl{color:#555}

.lbl.teammate-default{color:#b480e6;font-style:normal}
.lbl.auto-default{color:#9aa0a6;font-style:italic}
.team-badge{font-size:9px;color:#b480e6;background:#231a2c;border:1px solid #3a2b4a;padding:0 4px;border-radius:2px;flex-shrink:0;max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.team-badge.remote{color:#8a6cb0;border-style:dashed}

/* drag handle */
.dh{cursor:grab;color:#4a4a4a;font-size:11px;flex-shrink:0;padding:0 3px 0 0;user-select:none;line-height:1}
.dh:hover{color:#888}
.dh:active{cursor:grabbing}

/* sortable states */
.sort-ghost{opacity:.2;background:#4fc3f708;border:1px dashed #4fc3f730 !important;border-radius:3px}
.sort-chosen{box-shadow:0 0 0 1px #4fc3f720}
.sort-drag{opacity:.95}
/* highlight empty children zone when something is being dragged over it */
.children.sortable-drag-over{min-height:18px;border:1px dashed #4fc3f730;border-radius:3px;margin-left:2px}

.sr{display:flex;align-items:center;gap:4px}
.dot{width:5px;height:5px;border-radius:50%;flex-shrink:0}
.dot.open,.dot.idle{background:#4ec9b0}
.dot.inactive,.dot.unknown{background:#4a4a4a}
.spinner{display:inline-block;width:9px;font-family:Menlo,Monaco,monospace;font-size:12px;line-height:1;color:#e6b450;text-align:center;flex-shrink:0}

.lbl{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px;cursor:pointer}
.lbl:hover{color:#fff}
.lbl.unnamed{color:#555;font-style:italic}
.lbl.has-recap{text-decoration:underline dotted #555;text-underline-offset:3px}
.tip{position:fixed;background:#252525;color:#ccc;border:1px solid #3d3d3d;border-radius:4px;padding:5px 9px;font-size:11px;max-width:220px;line-height:1.4;word-break:break-word;z-index:9999;pointer-events:none;box-shadow:0 3px 10px rgba(0,0,0,.6)}
.lbl-inp{flex:1;background:#222;border:1px solid #4fc3f7;color:#ccc;font-size:11px;padding:0 4px;border-radius:2px;outline:none}

.smeta{font-size:10px;color:#5a5a5a;margin:1px 0 2px;padding-left:14px;display:flex;align-items:center;gap:5px;flex-wrap:wrap}
.weight-bar{display:inline-block;width:52px;height:8px;background:#222;border:1px solid #2f2f2f;border-radius:3px;overflow:hidden;vertical-align:middle;flex-shrink:0}
.weight-fill{display:block;height:100%;background:#3a5f58;border-radius:2px;transition:width .2s;box-shadow:0 0 4px rgba(78,201,176,.15) inset}
.weight-fill.w-med{background:linear-gradient(90deg,#3a8f80,#4ec9b0)}
.weight-fill.w-hi{background:linear-gradient(90deg,#4ec9b0,#e6b450);box-shadow:0 0 6px rgba(230,180,80,.25) inset}
.weight-fill.w-xxl{background:linear-gradient(90deg,#e6b450,#e48faa);box-shadow:0 0 8px rgba(228,143,170,.35) inset}
.compact-mark{font-size:10px;color:#c97ca0;flex-shrink:0;font-weight:600}
.branch{font-size:10px;color:#569cd6;background:#1a2530;border:1px solid #1e3a50;padding:0 4px;border-radius:2px;max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.worktree{font-size:9px;color:#888;background:#252525;border:1px solid #333;padding:0 4px;border-radius:2px;flex-shrink:0}
.sa{display:flex;gap:3px;flex-wrap:wrap;padding-left:14px}
.btn{font-size:10px;padding:1px 6px;border-radius:2px;cursor:pointer;background:transparent;border:1px solid #3a3a3a;color:#666;line-height:1.4}
.btn:hover{background:#242424;color:#bbb}
.btn.f{border-color:#2d5a54;color:#4ec9b0}.btn.f:hover{background:#1a3530}
.btn.r{border-color:#2d4a5c;color:#9cdcfe}.btn.r:hover{background:#182430}
.btn.arch{border-color:#3a2a2a;color:#664444}.btn.arch:hover{background:#2a1a1a;color:#cc6666}
.btn.unarch{border-color:#2a3a2a;color:#446644}.btn.unarch:hover{background:#1a2a1a;color:#66aa66}
.btn.del{border-color:#2a1a1a;color:#4a2a2a}.btn.del:hover{background:#2a1010;color:#cc3333;border-color:#4a2020}
.modal-backdrop{position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:10000;display:flex;align-items:center;justify-content:center}
.modal{background:#222;border:1px solid #3a3a3a;border-radius:6px;padding:16px 18px;max-width:220px;box-shadow:0 8px 28px rgba(0,0,0,.8)}
.modal p{font-size:11px;color:#bbb;line-height:1.5;margin-bottom:12px}
.modal p strong{color:#ddd}
.modal-btns{display:flex;gap:6px;justify-content:flex-end}
.modal-btns button{font-size:11px;padding:4px 12px;border-radius:3px;cursor:pointer;border:1px solid #3a3a3a;background:transparent;color:#888}
.modal-btns .modal-cancel:hover{background:#2a2a2a;color:#ccc}
.modal-btns .modal-confirm{border-color:#4a2020;color:#cc3333}.modal-btns .modal-confirm:hover{background:#2a1010;color:#ff4444}

.arch-row{font-size:10px;color:#555;cursor:pointer;padding:2px 4px 2px 4px;user-select:none;margin-top:2px;display:flex;align-items:center;gap:4px}
.arch-row:hover{color:#888}

.resume-menu{position:fixed;background:#252525;border:1px solid #3a3a3a;border-radius:4px;padding:3px 0;z-index:9999;min-width:120px;box-shadow:0 4px 14px rgba(0,0,0,.6)}
.resume-opt{padding:5px 12px;font-size:11px;color:#bbb;cursor:pointer;white-space:nowrap}
.resume-opt:hover{background:#333;color:#fff}
.resume-opt.disabled{color:#555;cursor:not-allowed}
.resume-opt.disabled:hover{background:transparent;color:#555}
.arch-chv{font-size:9px;transition:transform .1s;display:inline-block}
.arch-row.open .arch-chv{transform:rotate(90deg)}

#empty{text-align:center;color:#555;font-size:11px;margin-top:30px}
</style>
</head>
<body>
<div id="hdr">
  <h1>⚡ Claude</h1>
  <button id="probe-toggle" onclick="toggleProbe()" title="Show iTerm2 window probe">🔍</button>
</div>
<div id="probe" style="display:none"></div>
<input id="search" type="text" placeholder="Filter…" oninput="render()">
<div id="root">Loading…</div>

<script src="https://cdn.jsdelivr.net/npm/sortablejs@1.15.6/Sortable.min.js"></script>
<script>
let data = [];
let currentSids = new Set();
let focusedSid = '';
let focusedPaneAvailable = false;

// Braille spinner — animated locally so the bar doesn't refetch every 80ms.
const SPINNER_FRAMES = ['⠋','⠙','⠹','⠸','⠼','⠴','⠦','⠧','⠇','⠏'];
let _spinFrame = 0;
setInterval(() => {
  _spinFrame = (_spinFrame + 1) % SPINNER_FRAMES.length;
  const ch = SPINNER_FRAMES[_spinFrame];
  document.querySelectorAll('.spinner').forEach(el => { el.textContent = ch; });
}, 80);
// Per-tab override: projects whose collapse state the user toggled AWAY from the
// window-context default. sessionStorage so defaults re-apply on widget reload.
let manualCollapseToggle = new Set(JSON.parse(sessionStorage.getItem('cc_toggle')||'[]'));
let expandedArchived = new Set();
let isDragging = false;
let isEditing = false;
const projSortables = {};
let _tip = null;
let _tipTarget = null;

function projHasCurrent(p){
  const walk = (ss) => {
    for (const s of ss||[]) {
      if (currentSids.has(s.id)) return true;
      if (walk(s.children)) return true;
    }
    return false;
  };
  return walk(p.sessions);
}
function isProjCollapsed(enc, hasCurrent){
  const defaultCollapsed = !hasCurrent;
  return manualCollapseToggle.has(enc) ? !defaultCollapsed : defaultCollapsed;
}

function fmtInt(n){ if(!n) return '0'; if(n>=1e6) return (n/1e6).toFixed(1).replace(/\.0$/,'')+'M'; if(n>=1e3) return (n/1e3).toFixed(1).replace(/\.0$/,'')+'k'; return String(n); }

// Document-level delegation — more reliable than per-element listeners in WKWebView
document.addEventListener('mouseover', e => {
  const el = e.target.closest('[data-recap]');
  if (el && el !== _tipTarget) {
    _tipTarget = el;
    hideTip();
    _tip = document.createElement('div');
    _tip.className = 'tip';
    _tip.textContent = el.dataset.recap;
    document.documentElement.appendChild(_tip);
    requestAnimationFrame(() => {
      if (!_tip) return;
      const pad = 12, w = _tip.offsetWidth || 160, h = _tip.offsetHeight || 20;
      let tx = e.clientX + pad, ty = e.clientY + pad;
      if (tx + w > window.innerWidth)  tx = e.clientX - w - pad;
      if (ty + h > window.innerHeight) ty = e.clientY - h - pad;
      _tip.style.left = tx + 'px'; _tip.style.top = ty + 'px';
    });
  } else if (!el) {
    hideTip();
    _tipTarget = null;
  }
});
document.addEventListener('mousemove', e => {
  if (!_tip) return;
  const pad = 12, w = _tip.offsetWidth || 160, h = _tip.offsetHeight || 20;
  let tx = e.clientX + pad, ty = e.clientY + pad;
  if (tx + w > window.innerWidth)  tx = e.clientX - w - pad;
  if (ty + h > window.innerHeight) ty = e.clientY - h - pad;
  _tip.style.left = tx + 'px'; _tip.style.top = ty + 'px';
});
document.addEventListener('mouseleave', () => { hideTip(); _tipTarget = null; });
function hideTip() { if (_tip) { _tip.remove(); _tip = null; } }

// ── escaping ──────────────────────────────────────────────────────────────────
function x(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');}

// ── load / refresh ────────────────────────────────────────────────────────────
let _loadInFlight = false;
let _loadQueued = false;
async function doLoad(){
  if(isDragging || isEditing) return;
  if(_loadInFlight){ _loadQueued = true; return; }
  _loadInFlight = true;
  try{
    const r=await fetch('/api/data');
    const payload=await r.json();
    // Payload was wrapped to carry current-window context; stay compatible with the old shape too.
    if (Array.isArray(payload)) { data = payload; currentSids = new Set(); focusedSid = ''; focusedPaneAvailable = false; }
    else {
      data = payload.projects || [];
      currentSids = new Set(payload.current_window_session_ids || []);
      focusedSid = payload.focused_session_id || '';
      focusedPaneAvailable = !!payload.focused_pane_available;
    }
    render();
  }catch{
    document.getElementById('root').innerHTML='<div id="empty">Connection error</div>';
  } finally {
    _loadInFlight = false;
    if (_loadQueued) { _loadQueued = false; doLoad(); }
  }
}

// Server-sent events from Python: iTerm2 focus/layout/session changes + claude start/exit.
let _es = null;
function connectEvents(){
  try { if (_es) _es.close(); } catch(e){}
  _es = new EventSource('/api/events');
  _es.onmessage = () => doLoad();
  _es.onerror = () => {
    try { _es.close(); } catch(e){}
    setTimeout(connectEvents, 3000);
  };
}
connectEvents();
// Slow safety-net poll in case the SSE stream silently stalls.
setInterval(doLoad, 30000);

let _projSortable=null;

function saveProjectOrder(){
  const root=document.getElementById('root');
  const order=[...root.querySelectorAll(':scope>.proj')].map(el=>el.id.slice(1));
  fetch('/api/reorder-projects',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({order})});
}

// ── render ────────────────────────────────────────────────────────────────────
function render(){
  if(isDragging || isEditing) return;
  Object.keys(projSortables).forEach(e=>destroySortables(e));
  if(_projSortable){_projSortable.destroy();_projSortable=null;}
  const q=document.getElementById('search').value.toLowerCase();
  const root=document.getElementById('root');
  const scrollY=root.scrollTop;
  root.innerHTML='';
  let any=false;
  // Split: projects whose sessions include one from the frontmost window go first.
  const cur=[], other=[];
  for(const p of data){ (projHasCurrent(p)?cur:other).push(p); }
  for(const p of [...cur,...other]){
    const el=buildProject(p,q);
    if(el){root.appendChild(el);any=true;}
  }
  if(!any) root.innerHTML='<div id="empty">No sessions found</div>';
  root.scrollTop=scrollY;
  if(any&&!q){
    let _preCollapseState=null;
    _projSortable=Sortable.create(root,{
      animation:150,
      handle:'.pdh',
      draggable:'.proj',
      ghostClass:'sort-ghost',
      chosenClass:'sort-chosen',
      scroll:root,
      scrollSensitivity:60,
      scrollSpeed:12,
      onStart:()=>{
        isDragging=true;
        // Collapse all projects visually so the full list is draggable; don't
        // touch the persisted toggle set — we'll just re-render after drop.
        _preCollapseState=true;
        root.querySelectorAll('.proj:not(.collapsed)').forEach(el=>el.classList.add('collapsed'));
      },
      onEnd:()=>{
        isDragging=false;
        _preCollapseState=null;
        saveProjectOrder();
        // Re-render to restore each project's natural collapse state.
        render();
      },
    });
  }
}

// ── project element ───────────────────────────────────────────────────────────
function buildProject(p, q){
  const enc=p.encoded_name;
  const hasCurrent=projHasCurrent(p);
  const col=isProjCollapsed(enc, hasCurrent);
  const archExpanded=expandedArchived.has(enc);

  // filter active sessions (nested)
  const visActive=filterTree(p.sessions||[], q, p.display_name);
  const visArch=archExpanded?(q?(p.archived||[]).filter(s=>(s.label||'').toLowerCase().includes(q)||s.id.startsWith(q)):(p.archived||[])):[];
  if(!visActive.length&&!visArch.length&&q) return null;

  const div=document.createElement('div');
  div.className='proj'+(col?' collapsed':'');
  div.id='P'+enc;

  // header
  const phdr=document.createElement('div');
  phdr.className='phdr';
  const live=hasOpen(visActive);
  const total=countAll(p.sessions||[]);
  phdr.innerHTML=`<span class="pdh" title="Drag to reorder projects">⠿</span><span class="chv">▼</span><span class="pname" title="${x(p.path)}">${x(p.display_name)}</span>${live?'<span class="plive"></span>':''}<span class="pbadge">${total}</span>`;
  phdr.querySelector('.pdh').onclick=e=>e.stopPropagation();
  phdr.onclick=()=>tog(enc);
  div.appendChild(phdr);

  if(p.abbrev_path){
    const pp=document.createElement('div');
    pp.className='ppath';pp.title=p.path;pp.textContent=p.abbrev_path;
    div.appendChild(pp);
  }

  // sessions container
  const sessDiv=document.createElement('div');
  sessDiv.className='sessions';
  buildTree(visActive, sessDiv, p);

  // archived section
  const totalArch=(p.archived||[]).length;
  if(totalArch>0){
    const ar=document.createElement('div');
    ar.className='arch-row'+(archExpanded?' open':'');
    ar.innerHTML=`<span class="arch-chv">▶</span><span>${totalArch} archived</span>`;
    ar.onclick=()=>togArch(enc);
    sessDiv.appendChild(ar);
    if(archExpanded){
      for(const s of visArch){
        sessDiv.appendChild(makeSessEl(s,p));
      }
    }
  }

  div.appendChild(sessDiv);
  initSortables(div, enc);
  return div;
}

// ── session tree builder ──────────────────────────────────────────────────────
function buildTree(sessions, container, p){
  for(const s of sessions){
    const el=makeSessEl(s,p);
    const ch=el.querySelector('.children');
    if(s.children&&s.children.length) buildTree(s.children, ch, p);
    container.appendChild(el);
  }
}

function makeSessEl(s, p){
  const isArch=s.archived;
  const sc=s.is_open?'open':s.status;
  const el=document.createElement('div');
  el.className=`sess ${sc}${isArch?' archived-item':''}${s.is_teammate?' teammate':''}${s.id===focusedSid?' focused':''}`;
  el.dataset.id=s.id;
  el.dataset.tty=s.tty||'';
  el.dataset.path=p.path||'';
  el.dataset.enc=p.encoded_name;

  // sr row
  const sr=document.createElement('div');sr.className='sr';
  const dh=document.createElement('span');dh.className='dh';dh.title='Drag to reorder';dh.textContent='⠿';
  let dot;
  if (s.status === 'working') {
    dot = document.createElement('span');
    dot.className = 'spinner';
    dot.title = 'Working';
    dot.textContent = SPINNER_FRAMES[0];
  } else {
    dot = document.createElement('span');
    dot.className = `dot ${s.status}`;
    dot.title = s.status === 'idle' ? 'Idle' : 'Inactive';
  }
  const lbl=document.createElement('span');
  const displayLabel=s.label||s.default_label||'unnamed';
  const usingDefault=!s.label&&!!s.default_label;
  const showsUnnamed=!s.label&&!s.default_label;
  let cls='lbl';
  if (showsUnnamed) cls += ' unnamed';
  if (usingDefault) cls += s.is_teammate ? ' teammate-default' : ' auto-default';
  if (s.computed_title) cls += ' has-recap';
  lbl.className=cls;
  lbl.textContent=displayLabel;lbl.onclick=()=>renEl(lbl);
  if(s.computed_title){lbl.dataset.recap=s.computed_title;}
  if(s.default_label){lbl.dataset.default=s.default_label;}
  sr.append(dh,dot,lbl);el.appendChild(sr);

  // meta
  const sm=document.createElement('div');sm.className='smeta';
  let teamBadge='';
  if(s.is_teammate&&s.team_name){
    const remote=s.leader_proj_enc&&s.leader_proj_enc!==p.encoded_name;
    const tip=remote?`Team "${s.team_name}" — leader in another project`:`Team: ${s.team_name}`;
    teamBadge=`<span class="team-badge${remote?' remote':''}" title="${x(tip)}">⇌ ${x(s.team_name)}</span>`;
  }
  // Context-pressure bar: how full the model's context window is right now.
  // Linear: current_input_tokens / max_context.
  let weightBar='';
  const ctx=s.current_input_tokens||0;
  const maxCtx=s.max_context||200000;
  if(ctx>0||s.compactions>0||s.output_tokens>0){
    const frac=maxCtx>0?Math.min(1, ctx/maxCtx):0;
    const pct=Math.max(4, Math.round(frac*100));
    let cls='';
    if(frac>=0.85) cls=' w-xxl';
    else if(frac>=0.60) cls=' w-hi';
    else if(frac>=0.30) cls=' w-med';
    const tipLines=[
      `Context: ${fmtInt(ctx)} / ${fmtInt(maxCtx)} (${Math.round(frac*100)}%)`,
    ];
    if(s.model) tipLines.push(`Model: ${s.model}`);
    tipLines.push(`Total output: ${fmtInt(s.output_tokens||0)}`);
    tipLines.push(`Peak input: ${fmtInt(s.max_input_tokens||0)}`);
    tipLines.push(`Compactions: ${s.compactions||0}`);
    const tip=tipLines.join('\n');
    weightBar=`<span class="weight-bar" title="${x(tip)}"><span class="weight-fill${cls}" style="width:${pct}%"></span></span>`;
    if(s.compactions>0){
      weightBar+=`<span class="compact-mark" title="${s.compactions} compaction${s.compactions===1?'':'s'}">↺${s.compactions}</span>`;
    }
  }
  sm.innerHTML=`<span>${s.time_str}</span>${weightBar}${teamBadge}${s.branch&&s.branch!=='HEAD'?`<span class="branch" title="${x(s.branch)}">${x(s.branch)}</span>`:''}${s.is_worktree?'<span class="worktree">worktree</span>':''}`;
  el.appendChild(sm);

  // actions
  const sa=document.createElement('div');sa.className='sa';
  if(s.is_open&&!isArch){const b=document.createElement('button');b.className='btn f';b.textContent='Focus';b.onclick=()=>focusEl(el);sa.appendChild(b);}
  if(!s.is_open&&!isArch){const b=document.createElement('button');b.className='btn r';b.textContent='Resume';b.onclick=()=>resumeEl(b);sa.appendChild(b);}
  const rb=document.createElement('button');rb.className='btn';rb.title='Rename';rb.textContent='✎';rb.onclick=()=>renEl(lbl);sa.appendChild(rb);
  const ab=document.createElement('button');
  if(!isArch){ab.className='btn arch';ab.title='Archive';ab.textContent='⊟';ab.onclick=()=>archiveEl(ab,s.id,p.encoded_name);}
  else{ab.className='btn unarch';ab.title='Unarchive';ab.textContent='⊞';ab.onclick=()=>unarchiveEl(ab,s.id,p.encoded_name);}
  sa.appendChild(ab);
  const db=document.createElement('button');db.className='btn del';db.title='Delete session';db.textContent='🗑';db.onclick=()=>deleteEl(s.id,s.label||s.id.slice(0,8),p.encoded_name);sa.appendChild(db);
  el.appendChild(sa);

  // children container for nesting
  const ch=document.createElement('div');ch.className='children';el.appendChild(ch);
  return el;
}

// ── helpers ───────────────────────────────────────────────────────────────────
function filterTree(sessions, q, projName){
  if(!q) return sessions;
  return sessions.flatMap(s=>{
    const kids=filterTree(s.children||[],q,'');
    const hay=[(s.label||''),(s.default_label||''),(s.team_name||''),(s.agent_name||''),projName].join(' ').toLowerCase();
    const match=hay.includes(q)||s.id.startsWith(q);
    if(!match&&!kids.length) return [];
    return [{...s,children:kids}];
  });
}
function hasOpen(sessions){return sessions.some(s=>s.is_open||hasOpen(s.children||[]));}
function countAll(sessions){return sessions.reduce((n,s)=>n+1+countAll(s.children||[]),0);}

// ── SortableJS ────────────────────────────────────────────────────────────────
function destroySortables(enc){
  (projSortables[enc]||[]).forEach(s=>{try{s.destroy();}catch(e){}});
  projSortables[enc]=[];
}

function initSortables(projEl, enc){
  destroySortables(enc);
  const containers=[projEl.querySelector('.sessions'),...projEl.querySelectorAll('.children')];
  for(const c of containers){
    if(!c) continue;
    const s=Sortable.create(c,{
      group:{name:'s-'+enc,pull:true,put:(to,from,dragEl)=>dragEl.classList.contains('sess')},
      animation:150,
      handle:'.dh',
      ghostClass:'sort-ghost',
      chosenClass:'sort-chosen',
      dragClass:'sort-drag',
      fallbackOnBody:true,
      swapThreshold:0.55,
      emptyInsertThreshold:8,
      scroll:document.getElementById('root'),
      scrollSensitivity:60,
      scrollSpeed:12,
      onStart:()=>{isDragging=true;},
      onEnd:(evt)=>{
        isDragging=false;
        const pEl=document.getElementById('P'+enc);
        if(pEl){
          setTimeout(()=>{initSortables(pEl,enc);saveOrder(pEl,enc);},0);
        }
      },
    });
    (projSortables[enc]=projSortables[enc]||[]).push(s);
  }
}

function serializeContainer(c){
  const out=[];
  for(const child of c.children){
    if(!child.classList.contains('sess')) continue;
    const nested=child.querySelector(':scope>.children');
    const kids=nested&&nested.children.length?serializeContainer(nested):[];
    out.push(kids.length?{id:child.dataset.id,children:kids}:child.dataset.id);
  }
  return out;
}

async function saveOrder(projEl, enc){
  const sd=projEl.querySelector('.sessions');
  if(!sd) return;
  const order=serializeContainer(sd);
  await fetch('/api/reorder',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({encoded_name:enc,order})});
}

// ── actions ───────────────────────────────────────────────────────────────────
function tog(enc){
  const el=document.getElementById('P'+enc);if(!el)return;
  el.classList.toggle('collapsed');
  if(manualCollapseToggle.has(enc)) manualCollapseToggle.delete(enc);
  else manualCollapseToggle.add(enc);
  sessionStorage.setItem('cc_toggle',JSON.stringify([...manualCollapseToggle]));
}

function togArch(enc){
  expandedArchived.has(enc)?expandedArchived.delete(enc):expandedArchived.add(enc);
  render();
}

async function focusEl(el){
  await fetch('/api/focus',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tty:el.dataset.tty})});
}

function resumeEl(btn){
  // close any existing menu
  const existing=document.getElementById('resume-menu');
  if(existing){existing.remove();return;}
  const el=btn.closest('.sess');
  const menu=document.createElement('div');
  menu.id='resume-menu';menu.className='resume-menu';
  const opts=[
    {l:'Use focused pane',m:'here',disabled:!focusedPaneAvailable,
     reason:'Focused pane already has a session or is busy'},
    {l:'Split pane',m:'split'},
    {l:'New tab',m:'tab'},
    {l:'New window',m:'window'},
  ];
  for(const o of opts){
    const item=document.createElement('div');
    item.className='resume-opt'+(o.disabled?' disabled':'');
    item.textContent=o.l;
    if(o.disabled){
      if(o.reason) item.title=o.reason;
    } else {
      item.onclick=async()=>{
        menu.remove();
        btn.textContent='…';btn.disabled=true;
        await fetch('/api/resume',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:el.dataset.id,project_path:el.dataset.path,mode:o.m})});
        // Claude can take several seconds to start and register its PID
        setTimeout(doLoad,3000);
        setTimeout(doLoad,7000);
        setTimeout(doLoad,13000);
      };
    }
    menu.appendChild(item);
  }
  const r=btn.getBoundingClientRect();
  menu.style.top=(r.bottom+4)+'px';
  menu.style.left=r.left+'px';
  document.body.appendChild(menu);
  setTimeout(()=>document.addEventListener('click',()=>menu.remove(),{once:true}),0);
}

function renEl(lbl){
  if(!lbl||lbl.tagName==='INPUT') return;
  const sessEl=lbl.closest('.sess');
  const sid=sessEl.dataset.id;
  const enc=sessEl.dataset.enc;
  const cur=lbl.classList.contains('unnamed')?'':lbl.textContent;
  const inp=document.createElement('input');
  inp.className='lbl-inp';inp.value=cur;inp.placeholder='Label…';
  lbl.replaceWith(inp);inp.focus();inp.select();
  isEditing=true;
  let finished=false;
  const finish=()=>{ if(!finished){ finished=true; isEditing=false; doLoad(); } };
  const commit=async()=>{
    if(finished) return;
    const v=inp.value.trim();
    const span=document.createElement('span');
    const recap=lbl.dataset.recap||'';
    const def=lbl.dataset.default||'';
    const usingDefault=!v&&!!def;
    const showsUnnamed=!v&&!def;
    const isTeammate=sessEl.classList.contains('teammate');
    let cls='lbl';
    if (showsUnnamed) cls += ' unnamed';
    if (usingDefault) cls += isTeammate ? ' teammate-default' : ' auto-default';
    if (recap) cls += ' has-recap';
    span.className=cls;
    span.textContent=v||def||'unnamed';
    span.onclick=()=>renEl(span);
    if(recap) span.dataset.recap=recap;
    if(def) span.dataset.default=def;
    inp.replaceWith(span);
    finish();
    await fetch('/api/rename',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:sid,encoded_name:enc,label:v})});
  };
  inp.onblur=commit;
  inp.onkeydown=e=>{
    if(e.key==='Enter'){e.preventDefault();inp.blur();}
    if(e.key==='Escape'){inp.replaceWith(lbl); finish();}
  };
}

async function archiveEl(btn,sid,enc){
  btn.disabled=true;
  await fetch('/api/archive',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:sid,encoded_name:enc})});
  await doLoad();
}

async function unarchiveEl(btn,sid,enc){
  btn.disabled=true;
  await fetch('/api/unarchive',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:sid,encoded_name:enc})});
  await doLoad();
}

function showConfirm(message, onConfirm){
  const backdrop=document.createElement('div');backdrop.className='modal-backdrop';
  const modal=document.createElement('div');modal.className='modal';
  const p=document.createElement('p');p.innerHTML=message;
  const btns=document.createElement('div');btns.className='modal-btns';
  const cancel=document.createElement('button');cancel.className='modal-cancel';cancel.textContent='Cancel';
  const confirm=document.createElement('button');confirm.className='modal-confirm';confirm.textContent='Delete';
  btns.append(cancel,confirm);modal.append(p,btns);backdrop.appendChild(modal);
  document.documentElement.appendChild(backdrop);
  const close=()=>backdrop.remove();
  cancel.onclick=close;
  backdrop.onclick=e=>{if(e.target===backdrop)close();};
  confirm.onclick=()=>{close();onConfirm();};
  confirm.focus();
}

function deleteEl(sid,label,enc){
  const name=label.length>40?label.slice(0,40)+'…':label;
  showConfirm(`Permanently delete <strong>${x(name)}</strong>?<br><br>This removes the JSONL file and cannot be undone.`, async()=>{
    await fetch('/api/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:sid,encoded_name:enc})});
    await doLoad();
  });
}

// ── iTerm2 window probe ──────────────────────────────────────────────────────
function runProbe(){
  const out = {};
  try { out.href = location.href; } catch(e){ out.href_err = String(e); }
  try { out.ua = navigator.userAgent; } catch(e){}
  try { out.referrer = document.referrer; } catch(e){}
  try {
    out.viewport = {w: innerWidth, h: innerHeight, ow: outerWidth, oh: outerHeight};
    out.screen = {w: screen.width, h: screen.height, x: screen.availLeft, y: screen.availTop};
    out.position = {x: screenX, y: screenY};
    out.pixelRatio = devicePixelRatio;
  } catch(e){}
  // Probe WebKit message handlers (how WKWebView hosts expose bridges)
  try {
    const wk = window.webkit && window.webkit.messageHandlers;
    out.webkit_handlers = wk ? Object.keys(wk) : null;
  } catch(e){ out.webkit_err = String(e); }
  // Known iTerm2 bridge candidates — just check presence
  const candidates = ['iTerm2','iterm2','ITerm','iTerm','iTerm2Protocol','iTermToolbelt','iTermWebView','_iTerm','__iterm2','iTermID','iterm2SessionId'];
  const found = {};
  for (const name of candidates){
    try { if (window[name] !== undefined) found[name] = typeof window[name]; } catch(e){}
  }
  out.iterm_globals = found;
  // Enumerate non-standard globals (anything not in a baseline browser)
  const baseline = new Set(['window','self','document','navigator','location','history','screen','console','localStorage','sessionStorage','caches','crypto','performance','indexedDB','fetch','Request','Response','Headers','Blob','File','FileReader','URL','URLSearchParams','TextEncoder','TextDecoder','setTimeout','setInterval','clearTimeout','clearInterval','requestAnimationFrame','cancelAnimationFrame','addEventListener','removeEventListener','alert','confirm','prompt','getComputedStyle','matchMedia','atob','btoa','structuredClone','queueMicrotask','Promise','Proxy','Reflect','Symbol','Map','Set','WeakMap','WeakSet','Array','Object','String','Number','Boolean','Date','Math','JSON','RegExp','Error','TypeError','RangeError','SyntaxError','ReferenceError','URIError','EvalError','Function','Iterator','Intl','WeakRef','FinalizationRegistry','BigInt','ArrayBuffer','Int8Array','Uint8Array','Uint8ClampedArray','Int16Array','Uint16Array','Int32Array','Uint32Array','Float32Array','Float64Array','BigInt64Array','BigUint64Array','DataView','SharedArrayBuffer','Atomics','Notification','AbortController','AbortSignal','Event','EventTarget','CustomEvent','MessageEvent','PopStateEvent','HashChangeEvent','KeyboardEvent','MouseEvent','PointerEvent','TouchEvent','WheelEvent','Element','HTMLElement','HTMLDocument','HTMLCollection','NodeList','Node','Text','Comment','DocumentFragment','Attr','NamedNodeMap','Range','Selection','CSS','CSSStyleDeclaration','CSSStyleSheet','CSSRule','MutationObserver','IntersectionObserver','ResizeObserver','PerformanceObserver','WebSocket','XMLHttpRequest','FormData','Worker','SharedWorker','Crypto','SubtleCrypto','DOMException','DOMParser','XMLSerializer','Image','Audio','Video','HTMLImageElement','HTMLCanvasElement','HTMLAudioElement','HTMLVideoElement','CanvasRenderingContext2D','WebGLRenderingContext','WebGL2RenderingContext','OffscreenCanvas','Path2D','BroadcastChannel','Clipboard','ClipboardItem','FontFace','FontFaceSet','CustomElementRegistry','ShadowRoot','Storage','StorageEvent','Animation','AnimationEffect','KeyframeEffect','AnimationTimeline','DocumentTimeline']);
  const extras = [];
  try {
    for (const k of Object.keys(window)){
      if (!baseline.has(k) && !k.startsWith('webkit') && !/^[A-Z]/.test(k)){
        extras.push(k);
      }
    }
  } catch(e){ out.keys_err = String(e); }
  out.non_standard_globals = extras.slice(0, 80);
  return out;
}

let _probeResult = null;
function renderProbe(){
  const el = document.getElementById('probe');
  if (!_probeResult) { el.textContent = 'probing…'; return; }
  const p = _probeResult;
  const lines = [];
  const hitKeys = Object.keys(p.iterm_globals || {});
  lines.push(`<span class="${hitKeys.length?'hit':''}">iterm_globals: ${x(JSON.stringify(p.iterm_globals))}</span>`);
  lines.push(`<span class="${p.webkit_handlers&&p.webkit_handlers.length?'hit':''}">webkit_handlers: ${x(JSON.stringify(p.webkit_handlers))}</span>`);
  lines.push(`<span class="k">href</span>: ${x(String(p.href||''))}`);
  lines.push(`<span class="k">referrer</span>: ${x(String(p.referrer||''))}`);
  lines.push(`<span class="k">ua</span>: ${x(String(p.ua||'').slice(0,120))}`);
  lines.push(`<span class="k">viewport</span>: ${x(JSON.stringify(p.viewport||{}))}`);
  lines.push(`<span class="k">screenPos</span>: ${x(JSON.stringify(p.position||{}))} on ${x(JSON.stringify(p.screen||{}))}`);
  lines.push(`<span class="k">non_standard_globals</span> (${(p.non_standard_globals||[]).length}): ${x((p.non_standard_globals||[]).join(', '))}`);
  el.innerHTML = lines.join('<br>');
}
function toggleProbe(){
  const el = document.getElementById('probe');
  const show = el.style.display === 'none';
  el.style.display = show ? '' : 'none';
  if (show){
    _probeResult = runProbe();
    renderProbe();
    fetch('/api/probe',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(_probeResult)}).catch(()=>{});
  }
}

// Auto-run probe once on load so the server log captures it
try {
  const p = runProbe();
  fetch('/api/probe',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)}).catch(()=>{});
} catch(e){}

</script>
</body>
</html>"""


# ── Entry point ───────────────────────────────────────────────────────────────

async def _watch_iterm_events() -> None:
    """Subscribe to iTerm2 event streams; broadcast a refresh on each event."""
    async def run(name: str, cm_factory, method: str):
        while True:
            try:
                async with cm_factory() as mon:
                    fn = getattr(mon, method)
                    while True:
                        await fn()
                        _broadcast_refresh(name)
            except Exception as exc:
                print(f"[watch {name}] {exc!r}; retrying in 5s", file=sys.stderr)
                await asyncio.sleep(5)

    await asyncio.gather(
        run("focus", lambda: iterm2.FocusMonitor(_connection), "async_get_next_update"),
        run("layout", lambda: iterm2.LayoutChangeMonitor(_connection), "async_get"),
        run("new-session", lambda: iterm2.NewSessionMonitor(_connection), "async_get"),
        run("session-term", lambda: iterm2.SessionTerminationMonitor(_connection), "async_get"),
    )


def _watch_sessions_dir() -> None:
    """Poll ~/.claude/sessions/ for additions/removals — that's how we see claude start/exit."""
    sd = CLAUDE_DIR / "sessions"
    prev: Optional[frozenset] = None
    while True:
        try:
            cur = frozenset(os.listdir(sd)) if sd.exists() else frozenset()
            if prev is not None and cur != prev:
                _broadcast_refresh("sessions-dir")
            prev = cur
        except Exception:
            pass
        time.sleep(1.0)


def _watch_active_jsonls() -> None:
    """Poll JSONL mtimes for currently-active sessions; broadcast on any change.

    This is what flips a session between idle/working in real time. Only the
    active set (PIDs in ~/.claude/sessions/) is polled, so cost stays trivial.
    """
    sd = CLAUDE_DIR / "sessions"
    last: dict = {}
    while True:
        try:
            current: dict = {}
            sids: list = []
            if sd.exists():
                for pf in sd.iterdir():
                    if pf.suffix != ".json":
                        continue
                    try:
                        sid = json.loads(pf.read_text(encoding="utf-8")).get("sessionId")
                    except Exception:
                        sid = None
                    if sid:
                        sids.append(sid)
            for sid in sids:
                # Linear scan of project dirs is fine — there are <30 of them.
                for pd in PROJECTS_DIR.iterdir():
                    if not pd.is_dir():
                        continue
                    jf = pd / f"{sid}.jsonl"
                    if jf.exists():
                        try:
                            current[sid] = jf.stat().st_mtime
                        except Exception:
                            pass
                        break
            if last and (current != last):
                _broadcast_refresh("jsonl")
            last = current
        except Exception:
            pass
        time.sleep(1.5)


async def main(connection: iterm2.Connection) -> None:
    global _connection, _event_loop
    _connection = connection
    _event_loop = asyncio.get_running_loop()

    await iterm2.tool.async_register_web_view_tool(
        connection=connection,
        display_name="Claude Sessions",
        identifier=IDENTIFIER,
        reveal_if_already_registered=False,
        url=f"http://localhost:{PORT}/",
    )

    t = threading.Thread(target=_start_server, daemon=True, name="claude-sessions-http")
    t.start()
    threading.Thread(target=_watch_sessions_dir, daemon=True, name="claude-sessions-fs").start()
    threading.Thread(target=_watch_active_jsonls, daemon=True, name="claude-sessions-jsonl").start()
    print(f"[claude-sessions] running on port {PORT}", file=sys.stderr)

    # Run iTerm2 event watchers for the rest of the process lifetime.
    await _watch_iterm_events()


iterm2.run_forever(main, retry=True)
