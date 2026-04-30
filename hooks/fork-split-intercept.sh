#!/bin/bash
# Intercepts /fork-split prompts at UserPromptSubmit time.
# Runs the fork script and exits 2 to block the message from reaching Claude.
# This prevents Claude from making a response turn, so Stop hooks never fire.

INPUT=$(cat)
# UserPromptSubmit payload: {"prompt": "...", "session_id": ..., ...}.
# Older versions used "message"; accept either to stay compatible.
MSG=$(echo "$INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
print((d.get('prompt') or d.get('message') or '').strip())
" 2>/dev/null)

if [[ "$MSG" != /fork-split* ]]; then
  exit 0
fi

NAME=$(echo "$MSG" | sed 's|^/fork-split[[:space:]]*||')

# Plugin install path: Claude exports CLAUDE_PLUGIN_ROOT.
# Manual symlink install: fall back to the conventional ~/.claude location.
RUN="${CLAUDE_PLUGIN_ROOT:+${CLAUDE_PLUGIN_ROOT}/skills/fork-split/run.sh}"
RUN="${RUN:-$HOME/.claude/skills/fork-split/run.sh}"

if [[ -n "$NAME" ]]; then
  "$RUN" "$NAME"
else
  "$RUN"
fi

# Exit 2 blocks the message from Claude — no turn, no Stop hooks
exit 2
