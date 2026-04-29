---
name: fork-split
description: Fork the current conversation into a new iTerm2 split pane. Optional argument sets the session name (-n). Mirrors teammate-spawning layout: vertical split to the right if no right pane exists, or horizontal split below the bottommost session in the existing right column.
allowed-tools: Bash
---

Run `~/.claude/skills/fork-split/run.sh 'NAME'` where NAME is the argument the user passed (omit the arg entirely if none was given). Then say `Forked.` — nothing else.
