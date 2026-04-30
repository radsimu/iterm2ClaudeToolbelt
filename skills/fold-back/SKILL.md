---
name: fold-back
description: Summarize the current fork and inject the digest back into the parent conversation, then exit the fork pane. Use this from a session created with /fork-split when you want to bring conclusions back without polluting the main conversation with tool calls and dead ends.
allowed-tools: Bash
---

You are about to fold this forked conversation back into the parent that spawned it.

1. First call `${CLAUDE_PLUGIN_ROOT}/skills/fold-back/run.sh` exactly once. It writes a fold-back marker that records the parent session and pane.

2. After the script returns, write a concise digest of what we've concluded in THIS fork. Cover:
   - Conclusions, decisions, recommendations.
   - New facts the parent should know.
   - Code or commands the parent should pick up.

   Skip: tool-call details, dead ends, exploratory bits the parent doesn't need to relive.

3. Output ONLY the digest text — no preamble, no "Here's the summary:", no closing remark. Your final assistant message IS the payload that will be routed to the parent. After that, the Stop hook will close this pane automatically.
