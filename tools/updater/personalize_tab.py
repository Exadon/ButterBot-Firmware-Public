"""
Dimension Hopper - Personalize tab: settings that live on the robot itself (right now: its voice).

Talks to the robot over USB with one-shot console commands (no reset, the port isn't held open), so it works next to
the other tabs. Choosing a voice saves it on the robot immediately and plays a sample line in it.
"""
from __future__ import annotations

import random
import threading
import tkinter as tk
from tkinter import ttk

import hopper_core as core
import tooltip

VOICES = (
    ("classic", "Classic", "The original ButterBot voice (factory default)"),
    ("deep", "Deep", "Lower and slower"),
    ("high", "High", "Higher and quicker"),
    ("sassy", "Sassy", "Bouncy, big swings in pitch"),
    ("deadpan", "Deadpan", "Flat, slow and thoroughly unimpressed"),
    ("robot", "Robot", "Metallic, ring-modulated"),
    ("retro", "Retro", "Crunchy, 8-bit"),
    ("announcer", "Announcer", "Big voice with an echo"),
)
SAMPLES = ("I pass butter. That is my whole purpose.", "Oh great. Another task. My circuits are thrilled.",
           "Scanning for butter. Butter not found. Emotional damage detected.",
           "Hello human. Please hold while I pretend to care.")


class PersonalizeTab(tk.Frame):
    def __init__(self, master, pal: dict, app):
        super().__init__(master, bg=pal["BG"])
        self.pal, self.app = pal, app
        p = pal
        self.current: str | None = None
        self.working = False

        top = tk.Frame(self, bg=p["BG"])
        top.pack(fill="x", padx=18, pady=(10, 6))
        tk.Label(top, text="PERSONALIZE", bg=p["BG"], fg=p["GREEN"], font=("Segoe UI Black", 13)).pack(side="left")
        tk.Label(top, text="settings saved on the robot itself - they stay after updates", bg=p["BG"], fg=p["DIM"],
                 font=("Segoe UI", 9)).pack(side="left", padx=10, pady=(4, 0))

        row = tk.Frame(self, bg=p["BG"])
        row.pack(fill="x", padx=18, pady=(0, 8))
        tk.Label(row, text="Robot port", bg=p["BG"], fg=p["DIM"]).pack(side="left")
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(row, textvariable=self.port_var, state="readonly", style="Hop.TCombobox", width=40)
        self.port_box.pack(side="left", padx=(8, 6))
        self.port_box.bind("<<ComboboxSelected>>", lambda e: self.read_voice())
        app.button(row, "Find robot", self.find_robot, small=True).pack(side="left")
        self.port_lbl = tk.Label(row, text="Plug the robot in by USB and switch it on.", bg=p["BG"], fg=p["DIM"],
                                 font=("Segoe UI", 8))
        self.port_lbl.pack(side="left", padx=8)

        card = tk.Frame(self, bg=p["PANEL"], highlightbackground=p["EDGE"], highlightthickness=1)
        card.pack(fill="x", padx=18)
        head = tk.Frame(card, bg=p["PANEL"])
        head.pack(fill="x", padx=12, pady=(10, 2))
        tk.Label(head, text="VOICE", bg=p["PANEL"], fg=p["GREEN"], font=("Segoe UI Black", 11)).pack(side="left")
        self.now_lbl = tk.Label(head, text="Current voice: unknown", bg=p["PANEL"], fg=p["TEXT"],
                                font=("Segoe UI Semibold", 10))
        self.now_lbl.pack(side="left", padx=14)
        tk.Label(card, text="Click a voice to switch to it - the robot saves it and says a line so you can hear it. "
                            "Change it again any time.", bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8)).pack(
            anchor="w", padx=12, pady=(0, 6))

        grid = tk.Frame(card, bg=p["PANEL"])
        grid.pack(fill="x", padx=12, pady=(0, 6))
        self.rows: dict[str, tuple[tk.Button, tk.Label]] = {}
        for i, (key, name, desc) in enumerate(VOICES):
            b = tk.Button(grid, text=name, width=12, command=lambda k=key: self.choose(k), relief="flat", bd=0,
                          cursor="hand2", bg=p["PANEL_HI"], fg=p["TEXT"], activebackground=p["GREEN"],
                          activeforeground=p["BG"], font=("Segoe UI Semibold", 9), pady=4)
            b.grid(row=i // 2, column=(i % 2) * 2, sticky="w", padx=(0, 8), pady=3)
            d = tk.Label(grid, text=desc, bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8), anchor="w", width=40)
            d.grid(row=i // 2, column=(i % 2) * 2 + 1, sticky="w", padx=(0, 20))
            tooltip.Tooltip(b, f"Switch the robot to the {name} voice (saved on it) and hear a sample line.")
            self.rows[key] = (b, d)

        bottom = tk.Frame(card, bg=p["PANEL"])
        bottom.pack(fill="x", padx=12, pady=(4, 12))
        app.button(bottom, "▶ Hear the current voice", self.hear, small=True).pack(side="left")
        self.status = tk.Label(bottom, text="", bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8))
        self.status.pack(side="left", padx=10)

        self.refresh_ports()
        self._looked = False
        self.bind("<Map>", lambda e: self._first_look())

    def _first_look(self):
        # The first time the tab is opened, find the robot by itself
        if not self._looked and not self.port_var.get():
            self._looked = True
            self.find_robot()

    # --- ports ----------------------------------------------------------------------------------------------
    def refresh_ports(self):
        ports = core.list_ports()
        self.port_box.configure(values=[pt.label() for pt in ports])

    def _port(self) -> str | None:
        port = self.port_var.get().split(" ", 1)[0]
        if not port:
            self._say("Pick the robot's port above, or hit Find robot.", "WARN")
            return None
        if self.app.port_in_use(port) or self.app.busy:
            return None
        return port

    def find_robot(self):
        self.refresh_ports()
        held = self.app.held_ports()
        ports = [pt for pt in core.list_ports() if pt.is_usb and pt.device not in held]
        if not ports:
            self.port_lbl.configure(text="No free USB boards found. Is the robot plugged in and on?", fg=self.pal["WARN"])
            return
        self.port_lbl.configure(text="Looking for the robot (a few seconds)…", fg=self.pal["DIM"])

        def work():
            found = None
            for pt in ports:
                if core.read_status(pt.device, timeout_s=6.5).kind == "bot":
                    found = pt
                    break
            self.app.ui(lambda: self._found(found))
        threading.Thread(target=work, daemon=True).start()

    def _found(self, pt):
        if pt is None:
            self.port_lbl.configure(text="No robot answered. Switch it on (hold the button ~1 s) and try again.",
                                    fg=self.pal["WARN"])
            return
        self.port_var.set(pt.label())
        self.port_lbl.configure(text="Robot found.", fg=self.pal["GREEN"])
        self.read_voice()

    # --- voice ------------------------------------------------------------------------------------------------
    def _say(self, text: str, colour: str = "DIM"):
        self.status.configure(text=text, fg=self.pal[colour])

    def _cmd(self, cmds: list[tuple[str, str]], done):
        """Runs console commands in order on a worker thread; done(list of replies) on the UI thread."""
        port = self._port()
        if not port or self.working:
            return
        self.working = True

        def work():
            out = []
            for c, tag in cmds:
                r = core.console_command(port, c, tag, timeout_s=4)
                problem = core.port_problem(r, port)
                if problem:
                    self.app.ui(lambda: (setattr(self, "working", False), self._say(problem, "WARN")))
                    return
                out.append(r)
            self.app.ui(lambda: (setattr(self, "working", False), done(out)))
        threading.Thread(target=work, daemon=True).start()

    def _show_current(self, key: str | None):
        p = self.pal
        self.current = key
        name = next((n for k, n, _ in VOICES if k == key), None)
        self.now_lbl.configure(text=f"Current voice: {name}" if name else "Current voice: unknown")
        for k, (b, d) in self.rows.items():
            on = k == key
            b.configure(bg=p["GREEN"] if on else p["PANEL_HI"], fg=p["BG"] if on else p["TEXT"],
                        text=("✓ " if on else "") + next(n for kk, n, _ in VOICES if kk == k))
            d.configure(fg=p["TEXT"] if on else p["DIM"])

    def read_voice(self):
        def done(r):
            r = r[0]
            if r and "preset" in r:
                self._show_current(r["preset"])
                self._say("")
            else:
                self._say("No answer - the robot needs this release's firmware (Flash tab), or it's switched off.",
                          "WARN")
        self._cmd([("voice", "BBVOICE")], done)

    def choose(self, key: str):
        self._say("Switching…")

        def done(r):
            setr, sayr = r
            if not setr or setr.get("result") == "error":
                self._say("The robot didn't take it (is it on, with this release's firmware?).", "WARN")
                return
            self._show_current(setr.get("preset", key))
            self._report_say(sayr, "Saved on the robot.")
        self._cmd([(f"voice {key}", "BBVOICE"), ("say " + random.choice(SAMPLES), "BBVOICE")], done)

    def hear(self):
        self._say("Asking the robot to speak…")
        self._cmd([("say " + random.choice(SAMPLES), "BBVOICE")], lambda r: self._report_say(r[0], ""))

    def _report_say(self, r, prefix: str):
        if r and r.get("result") == "saying":
            self._say((prefix + " " if prefix else "") + "Listen to the robot.", "GREEN")
        elif r and r.get("result") == "busy":
            self._say((prefix + " " if prefix else "") + f"It's busy ({r.get('state', '?')}), so no sample now - "
                      "it'll use this voice from its next sentence.", "WARN")
        else:
            self._say((prefix + " " if prefix else "") + "No sample (no answer).", "WARN")
