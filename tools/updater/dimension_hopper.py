"""
DIMENSION HOPPER - interdimensional firmware delivery for ButterBot and its controller.

A mad-science themed front end for esptool:
  * finds the boards, probes them (16 MB flash = robot, 4 MB = controller)
  * pulls the latest GitHub release (stock or your fork), a local .bin, or an `idf.py build` folder
  * backs up the current flash first, refuses mismatched images, verifies the write
"""
from __future__ import annotations

import math
import os
import queue
import random
import sys
import threading
import time
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, ttk

import hopper_core as core
import mac_compat
import self_update
from guide_tab import GuideTab
from diag_tab import DiagnosticsTab
from console_tab import ConsoleTab
from controller_tab import ControllerTab
from ai_tab import AITab
from personalize_tab import PersonalizeTab
from routines_tab import RoutinesTab
from hop_dialog import HopDialog
import tooltip
from hopper_core import Kind
import backup as backup_mod

APP_TITLE = "Dimension Hopper"
VERSION = "1.10.0"
ROBOT_REPO = "Exadon/ButterBot-Firmware-Public"
CTRL_REPO = "Exadon/ButterBotCtrl-Firmware-Public"

# --- palette: portal-fluid green on lab-floor black --------------------------------------
BG = "#070b08"
PANEL = "#0e1510"
PANEL_HI = "#142018"
EDGE = "#1f3a24"
GREEN = "#39ff6a"
LIME = "#b6ff3b"
TEAL = "#1fd6b0"
DIM = "#5f8a68"
TEXT = "#d9ffe0"
WARN = "#ffb627"
BAD = "#ff4d6d"
MONO = ("Cascadia Mono", 9) if sys.platform in ("win32", "darwin") else ("Courier", 10)  # Menlo on a Mac

QUIPS = {
    "scan": ["Sniffing the USB multiverse...", "Interrogating serial ports. They'll talk.",
             "Pinging every dimension with a COM port..."],
    "probe": ["Poking the chip with a science stick...", "Asking the silicon how big its brain is...",
              "Measuring flash. Size matters here, literally."],
    "download": ["Siphoning firmware from the GitHub dimension...", "Pulling bits through the wormhole...",
                 "Downloading. Don't cross the streams. Different franchise."],
    "backup": ["Backing up this reality in case we need to come back...",
               "Photographing the old firmware before we vaporize it...",
               "Saving a lifeboat copy. Science is mostly undo buttons."],
    "flash": ["Folding spacetime around the flash chip...", "Injecting portal fluid into sector 0x0...",
              "Rewriting reality, one 4 KB sector at a time...", "Do NOT unplug. The universe is load-bearing."],
    "done": ["Firmware landed. Nobody exploded. Great day for science.", "Delivery complete. Dimension stabilized.",
             "Done. Verified. Smug about it."],
    "fail": ["The portal collapsed. Classic.", "Science happened, just not the good kind.",
             "That dimension rejected us. Check the log."],
}


def quip(kind: str) -> str:
    return random.choice(QUIPS[kind])


def load_art(name: str, size: int):
    """Loads a bundled PNG (works from source and inside the PyInstaller exe), resized to size px."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    path = base / name
    if not path.exists():
        return None
    try:
        from PIL import Image, ImageTk
        im = Image.open(path)
        scale = size / max(im.size)  # fit inside size x size, keeping the aspect ratio
        return ImageTk.PhotoImage(im.resize((round(im.width * scale), round(im.height * scale)), Image.LANCZOS))
    except Exception:
        try:
            return tk.PhotoImage(file=str(path))
        except tk.TclError:
            return None


# =============================================================================================
# Portal animation
# =============================================================================================

class Portal(tk.Canvas):
    """A procedurally drawn swirling portal. Spins faster while busy, flashes on success/failure."""

    ARMS = 7

    def __init__(self, master, size=150):
        super().__init__(master, width=size, height=size, bg=BG, highlightthickness=0)
        self.size = size
        self.c = size / 2
        self.angle = 0.0
        self.speed = 1.6
        self.target_speed = 1.6
        self.tint = None
        self.tint_until = 0.0
        self.particles = [self._new_particle() for _ in range(26)]
        self._tick()

    def _new_particle(self):
        return [random.uniform(0, 2 * math.pi), random.uniform(self.size * 0.30, self.size * 0.49),
                random.uniform(0.6, 1.8)]

    def busy(self, on: bool):
        self.target_speed = 9.0 if on else 1.6

    def flash(self, color: str, secs: float = 1.6):
        self.tint, self.tint_until = color, time.time() + secs

    def _tick(self):
        self.speed += (self.target_speed - self.speed) * 0.08
        self.angle = (self.angle + self.speed) % 360
        self.delete("all")
        c, s = self.c, self.size
        tint = self.tint if time.time() < self.tint_until else None
        ring = [tint or GREEN, tint or LIME, tint or TEAL]

        # outer glow
        for i, w in enumerate((14, 9, 5)):
            r = s * 0.47 - i
            self.create_oval(c - r, c - r * 0.92, c + r, c + r * 0.92, outline=["#0f2a16", "#1a5a2a", ring[0]][i], width=w)
        # swirl arms: arcs of shrinking radius, each offset in angle
        for k in range(self.ARMS):
            for j in range(5):
                r = s * (0.42 - j * 0.07)
                if r <= 4:
                    continue
                start = (self.angle * (1 + j * 0.35) + k * 360 / self.ARMS + j * 23) % 360
                self.create_arc(c - r, c - r * 0.92, c + r, c + r * 0.92, start=start, extent=34 - j * 3,
                                style="arc", outline=ring[(k + j) % 3], width=max(1, 4 - j))
        # sucked-in particles
        for p in self.particles:
            p[0] += math.radians(self.speed * 1.4)
            p[1] -= p[2] * (self.speed / 3.2)
            if p[1] < s * 0.06:
                p[:] = self._new_particle()
            x, y = c + math.cos(p[0]) * p[1], c + math.sin(p[0]) * p[1] * 0.92
            self.create_oval(x - 1.5, y - 1.5, x + 1.5, y + 1.5, fill=ring[1], outline="")
        # bright core
        for i, col in enumerate(("#123d1e", "#1f7a37", ring[0], "#eaffef")):
            r = s * (0.13 - i * 0.03)
            self.create_oval(c - r, c - r, c + r, c + r, fill=col, outline="")
        self.after(33, self._tick)


# =============================================================================================
# One device slot (robot or controller)
# =============================================================================================

class DeviceSlot(tk.Frame):
    SOURCES = (("release", "Latest GitHub release"), ("file", "Local .bin / backup"), ("build", "idf.py build folder"))

    def __init__(self, app: "App", master, kind: str):
        super().__init__(master, bg=PANEL, highlightbackground=EDGE, highlightthickness=1)
        self.app, self.kind = app, kind
        self.probe: core.ProbeResult | None = None
        self.release: core.Release | None = None
        self.local_path: Path | None = None

        pad = dict(padx=14)
        head = tk.Frame(self, bg=PANEL)
        head.pack(fill="x", pady=(10, 4), **pad)
        self.icon_img = load_art("bot.png" if kind == Kind.BOT else "controller.png", 84)
        if self.icon_img is not None:
            tk.Label(head, image=self.icon_img, bg=PANEL).pack(side="left", padx=(0, 10))
        names = tk.Frame(head, bg=PANEL)
        names.pack(side="left", anchor="center")
        tk.Label(names, text=Kind.LABEL[kind].upper(), bg=PANEL, fg=GREEN,
                 font=("Segoe UI Black", 15)).pack(anchor="w")
        tk.Label(names, text="16 MB · robot" if kind == Kind.BOT else "4 MB · handheld", bg=PANEL, fg=DIM,
                 font=("Segoe UI", 9)).pack(anchor="w")

        # port row
        row = tk.Frame(self, bg=PANEL)
        row.pack(fill="x", pady=4, **pad)
        tk.Label(row, text="Port", bg=PANEL, fg=DIM, width=7, anchor="w").pack(side="left")
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(row, textvariable=self.port_var, state="readonly", style="Hop.TCombobox")
        self.port_box.pack(side="left", fill="x", expand=True)
        self.port_box.bind("<<ComboboxSelected>>", lambda e: self._port_changed())
        self.probe_btn = app.button(row, "Probe", lambda: app.run_probe(self), small=True)
        self.probe_btn.pack(side="left", padx=(6, 0))
        self.status_btn = app.button(row, "Status", lambda: app.run_status(self), small=True)
        self.status_btn.pack(side="left", padx=(6, 0))
        self.wake_btn = app.button(row, "Wake", lambda: app.run_wake(self), small=True)
        self.wake_btn.pack(side="left", padx=(6, 0))
        self.forget_btn = app.button(row, "Forget pairing", lambda: app.run_forget(self), small=True)
        self.forget_btn.pack(side="left", padx=(6, 0))
        self.parked_lbl = tk.Label(self, text="⏸ PARKED IN BOOTLOADER - not running, can't pair. Beam or hit Wake.",
                                   bg=PANEL, fg=WARN, anchor="w", font=("Segoe UI Semibold", 8))

        self.chip_lbl = tk.Label(self, text="No board identified yet", bg=PANEL, fg=DIM, anchor="w",
                                 justify="left", wraplength=420, font=("Segoe UI", 9))
        self.chip_lbl.pack(fill="x", **pad)
        # health (RAM, uptime, last reset, boots) and how the board's firmware compares with the latest release
        self.health_lbl = tk.Label(self, text="", bg=PANEL, fg=DIM, anchor="w", justify="left", wraplength=420,
                                   font=("Segoe UI", 8))
        self.health_lbl.pack(fill="x", **pad)
        self.release_lbl = tk.Label(self, text="", bg=PANEL, fg=DIM, anchor="w", justify="left", wraplength=420,
                                    font=("Segoe UI Semibold", 8))
        self.release_lbl.pack(fill="x", **pad)
        self.dev_status: core.DeviceStatus | None = None

        # source
        src = tk.Frame(self, bg=PANEL)
        src.pack(fill="x", pady=(10, 2), **pad)
        tk.Label(src, text="Payload", bg=PANEL, fg=DIM, width=7, anchor="nw").pack(side="left", anchor="n")
        radios = tk.Frame(src, bg=PANEL)
        radios.pack(side="left", fill="x", expand=True)
        self.src_var = tk.StringVar(value="release")
        for key, text in self.SOURCES:
            tk.Radiobutton(radios, text=text, value=key, variable=self.src_var, command=self._source_changed,
                           bg=PANEL, fg=TEXT, selectcolor=PANEL_HI, activebackground=PANEL,
                           activeforeground=GREEN, highlightthickness=0, anchor="w",
                           font=("Segoe UI", 9)).pack(fill="x")

        self.repo_row = tk.Frame(self, bg=PANEL)
        self.repo_lbl = tk.Label(self.repo_row, text="Repo", bg=PANEL, fg=DIM, width=7, anchor="w")
        self.repo_lbl.pack(side="left")
        self.repo_var = tk.StringVar(value=app.cfg["bot_repo" if kind == Kind.BOT else "ctrl_repo"])
        # A dropdown, not free text: stock + fork presets, plus whatever was added under "Repos…".
        # The badge next to it makes "am I flashing stock or my fork?" impossible to miss.
        self.repo_box = ttk.Combobox(self.repo_row, textvariable=self.repo_var, state="readonly",
                                     style="Hop.TCombobox")
        self.repo_box.bind("<<ComboboxSelected>>", lambda ev: self.refresh_release())
        self.repo_badge = tk.Label(self.repo_row, text="", bg=PANEL, font=("Segoe UI Black", 8), padx=6)
        self.repos_btn = app.button(self.repo_row, "Repos…", app.open_repo_dialog, small=True)
        self.browse_btn = app.button(self.repo_row, "Browse…", self._browse, small=True)
        self.fork_build = core.find_fork_build(kind)
        self.fork_btn = app.button(self.repo_row, "Use this fork's build", self._use_fork_build, small=True)
        self.src_badge = tk.Label(self.repo_row, text="", bg=PANEL, font=("Segoe UI Black", 8), padx=6)
        self._pack_release_widgets()
        self.reload_repo_choices()

        self.payload_lbl = tk.Label(self, text="", bg=PANEL, fg=TEXT, anchor="w", justify="left",
                                    wraplength=380, font=("Segoe UI", 9))

        # progress + status
        self.bar = ttk.Progressbar(self, style="Hop.Horizontal.TProgressbar", maximum=100)
        self.status = tk.Label(self, text="Idle. Suspiciously idle.", bg=PANEL, fg=DIM, anchor="w",
                               justify="left", wraplength=380, font=("Segoe UI", 9))
        self.go_btn = app.button(self, "BEAM IT  ▶", lambda: app.run_update([self]), big=True)

        self.repo_row.pack(fill="x", pady=(6, 2), **pad)
        self.payload_lbl.pack(fill="x", pady=(2, 8), **pad)
        self.bar.pack(fill="x", pady=(4, 2), **pad)
        self.status.pack(fill="x", **pad)
        self.go_btn.pack(fill="x", pady=(10, 14), **pad)
        self._last_repo = self.repo_var.get()

    # --- source handling ------------------------------------------------------------------
    def _source_changed(self):
        mode = self.src_var.get()
        for w in (self.repo_lbl, self.repo_box, self.repo_badge, self.repos_btn, self.browse_btn,
                  self.fork_btn, self.src_badge):
            w.pack_forget()
        self.repo_lbl.pack(side="left")
        if mode == "release":
            self.repo_lbl.configure(text="Repo")
            self._pack_release_widgets()
            self.refresh_release()
            return
        self.repo_lbl.configure(text="File" if mode == "file" else "Folder")
        self.browse_btn.pack(side="left")
        if mode == "build" and self.fork_build:
            self.fork_btn.pack(side="left", padx=(6, 0))
        self.src_badge.pack(side="left", padx=(6, 0))
        self.local_path = None
        self._set_src_badge(None)
        self.payload_lbl.configure(text="Nothing selected - hit Browse.", fg=DIM)
        # Build mode: preselect the last build folder used, else this fork's own build
        if mode == "build":
            last = self.app.cfg.get(self._last_build_key())
            start = Path(last) if last and Path(last).exists() else self.fork_build
            if start:
                self._select_local(start)

    def _last_build_key(self) -> str:
        return f"last_build_dir_{self.kind}"  # per device: a shared key leaked the robot's build into the controller

    def _browse(self):
        if self.src_var.get() == "file":
            p = filedialog.askopenfilename(title=f"{Kind.LABEL[self.kind]} firmware image",
                                           initialdir=core.BACKUP_DIR if core.BACKUP_DIR.exists() else None,
                                           filetypes=[("Firmware image", "*.bin"), ("All files", "*.*")])
        else:
            p = filedialog.askdirectory(title="Project build/ folder (after idf.py build)")
        if p:
            self._select_local(Path(p))

    def _use_fork_build(self):
        self.fork_build = core.find_fork_build(self.kind) or self.fork_build
        if self.fork_build:
            self._select_local(self.fork_build)

    BADGES = {
        "BACKUP": ("BACKUP", TEAL), "RELEASE": ("RELEASE", WARN), "IMAGE": ("IMAGE", DIM),
        "BUILD": ("LOCAL BUILD", LIME), "FORK": ("FORK BUILD", LIME), "BAD": ("NOT FLASHABLE", BAD),
        "WRONG": ("WRONG DEVICE", BAD),
    }

    def _set_src_badge(self, key):
        if key is None:
            self.src_badge.configure(text="", bg=PANEL)
            return
        text, col = self.BADGES[key]
        self.src_badge.configure(text=text, fg=BG, bg=col)

    def _select_local(self, path: Path):
        self.local_path = path
        try:
            plan = self.make_local_plan()
        except core.ImageError as e:
            # Never keep a path we refused: BEAM IT must not be able to write it
            self.local_path = None
            self._set_src_badge("BAD")
            self.payload_lbl.configure(text=str(e), fg=BAD)
            return
        if plan.kind_hint and plan.kind_hint != self.kind:
            # A robot image in the controller slot (or vice versa): refuse it right away
            self.local_path = None
            self._set_src_badge("WRONG")
            what = plan.app.short() if plan.app else plan.title
            self.payload_lbl.configure(text=f"That's {Kind.LABEL[plan.kind_hint]} firmware ({what}), but this is the "
                                            f"{Kind.LABEL[self.kind]} slot. Pick the {Kind.LABEL[self.kind].lower()}'s "
                                            "build instead.", fg=BAD)
            return
        key = plan.source
        # The user may pick either the repo root or its build/ folder
        if key == "BUILD" and self.fork_build and \
                os.path.normcase(os.path.abspath(path)) in {os.path.normcase(str(self.fork_build)),
                                                           os.path.normcase(str(self.fork_build.parent))}:
            key = "FORK"
        self._set_src_badge(key)
        if plan.source == "BUILD":
            self.app.cfg[self._last_build_key()] = str(path)
            core.save_config(self.app.cfg)
        app = f"\nContains: {plan.app.short()}" if plan.app else ""
        self.payload_lbl.configure(text=f"Ready: {plan.title}{app}", fg=TEXT)

    def make_local_plan(self) -> core.FlashPlan:
        if not self.local_path:
            raise core.ImageError("Pick a file or folder first.")
        if self.src_var.get() == "file":
            return core.classify_full_image(self.local_path)
        return core.plan_from_build_dir(self.local_path)

    def _pack_release_widgets(self):
        self.repo_box.pack(side="left", fill="x", expand=True)
        self.repo_badge.pack(side="left", padx=(6, 0))
        self.repos_btn.pack(side="left", padx=(6, 0))

    def _is_stock(self) -> bool:
        return self.repo_var.get().split("/")[0].lower() == core.STOCK_OWNER.lower()

    def reload_repo_choices(self):
        choices = core.repo_choices(self.app.cfg, self.kind)
        self.repo_box.configure(values=choices)
        if self.repo_var.get() not in choices:
            self.repo_var.set(choices[0])
        self._update_badge()

    def _update_badge(self):
        stock = self._is_stock()
        self.repo_badge.configure(text="STOCK" if stock else "FORK", fg=BG, bg=WARN if stock else LIME)

    def payload_summary(self) -> str:
        mode = self.src_var.get()
        if mode == "release":
            tag = self.release.tag if self.release else "latest release"
            return f"{self.repo_var.get()} {tag} ({'STOCK firmware' if self._is_stock() else 'fork'})"
        if self.local_path:
            what = f"{'build folder' if mode == 'build' else 'file'} {self.local_path}"
            try:
                plan = self.make_local_plan()
                what += f"  [{self.src_badge.cget('text') or plan.source}]"
                if plan.app:
                    what += f"\n      contains {plan.app.short()}"
            except core.ImageError:
                pass
            return what
        return "nothing selected"

    def refresh_release(self, only_if_changed=False):
        self._update_badge()
        repo = self.repo_var.get().strip()
        if only_if_changed and repo == self._last_repo and self.release:
            return
        self._last_repo = repo
        self.app.cfg["bot_repo" if self.kind == Kind.BOT else "ctrl_repo"] = repo
        core.save_config(self.app.cfg)
        self.release = None
        self.payload_lbl.configure(text=f"Checking {repo} for the latest release…", fg=DIM)

        def work():
            try:
                rel = core.latest_release(repo)
                self.app.ui(lambda: self._set_release(rel, repo))
            except Exception as e:  # network, 404, rate limit
                msg = str(e)
                self.app.ui(lambda: self.payload_lbl.configure(text=f"Couldn't read releases: {msg}", fg=WARN)
                            if self.repo_var.get().strip() == repo else None)
        threading.Thread(target=work, daemon=True).start()

    def _set_release(self, rel: core.Release, repo: str):
        if self.repo_var.get().strip() != repo or self.src_var.get() != "release":
            return
        self.release = rel
        cached = " · cached" if rel.cache_path().exists() else ""
        self.payload_lbl.configure(text=f"{rel.tag}  ·  {rel.asset_name}  ·  {rel.size / core.MiB:.1f} MB{cached}",
                                   fg=TEXT)
        self._show_release_note()

    def _show_release_note(self):
        st = self.dev_status
        if st is None or st.error or self.release is None:
            self.release_lbl.configure(text="")
            return
        text, newer = core.release_note(st.firmware, self.release.tag)
        self.release_lbl.configure(text=("⬆ " if newer else "✔ ") + f"{self.release.repo}: {text}" if text else "",
                                   fg=WARN if newer else GREEN)

    def show_status(self, st: core.DeviceStatus):
        """Shows what a passive status read found: who this is, battery/link/firmware, and health."""
        self.dev_status = st
        if st.error:
            self.chip_lbl.configure(text=st.error, fg=BAD)
            self.health_lbl.configure(text="")
            self.release_lbl.configure(text="")
            return
        low = st.battery is not None and st.battery <= 15
        self.chip_lbl.configure(text=f"{Kind.LABEL.get(st.kind or '', 'Board')} running  ·  " + st.summary()
                                     + ("  ⚠ low battery" if low else ""), fg=WARN if low else GREEN)
        health, worry = st.health()
        self.health_lbl.configure(text=("⚠ " if worry else "") + health, fg=WARN if worry else DIM)
        self._show_release_note()

    # --- port / probe -------------------------------------------------------------------------
    def set_ports(self, ports: list[core.PortInfo]):
        labels = [p.label() for p in ports]
        self.port_box.configure(values=labels)
        cur = self.port()
        if cur and any(l.startswith(cur + " ") for l in labels):
            return
        self.port_var.set("")

    def select_port(self, device: str):
        for l in self.port_box.cget("values"):
            if l.startswith(device + " "):
                self.port_var.set(l)
                return

    def port(self) -> str:
        return self.port_var.get().split(" ", 1)[0]

    def _port_changed(self):
        if self.probe and self.probe.port != self.port():
            self.probe = None
            self.chip_lbl.configure(text="Port changed - probe to identify the board", fg=DIM)

    def show_parked(self, on: bool):
        if on and not self.parked_lbl.winfo_ismapped():
            self.parked_lbl.pack(fill="x", padx=14, after=self.chip_lbl)
        elif not on:
            self.parked_lbl.pack_forget()

    def show_probe(self, r: core.ProbeResult):
        self.probe = r
        if r.error:
            self.chip_lbl.configure(text=f"Probe failed: {r.error}", fg=BAD)
            return
        guess = Kind.LABEL.get(r.kind or "", "unknown board")
        ok = r.kind == self.kind
        self.chip_lbl.configure(text=f"{r.chip}  ·  {r.flash_bytes // core.MiB} MB flash  ·  looks like a {guess}"
                                     + ("" if ok else "  ⚠ wrong slot?"), fg=GREEN if ok else WARN)

    def set_status(self, text: str, color=DIM, pct: int | None = None):
        self.status.configure(text=text, fg=color)
        if pct is not None:
            self.bar["value"] = pct


# =============================================================================================
# App
# =============================================================================================

class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = core.load_config()
        self.version = VERSION
        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self.monitor_ports: set[str] = set()  # ports held open by the Diagnostics tab (robot and controller)
        self.known_ports: dict[str, str] = {}  # what a scan found: Kind.BOT / Kind.CTRL -> "COM7"
        self.scan_labels: list[tk.Label] = []  # the small "found …" note next to every tab's Scan button

        root.title(f"{APP_TITLE} {VERSION}")
        root.configure(bg=BG)
        root.minsize(1080, 860)
        self._style()

        # header: full size on the Flash tab, a single slim line on the others (more room for the cards)
        top = self.header = tk.Frame(root, bg=BG)
        top.pack(fill="x", padx=18, pady=(14, 6))
        self.portal = Portal(top, 150)
        self.portal.pack(side="left")
        titles = self.titles = tk.Frame(top, bg=BG)
        titles.pack(side="left", padx=16, fill="y")
        self.title_lbl = tk.Label(titles, text="DIMENSION HOPPER", bg=BG, fg=GREEN, font=("Segoe UI Black", 30))
        self.title_lbl.pack(anchor="w", pady=(18, 0))
        self.subtitle_lbl = tk.Label(titles, text="interdimensional firmware delivery  ·  ButterBot + Controller",
                                     bg=BG, fg=LIME, font=("Segoe UI Semibold", 11))
        self.subtitle_lbl.pack(anchor="w")
        self.headline = tk.Label(titles, text="Plug a board in over USB-C. I'll do the science.", bg=BG, fg=DIM,
                                 font=("Segoe UI", 10, "italic"))
        self.headline.pack(anchor="w", pady=(6, 0))
        self.button(top, "About", self.show_about, small=True).pack(side="right", anchor="n", pady=(6, 0))
        self.report_btn = self.button(top, "Problem report…", self.make_report, small=True)
        self.report_btn.pack(side="right", anchor="n", pady=(6, 0), padx=(0, 6))
        self.compact = False

        # Boards: one place to pick (or scan for) the robot's and the controller's port; every tab uses these
        self.board_bar = tk.Frame(root, bg=PANEL, highlightbackground=EDGE, highlightthickness=1)
        self.board_bar.pack(fill="x", padx=18, pady=(0, 8))
        self.board_vars: dict[str, tk.StringVar] = {}
        self.board_boxes: dict[str, ttk.Combobox] = {}
        for kind, label in ((Kind.BOT, "ButterBot"), (Kind.CTRL, "Controller")):
            tk.Label(self.board_bar, text=label, bg=PANEL, fg=GREEN, font=("Segoe UI Semibold", 10)).pack(
                side="left", padx=(10, 6), pady=6)
            var = self.board_vars[kind] = tk.StringVar()
            box = self.board_boxes[kind] = ttk.Combobox(self.board_bar, textvariable=var, state="readonly",
                                                         style="Hop.TCombobox",
                                                         width=9 if sys.platform == "win32" else 20)  # COM7 vs /dev/cu.usbserial-110
            box.pack(side="left", padx=(0, 14))
            box.bind("<<ComboboxSelected>>", lambda e, k=kind: self._board_picked(k))
            tooltip.attach(box, f"Board port: {label}")
        self.button(self.board_bar, "⟳ Scan", self.scan_from_bar, small=True).pack(side="left")
        self.bar_note = tk.Label(self.board_bar, text="", bg=PANEL, fg=DIM, font=("Segoe UI", 9))
        self.bar_note.pack(side="left", padx=10)
        self.scan_labels.append(self.bar_note)
        # The robot at a glance: battery, firmware, controller link, settings storage (refreshed every 20 s)
        self.bot_info = tk.Label(self.board_bar, text="", bg=PANEL, fg=DIM, font=("Segoe UI Semibold", 10))
        self.bot_info.pack(side="right", padx=10)
        self.bot_info_tip = tooltip.Tooltip(self.bot_info, "")
        self._info_polling = False
        root.after(4000, self._poll_bot_info)

        # "A new version is available" bar, shown under the header when the latest release has a newer app
        self.update_bar = tk.Frame(root, bg=PANEL_HI, highlightbackground=EDGE, highlightthickness=1)
        self.update_lbl = tk.Label(self.update_bar, text="", bg=PANEL_HI, fg=LIME, font=("Segoe UI Semibold", 10),
                                   anchor="w")
        self.update_lbl.pack(side="left", padx=10, pady=6, fill="x", expand=True)
        self.update_btns = [self.button(self.update_bar, t, c, small=True) for t, c in
                            (("Later", self.hide_update_bar), ("What's new", self._open_update_page),
                             ("Update now", lambda: self.start_self_update(self.pending_update)))]
        for b in self.update_btns:
            b.pack(side="right", padx=(0, 8), pady=6)
        self.pending_update: self_update.Update | None = None

        # tabs: Flash (the updater) and Guide (controls + voice commands)
        nb = self.nb = ttk.Notebook(root, style="Hop.TNotebook")
        nb.pack(fill="both", expand=True, padx=12, pady=(0, 10))
        flash = tk.Frame(nb, bg=BG)
        nb.add(flash, text="  Flash  ")
        pal = self.pal = dict(BG=BG, PANEL=PANEL, PANEL_HI=PANEL_HI, EDGE=EDGE, GREEN=GREEN, LIME=LIME, TEAL=TEAL,
                              DIM=DIM, TEXT=TEXT, WARN=WARN, BAD=BAD)
        # Everyday tabs first, the technical ones (Diagnostics, Console) last
        self.guide_tab = GuideTab(nb, pal, load_art, self)
        nb.add(self.guide_tab, text="  Guide  ")
        self.controller_tab = ControllerTab(nb, pal, self)
        nb.add(self.controller_tab, text="  Controller  ")
        self.personalize_tab = PersonalizeTab(nb, pal, self)
        nb.add(self.personalize_tab, text="  Personalize  ")
        self.routines_tab = RoutinesTab(nb, pal, self)
        nb.add(self.routines_tab, text="  Routines  ")
        self.ai_tab = AITab(nb, pal, self)
        nb.add(self.ai_tab, text="  AI  ")
        self.diag_tab = DiagnosticsTab(nb, pal, self)
        nb.add(self.diag_tab, text="  Diagnostics  ")
        self.console_tab = ConsoleTab(nb, pal, self)
        nb.add(self.console_tab, text="  Console  ")
        self._tab_order_init()
        nb.bind("<<NotebookTabChanged>>", lambda e: self._header_for_tab(), add="+")
        # Keep the window the size it opened at: the header shrinks on most tabs, and the window shouldn't jump
        root.after(400, self._settle_window_size)

        # devices
        mid = tk.Frame(flash, bg=BG)
        mid.pack(fill="x", padx=18, pady=6)
        mid.columnconfigure(0, weight=1, uniform="slot")
        mid.columnconfigure(1, weight=1, uniform="slot")
        self.slots = {Kind.BOT: DeviceSlot(self, mid, Kind.BOT), Kind.CTRL: DeviceSlot(self, mid, Kind.CTRL)}
        self.slots[Kind.BOT].grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.slots[Kind.CTRL].grid(row=0, column=1, sticky="nsew", padx=(8, 0))

        # action bar
        bar = tk.Frame(flash, bg=BG)
        bar.pack(fill="x", padx=18, pady=(8, 4))
        self.scan_btn = self.button(bar, "⟳  Scan dimensions", self.run_scan)
        self.scan_btn.pack(side="left")
        self.both_btn = self.button(bar, "Update both  ▶▶", lambda: self.run_update(list(self.slots.values())))
        self.both_btn.pack(side="left", padx=8)
        self.backup_var = tk.BooleanVar(value=bool(self.cfg.get("backup_first", True)))
        tk.Checkbutton(bar, text="Back up current firmware first", variable=self.backup_var,
                       command=self._save_backup_pref, bg=BG, fg=TEXT, selectcolor=PANEL_HI,
                       activebackground=BG, activeforeground=GREEN, highlightthickness=0,
                       font=("Segoe UI", 9)).pack(side="left", padx=10)
        # Keep pairing: leave the NVS partition (Bluetooth bonds, settings) alone. Quick: only write the 64 KB
        # pieces that differ from what's on the chip.
        self.keep_var = tk.BooleanVar(value=bool(self.cfg.get("keep_pairing", True)))
        self.quick_var = tk.BooleanVar(value=bool(self.cfg.get("quick_flash", True)))
        for text, var, key in (("Keep pairing & settings", self.keep_var, "keep_pairing"),
                               ("Only write what changed", self.quick_var, "quick_flash")):
            tk.Checkbutton(bar, text=text, variable=var, command=lambda v=var, k=key: self._save_pref(k, v),
                           bg=BG, fg=TEXT, selectcolor=PANEL_HI, activebackground=BG, activeforeground=GREEN,
                           highlightthickness=0, font=("Segoe UI", 9)).pack(side="left", padx=(0, 10))
        self.button(bar, "Backups folder", lambda: core.open_folder(core.BACKUP_DIR), small=True).pack(side="right")

        # log
        logf = tk.Frame(flash, bg=PANEL, highlightbackground=EDGE, highlightthickness=1)
        logf.pack(fill="both", expand=True, padx=18, pady=(4, 16))
        self.log_txt = tk.Text(logf, bg="#040705", fg="#7dffa0", insertbackground=GREEN, relief="flat",
                               font=MONO, height=10, wrap="none", state="disabled")
        sb = ttk.Scrollbar(logf, command=self.log_txt.yview, style="Hop.Vertical.TScrollbar")
        self.log_txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log_txt.pack(fill="both", expand=True, padx=6, pady=6)
        self.log_txt.tag_configure("hi", foreground=LIME)
        self.log_txt.tag_configure("bad", foreground=BAD)
        self.log_txt.tag_configure("warn", foreground=WARN)

        self.log(f"{APP_TITLE} {VERSION} online. Data lives in {core.short_path(core.APP_DIR)}", "hi")
        self.root.after(50, self._pump)
        self.refresh_ports(auto_assign=True)
        for s in self.slots.values():
            s.refresh_release()
        # A quiet scan at startup (listen only, never resets a board), so every tab - the AI tab above all - knows
        # which port is the robot before it talks to it
        root.after(300, lambda: self.run_scan(probe_silent=False, on_done=lambda found: self.ai_tab.startup_scan_done()))
        root.protocol("WM_DELETE_WINDOW", self._close)
        self.parked: set[str] = set()
        if self_update.cleanup_after_update():
            self.log(f"Updated to {APP_TITLE} {VERSION}. Same lab, new tricks.", "hi")
            self.headline.configure(text=f"Updated to {VERSION}. Same lab, new tricks.")
        root.after(1500, lambda: self.check_for_update(manual=False))

    def _settle_window_size(self):
        """Fixes the window at the size it opened at - but never bigger than the screen (the Diagnostics tab alone
        asks for ~1570 px, more than a 13-14" laptop has; a window opened at that size hangs off the edge)."""
        root = self.root
        w, h = root.winfo_width(), root.winfo_height()
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        # leave room for the taskbar, or the Mac's menu bar, title bar and Dock
        fit_w, fit_h = sw - 16, sh - (110 if sys.platform == "darwin" else 80)
        if w <= fit_w and h <= fit_h:
            root.geometry(f"{w}x{h}")
            return
        w, h = min(w, fit_w), min(h, fit_h)
        root.minsize(min(1080, w), min(860, h))
        if sys.platform == "darwin":
            root.wm_state("zoomed")  # fills the screen between the menu bar and the Dock, wherever the Dock is
            return
        root.geometry(f"{w}x{h}+{(sw - w) // 2}+{30 if sys.platform == 'darwin' else 0}")

    # --- widgets ----------------------------------------------------------------------------
    def _style(self):
        st = ttk.Style(self.root)
        st.theme_use("clam")
        st.configure("Hop.Horizontal.TProgressbar", troughcolor="#08100a", background=GREEN,
                     lightcolor=LIME, darkcolor=TEAL, bordercolor=EDGE, thickness=12)
        st.configure("Hop.TCombobox", fieldbackground=PANEL_HI, background=PANEL_HI, foreground=TEXT,
                     arrowcolor=GREEN, bordercolor=EDGE, lightcolor=PANEL_HI, darkcolor=PANEL_HI)
        st.map("Hop.TCombobox", fieldbackground=[("disabled", PANEL), ("readonly", PANEL_HI)],
               foreground=[("disabled", EDGE), ("readonly", TEXT)], arrowcolor=[("disabled", EDGE)],
               selectbackground=[("disabled", PANEL), ("readonly", PANEL_HI)],
               selectforeground=[("disabled", EDGE), ("readonly", GREEN)])
        st.configure("Hop.Vertical.TScrollbar", background=PANEL_HI, troughcolor="#040705", arrowcolor=GREEN,
                     bordercolor=EDGE, lightcolor=PANEL_HI, darkcolor=PANEL_HI, gripcount=0)
        st.map("Hop.Vertical.TScrollbar", background=[("active", "#1f5a2c")])
        st.configure("Hop.TNotebook", background=BG, borderwidth=0, tabmargins=(6, 4, 6, 0))
        st.configure("Hop.TNotebook.Tab", background=PANEL, foreground=DIM, padding=(14, 6),
                     font=("Segoe UI Semibold", 10), borderwidth=0)
        st.map("Hop.TNotebook.Tab", background=[("selected", PANEL_HI)], foreground=[("selected", GREEN)])
        st.configure("Hop.Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT, rowheight=22,
                     borderwidth=0, font=("Segoe UI", 9))
        st.map("Hop.Treeview", background=[("selected", "#1f5a2c")], foreground=[("selected", LIME)])
        st.configure("Hop.Treeview.Heading", background=PANEL_HI, foreground=GREEN, relief="flat",
                     font=("Segoe UI Semibold", 9))
        st.map("Hop.Treeview.Heading", background=[("active", PANEL_HI)])
        self.root.option_add("*TCombobox*Listbox.background", PANEL_HI)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", "#1f5a2c")

    def button(self, master, text, cmd, small=False, big=False):
        b = tk.Button(master, text=text, command=cmd, bg="#123a1d" if not small else PANEL_HI,
                      fg=LIME if not small else TEXT, activebackground=GREEN, activeforeground=BG,
                      relief="flat", bd=0, cursor="hand2", padx=12 if not small else 8, pady=8 if big else 4,
                      font=("Segoe UI Black", 12) if big else ("Segoe UI Semibold", 9 if small else 10),
                      highlightthickness=1, highlightbackground=EDGE)
        b.bind("<Enter>", lambda e: b.configure(bg="#1d5a2c") if str(b["state"]) != "disabled" else None)
        b.bind("<Leave>", lambda e: b.configure(bg="#123a1d" if not small else PANEL_HI))
        tooltip.attach(b, text)
        return b

    def _save_backup_pref(self):
        self.cfg["backup_first"] = bool(self.backup_var.get())
        core.save_config(self.cfg)

    def _save_pref(self, key: str, var: tk.BooleanVar):
        self.cfg[key] = bool(var.get())
        core.save_config(self.cfg)

    # --- thread plumbing --------------------------------------------------------------------
    def ui(self, fn):
        """Schedule fn on the Tk thread (worker threads must never touch widgets directly)."""
        self.q.put(fn)

    def _pump(self):
        try:
            while True:
                self.q.get_nowait()()
        except queue.Empty:
            pass
        self.root.after(50, self._pump)

    def log(self, text: str, tag: str | None = None):
        self.log_txt.configure(state="normal")
        self.log_txt.insert("end", time.strftime("%H:%M:%S  ") + text + "\n", tag or ())
        self.log_txt.see("end")
        self.log_txt.configure(state="disabled")

    def tlog(self, text: str, tag: str | None = None):
        self.ui(lambda: self.log(text, tag))

    def set_busy(self, on: bool):
        self.busy = on
        self.portal.busy(on)
        state = "disabled" if on else "normal"
        for b in (self.scan_btn, self.both_btn):
            b.configure(state=state)
        for s in self.slots.values():
            for b in (s.go_btn, s.probe_btn, s.status_btn, s.wake_btn, s.forget_btn, s.browse_btn):
                b.configure(state=state)

    def job(self, name: str, fn):
        if self.busy:
            return
        self.set_busy(True)

        def run():
            try:
                fn()
            except Exception as e:
                self.tlog(f"{name} crashed: {e}", "bad")
                self.tlog(traceback.format_exc(), "bad")
            finally:
                self.ui(lambda: self.set_busy(False))
        threading.Thread(target=run, daemon=True).start()

    # --- scan / probe -----------------------------------------------------------------------
    # --- tab order: drag a tab sideways to move it; the order is remembered ---------------------------------------
    def _tab_key(self, widget) -> str:
        return self.nb.tab(widget, "text").strip().lower()

    def _tab_order_init(self):
        self.default_tab_order = [self._tab_key(t) for t in self.nb.tabs()]
        self._apply_tab_order(self.cfg.get("tab_order") or [])
        self._dragged_tab = None
        self.nb.bind("<ButtonPress-1>", self._tab_press, add="+")
        self.nb.bind("<B1-Motion>", self._tab_drag, add="+")
        self.nb.bind("<ButtonRelease-1>", self._tab_release, add="+")

    def _apply_tab_order(self, order: list[str]):
        by_key = {self._tab_key(t): t for t in self.nb.tabs()}
        known = [k for k in order if k in by_key] + [k for k in by_key if k not in order]  # new tabs go at the end
        for i, k in enumerate(known):
            self.nb.insert(i, by_key[k])

    def _tab_at(self, x: int, y: int) -> int | None:
        if self.nb.identify(x, y) == "":
            return None
        try:
            return self.nb.index(f"@{x},{y}")
        except tk.TclError:
            return None

    def _tab_press(self, e):
        i = self._tab_at(e.x, e.y)
        self._dragged_tab = self.nb.tabs()[i] if i is not None else None

    def _tab_drag(self, e):
        if self._dragged_tab is None:
            return
        target = self._tab_at(e.x, e.y)
        if target is not None and target != self.nb.index(self._dragged_tab):
            self.nb.insert(target, self._dragged_tab)
            self.nb.configure(cursor="sb_h_double_arrow")

    def _tab_release(self, _e):
        if self._dragged_tab is None:
            return
        self._dragged_tab = None
        self.nb.configure(cursor="")
        order = [self._tab_key(t) for t in self.nb.tabs()]
        if order != self.cfg.get("tab_order"):
            self.cfg["tab_order"] = order
            core.save_config(self.cfg)

    def reset_tab_order(self):
        self._apply_tab_order(self.default_tab_order)
        self.cfg.pop("tab_order", None)
        core.save_config(self.cfg)

    # --- about -----------------------------------------------------------------------------------------------
    # --- problem report (report.py) ------------------------------------------------------------------------------
    def make_report(self):
        if getattr(self, "_reporting", False):
            return
        self._reporting = True
        self.log("Making a problem report…", "hi")
        bot, ctrl = self.board_vars[Kind.BOT].get(), self.board_vars[Kind.CTRL].get()
        held = self.held_ports()
        app_log = self.log_txt.get("1.0", "end")
        cfg = dict(self.cfg)

        def work():
            import report
            try:
                text = report.build(VERSION, bot, ctrl, cfg, held, app_log, lambda s: self.tlog(s))
            except Exception as e:  # noqa: BLE001 - shown to the user
                text, err = "", e
            else:
                err = None
            self.ui(lambda: self._save_report(text, err))
        threading.Thread(target=work, daemon=True).start()

    def _save_report(self, text: str, err):
        self._reporting = False
        if err is not None:
            self.info(f"Couldn't make the report: {err}", "Problem report")
            return
        from tkinter import filedialog
        start = Path.home() / "Desktop"
        path = filedialog.asksaveasfilename(
            title="Save the problem report", defaultextension=".txt", initialdir=str(start if start.is_dir() else Path.home()),
            initialfile=time.strftime("butterbot-report-%Y%m%d-%H%M.txt"), filetypes=[("Text", "*.txt")])
        if not path:
            self.log("Problem report not saved.")
            return
        try:
            Path(path).write_text(text, encoding="utf-8")
        except OSError as e:
            self.info(f"Couldn't save it: {e}", "Problem report")
            return
        self.log(f"Problem report saved: {core.short_path(path)}", "ok")
        core.open_path(path)  # opens it, so it can be read before sharing
        if HopDialog(self.root, self.pal, self.button, "info", "Problem report",
                     "Saved - it's open now, so you can read it before sharing. Names, WiFi details, addresses and "
                     "what was said to the AI are left out.\n\nTo report a problem, attach the file to a new issue "
                     "on GitHub (drag it into the Log box), or to your post.", [],
                     buttons=("Close", "Open a bug report on GitHub"), default=0, cancel=0).run() == 1:
            core.open_path(f"https://github.com/{ROBOT_REPO}/issues/new?template=bug_report.yml")

    def show_about(self):
        message = (f"Version {VERSION}\n\n"
                   "Made by Paul Binder.\n\n"
                   "Part of the ButterBot community fork - firmware for the robot and the controller, and this app "
                   "to update them, set up AI conversations and drive the robot from the PC.\n\n"
                   "Based on the original ButterBot firmware by CircuitMess (MIT License). An unofficial community "
                   "project, not made by or affiliated with CircuitMess.\n\n"
                   "Voice commands use the CMU Pronouncing Dictionary (Copyright 1993-2015 Carnegie Mellon "
                   "University, BSD-style licence - see CMUDICT_LICENSE in the source).\n\n"
                   "Tip: drag a tab left or right to reorder the tabs.")
        details = ["Robot firmware + this app:", "  github.com/" + ROBOT_REPO,
                   "Controller firmware:", "  github.com/" + CTRL_REPO]
        choice = HopDialog(self.root, self.pal, self.button, "info", "Dimension Hopper", message, details,
                           buttons=("Close", "Check for updates", "Open on GitHub", "Reset tab order"), default=0,
                           cancel=0).run()
        if choice == 1:
            self.check_for_update(manual=True)
        elif choice == 2:
            import webbrowser
            webbrowser.open("https://github.com/" + ROBOT_REPO)
        elif choice == 3:
            self.reset_tab_order()

    # --- updating Dimension Hopper itself (see self_update.py) -------------------------------------------
    @staticmethod
    def _my_version() -> str:
        return os.environ.get("DH_PRETEND_VERSION") or VERSION  # the override is a test aid for the updater

    def check_for_update(self, manual: bool):
        """Looks for a newer Dimension Hopper in the latest robot release. Automatic checks (at start-up) are quiet
        and reuse the last answer for most of a day; a manual one (About) always asks GitHub and reports back."""
        def work():
            try:
                u = self_update.cached_check(self.cfg, ROBOT_REPO, force=manual)
                core.save_config(self.cfg)
            except self_update.UpdateError as e:
                if manual:
                    self.ui(lambda: self.error(f"Couldn't check for updates: {e}.", "Update check"))
                else:
                    self.tlog(f"Update check skipped: {e}", "warn")
                return
            mine = self._my_version()
            if u and self_update.newer(u.version, mine):
                self.ui(lambda: self._show_update_bar(u))
                if "--update-now" in sys.argv:  # dev aid: install without asking (tests the swap end to end)
                    self.ui(lambda: self.start_self_update(u, ask=False))
            elif manual:
                self.ui(lambda: self.info(f"You have the latest version ({mine}).", "Up to date"))
        threading.Thread(target=work, daemon=True).start()

    def _show_update_bar(self, u: self_update.Update):
        self.pending_update = u
        self.update_lbl.configure(text=f"Dimension Hopper {u.version} is available (you have {self._my_version()}).",
                                  fg=LIME)
        for b in self.update_btns:
            b.configure(state="normal")
        if not self.update_bar.winfo_ismapped():
            self.update_bar.pack(fill="x", padx=18, pady=(0, 6), before=self.nb)
            self.log(f"Dimension Hopper {u.version} is available - Update now is at the top.", "hi")

    def hide_update_bar(self):
        self.update_bar.pack_forget()

    def _open_update_page(self):
        import webbrowser
        u = self.pending_update
        webbrowser.open(u.page if u and u.page else f"https://github.com/{ROBOT_REPO}/releases/latest")

    def start_self_update(self, u: self_update.Update | None, ask: bool = True):
        if u is None:
            return
        if not self_update.can_self_update():
            if sys.platform == "darwin" and getattr(sys, "frozen", False):
                self.info(f"Download {self_update.MAC_ASSET} from the release page, unzip it and drag the new "
                          "Dimension Hopper over the old one. Your settings and keys stay where they are.", "Update")
            else:
                self.info("This copy runs from the Python sources, so it can't replace itself - pull the new "
                          "version with git, or download Dimension Hopper from the release page.", "Update")
            self._open_update_page()
            return
        if self.busy:
            if not ask:  # --update-now: wait for the start-up scan instead
                self.root.after(1000, lambda: self.start_self_update(u, ask=False))
                return
            self.info("Dimension Hopper is busy (flashing, scanning or reading a board) - update when it's done.",
                      "Busy")
            return
        if ask and not self.confirm(
                f"Update to {u.version}?",
                "Dimension Hopper downloads the new version, closes and opens again (a few seconds). Anything "
                "running on the other tabs stops - an AI chat, the controller link, a diagnostics session. "
                "Your settings and keys stay as they are.", ok="Update & restart", cancel="Not now"):
            return
        self.set_busy(True)
        for b in self.update_btns:
            b.configure(state="disabled")
        self.log(f"Downloading Dimension Hopper {u.version}...", "hi")

        def progress(p: int):
            self.ui(lambda: self.update_lbl.configure(text=f"Downloading Dimension Hopper {u.version}... {p}%"))

        def work():
            try:
                new = self_update.download(u, progress)
            except self_update.UpdateError as e:
                why = str(e)
                self.ui(lambda: self._update_failed(why))
                return
            self.ui(lambda: self._install_update(new, u))
        threading.Thread(target=work, daemon=True).start()

    def _install_update(self, new: Path, u: self_update.Update):
        self.update_lbl.configure(text=f"Restarting into {u.version}...")
        self.root.update_idletasks()
        try:
            self_update.install_and_restart(new)
        except self_update.UpdateError as e:
            self._update_failed(str(e))
            return
        self.log(f"Installed {u.version}, restarting.", "hi")
        for tab in (self.diag_tab, self.console_tab, self.controller_tab, self.ai_tab):
            try:
                tab.shutdown()
            except Exception:  # noqa: BLE001 - quitting anyway
                pass
        self.root.destroy()

    def _update_failed(self, why: str):
        self.set_busy(False)
        if self.pending_update:
            self._show_update_bar(self.pending_update)
        self.log(f"Update failed: {why}", "bad")
        if self.confirm("Update failed", f"{why[:1].upper()}{why[1:]}.\n\nThis version keeps working. You can also "
                        "download the new one from the release page.", ok="Open release page", cancel="Close",
                        kind="error"):
            self._open_update_page()

    # --- header size and the shared board bar ---------------------------------------------------------------
    def _header_for_tab(self):
        """Full header on the Flash tab; on the others one slim line, so the cards get the room."""
        try:
            flash = self.nb.index(self.nb.select()) == self.nb.index(self.slots[Kind.BOT].master.master)
        except (tk.TclError, AttributeError, KeyError):
            return
        compact = not flash
        if compact == self.compact:
            return
        self.compact = compact
        if compact:
            self.portal.pack_forget()
            self.subtitle_lbl.pack_forget()
            self.headline.pack_forget()
            self.title_lbl.configure(font=("Segoe UI Black", 15))
            self.title_lbl.pack_configure(pady=(0, 0))
            self.header.pack_configure(pady=(8, 6))
            self.titles.pack_configure(padx=0)
        else:
            self.portal.pack(side="left", before=self.titles)
            self.title_lbl.configure(font=("Segoe UI Black", 30))
            self.title_lbl.pack_configure(pady=(18, 0))
            self.subtitle_lbl.pack(anchor="w")
            self.headline.pack(anchor="w", pady=(6, 0))
            self.header.pack_configure(pady=(14, 6))
            self.titles.pack_configure(padx=16)

    # --- the robot at a glance (board bar) ------------------------------------------------------------------------
    INFO_EVERY_MS = 20000

    def _poll_bot_info(self, again: bool = True):
        if again:
            self.root.after(self.INFO_EVERY_MS, self._poll_bot_info)
        port = self.board_vars[Kind.BOT].get()
        tabs = (self.personalize_tab, self.routines_tab)
        if (not port or self._info_polling or self.busy or port in self.held_ports()
                or any(getattr(t, "working", False) for t in tabs)):
            if not port:
                self._show_bot_info(None, "")
            return
        self._info_polling = True

        def work():
            r = core.console_command(port, "stat", "BBSTAT", timeout_s=6)
            problem = core.port_problem(r, port) if r is not None else None

            def ui():
                self._info_polling = False
                if self.board_vars[Kind.BOT].get() == port:
                    self._show_bot_info(None if problem or not r else r, "off or busy" if problem else
                                        ("no status (stock firmware?)" if not r else ""))
            self.ui(ui)
        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _storage(field: str) -> tuple[int, int] | None:
        try:
            used, total = (int(x) for x in field.split("/"))
            return (used, total) if total > 0 else None
        except (ValueError, AttributeError):
            return None

    def _show_bot_info(self, f: dict | None, why: str):
        if f is None:
            self.bot_info.configure(text=f"Robot: {why}" if why else "", fg=DIM)
            self.bot_info_tip.set("The robot's battery, firmware, controller link and settings storage show "
                              "here while it's on and plugged in.")
            return
        bits, tip, warn = [], [], False
        try:
            batt = int(f.get("batt", "-1"))
        except ValueError:
            batt = -1
        charge = f.get("charge", "")
        if batt >= 0:
            bits.append(f"Battery {batt}%" + (" (charging)" if charge == "charging" else ""))
            tip.append(f"Battery {batt}% ({charge or 'unknown'})")
            warn |= batt < 20 and charge != "charging"
        link = f.get("link", "")
        if link:
            bits.append("Controller linked" if link == "connected" else "Controller not linked")
            tip.append("Controller " + ("connected" if link == "connected" else "not connected"))
        free = []
        for key, what in (("nvs", "Personalize, WiFi, pairing"), ("user", "routines, commands, schedules")):
            st = self._storage(f.get(key, ""))
            if st:
                pct = 100 - st[0] * 100 // st[1]
                free.append(pct)
                tip.append(f"Storage for {what}: {pct}% free ({(st[1] - st[0]) * 32 // 1024} of "
                           f"{st[1] * 32 // 1024} KB)")
        if free:
            bits.append(f"Storage {min(free)}% free")
            warn |= min(free) < 15
        fw = f.get("fw", "")
        if fw:
            tip.append(f"Firmware {fw}")
        if not free and fw:
            tip.append("Update the robot's firmware to see its storage here.")
        self.bot_info.configure(text="Robot:  " + "   ·   ".join(bits), fg=WARN if warn else GREEN)
        self.bot_info_tip.set("\n".join(tip))

    def _board_picked(self, kind: str):
        device = self.board_vars[kind].get()
        if device:
            self.port_found(kind, device)
        if kind == Kind.BOT:
            self.root.after(300, lambda: self._poll_bot_info(again=False))

    def scan_from_bar(self):
        """The bar's Scan: the open tab's own scan when it has one (it reads its board afterwards)."""
        try:
            tab = self.nb.nametowidget(self.nb.select())
        except (tk.TclError, KeyError):
            tab = None
        if hasattr(tab, "find_robot"):
            tab.find_robot()
        else:
            self.run_scan()

    def _fill_board_bar(self, ports: list[core.PortInfo]):
        devices = [p.device for p in ports]
        for kind, box in self.board_boxes.items():
            box.configure(values=devices)
            if self.board_vars[kind].get() not in devices:
                self.board_vars[kind].set(self.known_ports.get(kind, "") if self.known_ports.get(kind) in devices
                                          else "")

    def refresh_ports(self, auto_assign=False) -> list[core.PortInfo]:
        ports = core.list_ports()
        if hasattr(self, "board_boxes"):
            self._fill_board_bar(ports)
        for s in self.slots.values():
            s.set_ports(ports)
        usb = [p for p in ports if p.is_usb]
        if auto_assign and usb:
            self.log(f"USB serial ports: {', '.join(p.device for p in usb)}. Hit Scan to see what's on them.")
        return ports

    # --- shared port scan: every tab's Scan button lands here -------------------------------------------------
    def scan_button(self, parent, bg: str):
        """A Scan button plus a short note, for any tab with a port dropdown."""
        self.button(parent, "⟳ Scan", self.run_scan, small=True).pack(side="left")
        lbl = tk.Label(parent, text="", bg=bg, fg=DIM, font=("Segoe UI", 8))
        lbl.pack(side="left", padx=(6, 0))
        self.scan_labels.append(lbl)
        return lbl

    def _scan_note(self, text: str, color=DIM):
        for lbl in self.scan_labels:
            lbl.configure(text=text, fg=color)

    def port_found(self, kind: str, device: str):
        """A board was identified on a port: every dropdown for that board switches to it."""
        if kind not in self.slots or not device:
            return
        self.known_ports[kind] = device
        if kind in self.board_vars:
            self.board_vars[kind].set(device)
        self.slots[kind].select_port(device)
        for tab in (self.ai_tab, self.diag_tab, self.personalize_tab, self.routines_tab, self.console_tab,
                    self.controller_tab):
            tab.use_port(kind, device)

    def _refresh_all_ports(self) -> list[core.PortInfo]:
        ports = self.refresh_ports()
        for tab in (self.ai_tab, self.diag_tab, self.personalize_tab, self.routines_tab, self.console_tab,
                    self.controller_tab):
            tab.refresh_ports()
        for kind, device in self.known_ports.items():
            if any(p.device == device for p in ports):
                self.port_found(kind, device)
        return ports

    def run_scan(self, probe_silent: bool = True, on_done=None):
        """Finds which board is on which port and fills every tab's dropdowns. probe_silent: boards that don't print a
        status line (stock firmware, or not running) get the bootloader probe, which resets them; the startup scan
        only listens."""
        if self.busy:
            self._scan_note("Busy - try again when the current job is done.", WARN)
            return
        ports = self._refresh_all_ports()
        # Ports open in the Diagnostics tab already know what's on them
        for link in getattr(self.diag_tab, "links", {}).values():
            if link.session is not None and link.dev in self.slots:
                self.port_found(link.dev, link.port)
        esp = [p for p in ports if p.is_usb and p.device not in self.held_ports()]  # skip ports open in other tabs
        if not esp:
            self.headline.configure(text="No USB serial boards found. Is the robot ON? Hold its power button ~2 s.", fg=WARN)
            self.log("No USB serial devices found. The robot's USB only appears while it's powered on "
                     "(hold the power button ~2 s), and the cable must carry data.", "warn")
            self._scan_note("No boards found - is the robot on?", WARN)
            if on_done:
                on_done({})
            return
        self.headline.configure(text=quip("probe"), fg=DIM)
        self.log(f"{quip('scan')} found {', '.join(p.device for p in esp)}")
        self._scan_note("Scanning (a few seconds)…")
        found_kinds: dict[str, str] = {}

        def finish():
            if found_kinds:
                self._scan_note("  ·  ".join(f"{Kind.LABEL[k]} on {d}" for k, d in sorted(found_kinds.items())), GREEN)
            else:
                self._scan_note("No board answered. Switch the robot on (hold its button ~1 s) and scan again.", WARN)
            if on_done:
                on_done(dict(found_kinds))

        def work():
            # 1. Listen first: fork firmware says who it is every 5 s ([BBSTAT] dev=bot|ctrl). That doesn't reset
            #    anything, so the boards keep running (and stay paired). All ports at once.
            self.tlog(f"Listening on {', '.join(p.device for p in esp)} for status lines (no reset)…")
            found: dict[str, core.DeviceStatus] = {}

            def listen(dev):
                found[dev] = core.read_status(dev, timeout_s=6.5, exclusive=False)
            threads = [threading.Thread(target=listen, args=(p.device,)) for p in esp]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            unknown = []
            for p in esp:
                st = found.get(p.device)
                if st is None or st.error or st.kind not in self.slots:
                    unknown.append(p)
                    continue
                self.tlog(f"{p.device}: {Kind.LABEL[st.kind]} - {st.summary()}", "hi")
                slot = self.slots[st.kind]
                found_kinds[st.kind] = st.port
                self.ui(lambda s=slot, st=st: (self.port_found(st.kind, st.port), s.show_status(st)))

            # 2. Anything silent (stock firmware, or not running) gets the bootloader probe
            if not probe_silent and unknown:
                self.tlog(f"{', '.join(p.device for p in unknown)}: no status line - hit Scan to probe "
                          f"{'it' if len(unknown) == 1 else 'them'}.")
                unknown = []
            for p in unknown:
                self.tlog(f"{p.device} is silent - probing it in bootloader mode…")
                r = core.probe(p.device, self.cfg["baud"])
                if r.error:
                    self.tlog(f"{p.device}: {r.error}", "bad")
                    continue
                self.tlog(f"{p.device}: {r.chip}, {r.flash_bytes // core.MiB} MB flash, MAC {r.mac} "
                          f"-> {Kind.LABEL.get(r.kind or '', 'unknown')}", "hi")
                if r.kind in self.slots:
                    slot = self.slots[r.kind]
                    found_kinds[r.kind] = r.port
                    self.ui(lambda s=slot, r=r: (self.port_found(r.kind, r.port), self._note_probe(r), s.show_probe(r)))
                else:
                    self.ui(lambda r=r: self._note_probe(r))
            self.ui(lambda: self.headline.configure(
                text=("Scan complete. The robot is parked in bootloader mode (it won't pair) until you beam "
                      "or hit Wake." if self.parked else "Scan complete - pick payloads and beam."),
                fg=WARN if self.parked else DIM))
            self.ui(finish)
        self.job("Scan", work)

    # --- themed dialogs (replace tkinter.messagebox) ------------------------------------------------
    def info(self, message: str, title: str = "Heads up"):
        HopDialog(self.root, self.pal, self.button, "info", title, message, cancel=0).run()

    def error(self, message: str, title: str = "Nope"):
        HopDialog(self.root, self.pal, self.button, "error", title, message, cancel=0).run()

    def confirm(self, title: str, message: str, details: list[str] | None = None, ok: str = "OK",
                cancel: str = "Cancel", kind: str = "question") -> bool:
        return HopDialog(self.root, self.pal, self.button, kind, title, message, details, (ok, cancel),
                         default=0, cancel=1).run() == 0

    def held_ports(self) -> set[str]:
        """Ports kept open by the Diagnostics or Console tab."""
        return {p for p in (*self.monitor_ports, self.console_tab.port_open()) if p}

    def port_in_use(self, port: str) -> bool:
        if port and port in self.monitor_ports:
            self.info(f"{port} is open in the Diagnostics tab. Disconnect it there first.", "Port busy")
            return True
        if port and port == self.console_tab.port_open():
            self.info(f"{port} is open in the Console tab. Disconnect it there first.", "Port busy")
            return True
        return False

    def run_forget(self, slot: DeviceSlot):
        """Drops the board's Bluetooth bonds (fork firmware 'forget' command) so the pair can bond again from scratch."""
        port = slot.port()
        if not port:
            self.info("Pick a port first (or hit Scan dimensions).", "Which board?")
            return
        if self.port_in_use(port):
            return
        if port in self.parked:
            self.info(f"{port} is parked in bootloader mode, so its firmware can't take commands. Hit Wake first.",
                      "Board is parked")
            return
        if not self.confirm("Forget pairing?",
                            f"The {Kind.LABEL[slot.kind]} on {port} will forget every robot/controller it has "
                            "paired with. Do it on BOTH boards, then switch both off and on so they pair fresh.",
                            ok="Forget", kind="warning"):
            return

        def work():
            r = core.console_command(port, "forget", "BBFORGET")
            if problem := core.port_problem(r, port):
                self.tlog(problem, "warn")
                return
            if r is None:
                self.tlog(f"{port}: no answer. Forget needs fork firmware with console commands "
                          "(this release or newer) and the board switched on.", "warn")
                return
            self.tlog(f"{port}: forgot {r.get('removed', '?')} pairing(s). Now do the other board, then power-cycle "
                      "both.", "hi")
        self.job("Forget", work)

    def run_status(self, slot: DeviceSlot):
        """Reads the board's [BBSTAT] line without resetting it: battery, charging, link, firmware."""
        port = slot.port()
        if not port:
            self.info("Pick a port first (or hit Scan dimensions).", "Which board?")
            return
        if self.port_in_use(port):
            return
        if port in self.parked:
            self.info(f"{port} is parked in bootloader mode, so its firmware isn't running and there's no status "
                      "to read. Hit Wake (or flash it) first.", "Board is parked")
            return
        slot.chip_lbl.configure(text=f"Listening on {port} for a status line…", fg=DIM)

        def work():
            st = core.read_status(port)
            ok = not st.error
            text = st.summary() + ("  ·  " + st.health()[0] if ok and st.health()[0] else "")
            self.tlog(f"{port}: {text}", "hi" if ok else "bad")
            self.ui(lambda: slot.show_status(st))
        self.job("Status", work)

    def run_probe(self, slot: DeviceSlot):
        port = slot.port()
        if not port:
            self.info("Pick a port first (or hit Scan dimensions).", "Which board?")
            return
        if self.port_in_use(port):
            return

        def work():
            r = core.probe(port, self.cfg["baud"], on_line=lambda s: None)
            self.tlog(f"{port}: " + (r.error or f"{r.chip}, {r.flash_bytes // core.MiB} MB flash"),
                      "bad" if r.error else "hi")
            self.ui(lambda: (self._note_probe(r), slot.show_probe(r)))
        self.job("Probe", work)

    # --- parked boards ----------------------------------------------------------------------
    # A probe or backup leaves the robot in the ROM bootloader so it doesn't power itself off. While
    # parked it runs no firmware: no face, no BLE, so the controller can't pair with it. Track those
    # ports so the user can see it, wake them, and so closing the app never leaves one stranded.
    def _set_parked(self, port: str, on: bool):
        (self.parked.add if on else self.parked.discard)(port)
        for s in self.slots.values():
            s.show_parked(s.port() in self.parked)

    def _note_probe(self, r: core.ProbeResult):
        if not r.error:
            self._set_parked(r.port, r.parked)
            if r.parked:
                self.log(f"{r.port} is parked in bootloader mode - it won't run (or pair) until you flash it "
                         "or hit Wake.", "warn")

    def run_wake(self, slot: DeviceSlot):
        port = slot.port()
        if not port:
            self.info("Pick a port first (or hit Scan dimensions).", "Which board?")
            return
        if self.port_in_use(port):
            return
        bot = slot.kind == Kind.BOT

        def work():
            try:
                core.wake(port)
            except core.EsptoolError as e:
                self.tlog(f"{port}: couldn't wake it ({e}). If it's already running, there's nothing to do; "
                          "otherwise unplug it and power-cycle it.", "warn")
                self.ui(lambda: self._set_parked(port, False))
                return
            self.tlog(f"{port}: rebooted into its firmware." +
                      (" The robot powers itself off when it boots like this - press its power button "
                       "to turn it on." if bot else ""), "hi")
            self.ui(lambda: self._set_parked(port, False))
        self.job("Wake", work)

    def _wake_all_parked(self):
        for port in sorted(self.parked):
            try:
                core.wake(port)
            except core.EsptoolError:
                pass
        self.parked.clear()

    # --- update -----------------------------------------------------------------------------
    def run_update(self, slots: list[DeviceSlot]):
        chosen = [s for s in slots if s.port()]
        if not chosen:
            self.info("No port picked. Hit 'Scan dimensions' first.", "Which board?")
            return
        if any(self.port_in_use(s.port()) for s in chosen):
            return
        if len({s.port() for s in chosen}) != len(chosen):
            self.error("Both slots point at the same port. That's how you get paradoxes.", "Paradox detected")
            return
        details = []
        for sl in chosen:
            details.append(f"{Kind.LABEL[sl.kind]} on {sl.port()}")
            details += ["   " + line.strip() for line in sl.payload_summary().split("\n")]
        if not self.confirm("Ready to beam?", "About to flash. Don't unplug anything until the portal calms down.",
                            details, ok="BEAM IT  ▶"):
            return
        backup_first = bool(self.backup_var.get())
        opts = dict(keep=bool(self.keep_var.get()), quick=bool(self.quick_var.get()))

        def work():
            # Two boards: flash both at the same time (each in its own worker process, on its own port).
            # One board failing doesn't stop the other.
            results: dict[str, bool] = {}
            if len(chosen) > 1:
                self.tlog("Updating both at the same time.", "hi")

                def one(s):
                    try:
                        results[s.kind] = self._update_one(s, backup_first, parallel=True, **opts)
                    except Exception as e:  # noqa: BLE001
                        self.tlog(f"[{Kind.LABEL[s.kind]}] {e}", "bad")
                        results[s.kind] = False
                threads = [threading.Thread(target=one, args=(s,), daemon=True) for s in chosen]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                failed = [Kind.LABEL[k] for k, good in results.items() if not good]
                if failed:
                    self.tlog(f"Done - {' and '.join(failed)} didn't finish; see {'its' if len(failed) == 1 else 'their'} "
                              "panel.", "bad")
                else:
                    self.tlog("Both updated ✔", "hi")
            else:
                for s in chosen:
                    results[s.kind] = self._update_one(s, backup_first, **opts)
            ok = sum(results.values())
            done = ok == len(chosen)
            self.ui(lambda: (self.portal.flash(LIME if done else BAD),
                             self.headline.configure(text=quip("done") if done else quip("fail"),
                                                     fg=GREEN if done else BAD)))
        self.job("Update", work)

    def _update_one(self, s: DeviceSlot, backup_first: bool, keep: bool = True, quick: bool = True,
                    parallel: bool = False) -> bool:
        name, port, baud = Kind.LABEL[s.kind], s.port(), self.cfg["baud"]

        def flash_op(fn: str, **kw):
            """Backups and writes: in a worker process when the other board is being flashed at the same time."""
            if parallel:
                return core.run_in_worker(fn, kw, kw.pop("on_line"), kw.pop("on_progress"))
            return getattr(core, fn)(**kw)

        def status(text, color=DIM, pct=None):
            self.ui(lambda: s.set_status(text, color, pct))

        def prog(base, span):
            last = [-1]

            def f(p):
                v = base + p * span // 100
                if v != last[0]:
                    last[0] = v
                    self.ui(lambda: s.bar.configure(value=v))
            return f

        try:
            # 0. save what the owner set up on it, while it still runs its firmware (the probe below stops it)
            pre = self._settings_before_flash(s, port, status)

            # 1. identify the board (always re-probe: ports get renumbered, cables get swapped)
            status(quip("probe"), pct=0)
            r = core.probe(port, baud)
            self.ui(lambda: (self._note_probe(r), s.show_probe(r)))
            if r.error:
                raise core.EsptoolError(f"Can't talk to {port}: {r.error}")
            if r.kind != s.kind:
                raise core.ImageError(f"{port} has {r.flash_bytes // core.MiB} MB flash - that's a "
                                      f"{Kind.LABEL.get(r.kind or '', 'mystery board')}, not the {name}.")

            # 2. resolve the payload
            mode = s.src_var.get()
            if mode == "release":
                rel = s.release or core.latest_release(s.repo_var.get())
                status(quip("download"), pct=0)
                self.tlog(f"[{name}] {rel.repo} {rel.tag}: {rel.asset_name}")
                path = core.download(rel, prog(0, 15))
                plan = core.classify_full_image(path)
                plan.title = f"{rel.repo} {rel.tag}"
            else:
                plan = s.make_local_plan()
            core.check_plan_fits(plan, s.kind, r.flash_bytes)
            self.tlog(f"[{name}] payload: {plan.title}", "hi")

            # 3. backup
            if backup_first:
                status(quip("backup"))
                self.tlog(f"[{name}] backing up {r.flash_bytes // core.MiB} MB…")
                self.ui(lambda: self._set_parked(port, True))  # backup leaves it in the bootloader
                bk = flash_op("backup", port=port, flash_bytes=r.flash_bytes, target=s.kind, baud=baud,
                              on_line=lambda l: None, on_progress=prog(15, 35))
                self.tlog(f"[{name}] backup saved: {bk}", "hi")

            # 4. flash + verify
            status(quip("flash"))
            show = lambda l: self.tlog(f"[{name}] {l}") if ("Hash of data" in l or "Wrote" in l) else None  # noqa: E731
            bar = prog(50 if backup_first else 15, 50 if backup_first else 85)
            if keep or quick:
                st = flash_op("smart_write", port=port, plan=plan, baud=baud, flash_bytes=r.flash_bytes,
                              keep_pairing=keep, on_line=show, on_progress=bar, compare=quick)
                saved = "" if not quick else (
                    f" Wrote {st.written / core.MiB:.1f} of {st.total / core.MiB:.1f} MB - the rest was already there."
                    if st.written < st.total else "")
                self.tlog(f"[{name}] written and verified.{saved}"
                          + (" Pairing and settings kept." if st.kept_nvs else "")
                          + (f" Note: {st.note}." if st.note else ""), "hi")
            else:
                flash_op("write", port=port, plan=plan, baud=baud, on_line=show, on_progress=bar)
            self.ui(lambda: self._set_parked(port, False))  # the write ends with a hard reset

            # 5. wait for it to run the new firmware, and check it's the version we wrote
            stock = s.src_var.get() == "release" and s._is_stock()
            if stock:  # stock firmware prints no status line, so there's nothing to confirm with
                status("Landed and verified. " + ("Press the power button to turn it on." if s.kind == Kind.BOT
                                                  else "It's rebooting."), GREEN, 100)
                self.tlog(f"[{name}] done ✔", "hi")
                return True
            bot = s.kind == Kind.BOT

            def waiting(state):
                if bot and state == "off":
                    status("Flashed ✔  Now turn the robot on: hold its power button ~1 s.", WARN, 100)
                elif state == "on":
                    status("Waiting for it to start up…", DIM, 100)
            if bot:
                self.ui(lambda: self.headline.configure(
                    text="Robot flashed. Hold its power button ~1 s to turn it on - I'll check it booted.", fg=WARN))
            st = core.wait_for_boot(port, plan.app.version if plan.app else "", 300 if bot else 40, on_wait=waiting)
            if st.error:
                status("Flashed and verified, but it didn't report back. " +
                       ("Turn it on (hold the button ~1 s) and hit Status." if bot else "Hit Status to check."),
                       WARN, 100)
                self.tlog(f"[{name}] flashed, but no status line within the wait ({st.error}).", "warn")
                return True
            self.ui(lambda: s.show_status(st))
            want = plan.app.version if plan.app else ""
            if want and not core.version_matches(st.firmware, want):
                status(f"⚠ It booted, but reports {st.firmware} instead of {want}.", WARN, 100)
                self.tlog(f"[{name}] booted {st.firmware}, expected {want}", "warn")
                return False
            status(f"Running the new firmware ({st.firmware}) ✔", GREEN, 100)
            self.tlog(f"[{name}] done ✔ - booted {st.firmware}", "hi")
            if pre:
                self._check_settings_after_flash(s, port, *pre)
            return True
        except (core.EsptoolError, core.ImageError, OSError, ValueError, RuntimeError) as e:
            status(f"✖ {e}", BAD)
            self.tlog(f"[{name}] {e}", "bad")
            return False

    # --- settings kept across a flash (backup.py) ---------------------------------------------------------------
    def _settings_before_flash(self, s: DeviceSlot, port: str, status) -> tuple[dict, Path] | None:
        """Backs up the board's settings (names, lines, routines... or the controller's theme) before flashing, if
        it runs firmware that has them. Returns (backup, file), or None if there was nothing to save."""
        bot = s.kind == Kind.BOT
        status("Saving your settings first…", pct=0)
        try:
            data, _ = backup_mod.collect(port if bot else None, None if bot else port, self.cfg, VERSION)
        except Exception as e:  # never stop a flash over this
            self.tlog(f"[{Kind.LABEL[s.kind]}] couldn't save its settings first: {e}", "warn")
            return None
        if not backup_mod.has_settings(data, s.kind):
            return None
        path = backup_mod.auto_save(data, s.kind)
        self.tlog(f"[{Kind.LABEL[s.kind]}] settings saved first: {core.short_path(path)}", "hi")
        return data, path

    def _check_settings_after_flash(self, s: DeviceSlot, port: str, before: dict, path: Path):
        """After a flash: if the board came back without the settings it had, offer to put them back."""
        bot = s.kind == Kind.BOT
        try:
            after, _ = backup_mod.collect(port if bot else None, None if bot else port, self.cfg, VERSION)
        except Exception:
            return
        if backup_mod.has_settings(after, s.kind):
            self.tlog(f"[{Kind.LABEL[s.kind]}] your settings are still there ✔", "hi")
            return
        self.tlog(f"[{Kind.LABEL[s.kind]}] came back without your settings - offering to restore them.", "warn")
        self.ui(lambda: self._offer_restore(s, before, path))

    def _offer_restore(self, s: DeviceSlot, data: dict, path: Path):
        if self.busy:  # the update job is still finishing
            self.root.after(500, lambda: self._offer_restore(s, data, path))
            return
        name = Kind.LABEL[s.kind]
        if not self.confirm(f"Put your {name} settings back?",
                            f"The {name} came back from the flash without the settings it had. Dimension Hopper "
                            "saved them just before flashing - restore them now?",
                            details=backup_mod.summary(data) + [f"Saved in {core.short_path(path)}"], ok="Restore"):
            return
        bot = s.kind == Kind.BOT
        port = s.port()
        data = dict(data, pc={})  # the board's settings only: this PC's AI settings never left

        def work():
            try:
                result = backup_mod.restore(data, port if bot else None, None if bot else port, self.cfg)
            except ValueError as e:
                result = [str(e)]
            bad = any(r.startswith(("Didn't", "Nothing")) for r in result)
            self.tlog(f"[{name}] restore: " + " ".join(result), "bad" if bad else "hi")
            self.ui(lambda: ((self.error if bad else self.info)("\n".join(result), "Restore"),
                             self.personalize_tab.after(300, self.personalize_tab._read_boards),
                             self.routines_tab.after(600, self.routines_tab.read_all)))
        self.job("Restore", work)

    # --- repo options ---------------------------------------------------------------------
    def open_repo_dialog(self):
        if getattr(self, "_repo_dlg", None) and self._repo_dlg.winfo_exists():
            self._repo_dlg.lift()
            return
        dlg = self._repo_dlg = tk.Toplevel(self.root, bg=BG)
        dlg.title("Release repositories")
        dlg.transient(self.root)
        dlg.resizable(False, False)
        tk.Label(dlg, text="RELEASE REPOSITORIES", bg=BG, fg=GREEN, font=("Segoe UI Black", 14)).pack(
            anchor="w", padx=16, pady=(14, 0))
        tk.Label(dlg, text="Built-in repos are always in the dropdown. Add any other GitHub repo that publishes "
                           "firmware .bin releases (owner/name or a github.com URL).",
                 bg=BG, fg=DIM, wraplength=560, justify="left", font=("Segoe UI", 9)).pack(anchor="w", padx=16, pady=(2, 8))

        def section(kind: str):
            box = tk.Frame(dlg, bg=PANEL, highlightbackground=EDGE, highlightthickness=1)
            box.pack(fill="x", padx=16, pady=6)
            tk.Label(box, text=Kind.LABEL[kind].upper(), bg=PANEL, fg=LIME, font=("Segoe UI Black", 11)).pack(
                anchor="w", padx=10, pady=(8, 2))
            lb = tk.Listbox(box, height=5, bg="#040705", fg=TEXT, selectbackground="#1f5a2c", relief="flat",
                            highlightthickness=1, highlightbackground=EDGE, font=("Segoe UI", 9), activestyle="none")
            lb.pack(fill="x", padx=10)
            presets = core.PRESET_REPOS[kind]

            def refill():
                lb.delete(0, "end")
                for r in presets:
                    lb.insert("end", f"{r}   (built-in)")
                    lb.itemconfigure("end", fg=DIM)
                for r in self.cfg["custom_repos"][kind]:
                    lb.insert("end", r)

            row = tk.Frame(box, bg=PANEL)
            row.pack(fill="x", padx=10, pady=8)
            var = tk.StringVar()
            ent = tk.Entry(row, textvariable=var, bg=PANEL_HI, fg=TEXT, insertbackground=GREEN, relief="flat",
                           highlightthickness=1, highlightbackground=EDGE, highlightcolor=GREEN, width=44)
            ent.pack(side="left", fill="x", expand=True, ipady=3)
            msg = tk.Label(box, text="", bg=PANEL, fg=DIM, anchor="w", font=("Segoe UI", 8))

            def add():
                try:
                    repo = core.normalize_repo(var.get())
                except ValueError as e:
                    msg.configure(text=str(e), fg=BAD)
                    return
                if repo.lower() in (r.lower() for r in core.repo_choices(self.cfg, kind)):
                    msg.configure(text=f"{repo} is already in the list.", fg=WARN)
                    return
                self.cfg["custom_repos"][kind].append(repo)
                core.save_config(self.cfg)
                var.set("")
                msg.configure(text=f"Added {repo} - pick it from the {Kind.LABEL[kind]} dropdown.", fg=GREEN)
                refill()
                self.slots[kind].reload_repo_choices()

            def remove():
                sel = lb.curselection()
                if not sel:
                    return
                i = sel[0] - len(presets)
                if i < 0:
                    msg.configure(text="Built-in repos can't be removed.", fg=WARN)
                    return
                repo = self.cfg["custom_repos"][kind].pop(i)
                core.save_config(self.cfg)
                msg.configure(text=f"Removed {repo}.", fg=DIM)
                refill()
                slot = self.slots[kind]
                was = slot.repo_var.get()
                slot.reload_repo_choices()
                if was != slot.repo_var.get() and slot.src_var.get() == "release":
                    slot.refresh_release()

            ent.bind("<Return>", lambda e: add())
            self.button(row, "Add", add, small=True).pack(side="left", padx=(6, 0))
            self.button(row, "Remove selected", remove, small=True).pack(side="left", padx=(6, 0))
            msg.pack(fill="x", padx=10, pady=(0, 8))
            refill()

        section(Kind.BOT)
        section(Kind.CTRL)
        self.button(dlg, "Done", dlg.destroy).pack(anchor="e", padx=16, pady=(6, 14))
        dlg.grab_set()

    def _close(self):
        self.diag_tab.shutdown()
        self.console_tab.shutdown()
        self.controller_tab.shutdown()
        self.ai_tab.shutdown()
        if self.busy and not self.confirm(
                "Flash in progress", "Quitting now can leave the board half-written. It stays recoverable over USB, "
                "but you'd have to flash it again.", ok="Quit anyway", cancel="Keep flashing", kind="warning"):
            return
        if self.parked and not self.busy:
            ports = ", ".join(sorted(self.parked))
            if self.confirm("Wake parked boards?",
                            f"{ports} is still parked in bootloader mode: no firmware running, so no driving and "
                            "no pairing. Reboot it now? (The robot then powers itself off - press its power "
                            "button to turn it on.)", ok="Wake & quit", cancel="Just quit"):
                self._wake_all_parked()
        self.root.destroy()


def _dpi_aware():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass


def tooltip_gaps() -> list[str]:
    """Button labels in the source with no tooltip (empty in the packaged exe, which has no .py sources)."""
    import re
    labels = set()
    for f in ("dimension_hopper.py", "ai_tab.py", "diag_tab.py", "console_tab.py", "personalize_tab.py",
              "controller_tab.py", "routines_tab.py"):
        p = Path(__file__).with_name(f)
        if p.exists():
            labels |= set(re.findall(r'button\([^,]+, *"([^"]+)"', p.read_text(encoding="utf-8")))
    return sorted(l for l in labels if l not in tooltip.TIPS)


def _selftest(out: str):
    """Dev aid for the frozen exe (which has no console): checks the bundled esptool and writes a report."""
    import esptool
    lines = [f"version {VERSION}", f"esptool {esptool.__version__}"]
    stub_dir = Path(esptool.__file__).parent / "targets" / "stub_flasher"
    stubs = sorted(p.name for p in stub_dir.rglob("*esp32s3*")) if stub_dir.exists() else []
    lines.append(f"esp32s3 stubs: {stubs or 'MISSING'}")
    lines.append(f"ports: {[p.device for p in core.list_ports()]}")
    lines.append(f"fork builds: bot={core.find_fork_build('bot')} ctrl={core.find_fork_build('ctrl')}")
    lines.append(f"ctrl repos: {core.repo_choices(core.load_config(), 'ctrl')}")
    from guide_tab import GuideTab, _asset
    lines.append(f"guide: {len(GuideTab._load_data()['entries'])} voice actions; images "
                 f"{[n for n in ('bot_guide.png', 'controller_guide.png') if _asset(n).exists()]}")
    lines.append(f"probe COM250: {core.probe('COM250').error[:60]}")
    cfg = core.load_config()
    ok = core.save_config(cfg)
    lines.append(f"settings: {core.CONFIG_PATH} keys={sorted(k for k in cfg if k.startswith('ai_') and 'pass' not in k)} "
                 f"save={'ok' if ok else core.LAST_SAVE_ERROR}")
    lines.append(f"tooltips: {len(tooltip.TIPS)} entries, missing for {tooltip_gaps() or 'none'}")
    try:  # AI tab dependencies are imported lazily, so make sure the exe really bundles them
        import faster_whisper, ctranslate2, anthropic, keyring, brain  # noqa: E401
        lines.append(f"ai: faster_whisper {faster_whisper.__version__}, ctranslate2 {ctranslate2.__version__}, "
                     f"anthropic {anthropic.__version__}, keyring backend {type(keyring.get_keyring()).__name__}, "
                     f"{len(brain.load_actions(Path(getattr(sys, '_MEIPASS', Path(__file__).parent)) / 'guide_data.json'))} actions")
    except Exception as e:  # noqa: BLE001
        lines.append(f"ai: MISSING {e!r}")
    import phonemes
    lines.append(f"phonemes: {phonemes.to_phonemes('party time')}")
    import controller_link
    lines.append(f"bluetooth: {controller_link.bluetooth_available()}")
    lines.append(f"self-update: can={self_update.can_self_update()} exe={self_update.exe_path().name} "
                 f"writable={self_update.writable(self_update.exe_path().parent)}")
    import screen_render
    lines.append(f"controller screen: {len(screen_render.GLYPHS)} glyphs, {len(screen_render.DATA['phrases'])} text lists")
    try:  # Update both runs flashes in worker processes (this exe again, with --flash-worker)
        core.run_in_worker("backup", dict(port="COM250", flash_bytes=4 * core.MiB, target="ctrl", baud=921600))
    except Exception as e:  # noqa: BLE001 - an error about COM250 means the worker ran
        lines.append(f"flash worker: {type(e).__name__} {str(e)[:50]}")
    Path(out).write_text("\n".join(lines), encoding="utf-8")


def main():
    if "--flash-worker" in sys.argv:  # a flash running in its own process (Update both flashes both at once)
        core.worker_main(sys.argv[sys.argv.index("--flash-worker") + 1])
        return
    if "--selftest" in sys.argv:
        _selftest(sys.argv[sys.argv.index("--selftest") + 1])
        return
    _dpi_aware()
    mac_compat.install()  # before the first widget: fonts, button colours (no-op on Windows)
    root = tk.Tk()
    assets = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    if sys.platform == "win32":
        try:
            root.iconbitmap(str(assets / "portal.ico"))
        except tk.TclError:
            pass
    app = App(root)
    mac_compat.setup_root(root, on_quit=app._close, icon_png=assets / "portal.png")

    if "--screenshot" in sys.argv:  # dev aid: render, grab the window, exit
        out = sys.argv[sys.argv.index("--screenshot") + 1]

        def grab():
            from PIL import ImageGrab
            root.update()
            x, y = root.winfo_rootx(), root.winfo_rooty()
            ImageGrab.grab(bbox=(x, y, x + root.winfo_width(), y + root.winfo_height())).save(out)
            root.destroy()
        root.after(int(float(sys.argv[sys.argv.index("--screenshot") + 2]) * 1000)
                   if len(sys.argv) > sys.argv.index("--screenshot") + 2 else 3000, grab)
    root.mainloop()


if __name__ == "__main__":
    main()
