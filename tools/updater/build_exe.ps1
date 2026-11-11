# Builds dist\DimensionHopper.exe (single file, no console). Run from tools\updater:
#   powershell -ExecutionPolicy Bypass -File build_exe.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path .venv)) { py -3 -m venv .venv }
.\.venv\Scripts\python -m pip install -q --disable-pip-version-check -r requirements.txt
.\.venv\Scripts\pyinstaller --noconfirm --clean --onefile --windowed `
    --name DimensionHopper --icon portal.ico --add-data "portal.ico;." `
    --collect-all esptool dimension_hopper.py
Write-Host "Built: $PSScriptRoot\dist\DimensionHopper.exe"
