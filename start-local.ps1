$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$pythonPath = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Local Python environment was not found. Run setup again."
}

Write-Host "Starting Xianyu AutoAgent..." -ForegroundColor Cyan
Write-Host "On first run, enter your API Key and Xianyu Cookie when prompted." -ForegroundColor Yellow
& $pythonPath "main.py"
