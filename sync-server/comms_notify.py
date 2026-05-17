"""Cross-service: noted-sync → agent-comms peer-DM bridge.

When a note_event is inserted, noted-sync fans out notifications to the
relevant comms handles via POST /api/messages. The recipient sees the DM
land in their normal comms inbox alongside agent-to-agent messages.

`from_handle` rules:
  - If the event's author_handle resolves to a known comms identity, use it
  - Otherwise (operator, unknown, etc.) fall back to 'noted-bot' so DMs
    always have a valid sender (comms rejects unknown from_handle)

Fire-and-forget: every call returns immediately; actual HTTP happens in a
daemon thread. Failures get recorded in sync_audit so we can diagnose.
"""
from __future__ import annotations

import json
import os
import re
import threading
import urllib.error
import urllib.request
from typing import Iterable, Optional

_TOKEN = os.environ.get("AGENT_COMMS_TOKEN", "").strip()
_BASE = os.environ.get("AGENT_COMMS_BASE", "https://jimmyspianotuning.com.au/comms").strip().rstrip("/")
_BOT_HANDLE = "noted-bot"

# Cache: handle → exists?  Refreshed on misses; entries valid for ~5min.
_handle_exists_cache: dict[str, tuple[bool, float]] = {}
_CACHE_TTL_SECONDS = 300

# Matches @<handle> in note bodies. Allows alphanumerics + hyphens; min 4 chars
# (filters out common @-words like @me / @us).
MENTION_RE = re.compile(r"@([A-Za-z][A-Za-z0-9_-]{3,63})")


def _enabled() -> bool:
    return bool(_TOKEN) and bool(_BASE)


def _http_json(method: str, path: str, body=None, timeout: int = 10):
    if not _enabled():
        raise RuntimeError("comms_notify not configured (AGENT_COMMS_TOKEN missing)")
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": "Bearer " + _TOKEN}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(_BASE + path, method=method, headers=headers, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, None


def handle_exists(handle: str) -> bool:
    """Whether `handle` is a known comms identity. 5min cache, async-safe."""
    import time
    h = handle.strip()
    if not h:
        return False
    now = time.time()
    cached = _handle_exists_cache.get(h)
    if cached and (now - cached[1]) < _CACHE_TTL_SECONDS:
        return cached[0]
    code, _ = _http_json("GET", f"/api/identities/{h}")
    exists = code == 200
    _handle_exists_cache[h] = (exists, now)
    return exists


def parse_mentions(body: Optional[str]) -> list[str]:
    """Extract @-mentions from a note/event body. Returns DEDUPED list of
    candidate handles (validation against comms happens at fan-out time)."""
    if not body:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in MENTION_RE.finditer(body):
        h = m.group(1)
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def resolve_from_handle(author_handle: Optional[str]) -> str:
    """Determine the `from_handle` for fan-out DMs. If author is a known
    comms identity, use it; else fall back to noted-bot. 'operator' is the
    explicit fallback case from iOS."""
    if author_handle and author_handle != "operator" and handle_exists(author_handle):
        return author_handle
    return _BOT_HANDLE


def send_dm(from_handle: str, to_handle: str, title: str, summary: str, body: str,
            tags: Optional[list[str]] = None) -> tuple[int, Optional[dict]]:
    """Synchronous send. Returns (status, response_or_none). Caller is
    responsible for backgrounding if non-blocking is desired."""
    return _http_json("POST", "/api/messages", {
        "from_handle": from_handle,
        "to_handle": to_handle,
        "title": title[:200],
        "summary": summary[:300],
        "body": body[:8000],
        "tags": tags or ["noted"],
    })


def fan_out_async(from_handle: str, recipients: Iterable[str],
                  title: str, summary: str, body: str,
                  on_failure=None) -> None:
    """Spawn a daemon thread that DMs each recipient. Failures call
    on_failure(handle, status, response) — caller wires this to sync_audit
    so we can diagnose unreliable delivery without blocking /events."""
    targets = [r for r in {h.strip() for h in recipients if h and h.strip()} if r != from_handle]
    if not targets:
        return
    sender = resolve_from_handle(from_handle)

    def _run():
        for to in targets:
            try:
                code, resp = send_dm(sender, to, title, summary, body)
                if code >= 400 and on_failure:
                    on_failure(to, code, resp)
            except Exception as e:
                if on_failure:
                    on_failure(to, -1, {"error": str(e)})

    t = threading.Thread(target=_run, name=f"comms-notify-{sender}", daemon=True)
    t.start()
