import { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import type { Folder, NoteMeta } from "../api";

export const COLORS = ["#7c8cff", "#ff7c9c", "#ffc97c", "#7cffb2", "#c97cff", "#7cd6ff", "#ff9c7c"];

export type CursorKey = { kind: "folder" | "note"; id: number } | null;

type Props = {
  collapsed: boolean;
  folders: Folder[];
  notes: NoteMeta[];
  openFolders: Set<number>;
  selectedNote: number | null;
  cursor: CursorKey;
  focused: boolean;
  renamingFolder: number | null;
  onClickFolder: (id: number) => void;
  onClickNote: (id: number) => void;
  onUpdateFolder: (id: number, patch: Partial<Folder>) => void;
  onCommitRename: (id: number, name: string) => void;
  onCancelRename: () => void;
  onCreateNoteInFolder: (id: number) => void;
};

export default function Sidebar(p: Props) {
  const [colorPicker, setColorPicker] = useState<number | null>(null);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!p.cursor || !listRef.current) return;
    const el = listRef.current.querySelector<HTMLElement>(
      `[data-key="${p.cursor.kind}-${p.cursor.id}"]`,
    );
    el?.scrollIntoView({ block: "nearest" });
  }, [p.cursor]);

  return (
    <AnimatePresence initial={false}>
      {!p.collapsed && (
        <motion.aside
          className={`sidebar ${p.focused ? "focused" : ""}`}
          initial={{ width: 0, opacity: 0 }}
          animate={{ width: 280, opacity: 1 }}
          exit={{ width: 0, opacity: 0 }}
          transition={{ type: "spring", stiffness: 320, damping: 32 }}
        >
          <div className="sidebar-inner">
            <div className="sidebar-header">
              <span className="brand">noted</span>
              <span className="focus-hint">{p.focused ? "● nav" : "Ctrl+1"}</span>
            </div>

            <div className="folder-list" ref={listRef}>
              {p.folders.map((f) => {
                const isOpen = p.openFolders.has(f.id);
                const folderNotes = p.notes.filter((n) => n.folder_id === f.id);
                const folderCursor =
                  p.cursor?.kind === "folder" && p.cursor.id === f.id;
                return (
                  <div key={f.id} className="folder">
                    <div
                      data-key={`folder-${f.id}`}
                      className={`folder-row ${folderCursor ? "cursor" : ""}`}
                      onClick={() => p.onClickFolder(f.id)}
                    >
                      <motion.span
                        className="chev"
                        animate={{ rotate: isOpen ? 90 : 0 }}
                        transition={{ duration: 0.18 }}
                      >
                        ▸
                      </motion.span>
                      <span
                        className="folder-dot"
                        style={{ background: f.color }}
                        onClick={(e) => {
                          e.stopPropagation();
                          setColorPicker(colorPicker === f.id ? null : f.id);
                        }}
                      />
                      {p.renamingFolder === f.id ? (
                        <input
                          autoFocus
                          defaultValue={f.name}
                          onBlur={(e) => p.onCommitRename(f.id, e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === "Enter") (e.target as HTMLInputElement).blur();
                            if (e.key === "Escape") p.onCancelRename();
                            e.stopPropagation();
                          }}
                          className="folder-input"
                          onClick={(e) => e.stopPropagation()}
                        />
                      ) : (
                        <span className="folder-name">{f.name}</span>
                      )}
                      <span className="count">{folderNotes.length}</span>
                    </div>

                    <AnimatePresence>
                      {colorPicker === f.id && (
                        <motion.div
                          className="color-picker"
                          initial={{ opacity: 0, height: 0 }}
                          animate={{ opacity: 1, height: "auto" }}
                          exit={{ opacity: 0, height: 0 }}
                        >
                          {COLORS.map((c) => (
                            <button
                              key={c}
                              className="swatch"
                              style={{ background: c }}
                              onClick={() => {
                                p.onUpdateFolder(f.id, { color: c });
                                setColorPicker(null);
                              }}
                            />
                          ))}
                        </motion.div>
                      )}
                    </AnimatePresence>

                    <AnimatePresence initial={false}>
                      {isOpen && (
                        <motion.div
                          className="notes"
                          initial={{ height: 0, opacity: 0 }}
                          animate={{ height: "auto", opacity: 1 }}
                          exit={{ height: 0, opacity: 0 }}
                          transition={{ duration: 0.2 }}
                        >
                          {folderNotes.map((n) => {
                            const noteCursor =
                              p.cursor?.kind === "note" && p.cursor.id === n.id;
                            return (
                              <div
                                key={n.id}
                                data-key={`note-${n.id}`}
                                className={`note-row ${p.selectedNote === n.id ? "active" : ""} ${noteCursor ? "cursor" : ""}`}
                                onClick={() => p.onClickNote(n.id)}
                              >
                                <span className="note-bar" style={{ background: f.color }} />
                                <span className="note-title">{n.title || "Untitled"}</span>
                              </div>
                            );
                          })}
                          <button
                            className="new-note-btn"
                            onClick={() => p.onCreateNoteInFolder(f.id)}
                          >
                            + New note
                          </button>
                        </motion.div>
                      )}
                    </AnimatePresence>
                  </div>
                );
              })}
            </div>
          </div>
        </motion.aside>
      )}
    </AnimatePresence>
  );
}
