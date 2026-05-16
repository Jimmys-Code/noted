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

# Two contexts: standalone (`python app.py` in sync-server/) and packaged
# (`python -m noted_sync` once deployed). Relative import wins in the package
# context; absolute falls back when run from this directory directly.
try:
    from .fuzzy_edit import str_replace as _fuzzy_str_replace
except ImportError:
    from fuzzy_edit import str_replace as _fuzzy_str_replace

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


def run():
    import uvicorn
    host = os.environ.get("NOTED_SYNC_HOST", "127.0.0.1")
    port = int(os.environ.get("NOTED_SYNC_PORT", "8770"))
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    run()
