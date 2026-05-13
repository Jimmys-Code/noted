const { app, BrowserWindow, ipcMain } = require("electron");
const path = require("path");
const { spawn } = require("child_process");
const http = require("http");

const PORT = 8765;
const isDev = !app.isPackaged && process.env.NODE_ENV !== "production";

let backend = null;
let win = null;

function startBackend() {
  const projectRoot = path.join(__dirname, "..", "..");
  const py = path.join(projectRoot, "backend", ".venv", "bin", "python");
  const script = path.join(projectRoot, "backend", "app.py");
  backend = spawn(py, [script], {
    env: { ...process.env, NOTED_PORT: String(PORT) },
    stdio: "inherit",
  });
  backend.on("exit", (code) => console.log("backend exit", code));
}

function waitForBackend(retries = 50) {
  return new Promise((resolve, reject) => {
    const tick = () => {
      const req = http.get(`http://127.0.0.1:${PORT}/health`, (res) => {
        if (res.statusCode === 200) return resolve();
        retry();
      });
      req.on("error", retry);
    };
    const retry = () => {
      if (--retries <= 0) return reject(new Error("backend timeout"));
      setTimeout(tick, 200);
    };
    tick();
  });
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
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  if (isDev) {
    win.loadURL("http://localhost:5173");
  } else {
    win.loadFile(path.join(__dirname, "..", "dist", "index.html"));
  }
}

ipcMain.handle("backend-port", () => PORT);

app.whenReady().then(async () => {
  startBackend();
  try {
    await waitForBackend();
  } catch (e) {
    console.error(e);
  }
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
