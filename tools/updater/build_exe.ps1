# Builds dist\DimensionHopper.exe (single file, no console). Run from tools\updater:
#   powershell -ExecutionPolicy Bypass -File build_exe.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path .venv)) {
    # CI (actions/setup-python) says which Python to use; locally, 3.13 - the speech-to-text packages
    # (ctranslate2, av, onnxruntime) publish Windows wheels up to 3.13, so "the newest Python 3" may not work.
    if ($env:pythonLocation) { & (Join-Path $env:pythonLocation "python.exe") -m venv .venv }
    else { py -3.13 -m venv .venv; if ($LASTEXITCODE -ne 0) { py -3 -m venv .venv } }
    if ($LASTEXITCODE -ne 0) { throw "couldn't create the Python environment" }
}
Write-Host "Python: $(.\.venv\Scripts\python --version 2>&1)"
$exe = Join-Path $PSScriptRoot "dist\DimensionHopper.exe"
if (Get-Process DimensionHopper -ErrorAction SilentlyContinue) {
    throw "DimensionHopper.exe is running - close it first (Windows locks a running exe, so it can't be replaced)."
}
.\.venv\Scripts\python -m pip install -q --disable-pip-version-check -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
# Voice-command list for the Guide tab, regenerated from the firmware sources on every build
.\.venv\Scripts\python gen_guide_data.py
if ($LASTEXITCODE -ne 0) { throw "gen_guide_data.py failed" }
$before = if (Test-Path $exe) { (Get-Item $exe).LastWriteTime } else { [datetime]::MinValue }
.\.venv\Scripts\pyinstaller --noconfirm --clean --onefile --windowed `
    --name DimensionHopper --icon portal.ico --add-data "portal.ico;." --add-data "bot.png;." --add-data "controller.png;." `
    --add-data "bot_guide.png;." --add-data "controller_guide.png;." --add-data "guide_data.json;." `
    --collect-all esptool --collect-all numpy --collect-all faster_whisper --collect-all ctranslate2 --collect-all tokenizers `
    --collect-submodules anthropic --collect-submodules keyring --hidden-import keyring.backends.Windows `
    dimension_hopper.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
if (-not (Test-Path $exe) -or (Get-Item $exe).LastWriteTime -le $before) { throw "dist\DimensionHopper.exe was not updated" }
Write-Host "Built: $exe"
