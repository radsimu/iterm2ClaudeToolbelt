#!/bin/bash
# Intercepts /fork-split prompts at UserPromptSubmit time.
# Runs the fork script and exits 2 to block the message from reaching Claude.
# This prevents Claude from making a response turn, so Stop hooks never fire.

INPUT=$(cat)
# UserPromptSubmit payload: {"prompt": "...", "session_id": ..., "transcript_path": ..., ...}.
# Older versions used "message" instead of "prompt"; accept either to stay compatible.
#
# Print:  <empty>          → not /fork-split, pass through
#         "skip"           → /fork-split but a duplicate replay (rewind) — skip
#         "<NAME>"         → run the fork with NAME as argument (NAME may be empty)
DECISION=$(echo "$INPUT" | python3 - <<'PYEOF'
import json, sys
from pathlib import Path

try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)

prompt = (d.get("prompt") or d.get("message") or "").strip()
if not prompt.startswith("/fork-split"):
    sys.exit(0)

# Replay/rewind safety: if this exact prompt already exists in the transcript,
# we're being re-fired by Claude Code's rewind machinery — bail out instead
# of spawning another fork (and corrupting the rewind in the process).
trans = d.get("transcript_path") or ""
if trans:
    try:
        with Path(trans).open("rb") as fh:
            for raw in fh:
                try:
                    rec = json.loads(raw)
                except Exception:
                    continue
                if rec.get("type") != "user":
                    continue
                content = (rec.get("message") or {}).get("content")
                if isinstance(content, str) and content.strip() == prompt:
                    print("skip")
                    sys.exit(0)
    except Exception:
        pass

name = prompt[len("/fork-split"):].strip()
# Print NAME (may be empty); the bash wrapper detects this by exit code 0
# and a non-empty stdout.
print("run::" + name)
PYEOF
)

# Empty stdout → DECISION not /fork-split, pass through.
[[ -z "$DECISION" ]] && exit 0

# "skip" → replay detected, pass through (don't run the fork).
[[ "$DECISION" == "skip" ]] && exit 0

# Otherwise: "run::<NAME>"
NAME="${DECISION#run::}"

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
