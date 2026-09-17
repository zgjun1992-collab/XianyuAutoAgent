const assert = require('node:assert/strict')
const http = require('node:http')
const test = require('node:test')

const { requestLocalBackend } = require('../electron/local-backend-client.cjs')

test('local backend requests bypass invalid proxy environment variables', async (t) => {
  const previousHttpProxy = process.env.HTTP_PROXY
  const previousHttpsProxy = process.env.HTTPS_PROXY
  process.env.HTTP_PROXY = 'http://127.0.0.1:1'
  process.env.HTTPS_PROXY = 'http://127.0.0.1:1'
  t.after(() => {
    if (previousHttpProxy === undefined) delete process.env.HTTP_PROXY
    else process.env.HTTP_PROXY = previousHttpProxy
    if (previousHttpsProxy === undefined) delete process.env.HTTPS_PROXY
    else process.env.HTTPS_PROXY = previousHttpsProxy
  })

  const server = http.createServer((request, response) => {
    response.writeHead(200, { 'Content-Type': 'application/json' })
    response.end(JSON.stringify({ ok: true, data: { status: 'ok' } }))
  })
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  t.after(() => new Promise((resolve) => server.close(resolve)))

  const response = await requestLocalBackend(server.address().port, '/health', { timeoutMs: 1000 })
  assert.equal(response.ok, true)
  assert.deepEqual(await response.json(), { ok: true, data: { status: 'ok' } })
})
