"""
Talks to the Clockstar watch over USB (its UsbConsole, firmware with the ButterBot app): clock sync, the main
menu's order, and themes.

Every reply line starts with "[CS] ". The port is opened without toggling DTR/RTS - doing that resets the watch.
All calls block (run them off the UI thread) and raise WatchError when the watch doesn't answer as expected.
"""
from __future__ import annotations

import base64
import calendar
import json
import sys
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import hopper_core as core

ID_RE = re.compile(r"[A-Za-z0-9_-]{1,8}")
CHUNK = 768                      # bytes per upload line (1 KB of base64 - the watch takes lines up to 1100)


class WatchError(Exception):
    pass


@dataclass
class App:
    id: str
    shown: bool
    title: str


@dataclass
class Info:
    fw: str
    theme: str            # "" = the watch's own look
    used: int
    total: int
    time: int
    clock: int = 24       # 24 = 24-hour clock, 12 = 12-hour with AM/PM (older watch firmware: 24)


@dataclass
class Steps:
    today: int
    goal: int             # 0 = no goal
    sens: int             # 0 low, 1 normal, 2 high
    nudge: bool           # an hour of barely moving: a notification
    days: list[int]       # today first, then the 6 days before


@dataclass
class Alarm:
    on: bool
    hour: int
    minute: int
    days: int             # bit 0 Monday .. bit 6 Sunday; 0 = once


DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass
class RobotLink:
    on: bool              # the watch stays linked to ButterBot in the background
    state: str            # connected / off / scanning / notfound / lost / connecting
    battery: int          # the robot's battery %, -1 = not heard
    charge: int           # 0 unplugged, 1 charging, 2 full


@dataclass
class Theme:
    id: str
    name: str
    description: str
    folder: Path          # on this PC: theme.json, preview.png and the .bin files

    def files(self) -> list[tuple[str, Path]]:
        """(path on the watch relative to the theme folder, file here) for every image."""
        return sorted((p.relative_to(self.folder).as_posix(), p) for p in self.folder.rglob("*.bin"))

    def problems(self) -> list[str]:
        """Why the watch couldn't take this theme: it stores it as t/<id>/<file>, 31 characters at most."""
        out = []
        if not ID_RE.fullmatch(self.id):
            out.append(f"theme id '{self.id}': 1-8 letters, digits, - or _")
        out += [f"{rel}: path too long for the watch" for rel, _ in self.files() if len(f"t/{self.id}/{rel}") > 31]
        return out

    @property
    def size(self) -> int:
        return sum(p.stat().st_size for _, p in self.files())


def bundled_themes_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "themes"


def user_themes_dir() -> Path:
    """Imported themes live next to Dimension Hopper's settings."""
    d = core.APP_DIR / "watch_themes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def local_themes() -> list[Theme]:
    out: dict[str, Theme] = {}
    for root in (bundled_themes_dir(), user_themes_dir()):
        if not root.is_dir():
            continue
        for folder in sorted(root.iterdir()):
            meta = folder / "theme.json"
            if not meta.is_file():
                continue
            try:
                j = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            tid = j.get("id") or folder.name
            out[tid] = Theme(tid, j.get("name") or tid, j.get("description", ""), folder)
    return list(out.values())


def local_epoch() -> int:
    """Now as the watch counts it: local wall-clock time as if it were UTC (the watch has no time zone)."""
    return calendar.timegm(time.localtime())


class Watch:
    """One USB session with the watch. Use as a context manager."""

    def __init__(self, port: str):
        self.port = port
        self.s = None

    def __enter__(self):
        import serial
        core._esptool_lock.acquire()
        try:
            s = serial.Serial()
            s.port, s.baudrate, s.timeout, s.write_timeout = self.port, 115200, 0.1, 5
            s.dtr = False
            s.rts = False
            s.open()
        except Exception as e:
            core._esptool_lock.release()
            raise WatchError(f"Couldn't open {self.port}: {e}") from e
        self.s = s
        time.sleep(0.15)
        s.reset_input_buffer()
        return self

    def __exit__(self, *exc):
        try:
            self.s.close()
        except Exception:
            pass
        core._esptool_lock.release()

    # --- low level ---
    def _send(self, line: str):
        self.s.write(line.encode() + b"\n")

    def _lines(self, until: Callable[[str], bool], timeout: float = 4.0) -> list[str]:
        """[CS] reply lines (without the tag) until until(line) is true."""
        out, buf, end = [], b"", time.time() + timeout
        while time.time() < end:
            # whatever has arrived (at least a byte): read(4096) would sit out the port's whole timeout every call
            buf += self.s.read(max(1, self.s.in_waiting))
            *done, buf = buf.split(b"\n")
            for raw in done:
                text = raw.decode("utf-8", "replace").strip()
                i = text.find("[CS] ")
                if i < 0:
                    continue
                line = text[i + 5:]
                out.append(line)
                if line.startswith("err "):
                    raise WatchError(line[4:])
                if until(line):
                    return out
        raise WatchError("the watch didn't answer - is it running firmware with the ButterBot app?")

    def _one(self, cmd: str, timeout: float = 4.0) -> str:
        self._send(cmd)
        return self._lines(lambda l: True, timeout)[-1]

    # --- commands ---
    def info(self) -> Info:
        line = self._one("info")
        if not line.startswith("info "):
            raise WatchError(f"unexpected answer: {line}")
        kv = dict(p.split("=", 1) for p in line[5:].split() if "=" in p)
        return Info(kv.get("fw", "?"), "" if kv.get("theme") in (None, "-") else kv["theme"],
                    int(kv.get("used", 0)), int(kv.get("total", 0)), int(kv.get("time", 0)),
                    12 if kv.get("clock") == "12" else 24)

    def sync_time(self) -> int:
        epoch = local_epoch()
        self._one(f"time {epoch}")
        return epoch

    def set_clock(self, hours: int):
        """24 = 24-hour clock, 12 = 12-hour with AM/PM."""
        line = self._one(f"clock {12 if hours == 12 else 24}")
        if line != "ok":
            raise WatchError("this watch firmware can't switch the clock style - update it from the Flash tab"
                             if "unknown" in line else line)

    def robot_link(self) -> RobotLink | None:
        """The background link to ButterBot (None: the watch firmware is older and has none)."""
        try:
            line = self._one("bb")
        except WatchError:
            return None
        if not line.startswith("bb "):
            return None
        kv = dict(p.split("=", 1) for p in line[3:].split() if "=" in p)
        return RobotLink(kv.get("link") == "1", kv.get("state", "?"), int(kv.get("batt", -1)), int(kv.get("charge", 0)))

    def set_robot_link(self, on: bool):
        line = self._one(f"bb link {1 if on else 0}")
        if line != "ok":
            raise WatchError(line)

    def test_notify(self):
        line = self._one("bb test")
        if line != "ok":
            raise WatchError(line)

    # --- steps, alarms, battery history (None / [] on watch firmware without them) ---
    def steps(self) -> Steps | None:
        try:
            line = self._one("steps")
        except WatchError:
            return None
        if not line.startswith("steps "):
            return None
        kv = dict(p.split("=", 1) for p in line[6:].split() if "=" in p)
        return Steps(int(kv["today"]), int(kv["goal"]), int(kv["sens"]), kv["nudge"] == "1",
                     [int(x) for x in kv["days"].split(",")])

    def set_steps(self, goal: int | None = None, sens: int | None = None, nudge: bool | None = None):
        for cmd in ([f"steps goal {goal}"] if goal is not None else []) +                    ([f"steps sens {sens}"] if sens is not None else []) +                    ([f"steps nudge {1 if nudge else 0}"] if nudge is not None else []):
            if self._one(cmd) != "ok":
                raise WatchError(f"{cmd}: no ok")

    def alarms(self) -> list[Alarm] | None:
        self._send("alarms")
        try:
            lines = self._lines(lambda l: l == "end")
        except WatchError:
            return None
        out = []
        for l in lines:
            if l.startswith("alarm "):
                _, _, on, hm, days = l.split()
                h, m = hm.split(":")
                out.append(Alarm(on == "1", int(h), int(m), int(days)))
        return out

    def set_alarm(self, number: int, a: Alarm):
        """number: 1-4"""
        if self._one(f"alarm {number} {1 if a.on else 0} {a.hour:02d}:{a.minute:02d} {a.days}") != "ok":
            raise WatchError("the alarm wasn't saved")

    def batt_history(self) -> tuple[int, list[int]] | None:
        """(minutes between samples, samples oldest first: percent, +128 while charging)."""
        self._send("batthist")
        try:
            lines = self._lines(lambda l: l == "end", timeout=6)
        except WatchError:
            return None
        every, vals = 10, []
        for l in lines:
            if l.startswith("batthist "):
                every = int(dict(p.split("=", 1) for p in l[9:].split()).get("every", 10))
            elif l.startswith("bh "):
                vals += [int(x) for x in l[3:].split(",") if x]
        return every, vals

    def ring(self):
        """Rings the watch like a finished timer (to hear the alarm sound)."""
        if self._one("ring") != "ok":
            raise WatchError("no ring")

    def stat(self) -> dict[str, str]:
        """Uptime, memory, why it last restarted, its battery ({} on older watch firmware)."""
        try:
            line = self._one("stat")
        except WatchError:
            return {}
        return dict(p.split("=", 1) for p in line[5:].split() if "=" in p) if line.startswith("stat ") else {}

    def log(self) -> list[str]:
        """The watch's recent warnings and errors, oldest first."""
        self._send("log")
        try:
            return [l[4:] for l in self._lines(lambda l: l == "end") if l.startswith("log ")]
        except WatchError:
            return []

    def apps(self) -> list[App]:
        self._send("apps")
        apps = []
        for line in self._lines(lambda l: l == "end"):
            if line.startswith("app "):
                _, aid, shown, title = line.split(" ", 3)
                apps.append(App(aid, shown == "1", title))
        return apps

    def set_apps(self, apps: list[App]):
        self._one("apps set " + ",".join(("" if a.shown else "-") + a.id for a in apps))

    def themes(self) -> tuple[dict[str, int], str]:
        """Installed themes {id: bytes} and the active one ("" = none)."""
        self._send("themes")
        installed, active = {}, ""
        for line in self._lines(lambda l: l == "end"):
            parts = line.split()
            if parts[0] == "theme" and len(parts) >= 4:
                installed[parts[1]] = int(parts[3])
            elif parts[0] == "active" and len(parts) >= 2:
                active = "" if parts[1] == "-" else parts[1]
        return installed, active

    def use_theme(self, theme_id: str):
        """Switches theme ("" = the watch's own look). The watch restarts to apply it."""
        self._one(f"theme set {theme_id or '-'}")

    def remove_theme(self, theme_id: str):
        self._one(f"theme rm {theme_id}", timeout=10)

    def upload_theme(self, theme: Theme, progress: Callable[[int, int], None] = lambda done, total: None):
        if bad := theme.problems():
            raise WatchError("; ".join(bad))
        files = theme.files()
        total = sum(p.stat().st_size for _, p in files)
        done = 0
        for rel, path in files:
            data = path.read_bytes()
            self._send(f"put t/{theme.id}/{rel} {len(data)}")
            self._lines(lambda l: l == "ready")
            for i in range(0, len(data), CHUNK):
                self._send(base64.b64encode(data[i:i + CHUNK]).decode())
                last = i + CHUNK >= len(data)
                # one line at a time: the watch's USB buffer doesn't hold the PC back, it drops what doesn't fit
                self._lines(lambda l: l == "done" if last else l.startswith("got "), timeout=6)
                done += min(CHUNK, len(data) - i)
                progress(done, total)
