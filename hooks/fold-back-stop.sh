#!/bin/bash
# Stop-hook: if the just-finished session has a fold-back marker, kick off the
# worker that routes the digest to the parent and closes this fork pane.
INPUT=$(cat)
SID=$(echo "$INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
print((d.get('session_id') or '').strip())
" 2>/dev/null)

[ -z "$SID" ] && exit 0
MARKER="$HOME/.claude/fold-back-pending/$SID.json"
[ -f "$MARKER" ] || exit 0

WORKER="${CLAUDE_PLUGIN_ROOT:+${CLAUDE_PLUGIN_ROOT}/hooks/fold-back-worker.py}"
WORKER="${WORKER:-$HOME/.claude/hooks/fold-back-worker.py}"
PY="${CLAUDE_FOLD_BACK_PYTHON:-${CLAUDE_FORK_SPLIT_PYTHON:-python3}}"

# Detached so the Stop hook returns instantly. The worker has a few seconds
# of runway because it sends `/exit` to the fork before closing the pane.
nohup "$PY" "$WORKER" "$SID" >"/tmp/fold-back-$SID.log" 2>&1 &
disown
exit 0
