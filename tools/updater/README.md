# Dimension Hopper

![portal](portal.png)

A one-window firmware updater for **ButterBot** and its **controller**, with a mad-science look. It wraps `esptool`, so nobody has to type `write_flash` again.

## What it does

- **Finds the boards.** *Scan dimensions* lists Espressif USB devices (VID `303A`) and probes each one. Both boards are ESP32-S3, so the probe tells them apart by flash size: 16 MB is the robot, 4 MB is the controller. Each board is dropped into the right slot automatically.
- **Picks a payload per board:**
  - *Latest GitHub release*: the stock CircuitMess repos by default. Type your fork's `owner/repo` to ship your own releases. Downloads are cached.
  - *Local .bin / backup*: any merged full-flash image written at `0x0`, including a backup this tool made.
  - *idf.py build folder*: point it at a project's `build/` folder and it flashes every part listed in `flasher_args.json` (bootloader, partition table, app, SPIFFS, models). This is the dev loop for the fork.
- **Refuses foot-guns.** It checks the ESP image magic, won't put a robot image (>4 MB) on the controller or the other way round, and won't write past the end of the chip's flash.
- **Backs up first** (on by default). The whole flash is read to `%LOCALAPPDATA%\DimensionHopper\backups`. If the backup comes back short, it doesn't flash.
- **Verifies** every region after writing (esptool MD5 check), then reboots the board.
- **Update both** runs the robot and then the controller in one go.

After a robot flash, press its power button. The firmware powers itself off if the button isn't held at boot.

## Build the exe

```powershell
powershell -ExecutionPolicy Bypass -File build_exe.ps1
```

This produces `dist\DimensionHopper.exe`: a single file with no console and no Python needed on the target PC.

## Run from source

```powershell
py -3 -m venv .venv; .\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python dimension_hopper.py
```

Dev flags: `--selftest <out.txt>` checks the bundled esptool and stubs (handy for the windowed exe), and `--screenshot <out.png> [secs]` renders the window and exits.

## Files

| File | Purpose |
|---|---|
| `hopper_core.py` | Ports, probe, GitHub releases, image checks, backup and write. No UI, so it can be tested headless. |
| `dimension_hopper.py` | Tk UI: the animated portal, device slots, job runner and log. |
| `build_exe.ps1` | Reproducible PyInstaller build. |
| `portal.ico` / `portal.png` | Procedurally drawn icon. |
