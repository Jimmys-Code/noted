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
from typing import Annotated, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

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
    uuid          TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    color         TEXT NOT NULL DEFAULT '#7c8cff',
    position      INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL,
    deleted_at    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_folders_updated ON folders(updated_at);

CREATE TABLE IF NOT EXISTS notes (
    uuid          TEXT PRIMARY KEY,
    folder_uuid   TEXT,
    title         TEXT NOT NULL DEFAULT 'Untitled',
    body          TEXT NOT NULL DEFAULT '',
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL,
    deleted_at    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_notes_updated ON notes(updated_at);
CREATE INDEX IF NOT EXISTS idx_notes_folder ON notes(folder_uuid);
"""


def init_db():
    with db() as c:
        c.executescript(SCHEMA)


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


class NoteIn(BaseModel):
    uuid: str
    folder_uuid: Optional[str] = None
    title: str = "Untitled"
    body: str = ""
    created_at: int
    updated_at: int
    deleted_at: Optional[int] = None


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
    }


def _row_to_note(r) -> dict:
    return {
        "uuid": r["uuid"], "folder_uuid": r["folder_uuid"],
        "title": r["title"], "body": r["body"],
        "created_at": r["created_at"], "updated_at": r["updated_at"],
        "deleted_at": r["deleted_at"],
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
    were accepted vs rejected so the client knows what to refresh."""
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
                """INSERT INTO folders (uuid,name,color,position,created_at,updated_at,deleted_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(uuid) DO UPDATE SET
                     name=excluded.name, color=excluded.color, position=excluded.position,
                     updated_at=excluded.updated_at, deleted_at=excluded.deleted_at""",
                (f.uuid, f.name, f.color, f.position, f.created_at, f.updated_at, f.deleted_at),
            )
            accepted_f.append(f.uuid)
        for n in payload.notes:
            existing = c.execute(
                "SELECT updated_at FROM notes WHERE uuid=?", (n.uuid,),
            ).fetchone()
            if existing and existing["updated_at"] >= n.updated_at:
                rejected_n.append(n.uuid)
                continue
            c.execute(
                """INSERT INTO notes (uuid,folder_uuid,title,body,created_at,updated_at,deleted_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(uuid) DO UPDATE SET
                     folder_uuid=excluded.folder_uuid, title=excluded.title, body=excluded.body,
                     updated_at=excluded.updated_at, deleted_at=excluded.deleted_at""",
                (n.uuid, n.folder_uuid, n.title, n.body, n.created_at, n.updated_at, n.deleted_at),
            )
            accepted_n.append(n.uuid)
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


# --- Optional: search endpoint for agent-comms / RAG hookup (Phase 2 stub) ---
# This is intentionally minimal — a simple substring scan across non-tombstoned notes.
# FTS5 / embeddings can come later if recall/perf becomes an issue.

@app.get("/sync/search", dependencies=[auth])
def search(q: str = Query(..., min_length=1, max_length=200), limit: int = Query(20, ge=1, le=100)):
    """Substring search across alive notes (title + body, case-insensitive).
    Returns matches with a 200-char snippet around the first hit. For agents
    that want to RAG against the user's notes."""
    needle = q.lower()
    with db() as c:
        rows = c.execute(
            """SELECT n.uuid, n.title, n.body, n.updated_at, f.name AS folder_name
               FROM notes n LEFT JOIN folders f ON f.uuid = n.folder_uuid
               WHERE n.deleted_at IS NULL
                 AND (LOWER(n.title) LIKE ? OR LOWER(n.body) LIKE ?)
               ORDER BY n.updated_at DESC LIMIT ?""",
            (f"%{needle}%", f"%{needle}%", limit),
        ).fetchall()
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
            "uuid": r["uuid"], "title": r["title"], "folder": r["folder_name"],
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
