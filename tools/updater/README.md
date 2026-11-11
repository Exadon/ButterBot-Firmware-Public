# Dimension Hopper

![portal](portal.png)

A one-window firmware updater for **ButterBot** and its **controller**, with a mad-science look. It wraps `esptool`, so nobody has to type `write_flash` again.

## What it does

- **Finds the boards.** *Scan dimensions* probes every USB serial port (the robot shows up as a CH340, Bluetooth ports are skipped). Both boards are ESP32-S3, so the probe tells them apart by flash size: 16 MB is the robot, 4 MB is the controller. Each board is dropped into the right slot automatically.
- **Keeps the robot awake.** The robot's USB chip runs off the robot's own power, and its firmware switches the robot off if it boots without the power button held. So after a probe or backup the chip stays parked in bootloader mode instead of rebooting. Turn the robot on (hold power ~2 s) *before* scanning.
- **Picks a payload per board:**
  - *Latest GitHub release*: pick from a dropdown with the stock CircuitMess repo and this fork (`Exadon/ButterBot-Firmware-Public`). A **STOCK** / **FORK** badge shows which you're about to flash, and the confirmation dialog spells it out too. Add other repos under **Repos…** (`owner/name` or a GitHub URL); they're saved and appear in the dropdown. Downloads are cached.
  - *Local .bin / backup*: any merged full-flash image written at `0x0`, including a backup this tool made. A badge says what it is (**BACKUP** with its date, **RELEASE**, **IMAGE**), and the firmware version inside it is shown.
  - *idf.py build folder*: point it at a project's `build/` folder and it flashes every part listed in `flasher_args.json` (bootloader, partition table, app, SPIFFS, models). **Use this fork's build** picks `<repo>/build` in one click (badge: **FORK BUILD**). This is the dev loop for the fork.
- **Refuses foot-guns.** A file flashed at `0x0` must be a complete image: bootloader, a partition table at `0x8000` and an app at `0x10000`. A bare app `.bin` from a build folder, a lone bootloader or a data partition is marked **NOT FLASHABLE** (writing one at `0x0` bricks the board until it's reflashed). It also won't put a robot image on the controller or the other way round (judged by the project name inside the image), and won't write past the end of the chip's flash.
- **Backs up first** (on by default). The whole flash is read to `%LOCALAPPDATA%\DimensionHopper\backups`. If the backup comes back short, it doesn't flash.
- **Verifies** every region after writing (esptool MD5 check), then reboots the board.
- **Update both** runs the robot and then the controller in one go.

After a robot flash, hold its power button to turn it on: the new firmware also powers off if the button isn't held at boot. Windows may hand the robot a different COM number after it power-cycles; just Scan again.

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
| `make_art.py` | Regenerates the art: `portal.ico`/`portal.png` (app icon), `bot.png` and `controller.png` (slot icons). |
