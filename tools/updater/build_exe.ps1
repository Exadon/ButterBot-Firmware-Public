# Builds dist\DimensionHopper.exe (single file, no console). Run from tools\updater:
#   powershell -ExecutionPolicy Bypass -File build_exe.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path .venv)) { py -3 -m venv .venv }
$exe = Join-Path $PSScriptRoot "dist\DimensionHopper.exe"
if (Get-Process DimensionHopper -ErrorAction SilentlyContinue) {
    throw "DimensionHopper.exe is running - close it first (Windows locks a running exe, so it can't be replaced)."
}
.\.venv\Scripts\python -m pip install -q --disable-pip-version-check -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
$before = if (Test-Path $exe) { (Get-Item $exe).LastWriteTime } else { [datetime]::MinValue }
.\.venv\Scripts\pyinstaller --noconfirm --clean --onefile --windowed `
    --name DimensionHopper --icon portal.ico --add-data "portal.ico;." --add-data "bot.png;." --add-data "controller.png;." `
    --collect-all esptool dimension_hopper.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
if (-not (Test-Path $exe) -or (Get-Item $exe).LastWriteTime -le $before) { throw "dist\DimensionHopper.exe was not updated" }
Write-Host "Built: $exe"
