const http = require('node:http')

function requestLocalBackend(port, requestPath, options = {}) {
  const method = options.method || 'GET'
  const body = options.body === undefined ? null : String(options.body)
  const headers = { ...(options.headers || {}) }
  if (body !== null && !Object.keys(headers).some((name) => name.toLowerCase() === 'content-length')) {
    headers['Content-Length'] = Buffer.byteLength(body)
  }

  return new Promise((resolve, reject) => {
    const request = http.request({
      hostname: '127.0.0.1',
      port: Number(port),
      path: requestPath,
      method,
      headers,
      agent: false
    }, (response) => {
      const chunks = []
      response.on('data', (chunk) => chunks.push(chunk))
      response.on('end', () => {
        const text = Buffer.concat(chunks).toString('utf8')
        resolve({
          ok: Number(response.statusCode) >= 200 && Number(response.statusCode) < 300,
          status: Number(response.statusCode || 0),
          json: async () => JSON.parse(text),
          text: async () => text
        })
      })
    })
    request.setTimeout(Number(options.timeoutMs || 3000), () => {
      request.destroy(new Error(`本地后台请求超时：${requestPath}`))
    })
    request.on('error', reject)
    if (body !== null) request.write(body)
    request.end()
  })
}

module.exports = { requestLocalBackend }
