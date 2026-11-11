"""
Dimension Hopper core: everything that talks to hardware, GitHub or the disk.
No UI code lives here, so it can be tested headless.

Both ButterBot and its controller are ESP32-S3 boards. They may show up through a USB-serial
bridge (the robot uses a CH340, VID 0x1A86) or the S3's native USB (VID 0x303A), so USB IDs
can't tell them apart. Their flash chips can: the robot has 16 MB, the controller 4 MB.
A probe (esptool flash_id) reads that.

The robot's CH340 sits on the robot's own power rail, and the stock firmware powers the robot
off when it boots without the power button held. So after a probe or a backup we leave the
chip parked in the ROM bootloader (--after no_reset) instead of rebooting it; only the final
write reboots into the new firmware.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
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
PRESET_REPOS = {
    "bot": ["CircuitMess/ButterBot-Firmware-Public", "Exadon/ButterBot-Firmware-Public"],
    "ctrl": ["CircuitMess/ButterBotCtrl-Firmware-Public"],
}
STOCK_OWNER = "CircuitMess"

DEFAULT_CONFIG = {
    "bot_repo": "CircuitMess/ButterBot-Firmware-Public",
    "ctrl_repo": "CircuitMess/ButterBotCtrl-Firmware-Public",
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


def find_fork_build() -> Optional[Path]:
    """
    The fork's own build folder, if we can find it: Dimension Hopper lives in <repo>/tools/updater
    (the exe in <repo>/tools/updater/dist), so walk up looking for <repo>/build/flasher_args.json.
    """
    starts = [Path(sys.executable).resolve(), Path(__file__).resolve()]
    for start in starts:
        for parent in list(start.parents)[:5]:
            cand = parent / "build"
            if (cand / "flasher_args.json").exists() and (parent / "main").is_dir():
                return cand
    return None


@dataclass
class Release:
    repo: str
    tag: str
    asset_name: str
    url: str
    size: int

    def cache_path(self) -> Path:
        return CACHE_DIR / self.repo.replace("/", "__") / self.tag / self.asset_name


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
    return Release(repo, data["tag_name"], a["name"], a["browser_download_url"], a["size"])


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


def save_config(cfg: dict):
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except OSError:
        pass


def open_folder(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606 - opening our own data folder in Explorer

