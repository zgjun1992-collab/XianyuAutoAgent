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
$sourceDesktop = Join-Path $PSScriptRoot "desktop_v2"
$sourceBackend = Join-Path $PSScriptRoot "dist-v3-release-backend"
$sourceElectronDist = Join-Path $sourceDesktop "node_modules\electron\dist"
$releaseTarget = Join-Path $sourceDesktop "release-v3.5"
$stageRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("xya-v35-build-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
$stageDesktop = Join-Path $stageRoot "desktop_v2"
$stageBackend = Join-Path $stageRoot "dist-v3-release-backend"

Push-Location $sourceDesktop
try {
    & $pnpmPath run build:web
    if ($LASTEXITCODE -ne 0) {
        throw "Desktop web build failed with exit code $LASTEXITCODE"
    }
    $pnpmStore = (& $pnpmPath store path).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $pnpmStore) {
        throw "Unable to locate the pnpm package store."
    }
} finally {
    Pop-Location
}

# pnpm 11 may intentionally skip dependency install scripts. In that case the
# Electron package exists but its runtime archive has not been expanded yet.
# Restore the pinned runtime from the local cache so release builds stay
# reproducible and do not require a network download.
if (-not (Test-Path -LiteralPath (Join-Path $sourceElectronDist "electron.exe"))) {
    $electronArchive = $null
    $electronCacheRoots = @($electronCache)
    if ($env:LOCALAPPDATA) {
        $electronCacheRoots += (Join-Path $env:LOCALAPPDATA "electron\Cache")
    }
    foreach ($cacheRoot in $electronCacheRoots) {
        if (-not (Test-Path -LiteralPath $cacheRoot)) {
            continue
        }
        $electronArchive = Get-ChildItem -LiteralPath $cacheRoot -Recurse -File -Filter "electron-v44.1.0-win32-x64.zip" |
            Select-Object -First 1
        if ($electronArchive) {
            break
        }
    }
    if (-not $electronArchive) {
        throw "Electron 44.1.0 runtime archive was not found in the configured caches."
    }
    if (Test-Path -LiteralPath $sourceElectronDist) {
        Remove-Item -LiteralPath $sourceElectronDist -Recurse -Force
    }
    New-Item -ItemType Directory -Path $sourceElectronDist -Force | Out-Null
    Expand-Archive -LiteralPath $electronArchive.FullName -DestinationPath $sourceElectronDist -Force
    if (-not (Test-Path -LiteralPath (Join-Path $sourceElectronDist "electron.exe"))) {
        throw "Electron runtime archive was found but could not be restored."
    }
}

try {
    New-Item -ItemType Directory -Path $stageDesktop -Force | Out-Null
    $excludedDirectories = @(
        (Join-Path $sourceDesktop "node_modules"),
        (Join-Path $sourceDesktop "release"),
        (Join-Path $sourceDesktop "release-v3.5")
    )
    & robocopy.exe $sourceDesktop $stageDesktop /E /NFL /NDL /NJH /NJS /NP /XD $excludedDirectories
    if ($LASTEXITCODE -gt 7) {
        throw "Desktop staging copy failed with exit code $LASTEXITCODE"
    }
    Copy-Item -LiteralPath $sourceBackend -Destination $stageBackend -Recurse

    Push-Location $stageDesktop
    try {
        & $pnpmPath install --frozen-lockfile --offline --store-dir $pnpmStore
        if ($LASTEXITCODE -ne 0) {
            throw "Staged desktop dependency install failed with exit code $LASTEXITCODE"
        }

        $stageElectronDist = Join-Path $stageDesktop "node_modules\electron\dist"
        if (-not (Test-Path -LiteralPath (Join-Path $stageElectronDist "electron.exe"))) {
            if (-not (Test-Path -LiteralPath (Join-Path $sourceElectronDist "electron.exe"))) {
                throw "Electron runtime was not found. Run the Electron install script or provide a local Electron 44.1.0 runtime."
            }
            Copy-Item -LiteralPath $sourceElectronDist -Destination $stageElectronDist -Recurse
        }

        # electron-builder 26 cannot currently read pnpm 11's dependency database on
        # this Windows runtime. The packaged app has no production Node dependency,
        # so use the checked-in empty npm collector response during packaging.
        $lockPath = Join-Path $stageDesktop "pnpm-lock.yaml"
        $heldLockPath = Join-Path $stageDesktop "pnpm-lock.build-hold.yaml"
        Move-Item -LiteralPath $lockPath -Destination $heldLockPath
        try {
            $env:PATH = "$(Join-Path $stageDesktop 'build-tools');$nodePath;$env:PATH"
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

    $installer = Get-ChildItem -LiteralPath (Join-Path $stageDesktop "release-v3.5") -File |
        Where-Object { $_.Name -like "XianyuCardAI-V3.5-Setup-*.exe" -and $_.Name -notlike "*.__uninstaller.exe" } |
        Select-Object -First 1
    if (-not $installer) {
        throw "The final installer was not generated."
    }
    if (Test-Path -LiteralPath $releaseTarget) {
        $expectedRelease = [System.IO.Path]::GetFullPath((Join-Path $sourceDesktop "release-v3.5"))
        $actualRelease = [System.IO.Path]::GetFullPath($releaseTarget)
        if (-not $actualRelease.Equals($expectedRelease, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to clean an unexpected release directory: $actualRelease"
        }
        Remove-Item -LiteralPath $releaseTarget -Recurse -Force
    }
    New-Item -ItemType Directory -Path $releaseTarget | Out-Null
    Copy-Item -LiteralPath $installer.FullName -Destination $releaseTarget
} finally {
    if (Test-Path -LiteralPath $stageRoot) {
        $tempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
        $actualStage = [System.IO.Path]::GetFullPath($stageRoot)
        if (-not $actualStage.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to clean an unexpected staging directory: $actualStage"
        }
        Remove-Item -LiteralPath $stageRoot -Recurse -Force
    }
}

Write-Host "V3.5 installer generated under desktop_v2\release-v3.5" -ForegroundColor Green
