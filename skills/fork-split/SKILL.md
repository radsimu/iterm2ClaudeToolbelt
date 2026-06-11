---
name: fork-split
description: Fork the current conversation into a new iTerm2 split pane. The optional argument is the fork's SEED PROMPT — the forked session (which inherits the parent's full context via --resume --fork-session) auto-runs it as its first message, and a short display title is derived from it. Mirrors teammate-spawning layout: vertical split to the right if no right pane exists, or horizontal split below the bottommost session in the existing right column.
allowed-tools: Bash
---

Run `${CLAUDE_PLUGIN_ROOT}/skills/fork-split/run.sh 'SEED_PROMPT'` where SEED_PROMPT is the argument the user passed (omit the arg entirely if none was given). Because the fork inherits the parent session's full context, SEED_PROMPT should be a **concise directive of what the new session should do next** — not a re-dump of context. It may be long or multi-line (it is stashed in a temp file, so the shell paste won't break). Then say `Forked.` — nothing else.
