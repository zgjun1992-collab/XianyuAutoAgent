function normalizeServiceStatus(status) {
  const value = String(status || '').trim().toLowerCase()
  const labels = {
    connected: '客服运行中',
    starting: '客服启动中',
    reconnecting: '客服重连中',
    verification_required: '等待闲鱼安全验证',
    stopped: '客服已停止',
    error: '客服异常'
  }
  return labels[value] || '状态未知'
}

function isRecordedBackend(record, identity) {
  return Boolean(
    record && identity &&
    Number.isInteger(Number(record.pid)) && Number(record.pid) > 0 &&
    Number.isInteger(Number(record.port)) && Number(record.port) > 0 &&
    Number(identity.pid) === Number(record.pid) &&
    String(record.edition || '') === 'V3.6' &&
    String(identity.edition || '') === String(record.edition || '') &&
    String(identity.version || '') === String(record.version || '')
  )
}

module.exports = { normalizeServiceStatus, isRecordedBackend }
