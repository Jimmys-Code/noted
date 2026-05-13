const BASE = "http://127.0.0.1:8765";

// Retry initial fetches while the Python backend is still spawning.
// We give up retrying after ~10s; transient errors after that bubble up.
const bootDeadline = Date.now() + 10_000;

async function j<T>(path: string, init?: RequestInit): Promise<T> {
  while (true) {
    try {
      const r = await fetch(BASE + path, {
        ...init,
        headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
      });
      if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
      return r.json();
    } catch (e) {
      if (Date.now() > bootDeadline) throw e;
      await new Promise((res) => setTimeout(res, 120));
    }
  }
}

export type Folder = { id: number; name: string; color: string; position: number; created_at: number };
export type NoteMeta = { id: number; folder_id: number | null; title: string; created_at: number; updated_at: number };
export type Note = NoteMeta & { body: string };

export const api = {
  folders: () => j<Folder[]>("/folders"),
  createFolder: (name: string, color: string) =>
    j<Folder>("/folders", { method: "POST", body: JSON.stringify({ name, color }) }),
  updateFolder: (id: number, patch: Partial<Pick<Folder, "name" | "color">>) =>
    j<Folder>(`/folders/${id}`, { method: "PATCH", body: JSON.stringify(patch) }),
  deleteFolder: (id: number) => j<{ ok: true }>(`/folders/${id}`, { method: "DELETE" }),

  notes: (folder_id?: number | null) =>
    j<NoteMeta[]>(`/notes${folder_id != null ? `?folder_id=${folder_id}` : ""}`),
  note: (id: number) => j<Note>(`/notes/${id}`),
  createNote: (folder_id: number | null, title = "Untitled", body = "") =>
    j<Note>("/notes", { method: "POST", body: JSON.stringify({ folder_id, title, body }) }),
  updateNote: (id: number, patch: Partial<Pick<Note, "title" | "body" | "folder_id">>) =>
    j<Note>(`/notes/${id}`, { method: "PATCH", body: JSON.stringify(patch) }),
  deleteNote: (id: number) => j<{ ok: true }>(`/notes/${id}`, { method: "DELETE" }),

  syncStatus: () => j<SyncStatus>("/sync/status"),
  syncNow: () => j<{ ok: true }>("/sync/now", { method: "POST" }),
};

export type SyncStatus = {
  enabled: boolean;
  url: string | null;
  last_pull_ts: number;
  last_push_ts: number;
  last_error: string | null;
  in_flight: boolean;
  pending: number;
  server_ts: number;
};
