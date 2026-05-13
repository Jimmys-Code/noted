"""
noted — local backend.

Owns a local SQLite DB. UI talks here for snappy reads/writes.
When configured with a remote sync server, runs a background sync worker:
  - pulls changes every 30s (and once on startup)
  - pushes local changes with a short debounce after each mutation
Conflict resolution: last-write-wins by updated_at (ms epoch).
"""

import os
import sqlite3
import threading
import time
import uuid as _uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

try:
    import tomllib  # py3.11+
except ImportError:
    import tomli as tomllib  # type: ignore

DB_PATH = Path(os.environ.get("NOTED_DB", Path.home() / ".local/share/noted/noted.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = Path(os.environ.get("NOTED_CONFIG", Path.home() / ".config/noted/sync.toml"))


def now_ms() -> int:
    return int(time.time() * 1000)


def new_uuid() -> str:
    return _uuid.uuid4().hex


# ---------------- DB ----------------

_db_lock = threading.RLock()


@contextmanager
def db():
    # Single connection per call; serialized via _db_lock for sync-thread + request-thread coexistence.
    with _db_lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def _col_exists(c: sqlite3.Connection, table: str, col: str) -> bool:
    rows = c.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == col for r in rows)


def migrate():
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS folders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                color TEXT NOT NULL DEFAULT '#ff9a3c',
                position INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                folder_id INTEGER REFERENCES folders(id) ON DELETE CASCADE,
                title TEXT NOT NULL DEFAULT 'Untitled',
                body TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sync_state (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )

        # Add sync columns if missing
        added = []
        if not _col_exists(c, "folders", "uuid"):
            c.execute("ALTER TABLE folders ADD COLUMN uuid TEXT")
            added.append("folders.uuid")
        if not _col_exists(c, "folders", "updated_at"):
            c.execute("ALTER TABLE folders ADD COLUMN updated_at INTEGER")
            added.append("folders.updated_at")
        if not _col_exists(c, "folders", "deleted_at"):
            c.execute("ALTER TABLE folders ADD COLUMN deleted_at INTEGER")
            added.append("folders.deleted_at")
        if not _col_exists(c, "notes", "uuid"):
            c.execute("ALTER TABLE notes ADD COLUMN uuid TEXT")
            added.append("notes.uuid")
        if not _col_exists(c, "notes", "deleted_at"):
            c.execute("ALTER TABLE notes ADD COLUMN deleted_at INTEGER")
            added.append("notes.deleted_at")

        # Backfill uuids
        for r in c.execute("SELECT id FROM folders WHERE uuid IS NULL OR uuid = ''").fetchall():
            c.execute("UPDATE folders SET uuid=? WHERE id=?", (new_uuid(), r["id"]))
        for r in c.execute("SELECT id FROM notes WHERE uuid IS NULL OR uuid = ''").fetchall():
            c.execute("UPDATE notes SET uuid=? WHERE id=?", (new_uuid(), r["id"]))

        # Backfill folders.updated_at from created_at
        c.execute("UPDATE folders SET updated_at = COALESCE(updated_at, created_at)")

        # Convert legacy second-epoch timestamps to ms. Anything < 10^12 is seconds (year ~2001 in ms).
        c.execute("UPDATE folders SET created_at = created_at * 1000 WHERE created_at < 100000000000")
        c.execute("UPDATE folders SET updated_at = updated_at * 1000 WHERE updated_at < 100000000000")
        c.execute("UPDATE notes   SET created_at = created_at * 1000 WHERE created_at < 100000000000")
        c.execute("UPDATE notes   SET updated_at = updated_at * 1000 WHERE updated_at < 100000000000")

        # Unique indexes on uuid
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_folders_uuid ON folders(uuid)")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_notes_uuid   ON notes(uuid)")

        # Seed a default folder if empty
        n = c.execute("SELECT COUNT(*) FROM folders WHERE deleted_at IS NULL").fetchone()[0]
        if n == 0:
            t = now_ms()
            c.execute(
                "INSERT INTO folders (uuid,name,color,position,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                (new_uuid(), "Inbox", "#ff9a3c", 0, t, t),
            )


migrate()


# ---------------- Sync state helpers ----------------

def state_get(key: str, default: Optional[str] = None) -> Optional[str]:
    with db() as c:
        r = c.execute("SELECT value FROM sync_state WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default


def state_set(key: str, value: str) -> None:
    with db() as c:
        c.execute(
            "INSERT INTO sync_state(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


# ---------------- API models ----------------

class FolderIn(BaseModel):
    name: str
    color: str = "#ff9a3c"


class FolderPatch(BaseModel):
    name: Optional[str] = None
    color: Optional[str] = None


class NoteIn(BaseModel):
    folder_id: Optional[int] = None
    title: str = "Untitled"
    body: str = ""


class NotePatch(BaseModel):
    folder_id: Optional[int] = None
    title: Optional[str] = None
    body: Optional[str] = None


# ---------------- App ----------------

app = FastAPI(title="noted")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

push_event = threading.Event()


def _trigger_push():
    push_event.set()


def folder_row(r) -> dict:
    if not r:
        return None  # type: ignore
    return {
        "id": r["id"], "uuid": r["uuid"], "name": r["name"], "color": r["color"],
        "position": r["position"], "created_at": r["created_at"], "updated_at": r["updated_at"],
    }


def note_meta_row(r) -> dict:
    return {
        "id": r["id"], "uuid": r["uuid"], "folder_id": r["folder_id"],
        "title": r["title"], "created_at": r["created_at"], "updated_at": r["updated_at"],
    }


def note_full_row(r) -> dict:
    d = note_meta_row(r)
    d["body"] = r["body"]
    return d


# ---------------- Folders ----------------

@app.get("/folders")
def list_folders():
    with db() as c:
        rows = c.execute(
            "SELECT * FROM folders WHERE deleted_at IS NULL ORDER BY position, id"
        ).fetchall()
        return [folder_row(r) for r in rows]


@app.post("/folders")
def create_folder(f: FolderIn):
    t = now_ms()
    with db() as c:
        pos = c.execute("SELECT COALESCE(MAX(position),-1)+1 FROM folders").fetchone()[0]
        cur = c.execute(
            "INSERT INTO folders (uuid,name,color,position,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (new_uuid(), f.name, f.color, pos, t, t),
        )
        row = c.execute("SELECT * FROM folders WHERE id=?", (cur.lastrowid,)).fetchone()
    _trigger_push()
    return folder_row(row)


@app.patch("/folders/{fid}")
def update_folder(fid: int, p: FolderPatch):
    t = now_ms()
    with db() as c:
        cur = c.execute("SELECT * FROM folders WHERE id=? AND deleted_at IS NULL", (fid,)).fetchone()
        if not cur:
            raise HTTPException(404)
        name = p.name if p.name is not None else cur["name"]
        color = p.color if p.color is not None else cur["color"]
        c.execute(
            "UPDATE folders SET name=?, color=?, updated_at=? WHERE id=?",
            (name, color, t, fid),
        )
        row = c.execute("SELECT * FROM folders WHERE id=?", (fid,)).fetchone()
    _trigger_push()
    return folder_row(row)


@app.delete("/folders/{fid}")
def delete_folder(fid: int):
    t = now_ms()
    with db() as c:
        # Soft-delete folder + cascade soft-delete its notes
        c.execute("UPDATE folders SET deleted_at=?, updated_at=? WHERE id=? AND deleted_at IS NULL",
                  (t, t, fid))
        c.execute("UPDATE notes SET deleted_at=?, updated_at=? WHERE folder_id=? AND deleted_at IS NULL",
                  (t, t, fid))
    _trigger_push()
    return {"ok": True}


# ---------------- Notes ----------------

@app.get("/notes")
def list_notes(folder_id: Optional[int] = None):
    with db() as c:
        if folder_id is None:
            rows = c.execute(
                "SELECT id,uuid,folder_id,title,created_at,updated_at FROM notes "
                "WHERE deleted_at IS NULL ORDER BY updated_at DESC"
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT id,uuid,folder_id,title,created_at,updated_at FROM notes "
                "WHERE deleted_at IS NULL AND folder_id=? ORDER BY updated_at DESC",
                (folder_id,),
            ).fetchall()
        return [note_meta_row(r) for r in rows]


@app.get("/notes/{nid}")
def get_note(nid: int):
    with db() as c:
        r = c.execute("SELECT * FROM notes WHERE id=? AND deleted_at IS NULL", (nid,)).fetchone()
        if not r:
            raise HTTPException(404)
        return note_full_row(r)


@app.post("/notes")
def create_note(n: NoteIn):
    t = now_ms()
    with db() as c:
        cur = c.execute(
            "INSERT INTO notes (uuid,folder_id,title,body,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (new_uuid(), n.folder_id, n.title, n.body, t, t),
        )
        row = c.execute("SELECT * FROM notes WHERE id=?", (cur.lastrowid,)).fetchone()
    _trigger_push()
    return note_full_row(row)


@app.patch("/notes/{nid}")
def update_note(nid: int, p: NotePatch):
    t = now_ms()
    with db() as c:
        cur = c.execute("SELECT * FROM notes WHERE id=? AND deleted_at IS NULL", (nid,)).fetchone()
        if not cur:
            raise HTTPException(404)
        folder_id = p.folder_id if p.folder_id is not None else cur["folder_id"]
        title = p.title if p.title is not None else cur["title"]
        body = p.body if p.body is not None else cur["body"]
        c.execute(
            "UPDATE notes SET folder_id=?, title=?, body=?, updated_at=? WHERE id=?",
            (folder_id, title, body, t, nid),
        )
        row = c.execute("SELECT * FROM notes WHERE id=?", (nid,)).fetchone()
    _trigger_push()
    return note_full_row(row)


@app.delete("/notes/{nid}")
def delete_note(nid: int):
    t = now_ms()
    with db() as c:
        c.execute("UPDATE notes SET deleted_at=?, updated_at=? WHERE id=? AND deleted_at IS NULL",
                  (t, t, nid))
    _trigger_push()
    return {"ok": True}


# ---------------- Sync ----------------

class SyncConfig:
    def __init__(self):
        self.url: Optional[str] = None
        self.token: Optional[str] = None
        self.load()

    def load(self):
        if not CONFIG_PATH.exists():
            return
        try:
            data = tomllib.loads(CONFIG_PATH.read_text())
            self.url = (data.get("server_url") or "").rstrip("/") or None
            self.token = data.get("token") or None
        except Exception as e:
            print(f"[sync] config load failed: {e}")


sync_cfg = SyncConfig()


class SyncStatus:
    def __init__(self):
        self.lock = threading.Lock()
        self.last_pull_ts: int = 0
        self.last_push_ts: int = 0
        self.last_error: Optional[str] = None
        self.in_flight: bool = False
        self.pending: int = 0


sync_status = SyncStatus()


def _row_to_payload_folder(r) -> dict:
    return {
        "uuid": r["uuid"], "name": r["name"], "color": r["color"],
        "position": r["position"], "created_at": r["created_at"],
        "updated_at": r["updated_at"], "deleted_at": r["deleted_at"],
    }


def _row_to_payload_note(r) -> dict:
    # We need the folder's uuid for sync; resolve by lookup
    folder_uuid = None
    if r["folder_id"] is not None:
        with db() as c:
            fr = c.execute("SELECT uuid FROM folders WHERE id=?", (r["folder_id"],)).fetchone()
            folder_uuid = fr["uuid"] if fr else None
    return {
        "uuid": r["uuid"], "folder_uuid": folder_uuid,
        "title": r["title"], "body": r["body"],
        "created_at": r["created_at"], "updated_at": r["updated_at"],
        "deleted_at": r["deleted_at"],
    }


def _pending_count() -> int:
    push_cursor = int(state_get("last_push_cursor", "0") or "0")
    with db() as c:
        f = c.execute("SELECT COUNT(*) FROM folders WHERE updated_at > ?", (push_cursor,)).fetchone()[0]
        n = c.execute("SELECT COUNT(*) FROM notes   WHERE updated_at > ?", (push_cursor,)).fetchone()[0]
        return f + n


def _apply_pull(payload: dict) -> int:
    """Apply pulled changes with last-write-wins. Returns number of records applied."""
    applied = 0
    folders = payload.get("folders") or []
    notes = payload.get("notes") or []

    with db() as c:
        # Folders first so notes can resolve folder_uuid -> folder_id
        for f in folders:
            cur = c.execute("SELECT * FROM folders WHERE uuid=?", (f["uuid"],)).fetchone()
            f_up = int(f.get("updated_at") or 0)
            if cur is None:
                if f.get("deleted_at"):
                    # tombstone for a row we never had — skip
                    continue
                c.execute(
                    "INSERT INTO folders (uuid,name,color,position,created_at,updated_at,deleted_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (f["uuid"], f.get("name", "Untitled"), f.get("color", "#ff9a3c"),
                     int(f.get("position", 0)), int(f.get("created_at") or now_ms()),
                     f_up, f.get("deleted_at")),
                )
                applied += 1
            elif f_up > int(cur["updated_at"] or 0):
                c.execute(
                    "UPDATE folders SET name=?, color=?, position=?, updated_at=?, deleted_at=? "
                    "WHERE uuid=?",
                    (f.get("name", cur["name"]), f.get("color", cur["color"]),
                     int(f.get("position", cur["position"])), f_up, f.get("deleted_at"), f["uuid"]),
                )
                applied += 1

        for n in notes:
            cur = c.execute("SELECT * FROM notes WHERE uuid=?", (n["uuid"],)).fetchone()
            n_up = int(n.get("updated_at") or 0)
            folder_id = None
            if n.get("folder_uuid"):
                fr = c.execute("SELECT id FROM folders WHERE uuid=?", (n["folder_uuid"],)).fetchone()
                folder_id = fr["id"] if fr else None
            if cur is None:
                if n.get("deleted_at"):
                    continue
                c.execute(
                    "INSERT INTO notes (uuid,folder_id,title,body,created_at,updated_at,deleted_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (n["uuid"], folder_id, n.get("title", "Untitled"), n.get("body", ""),
                     int(n.get("created_at") or now_ms()), n_up, n.get("deleted_at")),
                )
                applied += 1
            elif n_up > int(cur["updated_at"] or 0):
                c.execute(
                    "UPDATE notes SET folder_id=?, title=?, body=?, updated_at=?, deleted_at=? "
                    "WHERE uuid=?",
                    (folder_id, n.get("title", cur["title"]), n.get("body", cur["body"]),
                     n_up, n.get("deleted_at"), n["uuid"]),
                )
                applied += 1
    return applied


def _collect_local_changes(since: int) -> dict:
    with db() as c:
        f_rows = c.execute("SELECT * FROM folders WHERE updated_at > ?", (since,)).fetchall()
        n_rows = c.execute("SELECT * FROM notes   WHERE updated_at > ?", (since,)).fetchall()
    return {
        "folders": [_row_to_payload_folder(r) for r in f_rows],
        "notes":   [_row_to_payload_note(r)   for r in n_rows],
    }


def _sync_pull(client: httpx.Client) -> None:
    # Pull cursor (server time) — independent of push cursor (local updated_at watermark)
    pull_cursor = int(state_get("last_pull_server_ts", "0") or "0")
    r = client.get("/sync/changes", params={"since": pull_cursor})
    r.raise_for_status()
    data = r.json()
    applied = _apply_pull(data)
    server_ts = int(data.get("server_ts") or now_ms())
    state_set("last_pull_server_ts", str(server_ts))
    # Records we just applied from the server are already "in agreement" — bump push cursor
    # past their updated_at so we don't echo them back.
    if applied:
        all_ups = [
            *(int(f.get("updated_at") or 0) for f in (data.get("folders") or [])),
            *(int(n.get("updated_at") or 0) for n in (data.get("notes")   or [])),
        ]
        if all_ups:
            push_cursor = int(state_get("last_push_cursor", "0") or "0")
            state_set("last_push_cursor", str(max(push_cursor, max(all_ups))))
    with sync_status.lock:
        sync_status.last_pull_ts = now_ms()


def _sync_push(client: httpx.Client) -> None:
    push_cursor = int(state_get("last_push_cursor", "0") or "0")
    payload = _collect_local_changes(push_cursor)
    if not payload["folders"] and not payload["notes"]:
        return
    r = client.post("/sync/push", json=payload)
    r.raise_for_status()
    max_sent = max(
        [f["updated_at"] for f in payload["folders"]] +
        [n["updated_at"] for n in payload["notes"]],
        default=push_cursor,
    )
    state_set("last_push_cursor", str(max(push_cursor, max_sent)))
    with sync_status.lock:
        sync_status.last_push_ts = now_ms()


def _sync_cycle(client: httpx.Client) -> None:
    with sync_status.lock:
        sync_status.in_flight = True
    try:
        _sync_pull(client)
        _sync_push(client)
        with sync_status.lock:
            sync_status.last_error = None
            sync_status.pending = _pending_count()
    except Exception as e:
        with sync_status.lock:
            sync_status.last_error = str(e)[:200]
    finally:
        with sync_status.lock:
            sync_status.in_flight = False


def _sync_worker():
    if not (sync_cfg.url and sync_cfg.token):
        return
    headers = {"Authorization": f"Bearer {sync_cfg.token}"}
    with httpx.Client(base_url=sync_cfg.url, headers=headers, timeout=20.0) as client:
        # First cycle on startup
        _sync_cycle(client)
        while True:
            # Wait for a push trigger OR periodic pull tick (30s)
            triggered = push_event.wait(timeout=30.0)
            if triggered:
                # Debounce: coalesce bursts of edits
                time.sleep(2.0)
                push_event.clear()
            _sync_cycle(client)


@app.get("/sync/status")
def sync_status_endpoint():
    enabled = bool(sync_cfg.url and sync_cfg.token)
    with sync_status.lock:
        s = {
            "enabled": enabled,
            "url": sync_cfg.url,
            "last_pull_ts": sync_status.last_pull_ts,
            "last_push_ts": sync_status.last_push_ts,
            "last_error": sync_status.last_error,
            "in_flight": sync_status.in_flight,
            "pending": sync_status.pending if enabled else 0,
            "server_ts": now_ms(),
        }
    return s


@app.post("/sync/now")
def sync_now():
    _trigger_push()
    return {"ok": True}


@app.get("/health")
def health():
    return {"ok": True}


@app.on_event("startup")
def _startup():
    with sync_status.lock:
        sync_status.pending = _pending_count() if (sync_cfg.url and sync_cfg.token) else 0
    if sync_cfg.url and sync_cfg.token:
        t = threading.Thread(target=_sync_worker, daemon=True, name="noted-sync")
        t.start()
        print(f"[sync] worker started → {sync_cfg.url}")
    else:
        print("[sync] disabled (no ~/.config/noted/sync.toml or missing fields)")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("NOTED_PORT", "8765"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
