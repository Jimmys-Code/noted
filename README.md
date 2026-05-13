# noted

A native-feel markdown notes app for Linux with vim motions, color-coded folders, and a collapsible sidebar.

- **Backend:** Python + FastAPI + SQLite (data at `~/.local/share/noted/noted.db`)
- **Frontend:** Electron + React + Vite + TypeScript
- **Editor:** CodeMirror 6 with real vim keybindings
- **Fonts:** Inter (UI), JetBrains Mono (editor)

## First-time setup

```bash
cd ~/jimmys_projects/noted
backend/.venv/bin/pip install -r backend/requirements.txt
cd frontend && npm install
```

## Run (dev)

```bash
cd ~/jimmys_projects/noted/frontend
npm run dev
```

Electron launches the Python backend automatically.

## Shortcuts

- `Ctrl+B` toggle sidebar
- `Ctrl+N` new note
- `Ctrl+P` toggle preview
- Folder dot → click to recolor · double-click name to rename
