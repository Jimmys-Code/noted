"""noted sync server — keeps your notes in sync across devices.

Data model: each folder + note has a globally-unique UUID and an `updated_at`
timestamp (millis since epoch). Conflict resolution is last-write-wins by
`updated_at`. Deletes are tombstones (record kept with `deleted_at` set) so
the delete can propagate to other devices on their next pull.

The local Electron app is the source of truth for the user's UI — it reads
from its own local SQLite and stays snappy regardless of network. This server
just ferries deltas: clients PULL changes since their last sync, then PUSH
their own changes; they reconcile locally with LWW.

Auth: single shared bearer token (NOTED_SYNC_TOKEN env var). Single-user.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal, Optional

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

# Two contexts: standalone (`python app.py` in sync-server/) and packaged
# (`python -m noted_sync` once deployed). Relative import wins in the package
# context; absolute falls back when run from this directory directly.
try:
    from .fuzzy_edit import str_replace as _fuzzy_str_replace
    from . import crypto as _crypto
    from . import github_client as _gh
    from . import ai_metadata as _ai
except ImportError:
    from fuzzy_edit import str_replace as _fuzzy_str_replace
    import crypto as _crypto
    import github_client as _gh
    import ai_metadata as _ai

NoteStatus = Literal["idea", "open", "in-progress", "testing", "done"]
FolderKind = Literal["general", "project"]
SortKey = Literal["updated_at", "created_at"]

# Status sets for the project/issues 3-bucket split. ACTIONABLE = "things to
# do something about right now"; PARKED = "thinking, not yet actionable".
ACTIONABLE_STATUSES = ("open", "in-progress", "testing")
PARKED_STATUSES = ("idea",)

DB_PATH = Path(os.environ.get("NOTED_SYNC_DB", "/opt/noted-sync/data/noted_sync.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
DEFAULT_TOKEN = "dev-token-change-me"


def now_ms() -> int:
    return int(time.time() * 1000)


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")  # better concurrent reads
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS folders (
    uuid              TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    color             TEXT NOT NULL DEFAULT '#7c8cff',
    position          INTEGER NOT NULL DEFAULT 0,
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL,
    deleted_at        INTEGER,
    kind              TEXT NOT NULL DEFAULT 'general',
    active            INTEGER NOT NULL DEFAULT 1,
    last_activity_at  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_folders_updated ON folders(updated_at);

CREATE TABLE IF NOT EXISTS notes (
    uuid          TEXT PRIMARY KEY,
    folder_uuid   TEXT,
    title         TEXT NOT NULL DEFAULT 'Untitled',
    body          TEXT NOT NULL DEFAULT '',
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL,
    deleted_at    INTEGER,
    status        TEXT
);
CREATE INDEX IF NOT EXISTS idx_notes_updated ON notes(updated_at);
CREATE INDEX IF NOT EXISTS idx_notes_folder ON notes(folder_uuid);

CREATE TABLE IF NOT EXISTS attachments (
    uuid          TEXT PRIMARY KEY,
    sha256        TEXT NOT NULL UNIQUE,
    filename      TEXT NOT NULL,
    mime          TEXT NOT NULL DEFAULT 'application/octet-stream',
    size          INTEGER NOT NULL,
    uploaded_at   INTEGER NOT NULL,
    data          BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attachments_sha ON attachments(sha256);

-- --- Git integration tables ---
-- PATs encrypted with Fernet (see crypto.py). key_version stamps which key
-- was used so we can rotate without one-shot re-encrypting everything.
CREATE TABLE IF NOT EXISTS git_credentials (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    provider          TEXT NOT NULL,           -- 'github' | 'gitlab' | 'github_enterprise'
    account_label     TEXT NOT NULL DEFAULT 'personal',
    encrypted_token   BLOB NOT NULL,
    key_version       INTEGER NOT NULL DEFAULT 1,
    base_url          TEXT NOT NULL DEFAULT 'https://api.github.com',
    expires_at        INTEGER,                 -- epoch ms; null = unknown
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL
);

-- 1:1 folder → repo link. ON DELETE CASCADE so unlinking on the folder side
-- (actual row delete; tombstone won't cascade) cleans up the link row.
CREATE TABLE IF NOT EXISTS folder_git_link (
    folder_uuid       TEXT PRIMARY KEY REFERENCES folders(uuid) ON DELETE CASCADE,
    credential_id     INTEGER NOT NULL REFERENCES git_credentials(id) ON DELETE RESTRICT,
    owner             TEXT NOT NULL,
    repo              TEXT NOT NULL,
    default_branch    TEXT NOT NULL DEFAULT 'main',
    last_synced_at    INTEGER,
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_link_cred ON folder_git_link(credential_id);

-- Per-folder tree cache. ETag enables conditional GET — most refreshes are
-- 304s, costing ~1KB instead of full tree refetch.
CREATE TABLE IF NOT EXISTS git_tree_cache (
    folder_uuid       TEXT NOT NULL REFERENCES folders(uuid) ON DELETE CASCADE,
    branch            TEXT NOT NULL,
    path              TEXT NOT NULL,
    sha               TEXT NOT NULL,
    size              INTEGER,
    type              TEXT NOT NULL,           -- 'blob' | 'tree'
    PRIMARY KEY (folder_uuid, branch, path)
);
CREATE INDEX IF NOT EXISTS idx_tree_folder_branch ON git_tree_cache(folder_uuid, branch);

-- One row per branch holds the most-recent ETag + fetched_at for the whole
-- tree, so we can do conditional GET without per-path bookkeeping.
CREATE TABLE IF NOT EXISTS git_tree_meta (
    folder_uuid       TEXT NOT NULL REFERENCES folders(uuid) ON DELETE CASCADE,
    branch            TEXT NOT NULL,
    etag              TEXT,
    truncated         INTEGER NOT NULL DEFAULT 0,
    fetched_at        INTEGER NOT NULL,
    PRIMARY KEY (folder_uuid, branch)
);

-- LRU file cache. accessed_at separate from fetched_at so popular files
-- survive eviction even when they haven't been re-fetched.
CREATE TABLE IF NOT EXISTS git_file_cache (
    folder_uuid       TEXT NOT NULL REFERENCES folders(uuid) ON DELETE CASCADE,
    branch            TEXT NOT NULL,
    path              TEXT NOT NULL,
    sha               TEXT NOT NULL,
    size              INTEGER NOT NULL,
    content           BLOB NOT NULL,
    etag              TEXT,
    fetched_at        INTEGER NOT NULL,
    accessed_at       INTEGER NOT NULL,
    PRIMARY KEY (folder_uuid, branch, path)
);
CREATE INDEX IF NOT EXISTS idx_file_lru ON git_file_cache(accessed_at);

-- Cache for /user/repos (per credential). One row per repo. etag stored on
-- a meta-row keyed by credential_id alone.
CREATE TABLE IF NOT EXISTS git_repo_cache (
    credential_id     INTEGER NOT NULL REFERENCES git_credentials(id) ON DELETE CASCADE,
    owner             TEXT NOT NULL,
    repo              TEXT NOT NULL,
    default_branch    TEXT,
    pushed_at         TEXT,         -- ISO string from GitHub
    private           INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (credential_id, owner, repo)
);
CREATE TABLE IF NOT EXISTS git_repo_cache_meta (
    credential_id     INTEGER PRIMARY KEY REFERENCES git_credentials(id) ON DELETE CASCADE,
    etag              TEXT,
    fetched_at        INTEGER NOT NULL
);

-- Cache for branches per folder-link. last_commit_at is enriched by the
-- server (N+1 /commits call per branch — acceptable for ~30 branches per
-- repo, runs once per 5min cache window).
CREATE TABLE IF NOT EXISTS git_branch_cache (
    folder_uuid       TEXT NOT NULL REFERENCES folders(uuid) ON DELETE CASCADE,
    name              TEXT NOT NULL,
    sha               TEXT NOT NULL,
    last_commit_at    TEXT,
    PRIMARY KEY (folder_uuid, name)
);
CREATE TABLE IF NOT EXISTS git_branch_cache_meta (
    folder_uuid       TEXT PRIMARY KEY REFERENCES folders(uuid) ON DELETE CASCADE,
    etag              TEXT,
    fetched_at        INTEGER NOT NULL
);
"""


def init_db():
    with db() as c:
        c.executescript(SCHEMA)
        _migrate(c)


def _migrate(c):
    """Idempotent ALTER TABLE migrations for existing DBs. CREATE TABLE IF NOT
    EXISTS is a no-op once the table exists, so column additions must be ALTERed
    in explicitly. Safe to run on every boot."""
    fcols = {r["name"] for r in c.execute("PRAGMA table_info(folders)").fetchall()}
    ncols = {r["name"] for r in c.execute("PRAGMA table_info(notes)").fetchall()}

    if "kind" not in fcols:
        c.execute("ALTER TABLE folders ADD COLUMN kind TEXT NOT NULL DEFAULT 'general'")
    if "active" not in fcols:
        c.execute("ALTER TABLE folders ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
    if "last_activity_at" not in fcols:
        c.execute("ALTER TABLE folders ADD COLUMN last_activity_at INTEGER")
        # one-shot backfill so sidebar sorts correctly without waiting for a touch
        c.execute(
            """UPDATE folders SET last_activity_at = (
                 SELECT MAX(updated_at) FROM notes
                  WHERE folder_uuid = folders.uuid AND deleted_at IS NULL
               ) WHERE last_activity_at IS NULL"""
        )

    if "status" not in ncols:
        c.execute("ALTER TABLE notes ADD COLUMN status TEXT")

    # AI-generated metadata. Worker thread scans for ai_status='pending' rows
    # and fills these in by calling OpenRouter. Round-tripped via /sync/changes.
    for col, ddl in [
        ("ai_title",        "TEXT"),
        ("ai_tags",         "TEXT"),    # JSON array
        ("ai_summary",      "TEXT"),    # one sentence
        ("ai_tldr",         "TEXT"),    # paragraph
        ("ai_keypoints",    "TEXT"),    # JSON array
        ("ai_generated_at", "INTEGER"),
        ("ai_model",        "TEXT"),
        ("ai_status",       "TEXT"),    # NULL | 'pending' | 'ok' | 'failed' | 'skipped'
        ("ai_input_hash",   "TEXT"),    # dedup re-gen on no-op writes
        ("ai_attempts",     "INTEGER NOT NULL DEFAULT 0"),
        ("ai_error",        "TEXT"),    # last failure reason for diagnostics
    ]:
        if col not in ncols:
            c.execute(f"ALTER TABLE notes ADD COLUMN {col} {ddl}")
    # Index for the worker scan path
    c.execute("CREATE INDEX IF NOT EXISTS idx_notes_ai_pending ON notes(ai_status) WHERE ai_status='pending'")


init_db()


def require_token(authorization: Annotated[str | None, Header()] = None):
    expected = os.environ.get("NOTED_SYNC_TOKEN", DEFAULT_TOKEN)
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    if not secrets.compare_digest(authorization.removeprefix("Bearer ").strip(), expected):
        raise HTTPException(401, "bad token")
    return True


class FolderIn(BaseModel):
    uuid: str
    name: str
    color: str = "#7c8cff"
    position: int = 0
    created_at: int
    updated_at: int
    deleted_at: Optional[int] = None
    kind: FolderKind = "general"
    active: bool = True
    # client-supplied accepted but server-bump on note write is authoritative —
    # see push() for the MAX() merge that prevents going backwards.
    last_activity_at: Optional[int] = None


class NoteIn(BaseModel):
    uuid: str
    folder_uuid: Optional[str] = None
    title: str = "Untitled"
    body: str = ""
    created_at: int
    updated_at: int
    deleted_at: Optional[int] = None
    status: Optional[NoteStatus] = None


class FolderOut(FolderIn):
    pass


class NoteOut(NoteIn):
    pass


class PushIn(BaseModel):
    folders: list[FolderIn] = Field(default_factory=list)
    notes: list[NoteIn] = Field(default_factory=list)


class PushOut(BaseModel):
    accepted_folders: list[str]  # uuids the server accepted (their incoming was newer)
    rejected_folders: list[str]  # uuids where server kept its newer version
    accepted_notes: list[str]
    rejected_notes: list[str]
    server_ts: int


class ChangesOut(BaseModel):
    folders: list[FolderOut]
    notes: list[NoteOut]
    server_ts: int  # caller stores this as their next `since`


app = FastAPI(title="noted-sync")
auth = Depends(require_token)


def _row_to_folder(r) -> dict:
    return {
        "uuid": r["uuid"], "name": r["name"], "color": r["color"],
        "position": r["position"], "created_at": r["created_at"],
        "updated_at": r["updated_at"], "deleted_at": r["deleted_at"],
        "kind": r["kind"], "active": bool(r["active"]),
        "last_activity_at": r["last_activity_at"],
    }


def _row_to_note(r) -> dict:
    keys = r.keys() if hasattr(r, "keys") else []
    out = {
        "uuid": r["uuid"], "folder_uuid": r["folder_uuid"],
        "title": r["title"], "body": r["body"],
        "created_at": r["created_at"], "updated_at": r["updated_at"],
        "deleted_at": r["deleted_at"], "status": r["status"],
    }
    # AI metadata — present on every row read post-migration. JSON columns
    # decoded to native arrays so iOS doesn't double-parse.
    for k in ("ai_title", "ai_summary", "ai_tldr", "ai_generated_at",
              "ai_model", "ai_status"):
        if k in keys:
            out[k] = r[k]
    for json_col in ("ai_tags", "ai_keypoints"):
        if json_col in keys and r[json_col]:
            try:
                out[json_col] = json.loads(r[json_col])
            except (ValueError, TypeError):
                out[json_col] = None
        elif json_col in keys:
            out[json_col] = None
    return out


@app.get("/health")
def health():
    return {"ok": True, "server_ts": now_ms()}


@app.get("/sync/changes", response_model=ChangesOut, dependencies=[auth])
def get_changes(since: int = Query(0, ge=0, description="ms since epoch; returns records with updated_at > since")):
    """Pull: returns every record (alive AND tombstoned) that changed strictly after `since`.
    Client stores the returned server_ts as their next `since` for incremental pulls.

    Why server_ts (not max(updated_at)): so we never miss a record that was being written
    on the server at the exact moment of the query — using server clock at query time
    plus '>' on the next call closes that gap."""
    server_ts = now_ms()
    with db() as c:
        folders = c.execute(
            "SELECT * FROM folders WHERE updated_at > ? ORDER BY updated_at", (since,),
        ).fetchall()
        notes = c.execute(
            "SELECT * FROM notes WHERE updated_at > ? ORDER BY updated_at", (since,),
        ).fetchall()
    return {
        "folders": [_row_to_folder(r) for r in folders],
        "notes": [_row_to_note(r) for r in notes],
        "server_ts": server_ts,
    }


@app.post("/sync/push", response_model=PushOut, dependencies=[auth])
def push(payload: PushIn):
    """Push: client uploads a batch of local changes. Per record, last-write-wins by
    `updated_at`. On tie, server keeps its version (deterministic). Returns which UUIDs
    were accepted vs rejected so the client knows what to refresh.

    Side effect on accepted notes: bumps the parent folder's last_activity_at AND
    updated_at via MAX() so the new value never goes backwards. On a folder move
    (old folder_uuid != new), bumps BOTH old and new folder. updated_at gets bumped
    too because that's what /sync/changes uses as the cursor — without it iOS would
    not pull the new last_activity_at."""
    accepted_f: list[str] = []
    rejected_f: list[str] = []
    accepted_n: list[str] = []
    rejected_n: list[str] = []
    with db() as c:
        for f in payload.folders:
            existing = c.execute(
                "SELECT updated_at FROM folders WHERE uuid=?", (f.uuid,),
            ).fetchone()
            if existing and existing["updated_at"] >= f.updated_at:
                rejected_f.append(f.uuid)
                continue
            c.execute(
                """INSERT INTO folders (uuid,name,color,position,created_at,updated_at,deleted_at,kind,active,last_activity_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(uuid) DO UPDATE SET
                     name=excluded.name, color=excluded.color, position=excluded.position,
                     updated_at=excluded.updated_at, deleted_at=excluded.deleted_at,
                     kind=excluded.kind, active=excluded.active,
                     last_activity_at=MAX(COALESCE(folders.last_activity_at,0), COALESCE(excluded.last_activity_at,0))""",
                (f.uuid, f.name, f.color, f.position, f.created_at, f.updated_at, f.deleted_at,
                 f.kind, 1 if f.active else 0, f.last_activity_at),
            )
            accepted_f.append(f.uuid)
        for n in payload.notes:
            existing = c.execute(
                "SELECT updated_at, folder_uuid FROM notes WHERE uuid=?", (n.uuid,),
            ).fetchone()
            if existing and existing["updated_at"] >= n.updated_at:
                rejected_n.append(n.uuid)
                continue
            c.execute(
                """INSERT INTO notes (uuid,folder_uuid,title,body,created_at,updated_at,deleted_at,status)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(uuid) DO UPDATE SET
                     folder_uuid=excluded.folder_uuid, title=excluded.title, body=excluded.body,
                     updated_at=excluded.updated_at, deleted_at=excluded.deleted_at,
                     status=excluded.status""",
                (n.uuid, n.folder_uuid, n.title, n.body, n.created_at, n.updated_at, n.deleted_at, n.status),
            )
            accepted_n.append(n.uuid)
            # Bump parent folder(s). MAX() merge prevents going backwards if a
            # folder rename (with newer updated_at) raced ahead of this note write.
            old_fuuid = existing["folder_uuid"] if existing else None
            new_fuuid = n.folder_uuid
            touched: set[str] = set()
            if new_fuuid:
                touched.add(new_fuuid)
            if old_fuuid and old_fuuid != new_fuuid:
                touched.add(old_fuuid)
            for fuuid in touched:
                c.execute(
                    """UPDATE folders
                          SET last_activity_at = MAX(COALESCE(last_activity_at,0), ?),
                              updated_at       = MAX(updated_at, ?)
                        WHERE uuid = ?""",
                    (n.updated_at, n.updated_at, fuuid),
                )
            _mark_ai_pending(c, n.uuid)
    return {
        "accepted_folders": accepted_f, "rejected_folders": rejected_f,
        "accepted_notes": accepted_n, "rejected_notes": rejected_n,
        "server_ts": now_ms(),
    }


@app.get("/sync/state", dependencies=[auth])
def state():
    """Cheap status: server time + counts. Client can use server_ts to align its clock
    (or just use it as `since=0` on first ever sync to avoid pulling history)."""
    with db() as c:
        nf = c.execute("SELECT COUNT(*) FROM folders WHERE deleted_at IS NULL").fetchone()[0]
        nn = c.execute("SELECT COUNT(*) FROM notes WHERE deleted_at IS NULL").fetchone()[0]
        nd_f = c.execute("SELECT COUNT(*) FROM folders WHERE deleted_at IS NOT NULL").fetchone()[0]
        nd_n = c.execute("SELECT COUNT(*) FROM notes WHERE deleted_at IS NOT NULL").fetchone()[0]
    return {
        "server_ts": now_ms(),
        "folders": {"alive": nf, "tombstoned": nd_f},
        "notes": {"alive": nn, "tombstoned": nd_n},
        "db_bytes": DB_PATH.stat().st_size if DB_PATH.exists() else 0,
    }


# --- Attachments — binary blobs (images, etc) referenced from notes ---
# Auth model: upload + meta require the bearer token; raw is PUBLIC so markdown
# <img src="..."> tags render without an Authorization header. The UUID (32 hex
# chars) is the security: unguessable + the threat model is single-user-fleet.

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024  # 25 MB — nginx vhost has 20 MB cap; raise both if needed


@app.post("/attachments", dependencies=[auth])
async def upload_attachment(file: UploadFile = File(...)):
    import hashlib
    import uuid as _uuid
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(413, f"file too large ({len(data)} > {MAX_ATTACHMENT_BYTES})")
    sha = hashlib.sha256(data).hexdigest()
    fname = os.path.basename(file.filename or "attachment")
    fname = "".join(c if c.isprintable() and c not in '/\\' else "_" for c in fname)[:200] or "attachment"
    mime = file.content_type or "application/octet-stream"
    with db() as c:
        existing = c.execute("SELECT * FROM attachments WHERE sha256=?", (sha,)).fetchone()
        if existing:
            return {
                "uuid": existing["uuid"], "sha256": existing["sha256"],
                "filename": existing["filename"], "mime": existing["mime"],
                "size": existing["size"], "uploaded_at": existing["uploaded_at"],
                "deduped": True,
            }
        aid = _uuid.uuid4().hex
        c.execute(
            "INSERT INTO attachments (uuid,sha256,filename,mime,size,uploaded_at,data) VALUES (?,?,?,?,?,?,?)",
            (aid, sha, fname, mime, len(data), now_ms(), data),
        )
    return {"uuid": aid, "sha256": sha, "filename": fname, "mime": mime,
            "size": len(data), "uploaded_at": now_ms(), "deduped": False}


@app.get("/attachments/{aid}/meta", dependencies=[auth])
def attachment_meta(aid: str):
    with db() as c:
        r = c.execute(
            "SELECT uuid,sha256,filename,mime,size,uploaded_at FROM attachments WHERE uuid=?",
            (aid,),
        ).fetchone()
    if not r:
        raise HTTPException(404, "attachment not found")
    return dict(r)


@app.get("/attachments/{aid}/raw")
def attachment_raw(aid: str):
    """Public — no bearer required, so <img src> tags in markdown render directly."""
    with db() as c:
        r = c.execute("SELECT mime, filename, data FROM attachments WHERE uuid=?", (aid,)).fetchone()
    if not r:
        raise HTTPException(404, "attachment not found")
    return Response(
        content=r["data"], media_type=r["mime"],
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


# --- Read endpoints for agent-comms RAG / iOS-bridge consumers ---

@app.get("/sync/note/{nid}", dependencies=[auth])
def get_note(nid: str):
    """Single note read by uuid. Returns 404 on missing or tombstoned. Useful for
    agents that already know a uuid (e.g. from a prior search result) and don't
    want to fetch the full /sync/changes payload."""
    with db() as c:
        r = c.execute("SELECT * FROM notes WHERE uuid=? AND deleted_at IS NULL", (nid,)).fetchone()
    if not r:
        raise HTTPException(404, "note not found or deleted")
    return _row_to_note(r)


@app.get("/sync/folder/{fid}", dependencies=[auth])
def get_folder(fid: str):
    """Single folder read by uuid."""
    with db() as c:
        r = c.execute("SELECT * FROM folders WHERE uuid=? AND deleted_at IS NULL", (fid,)).fetchone()
    if not r:
        raise HTTPException(404, "folder not found or deleted")
    return _row_to_folder(r)


@app.get("/sync/folders", dependencies=[auth])
def list_folders(
    kind: Optional[FolderKind] = Query(None, description="filter by folder kind"),
    active: Optional[bool] = Query(None, description="filter by active flag (meaningful for kind=project)"),
    include_recent: int = Query(0, ge=0, le=20, description="if >0, include N most-recent notes per folder"),
):
    """All alive folders. Optional `kind` + `active` filters — e.g.
    /sync/folders?kind=project&active=true returns the iOS sidebar's project list.
    Pass include_recent=N to enrich each folder with its N most recently updated
    alive notes (title + status + updated_at, no body) — one round-trip dashboard."""
    q = "SELECT * FROM folders WHERE deleted_at IS NULL"
    args: list = []
    if kind is not None:
        q += " AND kind=?"
        args.append(kind)
    if active is not None:
        q += " AND active=?"
        args.append(1 if active else 0)
    q += " ORDER BY position, name"
    with db() as c:
        rows = c.execute(q, args).fetchall()
        folders = [_row_to_folder(r) for r in rows]
        if include_recent > 0:
            for f in folders:
                recent = c.execute(
                    """SELECT uuid, title, status, updated_at, created_at
                         FROM notes WHERE folder_uuid=? AND deleted_at IS NULL
                         ORDER BY updated_at DESC LIMIT ?""",
                    (f["uuid"], include_recent),
                ).fetchall()
                f["recent_notes"] = [dict(r) for r in recent]
    return folders


@app.get("/sync/notes", dependencies=[auth])
def list_notes(
    folder: Optional[str] = Query(None, description="folder uuid filter"),
    since: int = Query(0, ge=0, description="ms epoch; notes updated_at > since"),
    limit: int = Query(100, ge=1, le=500),
    body: bool = Query(False, description="include body in response (default: title+meta only)"),
    sort: SortKey = Query("updated_at", description="sort key — updated_at (recently edited) or created_at (recently captured)"),
):
    """List alive notes with optional folder filter + since cursor. Default skips
    body for index-style listings; pass body=true to inline content. Sort by
    updated_at (default — "recently edited") or created_at ("recently captured")."""
    q = "SELECT * FROM notes WHERE deleted_at IS NULL"
    args: list = []
    if folder is not None:
        q += " AND folder_uuid=?"
        args.append(folder)
    if since:
        q += " AND updated_at > ?"
        args.append(since)
    q += f" ORDER BY {sort} DESC LIMIT ?"
    args.append(limit)
    with db() as c:
        rows = c.execute(q, args).fetchall()
    out = []
    for r in rows:
        d = _row_to_note(r)
        if not body:
            d.pop("body", None)
        out.append(d)
    return out


# --- Substring search across alive notes ---

@app.get("/sync/search", dependencies=[auth])
def search(
    q: str = Query(..., min_length=1, max_length=200),
    folder: Optional[str] = Query(None, description="restrict to one folder uuid"),
    status: Optional[str] = Query(None, description="comma-separated status filter, e.g. 'open,in-progress'"),
    limit: int = Query(20, ge=1, le=100),
    sort: SortKey = Query("updated_at"),
):
    """Substring search across alive notes (title + body, case-insensitive).
    Returns matches with a 200-char snippet around the first hit. Optional
    folder restriction + multi-status filter ('open,in-progress'). Sort by
    updated_at (default) or created_at."""
    needle = q.lower()
    sql = ("""SELECT n.uuid, n.title, n.body, n.status, n.updated_at, n.created_at,
                     f.name AS folder_name, n.folder_uuid
              FROM notes n LEFT JOIN folders f ON f.uuid = n.folder_uuid
              WHERE n.deleted_at IS NULL
                AND (LOWER(n.title) LIKE ? OR LOWER(n.body) LIKE ?)""")
    args: list = [f"%{needle}%", f"%{needle}%"]
    if folder is not None:
        sql += " AND n.folder_uuid=?"
        args.append(folder)
    if status:
        wanted = [s.strip() for s in status.split(",") if s.strip()]
        if wanted:
            sql += " AND n.status IN (" + ",".join("?" * len(wanted)) + ")"
            args.extend(wanted)
    sql += f" ORDER BY n.{sort} DESC LIMIT ?"
    args.append(limit)
    with db() as c:
        rows = c.execute(sql, args).fetchall()
    out = []
    for r in rows:
        body = r["body"] or ""
        idx = body.lower().find(needle)
        if idx == -1:
            snippet = body[:200]
        else:
            start = max(0, idx - 80)
            snippet = ("…" if start > 0 else "") + body[start:start + 200]
        out.append({
            "uuid": r["uuid"], "title": r["title"], "status": r["status"],
            "folder": r["folder_name"], "folder_uuid": r["folder_uuid"],
            "updated_at": r["updated_at"], "created_at": r["created_at"],
            "snippet": snippet,
        })
    return {"q": q, "matches": out}


# --- Helpers shared by task-oriented endpoints ---

def _resolve_folder(c, name_or_uuid: str) -> dict:
    """Look up a folder by exact uuid or case-insensitive name. Raises 404 if
    not found. Used by /sync/project/{X} so agents can say 'open Focus' or
    'open the agent-comms project' without knowing the uuid."""
    # Try uuid first (cheap exact match)
    row = c.execute(
        "SELECT * FROM folders WHERE uuid=? AND deleted_at IS NULL",
        (name_or_uuid,),
    ).fetchone()
    if row:
        return _row_to_folder(row)
    # Fall back to case-insensitive name
    row = c.execute(
        "SELECT * FROM folders WHERE LOWER(name)=LOWER(?) AND deleted_at IS NULL",
        (name_or_uuid,),
    ).fetchone()
    if row:
        return _row_to_folder(row)
    raise HTTPException(404, f"folder not found by uuid or name: {name_or_uuid!r}")


def _note_summary(r) -> dict:
    """Compact note dict for list views — no body, includes everything an agent
    needs to decide whether to fetch the full body."""
    return {
        "uuid": r["uuid"], "title": r["title"], "status": r["status"],
        "folder_uuid": r["folder_uuid"],
        "created_at": r["created_at"], "updated_at": r["updated_at"],
    }


def _split_buckets(notes: list[dict]) -> dict:
    """Split a notes list into the 3 agent-friendly buckets used by
    /sync/project and /sync/issues."""
    issues = [n for n in notes if n["status"] in ACTIONABLE_STATUSES]
    ideas = [n for n in notes if n["status"] in PARKED_STATUSES]
    recent = [n for n in notes if n["status"] not in ACTIONABLE_STATUSES + PARKED_STATUSES]
    return {"issues": issues, "ideas": ideas, "recent": recent}


# --- Task-oriented read endpoints (agent ergonomics) ---

@app.get("/sync/project/{name_or_uuid}", dependencies=[auth])
def project_view(
    name_or_uuid: str,
    recent_limit: int = Query(20, ge=1, le=200, description="cap on recent[] (issues + ideas not capped)"),
    body: bool = Query(False, description="include full body inline for each note"),
):
    """Open a project (or any folder) in one call. Returns 3 buckets:
      - issues:  status in (open, in-progress, testing) — actionable
      - ideas:   status == 'idea'                       — parked for later
      - recent:  everything else (done + plain notes)    — capped by recent_limit

    All buckets sorted by updated_at DESC. Folder name lookup is case-
    insensitive. Pass body=true to inline note bodies (otherwise titles +
    meta only — agent fetches /sync/note/{uuid} for full text)."""
    with db() as c:
        folder = _resolve_folder(c, name_or_uuid)
        rows = c.execute(
            """SELECT * FROM notes WHERE folder_uuid=? AND deleted_at IS NULL
               ORDER BY updated_at DESC""",
            (folder["uuid"],),
        ).fetchall()
    all_notes = []
    for r in rows:
        n = _note_summary(r)
        if body:
            n["body"] = r["body"]
        all_notes.append(n)
    buckets = _split_buckets(all_notes)
    buckets["recent"] = buckets["recent"][:recent_limit]
    return {"folder": folder, **buckets}


@app.get("/sync/issues", dependencies=[auth])
def issues_view(
    project_only: bool = Query(True, description="if true, restrict to folders with kind='project'"),
    folder: Optional[str] = Query(None, description="restrict to a single folder uuid"),
):
    """Cross-folder issue tracker: all actionable + parked notes grouped by
    folder. Default scope is just kind='project' folders. Set project_only=false
    to include general folders too (e.g. open issues in 'Main')."""
    with db() as c:
        fq = "SELECT * FROM folders WHERE deleted_at IS NULL"
        fa: list = []
        if project_only:
            fq += " AND kind='project'"
        if folder is not None:
            fq += " AND uuid=?"
            fa.append(folder)
        fq += " ORDER BY last_activity_at DESC NULLS LAST, position, name"
        folders = c.execute(fq, fa).fetchall()
        groups = []
        total_issues = total_ideas = 0
        wanted_statuses = ACTIONABLE_STATUSES + PARKED_STATUSES
        for f in folders:
            placeholders = ",".join("?" * len(wanted_statuses))
            rows = c.execute(
                f"""SELECT * FROM notes WHERE folder_uuid=? AND deleted_at IS NULL
                    AND status IN ({placeholders})
                    ORDER BY updated_at DESC""",
                (f["uuid"], *wanted_statuses),
            ).fetchall()
            notes = [_note_summary(r) for r in rows]
            buckets = _split_buckets(notes)
            if not buckets["issues"] and not buckets["ideas"]:
                continue  # skip folders with no actionable work
            total_issues += len(buckets["issues"])
            total_ideas += len(buckets["ideas"])
            groups.append({
                "folder": _row_to_folder(f),
                "issues": buckets["issues"],
                "ideas": buckets["ideas"],
            })
    return {
        "total_issues": total_issues,
        "total_ideas": total_ideas,
        "groups": groups,
    }


@app.get("/sync/recent", dependencies=[auth])
def recent_view(
    limit: int = Query(20, ge=1, le=100),
    sort: SortKey = Query("updated_at", description="updated_at = recently edited; created_at = recently captured"),
    body: bool = Query(False),
):
    """Most-recent N notes across ALL alive folders. Each note includes its
    folder name + status. For 'find my latest note about X', combine with
    /sync/search?q=X&sort=created_at instead."""
    with db() as c:
        rows = c.execute(
            f"""SELECT n.*, f.name AS folder_name
                FROM notes n LEFT JOIN folders f ON f.uuid = n.folder_uuid
                WHERE n.deleted_at IS NULL
                ORDER BY n.{sort} DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        n = _note_summary(r)
        n["folder"] = r["folder_name"]
        if body:
            n["body"] = r["body"]
        out.append(n)
    return {"sort": sort, "limit": limit, "notes": out}


# --- Single-call write endpoints (agent ergonomics) ---
# These replace the read-modify-push dance that /sync/push requires. Server
# does the bookkeeping atomically: bump updated_at, bump parent folder's
# last_activity_at + updated_at, return the updated note.

def _bump_parent_folder(c, folder_uuid: Optional[str], ts: int):
    """Bump parent folder's last_activity_at + updated_at to ts via MAX merge
    (never goes backwards). No-op for NULL folder_uuid. Same logic as /sync/push."""
    if folder_uuid:
        c.execute(
            """UPDATE folders SET
                 last_activity_at = MAX(COALESCE(last_activity_at,0), ?),
                 updated_at       = MAX(updated_at, ?)
               WHERE uuid = ?""",
            (ts, ts, folder_uuid),
        )


def _fetch_note(c, nid: str) -> sqlite3.Row:
    row = c.execute(
        "SELECT * FROM notes WHERE uuid=? AND deleted_at IS NULL", (nid,),
    ).fetchone()
    if not row:
        raise HTTPException(404, f"note not found or deleted: {nid}")
    return row


class NoteCreateIn(BaseModel):
    folder: Optional[str] = None  # name OR uuid, or null for uncategorized
    title: str = "Untitled"
    body: str = ""
    status: Optional[NoteStatus] = None


@app.post("/sync/note", dependencies=[auth])
def create_note(payload: NoteCreateIn):
    """Create a new note. Server generates uuid + timestamps and bumps parent
    folder. `folder` accepts a name (case-insensitive) or uuid, or null for
    uncategorized. Returns the full created note."""
    import uuid as _uuid
    ts = now_ms()
    with db() as c:
        folder_uuid = None
        if payload.folder is not None:
            folder = _resolve_folder(c, payload.folder)
            folder_uuid = folder["uuid"]
        nid = _uuid.uuid4().hex
        c.execute(
            """INSERT INTO notes (uuid,folder_uuid,title,body,created_at,updated_at,deleted_at,status)
               VALUES (?,?,?,?,?,?,NULL,?)""",
            (nid, folder_uuid, payload.title, payload.body, ts, ts, payload.status),
        )
        _bump_parent_folder(c, folder_uuid, ts)
        _mark_ai_pending(c, nid)
        row = c.execute("SELECT * FROM notes WHERE uuid=?", (nid,)).fetchone()
    return _row_to_note(row)


class StatusIn(BaseModel):
    status: Optional[NoteStatus]  # required key; null clears status (plain note)


@app.post("/sync/note/{nid}/status", dependencies=[auth])
def set_status(nid: str, payload: StatusIn):
    """Change a note's status in one call. Pass status=null to clear (revert
    to plain note). Server bumps updated_at + parent folder atomically."""
    ts = now_ms()
    with db() as c:
        existing = _fetch_note(c, nid)
        c.execute(
            "UPDATE notes SET status=?, updated_at=? WHERE uuid=?",
            (payload.status, ts, nid),
        )
        _bump_parent_folder(c, existing["folder_uuid"], ts)
        _mark_ai_pending(c, nid)
        row = c.execute("SELECT * FROM notes WHERE uuid=?", (nid,)).fetchone()
    return _row_to_note(row)


class AppendIn(BaseModel):
    text: str


@app.post("/sync/note/{nid}/append", dependencies=[auth])
def append_to_note(nid: str, payload: AppendIn):
    """Append text to a note body. Server inserts a newline separator if the
    existing body is non-empty and doesn't already end in one. Atomic — no
    read-modify-write needed by the caller."""
    ts = now_ms()
    with db() as c:
        existing = _fetch_note(c, nid)
        body = existing["body"] or ""
        sep = "" if (not body or body.endswith("\n")) else "\n"
        new_body = body + sep + payload.text
        c.execute(
            "UPDATE notes SET body=?, updated_at=? WHERE uuid=?",
            (new_body, ts, nid),
        )
        _bump_parent_folder(c, existing["folder_uuid"], ts)
        _mark_ai_pending(c, nid)
        row = c.execute("SELECT * FROM notes WHERE uuid=?", (nid,)).fetchone()
    return _row_to_note(row)


class ReplaceIn(BaseModel):
    find: str
    replace: str


@app.post("/sync/note/{nid}/replace", dependencies=[auth])
def replace_in_note(nid: str, payload: ReplaceIn):
    """Find/replace in a note body via 3-layer fuzzy matching (exact →
    whitespace-normalized → difflib fuzzy at 0.8 threshold).

    On match: 200 with the updated note + `match_type` + `similarity`.
    On miss:  422 with `candidates` (top-3 close blocks) so the agent can
              self-correct its `find` string and retry."""
    ts = now_ms()
    with db() as c:
        existing = _fetch_note(c, nid)
        body = existing["body"] or ""
        result = _fuzzy_str_replace(body, payload.find, payload.replace)
        if not result.success:
            raise HTTPException(422, detail={
                "error": "no match",
                "message": result.message,
                "best_similarity": result.similarity,
                "candidates": result.candidates or [],
            })
        c.execute(
            "UPDATE notes SET body=?, updated_at=? WHERE uuid=?",
            (result.new_content, ts, nid),
        )
        _bump_parent_folder(c, existing["folder_uuid"], ts)
        _mark_ai_pending(c, nid)
        row = c.execute("SELECT * FROM notes WHERE uuid=?", (nid,)).fetchone()
    out = _row_to_note(row)
    out["match_type"] = result.match_type
    out["similarity"] = result.similarity
    return out


@app.delete("/sync/note/{nid}", dependencies=[auth])
def delete_note(nid: str):
    """Tombstone a note (sets deleted_at). The row stays in the DB so the
    delete syncs to other devices via /sync/changes. Parent folder bumps too."""
    ts = now_ms()
    with db() as c:
        existing = _fetch_note(c, nid)
        c.execute(
            "UPDATE notes SET deleted_at=?, updated_at=? WHERE uuid=?",
            (ts, ts, nid),
        )
        _bump_parent_folder(c, existing["folder_uuid"], ts)
    return {"ok": True, "uuid": nid, "deleted_at": ts}


# --- Git integration endpoints ---
# Layered model: PAT lives encrypted in git_credentials. folder_git_link
# attaches a folder→repo. Tree + file caches absorb GitHub round-trips so
# the agent-facing endpoints stay fast and don't burn rate limit on
# repeated reads. ETag conditional GET keeps refresh cheap.

TREE_TTL_MS = 5 * 60 * 1000          # under this age: serve cache, skip GitHub round-trip
FILE_TTL_MS = 5 * 60 * 1000          # under this age: serve cache, no GH refresh
FILE_CACHE_MAX_BYTES = 100 * 1024 * 1024   # 100 MB total file-cache budget; LRU evicts when exceeded


def _get_link(c, folder_uuid: str) -> sqlite3.Row:
    row = c.execute(
        "SELECT * FROM folder_git_link WHERE folder_uuid=?", (folder_uuid,),
    ).fetchone()
    if not row:
        raise HTTPException(404, f"folder not linked to a repo: {folder_uuid}")
    return row


def _get_credential_token(c, credential_id: int) -> tuple[sqlite3.Row, str]:
    row = c.execute(
        "SELECT * FROM git_credentials WHERE id=?", (credential_id,),
    ).fetchone()
    if not row:
        raise HTTPException(500, f"credential {credential_id} missing — link is orphaned")
    return row, _crypto.decrypt(row["encrypted_token"])


def _evict_file_cache_if_over_budget(c):
    """LRU eviction by accessed_at when total cached bytes exceeds budget."""
    total = c.execute("SELECT COALESCE(SUM(size),0) FROM git_file_cache").fetchone()[0]
    if total <= FILE_CACHE_MAX_BYTES:
        return
    # Evict oldest-accessed first until under budget
    over = total - FILE_CACHE_MAX_BYTES
    rows = c.execute(
        "SELECT folder_uuid, branch, path, size FROM git_file_cache ORDER BY accessed_at ASC",
    ).fetchall()
    freed = 0
    for r in rows:
        if freed >= over:
            break
        c.execute(
            "DELETE FROM git_file_cache WHERE folder_uuid=? AND branch=? AND path=?",
            (r["folder_uuid"], r["branch"], r["path"]),
        )
        freed += r["size"]


class CredentialIn(BaseModel):
    provider: Literal["github", "gitlab", "github_enterprise"] = "github"
    account_label: str = "personal"
    token: str
    base_url: str = "https://api.github.com"
    expires_at: Optional[int] = None


@app.post("/git/credentials", dependencies=[auth])
def save_credential(payload: CredentialIn):
    """Save a PAT (encrypted at rest via Fernet). Returns the id and a
    redacted preview — never the plaintext token. Pasting the same token
    twice creates a second row (no dedup) since labels can differ."""
    ts = now_ms()
    enc = _crypto.encrypt(payload.token)
    with db() as c:
        cur = c.execute(
            """INSERT INTO git_credentials
                 (provider, account_label, encrypted_token, key_version,
                  base_url, expires_at, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (payload.provider, payload.account_label, enc, _crypto.current_key_version(),
             payload.base_url, payload.expires_at, ts, ts),
        )
        cid = cur.lastrowid
    return {
        "id": cid, "provider": payload.provider,
        "account_label": payload.account_label,
        "base_url": payload.base_url,
        "token_preview": payload.token[:10] + "…" + payload.token[-4:],
        "created_at": ts,
    }


class GitLinkIn(BaseModel):
    credential_id: int
    owner: str
    repo: str
    default_branch: Optional[str] = None  # if None, server queries GitHub for the repo's default


@app.post("/folders/{folder_uuid}/git-link", dependencies=[auth])
def attach_git_link(folder_uuid: str, payload: GitLinkIn):
    """Attach a folder to a repo. Idempotent — re-POSTing updates the link.
    If default_branch is omitted, server queries GitHub once to resolve it."""
    ts = now_ms()
    with db() as c:
        # Verify folder exists + is alive
        folder = _resolve_folder(c, folder_uuid)
        cred, token = _get_credential_token(c, payload.credential_id)
        branch = payload.default_branch
        if not branch:
            branch = _gh.resolve_default_branch(payload.owner, payload.repo, token, cred["base_url"])
            if not branch:
                raise HTTPException(404, f"could not resolve default branch for {payload.owner}/{payload.repo} — check PAT scope")
        existing = c.execute(
            "SELECT folder_uuid FROM folder_git_link WHERE folder_uuid=?", (folder["uuid"],),
        ).fetchone()
        if existing:
            c.execute(
                """UPDATE folder_git_link SET
                     credential_id=?, owner=?, repo=?, default_branch=?, updated_at=?
                   WHERE folder_uuid=?""",
                (payload.credential_id, payload.owner, payload.repo, branch, ts, folder["uuid"]),
            )
        else:
            c.execute(
                """INSERT INTO folder_git_link
                     (folder_uuid, credential_id, owner, repo, default_branch, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (folder["uuid"], payload.credential_id, payload.owner, payload.repo, branch, ts, ts),
            )
        link = c.execute(
            "SELECT * FROM folder_git_link WHERE folder_uuid=?", (folder["uuid"],),
        ).fetchone()
    return dict(link)


@app.get("/git/folders/{folder_uuid}/link", dependencies=[auth])
def get_git_link(folder_uuid: str):
    """Read the folder→repo link metadata. Never exposes the PAT — just the
    credential_id and repo coordinates. Useful when an agent wants to know
    'what repo does this project map to' before fetching files."""
    with db() as c:
        link = _get_link(c, folder_uuid)
        cred = c.execute(
            "SELECT provider, account_label, base_url FROM git_credentials WHERE id=?",
            (link["credential_id"],),
        ).fetchone()
    return {**dict(link), "credential": dict(cred) if cred else None}


@app.get("/git/folders/{folder_uuid}/tree", dependencies=[auth])
def get_tree(folder_uuid: str, branch: Optional[str] = None,
             prefix: str = Query("", description="path-prefix filter (server-side)"),
             force: bool = Query(False, description="skip TTL, force GitHub round-trip")):
    """List tree entries for a linked folder. Defaults to default_branch.
    Cache strategy:
      - cache age < 5min and not force → serve cache, no GH call
      - cache age ≥ 5min OR force      → conditional GET (If-None-Match)
        - 304 → serve cache, bump fetched_at
        - 200 → replace cache, store new etag, serve fresh
      - cache miss → unconditional fetch, store, serve

    prefix='backend/' filters to a subtree without re-fetching."""
    ts = now_ms()
    with db() as c:
        link = _get_link(c, folder_uuid)
        b = branch or link["default_branch"]
        meta = c.execute(
            "SELECT * FROM git_tree_meta WHERE folder_uuid=? AND branch=?",
            (folder_uuid, b),
        ).fetchone()
        need_fetch = force or not meta or (ts - meta["fetched_at"]) >= TREE_TTL_MS
        if need_fetch:
            cred, token = _get_credential_token(c, link["credential_id"])
            status, new_etag, data = _gh.get_tree(
                link["owner"], link["repo"], b, token,
                prev_etag=(meta["etag"] if meta else None),
                base_url=cred["base_url"],
            )
            if status == 200:
                # Replace cache atomically
                c.execute("DELETE FROM git_tree_cache WHERE folder_uuid=? AND branch=?",
                          (folder_uuid, b))
                rows_in = [
                    (folder_uuid, b, e["path"], e["sha"], e.get("size"), e["type"])
                    for e in data["tree"]
                ]
                c.executemany(
                    "INSERT INTO git_tree_cache (folder_uuid,branch,path,sha,size,type) VALUES (?,?,?,?,?,?)",
                    rows_in,
                )
                c.execute(
                    """INSERT INTO git_tree_meta (folder_uuid,branch,etag,truncated,fetched_at)
                       VALUES (?,?,?,?,?)
                       ON CONFLICT(folder_uuid,branch) DO UPDATE SET
                         etag=excluded.etag, truncated=excluded.truncated,
                         fetched_at=excluded.fetched_at""",
                    (folder_uuid, b, new_etag, 1 if data.get("truncated") else 0, ts),
                )
            elif status == 304:
                c.execute("UPDATE git_tree_meta SET fetched_at=? WHERE folder_uuid=? AND branch=?",
                          (ts, folder_uuid, b))
            elif status in (401, 403):
                raise HTTPException(status, f"GitHub auth failed for {link['owner']}/{link['repo']} — check PAT scope")
            elif status == 404:
                raise HTTPException(404, f"repo or branch not found: {link['owner']}/{link['repo']}@{b}")
            else:
                raise HTTPException(502, f"GitHub returned {status}: {data}")
        # Now read cache (always — even on fetch path, cache was just refreshed)
        q = "SELECT path, sha, size, type FROM git_tree_cache WHERE folder_uuid=? AND branch=?"
        a: list = [folder_uuid, b]
        if prefix:
            q += " AND path LIKE ?"
            a.append(prefix + "%")
        q += " ORDER BY path"
        rows = c.execute(q, a).fetchall()
        meta_now = c.execute(
            "SELECT etag, truncated, fetched_at FROM git_tree_meta WHERE folder_uuid=? AND branch=?",
            (folder_uuid, b),
        ).fetchone()
    return {
        "owner": link["owner"], "repo": link["repo"], "branch": b,
        "prefix": prefix,
        "fetched_at": meta_now["fetched_at"] if meta_now else None,
        "etag": meta_now["etag"] if meta_now else None,
        "truncated": bool(meta_now["truncated"]) if meta_now else False,
        "entries": [dict(r) for r in rows],
    }


@app.get("/git/folders/{folder_uuid}/file", dependencies=[auth])
def get_file(folder_uuid: str, path: str = Query(..., min_length=1),
             branch: Optional[str] = None,
             force: bool = Query(False)):
    """Fetch a single file's contents. Cache + ETag identical to /tree.
    Response inlines content as UTF-8 string if decodable, otherwise base64."""
    ts = now_ms()
    with db() as c:
        link = _get_link(c, folder_uuid)
        b = branch or link["default_branch"]
        cached = c.execute(
            "SELECT * FROM git_file_cache WHERE folder_uuid=? AND branch=? AND path=?",
            (folder_uuid, b, path),
        ).fetchone()
        need_fetch = force or not cached or (ts - cached["fetched_at"]) >= FILE_TTL_MS
        if need_fetch:
            cred, token = _get_credential_token(c, link["credential_id"])
            status, new_etag, data = _gh.get_file(
                link["owner"], link["repo"], b, path, token,
                prev_etag=(cached["etag"] if cached else None),
                base_url=cred["base_url"],
            )
            if status == 200:
                content_bytes = _gh.decode_content_base64(data)
                c.execute(
                    """INSERT INTO git_file_cache
                         (folder_uuid,branch,path,sha,size,content,etag,fetched_at,accessed_at)
                       VALUES (?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(folder_uuid,branch,path) DO UPDATE SET
                         sha=excluded.sha, size=excluded.size, content=excluded.content,
                         etag=excluded.etag, fetched_at=excluded.fetched_at,
                         accessed_at=excluded.accessed_at""",
                    (folder_uuid, b, path, data["sha"], len(content_bytes), content_bytes,
                     new_etag, ts, ts),
                )
                _evict_file_cache_if_over_budget(c)
                cached = c.execute(
                    "SELECT * FROM git_file_cache WHERE folder_uuid=? AND branch=? AND path=?",
                    (folder_uuid, b, path),
                ).fetchone()
            elif status == 304:
                c.execute(
                    "UPDATE git_file_cache SET fetched_at=?, accessed_at=? WHERE folder_uuid=? AND branch=? AND path=?",
                    (ts, ts, folder_uuid, b, path),
                )
            elif status in (401, 403):
                raise HTTPException(status, "GitHub auth failed — check PAT scope")
            elif status == 404:
                raise HTTPException(404, f"file not found: {path}@{b}")
            else:
                raise HTTPException(502, f"GitHub returned {status}: {data}")
        else:
            # Cache hit, just bump accessed_at for LRU
            c.execute(
                "UPDATE git_file_cache SET accessed_at=? WHERE folder_uuid=? AND branch=? AND path=?",
                (ts, folder_uuid, b, path),
            )
    # Decide encoding for transport
    raw: bytes = cached["content"]
    try:
        body = raw.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        body = base64.b64encode(raw).decode("ascii")
        encoding = "base64"
    return {
        "path": path, "branch": b,
        "sha": cached["sha"], "size": cached["size"],
        "encoding": encoding, "content": body,
        "fetched_at": cached["fetched_at"],
        "from_cache": not need_fetch,
    }


REPO_LIST_TTL_MS = 5 * 60 * 1000
BRANCH_LIST_TTL_MS = 5 * 60 * 1000


@app.get("/git/credentials/{cid}/repos", dependencies=[auth])
def list_repos(cid: int,
               sort: Literal["pushed_desc", "name", "updated_desc"] = Query("pushed_desc"),
               limit: int = Query(50, ge=1, le=100),
               force: bool = Query(False)):
    """List repos accessible to a credential's PAT. Default sort = pushed_at
    desc so the operator's currently-active repo floats to the top of the
    picker. Cached for 5min with ETag conditional GET — picker hits are
    typically warm after the first."""
    ts = now_ms()
    with db() as c:
        cred_row = c.execute("SELECT * FROM git_credentials WHERE id=?", (cid,)).fetchone()
        if not cred_row:
            raise HTTPException(404, f"credential {cid} not found")
        token = _crypto.decrypt(cred_row["encrypted_token"])
        meta = c.execute("SELECT * FROM git_repo_cache_meta WHERE credential_id=?", (cid,)).fetchone()
        need_fetch = force or not meta or (ts - meta["fetched_at"]) >= REPO_LIST_TTL_MS
        if need_fetch:
            # GitHub's sort=pushed maps directly to our pushed_desc default
            gh_sort = "pushed" if sort == "pushed_desc" else ("updated" if sort == "updated_desc" else "full_name")
            status, new_etag, data = _gh.list_user_repos(
                token, prev_etag=(meta["etag"] if meta else None),
                sort=gh_sort, limit=limit, base_url=cred_row["base_url"],
            )
            if status == 200:
                c.execute("DELETE FROM git_repo_cache WHERE credential_id=?", (cid,))
                rows_in = [
                    (cid, r["owner"]["login"], r["name"], r.get("default_branch"),
                     r.get("pushed_at"), 1 if r.get("private") else 0)
                    for r in data
                ]
                c.executemany(
                    "INSERT INTO git_repo_cache (credential_id,owner,repo,default_branch,pushed_at,private) VALUES (?,?,?,?,?,?)",
                    rows_in,
                )
                c.execute(
                    """INSERT INTO git_repo_cache_meta (credential_id, etag, fetched_at)
                       VALUES (?,?,?)
                       ON CONFLICT(credential_id) DO UPDATE SET etag=excluded.etag, fetched_at=excluded.fetched_at""",
                    (cid, new_etag, ts),
                )
            elif status == 304:
                c.execute("UPDATE git_repo_cache_meta SET fetched_at=? WHERE credential_id=?", (ts, cid))
            elif status in (401, 403):
                raise HTTPException(status, "GitHub auth failed — check PAT scope")
            else:
                raise HTTPException(502, f"GitHub returned {status}: {data}")
        # Read cache + sort
        order_clause = {
            "pushed_desc":  "pushed_at DESC",
            "updated_desc": "pushed_at DESC",  # GitHub-side approximation
            "name":         "owner, repo",
        }[sort]
        rows = c.execute(
            f"SELECT * FROM git_repo_cache WHERE credential_id=? ORDER BY {order_clause} LIMIT ?",
            (cid, limit),
        ).fetchall()
        meta_now = c.execute("SELECT * FROM git_repo_cache_meta WHERE credential_id=?", (cid,)).fetchone()
    return {
        "credential_id": cid,
        "sort": sort,
        "fetched_at": meta_now["fetched_at"] if meta_now else None,
        "repos": [
            {
                "owner": r["owner"], "repo": r["repo"],
                "default_branch": r["default_branch"],
                "pushed_at": r["pushed_at"],
                "private": bool(r["private"]),
            }
            for r in rows
        ],
    }


@app.get("/git/folders/{folder_uuid}/branches", dependencies=[auth])
def list_branches_endpoint(folder_uuid: str, force: bool = Query(False)):
    """List branches of the folder's linked repo, sorted by last commit date
    desc ('alive' branches first). Server enriches each branch with a single
    /commits call to get committer date — N+1 to GitHub, but cached 5min so
    the hot path is one local SELECT. Up to ~30 branches per repo handled
    inline; larger repos would need pagination (not yet)."""
    ts = now_ms()
    with db() as c:
        link = _get_link(c, folder_uuid)
        meta = c.execute("SELECT * FROM git_branch_cache_meta WHERE folder_uuid=?", (folder_uuid,)).fetchone()
        need_fetch = force or not meta or (ts - meta["fetched_at"]) >= BRANCH_LIST_TTL_MS
        if need_fetch:
            cred, token = _get_credential_token(c, link["credential_id"])
            status, new_etag, data = _gh.list_branches(
                link["owner"], link["repo"], token,
                prev_etag=(meta["etag"] if meta else None),
                base_url=cred["base_url"],
            )
            if status == 200:
                c.execute("DELETE FROM git_branch_cache WHERE folder_uuid=?", (folder_uuid,))
                rows_in = []
                for b in data:
                    name = b["name"]
                    sha = b["commit"]["sha"]
                    # Enrichment: per-branch /commits call for last_commit_at
                    last_commit_at = _gh.get_latest_commit_for_branch(
                        link["owner"], link["repo"], name, token, base_url=cred["base_url"],
                    )
                    rows_in.append((folder_uuid, name, sha, last_commit_at))
                c.executemany(
                    "INSERT INTO git_branch_cache (folder_uuid,name,sha,last_commit_at) VALUES (?,?,?,?)",
                    rows_in,
                )
                c.execute(
                    """INSERT INTO git_branch_cache_meta (folder_uuid, etag, fetched_at)
                       VALUES (?,?,?)
                       ON CONFLICT(folder_uuid) DO UPDATE SET etag=excluded.etag, fetched_at=excluded.fetched_at""",
                    (folder_uuid, new_etag, ts),
                )
            elif status == 304:
                c.execute("UPDATE git_branch_cache_meta SET fetched_at=? WHERE folder_uuid=?", (ts, folder_uuid))
            elif status in (401, 403):
                raise HTTPException(status, "GitHub auth failed — check PAT scope")
            elif status == 404:
                raise HTTPException(404, f"repo not found: {link['owner']}/{link['repo']}")
            else:
                raise HTTPException(502, f"GitHub returned {status}: {data}")
        rows = c.execute(
            """SELECT name, sha, last_commit_at FROM git_branch_cache
               WHERE folder_uuid=? ORDER BY last_commit_at DESC NULLS LAST, name""",
            (folder_uuid,),
        ).fetchall()
        meta_now = c.execute("SELECT * FROM git_branch_cache_meta WHERE folder_uuid=?", (folder_uuid,)).fetchone()
    return {
        "owner": link["owner"], "repo": link["repo"],
        "default_branch": link["default_branch"],
        "fetched_at": meta_now["fetched_at"] if meta_now else None,
        "branches": [
            {"name": r["name"], "sha": r["sha"], "last_commit_at": r["last_commit_at"]}
            for r in rows
        ],
    }


@app.get("/git/folders/{folder_uuid}/search", dependencies=[auth])
def search_tree(folder_uuid: str, q: str = Query(..., min_length=1, max_length=200),
                branch: Optional[str] = None,
                limit: int = Query(50, ge=1, le=500)):
    """Filename substring search against the local tree cache. Sub-50ms
    typical for trees under 10k entries. Requires the tree to have been
    fetched at least once (hit /tree first if cold)."""
    with db() as c:
        link = _get_link(c, folder_uuid)
        b = branch or link["default_branch"]
        rows = c.execute(
            """SELECT path, sha, size, type FROM git_tree_cache
                WHERE folder_uuid=? AND branch=? AND LOWER(path) LIKE ?
                ORDER BY path LIMIT ?""",
            (folder_uuid, b, f"%{q.lower()}%", limit),
        ).fetchall()
    return {
        "q": q, "branch": b,
        "matches": [dict(r) for r in rows],
    }


# --- AI metadata: trigger + background worker ---
# Trigger: any write endpoint calls _mark_ai_pending(uuid). Sets ai_status=
# 'pending' unless the input_hash matches the existing hash (no-op write).
# Worker: a single daemon thread polls for pending rows every AI_POLL_SECONDS
# and processes up to AI_BATCH_SIZE per pass. Retries failed rows with
# exponential backoff capped at AI_MAX_BACKOFF_SECONDS.

AI_POLL_SECONDS = float(os.environ.get("AI_POLL_SECONDS", "1.5"))
AI_BATCH_SIZE = int(os.environ.get("AI_BATCH_SIZE", "3"))
AI_MAX_ATTEMPTS = int(os.environ.get("AI_MAX_ATTEMPTS", "5"))
AI_MAX_BACKOFF_SECONDS = int(os.environ.get("AI_MAX_BACKOFF_SECONDS", "300"))


def _mark_ai_pending(c, note_uuid: str):
    """Hook called by every write endpoint after the note row is updated.
    Recomputes input_hash from current note + folder state. If unchanged
    (e.g. status-only update with no body change), skips. If body is too
    short, marks 'skipped' to avoid wasting tokens on stubs. Otherwise
    marks 'pending' and resets attempts."""
    r = c.execute(
        """SELECT n.uuid, n.title, n.body, n.status, n.ai_input_hash, n.deleted_at,
                  f.name AS folder_name
             FROM notes n LEFT JOIN folders f ON f.uuid = n.folder_uuid
            WHERE n.uuid=?""",
        (note_uuid,),
    ).fetchone()
    if not r or r["deleted_at"]:
        return
    new_hash = _ai.compute_input_hash(r["body"] or "", r["title"], r["folder_name"], r["status"])
    if r["ai_input_hash"] == new_hash:
        return  # no-op write; don't re-burn tokens
    if len((r["body"] or "").strip()) < _ai.MIN_BODY_CHARS_FOR_AI:
        # Stub note — clear any previous AI metadata, mark skipped
        c.execute(
            """UPDATE notes SET ai_status='skipped', ai_input_hash=?,
                                ai_title=NULL, ai_tags=NULL, ai_summary=NULL,
                                ai_tldr=NULL, ai_keypoints=NULL, ai_attempts=0,
                                ai_error=NULL
                          WHERE uuid=?""",
            (new_hash, note_uuid),
        )
        return
    c.execute(
        "UPDATE notes SET ai_status='pending', ai_input_hash=?, ai_attempts=0, ai_error=NULL WHERE uuid=?",
        (new_hash, note_uuid),
    )


def _ai_worker_pass():
    """One scan iteration. Picks AI_BATCH_SIZE oldest pending rows,
    streams each through OpenRouter. Title + summary land first (progressive
    reveal — drives iOS's reactive notes list and swipe-left panel).
    TLDR/keypoints/tags land in one shot at stream end.

    Status transitions per note: pending → streaming → ok (or → failed on
    error). Each transition bumps updated_at so iOS /sync/changes pulls
    surface the new fields as they arrive."""
    with db() as c:
        rows = c.execute(
            """SELECT n.uuid, n.title, n.body, n.status, n.folder_uuid, n.ai_attempts,
                      f.name AS folder_name, f.kind AS folder_kind
                 FROM notes n LEFT JOIN folders f ON f.uuid = n.folder_uuid
                WHERE n.ai_status='pending' AND n.deleted_at IS NULL
                  AND n.ai_attempts < ?
             ORDER BY n.updated_at ASC LIMIT ?""",
            (AI_MAX_ATTEMPTS, AI_BATCH_SIZE),
        ).fetchall()
    for r in rows:
        nid = r["uuid"]
        # Sibling titles for context (richer titles)
        sib_titles: list[str] = []
        if r["folder_uuid"]:
            with db() as c2:
                sibs = c2.execute(
                    """SELECT title FROM notes
                        WHERE folder_uuid=? AND deleted_at IS NULL AND uuid<>?
                        ORDER BY updated_at DESC LIMIT ?""",
                    (r["folder_uuid"], nid, _ai.SIBLING_TITLES_COUNT),
                ).fetchall()
                sib_titles = [s["title"] for s in sibs if s["title"] and s["title"] != "Untitled"]

        def on_field(field_name: str, value: str, _nid=nid):
            """Stream callback: title closed or summary closed in the JSON.
            Update just that field + flip status to 'streaming' + bump
            updated_at so iOS /sync/changes pulls the partial state."""
            ts_f = now_ms()
            col = "ai_title" if field_name == "title" else "ai_summary"
            with db() as cf:
                cf.execute(
                    f"UPDATE notes SET {col}=?, ai_status='streaming', updated_at=? WHERE uuid=?",
                    (value, ts_f, _nid),
                )
                _bump_parent_folder(cf, r["folder_uuid"], ts_f)

        def on_done(meta: dict, _nid=nid):
            """Stream end: write the rest of the fields atomically + flip
            status to 'ok'. Final updated_at bump = last visible change."""
            ts_d = now_ms()
            with db() as cd:
                cd.execute(
                    """UPDATE notes SET
                           ai_title=?, ai_tags=?, ai_summary=?, ai_tldr=?, ai_keypoints=?,
                           ai_generated_at=?, ai_model=?, ai_status='ok', ai_error=NULL,
                           updated_at=?
                       WHERE uuid=?""",
                    (meta["title"], json.dumps(meta["tags"]), meta["summary"],
                     meta["tldr"], json.dumps(meta["key_points"]),
                     ts_d, meta["model"], ts_d, _nid),
                )
                _bump_parent_folder(cd, r["folder_uuid"], ts_d)

        try:
            _ai.stream_for_note(
                note_title=r["title"], body=r["body"] or "",
                folder_name=r["folder_name"], folder_kind=r["folder_kind"],
                status=r["status"], sibling_titles=sib_titles,
                on_field=on_field, on_done=on_done,
            )
        except Exception as e:
            # If title/summary already landed via on_field, we keep them and
            # mark 'ok' (partial); otherwise mark failed for retry. Either
            # way bump the attempt counter.
            with db() as c3:
                current = c3.execute(
                    "SELECT ai_title, ai_summary FROM notes WHERE uuid=?", (nid,),
                ).fetchone()
                has_partial = current and (current["ai_title"] or current["ai_summary"])
                next_attempts = r["ai_attempts"] + 1
                if has_partial:
                    # Partial reveal succeeded; treat as ok (no retry)
                    c3.execute(
                        "UPDATE notes SET ai_status='ok', ai_error=? WHERE uuid=?",
                        (f"stream ended early (kept partial): {str(e)[:300]}", nid),
                    )
                else:
                    c3.execute(
                        "UPDATE notes SET ai_attempts=?, ai_status=?, ai_error=? WHERE uuid=?",
                        (next_attempts,
                         "failed" if next_attempts >= AI_MAX_ATTEMPTS else "pending",
                         str(e)[:500], nid),
                    )


def _ai_worker_loop():
    """Daemon thread. Sleeps AI_POLL_SECONDS between passes. Caught errors
    don't kill the loop — only an interpreter shutdown should stop it."""
    import time as _t
    while True:
        try:
            _ai_worker_pass()
        except Exception as e:
            print(f"[ai_worker] pass error: {e}", flush=True)
        _t.sleep(AI_POLL_SECONDS)


_ai_worker_started = False


def _start_ai_worker():
    global _ai_worker_started
    if _ai_worker_started:
        return
    if not os.environ.get("OPENROUTER_API_KEY"):
        print("[ai_worker] OPENROUTER_API_KEY not set; AI metadata disabled", flush=True)
        return
    import threading
    t = threading.Thread(target=_ai_worker_loop, name="ai-metadata-worker", daemon=True)
    t.start()
    _ai_worker_started = True
    print(f"[ai_worker] started (poll={AI_POLL_SECONDS}s, batch={AI_BATCH_SIZE}, model={_ai.DEFAULT_MODEL})", flush=True)


@app.on_event("startup")
def _on_startup():
    _start_ai_worker()


def run():
    import uvicorn
    host = os.environ.get("NOTED_SYNC_HOST", "127.0.0.1")
    port = int(os.environ.get("NOTED_SYNC_PORT", "8770"))
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    run()
