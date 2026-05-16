"""3-layer str_replace fuzzy matching for note body edits.

Lifted from zeus-comms-feature-codex-cli-support-7db5 (MIT, 354 LOC trimmed
to ~100 — server only needs str_replace_content, not file I/O).

Layers, in order: exact str.find → whitespace-normalized → difflib fuzzy
(threshold 0.8). On failure, returns top-3 candidate blocks (>50% similar)
so the calling agent can self-correct its `find` string on retry.

Rationale: agents quote the note body from memory and frequently miss
whitespace by a character. Aider benchmarks show ~9x error reduction vs.
naive str.replace for this workload.
"""
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Optional

FUZZY_THRESHOLD = 0.8
MAX_CANDIDATES = 3


@dataclass
class EditResult:
    success: bool
    new_content: Optional[str] = None
    old_block: Optional[str] = None
    match_type: Optional[str] = None  # 'exact' | 'whitespace' | 'fuzzy' | 'failed'
    similarity: Optional[float] = None
    candidates: Optional[list[str]] = None
    message: str = ""


def _normalize_whitespace(text: str) -> str:
    lines = text.splitlines()
    out = []
    for line in lines:
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        out.append(" " * indent + " ".join(stripped.split()))
    return "\n".join(out)


def _find_best_match(content: str, search: str) -> tuple[Optional[int], Optional[int], str, float]:
    # Layer 1: exact
    idx = content.find(search)
    if idx != -1:
        return idx, idx + len(search), "exact", 1.0

    # Layer 2: whitespace-normalized (match in normalized space, map back to original)
    norm_search = _normalize_whitespace(search)
    content_lines = content.splitlines(keepends=True)
    search_lines = norm_search.splitlines()
    for i in range(len(content_lines) - len(search_lines) + 1):
        window = [_normalize_whitespace(line.rstrip("\n\r")) for line in content_lines[i:i + len(search_lines)]]
        if window == search_lines:
            start = sum(len(line) for line in content_lines[:i])
            end = sum(len(line) for line in content_lines[:i + len(search_lines)])
            return start, end, "whitespace", 0.95

    # Layer 3: fuzzy via SequenceMatcher over line-windows
    search_len = len(search.splitlines())
    if search_len == 0:
        return None, None, "failed", 0.0
    best_ratio, best_start, best_end = 0.0, None, None
    for i in range(len(content_lines) - search_len + 1):
        window = "".join(content_lines[i:i + search_len])
        ratio = SequenceMatcher(None, window, search).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_start = sum(len(line) for line in content_lines[:i])
            best_end = sum(len(line) for line in content_lines[:i + search_len])
    if best_ratio >= FUZZY_THRESHOLD:
        return best_start, best_end, "fuzzy", best_ratio
    return None, None, "failed", best_ratio


def _find_candidates(content: str, search: str) -> list[str]:
    content_lines = content.splitlines()
    search_len = len(search.splitlines()) or 1
    out: list[tuple[float, str]] = []
    for i in range(len(content_lines) - search_len + 1):
        window = "\n".join(content_lines[i:i + search_len])
        ratio = SequenceMatcher(None, window, search).ratio()
        if ratio > 0.5:
            out.append((ratio, window))
    out.sort(reverse=True, key=lambda x: x[0])
    return [w for _, w in out[:MAX_CANDIDATES]]


def str_replace(content: str, old: str, new: str) -> EditResult:
    """Replace `old` with `new` in `content` via 3-layer fuzzy matching.
    On match: returns success=True + new_content + match_type + similarity.
    On miss:  returns success=False + candidates (top-3 close matches) for
              the calling agent to self-correct."""
    if not old:
        return EditResult(success=False, match_type="failed", similarity=0.0,
                          message="empty `find` string")
    start, end, match_type, similarity = _find_best_match(content, old)
    if start is None:
        candidates = _find_candidates(content, old)
        return EditResult(
            success=False, match_type="failed", similarity=similarity,
            candidates=candidates,
            message=f"no match (best similarity {similarity:.0%}). "
                    f"{len(candidates)} candidate(s) returned.",
        )
    old_block = content[start:end]
    # Whitespace + fuzzy layers match against full line windows. If the matched
    # block ends with a newline but the agent's `new` doesn't, preserve the
    # newline so we don't accidentally merge two lines into one.
    effective_new = new
    if match_type in ("whitespace", "fuzzy") and old_block.endswith(("\n", "\r\n")) and not new.endswith(("\n", "\r\n")):
        effective_new = new + ("\r\n" if old_block.endswith("\r\n") else "\n")
    new_content = content[:start] + effective_new + content[end:]
    msg = {
        "exact": "exact match",
        "whitespace": "matched via whitespace normalization",
        "fuzzy": f"fuzzy match ({similarity:.0%} similar)",
    }[match_type]
    return EditResult(
        success=True, new_content=new_content,
        old_block=old_block, match_type=match_type,
        similarity=similarity, message=msg,
    )
