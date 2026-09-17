const test = require('node:test')
const assert = require('node:assert/strict')
const { normalizeServiceStatus, isRecordedBackend } = require('../electron/tray-policy.cjs')

test('formats customer service status for the tray menu', () => {
  assert.equal(normalizeServiceStatus('connected'), '客服运行中')
  assert.equal(normalizeServiceStatus('verification_required'), '等待闲鱼安全验证')
  assert.equal(normalizeServiceStatus('stopped'), '客服已停止')
  assert.equal(normalizeServiceStatus('unexpected'), '状态未知')
})

test('only accepts the exact recorded V3.6 backend identity', () => {
  const record = { pid: 1234, port: 56042, edition: 'V3.6', version: '0.11.5' }
  const identity = { pid: 1234, edition: 'V3.6', version: '0.11.5' }
  assert.equal(isRecordedBackend(record, identity), true)
  assert.equal(isRecordedBackend(record, { ...identity, pid: 9999 }), false)
  assert.equal(isRecordedBackend(record, { ...identity, edition: 'V3.5' }), false)
  assert.equal(isRecordedBackend(record, { ...identity, version: '0.11.4' }), false)
  assert.equal(isRecordedBackend({ ...record, pid: 0 }, identity), false)
})
