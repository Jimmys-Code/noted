"""Thin GitHub API client (stdlib urllib, ETag-aware conditional GET).

Returns 3-tuples: (status_code, etag, parsed_json | bytes). Status 304 means
the caller's cached copy is still valid — they should NOT replace cached data
but should bump fetched_at.

GitHub's tree endpoint resolves branch names automatically when used as the
SHA argument: GET /repos/{owner}/{repo}/git/trees/{branch}?recursive=1.
Response has a 'truncated' bool — true means the tree exceeded 100k entries
or 7MB and the caller should fall back to per-directory listing (we don't
handle that yet; just surface the flag).

File contents endpoint: GET /repos/{owner}/{repo}/contents/{path}?ref={branch}
returns base64-encoded content. We decode and return bytes; caller stores
in git_file_cache.
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional


def _request(method: str, url: str, token: str, etag: Optional[str] = None,
             timeout: int = 15) -> tuple[int, Optional[str], dict | bytes | None]:
    """Make a GitHub API request with bearer auth + optional If-None-Match.
    Returns (status, etag, body). 304 returns (304, etag, None)."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "noted-sync/1.0",
    }
    if etag:
        headers["If-None-Match"] = etag
    req = urllib.request.Request(url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            new_etag = r.headers.get("ETag")
            body = r.read()
            try:
                return r.status, new_etag, json.loads(body)
            except json.JSONDecodeError:
                return r.status, new_etag, body
    except urllib.error.HTTPError as e:
        # 304 is "not modified" — expected when ETag matches
        if e.code == 304:
            return 304, e.headers.get("ETag", etag), None
        # 404 / 401 / 403 / 422 — surface for the caller to translate to HTTPException
        body = e.read()
        try:
            return e.code, None, json.loads(body)
        except json.JSONDecodeError:
            return e.code, None, body.decode("utf-8", errors="replace")


def get_tree(owner: str, repo: str, branch: str, token: str,
             prev_etag: Optional[str] = None,
             base_url: str = "https://api.github.com",
             ) -> tuple[int, Optional[str], Optional[dict]]:
    """Recursive tree fetch for a branch (latest commit's tree)."""
    url = f"{base_url}/repos/{owner}/{repo}/git/trees/{urllib.parse.quote(branch)}?recursive=1"
    return _request("GET", url, token, etag=prev_etag)


def get_file(owner: str, repo: str, branch: str, path: str, token: str,
             prev_etag: Optional[str] = None,
             base_url: str = "https://api.github.com",
             ) -> tuple[int, Optional[str], Optional[dict]]:
    """Single-file fetch by path. Response has base64 'content' field which
    callers decode via decode_content_base64()."""
    quoted = "/".join(urllib.parse.quote(p) for p in path.split("/"))
    url = f"{base_url}/repos/{owner}/{repo}/contents/{quoted}?ref={urllib.parse.quote(branch)}"
    return _request("GET", url, token, etag=prev_etag)


def decode_content_base64(payload: dict) -> bytes:
    """GitHub's contents API returns base64-encoded body with embedded newlines."""
    return base64.b64decode(payload["content"])


def resolve_default_branch(owner: str, repo: str, token: str,
                            base_url: str = "https://api.github.com") -> Optional[str]:
    """Look up the default branch of a repo (used on link-creation if branch
    isn't specified)."""
    url = f"{base_url}/repos/{owner}/{repo}"
    status, _, payload = _request("GET", url, token)
    if status == 200 and isinstance(payload, dict):
        return payload.get("default_branch")
    return None
