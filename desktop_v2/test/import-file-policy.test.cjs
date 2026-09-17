const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const test = require('node:test')
const { stageStoreImport } = require('../electron/import-file-policy.cjs')

test('stages a temporary WeChat store file under persistent app data', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'xianyu-import-test-'))
  const sourceRoot = path.join(root, 'RWTemp')
  const userData = path.join(root, 'user-data')
  fs.mkdirSync(sourceRoot)
  const source = path.join(sourceRoot, '韩国料理门店(1).xlsx')
  fs.writeFileSync(source, Buffer.from('xlsx-placeholder'))

  const staged = stageStoreImport(source, userData)
  fs.unlinkSync(source)

  assert.equal(path.dirname(staged), path.join(userData, 'imports'))
  assert.equal(fs.readFileSync(staged, 'utf8'), 'xlsx-placeholder')
  assert.match(path.basename(staged), /韩国料理门店\(1\)\.xlsx$/)
})

test('rejects unsupported import extensions', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'xianyu-import-test-'))
  const source = path.join(root, 'stores.exe')
  fs.writeFileSync(source, 'not-a-store-file')
  assert.throws(() => stageStoreImport(source, root), /仅支持/)
})
