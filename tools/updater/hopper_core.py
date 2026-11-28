"""
Dimension Hopper core: everything that talks to hardware, GitHub or the disk.
No UI code lives here, so it can be tested headless.

Both ButterBot and its controller are ESP32-S3 boards. They may show up through a USB-serial
bridge (the robot uses a CH340, VID 0x1A86) or the S3's native USB (VID 0x303A), so USB IDs
can't tell them apart. Their flash chips can: the robot has 16 MB, the controller 4 MB.
A probe (esptool flash_id) reads that.

The robot's CH340 sits on the robot's own power rail, and the stock firmware powers the robot
off when it boots without the power button held. So after a probe or a backup we leave the
robot parked in the ROM bootloader (--after no_reset) instead of rebooting it; the final write
reboots it into the new firmware. A parked board runs nothing (no BLE, so no pairing), so the app
tracks parked ports and wakes them on request or when it closes. The controller doesn't lose power
on reboot, so it's woken straight after the probe.
"""
from __future__ import annotations

import contextlib
import errno
import io
import json
import hashlib
import os
import queue
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import serial
import serial.tools.list_ports


def _serial_like_windows():
    """Makes serial ports on macOS / Linux behave as the app expects from Windows:

    - Exclusive: Windows lets only one program open a port; macOS and Linux let several, and then they steal each
      other's bytes. Every open asks for an exclusive lock, so a busy port fails to open here too (the Diagnostics,
      Console and Controller tabs, a scan and a flash all rely on that). esptool asks for it itself.
    - No reset on open: opening a port raises DTR and RTS together (harmless), then pyserial drops DTR before RTS.
      In between, RTS up + DTR down is the ESP32's reset signal - the board reboots, and the robot, booting
      without its power button held, switches itself off. So when both are wanted low, RTS goes first
      (DTR up + RTS down resets nothing). Windows doesn't raise them on open.
    """
    if sys.platform == "win32" or getattr(serial.Serial, "_hop_patched", False):
        return
    orig_init = serial.Serial.__init__
    orig_open = serial.Serial.open
    orig_dtr = serial.Serial._update_dtr_state

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("exclusive", True)
        orig_init(self, *args, **kwargs)

    def _update_dtr_state(self):
        if not getattr(self, "_hop_opening", False):
            orig_dtr(self)

    def open(self):
        self._hop_opening = not self._dtr_state  # DTR low: hold it back until RTS is set
        try:
            orig_open(self)
        finally:
            held, self._hop_opening = self._hop_opening, False
        if held and self.is_open and not self._dsrdtr:
            try:
                orig_dtr(self)
            except OSError as e:  # like pyserial's open(): a port without modem lines (a pty) is fine
                if e.errno not in (errno.EINVAL, errno.ENOTTY):
                    raise

    serial.Serial.__init__ = __init__
    serial.Serial.open = open
    serial.Serial._update_dtr_state = _update_dtr_state
    serial.Serial._hop_patched = True


_serial_like_windows()

ESPRESSIF_VID = 0x303A
# USB-serial bridges commonly used on ESP32 boards, for nicer labels
BRIDGES = {0x303A: "ESP32 native USB", 0x1A86: "CH34x", 0x10C4: "CP210x", 0x0403: "FTDI"}
MiB = 1024 * 1024

APP_NAME = "DimensionHopper"


def _app_dir() -> Path:
    """Where settings, downloads and backups live: %LOCALAPPDATA% on Windows, Application Support on a Mac."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP_NAME
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / APP_NAME


APP_DIR = _app_dir()
CACHE_DIR = APP_DIR / "cache"
BACKUP_DIR = APP_DIR / "backups"
CONFIG_PATH = APP_DIR / "config.json"


def short_path(path) -> str:
    """A path for the screen and logs without the user name in it (people post screenshots)."""
    text = str(path)
    if sys.platform != "win32":
        home = str(Path.home())
        return "~" + text[len(home):] if text == home or text.startswith(home + os.sep) else text
    for var in ("LOCALAPPDATA", "APPDATA", "USERPROFILE"):
        base = os.environ.get(var)
        if base and text.lower().startswith(base.lower()):
            return f"%{var}%" + text[len(base):]
    return text

# Built-in choices for the "Latest GitHub release" dropdown: the stock firmware and this fork.
# More can be added at runtime (Repos… dialog); those live in config["custom_repos"].
# The fork first: this app ships with the fork's releases, so that's what a new user means to install.
PRESET_REPOS = {
    "bot": ["Exadon/ButterBot-Firmware-Public", "CircuitMess/ButterBot-Firmware-Public"],
    "ctrl": ["Exadon/ButterBotCtrl-Firmware-Public", "CircuitMess/ButterBotCtrl-Firmware-Public"],
    "clock": ["Exadon/Clockstar-v2-Firmware", "CircuitMess/Clockstar-v2-Firmware"],
}
STOCK_OWNER = "CircuitMess"

DEFAULT_CONFIG = {
    "bot_repo": "Exadon/ButterBot-Firmware-Public",       # the fork by default (STOCK is one click away)
    "ctrl_repo": "Exadon/ButterBotCtrl-Firmware-Public",
    "clock_repo": "Exadon/Clockstar-v2-Firmware",         # the fork with the ButterBot app (Watch tab)
    "custom_repos": {"bot": [], "ctrl": [], "clock": []},
    "backup_first": True,
    "baud": 921600,
}

REPO_RE = re.compile(r"[\w.-]+/[\w.-]+")


def normalize_repo(text: str) -> str:
    """Accepts 'owner/name' or a github.com URL; returns 'owner/name' or raises ValueError."""
    t = text.strip().removesuffix(".git").strip("/")
    t = re.sub(r"^(https?://)?(www\.)?github\.com/", "", t, flags=re.I)
    t = "/".join(t.split("/")[:2])
    if not REPO_RE.fullmatch(t):
        raise ValueError(f"'{text.strip()}' isn't an owner/repo name or GitHub URL")
    return t


def repo_choices(cfg: dict, kind: str) -> list[str]:
    seen, out = set(), []
    for r in PRESET_REPOS[kind] + list(cfg.get("custom_repos", {}).get(kind, [])):
        if r.lower() not in seen:
            seen.add(r.lower())
            out.append(r)
    return out


# ---------------------------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------------------------

class Kind:
    BOT = "bot"
    CTRL = "ctrl"
    CLOCK = "clock"   # CircuitMess Clockstar v2 watch (ESP32-S3 on its own USB)

    LABEL = {BOT: "ButterBot", CTRL: "Controller", CLOCK: "Clockstar"}
    FLASH = {BOT: 16 * MiB, CTRL: 4 * MiB, CLOCK: 4 * MiB}
    REPO_KEY = {BOT: "bot_repo", CTRL: "ctrl_repo", CLOCK: "clock_repo"}


@dataclass
class PortInfo:
    device: str
    description: str
    is_usb: bool          # a real USB serial device (Bluetooth SPP ports have no VID)
    bridge: str = ""

    def label(self) -> str:
        tag = f"  [{self.bridge}]" if self.bridge else ("  [USB]" if self.is_usb else "")
        return f"{self.device} - {self.description}{tag}"


@dataclass
class ProbeResult:
    port: str
    chip: str = ""
    flash_bytes: int = 0
    mac: str = ""
    error: str = ""
    parked: bool = False  # left in the ROM bootloader: not running firmware until woken or flashed
    native_usb: bool = False  # on the chip's own USB (Espressif VID), not a USB-serial bridge like the CH340

    @property
    def kind(self) -> Optional[str]:
        if self.flash_bytes >= 16 * MiB:
            return Kind.BOT
        # The controller and the Clockstar both have 4 MB; the controller talks through a CH340, the watch
        # through the ESP32-S3's own USB
        if 0 < self.flash_bytes <= 4 * MiB:
            return Kind.CLOCK if self.native_usb else Kind.CTRL
        return None


def select_port_in(box, var, device: str) -> bool:
    """Selects device (e.g. COM7) in a port dropdown, if it's listed. True if it was."""
    for label in box.cget("values"):
        if label.split(" ", 1)[0] == device:
            var.set(label)
            return True
    return False


def list_ports() -> list[PortInfo]:
    ports = []
    for p in serial.tools.list_ports.comports():
        ports.append(PortInfo(p.device, p.description or "", p.vid is not None, BRIDGES.get(p.vid or -1, "")))
    # USB devices first, then natural COM order
    ports.sort(key=lambda p: (not p.is_usb, int(re.sub(r"\D", "", p.device) or 0)))
    return ports


# ---------------------------------------------------------------------------------------------
# esptool runner
# ---------------------------------------------------------------------------------------------

_esptool_lock = threading.Lock()
_PCT = re.compile(r"\((\d{1,3}) ?%\)")


class _LineSink(io.TextIOBase):
    """stdout replacement that splits esptool's progress updates into separate lines: \\r for writes, backspaces
    for read_flash (a backup), which otherwise pile up unseen until the read ends."""

    def __init__(self, on_line: Callable[[str], None]):
        self.on_line = on_line
        self.buf = ""

    def write(self, s: str) -> int:
        self.buf += s
        while True:
            m = re.search(r"[\r\n\b]+", self.buf)
            if not m:
                break
            line, self.buf = self.buf[: m.start()], self.buf[m.end():]
            if line.strip():
                self.on_line(line.rstrip())
        return len(s)

    def flush(self):
        pass

    def isatty(self):
        return False


class EsptoolError(RuntimeError):
    pass


def run_esptool(args: list[str], on_line: Callable[[str], None] = lambda s: None,
                on_progress: Callable[[int], None] = lambda p: None,
                file_sizes: Optional[list[int]] = None) -> str:
    """
    Runs esptool in-process (works inside a --windowed PyInstaller exe, where a child process
    would have no stdout). Serialized: esptool keeps global state and we redirect sys.stdout.
    Returns the full output; raises EsptoolError on failure.
    file_sizes: the sizes of the files a write_flash writes, in order. esptool counts 0-100 % for each file,
    so without them the progress bar starts again for every file; with them it reports one overall percentage.
    """
    import esptool

    out: list[str] = []
    sizes = [max(1, n) for n in file_sizes or []]
    total = sum(sizes)
    current = 0  # index of the file being written

    def line(s: str):
        nonlocal current
        out.append(s)
        m = _PCT.search(s)
        if m:
            p = min(100, int(m.group(1)))
            if sizes:
                i = min(current, len(sizes) - 1)
                p = (sum(sizes[:i]) + sizes[i] * p // 100) * 100 // total
            on_progress(p)
        if sizes and s.startswith("Wrote "):  # one file done ("Wrote 8650752 bytes (... compressed) at 0x...")
            current += 1
        on_line(s)

    sink = _LineSink(line)
    with _esptool_lock, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        try:
            esptool.main(args)
        except SystemExit as e:  # esptool exits on some argument errors
            if e.code not in (0, None):
                raise EsptoolError(f"esptool exited with code {e.code}") from None
        except Exception as e:  # FatalError, serial errors, ...
            sink.write("\n")
            raise EsptoolError(str(e) or e.__class__.__name__) from None
        finally:
            sink.write("\n")
    return "\n".join(out)


MAC_CH34X_MAX_BAUD = 115200


def port_baud(port: str, baud: int) -> int:
    """The flashing speed to use on a port. macOS's own driver for the CH34x USB bridge (the robot's and the
    controller's, /dev/cu.usbserial-*) garbles what the board sends back at anything above 115200 baud
    ("Invalid head of packet", "Corrupt data" - measured: 230400 lost bytes within 64 KB), so a Mac uses 115200
    there: a full 16 MB backup takes ~25 minutes instead of ~3. WCH's own driver (/dev/cu.wchusbserial*) and
    Windows manage 921600."""
    if sys.platform != "darwin" or baud <= MAC_CH34X_MAX_BAUD or "wchusbserial" in port:
        return baud
    for p in serial.tools.list_ports.comports():
        if p.device == port and p.vid == 0x1A86:
            return MAC_CH34X_MAX_BAUD
    return baud


def _base_args(port: str, baud: int) -> list[str]:
    return ["--chip", "esp32s3", "--port", port, "--baud", str(port_baud(port, baud))]


def port_exists(port: str) -> bool:
    return any(p.device == port for p in serial.tools.list_ports.comports())


def probe(port: str, baud: int = 921600, on_line=lambda s: None) -> ProbeResult:
    """
    Resets the board into the ROM bootloader and reads chip + flash size. Leaves it parked there:
    rebooting would run the firmware, which powers the robot (and its USB bridge) off.
    """
    res = ProbeResult(port)
    if not port_exists(port):
        res.error = (f"{port} isn't there any more. The robot probably powered itself off - "
                     "hold its power button ~2 s to turn it on, then Scan again.")
        return res
    try:
        text = run_esptool(_base_args(port, baud) + ["--after", "no_reset", "flash_id"], on_line)
    except EsptoolError as e:
        res.error = str(e)
        return res

    if m := re.search(r"Chip is (.+)", text):
        res.chip = m.group(1).strip()
    if m := re.search(r"MAC: ([0-9a-fA-F:]{17})", text):
        res.mac = m.group(1)
    if m := re.search(r"Detected flash size: (\d+)\s*([MK])B", text):
        res.flash_bytes = int(m.group(1)) * (MiB if m.group(2) == "M" else 1024)
    if not res.flash_bytes:
        res.error = "Chip answered but did not report a flash size"
        return res
    res.native_usb = any(p.device == port and p.vid == ESPRESSIF_VID for p in serial.tools.list_ports.comports())
    res.parked = True
    # The controller keeps its USB power through a reboot, so don't leave it stuck in the bootloader: a
    # parked chip runs no firmware - no screen, no BLE, no pairing - until it's power-cycled. Flashing
    # later resets it back into the bootloader by itself.
    if res.kind in (Kind.CTRL, Kind.CLOCK):  # the watch has its own battery: same as the controller
        try:
            wake(port)
            res.parked = False
        except EsptoolError as e:
            on_line(f"could not reboot {port}: {e}")
    return res


def wake(port: str) -> None:
    """Reboot a chip that's parked in the ROM bootloader into its firmware (no reset into download mode
    first). The robot's firmware powers the robot off when it boots without the button held, so after
    waking a robot its port disappears until you turn it on."""
    run_esptool(_base_args(port, 115200) + ["--before", "no_reset", "--after", "hard_reset",
                                            "--connect-attempts", "2", "chip_id"])


# ---------------------------------------------------------------------------------------------
# Firmware sources
# ---------------------------------------------------------------------------------------------

@dataclass
class AppInfo:
    """The esp_app_desc_t that ESP-IDF embeds in every app image (version, project, build date)."""
    project: str
    version: str
    date: str
    time: str
    idf: str

    def short(self) -> str:
        return f"{self.project} {self.version} · built {self.date}"


APP_DESC_MAGIC = 0xABCD5432
APP_OFFSET = 0x10000          # factory app partition on the robot and the controller
PART_TABLE_OFFSET = 0x8000
# Where a partition table can sit: 0x8000 on the robot and controller, 0x10000 on the Clockstar (its bootloader is
# bigger, so its app starts at 0x20000). Images and chips are read for the table rather than assumed.
PART_TABLE_OFFSETS = (0x8000, 0x10000)
IMAGE_HEAD = 0x30000          # enough of a full image to hold any of those tables and the app's header
PART_TABLE_MAGIC = b"\xAA\x50"


def read_app_info(data: bytes, image_offset: int) -> Optional[AppInfo]:
    """esp_app_desc_t sits 0x20 bytes into an app image (after the image + first segment headers)."""
    d = image_offset + 0x20
    if len(data) < d + 144 or int.from_bytes(data[d:d + 4], "little") != APP_DESC_MAGIC:
        return None

    def txt(off, n):
        return data[d + off:d + off + n].split(b"\0")[0].decode("utf-8", "replace")
    return AppInfo(project=txt(48, 32), version=txt(16, 32), time=txt(80, 16), date=txt(96, 16), idf=txt(112, 32))


def find_table(data: bytes, base: int = 0) -> Optional[int]:
    """The flash address of the partition table in data (which starts at flash address base), or None."""
    for off in PART_TABLE_OFFSETS:
        rel = off - base
        if 0 <= rel and data[rel:rel + 2] == PART_TABLE_MAGIC:
            return off
    return None


def app_offset(data: bytes, table_off: int, base: int = 0) -> Optional[int]:
    """The first app partition (factory or ota_0) listed in the table at table_off."""
    rel = table_off - base
    for p in parse_partitions(data[rel:rel + 0xC00]):
        if p.type == 0:
            return p.offset
    return None


def kind_from_project(project: str) -> Optional[str]:
    p = project.lower()
    if "clockstar" in p:
        return Kind.CLOCK
    if "butterbotctrl" in p or "controller" in p:
        return Kind.CTRL
    if "butterbot" in p:
        return Kind.BOT
    return None


@dataclass
class FlashPlan:
    """What to write, where. A release/backup is one full image at 0x0; a build has several parts."""
    title: str
    parts: list[tuple[int, Path]]
    kind_hint: Optional[str] = None       # which device this image is for, if we could tell
    extra_write_args: list[str] = field(default_factory=list)
    app: Optional[AppInfo] = None         # what firmware is inside, if we could read it
    source: str = ""                      # BACKUP / RELEASE / IMAGE / BUILD - shown as a badge

    @property
    def total_bytes(self) -> int:
        return sum(p.stat().st_size for _, p in self.parts)


class ImageError(ValueError):
    pass


def source_of(path: Path) -> str:
    # Plain absolute paths, not resolve(): Microsoft Store Python transparently redirects
    # %LOCALAPPDATA% writes into its package folder, and resolve() follows that redirect.
    p = os.path.normcase(os.path.abspath(path))
    for root, tag in ((BACKUP_DIR, "BACKUP"), (CACHE_DIR, "RELEASE")):
        if p.startswith(os.path.normcase(os.path.abspath(root)) + os.sep):
            return tag
    return "IMAGE"


def backup_date(path: Path) -> str:
    """Backups are named <Device>-<PORT>-YYYYmmdd-HHMMSS.bin."""
    m = re.search(r"(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})\d{2}\.bin$", path.name)
    return f"{m[1]}-{m[2]}-{m[3]} {m[4]}:{m[5]}" if m else ""


def classify_full_image(path: Path) -> FlashPlan:
    """
    Validates a file meant to be written at 0x0 as a complete flash image. A real one has the
    bootloader at 0x0, a partition table at 0x8000 and the app at 0x10000. Anything else written
    at 0x0 bricks the board until it's reflashed (that's how the robot got bricked once: the bare
    app .bin from a build folder was written at 0x0).
    """
    size = path.stat().st_size
    if size < 64 * 1024:
        raise ImageError(f"{path.name} is only {size} bytes - that's not a firmware image")
    with open(path, "rb") as f:
        head = f.read(IMAGE_HEAD + 0x200)
    if head[:1] != b"\xE9":
        raise ImageError(f"{path.name} doesn't start with an ESP image header (0xE9). "
                         "Pick a merged full-flash .bin (like the GitHub releases) or a build folder.")

    if app_at_0 := read_app_info(head, 0):
        raise ImageError(f"{path.name} is only the APP part of a build ({app_at_0.short()}). "
                         "Writing it at 0x0 would wipe the bootloader and brick the board. "
                         "Use 'idf.py build folder' and pick the build/ folder instead."
                         + ("  (This looks like a backup of an already-broken board - don't restore it.)"
                            if source_of(path) == "BACKUP" else ""))
    table = find_table(head)
    if table is None:
        raise ImageError(f"{path.name} has no partition table (at 0x8000 or 0x10000), so it isn't a complete flash "
                         "image (maybe a bootloader or data partition on its own). Writing it at 0x0 would brick the "
                         "board.")
    app_at = app_offset(head, table)
    app = read_app_info(head, app_at) if app_at is not None else None
    if app is None:
        raise ImageError(f"{path.name} has a partition table but no valid app "
                         f"{f'at 0x{app_at:x}' if app_at is not None else 'listed'} - not bootable.")

    # Prefer the project name baked into the app; fall back to size (bot images carry ~12 MB of models).
    kind = kind_from_project(app.project) or (Kind.BOT if size > 4 * MiB else Kind.CTRL)
    src = source_of(path)
    when = backup_date(path) if src == "BACKUP" else ""
    title = f"{path.name} ({size / MiB:.1f} MB)" + (f" · backup from {when}" if when else "")
    return FlashPlan(title, [(0, path)], kind, app=app, source=src)


def plan_from_build_dir(build_dir: Path) -> FlashPlan:
    """Uses the flasher_args.json that `idf.py build` writes, so a local fork build flashes all its parts."""
    build_dir = Path(build_dir)
    if not (build_dir / "flasher_args.json").exists() and (build_dir / "build" / "flasher_args.json").exists():
        build_dir = build_dir / "build"
    args_path = build_dir / "flasher_args.json"
    if not args_path.exists():
        raise ImageError("No flasher_args.json here. Pick the project's build/ folder after `idf.py build`.")

    data = json.loads(args_path.read_text(encoding="utf-8"))
    parts = []
    for off, rel in sorted(data.get("flash_files", {}).items(), key=lambda kv: int(kv[0], 16)):
        p = build_dir / rel
        if not p.exists():
            raise ImageError(f"Build is incomplete: missing {rel}")
        parts.append((int(off, 16), p))
    if not parts:
        raise ImageError("flasher_args.json lists no files")

    fs = data.get("flash_settings", {})
    extra = []
    for key, flag in (("flash_mode", "--flash_mode"), ("flash_freq", "--flash_freq"), ("flash_size", "--flash_size")):
        if fs.get(key):
            extra += [flag, fs[key]]

    kind = None
    size_txt = str(fs.get("flash_size", "")).upper()
    if size_txt.startswith("16"):
        kind = Kind.BOT
    elif size_txt.startswith("4"):
        kind = Kind.CTRL  # or the Clockstar: the app's project name below decides
    desc = build_dir / "project_description.json"
    name = build_dir.parent.name
    if desc.exists():
        try:
            name = json.loads(desc.read_text(encoding="utf-8")).get("project_name", name)
        except ValueError:
            pass

    # A build must include the bootloader at 0x0 and the app (0x10000 on the robot and controller, 0x20000 on the
    # Clockstar: flasher_args.json says where), or the board won't boot.
    offsets = {off for off, _ in parts}
    app_at = int(str((data.get("app") or {}).get("offset", hex(APP_OFFSET))), 16)
    if 0 not in offsets or app_at not in offsets:
        raise ImageError(f"This build doesn't include both the bootloader (0x0) and the app (0x{app_at:x}) - "
                         "run a full `idf.py build` first.")
    app_path = dict(parts)[app_at]
    with open(app_path, "rb") as f:
        app = read_app_info(f.read(0x200), 0)
    if app and (not kind or kind_from_project(app.project) == Kind.CLOCK):
        kind = kind_from_project(app.project) or kind
    return FlashPlan(f"local build '{name}' ({len(parts)} parts)", parts, kind, extra, app=app, source="BUILD")


def _build_project(build_dir: Path) -> str:
    try:
        return json.loads((build_dir / "project_description.json").read_text(encoding="utf-8")).get("project_name", "")
    except (OSError, ValueError):
        return ""


def find_fork_build(kind: str = "bot") -> Optional[Path]:
    """
    The fork's own build folder for a device, if we can find it. Dimension Hopper lives in the robot
    repo's tools/updater (the exe in tools/updater/dist), so walk up to the robot repo; the controller
    repo is expected next to it (e.g. C:\\bb\\fw and C:\\bb\\ctrl). A build counts if it has
    flasher_args.json and its project name says which device it's for.
    """
    robot_root = None
    for start in (Path(sys.executable), Path(__file__)):
        for parent in list(Path(os.path.abspath(start)).parents)[:8]:  # a Mac app sits deeper (.app/Contents/MacOS)
            if (parent / "main").is_dir() and (parent / "tools" / "updater").is_dir():
                robot_root = parent
                break
        if robot_root:
            break
    if robot_root is None:
        return None

    candidates = [robot_root] if kind == Kind.BOT else \
        sorted(p for p in robot_root.parent.iterdir() if p.is_dir() and p != robot_root)
    for repo in candidates:
        build = repo / "build"
        if (build / "flasher_args.json").exists() and kind_from_project(_build_project(build)) == kind:
            return build
    return None


@dataclass
class Release:
    repo: str
    tag: str
    asset_name: str
    url: str
    size: int
    asset_id: int = 0  # GitHub's id for this upload: a re-uploaded file with the same name gets a new one

    def cache_path(self) -> Path:
        # Keyed by the upload's id too, so a release file that was replaced is downloaded again, never served stale
        name = f"{self.asset_id}-{self.asset_name}" if self.asset_id else self.asset_name
        return CACHE_DIR / self.repo.replace("/", "__") / self.tag / name


def _http_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}-updater",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def latest_release(repo: str) -> Release:
    repo = normalize_repo(repo)
    try:
        data = _http_json(f"https://api.github.com/repos/{repo}/releases/latest")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # Same 404 for "no such repo" and "repo has no releases yet"
            raise ValueError(f"{repo} has no releases yet (or doesn't exist). "
                             "Push a v* tag to publish one, or use the build-folder option.") from None
        if e.code == 403:
            raise ValueError("GitHub rate limit hit - try again in a few minutes") from None
        raise
    bins = [a for a in data.get("assets", []) if a["name"].lower().endswith(".bin")]
    if not bins:
        raise ValueError(f"{repo} {data.get('tag_name', '?')} has no .bin asset")
    a = max(bins, key=lambda a: a["size"])
    return Release(repo, data["tag_name"], a["name"], a["browser_download_url"], a["size"], int(a.get("id") or 0))


def download(rel: Release, on_progress=lambda p: None, cancel: Optional[threading.Event] = None) -> Path:
    dest = rel.cache_path()
    if dest.exists() and dest.stat().st_size == rel.size:
        on_progress(100)
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    req = urllib.request.Request(rel.url, headers={"User-Agent": f"{APP_NAME}-updater"})
    with urllib.request.urlopen(req, timeout=30) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or rel.size or 0)
        done = 0
        while chunk := r.read(256 * 1024):
            if cancel is not None and cancel.is_set():
                raise RuntimeError("Download cancelled")
            f.write(chunk)
            done += len(chunk)
            if total:
                on_progress(int(done * 100 / total))
    if rel.size and tmp.stat().st_size != rel.size:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("Download size mismatch - try again")
    os.replace(tmp, dest)
    return dest


# ---------------------------------------------------------------------------------------------
# Flash operations
# ---------------------------------------------------------------------------------------------

def check_plan_fits(plan: FlashPlan, target: str, flash_bytes: int):
    """Refuse the classic foot-gun: a 16 MB robot image onto the 4 MB controller (or vice versa)."""
    if plan.kind_hint and plan.kind_hint != target:
        raise ImageError(f"That's a {Kind.LABEL[plan.kind_hint]} image, but this slot is the "
                         f"{Kind.LABEL[target]}. Swap the source or the port.")
    end = max(off + p.stat().st_size for off, p in plan.parts)
    if flash_bytes and end > flash_bytes:
        raise ImageError(f"Image ends at {end / MiB:.1f} MB but the board only has {flash_bytes / MiB:.0f} MB of flash.")


def backup(port: str, flash_bytes: int, target: str, baud: int, on_line, on_progress) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    # the port's own name: "COM7", or "cu.usbserial-110" from /dev/cu.usbserial-110 (no slashes in a file name)
    dest = BACKUP_DIR / f"{Kind.LABEL[target]}-{os.path.basename(port)}-{stamp}.bin"
    if sys.platform == "win32":
        run_esptool(_base_args(port, baud) + ["--after", "no_reset", "read_flash", "0", hex(flash_bytes), str(dest)],
                    on_line, on_progress)
    else:
        _backup_in_pieces(port, flash_bytes, baud, dest, on_line, on_progress)
    if not dest.exists() or dest.stat().st_size != flash_bytes:
        raise EsptoolError("Backup came back short - not flashing without a good backup")
    return dest


BACKUP_PIECE = 0x40000   # 256 KB
BACKUP_TRIES = 4         # per piece, before giving up


def _backup_in_pieces(port: str, flash_bytes: int, baud: int, dest: Path, on_line, on_progress):
    """read_flash, but in 256 KB pieces, each checked against the MD5 the chip computes of it, and a bad piece read
    again. macOS's own CH34x driver now and then drops a few bytes of what the board sends (measured: once every
    few MB, even at 115200 baud), and a single 16 MB read_flash fails on the first one - after up to 25 minutes.
    Leaves the board in the bootloader, like read_flash --after no_reset."""
    import esptool
    from esptool.cmds import detect_chip

    sink = _LineSink(on_line)
    data = bytearray()
    esp = None
    tries = 0

    def drop():
        try:
            esp._port.close()  # noqa: SLF001 - esptool has no public close
        except Exception:
            pass

    with _esptool_lock, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        try:
            while len(data) < flash_bytes:
                addr = len(data)
                size = min(BACKUP_PIECE, flash_bytes - addr)
                try:
                    if esp is None:
                        esp = detect_chip(port, 115200, "default_reset")
                        esp = esp.run_stub()
                        esp.change_baud(port_baud(port, baud))
                        esp.flash_set_parameters(flash_bytes)
                    piece = esp.read_flash(addr, size)
                    if len(piece) != size or hashlib.md5(piece).hexdigest() != esp.flash_md5sum(addr, size):
                        raise esptool.FatalError("the piece read doesn't match the chip's checksum")
                except (esptool.FatalError, serial.SerialException, OSError) as e:
                    tries += 1
                    if tries >= BACKUP_TRIES or not port_exists(port):  # gone = unplugged / switched off
                        raise EsptoolError(f"Backup failed at {addr:#x} after {tries} tries: {e}") from None
                    on_line(f"Reading at {addr:#x} went wrong ({e}) - reconnecting and reading it again")
                    drop()
                    esp = None
                    continue
                tries = 0
                data += piece
                on_progress(len(data) * 100 // flash_bytes)
        finally:
            sink.write("\n")
            if esp is not None:
                drop()
    dest.write_bytes(bytes(data))


def write(port: str, plan: FlashPlan, baud: int, on_line, on_progress):
    args = _base_args(port, baud) + ["--before", "default_reset", "--after", "hard_reset", "write_flash", "-z"]
    args += plan.extra_write_args or ["--flash_mode", "keep", "--flash_freq", "keep", "--flash_size", "keep"]
    for off, p in plan.parts:
        args += [hex(off), str(p)]
    # esptool verifies each region's MD5 after writing ("Hash of data verified")
    run_esptool(args, on_line, on_progress, file_sizes=[Path(p).stat().st_size for _, p in plan.parts])


# ---------------------------------------------------------------------------------------------
# Smart write: skip what's already on the chip, and keep pairing/settings (the NVS partition)
# ---------------------------------------------------------------------------------------------

CHUNK = 0x10000  # compare and write in 64 KB pieces (a multiple of the 4 KB erase sector)
NVS_SUBTYPES = {0x02, 0x04}  # data partitions: nvs, nvs_keys - Bluetooth bonds and settings live here


@dataclass
class Partition:
    label: str
    type: int
    subtype: int
    offset: int
    size: int


def parse_partitions(table: bytes) -> list[Partition]:
    out = []
    for i in range(0, len(table) - 31, 32):
        e = table[i:i + 32]
        if e[:2] != PART_TABLE_MAGIC:
            break
        out.append(Partition(e[12:28].split(b"\0")[0].decode("ascii", "replace"), e[2], e[3],
                             int.from_bytes(e[4:8], "little"), int.from_bytes(e[8:12], "little")))
    return out


def _regions(plan: FlashPlan) -> list[tuple[int, bytes]]:
    return [(off, p.read_bytes()) for off, p in plan.parts]


def _image_table(regions: list[tuple[int, bytes]]) -> Optional[bytes]:
    for off, data in regions:
        if (table := find_table(data, off)) is not None:
            rel = table - off
            return data[rel:rel + 0xC00]
    return None


def _cut(regions: list[tuple[int, bytes]], start: int, end: int) -> list[tuple[int, bytes]]:
    """Removes [start, end) from the regions."""
    out = []
    for off, data in regions:
        r_end = off + len(data)
        if r_end <= start or off >= end:
            out.append((off, data))
            continue
        if off < start:
            out.append((off, data[:start - off]))
        if r_end > end:
            out.append((end, data[end - off:]))
    return out


def nvs_to_keep(chip: list[Partition], image: Optional[list[Partition]]) -> tuple[list[Partition], list[Partition]]:
    """
    Which of the image's NVS partitions (Bluetooth bonds, settings) to leave alone on the chip, and which ones get
    reset. Kept: one the chip has at the same place and size, and a new one in a spot the chip never used for
    anything (a layout that only adds a partition, like the robot's "userdata" at the end of the flash). Reset: one
    that moved or grew, since its old contents would be read from the wrong place.
    """
    if image is None:                       # no table in the image: the chip's layout stays
        image = chip
    is_nvs = lambda p: p.type == 0x01 and p.subtype in NVS_SUBTYPES  # noqa: E731
    keep, lost = [], []
    for p in filter(is_nvs, image):
        same = any(is_nvs(c) and c.offset == p.offset and c.size == p.size for c in chip)
        clash = any(c.offset < p.offset + p.size and p.offset < c.offset + c.size for c in chip)
        (keep if same or not clash else lost).append(p)
    return keep, lost


@dataclass
class WriteStats:
    total: int = 0          # bytes in the image (after leaving NVS out)
    written: int = 0        # bytes that actually had to be written
    kept_nvs: bool = False
    note: str = ""


def smart_write(port: str, plan: FlashPlan, baud: int, flash_bytes: int, keep_pairing: bool,
                on_line, on_progress, compare: bool = True) -> WriteStats:
    """
    Writes plan to the chip, but first asks the chip for the MD5 of every 64 KB piece and only writes the
    pieces that differ - a rebuilt app usually changes a few hundred KB of an 8 MB image, and the ~5 MB of
    speech and face models never change. With keep_pairing, the NVS partition (Bluetooth bonds, settings) is
    left alone as long as the image's partition layout matches the chip's.
    Ends with a hard reset into the new firmware.
    """
    import esptool
    from esptool.cmds import detect_chip

    stats = WriteStats()
    regions = _regions(plan)
    sink = _LineSink(on_line)
    with _esptool_lock, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        try:
            esp = detect_chip(port, 115200, "default_reset")
            esp = esp.run_stub()
            esp.change_baud(port_baud(port, baud))
            esp.flash_set_parameters(flash_bytes)

            if keep_pairing:
                img_table = _image_table(regions)
                chip_table = esp.read_flash(PART_TABLE_OFFSET, 0xC00)
                if chip_table[:2] != PART_TABLE_MAGIC:  # the Clockstar keeps its table at 0x10000
                    chip_table = esp.read_flash(0x10000, 0xC00)
                keep, lost = nvs_to_keep(parse_partitions(chip_table),
                                         parse_partitions(img_table) if img_table else None)
                for p in keep:
                    regions = _cut(regions, p.offset, p.offset + p.size)
                stats.kept_nvs = bool(keep) and not lost
                if lost:
                    stats.note = "partition layout moved the settings, so pairing and settings are reset this time"

            chunks = []
            for off, data in regions:
                for i in range(0, len(data), CHUNK):
                    chunks.append((off + i, data[i:i + CHUNK]))
            stats.total = sum(len(c) for _, c in chunks)
            changed = []
            for n, (addr, piece) in enumerate(chunks):
                if not compare or esp.flash_md5sum(addr, len(piece)) != hashlib.md5(piece).hexdigest():
                    changed.append((addr, piece))
                on_progress(n * 20 // max(1, len(chunks)))
            stats.written = sum(len(c) for _, c in changed)
            if not changed:
                esp.hard_reset()
        except Exception as e:
            raise EsptoolError(f"Couldn't compare with what's on the chip: {e}") from None
        finally:
            sink.write("\n")
            try:
                esp._port.close()  # noqa: SLF001 - esptool has no public close
            except Exception:
                pass

    if not changed:
        on_progress(100)
        return stats

    # Merge neighbouring pieces into runs, one temp file each
    runs: list[tuple[int, bytearray]] = []
    for addr, piece in changed:
        if runs and runs[-1][0] + len(runs[-1][1]) == addr:
            runs[-1][1].extend(piece)
        else:
            runs.append((addr, bytearray(piece)))
    with tempfile.TemporaryDirectory(prefix="hopper-") as tmp:
        args = _base_args(port, baud) + ["--before", "default_reset", "--after", "hard_reset", "write_flash", "-z"]
        args += plan.extra_write_args or ["--flash_mode", "keep", "--flash_freq", "keep", "--flash_size", "keep"]
        for i, (addr, data) in enumerate(runs):
            f = Path(tmp) / f"run{i}.bin"
            f.write_bytes(data)
            args += [hex(addr), str(f)]
        run_esptool(args, on_line, lambda p: on_progress(20 + p * 80 // 100), file_sizes=[len(d) for _, d in runs])
    return stats


# ---------------------------------------------------------------------------------------------
# After a flash: wait for the board to run the new firmware
# ---------------------------------------------------------------------------------------------

# ---------------------------------------------------------------------------------------------
# Flash worker: esptool runs one at a time inside a process (global state, redirected stdout), so flashing two
# boards at once runs each flash in its own process - this program again, started with --flash-worker.
# ---------------------------------------------------------------------------------------------

_WORKER_FNS = ("backup", "write", "smart_write")


def _worker_command(job: Path) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--flash-worker", str(job)]
    here = str(Path(__file__).resolve().parent)
    return [sys.executable, "-c",
            f"import sys; sys.path.insert(0, {here!r}); import hopper_core; hopper_core.worker_main({str(job)!r})"]


def run_in_worker(fn: str, kwargs: dict, on_line=lambda s: None, on_progress=lambda p: None):
    """Runs backup / write / smart_write in a separate process and returns its result. Progress and esptool lines
    come back through a small text file (a windowed exe's child has no usable stdout)."""
    import pickle
    import subprocess
    import uuid
    assert fn in _WORKER_FNS
    jobs = APP_DIR / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    job = jobs / f"{uuid.uuid4().hex}.job"
    out = job.with_suffix(".out")
    job.write_bytes(pickle.dumps({"fn": fn, "kwargs": kwargs}))
    out.write_text("", encoding="utf-8")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(_worker_command(job), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, creationflags=flags)
    result, error, pos, buf = None, None, 0, ""
    try:
        with open(out, "r", encoding="utf-8", errors="replace") as f:
            while True:
                chunk = f.read()
                if chunk:
                    buf += chunk
                    *lines, buf = buf.split("\n")
                    for line in lines:
                        tag, _, rest = line.partition(" ")
                        if tag == "L":
                            on_line(rest)
                        elif tag == "P":
                            try:
                                on_progress(int(rest))
                            except ValueError:
                                pass
                        elif tag == "R":
                            result = pickle.loads(bytes.fromhex(rest))
                        elif tag == "E":
                            error = rest
                elif proc.poll() is not None:
                    tail = f.read()
                    if not tail:
                        break
                    buf += tail
                    continue
                else:
                    time.sleep(0.1)
    finally:
        for p in (job, out):
            try:
                p.unlink()
            except OSError:
                pass
    if error is not None:
        kind, _, msg = error.partition(" ")
        raise {"ImageError": ImageError, "OSError": OSError, "ValueError": ValueError}.get(kind, EsptoolError)(msg)
    if proc.returncode != 0 and result is None:
        raise EsptoolError(f"The flash worker stopped unexpectedly (exit code {proc.returncode})")
    return result


def worker_main(job_path: str) -> None:
    """Entry point of a flash worker process: runs one job, writes L/P/R/E lines to <job>.out."""
    import pickle
    job = Path(job_path)
    spec = pickle.loads(job.read_bytes())
    with open(job.with_suffix(".out"), "a", encoding="utf-8", buffering=1) as out:
        def line(s: str):
            out.write("L " + s.replace("\n", " ").replace("\r", " ") + "\n")

        def progress(p: int):
            out.write(f"P {int(p)}\n")
        try:
            fn = globals()[spec["fn"]]
            res = fn(**spec["kwargs"], on_line=line, on_progress=progress)
            out.write("R " + pickle.dumps(res).hex() + "\n")
        except Exception as e:  # noqa: BLE001 - reported to the app
            msg = (str(e) or e.__class__.__name__).replace("\n", " ")
            out.write(f"E {e.__class__.__name__} {msg}\n")


def wait_for_boot(port: str, expect_version: str, timeout_s: float, cancel: Optional[threading.Event] = None,
                  on_wait: Callable[[str], None] = lambda s: None) -> DeviceStatus:
    """
    Waits for the board on `port` to print a status line (the robot first has to be switched on by hand: its
    port only exists while it's powered). Returns the status; .error is set on timeout.
    """
    end = time.time() + timeout_s
    seen_gone = False
    while time.time() < end:
        if cancel is not None and cancel.is_set():
            return DeviceStatus(port, error="cancelled")
        if not port_exists(port):
            if not seen_gone:
                on_wait("off")
            seen_gone = True
            time.sleep(0.5)
            continue
        on_wait("on")
        st = read_status(port, timeout_s=7.0)
        if not st.error:
            return st
    return DeviceStatus(port, error="never came back with a status line")


def version_matches(board_fw: str, expected: str) -> bool:
    """The status line carries esp_app_desc's version (32 chars max), the image the full one."""
    return bool(board_fw) and bool(expected) and (board_fw == expected or expected.startswith(board_fw)
                                                   or board_fw.startswith(expected))


def release_note(board_fw: str, tag: str) -> tuple[str, bool]:
    """Compares the firmware a board runs with a release tag. Returns (text, update_available)."""
    if not board_fw or not tag:
        return "", False
    if board_fw == tag:
        return f"up to date ({tag})", False
    if re.search(r"-\d+-g[0-9a-f]+", board_fw):
        base = re.sub(r"-\d+-g[0-9a-f]+.*$", "", board_fw)
        return (f"running a development build based on {base}; latest release is {tag}",
                base != tag)
    return f"update available: {tag} (board has {board_fw})", True


# ---------------------------------------------------------------------------------------------
# One-shot console commands (fork firmware): "forget" etc.
# ---------------------------------------------------------------------------------------------

PORT_BUSY = {"result": "port_busy"}   # another program has the port open
PORT_LOST = {"result": "port_lost"}   # the board vanished mid-command (switched off or reset)


def port_problem(r: Optional[dict], port: str) -> Optional[str]:
    """A plain-English message when console_command couldn't talk to the board for a port reason, else None."""
    if r is not None and r.get("result") == "port_busy":
        return (f"{port} is in use by another program (another Dimension Hopper, a serial monitor, or a script). "
                "Close it and try again.")
    if r is not None and r.get("result") == "port_lost":
        return f"{port} disappeared mid-command - the robot switched off or restarted. Switch it on and try again."
    return None


def console_command(port: str, command: str, reply_tag: str, timeout_s: float = 4.0,
                    need: Optional[str] = None) -> Optional[dict]:
    """Sends a console command without resetting the board and returns the fields of the [reply_tag] reply.
    need: only accept a reply that has this field (e.g. "result"), skipping progress lines with the same tag such as
    "[BBWIFI] event=dhcp_retry" that the robot prints while it works.
    None = no reply; PORT_BUSY / PORT_LOST (see port_problem) when the port itself was the problem. Never raises."""
    import serial

    if not port_exists(port):
        return None
    with _esptool_lock:
        s = serial.Serial()
        s.port, s.baudrate, s.timeout = port, 115200, 0.2
        s.dtr = False
        s.rts = False
        try:
            s.open()
        except serial.SerialException:
            return dict(PORT_BUSY)
        try:
            s.write(command.encode() + b"\n")
            buf, end = b"", time.time() + timeout_s
            pat = re.compile(r"\[" + re.escape(reply_tag) + r"\]((?:\s+\w+=\S+)+)")
            while time.time() < end:
                buf += s.read(4096)
                # Only complete lines: a long reply (the event log is up to ~26 KB of hex) arrives in pieces, and its
                # tag shows up long before the end of the line
                done, _, rest = buf.rpartition(b"\n")
                for line in done.decode("utf-8", "replace").splitlines():
                    if m := pat.search(line):
                        fields = dict(kv.split("=", 1) for kv in m.group(1).split())
                        if need is None or need in fields:
                            return fields
                buf = rest[-65536:]
        except serial.SerialException:
            return dict(PORT_LOST)
        finally:
            try:
                s.close()
            except serial.SerialException:
                pass
    return None


def console_watch(port: str, command: str, until: Callable[[str], bool], timeout_s: float) -> Optional[list[str]]:
    """Sends a console command, then collects the board's lines until until(line) is true or the time is up.
    None when the port couldn't be opened or went away. Never raises."""
    import serial

    if not port_exists(port):
        return None
    with _esptool_lock:
        s = serial.Serial()
        s.port, s.baudrate, s.timeout = port, 115200, 0.2
        s.dtr = False
        s.rts = False
        try:
            s.open()
        except serial.SerialException:
            return None
        lines: list[str] = []
        try:
            s.write(command.encode() + b"\n")
            buf, end = b"", time.time() + timeout_s
            while time.time() < end:
                buf += s.read(512)
                *done, buf = buf.split(b"\n")
                for raw in done:
                    line = raw.decode("utf-8", "replace").strip()
                    if line:
                        lines.append(line)
                        if until(line):
                            return lines
                buf = buf[-8192:]
        except serial.SerialException:
            return None
        finally:
            try:
                s.close()
            except serial.SerialException:
                pass
    return lines


# ---------------------------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------------------------

def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    # 1.11-1.12.0 defaulted the watch to the stock firmware (the fork wasn't published yet): move it to the fork once
    if not cfg.get("clock_repo_fork") and cfg.get("clock_repo") == "CircuitMess/Clockstar-v2-Firmware":
        cfg["clock_repo"] = DEFAULT_CONFIG["clock_repo"]
    cfg["clock_repo_fork"] = True
    cr = cfg.get("custom_repos")
    if not isinstance(cr, dict):
        cr = {}
    cfg["custom_repos"] = {k: [r for r in cr.get(k, []) if isinstance(r, str)] for k in ("bot", "ctrl", "clock")}
    return cfg


LAST_SAVE_ERROR = ""  # why the last save_config failed ("" = it worked); shown by the self-test


def save_config(cfg: dict) -> bool:
    """Writes the settings. Never raises (a failed save must not break the UI), but remembers why it failed."""
    global LAST_SAVE_ERROR
    try:
        text = json.dumps(cfg, indent=2, default=str)  # default=str: a stray non-JSON value can't block the save
        APP_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, CONFIG_PATH)  # atomic: a crash mid-write can't leave a half-written file
        LAST_SAVE_ERROR = ""
        return True
    except Exception as e:  # noqa: BLE001
        LAST_SAVE_ERROR = f"{type(e).__name__}: {e}"
        return False


def open_folder(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    open_path(path)


def open_path(target) -> None:
    """Opens a folder, file or link with whatever the system uses for it (Explorer / Finder / the browser)."""
    import subprocess
    target = str(target)
    try:
        if sys.platform == "win32":
            os.startfile(target)  # noqa: S606 - our own data folder, or a link we made
        elif sys.platform == "darwin":
            subprocess.Popen(["open", target], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["xdg-open", target], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    except OSError:
        import webbrowser
        webbrowser.open(target)



# ---------------------------------------------------------------------------------------------
# Live status (fork firmware prints "[BBSTAT] ..." on the serial console every 5 s)
# ---------------------------------------------------------------------------------------------

STATUS_RE = re.compile(r"\[BBSTAT\]((?:\s+\w+=\S+)+)")


@dataclass
class DeviceStatus:
    port: str
    fields: dict = field(default_factory=dict)
    error: str = ""

    @property
    def battery(self) -> Optional[int]:
        try:
            v = int(self.fields.get("batt", "-1"))
            return v if v >= 0 else None
        except ValueError:
            return None

    @property
    def kind(self) -> Optional[str]:
        return {"bot": Kind.BOT, "ctrl": Kind.CTRL}.get(self.fields.get("dev", ""))

    @property
    def firmware(self) -> str:
        return self.fields.get("fw", "")

    BAD_RESETS = {"crash": "crashed", "watchdog": "was reset by its watchdog (hung)", "brownout": "browned out (battery sagged)"}

    def health(self) -> tuple[str, bool]:
        """One line of health figures, and whether anything in it is worrying."""
        f, bits, worry = self.fields, [], False
        if "ram" in f:
            try:
                ram, low = int(f["ram"]), int(f.get("ram_min", f["ram"]))
                bits.append(f"RAM {ram} KB free (lowest {low} KB)")
                fails = int(f.get("alloc_fail", "0") or 0)
                if fails:
                    bits.append(f"{fails} memory request{'s' if fails != 1 else ''} failed")
                # Older firmware's "lowest" adds up per-region minimums and can read 0 without a real shortage;
                # firmware that reports alloc_fail measures the real low point, and failures are what matter
                worry |= bool(fails) or (low < 8 and "alloc_fail" not in f)
            except ValueError:
                pass
        if "up" in f:
            try:
                up = int(f["up"])
                bits.append(f"up {up // 3600}h {up // 60 % 60}m" if up >= 3600 else f"up {up // 60}m {up % 60}s")
            except ValueError:
                pass
        if f.get("reset"):
            why = self.BAD_RESETS.get(f["reset"])
            bits.append(f"last start: {why}" if why else f"last start: {f['reset']}")
            worry |= why is not None
        if f.get("boots"):
            bits.append(f"{f['boots']} boots")
        return "  ·  ".join(bits), worry

    def summary(self) -> str:
        if self.error:
            return self.error
        f = self.fields
        parts = []
        if self.battery is not None:
            parts.append(f"battery {self.battery}%")
        if f.get("charge") and f["charge"] != "unknown":
            parts.append(f["charge"])
        if f.get("link"):
            parts.append("controller link " + f["link"] if f.get("dev") == "bot" else "robot link " + f["link"])
        if f.get("fw"):
            parts.append("fw " + f["fw"])
        return "  ·  ".join(parts) or "status line had no fields"


def read_status(port: str, timeout_s: float = 12.0, exclusive: bool = True) -> DeviceStatus:
    """
    Listens on the port for a [BBSTAT] line WITHOUT resetting the board: DTR/RTS are set inactive before
    opening, so the CH340 auto-reset circuit isn't triggered and the firmware keeps running.
    Needs fork firmware (stock firmware never prints a status line).
    exclusive=False lets several ports be listened to at once (a scan); it's only safe when nothing is flashing.
    """
    import serial

    res = DeviceStatus(port)
    if not port_exists(port):
        res.error = f"{port} isn't there. Is the board switched on?"
        return res

    with (_esptool_lock if exclusive else contextlib.nullcontext()):  # never at the same time as a flash/probe
        s = serial.Serial()
        s.port, s.baudrate, s.timeout = port, 115200, 0.25
        s.dtr = False
        s.rts = False
        try:
            s.open()
        except serial.SerialException as e:
            res.error = f"Couldn't open {port}: {e}"
            return res
        try:
            buf = b""
            end = time.time() + timeout_s
            while time.time() < end:
                try:
                    buf += s.read(512)
                except serial.SerialException:  # the port vanished: the robot powered itself off
                    res.error = f"{port} went away while listening (the board switched off)."
                    return res
                for line in buf.decode("utf-8", "replace").splitlines():
                    if m := STATUS_RE.search(line):
                        res.fields = dict(kv.split("=", 1) for kv in m.group(1).split())
                        return res
                buf = buf[-2048:]
        finally:
            s.close()

    res.error = ("No status line received. The board may be in bootloader mode (after a probe or backup - "
                 "power-cycle it), or running firmware without status reporting (stock / older fork builds).")
    return res


# ---------------------------------------------------------------------------------------------
# Diagnostics session (fork firmware streams "[BBDIAG] ..." while asked to with "diag on")
# ---------------------------------------------------------------------------------------------

DIAG_RE = re.compile(r"\[(BBDIAG|BBSTAT|BBDRIVE|BBFORGET|BBVOICE|BBCAM|BBSCR|BBCOM|BBEMU)]((?:\s+\w+=\S+)+)")


class DiagSession:
    """
    Holds a serial port open (without resetting the board) and hands every line to on_line(kind, fields, raw_line).
    kind is "BBDIAG", "BBSTAT", "BBDRIVE", "BBFORGET", "BBVOICE" or "" (any other console output). With diag=True it also
    keeps the firmware's diagnostics stream alive by re-sending "diag on" every few seconds. send() queues a
    console command (e.g. "drive 60 60"). Stop with close().
    """

    KEEPALIVE_S = 5.0

    def __init__(self, port: str, on_line: Callable[[str, dict, str], None], on_end: Callable[[str], None],
                 diag: bool = True):
        self.port = port
        self.on_line = on_line
        self.on_end = on_end
        self.diag = diag
        self._stop = threading.Event()
        self._out: "queue.Queue[str]" = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def close(self):
        self._stop.set()

    def send(self, command: str):
        self._out.put(command)

    def _run(self):
        import serial
        s = serial.Serial()
        s.port, s.baudrate, s.timeout = self.port, 115200, 0.1
        s.dtr = False
        s.rts = False
        try:
            s.open()
        except serial.SerialException as e:
            self.on_end(f"Couldn't open {self.port}: {e}")
            return
        reason = "Disconnected"
        try:
            last_keepalive = 0.0
            buf = b""
            while not self._stop.is_set():
                if self.diag and time.time() - last_keepalive >= self.KEEPALIVE_S:
                    s.write(b"diag on\n")
                    last_keepalive = time.time()
                while not self._out.empty():
                    s.write(self._out.get_nowait().encode() + b"\n")
                buf += s.read(1024)
                *lines, buf = buf.split(b"\n")
                for raw in lines:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    m = DIAG_RE.search(line)
                    if m:
                        fields = dict(kv.split("=", 1) for kv in m.group(2).split())
                        self.on_line(m.group(1), fields, line)
                    else:
                        self.on_line("", {}, line)
                if len(buf) > 65536:  # a camera frame line is ~8 KB; anything this long without a newline is noise
                    buf = buf[-1024:]
            try:
                if self.diag:
                    s.write(b"diag off\n")
                while not self._out.empty():  # e.g. a final "stop" for the motors
                    s.write(self._out.get_nowait().encode() + b"\n")
            except serial.SerialException:
                pass
        except serial.SerialException as e:
            reason = f"Connection lost: {e}"
        finally:
            s.close()
            self.on_end(reason)
