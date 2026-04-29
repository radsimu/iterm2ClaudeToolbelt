#!/bin/bash
# Intercepts /fork-split prompts at UserPromptSubmit time.
# Runs the fork script and exits 2 to block the message from reaching Claude.
# This prevents Claude from making a response turn, so Stop hooks never fire.

INPUT=$(cat)
MSG=$(echo "$INPUT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('message','').strip())" 2>/dev/null)

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
