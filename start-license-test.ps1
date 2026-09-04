param([int]$Port = 8787)
$ErrorActionPreference = "Stop"
$dbPath = Join-Path $PSScriptRoot "work\cloud-license-test.db"
New-Item -ItemType Directory -Path (Split-Path $dbPath -Parent) -Force | Out-Null
$env:LICENSE_COOKIE_SECURE = "false"
$env:LICENSE_ALLOWED_HOSTS = "127.0.0.1,localhost"
& "$PSScriptRoot\.venv\Scripts\python.exe" -m cloud_license.server --host 127.0.0.1 --port $Port --db $dbPath
