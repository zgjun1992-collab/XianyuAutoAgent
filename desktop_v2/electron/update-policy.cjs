const FORCE_UPDATE_MARKERS = ['【强制更新】', '[force-update]']

function normalizeReleaseNotes(value) {
  if (typeof value === 'string') return value.trim()
  if (!Array.isArray(value)) return ''
  return value
    .map((entry) => typeof entry === 'string' ? entry : entry?.note)
    .filter(Boolean)
    .join('\n')
    .trim()
}

function isForcedUpdate(info) {
  const notes = normalizeReleaseNotes(info?.releaseNotes).toLowerCase()
  return FORCE_UPDATE_MARKERS.some((marker) => notes.includes(marker.toLowerCase()))
}

function publicUpdateState(state) {
  return {
    status: state.status,
    currentVersion: state.currentVersion,
    availableVersion: state.availableVersion,
    progress: state.progress,
    releaseNotes: state.releaseNotes,
    error: state.error,
    forced: Boolean(state.forced),
    checkedAt: state.checkedAt
  }
}

module.exports = { FORCE_UPDATE_MARKERS, normalizeReleaseNotes, isForcedUpdate, publicUpdateState }
