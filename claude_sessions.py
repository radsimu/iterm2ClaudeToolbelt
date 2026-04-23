#!/usr/bin/env python3
"""
Claude Code Session Manager — iTerm2 Toolbelt Widget
"""

import asyncio
import datetime
import json
import os
import shlex
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

import iterm2

# ── Constants ─────────────────────────────────────────────────────────────────

PORT = 9837
IDENTIFIER = "com.claude-code.session-manager"
CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
SESSION_LABELS_FILE = CLAUDE_DIR / "session-labels.json"
PID_TO_SESSION_FILE = CLAUDE_DIR / "pid-to-session.json"
SESSION_STATUS_FILE = CLAUDE_DIR / "session-status.json"
ARCHIVE_FILE = CLAUDE_DIR / "session-manager-archive.json"
ORDER_FILE  = CLAUDE_DIR / "session-manager-order.json"

# ── Shared state ──────────────────────────────────────────────────────────────

_connection: Optional[iterm2.Connection] = None
_event_loop: Optional[asyncio.AbstractEventLoop] = None


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


def _session_git_info(jf: Path) -> tuple:
    """Return (branch, cwd, computed_title) from the JSONL file.

    Title priority: away_summary → custom-title → first user prompt.
    """
    branch, cwd, title = None, None, None
    try:
        with jf.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()

            # Head: first user prompt lives here
            f.seek(0)
            head = f.read(min(size, 8192)).decode("utf-8", errors="ignore")

            # Small tail: branch + cwd live near the end
            f.seek(max(0, size - 8192))
            small_tail = f.read().decode("utf-8", errors="ignore")

            # Large tail: recap entries can be buried further back
            f.seek(max(0, size - 524288))
            large_tail = f.read().decode("utf-8", errors="ignore")

        for line in reversed(small_tail.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if not branch and d.get("gitBranch") and d.get("cwd"):
                    branch, cwd = d["gitBranch"], d["cwd"]
                    if branch:
                        break
            except json.JSONDecodeError:
                continue

        # Most recent recap: away_summary > custom-title
        for line in reversed(large_tail.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if d.get("type") == "system" and d.get("subtype") == "away_summary":
                    title = d.get("content") or None
                    if title:
                        break
                if not title and d.get("type") == "custom-title":
                    title = d.get("customTitle") or None
            except json.JSONDecodeError:
                continue

        # Fallback: first user prompt from the head of the file
        if not title:
            for line in head.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
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
                        if text and not text.startswith("<local-command-caveat>"):
                            title = text[:120].replace("\n", " ")
                            break
                except json.JSONDecodeError:
                    continue

    except Exception:
        pass
    return branch, cwd, title


def _abbrev_path(path: str) -> str:
    """Shorten a path for display: ~/Work/foo/bar or last 3 components."""
    home = str(Path.home())
    if path.startswith(home):
        path = "~" + path[len(home):]
    parts = path.replace("\\", "/").split("/")
    if len(parts) > 4:
        return "/".join(parts[:2]) + "/…/" + "/".join(parts[-2:])
    return path


def _decode_project_path(encoded: str) -> Optional[str]:
    """Resolve encoded dir name (/-replaced-with-) to a real filesystem path."""
    if not encoded.startswith("-"):
        return None
    parts = encoded[1:].split("-")
    current = "/"
    remaining = list(parts)
    while remaining:
        matched = False
        # Try longest-first greedy match against actual filesystem
        for length in range(len(remaining), 0, -1):
            candidate = "-".join(remaining[:length])
            candidate_path = os.path.join(current, candidate)
            if os.path.isdir(candidate_path):
                current = candidate_path
                remaining = remaining[length:]
                matched = True
                break
        if not matched:
            return None
    return current if current != "/" else None


def _get_active_session_ttys() -> dict:
    """Return {session_id: tty} by matching running claude processes to sessions.

    Strategy:
    1. If process was started with --resume <session-id>, use that directly.
    2. Otherwise, find the project dir via lsof and pick the most-recently-modified JSONL.
    """
    try:
        pids = subprocess.run(
            ["pgrep", "-x", "claude"], capture_output=True, text=True, timeout=2
        ).stdout.split()
    except Exception:
        return {}
    if not pids:
        return {}

    home = str(Path.home()) + "/"
    claude_prefix = str(CLAUDE_DIR) + "/"

    explicit: dict = {}   # session_id -> tty (from --resume args)
    pid_project: list = []  # (project_path, tty) for PIDs without --resume

    for pid in pids:
        tty = _normalize_tty(_pid_tty(pid) or "")

        # Check if started with --resume <session-id>
        try:
            args = subprocess.run(
                ["ps", "-o", "args=", "-p", pid], capture_output=True, text=True, timeout=1
            ).stdout.strip()
        except Exception:
            args = ""
        if "--resume" in args:
            parts = args.split()
            try:
                sid = parts[parts.index("--resume") + 1]
                if tty:
                    explicit[sid] = tty
                continue
            except (ValueError, IndexError):
                pass

        if not tty:
            continue

        try:
            r = subprocess.run(
                ["lsof", "-p", pid, "-Fn"], capture_output=True, text=True, timeout=3
            )
        except Exception:
            continue
        for line in r.stdout.splitlines():
            if not line.startswith("n"):
                continue
            path = line[1:]
            if (path.startswith(home)
                    and not path.startswith(claude_prefix)
                    and not path.startswith(home + "Library/")
                    and not path.startswith(home + ".")
                    and os.path.isdir(path)):
                pid_project.append((path, tty))
                break

    result: dict = dict(explicit)

    # For PIDs without --resume, map project dir → most recent JSONL
    if pid_project:
        for proj_dir in PROJECTS_DIR.iterdir():
            if not proj_dir.is_dir():
                continue
            proj_path = _decode_project_path(proj_dir.name)
            if not proj_path:
                continue
            matching_ttys = [
                tty for pp, tty in pid_project
                if pp == proj_path or pp.startswith(proj_path + "/")
            ]
            if not matching_ttys:
                continue
            try:
                jsonls = sorted(
                    [(f.stem, f.stat().st_mtime) for f in proj_dir.iterdir() if f.suffix == ".jsonl"],
                    key=lambda x: x[1], reverse=True,
                )
            except Exception:
                continue
            # Assign one TTY per JSONL (top N most recent, where N = number of matching PIDs)
            for i, (sid, _) in enumerate(jsonls[:len(matching_ttys)]):
                if sid not in result:
                    result[sid] = matching_ttys[i]

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
    labels = _read_json(SESSION_LABELS_FILE)
    status_map = _read_json(SESSION_STATUS_FILE)
    archived_set = _read_archive()
    order_data = _read_order()
    order_changed = False

    session_tty = _get_active_session_ttys()

    projects: list = []
    if not PROJECTS_DIR.exists():
        return projects

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
            tty = session_tty.get(sid, "")
            branch, sess_cwd, computed_title = _session_git_info(jf)
            is_worktree = False
            if sess_cwd:
                try:
                    is_worktree = Path(sess_cwd, ".git").is_file()
                except Exception:
                    pass
            raw_status = status_map.get(sid, "unknown")
            status = raw_status if tty else ("idle" if raw_status == "working" else raw_status)
            flat.append({
                "id": sid,
                "label": labels.get(sid, ""),
                "computed_title": computed_title or "",
                "status": status,
                "mtime": mtime,
                "time_str": _relative_time(mtime),
                "is_open": bool(tty),
                "tty": tty,
                "archived": sid in archived_set,
                "branch": branch or "",
                "is_worktree": is_worktree,
                "children": [],
            })

        if not flat:
            continue

        enc = proj_dir.name
        active_map = {s["id"]: s for s in flat if not s["archived"]}
        archived_list = sorted([s for s in flat if s["archived"]], key=lambda s: s["mtime"], reverse=True)

        order_tree = order_data.get(enc, [])
        known = _ids_in_order(order_tree)
        # New active sessions not yet in order file → prepend at top
        new_ids = [sid for sid in sorted(active_map, key=lambda sid: active_map[sid]["mtime"], reverse=True)
                   if sid not in known]
        if new_ids:
            order_tree = new_ids + order_tree
            order_data[enc] = order_tree
            order_changed = True

        active_tree = _build_tree(order_tree, active_map)
        last_active = max(s["mtime"] for s in flat)

        display = Path(project_path).name if project_path else proj_dir.name
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

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html"):
            self._send_html(MAIN_HTML)
        elif self.path == "/api/data":
            self._send_json(build_projects())
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
                label = body.get("label", "")
                labels = _read_json(SESSION_LABELS_FILE)
                if label:
                    labels[sid] = label
                elif sid in labels:
                    del labels[sid]
                self._send_json({"ok": _write_json(SESSION_LABELS_FILE, labels)})

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

            elif self.path == "/api/reorder-projects":
                proj_order = body.get("order", [])
                od = _read_order()
                od["__project_order__"] = proj_order
                self._send_json({"ok": _write_order(od)})

            else:
                self.send_response(404)
                self.end_headers()
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, 500)


class _Server(HTTPServer):
    allow_reuse_address = True


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
#hdr{display:flex;align-items:center;gap:6px;margin-bottom:6px;padding-bottom:6px;border-bottom:1px solid #2a2a2a}
#hdr h1{font-size:11px;font-weight:600;color:#666;text-transform:uppercase;letter-spacing:.5px;flex:1}
#rbtn{background:none;border:none;color:#555;cursor:pointer;font-size:14px;padding:0;line-height:1}
#rbtn:hover{color:#999}
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

.sess{padding:3px 4px 3px 6px;border-left:2px solid #333;margin:1px 0;border-radius:0 2px 2px 0}
.sess:hover{background:#1e1e1e}
.sess.open{border-left-color:#4ec9b0}
.sess.working{border-left-color:#e6b450}
.sess.archived-item{opacity:.5;border-left-color:#333}
.sess.archived-item .lbl{color:#555}

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
.dot.open{background:#4ec9b0}
.dot.working{background:#e6b450}
.dot.idle,.dot.unknown{background:#4a4a4a}

.lbl{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px;cursor:pointer}
.lbl:hover{color:#fff}
.lbl.unnamed{color:#555;font-style:italic}
.lbl.has-recap{text-decoration:underline dotted #555;text-underline-offset:3px}
.tip{position:fixed;background:#252525;color:#ccc;border:1px solid #3d3d3d;border-radius:4px;padding:5px 9px;font-size:11px;max-width:220px;line-height:1.4;word-break:break-word;z-index:9999;pointer-events:none;box-shadow:0 3px 10px rgba(0,0,0,.6)}
.lbl-inp{flex:1;background:#222;border:1px solid #4fc3f7;color:#ccc;font-size:11px;padding:0 4px;border-radius:2px;outline:none}

.smeta{font-size:10px;color:#5a5a5a;margin:1px 0 2px;padding-left:14px;display:flex;align-items:center;gap:5px;flex-wrap:wrap}
.branch{font-size:10px;color:#569cd6;background:#1a2530;border:1px solid #1e3a50;padding:0 4px;border-radius:2px;max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.worktree{font-size:9px;color:#888;background:#252525;border:1px solid #333;padding:0 4px;border-radius:2px;flex-shrink:0}
.sa{display:flex;gap:3px;flex-wrap:wrap;padding-left:14px}
.btn{font-size:10px;padding:1px 6px;border-radius:2px;cursor:pointer;background:transparent;border:1px solid #3a3a3a;color:#666;line-height:1.4}
.btn:hover{background:#242424;color:#bbb}
.btn.f{border-color:#2d5a54;color:#4ec9b0}.btn.f:hover{background:#1a3530}
.btn.r{border-color:#2d4a5c;color:#9cdcfe}.btn.r:hover{background:#182430}
.btn.arch{border-color:#3a2a2a;color:#664444}.btn.arch:hover{background:#2a1a1a;color:#cc6666}
.btn.unarch{border-color:#2a3a2a;color:#446644}.btn.unarch:hover{background:#1a2a1a;color:#66aa66}

.arch-row{font-size:10px;color:#555;cursor:pointer;padding:2px 4px 2px 4px;user-select:none;margin-top:2px;display:flex;align-items:center;gap:4px}
.arch-row:hover{color:#888}

.resume-menu{position:fixed;background:#252525;border:1px solid #3a3a3a;border-radius:4px;padding:3px 0;z-index:9999;min-width:120px;box-shadow:0 4px 14px rgba(0,0,0,.6)}
.resume-opt{padding:5px 12px;font-size:11px;color:#bbb;cursor:pointer;white-space:nowrap}
.resume-opt:hover{background:#333;color:#fff}
.arch-chv{font-size:9px;transition:transform .1s;display:inline-block}
.arch-row.open .arch-chv{transform:rotate(90deg)}

#empty{text-align:center;color:#555;font-size:11px;margin-top:30px}
.spin{display:inline-block;animation:spin .8s linear infinite}
@keyframes spin{from{transform:rotate(0deg)}to{transform:rotate(360deg)}}
</style>
</head>
<body>
<div id="hdr">
  <h1>⚡ Claude</h1>
  <button id="rbtn" onclick="doLoad()" title="Refresh"><span id="rs">↻</span></button>
</div>
<input id="search" type="text" placeholder="Filter…" oninput="render()">
<div id="root">Loading…</div>

<script src="https://cdn.jsdelivr.net/npm/sortablejs@1.15.6/Sortable.min.js"></script>
<script>
let data = [];
let collapsed = new Set(JSON.parse(localStorage.getItem('cc_col')||'[]'));
let expandedArchived = new Set();
let isDragging = false;
const projSortables = {};
let _tip = null;
let _tipTarget = null;

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
async function doLoad(){
  if(isDragging) return;
  document.getElementById('rs').className='spin';
  try{
    const r=await fetch('/api/data');
    data=await r.json();
    render();
  }catch{
    document.getElementById('root').innerHTML='<div id="empty">Connection error</div>';
  }
  document.getElementById('rs').className='';
}

let _projSortable=null;

function saveProjectOrder(){
  const root=document.getElementById('root');
  const order=[...root.querySelectorAll(':scope>.proj')].map(el=>el.id.slice(1));
  fetch('/api/reorder-projects',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({order})});
}

// ── render ────────────────────────────────────────────────────────────────────
function render(){
  if(isDragging) return;
  Object.keys(projSortables).forEach(e=>destroySortables(e));
  if(_projSortable){_projSortable.destroy();_projSortable=null;}
  const q=document.getElementById('search').value.toLowerCase();
  const root=document.getElementById('root');
  const scrollY=root.scrollTop;
  root.innerHTML='';
  let any=false;
  for(const p of data){
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
        // Collapse all projects so the full list is visible for dragging
        _preCollapseState=new Set(collapsed);
        root.querySelectorAll('.proj:not(.collapsed)').forEach(el=>{
          const enc=el.id.slice(1);
          collapsed.add(enc);
          el.classList.add('collapsed');
        });
      },
      onEnd:()=>{
        isDragging=false;
        // Restore collapse state
        if(_preCollapseState!==null){
          root.querySelectorAll('.proj').forEach(el=>{
            const enc=el.id.slice(1);
            if(_preCollapseState.has(enc)) collapsed.add(enc);
            else{ collapsed.delete(enc); el.classList.remove('collapsed'); }
          });
          localStorage.setItem('cc_col',JSON.stringify([...collapsed]));
          _preCollapseState=null;
        }
        saveProjectOrder();
      },
    });
  }
}

// ── project element ───────────────────────────────────────────────────────────
function buildProject(p, q){
  const enc=p.encoded_name;
  const col=collapsed.has(enc);
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
  el.className=`sess ${sc}${isArch?' archived-item':''}`;
  el.dataset.id=s.id;
  el.dataset.tty=s.tty||'';
  el.dataset.path=p.path||'';
  el.dataset.enc=p.encoded_name;

  // sr row
  const sr=document.createElement('div');sr.className='sr';
  const dh=document.createElement('span');dh.className='dh';dh.title='Drag to reorder';dh.textContent='⠿';
  const dot=document.createElement('span');dot.className=`dot ${s.is_open?'open':s.status}`;dot.title=s.is_open?'Open in iTerm2':s.status;
  const lbl=document.createElement('span');lbl.className='lbl'+(s.label?'':' unnamed')+(s.computed_title?' has-recap':'');lbl.textContent=s.label||'unnamed';lbl.onclick=()=>renEl(lbl);
  if(s.computed_title){lbl.dataset.recap=s.computed_title;}
  sr.append(dh,dot,lbl);el.appendChild(sr);

  // meta
  const sm=document.createElement('div');sm.className='smeta';
  sm.innerHTML=`<span>${s.time_str}</span>${s.branch&&s.branch!=='HEAD'?`<span class="branch" title="${x(s.branch)}">${x(s.branch)}</span>`:''}${s.is_worktree?'<span class="worktree">worktree</span>':''}`;
  el.appendChild(sm);

  // actions
  const sa=document.createElement('div');sa.className='sa';
  if(s.is_open&&!isArch){const b=document.createElement('button');b.className='btn f';b.textContent='Focus';b.onclick=()=>focusEl(el);sa.appendChild(b);}
  if(!s.is_open&&!isArch){const b=document.createElement('button');b.className='btn r';b.textContent='Resume';b.onclick=()=>resumeEl(b);sa.appendChild(b);}
  const rb=document.createElement('button');rb.className='btn';rb.title='Rename';rb.textContent='✎';rb.onclick=()=>renEl(lbl);sa.appendChild(rb);
  const ab=document.createElement('button');
  if(!isArch){ab.className='btn arch';ab.title='Archive';ab.textContent='⊟';ab.onclick=()=>archiveEl(ab,s.id,p.encoded_name);}
  else{ab.className='btn unarch';ab.title='Unarchive';ab.textContent='⊞';ab.onclick=()=>unarchiveEl(ab,s.id,p.encoded_name);}
  sa.appendChild(ab);el.appendChild(sa);

  // children container for nesting
  const ch=document.createElement('div');ch.className='children';el.appendChild(ch);
  return el;
}

// ── helpers ───────────────────────────────────────────────────────────────────
function filterTree(sessions, q, projName){
  if(!q) return sessions;
  return sessions.flatMap(s=>{
    const kids=filterTree(s.children||[],q,'');
    const match=(s.label||'').toLowerCase().includes(q)||projName.toLowerCase().includes(q)||s.id.startsWith(q);
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
  collapsed.has(enc)?collapsed.delete(enc):collapsed.add(enc);
  localStorage.setItem('cc_col',JSON.stringify([...collapsed]));
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
  const opts=[{l:'Split pane',m:'split'},{l:'New tab',m:'tab'},{l:'New window',m:'window'}];
  for(const o of opts){
    const item=document.createElement('div');
    item.className='resume-opt';item.textContent=o.l;
    item.onclick=async()=>{
      menu.remove();
      btn.textContent='…';btn.disabled=true;
      await fetch('/api/resume',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:el.dataset.id,project_path:el.dataset.path,mode:o.m})});
      // Claude can take several seconds to start and register its PID
      setTimeout(doLoad,3000);
      setTimeout(doLoad,7000);
      setTimeout(doLoad,13000);
    };
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
  const sid=lbl.closest('.sess').dataset.id;
  const cur=lbl.classList.contains('unnamed')?'':lbl.textContent;
  const inp=document.createElement('input');
  inp.className='lbl-inp';inp.value=cur;inp.placeholder='Label…';
  lbl.replaceWith(inp);inp.focus();inp.select();
  const commit=async()=>{
    const v=inp.value.trim();
    const span=document.createElement('span');
    const recap=lbl.dataset.recap||'';
    span.className='lbl'+(v?'':' unnamed')+(recap?' has-recap':'');span.textContent=v||'unnamed';span.onclick=()=>renEl(span);
    if(recap) span.dataset.recap=recap;
    inp.replaceWith(span);
    await fetch('/api/rename',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:sid,label:v})});
  };
  inp.onblur=commit;
  inp.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();inp.blur();}if(e.key==='Escape'){inp.replaceWith(lbl);}};
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

doLoad();
setInterval(doLoad,8000);
</script>
</body>
</html>"""


# ── Entry point ───────────────────────────────────────────────────────────────

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
    print(f"[claude-sessions] running on port {PORT}", file=sys.stderr)

    await asyncio.Event().wait()


iterm2.run_forever(main, retry=True)
