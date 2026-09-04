param(
    [string]$AdminKey = "change-this-private-admin-key-2026",
    [int]$Port = 8787
)
$ErrorActionPreference = "Stop"
$env:LICENSE_ADMIN_KEY = $AdminKey
$dbPath = Join-Path $PSScriptRoot "work\cloud-license-test.db"
New-Item -ItemType Directory -Path (Split-Path $dbPath -Parent) -Force | Out-Null
& "$PSScriptRoot\.venv\Scripts\python.exe" -m cloud_license.server --host 127.0.0.1 --port $Port --db $dbPath
