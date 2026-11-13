# Dimension Hopper

![portal](portal.png)

A one-window Windows app for **ButterBot** and its **controller**, with a mad-science look. It updates both boards,
sets them up, lets you test and drive them, and runs the **AI brain** that gives ButterBot real conversations.

Part of the ButterBot community fork - see the [robot firmware](../../README.md) for the overview and the
[controller firmware](https://github.com/Exadon/ButterBotCtrl-Firmware-Public).
Every button has a tooltip: hover over it to see what it does.

## Tabs

### Flash
- **Scan dimensions** finds the robot and the controller on USB and tells them apart (it listens for their status
  line first, and only probes boards that stay silent).
- **Pick a payload per board:**
  - *Latest GitHub release* from the stock CircuitMess repository or this fork (a **STOCK** / **FORK** badge shows
    which), plus any repository you add under **Repos…**. Downloads are cached.
  - *Local .bin / backup*: a full-flash image, including a backup this app made.
  - *idf.py build folder*: everything a build produced. **Use this fork's build** picks it in one click.
- **Only write what changed** (on by default): compares the board with the image in 64 KB pieces and skips what's
  already there - a typical firmware update takes about a minute.
- **Keep pairing & settings** (on by default): pairing, voice, WiFi and AI settings survive the update.
- **Refuses foot-guns**: partial images, a robot image on the controller (or the other way round), images bigger than
  the chip.
- **Backs up first** (optional) to `%LOCALAPPDATA%\DimensionHopper\backups`, **verifies** every region after writing,
  then **waits for the board to boot** and checks it's running the version it was given.
- **Update both** does the robot and then the controller.
- Per board: **Status** (battery, charging, link, firmware, free memory, why it last restarted), **Probe**, **Wake**,
  **Forget pairing**.

The robot only stays on while its power button was held at start-up, so after a flash switch it on again (hold power
about 1 second).

### Guide
Every voice command and the controller's buttons, with pictures.

### Personalize
Settings that live on the robot. Right now: its **voice** - click one of the eight presets to switch to it and hear a
sample. Classic is the stock voice.

### Diagnostics
Live readings from either board (sensors, motors, battery, link, motion events) and a **drive pad**: hold an arrow (or
use the arrow keys / WASD) to drive the robot. It won't drive forward into an obstacle or off an edge, and stops when
you let go.

### Console
The board's raw log with filters, a table of what the robot heard and matched, a box to send console commands, and
**Save log…**.

### AI - real conversations
Four steps, each with a lamp (NOT SET UP / TO DO / WORKING… / READY / PROBLEM). Steps 2-4 unlock once step 1 works.

1. **Robot WiFi** - **Scan** lists the networks the robot can see (2.4 GHz only), pick yours, type the password,
   **Send to robot**, then **Test robot WiFi**. Tick *remember on this PC* to keep the password in Windows Credential
   Manager. The line underneath shows this PC's address, which is given to the robot so it finds the brain even when
   the robot and the PC are on different sub-networks (common with mesh WiFi).
2. **Hearing** - loads [Whisper](https://github.com/SYSTRAN/faster-whisper) on this PC (base.en, about 140 MB,
   downloaded once from Hugging Face - **Source…** can point it at a mirror or a folder instead).
3. **Thinking** - [Ollama](https://ollama.com), free and local. Install it, run `ollama pull llama3.1:8b` (or
   `llama3.2` on PCs without a strong graphics card), press **Check**, and **Try it** with a typed message.
4. **Turn it on** - **Start AI conversations** starts the brain and, if the robot is plugged in, sets it up to use it.
   The brain warms up (it runs Whisper once and has the model read ButterBot's instructions) before showing READY, so
   the first answer is as quick as the rest. After the first successful conversation it starts by itself whenever
   Dimension Hopper opens.

The **Conversations** table shows what you said, ButterBot's answer, the action, and how long the upload, hearing and
thinking took. Every conversation is also logged to `%LOCALAPPDATA%\DimensionHopper\brain_log.jsonl`.

Privacy: your voice goes from the robot to this PC over your own network and is turned into text here; with Ollama the
AI runs here too. Nothing is sent to the internet. The WiFi password goes to the robot over USB and, if you choose,
into Windows Credential Manager - never into a plain file.

## Build the exe

```powershell
powershell -ExecutionPolicy Bypass -File build_exe.ps1
```

This produces `dist\DimensionHopper.exe`: a single file with no console and no Python needed on the target PC (it's
large, about 110 MB, because it includes the speech-to-text engine).

## Run from source

```powershell
py -3 -m venv .venv; .\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python dimension_hopper.py
```

Dev flags: `--selftest <out.txt>` checks the bundled esptool, stubs and AI libraries (handy for the windowed exe).

## Files

| File | Purpose |
|---|---|
| `hopper_core.py` | Ports, probe, status, GitHub releases, image checks, quick flash, backup, console commands. No UI. |
| `dimension_hopper.py` | Tk UI: the animated portal, device slots, job runner, log; hosts the tabs. |
| `guide_tab.py`, `personalize_tab.py`, `diag_tab.py`, `console_tab.py`, `ai_tab.py` | The tabs. |
| `brain.py` | The AI brain: Whisper, Ollama, the robot protocol and server, persona and timings. |
| `tooltip.py` | Hover help for every button. |
| `build_exe.ps1` | Reproducible PyInstaller build. |
| `make_art.py` | Regenerates the art: `portal.ico`/`portal.png` (app icon), `bot.png` and `controller.png` (slot icons). |
