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
from guide_tab import GuideTab
from diag_tab import DiagnosticsTab
from console_tab import ConsoleTab
from ai_tab import AITab
from personalize_tab import PersonalizeTab
from hop_dialog import HopDialog
import tooltip
from hopper_core import Kind

APP_TITLE = "Dimension Hopper"
VERSION = "1.2.4"

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
MONO = ("Cascadia Mono", 9) if sys.platform == "win32" else ("Courier", 10)

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
        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self.monitor_ports: set[str] = set()  # ports held open by the Diagnostics tab (robot and controller)

        root.title(f"{APP_TITLE} {VERSION}")
        root.configure(bg=BG)
        root.minsize(1080, 860)
        self._style()

        # header
        top = tk.Frame(root, bg=BG)
        top.pack(fill="x", padx=18, pady=(14, 6))
        self.portal = Portal(top, 150)
        self.portal.pack(side="left")
        titles = tk.Frame(top, bg=BG)
        titles.pack(side="left", padx=16, fill="y")
        tk.Label(titles, text="DIMENSION HOPPER", bg=BG, fg=GREEN, font=("Segoe UI Black", 30)).pack(anchor="w", pady=(18, 0))
        tk.Label(titles, text="interdimensional firmware delivery  ·  ButterBot + Controller", bg=BG, fg=LIME,
                 font=("Segoe UI Semibold", 11)).pack(anchor="w")
        self.headline = tk.Label(titles, text="Plug a board in over USB-C. I'll do the science.", bg=BG, fg=DIM,
                                 font=("Segoe UI", 10, "italic"))
        self.headline.pack(anchor="w", pady=(6, 0))

        # tabs: Flash (the updater) and Guide (controls + voice commands)
        nb = ttk.Notebook(root, style="Hop.TNotebook")
        nb.pack(fill="both", expand=True, padx=12, pady=(0, 10))
        flash = tk.Frame(nb, bg=BG)
        nb.add(flash, text="  Flash  ")
        pal = self.pal = dict(BG=BG, PANEL=PANEL, PANEL_HI=PANEL_HI, EDGE=EDGE, GREEN=GREEN, LIME=LIME, TEAL=TEAL,
                              DIM=DIM, TEXT=TEXT, WARN=WARN, BAD=BAD)
        nb.add(GuideTab(nb, pal, load_art), text="  Guide  ")
        self.personalize_tab = PersonalizeTab(nb, pal, self)
        nb.add(self.personalize_tab, text="  Personalize  ")
        self.diag_tab = DiagnosticsTab(nb, pal, self)
        nb.add(self.diag_tab, text="  Diagnostics  ")
        self.console_tab = ConsoleTab(nb, pal, self)
        nb.add(self.console_tab, text="  Console  ")
        self.ai_tab = AITab(nb, pal, self)
        nb.add(self.ai_tab, text="  AI  ")

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

        self.log(f"{APP_TITLE} {VERSION} online. Data lives in {core.APP_DIR}", "hi")
        self.root.after(50, self._pump)
        self.refresh_ports(auto_assign=True)
        for s in self.slots.values():
            s.refresh_release()
        root.protocol("WM_DELETE_WINDOW", self._close)
        self.parked: set[str] = set()

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
    def refresh_ports(self, auto_assign=False) -> list[core.PortInfo]:
        ports = core.list_ports()
        for s in self.slots.values():
            s.set_ports(ports)
        usb = [p for p in ports if p.is_usb]
        if auto_assign and usb:
            self.log(f"USB serial ports: {', '.join(p.device for p in usb)}. Hit Scan to see what's on them.")
        return ports

    def run_scan(self):
        ports = self.refresh_ports()
        esp = [p for p in ports if p.is_usb and p.device not in self.held_ports()]  # skip ports open in other tabs
        if not esp:
            self.headline.configure(text="No USB serial boards found. Is the robot ON? Hold its power button ~2 s.", fg=WARN)
            self.log("No USB serial devices found. The robot's USB only appears while it's powered on "
                     "(hold the power button ~2 s), and the cable must carry data.", "warn")
            return
        self.headline.configure(text=quip("probe"), fg=DIM)
        self.log(f"{quip('scan')} found {', '.join(p.device for p in esp)}")

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
                self.ui(lambda s=slot, st=st: (s.select_port(st.port), s.show_status(st)))

            # 2. Anything silent (stock firmware, or not running) gets the bootloader probe
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
                    self.ui(lambda s=slot, r=r: (s.select_port(r.port), self._note_probe(r), s.show_probe(r)))
                else:
                    self.ui(lambda r=r: self._note_probe(r))
            self.ui(lambda: self.headline.configure(
                text=("Scan complete. The robot is parked in bootloader mode (it won't pair) until you beam "
                      "or hit Wake." if self.parked else "Scan complete - pick payloads and beam."),
                fg=WARN if self.parked else DIM))
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
            ok = 0
            for s in chosen:
                if self._update_one(s, backup_first, **opts):
                    ok += 1
            done = ok == len(chosen)
            self.ui(lambda: (self.portal.flash(LIME if done else BAD),
                             self.headline.configure(text=quip("done") if done else quip("fail"),
                                                     fg=GREEN if done else BAD)))
        self.job("Update", work)

    def _update_one(self, s: DeviceSlot, backup_first: bool, keep: bool = True, quick: bool = True) -> bool:
        name, port, baud = Kind.LABEL[s.kind], s.port(), self.cfg["baud"]

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
                bk = core.backup(port, r.flash_bytes, s.kind, baud, lambda l: None, prog(15, 35))
                self.tlog(f"[{name}] backup saved: {bk}", "hi")

            # 4. flash + verify
            status(quip("flash"))
            show = lambda l: self.tlog(f"[{name}] {l}") if ("Hash of data" in l or "Wrote" in l) else None  # noqa: E731
            bar = prog(50 if backup_first else 15, 50 if backup_first else 85)
            if keep or quick:
                st = core.smart_write(port, plan, baud, r.flash_bytes, keep, show, bar, compare=quick)
                saved = "" if not quick else (
                    f" Wrote {st.written / core.MiB:.1f} of {st.total / core.MiB:.1f} MB - the rest was already there."
                    if st.written < st.total else "")
                self.tlog(f"[{name}] written and verified.{saved}"
                          + (" Pairing and settings kept." if st.kept_nvs else "")
                          + (f" Note: {st.note}." if st.note else ""), "hi")
            else:
                core.write(port, plan, baud, show, bar)
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
            return True
        except (core.EsptoolError, core.ImageError, OSError, ValueError, RuntimeError) as e:
            status(f"✖ {e}", BAD)
            self.tlog(f"[{name}] {e}", "bad")
            return False

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
    for f in ("dimension_hopper.py", "ai_tab.py", "diag_tab.py", "console_tab.py", "personalize_tab.py"):
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
    Path(out).write_text("\n".join(lines), encoding="utf-8")


def main():
    if "--selftest" in sys.argv:
        _selftest(sys.argv[sys.argv.index("--selftest") + 1])
        return
    _dpi_aware()
    root = tk.Tk()
    try:
        icon = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "portal.ico"
        if icon.exists():
            root.iconbitmap(str(icon))
    except tk.TclError:
        pass
    App(root)

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
