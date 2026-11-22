"""
Dimension Hopper - Watch tab: the Clockstar watch, over USB (clockstar_link.py; needs watch firmware with the
ButterBot app).

  Clock   set the watch to this PC's time - by hand, or by itself whenever a scan finds the watch; 12 or 24 hours
  Robot   keep the watch linked to ButterBot: the robot's battery on the watch face, its answers and alarms as
          notifications
  Apps    the watch menu: show / hide apps and change their order (Settings always stays)
  Themes  put a theme on the watch and switch to it, go back to the watch's own look, or import a theme made of
          your own PNGs (bg.png, bg_bot.png, 0-9.png, colon.png - any of them)
  Everyday (its own page): steps (today, the last 7 days, goal, sensitivity, the activity nudge, CSV), the four
          alarms, and the watch's battery over the last 3 days

The tab only shows once a scan has found a watch.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import queue
import re
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, simpledialog, ttk

from PIL import Image, ImageTk

import clockstar_link as cl
import hopper_core as core
import lvgl_image as li
import tooltip
from hopper_core import Kind

# What an imported theme may contain: PNG name -> (file on the watch, size)
IMPORT_FILES = {"bg.png": ("bg.bin", (128, 128)), "bg_bot.png": ("bg_bot.bin", (128, 128)),
                "colon.png": ("clockIcons/colon.bin", (7, 20)),
                **{f"{d}.png": (f"clockIcons/{d}.bin", (20, 20)) for d in "0123456789"}}


class WatchTab(tk.Frame):
    def __init__(self, master, pal: dict, app):
        super().__init__(master, bg=pal["BG"])
        self.pal, self.app = pal, app
        self.q: queue.Queue = queue.Queue()
        self.working = False
        self.apps: list[cl.App] = []
        self.installed: dict[str, int] = {}
        self.active = ""
        self.info: cl.Info | None = None
        self.robot: cl.RobotLink | None = None
        self.steps: cl.Steps | None = None
        self.alarms: list[cl.Alarm] | None = None
        self.batt: tuple[int, list[int]] | None = None
        self.previews: list[ImageTk.PhotoImage] = []
        p = pal

        top = tk.Frame(self, bg=p["BG"])
        top.pack(fill="x", padx=18, pady=(10, 4))
        tk.Label(top, text="WATCH", bg=p["BG"], fg=p["GREEN"], font=("Segoe UI Black", 13)).pack(side="left")
        tk.Label(top, text="the Clockstar: its clock, its menu and its look - over USB", bg=p["BG"], fg=p["DIM"],
                 font=("Segoe UI", 9)).pack(side="left", padx=10, pady=(4, 0))

        # port
        row = tk.Frame(self, bg=p["BG"])
        row.pack(fill="x", padx=18, pady=4)
        tk.Label(row, text="Watch on", bg=p["BG"], fg=p["TEXT"], font=("Segoe UI", 9)).pack(side="left")
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(row, textvariable=self.port_var, state="readonly", style="Hop.TCombobox",
                                     width=34)
        self.port_box.pack(side="left", padx=6)
        self.app.scan_button(row, p["BG"])
        self.read_btn = self.app.button(row, "Read watch", self.refresh, small=True)
        self.read_btn.pack(side="left", padx=(10, 0))
        self.status = tk.Label(self, text="Plug the watch in and hit Scan, then Read watch.", bg=p["BG"],
                               fg=p["DIM"], font=("Segoe UI", 9), anchor="w")
        self.status.pack(fill="x", padx=18)

        # two pages: setting the watch up, and its everyday apps
        self.page_var = tk.StringVar(value="setup")
        prow = tk.Frame(self, bg=p["BG"])
        prow.pack(fill="x", padx=18, pady=(6, 0))
        for key, label in (("setup", "Setup"), ("daily", "Everyday: steps, alarms, battery")):
            tk.Radiobutton(prow, text=label, value=key, variable=self.page_var, command=self._show_page,
                           indicatoron=False, bg=p["PANEL"], fg=p["TEXT"], selectcolor=p["PANEL_HI"],
                           activebackground=p["PANEL_HI"], activeforeground=p["GREEN"], relief="flat", bd=0,
                           padx=12, pady=4, highlightthickness=1, highlightbackground=p["EDGE"],
                           font=("Segoe UI Semibold", 9)).pack(side="left", padx=(0, 6))
        self.pages = {k: tk.Frame(self, bg=p["BG"]) for k in ("setup", "daily")}
        self._page = self.pages["setup"]

        # clock
        clock = self._card("CLOCK")
        crow = tk.Frame(clock, bg=p["PANEL"])
        crow.pack(fill="x", padx=10, pady=(0, 8))
        self.app.button(crow, "Sync time now", self.sync_time, small=True).pack(side="left")
        self.auto_var = tk.BooleanVar(value=self.app.cfg.get("watch_autosync", True))
        tk.Checkbutton(crow, text="Sync by itself when a scan finds the watch", variable=self.auto_var,
                       command=self._save_auto, bg=p["PANEL"], fg=p["TEXT"], selectcolor=p["BG"],
                       activebackground=p["PANEL"], activeforeground=p["TEXT"], font=("Segoe UI", 9)).pack(
            side="left", padx=12)
        self.clock_lbl = tk.Label(crow, text="", bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 9))
        self.clock_lbl.pack(side="left", padx=6)
        frow = tk.Frame(clock, bg=p["PANEL"])
        frow.pack(fill="x", padx=10, pady=(0, 8))
        tk.Label(frow, text="Show the time as", bg=p["PANEL"], fg=p["TEXT"], font=("Segoe UI", 9)).pack(side="left")
        self.hours_var = tk.IntVar(value=0)   # 0 = not read yet
        for hours, label in ((24, "24-hour (13:45)"), (12, "12-hour (1:45 PM)")):
            tk.Radiobutton(frow, text=label, value=hours, variable=self.hours_var, command=self.set_clock_style,
                           bg=p["PANEL"], fg=p["TEXT"], selectcolor=p["BG"], activebackground=p["PANEL"],
                           activeforeground=p["TEXT"], font=("Segoe UI", 9)).pack(side="left", padx=(10, 0))

        # ButterBot link
        bot = self._card("BUTTERBOT")
        brow = tk.Frame(bot, bg=p["PANEL"])
        brow.pack(fill="x", padx=10, pady=(0, 8))
        self.link_var = tk.BooleanVar(value=False)
        self.link_chk = tk.Checkbutton(brow, text="Stay connected to ButterBot", variable=self.link_var,
                                       command=self.set_robot_link, bg=p["PANEL"], fg=p["TEXT"], selectcolor=p["BG"],
                                       activebackground=p["PANEL"], activeforeground=p["TEXT"],
                                       font=("Segoe UI", 9))
        self.link_chk.pack(side="left")
        tooltip.attach(self.link_chk, "The robot's battery shows on the watch face, and its AI answers and alarms "
                                      "(low battery, intruder, gas) arrive as watch notifications. It takes one of "
                                      "the robot's two Bluetooth places, and uses a little more of the watch's "
                                      "battery. Also in the watch's Settings (Robot link).")
        self.app.button(brow, "Test notification", self.test_notify, small=True).pack(side="right")
        self.link_lbl = tk.Label(brow, text="", bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 9))
        self.link_lbl.pack(side="left", padx=12)

        cols = tk.Frame(self._page, bg=p["BG"])
        cols.pack(fill="both", expand=True, padx=18, pady=(4, 10))
        cols.columnconfigure(0, weight=2, uniform="c")
        cols.columnconfigure(1, weight=3, uniform="c")

        # apps
        apps = self._card("MENU", cols)
        apps.master.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        tk.Label(apps, text="Tick the apps to show; arrows move them. Settings always stays.", bg=p["PANEL"],
                 fg=p["DIM"], font=("Segoe UI", 8), anchor="w", wraplength=260, justify="left").pack(fill="x",
                                                                                                     padx=10)
        self.apps_box = tk.Frame(apps, bg=p["PANEL"])
        self.apps_box.pack(fill="x", padx=10, pady=6)
        self.save_apps_btn = self.app.button(apps, "Save menu to watch", self.save_apps, small=True)
        self.save_apps_btn.pack(anchor="w", padx=10, pady=(0, 10))

        # themes
        themes = self._card("THEMES", cols)
        themes.master.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        trow = tk.Frame(themes, bg=p["PANEL"])
        trow.pack(fill="x", padx=10)
        self.app.button(trow, "Watch's own look", lambda: self.use_theme(""), small=True).pack(side="left")
        self.app.button(trow, "Import theme from images…", self.import_theme, small=True).pack(side="left", padx=8)
        self.storage_lbl = tk.Label(trow, text="", bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8))
        self.storage_lbl.pack(side="right")
        self.themes_box = tk.Frame(themes, bg=p["PANEL"])
        self.themes_box.pack(fill="both", expand=True, padx=10, pady=6)

        self._page = self.pages["daily"]
        self._build_daily()
        self._show_page()

        self._draw_apps()
        self._draw_themes()
        self.after(100, self._pump)

    # --- layout helpers -----------------------------------------------------------------------------------
    def _card(self, title: str, parent=None) -> tk.Frame:
        p = self.pal
        outer = tk.Frame(parent or self._page, bg=p["PANEL"], highlightbackground=p["EDGE"], highlightthickness=1)
        if parent is None:
            outer.pack(fill="x", padx=18, pady=6)
        tk.Label(outer, text=title, bg=p["PANEL"], fg=p["GREEN"], font=("Segoe UI Black", 10), anchor="w").pack(
            fill="x", padx=10, pady=(8, 4))
        inner = tk.Frame(outer, bg=p["PANEL"])
        inner.pack(fill="both", expand=True)
        return inner

    def _say(self, text: str, kind: str = "DIM"):
        self.status.configure(text=text, fg=self.pal[kind])

    # --- ports (called by the main window) ----------------------------------------------------------------
    def refresh_ports(self):
        labels = [pt.label() for pt in core.list_ports()]
        self.port_box.configure(values=labels)
        port = self.app.known_ports.get(Kind.CLOCK) if hasattr(self.app, "known_ports") else ""
        if not self.port_var.get() and port:
            core.select_port_in(self.port_box, self.port_var, port)

    def use_port(self, kind: str, device: str):
        if kind != Kind.CLOCK:
            return
        core.select_port_in(self.port_box, self.port_var, device)
        if self.auto_var.get():
            self.sync_time(quiet=True)

    def _port(self) -> str:
        return self.port_var.get().split(" ", 1)[0]

    # --- background work ----------------------------------------------------------------------------------
    def _pump(self):
        try:
            while True:
                self.q.get_nowait()()
        except queue.Empty:
            pass
        self.after(100, self._pump)

    def _run(self, what: str, job, done=None, quiet=False):
        """Runs job(watch) on a worker thread with the watch open; done(result) back on the UI thread."""
        port = self._port()
        if not port:
            if not quiet:
                self._say("Pick the watch's port first (or hit Scan).", "WARN")
            return
        if self.working or self.app.busy:
            if not quiet:
                self._say("Busy - try again in a moment.", "WARN")
            return
        self.working = True
        self._say(what + "…")

        def work():
            try:
                with cl.Watch(port) as w:
                    res = job(w)
                self.q.put(lambda: (done(res) if done else None))
            except cl.WatchError as e:
                msg = str(e)
                self.q.put(lambda: self._say(msg if not quiet else f"Couldn't reach the watch: {msg}", "BAD"))
            except Exception as e:  # never let a worker die silently
                msg = f"{type(e).__name__}: {e}"
                self.q.put(lambda: self._say(msg, "BAD"))
            finally:
                self.q.put(lambda: setattr(self, "working", False))
        threading.Thread(target=work, daemon=True).start()

    # --- reading ------------------------------------------------------------------------------------------
    def refresh(self):
        def job(w):
            return w.info(), w.apps(), w.themes(), w.robot_link(), w.steps(), w.alarms(), w.batt_history()

        def done(res):
            self.info, self.apps, (self.installed, self.active), self.robot, self.steps, self.alarms, self.batt = res
            self._show_robot()
            self._show_daily()
            self._draw_apps()
            self._draw_themes()
            self.hours_var.set(self.info.clock)
            self._show_clock()
            free = max(0, self.info.total - self.info.used)
            self.storage_lbl.configure(text=f"{free // 1024} KB free on the watch")
            self._say(f"Watch read - firmware {self.info.fw}.", "GREEN")
        self._run("Reading the watch", job, done)

    def _show_clock(self):
        if not self.info:
            return
        drift = self.info.time - cl.local_epoch()
        txt = self._fmt(self.info.time, seconds=False)
        self.clock_lbl.configure(text=f"Watch says {txt}" + ("" if abs(drift) < 60 else f"  ({drift // 60:+d} min off)"),
                                 fg=self.pal["DIM"] if abs(drift) < 60 else self.pal["WARN"])

    # --- clock --------------------------------------------------------------------------------------------
    def _save_auto(self):
        self.app.cfg["watch_autosync"] = bool(self.auto_var.get())
        core.save_config(self.app.cfg)

    def sync_time(self, quiet: bool = False):
        def done(epoch):
            self.clock_lbl.configure(text=f"Set to {self._fmt(epoch)}", fg=self.pal["GREEN"])
            self._say("The watch's clock now matches this PC.", "GREEN")
        self._run("Setting the watch's clock", lambda w: w.sync_time(), done, quiet=quiet)

    def _fmt(self, epoch: int, seconds: bool = True) -> str:
        """A watch time (local wall time stored as UTC) in the watch's own clock style."""
        t = time.gmtime(epoch)
        if self.hours_var.get() == 12:
            s = time.strftime("%I:%M:%S %p" if seconds else "%I:%M %p", t)
            return s[1:] if s.startswith("0") else s
        return time.strftime("%H:%M:%S" if seconds else "%H:%M", t)

    def set_clock_style(self):
        hours = self.hours_var.get()
        previous = self.info.clock if self.info else 24

        def done(_):
            if self.info:
                self.info.clock = hours
            self._show_clock()
            self._say("The watch now shows " + ("12-hour time with AM/PM." if hours == 12 else "24-hour time."),
                      "GREEN")

        def job(w):
            try:
                w.set_clock(hours)
            except cl.WatchError:
                self.q.put(lambda: self.hours_var.set(previous))
                raise
        self._run("Changing the clock style", job, done)

    # --- pages ------------------------------------------------------------------------------------------------
    def _show_page(self):
        for f in self.pages.values():
            f.pack_forget()
        self.pages[self.page_var.get()].pack(fill="both", expand=True)

    # --- everyday: steps, alarms, battery -----------------------------------------------------------------------
    def _check(self, master, text, var, cmd=None):
        p = self.pal
        return tk.Checkbutton(master, text=text, variable=var, command=cmd, bg=p["PANEL"], fg=p["TEXT"],
                              selectcolor=p["BG"], activebackground=p["PANEL"], activeforeground=p["TEXT"],
                              font=("Segoe UI", 9))

    def _build_daily(self):
        p = self.pal
        cols = tk.Frame(self._page, bg=p["BG"])
        cols.pack(fill="x", padx=18, pady=(4, 0))
        cols.columnconfigure(0, weight=1, uniform="d")
        cols.columnconfigure(1, weight=1, uniform="d")

        # steps
        st = self._card("STEPS", cols)
        st.master.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.steps_lbl = tk.Label(st, text="Read watch to see your steps.", bg=p["PANEL"], fg=p["TEXT"],
                                  font=("Segoe UI Semibold", 11), anchor="w")
        self.steps_lbl.pack(fill="x", padx=10)
        self.steps_cv = tk.Canvas(st, height=130, bg=p["PANEL"], highlightthickness=0)
        self.steps_cv.pack(fill="x", padx=10, pady=6)
        self.steps_cv.bind("<Configure>", lambda e: self._draw_steps())
        g = tk.Frame(st, bg=p["PANEL"])
        g.pack(fill="x", padx=10, pady=(0, 4))
        tk.Label(g, text="Daily goal", bg=p["PANEL"], fg=p["TEXT"], font=("Segoe UI", 9)).pack(side="left")
        self.goal_var = tk.StringVar(value="")
        tk.Entry(g, textvariable=self.goal_var, width=7, bg=p["PANEL_HI"], fg=p["TEXT"], insertbackground=p["TEXT"],
                 relief="flat").pack(side="left", padx=6)
        tk.Label(g, text="steps (0 = none)", bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8)).pack(side="left")
        sr = tk.Frame(st, bg=p["PANEL"])
        sr.pack(fill="x", padx=10, pady=(0, 4))
        tk.Label(sr, text="Counting", bg=p["PANEL"], fg=p["TEXT"], font=("Segoe UI", 9)).pack(side="left")
        self.sens_var = tk.IntVar(value=1)
        for v, label in ((0, "Strict"), (1, "Normal"), (2, "Sensitive")):
            tk.Radiobutton(sr, text=label, value=v, variable=self.sens_var, bg=p["PANEL"], fg=p["TEXT"],
                           selectcolor=p["BG"], activebackground=p["PANEL"], activeforeground=p["TEXT"],
                           font=("Segoe UI", 9)).pack(side="left", padx=(8, 0))
        tooltip.attach(sr, "Strict skips more false steps (typing, bumpy rides); Sensitive catches slow, soft steps.")
        self.nudge_var = tk.BooleanVar(value=True)
        self._check(st, "Nudge me after an hour of barely moving (9:00-21:00)", self.nudge_var).pack(anchor="w",
                                                                                                    padx=10)
        br = tk.Frame(st, bg=p["PANEL"])
        br.pack(fill="x", padx=10, pady=(4, 10))
        self.app.button(br, "Save step settings", self.save_steps, small=True).pack(side="left")
        self.app.button(br, "Save history as CSV…", self.steps_csv, small=True).pack(side="left", padx=8)

        # alarms
        al = self._card("ALARMS", cols)
        al.master.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        tk.Label(al, text="They beep on the watch, asleep or not (it has no vibration motor). No day ticked = once.",
                 bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8), anchor="w", wraplength=420,
                 justify="left").pack(fill="x", padx=10)
        grid = tk.Frame(al, bg=p["PANEL"])
        grid.pack(fill="x", padx=10, pady=6)
        for j, d in enumerate(cl.DAY_NAMES):
            tk.Label(grid, text=d[:2], bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8)).grid(row=0, column=3 + j)
        self.alarm_rows = []
        for i in range(4):
            on = tk.BooleanVar(value=False)
            tm = tk.StringVar(value="07:00")
            days = [tk.BooleanVar(value=j < 5) for j in range(7)]
            tk.Label(grid, text=f"{i + 1}", bg=p["PANEL"], fg=p["GREEN"], font=("Segoe UI Black", 9)).grid(
                row=i + 1, column=0, padx=(0, 4))
            self._check(grid, "On", on).grid(row=i + 1, column=1)
            tk.Entry(grid, textvariable=tm, width=6, bg=p["PANEL_HI"], fg=p["TEXT"], insertbackground=p["TEXT"],
                     relief="flat", justify="center").grid(row=i + 1, column=2, padx=6, pady=2)
            for j in range(7):
                tk.Checkbutton(grid, variable=days[j], bg=p["PANEL"], selectcolor=p["BG"], activebackground=p["PANEL"],
                               bd=0, highlightthickness=0).grid(row=i + 1, column=3 + j)
            self.alarm_rows.append((on, tm, days))
        tk.Label(al, text="Time as 24-hour HH:MM (e.g. 06:45 or 18:30).", bg=p["PANEL"], fg=p["DIM"],
                 font=("Segoe UI", 8), anchor="w").pack(fill="x", padx=10)
        ar = tk.Frame(al, bg=p["PANEL"])
        ar.pack(fill="x", padx=10, pady=(6, 10))
        self.app.button(ar, "Save alarms to watch", self.save_alarms, small=True).pack(side="left")
        self.app.button(ar, "Hear the alarm", self.ring, small=True).pack(side="left", padx=8)

        # battery
        bt = self._card("WATCH BATTERY - THE LAST 3 DAYS")
        self.batt_lbl = tk.Label(bt, text="Read watch to see its battery history (it starts again when the watch "
                                          "restarts).", bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8), anchor="w")
        self.batt_lbl.pack(fill="x", padx=10)
        self.batt_cv = tk.Canvas(bt, height=150, bg=p["PANEL"], highlightthickness=0)
        self.batt_cv.pack(fill="x", padx=10, pady=(4, 10))
        self.batt_cv.bind("<Configure>", lambda e: self._draw_batt())

    def _show_daily(self):
        s = self.steps
        if s is None:
            self.steps_lbl.configure(text="This watch firmware has no step counter - update it from the Flash tab.")
        else:
            goal = f" of {s.goal:,}" if s.goal else ""
            self.steps_lbl.configure(text=f"Today: {s.today:,}{goal} steps")
            self.goal_var.set(str(s.goal))
            self.sens_var.set(s.sens)
            self.nudge_var.set(s.nudge)
        for row, a in zip(self.alarm_rows, self.alarms or []):
            on, tm, days = row
            on.set(a.on)
            tm.set(f"{a.hour:02d}:{a.minute:02d}")
            for j in range(7):
                days[j].set(bool(a.days >> j & 1))
        self._draw_steps()
        self._draw_batt()

    def _draw_steps(self):
        cv, p = self.steps_cv, self.pal
        cv.delete("all")
        if not self.steps:
            return
        w = max(cv.winfo_width(), 200)
        days = list(reversed(self.steps.days))          # oldest first
        goal = self.steps.goal
        top = max(days + [goal, 1])
        today = dt.date.today()
        bw = (w - 20) / 7
        if goal:
            y = 110 - 95 * goal / top
            cv.create_line(0, y, w, y, fill=p["DIM"], dash=(3, 3))
            cv.create_text(w - 2, y - 7, text=f"goal {goal:,}", fill=p["DIM"], anchor="e", font=("Segoe UI", 7))
        for i, v in enumerate(days):
            x0 = 10 + i * bw + bw * 0.18
            x1 = 10 + (i + 1) * bw - bw * 0.18
            y = 110 - 95 * v / top
            color = p["GREEN"] if i == 6 else (p["LIME"] if goal and v >= goal else p["EDGE"])
            cv.create_rectangle(x0, y, x1, 110, fill=color, outline="")
            if v:
                cv.create_text((x0 + x1) / 2, y - 7, text=f"{v:,}", fill=p["TEXT"], font=("Segoe UI", 7))
            day = today - dt.timedelta(days=6 - i)
            cv.create_text((x0 + x1) / 2, 121, text="today" if i == 6 else day.strftime("%a"),
                           fill=p["GREEN"] if i == 6 else p["DIM"], font=("Segoe UI", 8))

    def _draw_batt(self):
        cv, p = self.batt_cv, self.pal
        cv.delete("all")
        if not self.batt or not self.batt[1]:
            if self.info is not None and not self.batt:
                self.batt_lbl.configure(text="No battery history from this watch firmware.")
            return
        every, vals = self.batt
        w, h = max(cv.winfo_width(), 300), 150
        span = 3 * 24 * 60                               # minutes shown
        left, right, top, bot = 34, w - 8, 8, h - 20

        def x(minutes_ago):
            return right - (right - left) * minutes_ago / span

        def y(pct):
            return bot - (bot - top) * pct / 100
        for pct in (0, 25, 50, 75, 100):
            cv.create_line(left, y(pct), right, y(pct), fill=p["EDGE"])
            cv.create_text(left - 4, y(pct), text=f"{pct}%", anchor="e", fill=p["DIM"], font=("Segoe UI", 7))
        for d in range(4):
            xx = x(d * 24 * 60)
            cv.create_line(xx, top, xx, bot, fill=p["EDGE"], dash=(2, 4))
            cv.create_text(xx, bot + 10, text="now" if d == 0 else f"{d} day{'s' if d > 1 else ''} ago",
                           fill=p["DIM"], font=("Segoe UI", 7))
        n = len(vals)
        pts = [(x((n - 1 - i) * every), y(v & 0x7F), v >= 128) for i, v in enumerate(vals)]
        for (x0, _, c0), (x1, _, _) in zip(pts, pts[1:]):
            if c0:
                cv.create_rectangle(x0, top, x1, bot, fill="#10301a", outline="")
        for (x0, y0, _), (x1, y1, _) in zip(pts, pts[1:]):
            cv.create_line(x0, y0, x1, y1, fill=p["GREEN"], width=2)
        if n == 1:
            cv.create_oval(pts[0][0] - 3, pts[0][1] - 3, pts[0][0] + 3, pts[0][1] + 3, fill=p["GREEN"], outline="")
        hours = (n - 1) * every / 60
        now = vals[-1]
        self.batt_lbl.configure(
            text=f"Now {now & 0x7F}%{' (charging)' if now >= 128 else ''}. A reading every {every} minutes, "
                 f"{hours:.1f} hours so far; shaded = charging. It starts again when the watch restarts.")

    def save_steps(self):
        try:
            goal = int(self.goal_var.get().strip() or "0")
            if not 0 <= goal <= 100000:
                raise ValueError
        except ValueError:
            self._say("The goal needs to be a number of steps, 0-100000.", "WARN")
            return
        sens, nudge = self.sens_var.get(), bool(self.nudge_var.get())

        def job(w):
            w.set_steps(goal=goal, sens=sens, nudge=nudge)
            return w.steps()

        def done(s):
            self.steps = s
            self._show_daily()
            self._say("Step settings saved on the watch.", "GREEN")
        self._run("Saving step settings", job, done)

    def steps_csv(self):
        if not self.steps:
            self._say("Read the watch first.", "WARN")
            return
        path = filedialog.asksaveasfilename(parent=self, title="Save step history", defaultextension=".csv",
                                            initialfile=f"steps-{dt.date.today()}.csv",
                                            filetypes=[("CSV", "*.csv")])
        if not path:
            return
        today = dt.date.today()
        with open(path, "w", newline="", encoding="utf-8") as f:
            wr = csv.writer(f)
            wr.writerow(["date", "steps"])
            for i in range(6, -1, -1):
                wr.writerow([(today - dt.timedelta(days=i)).isoformat(), self.steps.days[i]])
        self._say(f"Saved {Path(path).name}.", "GREEN")

    def save_alarms(self):
        alarms = []
        for i, (on, tm, days) in enumerate(self.alarm_rows):
            m = re.fullmatch(r"\s*(\d{1,2})[:.](\d{2})\s*", tm.get())
            if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
                self._say(f"Alarm {i + 1}: the time should look like 06:45 (24-hour).", "WARN")
                return
            mask = sum(1 << j for j in range(7) if days[j].get())
            alarms.append(cl.Alarm(bool(on.get()), int(m.group(1)), int(m.group(2)), mask))

        def job(w):
            for i, a in enumerate(alarms):
                w.set_alarm(i + 1, a)
            return w.alarms()

        def done(res):
            self.alarms = res
            self._show_daily()
            n = sum(a.on for a in alarms)
            self._say(f"Alarms saved - {n} on." if n else "Alarms saved - all off.", "GREEN")
        self._run("Saving the alarms", job, done)

    def ring(self):
        self._run("Ringing the watch", lambda w: w.ring(),
                  lambda _: self._say("The watch is ringing - press any button on it to stop.", "GREEN"))


    # --- ButterBot link -------------------------------------------------------------------------------------
    def _show_robot(self):
        r = self.robot
        if r is None:
            self.link_chk.configure(state="disabled")
            self.link_lbl.configure(text="This watch firmware has no robot link - update it from the Flash tab.",
                                    fg=self.pal["DIM"])
            return
        self.link_chk.configure(state="normal")
        self.link_var.set(r.on)
        if not r.on:
            text, kind = "Off - the robot only connects while the ButterBot app is open.", "DIM"
        elif r.state == "connected":
            batt = "full" if r.charge == 2 else (f"{r.battery}%" + (", charging" if r.charge == 1 else "")
                                                 if r.battery >= 0 else "battery not heard yet")
            text, kind = f"Connected - robot battery {batt}.", "GREEN"
        elif r.state in ("scanning", "connecting"):
            text, kind = "Looking for the robot…", "DIM"
        else:
            text, kind = "Not in reach - the watch looks again every so often (switched on?).", "WARN"
        self.link_lbl.configure(text=text, fg=self.pal[kind])

    def set_robot_link(self):
        on = bool(self.link_var.get())

        def job(w):
            try:
                w.set_robot_link(on)
            except cl.WatchError:
                self.q.put(lambda: self.link_var.set(not on))
                raise
            time.sleep(0.2)
            return w.robot_link()

        def done(r):
            self.robot = r
            self._show_robot()
            self._say("The watch now stays linked to ButterBot." if on else "The background link is off.", "GREEN")
            if on:   # finding the robot takes a few seconds: look again
                self.after(9000, lambda: self._run("Checking the robot link", lambda w: w.robot_link(),
                                                   lambda r2: (setattr(self, "robot", r2), self._show_robot()),
                                                   quiet=True))
        self._run("Changing the robot link", job, done)

    def test_notify(self):
        self._run("Sending a test notification", lambda w: w.test_notify(),
                  lambda _: self._say("Sent - look at the watch: a ButterBot notification with the robot icon.",
                                      "GREEN"))

    # --- apps ---------------------------------------------------------------------------------------------
    def _draw_apps(self):
        p = self.pal
        for w in self.apps_box.winfo_children():
            w.destroy()
        if not self.apps:
            tk.Label(self.apps_box, text="Read watch to see its menu.", bg=p["PANEL"], fg=p["DIM"],
                     font=("Segoe UI", 9)).pack(anchor="w")
            return
        for i, a in enumerate(self.apps):
            r = tk.Frame(self.apps_box, bg=p["PANEL"])
            r.pack(fill="x", pady=1)
            var = tk.BooleanVar(value=a.shown)
            locked = a.id == "settings"

            def toggle(v=var, app=a):
                app.shown = bool(v.get())
            tk.Checkbutton(r, text=a.title, variable=var, command=toggle, bg=p["PANEL"], fg=p["TEXT"],
                           selectcolor=p["BG"], activebackground=p["PANEL"], activeforeground=p["TEXT"],
                           font=("Segoe UI", 9), state="disabled" if locked else "normal",
                           anchor="w").pack(side="left", fill="x", expand=True)
            for sym, d in (("▲", -1), ("▼", 1)):
                b = tk.Button(r, text=sym, command=lambda i=i, d=d: self._move(i, d), bg=p["PANEL_HI"], fg=p["TEXT"],
                              relief="flat", bd=0, padx=6, font=("Segoe UI", 8), cursor="hand2")
                b.pack(side="left", padx=1)
                if (d < 0 and i == 0) or (d > 0 and i == len(self.apps) - 1):
                    b.configure(state="disabled")

    def _move(self, i: int, d: int):
        j = i + d
        if 0 <= j < len(self.apps):
            self.apps[i], self.apps[j] = self.apps[j], self.apps[i]
            self._draw_apps()

    def save_apps(self):
        if not self.apps:
            self._say("Read watch first.", "WARN")
            return
        apps = list(self.apps)
        self._run("Saving the menu", lambda w: w.set_apps(apps),
                  lambda _: self._say("Menu saved - it shows the next time you open the watch's menu.", "GREEN"))

    # --- themes -------------------------------------------------------------------------------------------
    def _draw_themes(self):
        p = self.pal
        for w in self.themes_box.winfo_children():
            w.destroy()
        self.previews.clear()
        local = cl.local_themes()
        ids = {t.id for t in local}
        if not self.active:
            tk.Label(self.themes_box, text="Now on the watch: its own look" if self.info else
                     "Read watch to see which theme it uses.", bg=p["PANEL"], fg=p["DIM"],
                     font=("Segoe UI", 9)).pack(anchor="w", pady=(0, 4))
        for t in local:
            self._theme_row(t, t.id in self.installed, t.id == self.active)
        for tid in self.installed:                       # on the watch but not on this PC
            if tid not in ids:
                self._theme_row(None, True, tid == self.active, tid)

    def _theme_row(self, t: cl.Theme | None, on_watch: bool, active: bool, tid: str = ""):
        p = self.pal
        tid = t.id if t else tid
        r = tk.Frame(self.themes_box, bg=p["PANEL_HI"] if active else p["PANEL"], highlightthickness=1,
                     highlightbackground=p["GREEN"] if active else p["EDGE"])
        r.pack(fill="x", pady=3)
        bg = r["bg"]
        prev = t.folder / "preview.png" if t else None
        if prev and prev.is_file():
            img = ImageTk.PhotoImage(Image.open(prev).resize((64, 64), Image.NEAREST))
            self.previews.append(img)
            tk.Label(r, image=img, bg=bg).pack(side="left", padx=6, pady=6)
        txt = tk.Frame(r, bg=bg)
        txt.pack(side="left", fill="both", expand=True, pady=6)
        name = t.name if t else tid
        state = " - in use" if active else (" - on the watch" if on_watch else "")
        tk.Label(txt, text=name + state, bg=bg, fg=p["GREEN"] if active else p["TEXT"],
                 font=("Segoe UI Semibold", 10), anchor="w").pack(fill="x")
        desc = t.description if t else "On the watch only (not on this PC)."
        if t:
            desc += f"  ({t.size // 1024 + 1} KB)"
        tk.Label(txt, text=desc, bg=bg, fg=p["DIM"], font=("Segoe UI", 8), anchor="w", justify="left",
                 wraplength=330).pack(fill="x")
        btns = tk.Frame(r, bg=bg)
        btns.pack(side="right", padx=6)
        if not active and t:
            self.app.button(btns, "Use" if on_watch else "Put on watch + use", lambda: self.put_theme(t, on_watch),
                            small=True).pack(anchor="e", pady=2)
        elif not active and on_watch:
            self.app.button(btns, "Use", lambda: self.use_theme(tid), small=True).pack(anchor="e", pady=2)
        if on_watch:
            self.app.button(btns, "Remove from watch", lambda: self.remove_theme(tid), small=True).pack(anchor="e",
                                                                                                         pady=2)

    def _after_restart(self, msg: str):
        self._say(msg, "GREEN")
        self.after(9000, self.refresh)   # the watch restarts to apply a theme: read it again once it's back

    def put_theme(self, t: cl.Theme, on_watch: bool):
        if bad := t.problems():
            self._say("; ".join(bad), "BAD")
            return
        if not on_watch and self.info and t.size + 8192 > self.info.total - self.info.used:
            self._say("Not enough room on the watch - remove a theme first.", "BAD")
            return

        def job(w):
            if not on_watch:
                w.upload_theme(t, lambda done, total: self.q.put(
                    lambda: self._say(f"Putting {t.name} on the watch… {done * 100 // max(1, total)}%")))
            w.use_theme(t.id)
        self._run(f"Putting {t.name} on the watch", job,
                  lambda _: self._after_restart(f"{t.name} is on - the watch restarts to show it."))

    def use_theme(self, tid: str):
        self._run("Switching theme", lambda w: w.use_theme(tid),
                  lambda _: self._after_restart("Switched - the watch restarts to show it."))

    def remove_theme(self, tid: str):
        self._run("Removing the theme", lambda w: w.remove_theme(tid),
                  lambda _: self._after_restart("Removed.") if tid == self.active else self.refresh())

    def import_theme(self):
        folder = filedialog.askdirectory(title="Folder with your theme's PNGs (bg.png, bg_bot.png, 0-9.png, colon.png)")
        if not folder:
            return
        src = Path(folder)
        found = {n: v for n, v in IMPORT_FILES.items() if (src / n).is_file()}
        if not found:
            self._say("No theme images there: name them bg.png, bg_bot.png, 0.png … 9.png, colon.png.", "WARN")
            return
        name = simpledialog.askstring("Theme name", "Name for this theme:", initialvalue=src.name, parent=self)
        if not name:
            return
        tid = re.sub(r"[^a-z0-9]", "", name.lower())[:8] or "mine"
        dest = cl.user_themes_dir() / tid
        try:
            dest.mkdir(parents=True, exist_ok=True)
            for png, (rel, size) in found.items():
                out = dest / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(li.from_file(src / png, size))
            pv = Image.open(src / "bg.png").convert("RGBA").resize((128, 128)) if "bg.png" in found else \
                Image.new("RGBA", (128, 128), (0, 0, 0, 255))
            pv.save(dest / "preview.png")
            (dest / "theme.json").write_text(json.dumps({"id": tid, "name": name, "description":
                                                         f"Imported from {src.name}: {len(found)} images."}),
                                             encoding="utf-8")
        except (OSError, ValueError) as e:
            self._say(f"Couldn't import: {e}", "BAD")
            return
        self._say(f"Imported {name} ({len(found)} images). Put it on the watch from the list.", "GREEN")
        self._draw_themes()
