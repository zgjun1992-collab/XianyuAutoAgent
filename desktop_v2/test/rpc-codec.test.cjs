const assert = require('node:assert/strict')
const test = require('node:test')
const { stringifyAsciiJson } = require('../electron/rpc-codec.cjs')

test('encodes Chinese Windows paths as ASCII-only JSON', () => {
  const value = {
    id: 'request-1',
    path: '/store-lists/import',
    body: { path: 'C:\\微信文件\\朱富贵火锅[1666390887]_门店(34)(1).xlsx', name: '朱富贵火锅门店' }
  }
  const encoded = stringifyAsciiJson(value)
  assert.match(encoded, /^[\x00-\x7f]+$/)
  assert.equal(JSON.parse(encoded).body.path, value.body.path)
  assert.equal(JSON.parse(encoded).body.name, value.body.name)
})

test('preserves emoji through escaped surrogate pairs', () => {
  const value = { message: '验证完成✅' }
  assert.deepEqual(JSON.parse(stringifyAsciiJson(value)), value)
})
