const test = require('node:test')
const assert = require('node:assert/strict')
const { parseTasklistPids, staleBackendPids } = require('../electron/backend-process-policy.cjs')

test('parses matching tasklist rows and ignores localized no-task text', () => {
  const output = [
    '"xianyu-v3-backend.exe","26284","Console","1","52,000 K"',
    '"other.exe","100","Console","1","1,000 K"',
    '信息: 没有运行的任务匹配指定标准。',
    '"xianyu-v3-backend.exe","44660","Console","1","51,000 K"'
  ].join('\r\n')
  assert.deepEqual(parseTasklistPids(output, 'xianyu-v3-backend.exe'), [26284, 44660])
  assert.deepEqual(staleBackendPids(output, 'xianyu-v3-backend.exe', 44660), [26284])
})
