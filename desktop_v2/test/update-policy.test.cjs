const assert = require('node:assert/strict')
const test = require('node:test')
const { isForcedUpdate, normalizeReleaseNotes, publicUpdateState } = require('../electron/update-policy.cjs')

test('normalizes string and array release notes', () => {
  assert.equal(normalizeReleaseNotes('  修复门店查询  '), '修复门店查询')
  assert.equal(normalizeReleaseNotes([{ version: '0.11.4', note: '修复日期' }, { note: '优化退款' }]), '修复日期\n优化退款')
})

test('detects explicit force update markers only', () => {
  assert.equal(isForcedUpdate({ releaseNotes: '普通更新' }), false)
  assert.equal(isForcedUpdate({ releaseNotes: '【强制更新】\n修复关键问题' }), true)
  assert.equal(isForcedUpdate({ releaseNotes: '[FORCE-UPDATE] security fix' }), true)
})

test('public state does not expose internal updater objects', () => {
  const state = publicUpdateState({
    status: 'available', currentVersion: '0.11.3', availableVersion: '0.11.4',
    progress: 0, releaseNotes: '修复', error: '', forced: false,
    checkedAt: '2026-09-14T00:00:00.000Z', internal: { secret: true }
  })
  assert.equal(state.availableVersion, '0.11.4')
  assert.equal(Object.hasOwn(state, 'internal'), false)
})
