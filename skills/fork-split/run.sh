#!/bin/bash
# Usage: run.sh [session name]
# Requires: python3 with the `iterm2` package installed (pip install iterm2).
# Optionally honors $CLAUDE_FORK_SPLIT_PYTHON to pin a specific interpreter.
export FORK_NAME="${1:-}"

PY="${CLAUDE_FORK_SPLIT_PYTHON:-python3}"

"$PY" << 'PYEOF'
import iterm2, os, json, glob, sys, subprocess, re
from pathlib import Path

FORK_NAME          = os.environ.get('FORK_NAME', '')
CURRENT_SESSION_ID = os.environ.get('ITERM_SESSION_ID', '').split(':')[-1]

# Detect the iterm2ClaudeToolbelt: it registers a launchd agent that owns the
# session-order file. If it isn't installed we skip the auto-nest entirely.
TOOLBELT_PRESENT = (Path.home() / 'Library/LaunchAgents/com.claude-code.session-manager.plist').exists()

# ── Find most recent Claude session file ─────────────────────────────────────
files = sorted(
    [f for f in glob.glob(os.path.expanduser('~/.claude/projects/*/*.jsonl'))
     if '/memory/' not in f],
    key=os.path.getmtime, reverse=True
)
session_id = session_cwd = parent_jsonl_path = ''
for path in files[:3]:
    with open(path) as f:
        for line in f:
            try:
                d = json.loads(line.strip())
                if d.get('sessionId') and d.get('cwd'):
                    session_id, session_cwd = d['sessionId'], d['cwd']
                    parent_jsonl_path = path
                    break
            except Exception:
                pass
    if session_id:
        break

if not session_id:
    print("ERROR: Could not find Claude session", file=sys.stderr)
    sys.exit(1)

# ── Inherit flags from parent claude process ──────────────────────────────────
def parent_claude_command():
    pid = os.getpid()
    seen = set()
    for _ in range(15):
        if pid in seen or pid in (0, 1):
            break
        seen.add(pid)
        r = subprocess.run(['ps', '-p', str(pid), '-o', 'ppid=,command='],
                           capture_output=True, text=True)
        if r.returncode != 0 or not r.stdout.strip():
            break
        parts = r.stdout.strip().split(None, 1)
        ppid = int(parts[0].strip())
        cmd  = parts[1] if len(parts) > 1 else ''
        # Match `claude` as a complete command token — followed by whitespace or
        # end-of-string, optionally with a `.js` suffix. Avoids false matches on
        # paths like `/tmp/claude-XXXX-cwd` that the Bash tool appends to commands.
        if re.search(r'(^|[/ ])claude(\.js)?(\s|$)', cmd):
            return cmd
        pid = ppid
    return ''

parent_cmd  = parent_claude_command()
extra_flags = []

if '--dangerously-skip-permissions' in parent_cmd:
    extra_flags.append('--dangerously-skip-permissions')
elif '--allow-dangerously-skip-permissions' in parent_cmd:
    extra_flags.append('--allow-dangerously-skip-permissions')

if '--permission-mode' in parent_cmd:
    m = re.search(r'--permission-mode[=\s]+(\S+)', parent_cmd)
    if m:
        extra_flags.append(f'--permission-mode {m.group(1)}')

if '--model' in parent_cmd:
    m = re.search(r'--model[=\s]+(\S+)', parent_cmd)
    if m:
        extra_flags.append(f'--model {m.group(1)}')

# ── Build fork command ────────────────────────────────────────────────────────
escaped_cwd = session_cwd.replace("'", "'\"'\"'")
extra        = (' ' + ' '.join(extra_flags)) if extra_flags else ''
cmd = f"cd '{escaped_cwd}' && claude --resume {session_id} --fork-session{extra}"
if FORK_NAME:
    escaped_name = FORK_NAME.replace("'", "'\"'\"'")
    cmd += f" -n '{escaped_name}'"

# ── Background nest-watcher (only used when the toolbelt is installed) ───────
# Waits for the fork's JSONL to appear, then rewrites the toolbelt's order file
# so the new session is nested under the parent (instead of getting prepended
# at the top of the project as a fresh root).
NEST_WATCHER_CODE = r"""
import sys, time, json
from pathlib import Path

proj_dir = Path(sys.argv[1])
parent_sid = sys.argv[2]
existing = set(filter(None, sys.argv[3].split(',')))
parent_cwd = sys.argv[4]
order_file = Path.home() / '.claude/session-manager-order.json'
sessions_dir = Path.home() / '.claude/sessions'


def remove_from_tree(nodes, sid):
    out = []
    for node in nodes:
        nid = node if isinstance(node, str) else node['id']
        if nid == sid:
            continue
        if isinstance(node, dict):
            node = {**node, 'children': remove_from_tree(node.get('children', []), sid)}
        out.append(node)
    return out


def insert_as_child(nodes, parent_sid, child_sid):
    out = []
    for node in nodes:
        if isinstance(node, str):
            if node == parent_sid:
                out.append({'id': node, 'children': [child_sid]})
            else:
                out.append(node)
        else:
            kids = node.get('children') or []
            if node['id'] == parent_sid:
                out.append({**node, 'children': [child_sid] + list(kids)})
            else:
                out.append({**node, 'children': insert_as_child(kids, parent_sid, child_sid)})
    return out


def find_new_sid():
    # Source 1: a newly-written pid file in ~/.claude/sessions/ pointing to our cwd.
    # Claude writes this on startup, so we see the fork's sid before any JSONL exists.
    # If several pid files match, pick the most recently created one — that's the fork.
    candidates = []
    try:
        for pf in sessions_dir.iterdir():
            if pf.suffix != '.json':
                continue
            try:
                d = json.loads(pf.read_text(encoding='utf-8'))
            except Exception:
                continue
            sid = d.get('sessionId')
            if sid and sid not in existing and d.get('cwd') == parent_cwd:
                try:
                    candidates.append((pf.stat().st_mtime, sid))
                except Exception:
                    candidates.append((0, sid))
    except Exception:
        pass
    if candidates:
        return max(candidates)[1]
    # Source 2: a new JSONL in the project dir (covers cases where the pid file
    # is missing for some reason, e.g. older Claude versions).
    try:
        cur = {f.stem for f in proj_dir.iterdir() if f.suffix == '.jsonl'}
        diff = cur - existing
        if diff:
            return max(diff, key=lambda s: (proj_dir / f'{s}.jsonl').stat().st_mtime)
    except Exception:
        pass
    return None


new_sid = None
deadline = time.time() + 30
while time.time() < deadline:
    new_sid = find_new_sid()
    if new_sid:
        break
    time.sleep(0.3)

if not new_sid:
    sys.exit(0)

# Give the toolbelt a moment to react to the new sid first (so we don't fight
# with its own prepend write), then settle the position by re-inserting under
# the parent.
time.sleep(1.0)

try:
    order = json.loads(order_file.read_text(encoding='utf-8'))
except Exception:
    order = {}

enc = proj_dir.name
tree = order.get(enc, [])
tree = remove_from_tree(tree, new_sid)
tree = insert_as_child(tree, parent_sid, new_sid)
order[enc] = tree

try:
    order_file.write_text(json.dumps(order, indent=2, ensure_ascii=False), encoding='utf-8')
except Exception:
    pass
"""


# ── iTerm2 split logic ────────────────────────────────────────────────────────
def leaves(node):
    if isinstance(node, iterm2.Session):
        return [node]
    return [s for child in node.children for s in leaves(child)]

def right_subtree(node, sid):
    if isinstance(node, iterm2.Session):
        return None
    for i, child in enumerate(node.children):
        if any(s.session_id == sid for s in leaves(child)):
            if node.vertical and i + 1 < len(node.children):
                return node.children[i + 1]
            return right_subtree(child, sid)
    return None

async def main(connection):
    app = await iterm2.async_get_app(connection)
    cur = app.get_session_by_id(CURRENT_SESSION_ID)
    if not cur:
        print(f"ERROR: session {CURRENT_SESSION_ID} not found", file=sys.stderr)
        sys.exit(1)

    # Snapshot existing sids BEFORE sending the fork command — otherwise the
    # forked claude can start, write ~/.claude/sessions/<pid>.json, and have
    # its sid appear in our "existing" set before we even capture it. Then the
    # watcher finds nothing new and the order file never gets updated.
    existing_sids: set = set()
    proj_dir = Path(parent_jsonl_path).parent if parent_jsonl_path else None
    if TOOLBELT_PRESENT and proj_dir is not None:
        try:
            existing_sids.update(p.stem for p in proj_dir.iterdir() if p.suffix == '.jsonl')
        except Exception:
            pass
        sessions_dir = Path.home() / '.claude/sessions'
        try:
            for pf in sessions_dir.iterdir():
                if pf.suffix != '.json':
                    continue
                try:
                    d = json.loads(pf.read_text(encoding='utf-8'))
                except Exception:
                    continue
                if d.get('sessionId'):
                    existing_sids.add(d['sessionId'])
        except Exception:
            pass

    rhs = right_subtree(cur.tab.root, CURRENT_SESSION_ID)
    new = await (leaves(rhs)[-1].async_split_pane(vertical=False)
                 if rhs else
                 cur.async_split_pane(vertical=True))
    await new.async_send_text(cmd + '\n')

    if TOOLBELT_PRESENT and proj_dir is not None:
        subprocess.Popen(
            [sys.executable, '-c', NEST_WATCHER_CODE,
             str(proj_dir), session_id, ','.join(sorted(existing_sids)), session_cwd],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

iterm2.run_until_complete(main)
PYEOF
