$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$pythonPath = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$nodePath = "C:\Users\ASUS\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin"
$electronCache = Join-Path (Split-Path $PSScriptRoot -Parent) "work\electron-cache"
$builderCache = Join-Path (Split-Path $PSScriptRoot -Parent) "work\electron-builder-cache"

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Local Python environment was not found. Install requirements-dev.txt first."
}
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot "desktop_v2\node_modules\vite\bin\vite.js"))) {
    throw "Desktop dependencies were not found. Run pnpm install in desktop_v2 first."
}

# Keep release output separate from the development backend. The latter may be
# running locally and Windows will then refuse PyInstaller's overwrite.
& $pythonPath -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --console `
    --name "xianyu-v3-backend" `
    --distpath "dist-v3-release-backend" `
    --workpath "build-v3-backend" `
    --specpath "build-v3-backend" `
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
    & node ".\node_modules\vite\bin\vite.js" build
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

Write-Host "V3.5 installer generated under desktop_v2\release-v3.5" -ForegroundColor Green
