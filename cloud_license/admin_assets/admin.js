let csrfToken = ''
let selectedUserId = null

const byId = (id) => document.getElementById(id)
const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]))
const formatTime = (value) => value ? new Date(value).toLocaleString('zh-CN', {hour12:false}) : '—'
const planNames = {trial:'3天体验版', weekly:'周卡', monthly:'月卡', quarterly:'季卡', yearly:'年卡'}

function message(text, ok=true) {
  const node = byId('message')
  node.textContent = text
  node.className = `toast${ok ? '' : ' bad'}`
  window.clearTimeout(message.timer)
  message.timer = window.setTimeout(() => node.classList.add('hidden'), 4500)
}

async function api(method, path, body) {
  const headers = {'Accept':'application/json'}
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  if (!['GET','HEAD'].includes(method) && csrfToken) headers['X-CSRF-Token'] = csrfToken
  const response = await fetch(path, {method, headers, credentials:'same-origin', body:body === undefined ? undefined : JSON.stringify(body)})
  let payload
  try { payload = await response.json() } catch (_error) { payload = {ok:false,error:`服务器响应异常 ${response.status}`} }
  if (!response.ok || !payload.ok) {
    if ([401,403].includes(response.status) && path !== '/v1/admin/auth/login') showLogin()
    throw new Error(payload.error || '操作失败')
  }
  return payload.data
}

function showLogin() {
  csrfToken = ''
  byId('loginPanel').classList.remove('hidden')
  byId('dashboard').classList.add('hidden')
  byId('sessionActions').classList.add('hidden')
}

function showDashboard(admin) {
  byId('adminName').textContent = admin.username
  byId('loginPanel').classList.add('hidden')
  byId('dashboard').classList.remove('hidden')
  byId('sessionActions').classList.remove('hidden')
}

async function restoreSession() {
  try {
    const result = await api('GET', '/v1/admin/auth/me')
    csrfToken = result.csrf_token
    showDashboard(result.admin)
    await Promise.all([loadUsers(), loadAudit()])
  } catch (_error) { showLogin() }
}

async function login(event) {
  event.preventDefault()
  try {
    const result = await api('POST', '/v1/admin/auth/login', {username:byId('adminUsername').value,password:byId('adminPassword').value})
    csrfToken = result.csrf_token
    byId('adminPassword').value = ''
    showDashboard(result.admin)
    await Promise.all([loadUsers(), loadAudit()])
    message('登录成功')
  } catch (error) { message(error.message, false) }
}

async function logout() {
  try { await api('POST', '/v1/admin/auth/logout', {}) } catch (_error) {}
  showLogin()
}

async function createUser(event) {
  event.preventDefault()
  try {
    await api('POST', '/v1/admin/users', {username:byId('username').value,password:byId('password').value,display_name:byId('displayName').value})
    byId('createUserForm').reset()
    await Promise.all([loadUsers(), loadAudit()])
    message('客户创建成功，请安全地把初始密码交给客户')
  } catch (error) { message(error.message, false) }
}

async function loadUsers() {
  const rows = await api('GET', '/v1/admin/users')
  byId('users').innerHTML = rows.map((user) => `<tr><td>${user.id}</td><td><b>${escapeHtml(user.display_name || user.username)}</b><br><small>${escapeHtml(user.username)}</small></td><td class="status-${escapeHtml(user.status)}">${escapeHtml(user.status)}</td><td>${escapeHtml(planNames[user.plan_code] || user.plan_code || '未开通')}</td><td>${escapeHtml(formatTime(user.expires_at))}</td><td><div class="actions"><button data-action="grant" data-id="${user.id}" data-plan="trial">体验</button><button data-action="grant" data-id="${user.id}" data-plan="weekly">周卡</button><button data-action="grant" data-id="${user.id}" data-plan="monthly">月卡</button><button data-action="grant" data-id="${user.id}" data-plan="quarterly">季卡</button><button data-action="grant" data-id="${user.id}" data-plan="yearly">年卡</button><button class="secondary" data-action="devices" data-id="${user.id}" data-name="${escapeHtml(user.display_name || user.username)}">设备</button><button class="danger" data-action="status" data-id="${user.id}" data-status="${user.status === 'active' ? 'frozen' : 'active'}">${user.status === 'active' ? '冻结' : '恢复'}</button></div></td></tr>`).join('') || '<tr><td colspan="6" class="muted">暂无客户</td></tr>'
}

async function grant(userId, plan) {
  if (!window.confirm(`确认开通或续费${planNames[plan] || plan}？`)) return
  await api('POST', `/v1/admin/users/${userId}/subscription`, {plan_code:plan})
  await Promise.all([loadUsers(), loadAudit()])
  message('订阅开通或续费成功')
}

async function setStatus(userId, status) {
  if (!window.confirm(`确认将客户状态修改为 ${status}？`)) return
  await api('POST', `/v1/admin/users/${userId}/status`, {status})
  await Promise.all([loadUsers(), loadAudit()])
  message('客户状态已更新')
}

async function loadDevices(userId, owner='') {
  selectedUserId = userId
  byId('deviceOwner').textContent = `${owner} · 用户ID ${userId}`
  const rows = await api('GET', `/v1/admin/users/${userId}/devices`)
  byId('devices').innerHTML = rows.map((device) => `<tr><td>${escapeHtml(device.device_name || '未命名设备')}</td><td><code>${escapeHtml(device.device_id)}</code></td><td class="status-${escapeHtml(device.status)}">${escapeHtml(device.status)}</td><td>${escapeHtml(formatTime(device.first_seen_at))}</td><td>${escapeHtml(formatTime(device.last_seen_at))}</td><td>${device.status === 'active' ? `<button class="danger compact" data-action="revoke-device" data-device="${escapeHtml(device.device_id)}">解绑</button>` : '—'}</td></tr>`).join('') || '<tr><td colspan="6" class="muted">该客户暂无设备</td></tr>'
}

async function revokeDevice(deviceId) {
  if (!selectedUserId || !window.confirm('确认解绑此设备？原设备下一次联网后将失效。')) return
  await api('POST', `/v1/admin/users/${selectedUserId}/devices/revoke`, {device_id:deviceId})
  await Promise.all([loadDevices(selectedUserId, byId('deviceOwner').textContent.split(' · ')[0]), loadAudit()])
  message('设备已解绑')
}

async function loadAudit() {
  const rows = await api('GET', '/v1/admin/audit-logs?limit=200')
  byId('audit').innerHTML = rows.map((row) => `<tr><td>${escapeHtml(formatTime(row.created_at))}</td><td>${escapeHtml(row.actor)}</td><td>${escapeHtml(row.action)}</td><td>${escapeHtml(row.target)}</td><td>${escapeHtml(row.detail)}</td></tr>`).join('') || '<tr><td colspan="5" class="muted">暂无记录</td></tr>'
}

async function changePassword(event) {
  event.preventDefault()
  try {
    await api('POST', '/v1/admin/auth/change-password', {current_password:byId('currentAdminPassword').value,new_password:byId('newAdminPassword').value})
    byId('changePasswordForm').reset()
    showLogin()
    message('密码已修改，请重新登录')
  } catch (error) { message(error.message, false) }
}

document.addEventListener('DOMContentLoaded', () => {
  byId('loginForm').addEventListener('submit', login)
  byId('logoutButton').addEventListener('click', logout)
  byId('createUserForm').addEventListener('submit', createUser)
  byId('changePasswordForm').addEventListener('submit', changePassword)
  byId('refreshUsersButton').addEventListener('click', () => loadUsers().catch((error) => message(error.message, false)))
  byId('refreshAuditButton').addEventListener('click', () => loadAudit().catch((error) => message(error.message, false)))
  byId('users').addEventListener('click', (event) => {
    const button = event.target.closest('button[data-action]')
    if (!button) return
    const action = button.dataset.action
    const task = action === 'grant' ? grant(Number(button.dataset.id), button.dataset.plan) : action === 'status' ? setStatus(Number(button.dataset.id), button.dataset.status) : loadDevices(Number(button.dataset.id), button.dataset.name)
    Promise.resolve(task).catch((error) => message(error.message, false))
  })
  byId('devices').addEventListener('click', (event) => {
    const button = event.target.closest('button[data-action="revoke-device"]')
    if (button) revokeDevice(button.dataset.device).catch((error) => message(error.message, false))
  })
  restoreSession()
})
