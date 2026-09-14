const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('xianyuDesktop', {
  backend: (method, path, body) => ipcRenderer.invoke('backend:request', { method, path, body }),
  licenseLogin: (payload) => ipcRenderer.invoke('license:login', payload),
  licenseStatus: () => ipcRenderer.invoke('license:status'),
  licenseLogout: () => ipcRenderer.invoke('license:logout'),
  getVersion: () => ipcRenderer.invoke('app:version'),
  getUpdateState: () => ipcRenderer.invoke('update:state'),
  checkForUpdates: () => ipcRenderer.invoke('update:check'),
  downloadUpdate: () => ipcRenderer.invoke('update:download'),
  installUpdate: () => ipcRenderer.invoke('update:install'),
  setBrowserBounds: (bounds) => ipcRenderer.send('browser:set-bounds', bounds),
  browser: (action) => ipcRenderer.invoke('browser:action', action),
  chooseExcel: () => ipcRenderer.invoke('dialog:choose-excel'),
  chooseImage: () => ipcRenderer.invoke('dialog:choose-image'),
  imagePreview: (filePath) => ipcRenderer.invoke('asset:preview', filePath),
  getConfig: () => ipcRenderer.invoke('config:get'),
  saveConfig: (config) => ipcRenderer.invoke('config:save', config),
  syncCookie: () => ipcRenderer.invoke('cookie:sync'),
  onAppEvent: (callback) => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('app:event', listener)
    return () => ipcRenderer.removeListener('app:event', listener)
  }
})
