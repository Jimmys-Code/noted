import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import MarkdownIt from "markdown-it";
import { motion } from "framer-motion";
import Sidebar, { COLORS, CursorKey } from "./components/Sidebar";
import Editor from "./components/Editor";
import Help from "./components/Help";
import { api, Folder, Note, NoteMeta } from "./api";

const md = new MarkdownIt({ html: false, linkify: true, breaks: true, typographer: true });

type Focus = "sidebar" | "editor";

type Flat =
  | { kind: "folder"; id: number; folder: Folder }
  | { kind: "note"; id: number; note: NoteMeta; folder: Folder };

export default function App() {
  const [collapsed, setCollapsed] = useState(false);
  const [folders, setFolders] = useState<Folder[]>([]);
  const [notes, setNotes] = useState<NoteMeta[]>([]);
  const [openFolders, setOpenFolders] = useState<Set<number>>(new Set());
  const [selectedNote, setSelectedNote] = useState<number | null>(null);
  const [current, setCurrent] = useState<Note | null>(null);
  const [vimMode, setVimMode] = useState(true);
  const [preview, setPreview] = useState(false);
  const [focus, setFocus] = useState<Focus>("sidebar");
  const [cursor, setCursor] = useState<CursorKey>(null);
  const [renaming, setRenaming] = useState<number | null>(null);
  const [helpOpen, setHelpOpen] = useState(false);
  const lastGRef = useRef<number>(0);
  const saveTimer = useRef<number | null>(null);
  const editorWrapRef = useRef<HTMLDivElement>(null);
  const titleRef = useRef<HTMLInputElement>(null);

  const focusEditor = useCallback(() => {
    setFocus("editor");
    let tries = 0;
    const tick = () => {
      const cm = editorWrapRef.current?.querySelector<HTMLElement>(".cm-content");
      if (cm) { cm.focus(); return; }
      if (++tries < 20) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }, []);

  const focusTitle = useCallback(() => {
    setFocus("editor");
    let tries = 0;
    const tick = () => {
      const el = titleRef.current;
      if (el) { el.focus(); el.select(); return; }
      if (++tries < 20) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }, []);

  const refreshFolders = useCallback(async () => setFolders(await api.folders()), []);
  const refreshNotes = useCallback(async () => setNotes(await api.notes()), []);

  useEffect(() => {
    (async () => {
      await refreshFolders();
      await refreshNotes();
    })();
  }, [refreshFolders, refreshNotes]);

  useEffect(() => {
    if (folders.length && !cursor) {
      setCursor({ kind: "folder", id: folders[0].id });
      setOpenFolders(new Set([folders[0].id]));
    }
  }, [folders, cursor]);

  useEffect(() => {
    if (selectedNote == null) {
      setCurrent(null);
      return;
    }
    api.note(selectedNote).then(setCurrent);
  }, [selectedNote]);

  // Flat list for sidebar navigation
  const flat = useMemo<Flat[]>(() => {
    const out: Flat[] = [];
    for (const f of folders) {
      out.push({ kind: "folder", id: f.id, folder: f });
      if (openFolders.has(f.id)) {
        for (const n of notes.filter((x) => x.folder_id === f.id)) {
          out.push({ kind: "note", id: n.id, note: n, folder: f });
        }
      }
    }
    return out;
  }, [folders, notes, openFolders]);

  const cursorIdx = useMemo(() => {
    if (!cursor) return -1;
    return flat.findIndex((x) => x.kind === cursor.kind && x.id === cursor.id);
  }, [flat, cursor]);

  const setCursorAt = useCallback((i: number) => {
    if (i < 0 || i >= flat.length) return;
    const it = flat[i];
    setCursor({ kind: it.kind, id: it.id });
  }, [flat]);

  const pendingPatch = useRef<Partial<Note> | null>(null);
  const pendingId = useRef<number | null>(null);

  const doSave = useCallback(async () => {
    if (saveTimer.current) { window.clearTimeout(saveTimer.current); saveTimer.current = null; }
    const id = pendingId.current;
    const patch = pendingPatch.current;
    if (id == null || !patch) return;
    pendingPatch.current = null;
    pendingId.current = null;
    const updated = await api.updateNote(id, patch);
    setNotes((prev) =>
      prev.map((n) =>
        n.id === updated.id
          ? { id: updated.id, folder_id: updated.folder_id, title: updated.title, created_at: updated.created_at, updated_at: updated.updated_at }
          : n,
      ),
    );
  }, []);

  const flush = useCallback(() => { void doSave(); }, [doSave]);

  const scheduleSave = useCallback((next: Partial<Note>) => {
    if (!current) return;
    setCurrent({ ...current, ...next } as Note);
    pendingId.current = current.id;
    pendingPatch.current = { ...(pendingPatch.current || {}), ...next };
    if (saveTimer.current) window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(() => { void doSave(); }, 350);
  }, [current, doSave]);

  // Save on window unload
  useEffect(() => {
    const h = () => { void doSave(); };
    window.addEventListener("beforeunload", h);
    return () => window.removeEventListener("beforeunload", h);
  }, [doSave]);

  const currentFolderId = useCallback((): number | null => {
    if (cursor?.kind === "folder") return cursor.id;
    if (cursor?.kind === "note") {
      const n = notes.find((x) => x.id === cursor.id);
      return n?.folder_id ?? folders[0]?.id ?? null;
    }
    return folders[0]?.id ?? null;
  }, [cursor, notes, folders]);

  const createNote = useCallback(async (folderId: number | null) => {
    if (folderId == null) return;
    const n = await api.createNote(folderId, "Untitled", "# Untitled\n\n");
    await refreshNotes();
    setOpenFolders((s) => new Set([...s, folderId]));
    setSelectedNote(n.id);
    setCursor({ kind: "note", id: n.id });
  }, [refreshNotes]);

  const createFolder = useCallback(async () => {
    const name = prompt("Folder name");
    if (!name) return;
    const color = COLORS[Math.floor(Math.random() * COLORS.length)];
    const f = await api.createFolder(name, color);
    await refreshFolders();
    setCursor({ kind: "folder", id: f.id });
    setRenaming(f.id);
  }, [refreshFolders]);

  const cycleColor = useCallback(async (folderId: number) => {
    const f = folders.find((x) => x.id === folderId);
    if (!f) return;
    const idx = COLORS.indexOf(f.color);
    const next = COLORS[(idx + 1) % COLORS.length];
    await api.updateFolder(folderId, { color: next });
    await refreshFolders();
  }, [folders, refreshFolders]);

  const deleteAtCursor = useCallback(async () => {
    if (!cursor) return;
    if (cursor.kind === "folder") {
      const f = folders.find((x) => x.id === cursor.id);
      if (!f) return;
      if (!confirm(`Delete folder "${f.name}" and its notes?`)) return;
      await api.deleteFolder(cursor.id);
      await refreshFolders();
      await refreshNotes();
      setCursor(folders[0] && folders[0].id !== cursor.id ? { kind: "folder", id: folders[0].id } : null);
    } else {
      if (!confirm("Delete note?")) return;
      await api.deleteNote(cursor.id);
      if (selectedNote === cursor.id) setSelectedNote(null);
      await refreshNotes();
    }
  }, [cursor, folders, refreshFolders, refreshNotes, selectedNote]);

  const toggleFolder = useCallback((id: number) => {
    setOpenFolders((s) => {
      const n = new Set(s);
      n.has(id) ? n.delete(id) : n.add(id);
      return n;
    });
  }, []);

  // Global key handler
  useEffect(() => {
    const isEditable = (el: EventTarget | null) => {
      if (!(el instanceof HTMLElement)) return false;
      const tag = el.tagName;
      return tag === "INPUT" || tag === "TEXTAREA" || el.isContentEditable;
    };
    const isInEditor = (el: EventTarget | null) =>
      el instanceof HTMLElement && !!el.closest(".editor-host");

    const h = (e: KeyboardEvent) => {
      // Help overlay always closeable with Esc / ?
      if (helpOpen && e.key === "Escape") {
        setHelpOpen(false);
        e.preventDefault();
        return;
      }

      const inEditor = isInEditor(e.target);
      const editing = renaming != null || (isEditable(e.target) && !inEditor);

      // GLOBAL app shortcuts — these always win, even inside vim
      const ctrl = e.ctrlKey || e.metaKey;
      if (!editing) {
        if (ctrl && e.key.toLowerCase() === "b") { e.preventDefault(); setCollapsed((c) => !c); return; }
        if (ctrl && e.key === "1") { e.preventDefault(); setFocus("sidebar"); (document.activeElement as HTMLElement)?.blur?.(); return; }
        if (ctrl && e.key === "2") { e.preventDefault(); focusEditor(); return; }
        if (ctrl && e.key.toLowerCase() === "t") { e.preventDefault(); focusTitle(); return; }
        if (ctrl && e.key.toLowerCase() === "s") { e.preventDefault(); flush(); return; }
        if (ctrl && e.shiftKey && e.key.toLowerCase() === "n") { e.preventDefault(); createFolder(); return; }

        // Shortcuts that must NOT fire while the editor is focused (they conflict with vim)
        if (!inEditor) {
          if (ctrl && !e.shiftKey && e.key.toLowerCase() === "n") { e.preventDefault(); createNote(currentFolderId()); return; }
          if (ctrl && e.key.toLowerCase() === "e") { e.preventDefault(); setPreview((p) => !p); return; }
          if (e.key === "?" || (e.shiftKey && e.key === "/")) { e.preventDefault(); setHelpOpen((o) => !o); return; }
        }
      }

      if (e.key === "Escape") {
        if (isInEditor(e.target)) {
          // let vim handle Esc inside editor — but switch focus to sidebar if Shift+Esc
        } else if (!editing) {
          setFocus("sidebar");
        }
      }

      // SIDEBAR-FOCUSED keys
      if (focus !== "sidebar" || editing || helpOpen) return;
      // If user is typing inside the title input, skip
      if (isEditable(e.target)) return;

      const move = (delta: number) => {
        if (flat.length === 0) return;
        const i = cursorIdx < 0 ? 0 : Math.max(0, Math.min(flat.length - 1, cursorIdx + delta));
        setCursorAt(i);
      };

      switch (e.key) {
        case "j": case "ArrowDown": e.preventDefault(); move(1); break;
        case "k": case "ArrowUp": e.preventDefault(); move(-1); break;
        case "G": e.preventDefault(); setCursorAt(flat.length - 1); break;
        case "g": {
          const now = Date.now();
          if (now - lastGRef.current < 400) { setCursorAt(0); lastGRef.current = 0; }
          else lastGRef.current = now;
          e.preventDefault();
          break;
        }
        case "l": case "ArrowRight": {
          e.preventDefault();
          if (cursor?.kind === "folder") {
            if (!openFolders.has(cursor.id)) toggleFolder(cursor.id);
            else move(1);
          }
          break;
        }
        case "h": case "ArrowLeft": {
          e.preventDefault();
          if (cursor?.kind === "folder" && openFolders.has(cursor.id)) {
            toggleFolder(cursor.id);
          } else if (cursor?.kind === "note") {
            const n = notes.find((x) => x.id === cursor.id);
            if (n?.folder_id != null) setCursor({ kind: "folder", id: n.folder_id });
          }
          break;
        }
        case "Enter": case "o": {
          e.preventDefault();
          if (cursor?.kind === "folder") toggleFolder(cursor.id);
          else if (cursor?.kind === "note") { flush(); setSelectedNote(cursor.id); focusEditor(); }
          break;
        }
        case "r": {
          e.preventDefault();
          if (cursor?.kind === "folder") setRenaming(cursor.id);
          break;
        }
        case "c": {
          e.preventDefault();
          if (cursor?.kind === "folder") cycleColor(cursor.id);
          break;
        }
        case "d": {
          e.preventDefault();
          deleteAtCursor();
          break;
        }
      }
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [focus, cursor, cursorIdx, flat, openFolders, notes, helpOpen, renaming, createFolder, createNote, currentFolderId, cycleColor, deleteAtCursor, flush, toggleFolder, setCursorAt, focusEditor, focusTitle]);

  // When focus switches to editor, focus the CodeMirror element
  useEffect(() => {
    if (focus !== "editor") return;
    const t = setTimeout(() => {
      const cm = editorWrapRef.current?.querySelector<HTMLElement>(".cm-content");
      cm?.focus();
    }, 50);
    return () => clearTimeout(t);
  }, [focus, current?.id, preview]);

  const folderColor = useMemo(() => {
    const f = folders.find((x) => x.id === current?.folder_id);
    return f?.color ?? "#7c8cff";
  }, [folders, current]);

  return (
    <div className="app">
      <Sidebar
        collapsed={collapsed}
        folders={folders}
        notes={notes}
        openFolders={openFolders}
        selectedNote={selectedNote}
        cursor={cursor}
        focused={focus === "sidebar"}
        renamingFolder={renaming}
        onClickFolder={(id) => {
          setFocus("sidebar");
          setCursor({ kind: "folder", id });
          toggleFolder(id);
        }}
        onClickNote={(id) => {
          flush();
          setCursor({ kind: "note", id });
          setSelectedNote(id);
          focusEditor();
        }}
        onUpdateFolder={async (id, patch) => {
          await api.updateFolder(id, patch as any);
          await refreshFolders();
        }}
        onCommitRename={async (id, name) => {
          await api.updateFolder(id, { name });
          await refreshFolders();
          setRenaming(null);
        }}
        onCancelRename={() => setRenaming(null)}
        onCreateNoteInFolder={(id) => createNote(id)}
      />

      <main className="main">
        <header className="topbar">
          <button className="icon-btn" onClick={() => setCollapsed((c) => !c)} title="Toggle sidebar (Ctrl+B)">
            ☰
          </button>
          {current ? (
            <input
              ref={titleRef}
              className="title-input"
              value={current.title}
              onChange={(e) => scheduleSave({ title: e.target.value })}
              onKeyDown={(e) => {
                if (e.key === "Enter") { e.preventDefault(); focusEditor(); }
                if (e.key === "Escape") { e.preventDefault(); (e.target as HTMLInputElement).blur(); }
              }}
              style={{ borderLeft: `3px solid ${folderColor}` }}
            />
          ) : (
            <span className="title-placeholder">No note selected — Ctrl+N to create one</span>
          )}
          <div className="toolbar">
            <button
              className={`pill ${vimMode ? "on" : ""}`}
              onClick={() => setVimMode((v) => !v)}
              title="Toggle vim mode"
            >
              VIM
            </button>
            <button
              className={`pill ${preview ? "on" : ""}`}
              onClick={() => setPreview((p) => !p)}
              title="Toggle preview (Ctrl+E)"
            >
              PREVIEW
            </button>
            <button
              className="pill help-pill"
              onClick={() => setHelpOpen(true)}
              title="Keyboard controls (?)"
            >
              ?
            </button>
          </div>
        </header>

        <motion.section
          className="workspace"
          key={current?.id ?? "empty"}
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.18 }}
        >
          <div ref={editorWrapRef} style={{ height: "100%" }}>
            {current ? (
              preview ? (
                <article
                  className="preview markdown-body"
                  dangerouslySetInnerHTML={{ __html: md.render(current.body || "") }}
                />
              ) : (
                <Editor
                  value={current.body}
                  onChange={(v) => scheduleSave({ body: v })}
                  vimMode={vimMode}
                />
              )
            ) : (
              <div className="empty">
                <p>Pick a folder and create a note.</p>
                <p className="hint">Press <kbd>?</kbd> for keyboard controls</p>
              </div>
            )}
          </div>
        </motion.section>
      </main>

      <Help open={helpOpen} onClose={() => setHelpOpen(false)} />
    </div>
  );
}
