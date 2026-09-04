$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$pythonPath = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$nodePath = "C:\Users\ASUS\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin"
$pnpmPath = "C:\Users\ASUS\.cache\codex-runtimes\codex-primary-runtime\dependencies\bin\fallback\pnpm.cmd"
$electronCache = Join-Path (Split-Path $PSScriptRoot -Parent) "work\electron-cache"
$builderCache = Join-Path (Split-Path $PSScriptRoot -Parent) "work\electron-builder-cache"

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "未找到本地 Python 环境，请先安装 requirements-dev.txt。"
}
if (-not (Test-Path -LiteralPath $pnpmPath)) {
    throw "未找到桌面构建运行时。"
}

& $pythonPath -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --console `
    --name "xianyu-v2-backend" `
    --distpath "dist-v2-backend" `
    --workpath "build-v2-backend" `
    --specpath "build-v2-backend" `
    --add-data "$PSScriptRoot\prompts;prompts" `
    "$PSScriptRoot\v2_backend.py"
if ($LASTEXITCODE -ne 0) {
    throw "Python 后端打包失败，退出码：$LASTEXITCODE"
}

$env:PATH = "$nodePath;$env:PATH"
$env:electron_config_cache = $electronCache
$env:ELECTRON_CACHE = $electronCache
$env:ELECTRON_BUILDER_CACHE = $builderCache
Push-Location "desktop_v2"
try {
    & $pnpmPath run build
    if ($LASTEXITCODE -ne 0) {
        throw "Electron 安装包构建失败，退出码：$LASTEXITCODE"
    }
} finally {
    Pop-Location
}

Write-Host "V2安装包已生成：desktop_v2\release" -ForegroundColor Green
