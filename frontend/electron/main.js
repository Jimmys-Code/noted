const { app, BrowserWindow, ipcMain } = require("electron");
const path = require("path");
const fs = require("fs");
const { spawn } = require("child_process");
const net = require("net");

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
  win = new BrowserWindow({
    width: 1280,
    height: 820,
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
  win.once("ready-to-show", () => win.show());

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
