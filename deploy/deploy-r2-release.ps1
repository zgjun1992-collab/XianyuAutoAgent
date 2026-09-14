param(
    [string]$Bucket = "xianyu-releases",
    [string]$Channel = "v3.6",
    [string]$ReleaseDirectory = (Join-Path $PSScriptRoot "..\desktop_v2\release-cloud-prod"),
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$releasePath = [System.IO.Path]::GetFullPath($ReleaseDirectory)
$latestPath = Join-Path $releasePath "latest.yml"

if (-not (Test-Path -LiteralPath $latestPath -PathType Leaf)) {
    throw "latest.yml was not found under $releasePath"
}

$latestText = Get-Content -LiteralPath $latestPath -Raw
$pathMatch = [regex]::Match($latestText, '(?m)^path:\s*(.+?)\s*$')
if (-not $pathMatch.Success) {
    throw "latest.yml does not contain a release path."
}

$installerName = $pathMatch.Groups[1].Value.Trim('"', "'")
$installerPath = Join-Path $releasePath $installerName
$blockmapPath = "$installerPath.blockmap"

foreach ($requiredPath in @($installerPath, $blockmapPath, $latestPath)) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "Required release artifact was not found: $requiredPath"
    }
}

$artifacts = @(
    @{
        File = $installerPath
        Key = "$Channel/$installerName"
        ContentType = "application/vnd.microsoft.portable-executable"
        CacheControl = "public, max-age=31536000, immutable"
    },
    @{
        File = $blockmapPath
        Key = "$Channel/$installerName.blockmap"
        ContentType = "application/octet-stream"
        CacheControl = "public, max-age=31536000, immutable"
    },
    @{
        File = $latestPath
        Key = "$Channel/latest.yml"
        ContentType = "text/yaml; charset=utf-8"
        CacheControl = "no-store, max-age=0"
    }
)

foreach ($artifact in $artifacts) {
    $objectName = "$Bucket/$($artifact.Key)"
    $arguments = @(
        "wrangler@latest", "r2", "object", "put", $objectName,
        "--file", $artifact.File,
        "--content-type", $artifact.ContentType,
        "--cache-control", $artifact.CacheControl,
        "--remote"
    )

    if ($DryRun) {
        Write-Host "DRY RUN: pnpm dlx $($arguments -join ' ')"
        continue
    }

    & pnpm dlx @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Upload failed for $objectName with exit code $LASTEXITCODE"
    }
}

Write-Host "Release uploaded in safe order: installer, blockmap, latest.yml." -ForegroundColor Green
