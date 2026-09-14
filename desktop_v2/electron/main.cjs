const { app, BrowserWindow, WebContentsView, ipcMain, session, safeStorage, dialog, Notification, shell, Menu } = require('electron')
const { spawn } = require('child_process')
const fs = require('fs')
const net = require('net')
const os = require('os')
const path = require('path')
const crypto = require('crypto')
const { autoUpdater } = require('./generated-updater.cjs')
const { normalizeReleaseNotes, isForcedUpdate, publicUpdateState } = require('./update-policy.cjs')

console.error('XianyuCardAI V3 main process starting')
process.on('uncaughtException', (error) => console.error('V3 uncaughtException:', error))
process.on('unhandledRejection', (error) => console.error('V3 unhandledRejection:', error))
process.on('exit', (code) => console.error('V3 process exit:', code))
// Some Windows GPU drivers fail before Electron's ready event when the GPU
// process sandbox is enabled. This does not disable the renderer sandbox used
// by the embedded Goofish page.
if (process.platform === 'win32') {
  app.commandLine.appendSwitch('disable-gpu-sandbox')
  console.error('V3 compatibility: GPU sandbox disabled')
}
// Chromium follows the Windows Internet Settings proxy, which can retain a
// dead local port after proxy software changes ports.  Node/Python already use
// HTTPS_PROXY/HTTP_PROXY, so prefer that live runtime proxy for the embedded
// Goofish page as well.  Credentials are never logged or persisted here.
const runtimeProxy = process.env.HTTPS_PROXY || process.env.HTTP_PROXY || ''
if (runtimeProxy) {
  try {
    const parsedProxy = new URL(runtimeProxy)
    if (['http:', 'https:', 'socks:', 'socks5:'].includes(parsedProxy.protocol)) {
      const proxyOrigin = `${parsedProxy.protocol}//${parsedProxy.hostname}${parsedProxy.port ? `:${parsedProxy.port}` : ''}`
      app.commandLine.appendSwitch('proxy-server', proxyOrigin)
      console.error(`V3 proxy: using runtime proxy ${parsedProxy.hostname}:${parsedProxy.port || 'default'}`)
    }
  } catch (error) {
    console.error('V3 proxy: ignored invalid runtime proxy', error?.message || String(error))
  }
}
// Only one desktop main process may own the Xianyu WebSocket/backend.  Without
// this lock, double-clicking the shortcut twice starts two independent backend
// processes and both of them can reply to the same buyer message.
const gotSingleInstanceLock = app.requestSingleInstanceLock()
if (!gotSingleInstanceLock) {
  console.error('V3 single-instance: another instance is already running')
  app.quit()
}
for (const eventName of ['will-finish-launching', 'ready', 'before-quit', 'will-quit', 'quit']) {
  app.on(eventName, (_event, exitCode) => console.error(`V3 lifecycle: ${eventName}`, exitCode ?? ''))
}

let mainWindow = null
let goofishView = null
let backendProcess = null
let backendPort = null
let backendReady = false
let backendIdentity = null
let lastPendingCount = 0
let cookieTimer = null
let goofishSession = null
let updateCheckWasManual = false
let promptedUpdateVersion = ''
let promptedInstallVersion = ''

const updaterState = {
  status: 'idle',
  currentVersion: app.getVersion(),
  availableVersion: '',
  progress: 0,
  releaseNotes: '',
  error: '',
  forced: false,
  checkedAt: ''
}

const isDev = !app.isPackaged
const projectRoot = path.resolve(__dirname, '..', '..')
const OFFICIAL_LICENSE_SERVER_URL = 'https://api.yituan123.com'
const DEFAULT_LICENSE_SERVER_URL = isDev
  ? (process.env.XIANYU_LICENSE_SERVER_URL || OFFICIAL_LICENSE_SERVER_URL)
  : OFFICIAL_LICENSE_SERVER_URL

function settingsPath() {
  return path.join(app.getPath('userData'), 'desktop-settings.json')
}

function readSettings() {
  try {
    return JSON.parse(fs.readFileSync(settingsPath(), 'utf8'))
  } catch (_error) {
    return { base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen-plus' }
  }
}

function writeSettings(value) {
  fs.mkdirSync(path.dirname(settingsPath()), { recursive: true })
  fs.writeFileSync(settingsPath(), JSON.stringify(value, null, 2), 'utf8')
}

function appendUpdateLog(level, message, detail = '') {
  const line = `${new Date().toISOString()} [${level}] ${message}${detail ? ` ${String(detail).replace(/[\r\n]+/g, ' ')}` : ''}\n`
  try {
    const logDir = path.join(app.getPath('userData'), 'logs')
    fs.mkdirSync(logDir, { recursive: true })
    fs.appendFileSync(path.join(logDir, 'updater.log'), line, 'utf8')
  } catch (error) {
    console.error('V3 updater: failed to write log', error?.message || String(error))
  }
  console.error(`V3 updater ${level}: ${message}`, detail)
}

function setUpdaterState(status, patch = {}) {
  Object.assign(updaterState, patch, { status })
  mainWindow?.webContents.send('app:event', { type: 'update-state', state: publicUpdateState(updaterState) })
  return publicUpdateState(updaterState)
}

function forceUpdatePending() {
  return Boolean(
    updaterState.forced &&
    updaterState.availableVersion &&
    ['available', 'downloading', 'downloaded', 'error'].includes(updaterState.status)
  )
}

async function promptUpdateAvailable(info) {
  const version = String(info?.version || '')
  if (!mainWindow || !version || promptedUpdateVersion === version) return
  promptedUpdateVersion = version
  const forced = isForcedUpdate(info)
  const buttons = forced ? ['立即下载', '退出软件'] : ['稍后更新', '立即下载']
  const result = await dialog.showMessageBox(mainWindow, {
    type: forced ? 'warning' : 'info',
    title: forced ? '发现必须安装的更新' : '发现新版本',
    message: `发现新版本 ${version}`,
    detail: `${normalizeReleaseNotes(info?.releaseNotes) || '本次版本包含功能改进和问题修复。'}${forced ? '\n\n这是强制更新，安装前不能启动自动客服。' : ''}`,
    buttons,
    defaultId: forced ? 0 : 1,
    cancelId: forced ? 1 : 0,
    noLink: true
  })
  if ((forced && result.response === 0) || (!forced && result.response === 1)) {
    await downloadAppUpdate()
  } else if (forced) {
    app.quit()
  }
}

async function promptUpdateDownloaded(info) {
  const version = String(info?.version || updaterState.availableVersion || '')
  if (!mainWindow || !version || promptedInstallVersion === version) return
  promptedInstallVersion = version
  const buttons = updaterState.forced ? ['立即重启安装', '退出软件'] : ['稍后安装', '立即重启安装']
  const result = await dialog.showMessageBox(mainWindow, {
    type: 'info',
    title: '更新下载完成',
    message: `新版本 ${version} 已下载完成`,
    detail: '立即安装会先停止自动客服和本地后台，然后关闭软件、安装更新并重新启动。',
    buttons,
    defaultId: updaterState.forced ? 0 : 1,
    cancelId: updaterState.forced ? 1 : 0,
    noLink: true
  })
  if ((updaterState.forced && result.response === 0) || (!updaterState.forced && result.response === 1)) {
    await installDownloadedUpdate()
  } else if (updaterState.forced) {
    app.quit()
  }
}

function registerUpdater() {
  autoUpdater.autoDownload = false
  autoUpdater.autoInstallOnAppQuit = false
  autoUpdater.allowPrerelease = false
  autoUpdater.fullChangelog = true
  autoUpdater.logger = {
    info: (...args) => appendUpdateLog('INFO', args.join(' ')),
    warn: (...args) => appendUpdateLog('WARN', args.join(' ')),
    error: (...args) => appendUpdateLog('ERROR', args.join(' ')),
    debug: (...args) => appendUpdateLog('DEBUG', args.join(' '))
  }
  autoUpdater.on('checking-for-update', () => {
    setUpdaterState('checking', { error: '', progress: 0 })
  })
  autoUpdater.on('update-available', (info) => {
    const releaseNotes = normalizeReleaseNotes(info?.releaseNotes)
    const forced = isForcedUpdate(info)
    setUpdaterState('available', {
      availableVersion: String(info?.version || ''),
      releaseNotes,
      forced,
      checkedAt: new Date().toISOString(),
      error: ''
    })
    appendUpdateLog('INFO', `update available ${info?.version || ''}`, forced ? 'forced' : 'optional')
    promptUpdateAvailable(info).catch((error) => appendUpdateLog('ERROR', 'update prompt failed', error?.message || error))
  })
  autoUpdater.on('update-not-available', (info) => {
    setUpdaterState('latest', {
      availableVersion: '',
      releaseNotes: '',
      forced: false,
      checkedAt: new Date().toISOString(),
      error: ''
    })
    appendUpdateLog('INFO', `already current ${info?.version || app.getVersion()}`)
    if (updateCheckWasManual && mainWindow) {
      dialog.showMessageBox(mainWindow, { type: 'info', title: '检查更新', message: '当前已经是最新版本。', buttons: ['确定'] })
    }
  })
  autoUpdater.on('download-progress', (progress) => {
    setUpdaterState('downloading', { progress: Number(progress?.percent || 0), error: '' })
  })
  autoUpdater.on('update-downloaded', (info) => {
    setUpdaterState('downloaded', { progress: 100, error: '' })
    appendUpdateLog('INFO', `update downloaded ${info?.version || ''}`)
    promptUpdateDownloaded(info).catch((error) => appendUpdateLog('ERROR', 'install prompt failed', error?.message || error))
  })
  autoUpdater.on('error', (error) => {
    setUpdaterState('error', { error: error?.message || String(error) })
    appendUpdateLog('ERROR', 'update operation failed', error?.stack || error?.message || error)
  })
}

async function checkForAppUpdate(manual = false) {
  updateCheckWasManual = Boolean(manual)
  if (isDev) {
    const state = setUpdaterState('unsupported', { error: '开发模式不检查远程更新，请使用正式安装版验证。' })
    if (manual) throw new Error(state.error)
    return state
  }
  if (['checking', 'downloading', 'installing'].includes(updaterState.status)) return publicUpdateState(updaterState)
  setUpdaterState('checking', { error: '', progress: 0 })
  appendUpdateLog('INFO', manual ? 'manual update check started' : 'startup update check started')
  await autoUpdater.checkForUpdates()
  return publicUpdateState(updaterState)
}

async function downloadAppUpdate() {
  if (isDev) throw new Error('开发模式不能下载更新')
  if (!updaterState.availableVersion) throw new Error('当前没有可下载的新版本')
  setUpdaterState('downloading', { progress: 0, error: '' })
  appendUpdateLog('INFO', `update download started ${updaterState.availableVersion}`)
  await autoUpdater.downloadUpdate()
  return publicUpdateState(updaterState)
}

async function stopBackendForUpdate() {
  if (backendReady) {
    try {
      await requestBackend('POST', '/service/stop', {})
      appendUpdateLog('INFO', 'customer service stopped before update')
    } catch (error) {
      appendUpdateLog('WARN', 'failed to stop service gracefully', error?.message || error)
    }
  }
  if (!backendProcess || backendProcess.killed) return
  const processToStop = backendProcess
  await new Promise((resolve) => {
    let settled = false
    const finish = () => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      resolve()
    }
    const timer = setTimeout(finish, 5000)
    processToStop.once('exit', finish)
    processToStop.kill()
  })
  backendReady = false
  backendIdentity = null
  backendProcess = null
  appendUpdateLog('INFO', 'backend process stopped before update')
}

async function installDownloadedUpdate() {
  if (isDev) throw new Error('开发模式不能安装更新')
  if (updaterState.status !== 'downloaded') throw new Error('更新尚未下载完成')
  setUpdaterState('installing', { error: '' })
  await stopBackendForUpdate()
  appendUpdateLog('INFO', `restarting to install ${updaterState.availableVersion}`)
  setImmediate(() => autoUpdater.quitAndInstall(false, true))
  return publicUpdateState(updaterState)
}

function encryptSecret(value) {
  if (!value) return ''
  if (!safeStorage.isEncryptionAvailable()) throw new Error('Windows 安全存储暂不可用')
  return safeStorage.encryptString(value).toString('base64')
}

function decryptSecret(value) {
  if (!value) return ''
  try {
    return safeStorage.decryptString(Buffer.from(value, 'base64'))
  } catch (_error) {
    return ''
  }
}

function licenseSettings() {
  const saved = readSettings()
  let changed = false
  if (!saved.device_id) {
    saved.device_id = crypto.randomUUID()
    changed = true
  }
  if (!isDev && saved.license_server_url !== DEFAULT_LICENSE_SERVER_URL) {
    saved.license_server_url = DEFAULT_LICENSE_SERVER_URL
    delete saved.license_token_encrypted
    delete saved.license_snapshot_encrypted
    delete saved.license_username
    changed = true
  }
  if (changed) writeSettings(saved)
  return saved
}

async function licenseRequest(requestPath, body) {
  const saved = licenseSettings()
  const baseUrl = String(saved.license_server_url || DEFAULT_LICENSE_SERVER_URL).replace(/\/$/, '')
  const response = await fetch(`${baseUrl}${requestPath}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(12000)
  })
  const payload = await response.json()
  if (!response.ok || !payload.ok) {
    const error = new Error(payload.error || `授权服务器错误 ${response.status}`)
    error.authoritative = true
    throw error
  }
  return payload.data
}

async function licenseLogin(incoming) {
  const saved = licenseSettings()
  if (isDev && incoming.server_url) {
    const requested = new URL(String(incoming.server_url).trim())
    const localHttp = requested.protocol === 'http:' && ['127.0.0.1', 'localhost'].includes(requested.hostname)
    if (requested.protocol !== 'https:' && !localHttp) throw new Error('授权服务器必须使用HTTPS')
    saved.license_server_url = requested.origin
  } else {
    saved.license_server_url = DEFAULT_LICENSE_SERVER_URL
  }
  const result = await licenseRequest('/v1/auth/login', {
    username: incoming.username,
    password: incoming.password,
    device_id: saved.device_id,
    device_name: os.hostname()
  })
  saved.license_token_encrypted = encryptSecret(result.token)
  saved.license_username = result.user.username
  saved.license_snapshot_encrypted = encryptSecret(JSON.stringify({
    user: result.user,
    entitlement: result.entitlement,
    checked_at: new Date().toISOString()
  }))
  writeSettings(saved)
  return { ...result, token: undefined, server_url: saved.license_server_url, device_id: saved.device_id, mode: 'online' }
}

function cachedLicense(saved) {
  try {
    const value = JSON.parse(decryptSecret(saved.license_snapshot_encrypted))
    const ageHours = (Date.now() - new Date(value.checked_at).getTime()) / 3600000
    if (value.entitlement?.active && ageHours >= 0 && ageHours <= 48 && new Date(value.entitlement.expires_at) > new Date()) {
      return { ...value, server_url: saved.license_server_url, device_id: saved.device_id, mode: 'offline_grace', grace_hours_left: Math.max(0, 48 - ageHours) }
    }
  } catch (_error) {}
  return null
}

async function licenseStatus() {
  const saved = licenseSettings()
  const token = decryptSecret(saved.license_token_encrypted)
  if (!token) return { active: false, logged_in: false, server_url: saved.license_server_url || DEFAULT_LICENSE_SERVER_URL, device_id: saved.device_id }
  try {
    const result = await licenseRequest('/v1/license/verify', { token, device_id: saved.device_id })
    saved.license_snapshot_encrypted = encryptSecret(JSON.stringify(result))
    writeSettings(saved)
    return { ...result, active: true, logged_in: true, server_url: saved.license_server_url, device_id: saved.device_id, mode: 'online' }
  } catch (error) {
    if (!error.authoritative) {
      const cached = cachedLicense(saved)
      if (cached) return { ...cached, active: true, logged_in: true }
    }
    return { active: false, logged_in: true, error: error.message, server_url: saved.license_server_url, device_id: saved.device_id }
  }
}

function licenseLogout() {
  const saved = licenseSettings()
  delete saved.license_token_encrypted
  delete saved.license_snapshot_encrypted
  delete saved.license_username
  writeSettings(saved)
  return { active: false, logged_in: false, server_url: saved.license_server_url, device_id: saved.device_id }
}

async function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer()
    server.unref()
    server.on('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const port = server.address().port
      server.close(() => resolve(port))
    })
  })
}

function canReachProxy(host, port, timeout = 450) {
  return new Promise((resolve) => {
    const socket = net.createConnection({ host, port: Number(port) })
    let settled = false
    const finish = (value) => {
      if (settled) return
      settled = true
      socket.destroy()
      resolve(value)
    }
    socket.setTimeout(timeout)
    socket.once('connect', () => finish(true))
    socket.once('timeout', () => finish(false))
    socket.once('error', () => finish(false))
  })
}

async function configureGoofishProxy(goofishSession) {
  const candidates = []
  const addCandidate = (host, port, rule, source) => {
    if (!host || !port || candidates.some((item) => item.host === host && item.port === String(port))) return
    candidates.push({ host, port: String(port), rule, source })
  }

  for (const value of [process.env.HTTPS_PROXY, process.env.HTTP_PROXY]) {
    if (!value) continue
    try {
      const parsed = new URL(value)
      const port = parsed.port || (parsed.protocol === 'https:' ? '443' : '80')
      addCandidate(parsed.hostname, port, `${parsed.protocol}//${parsed.hostname}:${port}`, 'environment')
    } catch (_error) {}
  }

  try {
    const resolved = await goofishSession.resolveProxy('https://www.goofish.com/im')
    for (const match of resolved.matchAll(/(?:PROXY|HTTPS?|SOCKS5?)\s+([^:;\s]+):(\d+)/gi)) {
      addCandidate(match[1], match[2], `http://${match[1]}:${match[2]}`, 'system')
    }
  } catch (error) {
    console.error('V3 proxy: resolveProxy failed', error?.message || String(error))
  }

  // Local proxy clients commonly expose a mixed HTTP port here. Probe only
  // loopback so this cannot redirect the embedded browser to a remote host.
  for (const port of ['7897', '7890', '10809', '10808']) {
    addCandidate('127.0.0.1', port, `http://127.0.0.1:${port}`, 'local-probe')
  }

  for (const candidate of candidates) {
    if (!await canReachProxy(candidate.host, candidate.port)) continue
    await goofishSession.setProxy({ mode: 'fixed_servers', proxyRules: candidate.rule })
    // The Python receiver must use the same reachable route as the embedded
    // workbench.  Set this before spawning it so token and WebSocket traffic
    // does not hang on a dead system proxy or an unavailable direct route.
    process.env.HTTP_PROXY = candidate.rule
    process.env.HTTPS_PROXY = candidate.rule
    console.error(`V3 proxy: embedded workbench uses ${candidate.host}:${candidate.port} (${candidate.source})`)
    return candidate
  }

  await goofishSession.setProxy({ mode: 'direct' })
  console.error('V3 proxy: no reachable proxy found; embedded workbench uses direct connection')
  return null
}

async function requestBackend(method, requestPath, body) {
  if (!backendReady) await waitForBackend()
  if (method === 'POST' && requestPath === '/service/start') {
    if (forceUpdatePending()) throw new Error(`必须先安装新版本 ${updaterState.availableVersion}，当前不能启动自动客服`)
    const currentLicense = await licenseStatus()
    if (!currentLicense.active) throw new Error(currentLicense.error || '请先登录并开通有效套餐')
  }
  const response = await fetch(`http://127.0.0.1:${backendPort}${requestPath}`, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body)
  })
  const payload = await response.json()
  if (!response.ok || !payload.ok) throw new Error(payload.error || `本地服务错误 ${response.status}`)
  return payload.data
}

async function waitForBackend() {
  const started = Date.now()
  while (Date.now() - started < 25000) {
    try {
      const response = await fetch(`http://127.0.0.1:${backendPort}/health`)
      if (response.ok) {
        const payload = await response.json()
        const identity = payload?.data || {}
        const expectedVersion = app.getVersion()
        if (identity.edition !== 'V3.6' || identity.version !== expectedVersion) {
          throw new Error(
            `前后端版本不一致：桌面端 V3.6/${expectedVersion}，` +
            `后台 ${identity.edition || '未知版本'}/${identity.version || '未知版本'}。` +
            '请关闭旧程序后重新安装当前版本。'
          )
        }
        backendIdentity = identity
        backendReady = true
        return
      }
    } catch (error) {
      if (String(error?.message || error).includes('前后端版本不一致')) throw error
    }
    await new Promise((resolve) => setTimeout(resolve, 250))
  }
  throw new Error('本地AI服务启动超时')
}

async function startBackend() {
  backendPort = await freePort()
  const dataDir = path.join(app.getPath('userData'), 'data')
  let executable
  let args
  if (isDev) {
    executable = path.join(projectRoot, '.venv', 'Scripts', 'python.exe')
    args = [path.join(projectRoot, 'v2_backend.py'), '--port', String(backendPort), '--data-dir', dataDir]
  } else {
    executable = path.join(process.resourcesPath, 'backend', 'xianyu-cloud-preview-backend.exe')
    args = ['--port', String(backendPort), '--data-dir', dataDir]
  }
  backendProcess = spawn(executable, args, {
    cwd: isDev ? projectRoot : path.dirname(executable),
    windowsHide: true,
    stdio: ['ignore', 'pipe', 'pipe']
  })
  backendProcess.stdout.on('data', (chunk) => {
    const text = chunk.toString()
    if (text.includes('"ready": true')) backendReady = true
  })
  backendProcess.stderr.on('data', (chunk) => {
    mainWindow?.webContents.send('app:event', { type: 'backend-log', message: chunk.toString() })
  })
  backendProcess.on('exit', (code) => {
    backendReady = false
    backendIdentity = null
    mainWindow?.webContents.send('app:event', { type: 'backend-exit', code })
  })
  await waitForBackend()
  await pushRuntimeConfig()
}

async function pushRuntimeConfig() {
  const saved = readSettings()
  await requestBackend('POST', '/config/runtime', {
    api_key: decryptSecret(saved.api_key_encrypted),
    cookie: decryptSecret(saved.cookie_encrypted),
    base_url: saved.base_url || 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    model: saved.model || 'qwen-plus'
  })
}

async function syncGoofishCookie() {
  if (!goofishView) return { saved: false, count: 0 }
  const cookies = await goofishView.webContents.session.cookies.get({})
  const relevant = cookies.filter((item) => {
    const domain = (item.domain || '').replace(/^\./, '')
    return domain === 'goofish.com' || domain.endsWith('.goofish.com')
  })
  if (!relevant.length) return { saved: false, count: 0 }
  const cookie = relevant.map((item) => `${item.name}=${item.value}`).join('; ')
  const saved = readSettings()
  saved.cookie_encrypted = encryptSecret(cookie)
  saved.cookie_updated_at = new Date().toISOString()
  writeSettings(saved)
  await requestBackend('POST', '/config/runtime', { cookie })
  mainWindow?.webContents.send('app:event', { type: 'cookie-synced', count: relevant.length, at: saved.cookie_updated_at })
  return { saved: true, count: relevant.length, at: saved.cookie_updated_at }
}

async function createGoofishView() {
  goofishView = new WebContentsView({
    webPreferences: {
      session: goofishSession,
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
      devTools: false
    }
  })
  goofishView.setBackgroundColor('#f7f3e8')
  mainWindow.contentView.addChildView(goofishView)
  goofishView.setBounds({ x: 228, y: 80, width: 900, height: 700 })
  goofishView.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith('https://www.goofish.com/')) {
      goofishView.webContents.loadURL(url)
    } else {
      shell.openExternal(url)
    }
    return { action: 'deny' }
  })
  goofishView.webContents.on('did-finish-load', () => {
    mainWindow?.webContents.send('app:event', { type: 'browser-loaded', url: goofishView.webContents.getURL() })
    clearTimeout(cookieTimer)
    cookieTimer = setTimeout(() => syncGoofishCookie().catch(() => {}), 1200)
  })
  goofishSession.cookies.on('changed', () => {
    clearTimeout(cookieTimer)
    cookieTimer = setTimeout(() => syncGoofishCookie().catch(() => {}), 1500)
  })
  goofishView.webContents.loadURL('https://www.goofish.com/im')
}

async function createWindow() {
  Menu.setApplicationMenu(null)
  mainWindow = new BrowserWindow({
    width: 1540,
    height: 980,
    minWidth: 1180,
    minHeight: 760,
    backgroundColor: '#f4efe2',
    title: '闲鱼卡券 AI 客服 V3.6 云端测试版',
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true
    }
  })
  if (isDev) await mainWindow.loadURL('http://127.0.0.1:5173')
  else await mainWindow.loadFile(path.join(__dirname, '..', 'dist', 'index.html'))
  await createGoofishView()
  mainWindow.on('closed', () => {
    if (goofishView && !goofishView.webContents.isDestroyed()) goofishView.webContents.close()
    goofishView = null
    mainWindow = null
  })
}

function registerIpc() {
  ipcMain.handle('backend:request', (_event, payload) => requestBackend(payload.method || 'GET', payload.path, payload.body))
  ipcMain.handle('license:login', (_event, payload) => licenseLogin(payload || {}))
  ipcMain.handle('license:status', () => licenseStatus())
  ipcMain.handle('license:logout', () => licenseLogout())
  ipcMain.handle('app:version', () => ({
    edition: 'V3.6',
    frontend_version: app.getVersion(),
    backend_version: backendIdentity?.version || '',
    build_commit: backendIdentity?.build_commit || ''
  }))
  ipcMain.handle('update:state', () => publicUpdateState(updaterState))
  ipcMain.handle('update:check', () => checkForAppUpdate(true))
  ipcMain.handle('update:download', () => downloadAppUpdate())
  ipcMain.handle('update:install', () => installDownloadedUpdate())
  ipcMain.on('browser:set-bounds', (_event, bounds) => {
    if (!goofishView) return
    const safe = {
      x: Math.max(0, Math.round(bounds.x || 0)),
      y: Math.max(0, Math.round(bounds.y || 0)),
      width: Math.max(0, Math.round(bounds.width || 0)),
      height: Math.max(0, Math.round(bounds.height || 0))
    }
    goofishView.setBounds(safe)
  })
  ipcMain.handle('browser:action', async (_event, action) => {
    if (!goofishView) return null
    const command = typeof action === 'string' ? action : action?.action
    if (command === 'back' && goofishView.webContents.canGoBack()) goofishView.webContents.goBack()
    if (command === 'forward' && goofishView.webContents.canGoForward()) goofishView.webContents.goForward()
    if (command === 'reload') goofishView.webContents.reload()
    if (command === 'home') await goofishView.webContents.loadURL('https://www.goofish.com/im')
    if (command === 'navigate') {
      const target = String(action?.url || '')
      const parsed = new URL(target)
      if (!['www.goofish.com', 'h5.m.goofish.com', '2.taobao.com'].includes(parsed.hostname)) {
        throw new Error('仅允许在内置浏览器中打开闲鱼页面')
      }
      await goofishView.webContents.loadURL(parsed.toString())
    }
    return { url: goofishView.webContents.getURL() }
  })
  ipcMain.handle('dialog:choose-excel', async () => {
    const result = await dialog.showOpenDialog(mainWindow, {
      title: '选择可用门店文件',
      properties: ['openFile'],
      filters: [{ name: '门店文件', extensions: ['xlsx', 'xlsm', 'csv', 'txt'] }]
    })
    return result.canceled ? '' : result.filePaths[0]
  })
  ipcMain.handle('dialog:choose-image', async () => {
    const result = await dialog.showOpenDialog(mainWindow, {
      title: '选择套餐图片',
      properties: ['openFile'],
      filters: [{ name: '套餐图片', extensions: ['png', 'jpg', 'jpeg', 'webp'] }]
    })
    return result.canceled ? '' : result.filePaths[0]
  })
  ipcMain.handle('asset:preview', async (_event, filePath) => {
    const assetRoot = path.resolve(app.getPath('userData'), 'data', 'product-images')
    const resolved = path.resolve(String(filePath || ''))
    if (!resolved.startsWith(`${assetRoot}${path.sep}`)) throw new Error('图片路径不在软件数据目录中')
    const extension = path.extname(resolved).toLowerCase()
    const mime = extension === '.png' ? 'image/png' : extension === '.webp' ? 'image/webp' : 'image/jpeg'
    return `data:${mime};base64,${fs.readFileSync(resolved).toString('base64')}`
  })
  ipcMain.handle('config:get', async () => {
    const saved = readSettings()
    return {
      api_key_saved: Boolean(saved.api_key_encrypted),
      cookie_saved: Boolean(saved.cookie_encrypted),
      cookie_updated_at: saved.cookie_updated_at || '',
      base_url: saved.base_url || 'https://dashscope.aliyuncs.com/compatible-mode/v1',
      model: saved.model || 'qwen-plus'
    }
  })
  ipcMain.handle('config:save', async (_event, incoming) => {
    const saved = readSettings()
    if (incoming.api_key) saved.api_key_encrypted = encryptSecret(String(incoming.api_key).trim())
    if (incoming.base_url) saved.base_url = String(incoming.base_url).trim()
    if (incoming.model) saved.model = String(incoming.model).trim()
    writeSettings(saved)
    await pushRuntimeConfig()
    return { ok: true, api_key_saved: Boolean(saved.api_key_encrypted) }
  })
  ipcMain.handle('cookie:sync', () => syncGoofishCookie())
}

async function monitorReviews() {
  if (!backendReady) return
  try {
    const snapshot = await requestBackend('GET', '/snapshot')
    const count = snapshot.dashboard.audits.pending || 0
    if (count > lastPendingCount && Notification.isSupported()) {
      new Notification({ title: '有回复需要审核', body: `当前有 ${count} 条高风险回复等待确认` }).show()
    }
    lastPendingCount = count
  } catch (_error) {}
}

if (gotSingleInstanceLock) app.whenReady().then(async () => {
  try {
    console.error('V3 stage: app ready')
    registerIpc()
    registerUpdater()
    console.error('V3 stage: IPC ready')
    goofishSession = session.fromPartition('persist:xianyu-main')
    await configureGoofishProxy(goofishSession)
    console.error('V3 stage: network route ready')
    await startBackend()
    console.error('V3 stage: backend ready')
    await createWindow()
    console.error('V3 stage: window ready')
    setTimeout(() => checkForAppUpdate(false).catch((error) => appendUpdateLog('ERROR', 'startup update check failed', error?.message || error)), 5000)
    setInterval(() => syncGoofishCookie().catch(() => {}), 30000)
    setInterval(monitorReviews, 2500)
    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow()
    })
  } catch (error) {
    console.error('V3 startup failed:', error)
    dialog.showErrorBox('闲鱼卡券AI客服启动失败', error?.stack || String(error))
    app.quit()
  }
})

app.on('second-instance', () => {
  if (!mainWindow) return
  if (mainWindow.isMinimized()) mainWindow.restore()
  mainWindow.show()
  mainWindow.focus()
})

app.on('before-quit', () => {
  if (backendProcess && !backendProcess.killed) backendProcess.kill()
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})
