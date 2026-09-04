$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$pythonPath = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$nodePath = "C:\Users\ASUS\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin"
$pnpmPath = "C:\Users\ASUS\.cache\codex-runtimes\codex-primary-runtime\dependencies\bin\fallback\pnpm.cmd"
$electronCache = Join-Path (Split-Path $PSScriptRoot -Parent) "work\electron-cache"
$builderCache = Join-Path (Split-Path $PSScriptRoot -Parent) "work\electron-builder-cache"

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Local Python environment was not found. Install requirements-dev.txt first."
}
if (-not (Test-Path -LiteralPath $pnpmPath)) {
    throw "Desktop build runtime was not found."
}

& $pythonPath -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --console `
    --name "xianyu-cloud-test-backend" `
    --distpath "dist-cloud-backend" `
    --workpath "build-cloud-backend" `
    --specpath "build-cloud-backend" `
    --add-data "$PSScriptRoot\prompts;prompts" `
    "$PSScriptRoot\v2_backend.py"
if ($LASTEXITCODE -ne 0) {
    throw "Python backend build failed with exit code $LASTEXITCODE"
}

$env:PATH = "$nodePath;$env:PATH"
$env:CI = "true"
$env:electron_config_cache = $electronCache
$env:ELECTRON_CACHE = $electronCache
$env:ELECTRON_BUILDER_CACHE = $builderCache
Push-Location "desktop_v2"
try {
    & $pnpmPath run build:web
    if ($LASTEXITCODE -ne 0) {
        throw "Desktop web build failed with exit code $LASTEXITCODE"
    }

    # electron-builder 26 cannot currently read pnpm 11's dependency database on
    # this Windows runtime. The packaged app has no production Node dependency,
    # so use the checked-in empty npm collector response during packaging.
    $lockPath = Join-Path (Get-Location) "pnpm-lock.yaml"
    $heldLockPath = Join-Path (Get-Location) "pnpm-lock.build-hold.yaml"
    Move-Item -LiteralPath $lockPath -Destination $heldLockPath
    try {
        $env:PATH = "$(Join-Path (Get-Location) 'build-tools');$nodePath;$env:PATH"
        & node ".\node_modules\electron-builder\out\cli\cli.js" --win nsis --publish never
        if ($LASTEXITCODE -ne 0) {
            throw "Electron installer build failed with exit code $LASTEXITCODE"
        }
    } finally {
        Move-Item -LiteralPath $heldLockPath -Destination $lockPath
    }
} finally {
    Pop-Location
}

Write-Host "V3.6 cloud test installer generated under desktop_v2\release-cloud-test" -ForegroundColor Green
