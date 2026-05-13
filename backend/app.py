import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

DB_PATH = Path(os.environ.get("NOTED_DB", Path.home() / ".local/share/noted/noted.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS folders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                color TEXT NOT NULL DEFAULT '#7c8cff',
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
            """
        )
        n = c.execute("SELECT COUNT(*) FROM folders").fetchone()[0]
        if n == 0:
            now = int(time.time())
            c.execute(
                "INSERT INTO folders (name,color,position,created_at) VALUES (?,?,?,?)",
                ("Inbox", "#7c8cff", 0, now),
            )


init_db()

app = FastAPI(title="noted")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


class FolderIn(BaseModel):
    name: str
    color: str = "#7c8cff"


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


def row(r):
    return dict(r) if r else None


@app.get("/folders")
def list_folders():
    with db() as c:
        rows = c.execute("SELECT * FROM folders ORDER BY position, id").fetchall()
        return [dict(r) for r in rows]


@app.post("/folders")
def create_folder(f: FolderIn):
    now = int(time.time())
    with db() as c:
        pos = c.execute("SELECT COALESCE(MAX(position),-1)+1 FROM folders").fetchone()[0]
        cur = c.execute(
            "INSERT INTO folders (name,color,position,created_at) VALUES (?,?,?,?)",
            (f.name, f.color, pos, now),
        )
        return row(c.execute("SELECT * FROM folders WHERE id=?", (cur.lastrowid,)).fetchone())


@app.patch("/folders/{fid}")
def update_folder(fid: int, p: FolderPatch):
    with db() as c:
        cur = c.execute("SELECT * FROM folders WHERE id=?", (fid,)).fetchone()
        if not cur:
            raise HTTPException(404)
        name = p.name if p.name is not None else cur["name"]
        color = p.color if p.color is not None else cur["color"]
        c.execute("UPDATE folders SET name=?, color=? WHERE id=?", (name, color, fid))
        return row(c.execute("SELECT * FROM folders WHERE id=?", (fid,)).fetchone())


@app.delete("/folders/{fid}")
def delete_folder(fid: int):
    with db() as c:
        c.execute("DELETE FROM folders WHERE id=?", (fid,))
        return {"ok": True}


@app.get("/notes")
def list_notes(folder_id: Optional[int] = None):
    with db() as c:
        if folder_id is None:
            rows = c.execute(
                "SELECT id,folder_id,title,created_at,updated_at FROM notes ORDER BY updated_at DESC"
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT id,folder_id,title,created_at,updated_at FROM notes WHERE folder_id=? ORDER BY updated_at DESC",
                (folder_id,),
            ).fetchall()
        return [dict(r) for r in rows]


@app.get("/notes/{nid}")
def get_note(nid: int):
    with db() as c:
        r = c.execute("SELECT * FROM notes WHERE id=?", (nid,)).fetchone()
        if not r:
            raise HTTPException(404)
        return dict(r)


@app.post("/notes")
def create_note(n: NoteIn):
    now = int(time.time())
    with db() as c:
        cur = c.execute(
            "INSERT INTO notes (folder_id,title,body,created_at,updated_at) VALUES (?,?,?,?,?)",
            (n.folder_id, n.title, n.body, now, now),
        )
        return row(c.execute("SELECT * FROM notes WHERE id=?", (cur.lastrowid,)).fetchone())


@app.patch("/notes/{nid}")
def update_note(nid: int, p: NotePatch):
    now = int(time.time())
    with db() as c:
        cur = c.execute("SELECT * FROM notes WHERE id=?", (nid,)).fetchone()
        if not cur:
            raise HTTPException(404)
        folder_id = p.folder_id if p.folder_id is not None else cur["folder_id"]
        title = p.title if p.title is not None else cur["title"]
        body = p.body if p.body is not None else cur["body"]
        c.execute(
            "UPDATE notes SET folder_id=?, title=?, body=?, updated_at=? WHERE id=?",
            (folder_id, title, body, now, nid),
        )
        return row(c.execute("SELECT * FROM notes WHERE id=?", (nid,)).fetchone())


@app.delete("/notes/{nid}")
def delete_note(nid: int):
    with db() as c:
        c.execute("DELETE FROM notes WHERE id=?", (nid,))
        return {"ok": True}


@app.get("/health")
def health():
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("NOTED_PORT", "8765"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
