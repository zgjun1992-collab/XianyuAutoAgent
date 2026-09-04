$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$pythonPath = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Local Python environment was not found."
}

& $pythonPath -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --onedir `
    --name "XianyuCardAI" `
    --add-data "prompts;prompts" `
    --add-data ".env.example;." `
    "desktop_app.py"

Write-Host "Build complete: dist\XianyuCardAI\XianyuCardAI.exe" -ForegroundColor Green
