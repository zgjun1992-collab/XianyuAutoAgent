function parseTasklistPids(output, imageName) {
  const expected = String(imageName || '').trim().toLowerCase()
  if (!expected) return []
  const pids = []
  for (const line of String(output || '').split(/\r?\n/)) {
    const match = line.trim().match(/^"([^"]+)","(\d+)"/)
    if (!match || match[1].toLowerCase() !== expected) continue
    const pid = Number(match[2])
    if (Number.isInteger(pid) && pid > 0 && !pids.includes(pid)) pids.push(pid)
  }
  return pids
}

function staleBackendPids(output, imageName, keepPid = 0) {
  const current = Number(keepPid || 0)
  return parseTasklistPids(output, imageName).filter((pid) => pid !== current)
}

module.exports = { parseTasklistPids, staleBackendPids }
