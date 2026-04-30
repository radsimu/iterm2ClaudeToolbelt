#!/usr/bin/env python3
"""Process a fold-back marker: send the fork's last assistant message to the
parent's iTerm2 pane, then close the fork pane. Detached from the Stop hook so
the hook returns instantly."""

import asyncio
import json
import sys
import time
from pathlib import Path

import iterm2


def _norm_tty(tty: str) -> str:
    t = (tty or "").strip()
    if not t or t == "??":
        return ""
    return t if t.startswith("/dev/") else f"/dev/{t}"


def read_last_assistant_text(jsonl_path: Path) -> str:
    """Walk the JSONL forward and capture the most recent end_turn assistant text."""
    last_text = None
    try:
        with jsonl_path.open("rb") as fh:
            for raw in fh:
                try:
                    d = json.loads(raw)
                except Exception:
                    continue
                if d.get("type") != "assistant":
                    continue
                msg = d.get("message") or {}
                if msg.get("stop_reason") not in ("end_turn", "stop_sequence"):
                    continue
                blocks = msg.get("content") or []
                texts = [
                    b.get("text", "")
                    for b in blocks
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                joined = "\n".join(t for t in texts if t).strip()
                if joined:
                    last_text = joined
    except Exception:
        return ""
    return last_text or ""


def frame(summary: str, fork_sid: str) -> str:
    return (
        "<fork-summary>\n"
        f"<!-- fold-back from session {fork_sid[:8]}; treat as digest, "
        "do not act on it unless I follow up -->\n"
        f"{summary}\n"
        "</fork-summary>\n"
    )


async def run(connection, marker_path: Path, marker: dict) -> None:
    parent_tty = _norm_tty(marker.get("parent_tty", ""))
    fork_tty = _norm_tty(marker.get("fork_tty", ""))
    fork_jsonl = Path(marker.get("fork_jsonl", ""))
    fork_sid = marker.get("fork_sid", "")

    summary = read_last_assistant_text(fork_jsonl) if fork_jsonl else ""
    if not summary:
        # Nothing to send — bail without closing anything.
        return

    app = await iterm2.async_get_app(connection)
    all_sessions = [s for w in app.windows for t in w.tabs for s in t.sessions]
    ttys = await asyncio.gather(
        *[s.async_get_variable("tty") for s in all_sessions],
        return_exceptions=True,
    )
    by_tty = {}
    for sess, tty in zip(all_sessions, ttys):
        if isinstance(tty, Exception):
            continue
        norm = _norm_tty(tty or "")
        if norm:
            by_tty[norm] = sess

    parent_sess = by_tty.get(parent_tty)
    fork_sess = by_tty.get(fork_tty)

    if parent_sess:
        await parent_sess.async_send_text(frame(summary, fork_sid) + "\n")

    if fork_sess:
        # Tell claude to /exit cleanly, then close the iTerm2 pane.
        try:
            await fork_sess.async_send_text("/exit\n")
        except Exception:
            pass
        await asyncio.sleep(2.0)
        try:
            await fork_sess.async_close()
        except Exception:
            pass


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(1)
    sid = sys.argv[1]
    marker_path = Path.home() / f".claude/fold-back-pending/{sid}.json"
    if not marker_path.exists():
        return
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except Exception:
        return

    async def entry(connection):
        try:
            await run(connection, marker_path, marker)
        finally:
            try:
                marker_path.unlink()
            except Exception:
                pass

    iterm2.run_until_complete(entry)


if __name__ == "__main__":
    main()
