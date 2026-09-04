$ErrorActionPreference = "Stop"
Set-Location -LiteralPath (Join-Path $PSScriptRoot "desktop_v2")
$nodePath = "C:\Users\ASUS\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin"
$pnpmPath = "C:\Users\ASUS\.cache\codex-runtimes\codex-primary-runtime\dependencies\bin\fallback\pnpm.cmd"
$env:PATH = "$nodePath;$env:PATH"
$env:XIANYU_LICENSE_SERVER_URL = "http://127.0.0.1:8787"
& $pnpmPath run dev
