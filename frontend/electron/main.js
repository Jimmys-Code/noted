const { app, BrowserWindow, ipcMain, screen } = require("electron");
const path = require("path");
const fs = require("fs");
const os = require("os");
const { spawn } = require("child_process");
const net = require("net");

const STATE_PATH = path.join(os.homedir(), ".local/share/noted/window.json");

function loadBounds() {
  try {
    const b = JSON.parse(fs.readFileSync(STATE_PATH, "utf8"));
    if (b && typeof b.width === "number" && typeof b.height === "number") return b;
  } catch (_) {}
  return null;
}

function saveBounds(win) {
  if (!win || win.isDestroyed()) return;
  try {
    fs.mkdirSync(path.dirname(STATE_PATH), { recursive: true });
    const b = win.getBounds();
    b.maximized = win.isMaximized();
    fs.writeFileSync(STATE_PATH, JSON.stringify(b));
  } catch (_) {}
}

function clampBounds(b) {
  // If saved bounds reference a disconnected monitor, fall back to primary.
  const displays = screen.getAllDisplays();
  const onScreen = displays.some((d) => {
    const a = d.bounds;
    return b.x < a.x + a.width && b.x + b.width > a.x && b.y < a.y + a.height && b.y + b.height > a.y;
  });
  return onScreen ? b : null;
}

const PORT = 8765;
const isDev = process.env.NOTED_DEV === "1";

let backend = null;
let win = null;

function tcpAlive(port) {
  return new Promise((resolve) => {
    const s = net.createConnection({ host: "127.0.0.1", port, timeout: 200 }, () => {
      s.end();
      resolve(true);
    });
    s.on("error", () => resolve(false));
    s.on("timeout", () => { s.destroy(); resolve(false); });
  });
}

async function ensureBackend() {
  // If something (e.g. systemd user service) already owns the port, do nothing.
  if (await tcpAlive(PORT)) return;
  const projectRoot = path.join(__dirname, "..", "..");
  const py = path.join(projectRoot, "backend", ".venv", "bin", "python");
  const script = path.join(projectRoot, "backend", "app.py");
  backend = spawn(py, [script], {
    env: { ...process.env, NOTED_PORT: String(PORT) },
    stdio: "ignore",
    detached: false,
  });
  backend.on("exit", (code) => console.log("backend exit", code));
}

function createWindow() {
  const saved = loadBounds();
  const valid = saved ? clampBounds(saved) : null;
  win = new BrowserWindow({
    x: valid?.x,
    y: valid?.y,
    width: valid?.width ?? 1280,
    height: valid?.height ?? 820,
    minWidth: 720,
    minHeight: 480,
    backgroundColor: "#16171c",
    title: "noted",
    icon: path.join(__dirname, "..", "..", "icons", "noted-512.png"),
    titleBarStyle: "hiddenInset",
    autoHideMenuBar: true,
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  if (saved?.maximized) win.maximize();
  win.once("ready-to-show", () => win.show());

  // Persist bounds with a small debounce so we don't write on every pixel of a drag.
  let saveTimer = null;
  const schedule = () => {
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(() => saveBounds(win), 300);
  };
  win.on("move", schedule);
  win.on("resize", schedule);
  win.on("maximize", schedule);
  win.on("unmaximize", schedule);
  win.on("close", () => saveBounds(win));

  if (isDev) {
    win.loadURL("http://localhost:5173");
  } else {
    const indexHtml = path.join(__dirname, "..", "dist", "index.html");
    if (!fs.existsSync(indexHtml)) {
      // Fallback if user hasn't built yet — point at vite dev server.
      win.loadURL("http://localhost:5173");
    } else {
      win.loadFile(indexHtml);
    }
  }
}

ipcMain.handle("backend-port", () => PORT);

app.whenReady().then(() => {
  // Spawn backend in parallel — don't block the window.
  ensureBackend().catch((e) => console.error("backend spawn:", e));
  createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  if (backend) backend.kill();
  if (process.platform !== "darwin") app.quit();
});

app.on("before-quit", () => {
  if (backend) backend.kill();
});
