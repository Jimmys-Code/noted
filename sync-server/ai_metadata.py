"""AI-generated metadata for notes: title, tags, summary, tldr, key_points.

Lifted the OpenRouter call pattern from /root/jimmys_projects/piano-tuning/ai/messages.py
(same droplet, same API key in env). Defaults to google/gemini-3-flash-preview —
cheap (~$0.0001 per note), fast (1-2s typical), good at structured output.

Worker thread in app.py polls notes WHERE ai_status='pending' and calls
generate_for_note() against each. Results write back to the same row.

Failure mode: returns partial dict {'error': '...', 'attempts': N} so the
worker can record + retry with backoff. Never raises into the worker loop —
keeps the worker alive even if OpenRouter is briefly down.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from typing import Optional

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Reuse the model from piano-tuning's .env if available, otherwise sensible default.
DEFAULT_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemini-3-flash-preview").strip()

# Notes shorter than this get skipped (no AI metadata, ai_status='skipped').
# Avoids garbage titles for "asdf" / "test" stubs.
MIN_BODY_CHARS_FOR_AI = int(os.getenv("AI_MIN_BODY_CHARS", "30"))

# Sibling note titles included in context (helps Gemini disambiguate "the bug
# I just logged" by showing what the other open issues look like).
SIBLING_TITLES_COUNT = 5


def _key() -> str:
    return os.getenv("OPENROUTER_API_KEY", "").strip()


SYSTEM_PROMPT = """You generate metadata for a personal note. The note's
author is one specific operator — your output goes into THEIR private note
index, not to other users.

Return ONLY a JSON object, no preamble, no markdown fence. Schema:

{
  "title":       "2-4 words, ideally 2-3, never generic",
  "tags":        ["lowercase", "kebab-case", "max 5 tags"],
  "summary":     "ONE sentence describing what the note is about (under 100 chars).",
  "tldr":        "ONE paragraph (max 4 sentences) — what + why + key takeaway. No preamble.",
  "key_points":  ["bullet 1", "bullet 2", ...]    // 2-6 punchy bullets
}

TITLE RULES — load-bearing:
- 2-4 words MAX. 2-3 ideal.
- Concrete + distinguishing. NEVER generic ("bug fix", "note", "todo",
  "issue tracker", "thoughts on X"). If the title would also apply to 100
  other notes, it's wrong.
- Use specifics from the body (filename, person name, exact symptom,
  product name) so the operator's brain instantly recalls "oh THAT one".
  Example: "Vim cursor jumps on save" not "Editor bug".
- If the note has an existing title that's already specific, REUSE it
  verbatim (don't paraphrase). Only generate a fresh title when the
  existing one is generic / placeholder / missing.

OTHER RULES:
- TLDR should answer: what is this note about? what's the action item or
  takeaway? Skip filler like "This note discusses…".
- Key points: extract concrete claims/actions/symptoms, not paraphrases.
  If the note is a checklist, surface the items. If it's an issue, surface
  the symptom + tried fixes + state.
- Tags: lowercase, kebab-case, concrete domain words ("ios", "vim",
  "noted-app"), NOT generic ("idea", "todo", "issue" — those are statuses,
  not tags).
- Match the operator's voice — they write terse, no AI-style filler. No
  em-dashes, no "robust", no "delve into", no "tapestry".

If the body is non-English, return metadata in the body's language.
If the body is empty or near-empty, return {"title": "Untitled", "tags":
[], "summary": "", "tldr": "", "key_points": []}."""


def _build_user_prompt(note_title: Optional[str], body: str,
                        folder_name: Optional[str], folder_kind: Optional[str],
                        status: Optional[str], sibling_titles: list[str]) -> str:
    title_line = f'Existing title: "{note_title}"' if note_title and note_title.strip() and note_title != "Untitled" else "Existing title: (none — generate one)"
    folder_line = f"Folder: {folder_name} (kind={folder_kind})" if folder_name else "Folder: (uncategorized)"
    status_line = f"Status: {status}" if status else "Status: (no status)"
    sibling_block = ""
    if sibling_titles:
        sibling_block = "\n\nSibling note titles in the same folder (for disambiguation context — do not repeat them):\n" + \
            "\n".join(f"  - {t}" for t in sibling_titles)
    return f"""{title_line}
{folder_line}
{status_line}{sibling_block}

NOTE BODY:
{body}

Return JSON only."""


def _call_openrouter(system: str, user: str, model: str = DEFAULT_MODEL,
                     max_tokens: int = 800, temperature: float = 0.3,
                     timeout: int = 30) -> str:
    """Returns raw model text. Raises on transport/auth failure."""
    key = _key()
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
        # Suppress reasoning tokens (waste of budget for structured output)
        "reasoning": {"enabled": False, "exclude": True},
    }
    req = urllib.request.Request(
        OPENROUTER_URL, method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://jimmyspianotuning.com.au/noted",
            "X-Title": "noted-sync metadata worker",
        },
        data=json.dumps(body).encode(),
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode())
    msg = data["choices"][0]["message"]
    content = msg.get("content") or ""
    if not content:
        # Some reasoning models return content=None even with reasoning suppressed
        raise RuntimeError(f"empty response from {model}: {json.dumps(data)[:300]}")
    return content


_DASH_REPLACEMENTS = [("—", ","), ("–", "-"), ("−", "-")]


def _strip_dashes(s: Optional[str]) -> Optional[str]:
    if not s:
        return s
    for src, dst in _DASH_REPLACEMENTS:
        s = s.replace(src, dst)
    return re.sub(r"\s{2,}", " ", s).strip()


def _parse_response(raw: str) -> dict:
    """Tolerant JSON parser: strip ```json fences if model added them despite
    response_format=json_object; trim BOM/whitespace; validate shape."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```$", "", text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"model returned non-JSON: {text[:200]!r}") from e
    if not isinstance(parsed, dict):
        raise RuntimeError(f"expected object, got {type(parsed).__name__}: {text[:200]!r}")
    # Normalize + clean
    return {
        "title":      _strip_dashes(parsed.get("title")),
        "tags":       [t.lower().strip() for t in (parsed.get("tags") or []) if isinstance(t, str) and t.strip()][:5],
        "summary":    _strip_dashes(parsed.get("summary")),
        "tldr":       _strip_dashes(parsed.get("tldr")),
        "key_points": [_strip_dashes(p) for p in (parsed.get("key_points") or []) if isinstance(p, str) and p.strip()][:8],
    }


def generate_for_note(note_title: Optional[str], body: str,
                      folder_name: Optional[str], folder_kind: Optional[str],
                      status: Optional[str], sibling_titles: list[str],
                      model: str = DEFAULT_MODEL) -> dict:
    """Top-level entry. Returns {title, tags, summary, tldr, key_points, model}
    on success. Raises on failure — caller wraps + records ai_error."""
    user_prompt = _build_user_prompt(note_title, body, folder_name, folder_kind, status, sibling_titles)
    raw = _call_openrouter(SYSTEM_PROMPT, user_prompt, model=model)
    metadata = _parse_response(raw)
    metadata["model"] = model
    return metadata


# --- Streaming generator (progressive title/summary reveal) ---
# Parses the JSON stream char-by-char and fires callbacks the moment a field
# CLOSES (closing quote). Title fires first (it's the first key in the prompt
# schema), then summary, then the final 'done' callback with the full payload
# parsed from the complete stream.
#
# Callback contract:
#   on_field(field_name, value)  # 'title' or 'summary' with the extracted string
#   on_done(full_metadata_dict)  # all 5 fields + model after stream completes
#
# Robustness: we don't require the model to emit fields in any particular
# order. We scan the accumulated buffer after each chunk for "<field>": "...".
# Same field is never re-fired (de-duped by a 'fired' set).


# Match a complete JSON string value: "...", allowing escaped quotes.
# Group 1 = unescaped contents.
_FIELD_PATTERNS = {
    "title":   re.compile(r'"title"\s*:\s*"((?:[^"\\]|\\.)*)"'),
    "summary": re.compile(r'"summary"\s*:\s*"((?:[^"\\]|\\.)*)"'),
}


def _unescape_json_string(s: str) -> str:
    """Lightweight JSON string unescape — handles \\n, \\", \\\\, \\t."""
    return s.replace('\\"', '"').replace('\\n', '\n').replace('\\t', '\t').replace('\\\\', '\\')


def _call_openrouter_stream(system: str, user: str, model: str, max_tokens: int = 800,
                             temperature: float = 0.3, timeout: int = 60):
    """Generator yielding text deltas as they arrive over OpenRouter SSE."""
    key = _key()
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
        "reasoning": {"enabled": False, "exclude": True},
        "stream": True,
    }
    req = urllib.request.Request(
        OPENROUTER_URL, method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "HTTP-Referer": "https://jimmyspianotuning.com.au/noted",
            "X-Title": "noted-sync metadata worker (stream)",
        },
        data=json.dumps(body).encode(),
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        # SSE: "data: {...}\n\n" repeated, ending with "data: [DONE]"
        buf = ""
        while True:
            chunk = r.read(1024)
            if not chunk:
                break
            buf += chunk.decode("utf-8", errors="replace")
            while "\n\n" in buf:
                event, buf = buf.split("\n\n", 1)
                for line in event.split("\n"):
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    if payload == "[DONE]":
                        return
                    try:
                        msg = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    choices = msg.get("choices") or []
                    if not choices:
                        continue
                    delta = (choices[0].get("delta") or {}).get("content")
                    if delta:
                        yield delta


def stream_for_note(note_title: Optional[str], body: str,
                    folder_name: Optional[str], folder_kind: Optional[str],
                    status: Optional[str], sibling_titles: list[str],
                    on_field, on_done,
                    model: str = DEFAULT_MODEL) -> None:
    """Stream variant. Calls on_field('title', value) the moment the title
    field closes in the JSON stream; same for 'summary'. Then on_done(meta)
    with the full parsed payload. Raises on transport / parse failure —
    caller catches and records ai_error."""
    user_prompt = _build_user_prompt(note_title, body, folder_name, folder_kind, status, sibling_titles)
    buffer = ""
    fired: set[str] = set()
    for delta in _call_openrouter_stream(SYSTEM_PROMPT, user_prompt, model=model):
        buffer += delta
        for field, pattern in _FIELD_PATTERNS.items():
            if field in fired:
                continue
            m = pattern.search(buffer)
            if m:
                value = _strip_dashes(_unescape_json_string(m.group(1)))
                fired.add(field)
                try:
                    on_field(field, value)
                except Exception as e:
                    # Don't let a write hiccup kill the stream; log only
                    print(f"[ai_stream] on_field({field}) callback raised: {e}", flush=True)
    # End of stream — parse full payload
    metadata = _parse_response(buffer)
    metadata["model"] = model
    on_done(metadata)


def compute_input_hash(body: str, title: Optional[str], folder_name: Optional[str],
                       status: Optional[str]) -> str:
    """Stable hash of the inputs that drive AI generation. Stored on rows so
    no-op writes (e.g. status change with no body change) don't re-burn tokens."""
    payload = "|".join([
        (title or ""),
        (folder_name or ""),
        (status or ""),
        body,
    ])
    return hashlib.sha256(payload.encode()).hexdigest()
