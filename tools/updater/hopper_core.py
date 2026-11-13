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

import serial.tools.list_ports

ESPRESSIF_VID = 0x303A
# USB-serial bridges commonly used on ESP32 boards, for nicer labels
BRIDGES = {0x303A: "ESP32 native USB", 0x1A86: "CH34x", 0x10C4: "CP210x", 0x0403: "FTDI"}
MiB = 1024 * 1024

APP_NAME = "DimensionHopper"
APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP_NAME
CACHE_DIR = APP_DIR / "cache"
BACKUP_DIR = APP_DIR / "backups"
CONFIG_PATH = APP_DIR / "config.json"

# Built-in choices for the "Latest GitHub release" dropdown: the stock firmware and this fork.
# More can be added at runtime (Repos… dialog); those live in config["custom_repos"].
# The fork first: this app ships with the fork's releases, so that's what a new user means to install.
PRESET_REPOS = {
    "bot": ["Exadon/ButterBot-Firmware-Public", "CircuitMess/ButterBot-Firmware-Public"],
    "ctrl": ["Exadon/ButterBotCtrl-Firmware-Public", "CircuitMess/ButterBotCtrl-Firmware-Public"],
}
STOCK_OWNER = "CircuitMess"

DEFAULT_CONFIG = {
    "bot_repo": "Exadon/ButterBot-Firmware-Public",       # the fork by default (STOCK is one click away)
    "ctrl_repo": "Exadon/ButterBotCtrl-Firmware-Public",
    "custom_repos": {"bot": [], "ctrl": []},
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

    LABEL = {BOT: "ButterBot", CTRL: "Controller"}
    FLASH = {BOT: 16 * MiB, CTRL: 4 * MiB}


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

    @property
    def kind(self) -> Optional[str]:
        if self.flash_bytes >= 16 * MiB:
            return Kind.BOT
        if 0 < self.flash_bytes <= 4 * MiB:
            return Kind.CTRL
        return None


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
    """stdout replacement that splits esptool's \\r progress updates into separate lines."""

    def __init__(self, on_line: Callable[[str], None]):
        self.on_line = on_line
        self.buf = ""

    def write(self, s: str) -> int:
        self.buf += s
        while True:
            m = re.search(r"[\r\n]", self.buf)
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
                on_progress: Callable[[int], None] = lambda p: None) -> str:
    """
    Runs esptool in-process (works inside a --windowed PyInstaller exe, where a child process
    would have no stdout). Serialized: esptool keeps global state and we redirect sys.stdout.
    Returns the full output; raises EsptoolError on failure.
    """
    import esptool

    out: list[str] = []

    def line(s: str):
        out.append(s)
        m = _PCT.search(s)
        if m:
            on_progress(min(100, int(m.group(1))))
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


def _base_args(port: str, baud: int) -> list[str]:
    return ["--chip", "esp32s3", "--port", port, "--baud", str(baud)]


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
    res.parked = True
    # The controller keeps its USB power through a reboot, so don't leave it stuck in the bootloader: a
    # parked chip runs no firmware - no screen, no BLE, no pairing - until it's power-cycled. Flashing
    # later resets it back into the bootloader by itself.
    if res.kind == Kind.CTRL:
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
APP_OFFSET = 0x10000          # factory app partition on both boards
PART_TABLE_OFFSET = 0x8000
PART_TABLE_MAGIC = b"\xAA\x50"


def read_app_info(data: bytes, image_offset: int) -> Optional[AppInfo]:
    """esp_app_desc_t sits 0x20 bytes into an app image (after the image + first segment headers)."""
    d = image_offset + 0x20
    if len(data) < d + 144 or int.from_bytes(data[d:d + 4], "little") != APP_DESC_MAGIC:
        return None

    def txt(off, n):
        return data[d + off:d + off + n].split(b"\0")[0].decode("utf-8", "replace")
    return AppInfo(project=txt(48, 32), version=txt(16, 32), time=txt(80, 16), date=txt(96, 16), idf=txt(112, 32))


def kind_from_project(project: str) -> Optional[str]:
    p = project.lower()
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
        head = f.read(APP_OFFSET + 0x200)
    if head[:1] != b"\xE9":
        raise ImageError(f"{path.name} doesn't start with an ESP image header (0xE9). "
                         "Pick a merged full-flash .bin (like the GitHub releases) or a build folder.")

    if app_at_0 := read_app_info(head, 0):
        raise ImageError(f"{path.name} is only the APP part of a build ({app_at_0.short()}). "
                         "Writing it at 0x0 would wipe the bootloader and brick the board. "
                         "Use 'idf.py build folder' and pick the build/ folder instead."
                         + ("  (This looks like a backup of an already-broken board - don't restore it.)"
                            if source_of(path) == "BACKUP" else ""))
    if head[PART_TABLE_OFFSET:PART_TABLE_OFFSET + 2] != PART_TABLE_MAGIC:
        raise ImageError(f"{path.name} has no partition table at 0x8000, so it isn't a complete flash image "
                         "(maybe a bootloader or data partition on its own). Writing it at 0x0 would brick the board.")
    app = read_app_info(head, APP_OFFSET)
    if app is None:
        raise ImageError(f"{path.name} has a partition table but no valid app at 0x10000 - not bootable.")

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
        kind = Kind.CTRL
    desc = build_dir / "project_description.json"
    name = build_dir.parent.name
    if desc.exists():
        try:
            name = json.loads(desc.read_text(encoding="utf-8")).get("project_name", name)
        except ValueError:
            pass

    # A build must include the bootloader at 0x0 and the app at 0x10000, or the board won't boot.
    offsets = {off for off, _ in parts}
    if 0 not in offsets or APP_OFFSET not in offsets:
        raise ImageError("This build doesn't include both the bootloader (0x0) and the app (0x10000) - "
                         "run a full `idf.py build` first.")
    app_path = dict(parts)[APP_OFFSET]
    with open(app_path, "rb") as f:
        app = read_app_info(f.read(0x200), 0)
    if app and not kind:
        kind = kind_from_project(app.project)
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
        for parent in list(Path(os.path.abspath(start)).parents)[:5]:
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
    dest = BACKUP_DIR / f"{Kind.LABEL[target]}-{port}-{stamp}.bin"
    run_esptool(_base_args(port, baud) + ["--after", "no_reset", "read_flash", "0", hex(flash_bytes), str(dest)],
                on_line, on_progress)
    if not dest.exists() or dest.stat().st_size != flash_bytes:
        raise EsptoolError("Backup came back short - not flashing without a good backup")
    return dest


def write(port: str, plan: FlashPlan, baud: int, on_line, on_progress):
    args = _base_args(port, baud) + ["--before", "default_reset", "--after", "hard_reset", "write_flash", "-z"]
    args += plan.extra_write_args or ["--flash_mode", "keep", "--flash_freq", "keep", "--flash_size", "keep"]
    for off, p in plan.parts:
        args += [hex(off), str(p)]
    # esptool verifies each region's MD5 after writing ("Hash of data verified")
    run_esptool(args, on_line, on_progress)


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
        if off <= PART_TABLE_OFFSET < off + len(data):
            rel = PART_TABLE_OFFSET - off
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
            esp.change_baud(baud)
            esp.flash_set_parameters(flash_bytes)

            if keep_pairing:
                img_table = _image_table(regions)
                chip_table = esp.read_flash(PART_TABLE_OFFSET, 0xC00)
                img_parts = parse_partitions(img_table) if img_table else parse_partitions(chip_table)
                if img_table is None or img_table[:len(chip_table)] == chip_table:
                    for p in img_parts:
                        if p.type == 0x01 and p.subtype in NVS_SUBTYPES:
                            regions = _cut(regions, p.offset, p.offset + p.size)
                    stats.kept_nvs = True
                else:
                    stats.note = "partition layout changed, so pairing and settings are reset this time"

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
        run_esptool(args, on_line, lambda p: on_progress(20 + p * 80 // 100))
    return stats


# ---------------------------------------------------------------------------------------------
# After a flash: wait for the board to run the new firmware
# ---------------------------------------------------------------------------------------------

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


def console_command(port: str, command: str, reply_tag: str, timeout_s: float = 4.0) -> Optional[dict]:
    """Sends a console command without resetting the board and returns the fields of the [reply_tag] reply.
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
                buf += s.read(512)
                for line in buf.decode("utf-8", "replace").splitlines():
                    if m := pat.search(line):
                        return dict(kv.split("=", 1) for kv in m.group(1).split())
                buf = buf[-2048:]
        except serial.SerialException:
            return dict(PORT_LOST)
        finally:
            try:
                s.close()
            except serial.SerialException:
                pass
    return None


# ---------------------------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------------------------

def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    cr = cfg.get("custom_repos")
    if not isinstance(cr, dict):
        cr = {}
    cfg["custom_repos"] = {k: [r for r in cr.get(k, []) if isinstance(r, str)] for k in ("bot", "ctrl")}
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
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606 - opening our own data folder in Explorer



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
                worry |= low < 8
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

DIAG_RE = re.compile(r"\[(BBDIAG|BBSTAT|BBDRIVE|BBFORGET|BBVOICE|BBCAM)\]((?:\s+\w+=\S+)+)")


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
