const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("noted", {
  backendPort: () => ipcRenderer.invoke("backend-port"),
});
