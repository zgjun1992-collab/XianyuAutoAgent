const crypto = require('node:crypto')
const fs = require('node:fs')
const path = require('node:path')

const ALLOWED_STORE_EXTENSIONS = new Set(['.xlsx', '.xlsm', '.csv', '.txt'])

function stageStoreImport(sourcePath, userDataPath) {
  const source = path.resolve(String(sourcePath || ''))
  const sourceStat = fs.statSync(source)
  if (!sourceStat.isFile()) throw new Error('选择的门店表格不是有效文件')
  const extension = path.extname(source).toLowerCase()
  if (!ALLOWED_STORE_EXTENSIONS.has(extension)) {
    throw new Error('门店文件仅支持 xlsx、xlsm、csv 或 txt')
  }

  const importRoot = path.resolve(userDataPath, 'imports')
  fs.mkdirSync(importRoot, { recursive: true })
  const originalName = path.basename(source).replace(/[<>:"/\\|?*\u0000-\u001f]/g, '_')
  const target = path.join(importRoot, `${Date.now()}-${crypto.randomBytes(4).toString('hex')}-${originalName}`)
  fs.copyFileSync(source, target)
  const targetStat = fs.statSync(target)
  if (!targetStat.isFile() || targetStat.size !== sourceStat.size) {
    throw new Error('门店表格复制到软件本地目录失败，请重新选择')
  }
  return target
}

module.exports = { ALLOWED_STORE_EXTENSIONS, stageStoreImport }
