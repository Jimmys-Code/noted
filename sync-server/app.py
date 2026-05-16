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

NoteStatus = Literal["idea", "open", "in-progress", "testing", "done"]
FolderKind = Literal["general", "project"]

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
    return {
        "uuid": r["uuid"], "folder_uuid": r["folder_uuid"],
        "title": r["title"], "body": r["body"],
        "created_at": r["created_at"], "updated_at": r["updated_at"],
        "deleted_at": r["deleted_at"], "status": r["status"],
    }


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
):
    """All alive folders. Optional `kind` + `active` filters — e.g.
    /sync/folders?kind=project&active=true returns the iOS sidebar's project list."""
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
    return [_row_to_folder(r) for r in rows]


@app.get("/sync/notes", dependencies=[auth])
def list_notes(
    folder: Optional[str] = Query(None, description="folder uuid filter"),
    since: int = Query(0, ge=0, description="ms epoch; notes updated_at > since"),
    limit: int = Query(100, ge=1, le=500),
    body: bool = Query(False, description="include body in response (default: title+meta only)"),
):
    """List alive notes with optional folder filter + since cursor. Default skips
    body for index-style listings; pass body=true to inline content."""
    q = "SELECT * FROM notes WHERE deleted_at IS NULL"
    args: list = []
    if folder is not None:
        q += " AND folder_uuid=?"
        args.append(folder)
    if since:
        q += " AND updated_at > ?"
        args.append(since)
    q += " ORDER BY updated_at DESC LIMIT ?"
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
    limit: int = Query(20, ge=1, le=100),
):
    """Substring search across alive notes (title + body, case-insensitive).
    Returns matches with a 200-char snippet around the first hit. For agents
    that want to RAG against the user's notes; iOS uses for global search."""
    needle = q.lower()
    sql = ("""SELECT n.uuid, n.title, n.body, n.updated_at, f.name AS folder_name, n.folder_uuid
              FROM notes n LEFT JOIN folders f ON f.uuid = n.folder_uuid
              WHERE n.deleted_at IS NULL
                AND (LOWER(n.title) LIKE ? OR LOWER(n.body) LIKE ?)""")
    args = [f"%{needle}%", f"%{needle}%"]
    if folder is not None:
        sql += " AND n.folder_uuid=?"
        args.append(folder)
    sql += " ORDER BY n.updated_at DESC LIMIT ?"
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
            "uuid": r["uuid"], "title": r["title"],
            "folder": r["folder_name"], "folder_uuid": r["folder_uuid"],
            "updated_at": r["updated_at"], "snippet": snippet,
        })
    return {"q": q, "matches": out}


def run():
    import uvicorn
    host = os.environ.get("NOTED_SYNC_HOST", "127.0.0.1")
    port = int(os.environ.get("NOTED_SYNC_PORT", "8770"))
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    run()
