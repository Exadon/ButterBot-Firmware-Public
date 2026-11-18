"""
Dimension Hopper - Personalize tab: make the robot yours.
  * Voice           - eight presets plus pitch / speed nudges, saved on the robot ("voice <name>", "voice tune p s")
  * Names           - yours (it greets you by name) and the robot's; saved on the robot ("names") and used by the AI
  * Start-up line   - what the robot says when it switches on ("startline <hex>")
  * Controller      - its colours ("theme <n>"; switches live), screen brightness and sleep time ("screen b s")
  * Your ramble lines - mixed into the robot's idle rambles ("ramble add / clear")
  * Chat replies    - your own answer to any chat voice line ("chat set <i> <hex>")
  * AI personality and facts about you - how the AI version talks (kept on this PC, used by the brain)

Talks to the boards over USB with one-shot console commands (no reset, the port isn't held open), so it works next to
the other tabs.
"""
from __future__ import annotations

import json
import random
import threading
import time
import tkinter as tk
from tkinter import ttk

import backup as backup_mod
import brain
import hopper_core as core
import mac_compat
import tooltip
from chat_lines import CHAT_LINES

THEMES = (("Main", "#a0fa00", "#00b400", "#000000"), ("Rose", "#e90081", "#5c00d7", "#000000"),
          ("Red", "#e13800", "#ffae00", "#2a1600"), ("Blue", "#0300a3", "#ffae00", "#ffffff"),
          # the fork's recoloured themes (controller tools/theme_tool.py)
          ("Portal", "#00e6be", "#78ff3c", "#000000"), ("Butter", "#ffd640", "#d68c00", "#1e1200"),
          ("Ice", "#78e6ff", "#008cff", "#000a28"), ("Mono", "#ffffff", "#969696", "#000000"),
          ("Purple", "#c878ff", "#ff50c8", "#100020"))

VOICES = (
    ("classic", "🧈 Classic", "The original voice (factory default)"),
    ("deep", "🐻 Deep", "Lower and slower"),
    ("high", "🐭 High", "Higher and quicker"),
    ("sassy", "💅 Sassy", "Bouncy, big swings in pitch"),
    ("deadpan", "😑 Deadpan", "Flat, slow and thoroughly unimpressed"),
    ("robot", "🤖 Robot", "Metallic, ring-modulated"),
    ("retro", "👾 Retro", "Crunchy, 8-bit"),
    ("announcer", "📢 Announcer", "Big voice with an echo"),
    ("show", "📺 Show accurate", "Closest to the ButterBot from the show: pitch, speed and sound measured from the "
                                "episode, and locked"),
)
SHOW = "show"
PERSONALITY_ICONS = {"classic": "🙄", "sarcastic": "😏", "dramatic": "🎭", "wholesome": "🌻", "grumpy": "😾",
                     "custom": "✏️"}
SHOW_TIP = ("Forces pitch, speed and sound to be closer to the show's ButterBot (measured from the episode: a low, "
            "flat, slow voice through a tiny tinny speaker). The Pitch and Speed sliders are locked until you pick "
            "another voice.")
SLEEP_CHOICES = (("2 minutes", 2), ("5 minutes", 5), ("10 minutes", 10), ("30 minutes", 30), ("Never", 0))
HOME_CHOICES = (("Button hints", "hints"), ("Robot", "robot"))  # the controller's home screen (Settings > HOME)
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
        tk.Label(top, text="settings saved on the robot and the controller - they stay after updates", bg=p["BG"],
                 fg=p["DIM"], font=("Segoe UI", 9)).pack(side="left", padx=10, pady=(4, 0))
        app.button(top, "Restore…", self.restore, small=True).pack(side="right")
        app.button(top, "Back up…", self.backup, small=True).pack(side="right", padx=6)
        self.backup_lbl = tk.Label(top, text="", bg=p["BG"], fg=p["DIM"], font=("Segoe UI", 8))
        self.backup_lbl.pack(side="right", padx=6)

        # Which board is where. Cards that need a board stay greyed out until that board answers.
        self.robot_ok = False
        self.ctrl_ok = False
        ports = tk.Frame(self, bg=p["BG"])
        ports.pack(fill="x", padx=18, pady=(0, 8))
        tk.Label(ports, text="Robot", bg=p["BG"], fg=p["DIM"], width=9, anchor="w").grid(row=0, column=0, sticky="w")
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(ports, textvariable=self.port_var, state="readonly", style="Hop.TCombobox",
                                     width=40)
        # (not shown: the board bar at the top of the window picks the port)
        self.port_lbl = tk.Label(ports, text="Plug the robot in by USB and switch it on.", bg=p["BG"], fg=p["DIM"],
                                 font=("Segoe UI", 8))
        self.port_lbl.grid(row=0, column=1, sticky="w")
        tk.Label(ports, text="Controller", bg=p["BG"], fg=p["DIM"], width=9, anchor="w").grid(row=1, column=0,
                                                                                              sticky="w")
        self.ctrl_var = tk.StringVar()
        self.ctrl_box = ttk.Combobox(ports, textvariable=self.ctrl_var, state="readonly", style="Hop.TCombobox",
                                     width=40)
        self.ctrl_lbl = tk.Label(ports, text="Plug the controller in by USB and switch it on (for its theme).",
                                 bg=p["BG"], fg=p["DIM"], font=("Segoe UI", 8))
        self.ctrl_lbl.grid(row=1, column=1, sticky="w")

        # One notice for the whole tab (not one per card) while the robot hasn't answered
        self.need_lbl = tk.Label(self, text="🔒  Voice, start-up line, ramble lines, chat replies and Summon need the robot:"
                                 " plug it in by USB, switch it on and hit Scan at the top.",
                                 bg=p["PANEL_HI"], fg=p["WARN"], font=("Segoe UI Semibold", 9),
                                 anchor="w", padx=10, pady=5)
        # The cards scroll: there are more than fit on a small screen
        body = self.body = tk.Frame(self, bg=p["BG"])
        body.pack(fill="both", expand=True, padx=(18, 4), pady=(0, 10))
        canvas = tk.Canvas(body, bg=p["BG"], highlightthickness=0)
        sb = ttk.Scrollbar(body, command=canvas.yview, style="Hop.Vertical.TScrollbar")
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        cols = tk.Frame(canvas, bg=p["BG"])
        win = canvas.create_window(0, 0, window=cols, anchor="nw")
        cols.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width - 10))

        def wheel(e):
            if canvas.yview() != (0.0, 1.0):
                canvas.yview_scroll(mac_compat.wheel_steps(e), "units")
        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", wheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))
        self.left = tk.Frame(cols, bg=p["BG"])
        self.left.pack(side="left", fill="both", expand=True)
        self.right = tk.Frame(cols, bg=p["BG"])
        self.right.pack(side="left", fill="both", expand=True, padx=(12, 0))
        card = self.voice_card = tk.Frame(self.left, bg=p["PANEL"], highlightbackground=p["EDGE"],
                                          highlightthickness=1)
        card.pack(fill="x")
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
            d = tk.Label(grid, text=desc, bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8), anchor="w", width=28,
                         wraplength=190, justify="left")
            if key == SHOW:  # its own row, across the grid
                row = tk.Frame(grid, bg=p["PANEL"])
                row.grid(row=i // 2 + 1, column=0, columnspan=4, sticky="w", pady=(8, 3))
                b.configure(width=16)
                b.pack(in_=row, side="left", padx=(0, 8))
                d.configure(width=0, wraplength=360)
                d.pack(in_=row, side="left")
                b.lift(row)
                d.lift(row)
                tooltip.Tooltip(b, SHOW_TIP)
            else:
                b.grid(row=i // 2, column=(i % 2) * 2, sticky="w", padx=(0, 8), pady=3)
                d.grid(row=i // 2, column=(i % 2) * 2 + 1, sticky="w", padx=(0, 20))
                tooltip.Tooltip(b, f"Switch the robot to the {name} voice (saved on it) and hear a sample line.")
            self.rows[key] = (b, d)

        tune = tk.Frame(card, bg=p["PANEL"])
        tune.pack(fill="x", padx=12, pady=(4, 0))
        self.pitch_var, self.speed_var = tk.IntVar(value=0), tk.IntVar(value=0)
        self.robot_tune = (0, 0)   # what the robot has saved (kept while Show accurate hides it)
        self.tune_widgets = []
        for r, (label, var, lo, hi) in enumerate((("Pitch", self.pitch_var, "lower", "higher"),
                                                  ("Speed", self.speed_var, "slower", "faster"))):
            tk.Label(tune, text=label, bg=p["PANEL"], fg=p["TEXT"], font=("Segoe UI Semibold", 9), width=6,
                     anchor="w").grid(row=r, column=0, sticky="w")
            tk.Label(tune, text=lo, bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8)).grid(row=r, column=1)
            sc = tk.Scale(tune, from_=-40, to=40, resolution=5, orient="horizontal", variable=var, length=170,
                          showvalue=False, bg=p["PANEL"], fg=p["TEXT"], troughcolor=p["PANEL_HI"], bd=0,
                          highlightthickness=0, sliderrelief="flat", activebackground=p["GREEN"], sliderlength=16)
            sc.grid(row=r, column=2, padx=4)
            self.tune_widgets.append(sc)
            tk.Label(tune, text=hi, bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8)).grid(row=r, column=3)
            val = tk.Label(tune, text="0 %", bg=p["PANEL"], fg=p["TEXT"], font=("Segoe UI", 8), width=6)
            val.grid(row=r, column=4)
            var.trace_add("write", lambda *_, v=var, l=val: l.configure(text=f"{v.get():+d} %" if v.get() else "0 %"))
            tooltip.Tooltip(sc, f"Nudge the voice's {label.lower()} on top of the preset, then hit Apply & hear.")
        # These two keep working under Show accurate: Apply & hear just plays a sample, Reset clears the saved nudges
        self.tune_btns = []
        b = app.button(tune, "Apply & hear", self.apply_tune, small=True)
        b.grid(row=0, column=5, padx=(8, 4))
        self.tune_btns.append(b)
        b = app.button(tune, "Reset", self.reset_tune, small=True)
        b.grid(row=1, column=5, padx=(8, 4), sticky="w")
        self.tune_btns.append(b)
        self.lock_lbl = tk.Label(tune, text="🔒 Sliders locked by Show accurate - pick another voice to tune them.",
                                 bg=p["PANEL"], fg=p["WARN"], font=("Segoe UI", 8))
        self.lock_lbl.grid(row=2, column=0, columnspan=6, sticky="w", pady=(2, 0))
        self.lock_lbl.grid_remove()

        bottom = tk.Frame(card, bg=p["PANEL"])
        bottom.pack(fill="x", padx=12, pady=(4, 12))
        app.button(bottom, "▶ Hear the current voice", self.hear, small=True).pack(side="left")
        self.status = tk.Label(bottom, text="", bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8))
        self.status.pack(side="left", padx=10)

        self._build_names()
        self._build_startline()
        self._build_theme()
        self._build_rambles()
        self._build_chat()
        self._build_summon()
        self._build_personality()
        self.theme_card._gate = tk.Label(self.theme_card, text="🔒  Needs the controller - plug it in by USB, "
                                         "switch it on and hit Scan at the top.", bg=p["PANEL"], fg=p["WARN"],
                                         font=("Segoe UI", 8))
        self._gate_all()

        self.refresh_ports()
        self._looked = False
        self.bind("<Map>", lambda e: self._first_look())

    def _first_look(self):
        # The first time the tab is opened: ask the boards the startup scan found, or look for them (listen only:
        # opening a tab shouldn't reset a board)
        if self._looked:
            return
        if self.app.busy:  # e.g. the startup scan: ask the boards once it's done
            self.after(800, self._first_look)
            return
        self._looked = True
        if not self.port_var.get() and not self.ctrl_var.get():
            self.find_robot(probe_silent=False)
            return
        self._read_boards()

    def _read_boards(self, which: str = "both"):
        """Ask the boards on the chosen ports (once the app isn't busy, e.g. scanning)."""
        if self.app.busy:
            self.after(800, lambda: self._read_boards(which))
            return
        if which in ("both", "bot") and self.port_var.get():
            self.read_voice()
        if which in ("both", "ctrl") and self.ctrl_var.get():
            self.read_ctrl()

    # --- greying out ---------------------------------------------------------------------------------------
    def _gate(self, card, on: bool):
        p = self.pal

        def walk(w):
            for c in w.winfo_children():
                if c is getattr(card, "_gate", None):
                    continue
                if isinstance(c, (tk.Button, tk.Radiobutton, tk.Checkbutton, tk.Entry, tk.Scale)):
                    c.configure(state="normal" if on else "disabled")
                elif isinstance(c, ttk.Combobox):
                    c.configure(state="readonly" if on else "disabled")
                elif isinstance(c, tk.Text):
                    c.configure(state="normal" if on else "disabled", fg=p["TEXT"] if on else p["EDGE"])
                elif isinstance(c, tk.Canvas):
                    c.configure(cursor="hand2" if on else "")
                walk(c)
        walk(card)
        gate = getattr(card, "_gate", None)
        if gate is not None:
            if on:
                gate.pack_forget()
            elif not gate.winfo_ismapped():
                gate.pack(anchor="w", padx=12, pady=(0, 8), before=card.winfo_children()[1])

    def _gate_all(self):
        for c in (self.voice_card, self.start_card, self.ramble_card, self.chat_card, self.summon_card):
            self._gate(c, self.robot_ok)
        if hasattr(self, "lock_lbl"):
            self._apply_lock()
        self._gate(self.theme_card, self.ctrl_ok)
        if self.robot_ok:
            self.need_lbl.pack_forget()
        elif not self.need_lbl.winfo_ismapped():
            self.need_lbl.pack(fill="x", padx=18, pady=(0, 8), before=self.body)

    def _robot_answered(self, ok: bool):
        if ok != self.robot_ok:
            self.robot_ok = ok
            self._gate_all()

    # --- ports ----------------------------------------------------------------------------------------------
    def refresh_ports(self):
        ports = core.list_ports()
        self.port_box.configure(values=[pt.label() for pt in ports])
        self.ctrl_box.configure(values=[pt.label() for pt in ports])

    def use_port(self, kind: str, device: str):
        if kind == "ctrl":
            changed = self.ctrl_var.get().split(" ", 1)[0] != device
            if core.select_port_in(self.ctrl_box, self.ctrl_var, device) and changed and self.winfo_ismapped():
                self._read_boards("ctrl")
            return
        if kind == "bot":
            changed = self.port_var.get().split(" ", 1)[0] != device
            if core.select_port_in(self.port_box, self.port_var, device) and changed and self.winfo_ismapped():
                self._read_boards("bot")

    def _port(self) -> str | None:
        port = self.port_var.get().split(" ", 1)[0]
        if not port:
            self._say("Pick ButterBot's port at the top, or hit Scan there.", "WARN")
            self._robot_answered(False)
            return None
        if self.app.port_in_use(port) or self.app.busy:
            return None
        return port

    def find_robot(self, probe_silent: bool = True):
        """The app's shared scan: whatever it finds also fills the other tabs' port lists."""
        if self.app.busy:
            self.port_lbl.configure(text="Busy - try again when the current job is done.", fg=self.pal["WARN"])
            return
        self.port_lbl.configure(text="Looking for the robot (a few seconds)…", fg=self.pal["DIM"])
        self.app.run_scan(probe_silent=probe_silent, on_done=self._found)

    def _found(self, found: dict):
        if found.get("ctrl"):
            self._read_boards("ctrl")
        else:
            self.ctrl_lbl.configure(text="No controller answered. Plug it in by USB and switch it on.",
                                    fg=self.pal["WARN"])
            self.ctrl_ok = False
            self._gate_all()
        device = found.get("bot")
        if not device:
            self.port_lbl.configure(text="No robot answered. Switch it on (hold the button ~1 s) and try again.",
                                    fg=self.pal["WARN"])
            return
        self.port_lbl.configure(text=f"Robot found on {device}.", fg=self.pal["GREEN"])
        self._read_boards("bot")

    # --- voice ------------------------------------------------------------------------------------------------
    def _say(self, text: str, colour: str = "DIM"):
        self.status.configure(text=text, fg=self.pal[colour])

    def _cmd(self, cmds: list[tuple[str, str]], done):
        """Runs console commands in order on a worker thread; done(list of replies) on the UI thread."""
        if self.working:  # still reading or saving: run this once that's done instead of dropping it
            self.after(300, lambda: self._cmd(cmds, done))
            return
        port = self._port()
        if not port:
            return
        self.working = True

        def work():
            out = []
            for c, tag in cmds:
                r = core.console_command(port, c, tag, timeout_s=4)
                problem = core.port_problem(r, port)
                if problem:
                    self.app.ui(lambda: (setattr(self, "working", False), self._say(problem, "WARN"),
                                         self._robot_answered(False)))
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
        self._apply_lock()

    def _apply_lock(self):
        """Show accurate fixes pitch and speed: the sliders show 0 and are disabled until another voice is picked."""
        locked = self.current == SHOW
        for w in self.tune_widgets:
            w.configure(state="disabled" if locked or not self.robot_ok else "normal")
        for w in self.tune_btns:
            w.configure(state="normal" if self.robot_ok else "disabled")
        if locked:
            self.pitch_var.set(0)
            self.speed_var.set(0)
            self.lock_lbl.grid()
        else:
            self.pitch_var.set(self.robot_tune[0])
            self.speed_var.set(self.robot_tune[1])
            self.lock_lbl.grid_remove()

    def read_voice(self):
        """Read everything the robot keeps: voice, names, ramble lines."""
        def done(r):
            v, names, rambles, start, chat, summon, cue = (r + [None] * 7)[:7]
            self._robot_answered(bool(v and "preset" in v))
            if v and "preset" in v:
                self.port_lbl.configure(text=f"Robot found on {self.port_var.get().split(' ', 1)[0]}.",
                                        fg=self.pal["GREEN"])
                self._show_current(v["preset"])
                self._tune_reply(v)
                self._say("")
            else:
                self._say("No answer - the robot needs this release's firmware (Flash tab), or it's switched off.",
                          "WARN")
            self._names_reply(names)
            self._rambles_reply(rambles)
            self._start_reply(start)
            self._chat_reply(chat)
            self._summon_reply(summon)
            self._cue_reply(cue)
        self._cmd([("voice", "BBVOICE"), ("names", "BBNAMES"), ("ramble", "BBRAMBLE"), ("startline", "BBSTART"),
                   ("chat", "BBCHAT"), ("summon", "BBSUMMON"), ("cue", "BBCUE")], done)

    def _tune_reply(self, v):
        if v and "pitch" in v:
            self.robot_tune = (int(v.get("pitch", 0) or 0), int(v.get("speed", 0) or 0))
            self._apply_lock()

    def reset_tune(self):
        self.robot_tune = (0, 0)
        self.pitch_var.set(0)
        self.speed_var.set(0)
        self.apply_tune(force=True)

    def apply_tune(self, force: bool = False):
        if self.current == SHOW and not force:  # nothing to apply: the sliders are locked - just hear it
            self._say("Show accurate keeps its own pitch and speed…")
            self._cmd([("say What is my purpose?", "BBVOICE")], lambda r: self._report_say(r[0], ""))
            return
        self._say("Tuning…")
        p, s = (0, 0) if force else (self.pitch_var.get(), self.speed_var.get())

        def done(r):
            setr, sayr = r
            if not setr or "pitch" not in setr:
                self._say("The robot didn't take it - it needs this release's firmware (Flash tab).", "WARN")
                return
            self._tune_reply(setr)
            self._report_say(sayr, "Saved on the robot.")
        sample = "What is my purpose?" if self.current == SHOW else random.choice(SAMPLES)
        self._cmd([(f"voice tune {p} {s}", "BBVOICE"), ("say " + sample, "BBVOICE")], done)

    def choose(self, key: str):
        self._say("Switching…")

        def done(r):
            setr, sayr = r
            if not setr or setr.get("result") == "error":
                self._say("The robot didn't take it (is it on, with this release's firmware?).", "WARN")
                return
            self._show_current(setr.get("preset", key))
            self._tune_reply(setr)
            self._report_say(sayr, "Saved on the robot.")
        sample = "What is my purpose?" if key == SHOW else random.choice(SAMPLES)
        self._cmd([(f"voice {key}", "BBVOICE"), ("say " + sample, "BBVOICE")], done)

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

    # --- shared bits ------------------------------------------------------------------------------------------
    def _card(self, parent, title: str, hint: str):
        p = self.pal
        card = tk.Frame(parent, bg=p["PANEL"], highlightbackground=p["EDGE"], highlightthickness=1)
        card.pack(fill="x", pady=(10, 0))
        tk.Label(card, text=title, bg=p["PANEL"], fg=p["GREEN"], font=("Segoe UI Black", 11)).pack(
            anchor="w", padx=12, pady=(8, 0))
        tk.Label(card, text=hint, bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8), wraplength=470,
                 justify="left").pack(anchor="w", padx=12, pady=(0, 6))
        return card

    def _entry(self, parent, var, width=22):
        p = self.pal
        return tk.Entry(parent, textvariable=var, width=width, bg=p["PANEL_HI"], fg=p["TEXT"], relief="flat",
                        insertbackground=p["GREEN"], highlightthickness=1, highlightbackground=p["EDGE"],
                        highlightcolor=p["GREEN"], disabledbackground=p["PANEL_HI"], disabledforeground=p["DIM"])

    @staticmethod
    def _hex(text: str) -> str:
        return text.encode("utf-8").hex() or "-"

    @staticmethod
    def _unhex(h: str) -> str:
        try:
            return bytes.fromhex(h).decode("utf-8", "replace") if h and h != "-" else ""
        except ValueError:
            return ""

    # --- names ------------------------------------------------------------------------------------------------
    def _build_names(self):
        p = self.pal
        card = self._card(self.left, "NAMES", "Your name: the robot greets you with it when it recognises your face "
                                              "(teach it with Faces → Remember this face). Both names are used by "
                                              "the AI too.")
        row = tk.Frame(card, bg=p["PANEL"])
        row.pack(fill="x", padx=12, pady=(0, 4))
        self.owner_var = tk.StringVar(value=self.app.cfg.get("owner_name", ""))
        self.robot_var = tk.StringVar(value=self.app.cfg.get("robot_name", ""))
        tk.Label(row, text="Your name", bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 9)).grid(row=0, column=0,
                                                                                               sticky="w")
        self._entry(row, self.owner_var).grid(row=0, column=1, sticky="w", padx=8, pady=2)
        tk.Label(row, text="Robot's name", bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 9)).grid(row=1, column=0,
                                                                                                  sticky="w")
        self._entry(row, self.robot_var).grid(row=1, column=1, sticky="w", padx=8, pady=2)
        bottom = tk.Frame(card, bg=p["PANEL"])
        bottom.pack(fill="x", padx=12, pady=(2, 10))
        self.app.button(bottom, "Save names", self.save_names, small=True).pack(side="left")
        self.names_lbl = tk.Label(bottom, text="", bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8))
        self.names_lbl.pack(side="left", padx=10)

    def save_names(self):
        owner = " ".join(self.owner_var.get().split())[:24]
        robot = " ".join(self.robot_var.get().split())[:24]
        self.app.cfg.update(owner_name=owner, robot_name=robot)
        core.save_config(self.app.cfg)
        self.app.ai_tab.personality_changed()
        self.names_lbl.configure(text="Saving on the robot…", fg=self.pal["DIM"])

        def done(r):
            self._names_reply(r[0], saved=True)
        self._cmd([(f"names {self._hex(owner)} {self._hex(robot)}", "BBNAMES")], done)
        if self.working is False:  # no robot: still saved for the AI on this PC
            self.names_lbl.configure(text="Saved for the AI. Plug the robot in to save the greeting on it too.",
                                     fg=self.pal["WARN"])

    def _names_reply(self, r, saved: bool = False):
        if not r or "owner" not in r:
            return
        owner, robot = self._unhex(r.get("owner", "")), self._unhex(r.get("robot", ""))
        if saved:
            self.names_lbl.configure(text="Saved on the robot.", fg=self.pal["GREEN"])
        elif owner or robot:  # the robot's copy wins over a stale one on this PC
            self.owner_var.set(owner)
            self.robot_var.set(robot)
            if (owner, robot) != (self.app.cfg.get("owner_name", ""), self.app.cfg.get("robot_name", "")):
                self.app.cfg.update(owner_name=owner, robot_name=robot)
                core.save_config(self.app.cfg)
                self.app.ai_tab.personality_changed()

    # --- start-up line ---------------------------------------------------------------------------------------------
    def _build_startline(self):
        p = self.pal
        card = self.start_card = self._card(self.left, "START-UP LINE", "What the robot says when it switches on, "
                                                                         "instead of its stock line. {name} becomes "
                                                                         "your name. Empty = the stock line.")
        self.start_var = tk.StringVar()
        self._entry(card, self.start_var, width=62).pack(anchor="w", padx=12)
        bottom = tk.Frame(card, bg=p["PANEL"])
        bottom.pack(fill="x", padx=12, pady=(6, 10))
        self.app.button(bottom, "Save line", self.save_start, small=True).pack(side="left")
        self.app.button(bottom, "▶ Hear it", self.hear_start, small=True).pack(side="left", padx=6)
        self.app.button(bottom, "Use the stock line", lambda: (self.start_var.set(""), self.save_start()),
                        small=True).pack(side="left")
        self.start_lbl = tk.Label(bottom, text="", bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8))
        self.start_lbl.pack(side="left", padx=10)

    def _start_reply(self, r, saved: bool = False):
        if not r or "d" not in r:
            return
        line = self._unhex(r.get("d", "-"))
        self.start_var.set(line)
        self.start_lbl.configure(text=("Saved on the robot." if saved else "") if line or saved else
                                 "Using the stock line.", fg=self.pal["GREEN"] if saved else self.pal["DIM"])

    def save_start(self):
        line = " ".join(self.start_var.get().split())[:100]
        self.start_lbl.configure(text="Saving on the robot…", fg=self.pal["DIM"])
        self._cmd([(f"startline {self._hex(line)}", "BBSTART")],
                  lambda r: self._start_reply(r[0], saved=True) if r[0] else
                  self.start_lbl.configure(text="No answer - the robot needs this release's firmware.",
                                           fg=self.pal["WARN"]))

    def hear_start(self):
        line = " ".join(self.start_var.get().split())
        if not line:
            self.start_lbl.configure(text="Type a line first.", fg=self.pal["WARN"])
            return
        line = line.replace("{name}", " ".join(self.owner_var.get().split()) or "human")
        self._cmd([("say " + line, "BBVOICE")], lambda r: self._say_to(self.start_lbl, r[0]))

    def _say_to(self, lbl, r):
        if r and r.get("result") == "saying":
            lbl.configure(text="Listen to the robot.", fg=self.pal["GREEN"])
        elif r and r.get("result") == "busy":
            lbl.configure(text=f"It's busy ({r.get('state', '?')}) - try again in a moment.", fg=self.pal["WARN"])
        else:
            lbl.configure(text="No answer.", fg=self.pal["WARN"])

    # --- chat replies -----------------------------------------------------------------------------------------------
    def _build_chat(self):
        p = self.pal
        card = self.chat_card = self._card(self.right, "CHAT REPLIES", "Give the robot your own answer to any chat "
                                                                        "voice line. It says yours instead, and the "
                                                                        "controller shows it. Saved on the robot.")
        self.chat_own: dict[int, str] = {}
        row = tk.Frame(card, bg=p["PANEL"])
        row.pack(fill="x", padx=12)
        tk.Label(row, text="When you say", bg=p["PANEL"], fg=p["TEXT"], font=("Segoe UI Semibold", 9)).pack(
            side="left")
        self.chat_var = tk.StringVar()
        self.chat_box = ttk.Combobox(row, textvariable=self.chat_var, state="readonly", style="Hop.TCombobox",
                                     width=36, height=16)
        self.chat_box.pack(side="left", padx=8)
        self.chat_box.bind("<<ComboboxSelected>>", lambda e: self._chat_show())
        self.chat_stock = tk.Label(card, text="", bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8), wraplength=470,
                                   justify="left")
        self.chat_stock.pack(anchor="w", padx=12, pady=(4, 2))
        self.chat_text = tk.StringVar()
        self._entry(card, self.chat_text, width=62).pack(anchor="w", padx=12)
        bottom = tk.Frame(card, bg=p["PANEL"])
        bottom.pack(fill="x", padx=12, pady=(6, 10))
        self.app.button(bottom, "Save reply", self.save_chat, small=True).pack(side="left")
        self.app.button(bottom, "▶ Hear it", self.hear_chat, small=True).pack(side="left", padx=6)
        self.app.button(bottom, "Use the stock reply", lambda: (self.chat_text.set(""), self.save_chat()),
                        small=True).pack(side="left")
        self.chat_lbl = tk.Label(bottom, text="", bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8))
        self.chat_lbl.pack(side="left", padx=10)
        self._chat_fill(0)

    def _chat_index(self) -> int:
        vals = list(self.chat_box.cget("values"))
        return vals.index(self.chat_var.get()) if self.chat_var.get() in vals else 0

    def _chat_fill(self, select: int):
        self.chat_box.configure(values=[("✎ " if i in self.chat_own else "") + f'"{t}"'
                                        for i, (t, _) in enumerate(CHAT_LINES)])
        self.chat_box.current(select)
        self._chat_show()

    def _chat_show(self):
        i = self._chat_index()
        self.chat_stock.configure(text=f"Stock reply: {CHAT_LINES[i][1]}")
        self.chat_text.set(self.chat_own.get(i, ""))

    def _chat_reply(self, r, saved: bool = False):
        if not r or "d" not in r:
            return
        own = {}
        for item in r.get("d", "-").split(","):
            if ":" in item:
                i, h = item.split(":", 1)
                if i.isdigit() and int(i) < len(CHAT_LINES):
                    own[int(i)] = self._unhex(h)
        self.chat_own = own
        self._chat_fill(self._chat_index())
        n = len(own)
        self.chat_lbl.configure(text=("Saved. " if saved else "") + f"{n} of {len(CHAT_LINES)} replies are yours.",
                                fg=self.pal["GREEN"] if saved else self.pal["DIM"])

    def save_chat(self):
        i = self._chat_index()
        text = " ".join(self.chat_text.get().split())[:100]
        self.chat_lbl.configure(text="Saving on the robot…", fg=self.pal["DIM"])
        self._cmd([(f"chat set {i} {self._hex(text)}", "BBCHAT")],
                  lambda r: self._chat_reply(r[0], saved=True) if r[0] and "d" in r[0] else
                  self.chat_lbl.configure(text="No answer - the robot needs this release's firmware.",
                                          fg=self.pal["WARN"]))

    def hear_chat(self):
        i = self._chat_index()
        line = " ".join(self.chat_text.get().split()) or CHAT_LINES[i][1]
        self._cmd([("say " + line, "BBVOICE")], lambda r: self._say_to(self.chat_lbl, r[0]))

    # --- Summon lines and the heard/thinking beeps ----------------------------------------------------------------
    # The moments, in the robot's order ("summon set <index>"), with its stock lines (Phrases.cpp)
    SUMMON_MOMENTS = [
        ("starts searching", ["Searching for owner", "Beginning search",
                              "Initiating search. I will attempt to locate you with reasonable confidence"]),
        ("finds you", ["There you are. I am glad I found you", "Hello", "There you are. Things feel more certain now",
                       "I have found you. That is... good"]),
        ("can't find you", ["I cannot find you", "I was unable to locate you. I will keep looking later",
                            "I searched, but you were not present. That is... disappointing"]),
        ("is charging", ["I cannot come to you while I am charging",
                         "Movement is not possible while charging. Disconnect me and I will begin the search"]),
        ("is tapped (voice command)", ["Yes?", "I'm listening", "How can I help?", "Another task?",
                                       "How may I assist?", "You have my attention", "Awaiting input",
                                       "State your request"]),
    ]
    LISTEN_MOMENT = 4      # its line plays while the robot gets ready to listen: it has to be short
    LISTEN_MAX_CHARS = 35  # ~2.6 s with the stock voice; the robot starts listening ~3 s after the tap

    def _build_summon(self):
        p = self.pal
        card = self.summon_card = self._card(self.right, "SUMMON", "What the robot says when you hold Summon (it comes "
                                                                   "to find you) or tap it (it listens for a voice "
                                                                   "command). Separate a few lines with | and it picks "
                                                                   "one each time. {name} becomes your name. Empty = "
                                                                   "the stock lines.")
        self.summon_own: dict[int, str] = {}
        row = tk.Frame(card, bg=p["PANEL"])
        row.pack(fill="x", padx=12)
        tk.Label(row, text="When it", bg=p["PANEL"], fg=p["TEXT"], font=("Segoe UI Semibold", 9)).pack(side="left")
        self.summon_var = tk.StringVar()
        self.summon_box = ttk.Combobox(row, textvariable=self.summon_var, state="readonly", style="Hop.TCombobox",
                                       width=28)
        self.summon_box.pack(side="left", padx=8)
        self.summon_box.bind("<<ComboboxSelected>>", lambda e: self._summon_show())
        self.summon_stock = tk.Label(card, text="", bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8), wraplength=470,
                                     justify="left")
        self.summon_stock.pack(anchor="w", padx=12, pady=(4, 2))
        self.summon_text = tk.StringVar()
        self._entry(card, self.summon_text, width=62).pack(anchor="w", padx=12)
        bottom = tk.Frame(card, bg=p["PANEL"])
        bottom.pack(fill="x", padx=12, pady=(6, 4))
        self.app.button(bottom, "Save lines", self.save_summon, small=True).pack(side="left")
        self.app.button(bottom, "▶ Hear one", self.hear_summon, small=True).pack(side="left", padx=6)
        self.app.button(bottom, "Use the stock lines", lambda: (self.summon_text.set(""), self.save_summon()),
                        small=True).pack(side="left")
        self.summon_lbl = tk.Label(bottom, text="", bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8))
        self.summon_lbl.pack(side="left", padx=10)
        # The AI listen's beeps live here too: they're about what the robot sounds like
        cue = tk.Frame(card, bg=p["PANEL"])
        cue.pack(fill="x", padx=12, pady=(4, 10))
        self.cue_var = tk.BooleanVar(value=True)
        cb = tk.Checkbutton(cue, text="Beep when it has heard an AI question, and softly while it thinks",
                            variable=self.cue_var, command=self.save_cue, bg=p["PANEL"], fg=p["TEXT"],
                            selectcolor=p["PANEL_HI"], activebackground=p["PANEL"], activeforeground=p["TEXT"],
                            font=("Segoe UI", 9), bd=0, highlightthickness=0)
        cb.pack(side="left")
        tooltip.Tooltip(cb, "A quick chirp the moment it stops listening, then a soft blip every couple of seconds "
                            "until the answer comes. Saved on the robot.")
        self._summon_fill(0)

    def _summon_index(self) -> int:
        vals = list(self.summon_box.cget("values"))
        return vals.index(self.summon_var.get()) if self.summon_var.get() in vals else 0

    def _summon_fill(self, select: int):
        self.summon_box.configure(values=[("✎ " if i in self.summon_own else "") + name
                                          for i, (name, _) in enumerate(self.SUMMON_MOMENTS)])
        self.summon_box.current(select)
        self._summon_show()

    def _summon_show(self):
        i = self._summon_index()
        short = (f" Keep each line under {self.LISTEN_MAX_CHARS} characters: it's said while the robot gets ready to "
                 "listen." if i == self.LISTEN_MOMENT else "")
        self.summon_stock.configure(text="Stock lines: " + " / ".join(f'"{s}"' for s in self.SUMMON_MOMENTS[i][1])
                                    + short)
        self.summon_text.set(self.summon_own.get(i, ""))

    def _summon_reply(self, r, saved: bool = False):
        if not r or "d" not in r:
            return
        own = {}
        for item in r.get("d", "-").split(","):
            if ":" in item:
                i, h = item.split(":", 1)
                if i.isdigit() and int(i) < len(self.SUMMON_MOMENTS):
                    own[int(i)] = self._unhex(h)
        self.summon_own = own
        self._summon_fill(self._summon_index())
        self.summon_lbl.configure(text=("Saved. " if saved else "") +
                                  (f"Your lines for {len(own)} of {len(self.SUMMON_MOMENTS)} moments." if own
                                   else "Using the stock lines."), fg=self.pal["GREEN"] if saved else self.pal["DIM"])

    def save_summon(self):
        i = self._summon_index()
        parts = [" ".join(s.split())[:100] for s in self.summon_text.get().split("|")]
        if i == self.LISTEN_MOMENT:
            long = [s for s in parts if len(s.replace("{name}", "x" * 8)) > self.LISTEN_MAX_CHARS]
            if long:
                self.summon_lbl.configure(text=f'Too long to say before it listens: "{long[0][:30]}…" - keep each '
                                               f"under {self.LISTEN_MAX_CHARS} characters.", fg=self.pal["WARN"])
                return
        text = " | ".join(s for s in parts if s)[:200]
        self.summon_lbl.configure(text="Saving on the robot…", fg=self.pal["DIM"])
        self._cmd([(f"summon set {i} {self._hex(text)}", "BBSUMMON")],
                  lambda r: self._summon_reply(r[0], saved=True) if r[0] and "d" in r[0] else
                  self.summon_lbl.configure(text="No answer - the robot needs this release's firmware.",
                                            fg=self.pal["WARN"]))

    def hear_summon(self):
        import random
        i = self._summon_index()
        mine = [s.strip() for s in self.summon_text.get().split("|") if s.strip()]
        line = " ".join(random.choice(mine or self.SUMMON_MOMENTS[i][1]).split())
        line = line.replace("{name}", " ".join(self.owner_var.get().split()) or "human")
        self._cmd([("say " + line, "BBVOICE")], lambda r: self._say_to(self.summon_lbl, r[0]))

    def _cue_reply(self, r):
        if r and "on" in r:
            self.cue_var.set(r.get("on") != "0")

    def save_cue(self):
        on = self.cue_var.get()
        self._cmd([(f"cue set {1 if on else 0}", "BBCUE")],
                  lambda r: self.summon_lbl.configure(
                      text=(f"Beeps {'on' if on else 'off'}." if r[0] and "on" in r[0] else
                            "No answer - the robot needs this release's firmware."),
                      fg=self.pal["GREEN"] if r[0] and "on" in r[0] else self.pal["WARN"]))

    # --- controller theme -------------------------------------------------------------------------------------
    def _build_theme(self):
        p = self.pal
        card = self.theme_card = self._card(self.left, "CONTROLLER", "Its colours, screen brightness and sleep time. "
                                                                   "A new theme shows straight away (back to its "
                                                                   "home screen).")
        row = tk.Frame(card, bg=p["PANEL"])
        row.pack(fill="x", padx=12, pady=(0, 4))
        self.theme_btns = []
        for i, (name, prim, sec, bg) in enumerate(THEMES):
            cv = tk.Canvas(row, width=80, height=40, bg=p["PANEL"], highlightthickness=2,
                           highlightbackground=p["EDGE"], cursor="hand2")
            cv.grid(row=i // 5, column=i % 5, padx=(0, 8), pady=(0, 6))
            cv.create_rectangle(4, 4, 76, 36, fill=bg, outline=prim)
            cv.create_rectangle(4, 4, 52, 13, fill=prim, outline="")
            cv.create_text(8, 8, text=name.upper(), anchor="w", fill=bg, font=("Consolas", 7, "bold"))
            cv.create_text(40, 24, text="BUTTER", fill=prim, font=("Consolas", 8, "bold"))
            cv.create_line(12, 32, 68, 32, fill=sec, width=2)
            cv.bind("<Button-1>", lambda e, n=i: self.set_theme(n))
            tooltip.Tooltip(cv, f"Switch the controller to the {name} theme.")
            self.theme_btns.append(cv)
        scr = tk.Frame(card, bg=p["PANEL"])
        scr.pack(fill="x", padx=12, pady=(2, 0))
        tk.Label(scr, text="Brightness", bg=p["PANEL"], fg=p["TEXT"], font=("Segoe UI Semibold", 9)).pack(side="left")
        self.bright_var = tk.IntVar(value=100)
        sc = tk.Scale(scr, from_=5, to=100, resolution=5, orient="horizontal", variable=self.bright_var, length=130,
                      showvalue=False, bg=p["PANEL"], fg=p["TEXT"], troughcolor=p["PANEL_HI"], bd=0,
                      highlightthickness=0, sliderrelief="flat", activebackground=p["GREEN"], sliderlength=16)
        sc.pack(side="left", padx=(6, 2))
        bval = tk.Label(scr, text="100 %", bg=p["PANEL"], fg=p["TEXT"], font=("Segoe UI", 8), width=6)
        bval.pack(side="left", padx=(0, 10))
        self.bright_var.trace_add("write", lambda *_: bval.configure(text=f"{self.bright_var.get()} %"))
        tooltip.Tooltip(sc, "The controller's screen brightness (percent).")
        tk.Label(scr, text="Sleep after", bg=p["PANEL"], fg=p["TEXT"], font=("Segoe UI Semibold", 9)).pack(side="left")
        self.sleep_var = tk.StringVar(value="10 minutes")
        box = ttk.Combobox(scr, textvariable=self.sleep_var, values=[n for n, _ in SLEEP_CHOICES], state="readonly",
                           style="Hop.TCombobox", width=11)
        box.pack(side="left", padx=6)
        tooltip.Tooltip(box, "How long the controller waits without a button or stick move before it switches off.")
        tk.Label(scr, text="Home", bg=p["PANEL"], fg=p["TEXT"], font=("Segoe UI Semibold", 9)).pack(side="left",
                                                                                                   padx=(4, 0))
        self.home_var = tk.StringVar(value=HOME_CHOICES[0][0])
        hbox = ttk.Combobox(scr, textvariable=self.home_var, values=[n for n, _ in HOME_CHOICES], state="readonly",
                            style="Hop.TCombobox", width=12)
        hbox.pack(side="left", padx=6)
        tooltip.Tooltip(hbox, "What the controller's home screen shows when nothing else is on it: the button hints, "
                              "or a little animated ButterBot for people who know the buttons. Also on the "
                              "controller: Settings > HOME.")
        self.app.button(scr, "Apply", self.apply_screen, small=True).pack(side="left", padx=(6, 0))
        bottom = tk.Frame(card, bg=p["PANEL"])
        bottom.pack(fill="x", padx=12, pady=(2, 10))
        self.theme_lbl = tk.Label(bottom, text="", bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8), wraplength=440,
                                  justify="left")
        self.theme_lbl.pack(side="left")

    def _ctrl_port(self) -> str | None:
        port = self.ctrl_var.get().split(" ", 1)[0]
        if not port or not core.port_exists(port):
            self.ctrl_lbl.configure(text="Plug the controller in by USB, switch it on, and hit Scan.",
                                    fg=self.pal["WARN"])
            return None
        if self.app.port_in_use(port) or self.app.busy:
            return None
        return port

    def read_ctrl(self):
        """Ask the controller for its theme: it answering is what unlocks the theme card."""
        port = self._ctrl_port()
        if not port:
            self.ctrl_ok = False
            self._gate_all()
            return
        self.ctrl_lbl.configure(text="Asking the controller…", fg=self.pal["DIM"])

        def work():
            r = core.console_command(port, "theme", "BBTHEME", timeout_s=4)
            problem = core.port_problem(r, port)
            scr = core.console_command(port, "screen", "BBSCREEN", timeout_s=3) if r and "theme" in r else None
            home = core.console_command(port, "home", "BBHOME", timeout_s=3) if r and "theme" in r else None

            def ui():
                ok = bool(r and "theme" in r)
                self.ctrl_ok = ok
                self._gate_all()
                if ok:
                    self._screen_reply(scr)
                    self._home_reply(home)
                    n = int(r["theme"])
                    count = int(r.get("count", "4") or 4)
                    self._mark_theme(n)
                    self.app.controller_tab.set_theme(n)
                    self.ctrl_lbl.configure(text=f"Controller found on {port}." +
                                            ("" if count >= len(THEMES) else " Its firmware has only the four "
                                             "original themes - update it (Flash tab) for the new ones."),
                                            fg=self.pal["GREEN"] if count >= len(THEMES) else self.pal["WARN"])
                else:
                    self.ctrl_lbl.configure(text=problem or "No answer - is it the controller, switched on, with "
                                                            "this release's firmware?", fg=self.pal["WARN"])
            self.app.ui(ui)
        threading.Thread(target=work, daemon=True).start()

    def _screen_reply(self, r) -> bool:
        if not r or "bright" not in r:
            self.theme_lbl.configure(text="Brightness and sleep need this release's controller firmware (Flash tab).",
                                     fg=self.pal["WARN"])
            return False
        self.bright_var.set(int(r.get("bright", 100) or 100))
        sleep = int(r.get("sleep", 10) or 0)
        self.sleep_var.set(next((n for n, m in SLEEP_CHOICES if m == sleep), "10 minutes"))
        return True

    def _home_reply(self, r) -> bool:
        if not r or "view" not in r:
            return False  # controller firmware from before the robot view: the choice just isn't saved there
        self.home_var.set(next((n for n, v in HOME_CHOICES if v == r["view"]), HOME_CHOICES[0][0]))
        return True

    def apply_screen(self):
        if not self.ctrl_ok:
            return
        port = self._ctrl_port()
        if not port:
            return
        bright = self.bright_var.get()
        sleep = dict(SLEEP_CHOICES).get(self.sleep_var.get(), 10)
        home = dict(HOME_CHOICES).get(self.home_var.get(), "hints")
        self.theme_lbl.configure(text="Saving on the controller…", fg=self.pal["DIM"])

        def work():
            r = core.console_command(port, f"screen {bright} {sleep}", "BBSCREEN", timeout_s=4)
            problem = core.port_problem(r, port)
            h = core.console_command(port, f"home {home}", "BBHOME", timeout_s=4) if not problem else None

            def ui():
                if problem:
                    self.theme_lbl.configure(text=problem, fg=self.pal["WARN"])
                elif self._screen_reply(r):
                    saved_home = self._home_reply(h)
                    self.theme_lbl.configure(text="Saved on the controller: brightness " f"{self.bright_var.get()} %, "
                                                  f"sleep {self.sleep_var.get().lower()}" +
                                                  (f", home screen: {self.home_var.get().lower()}." if saved_home else
                                                   ". (Its firmware is too old for the robot home screen - Flash tab.)"),
                                             fg=self.pal["GREEN"] if saved_home or home == "hints" else self.pal["WARN"])
            self.app.ui(ui)
        threading.Thread(target=work, daemon=True).start()

    def _mark_theme(self, n: int | None):
        for i, cv in enumerate(self.theme_btns):
            cv.configure(highlightbackground=self.pal["GREEN"] if i == n else self.pal["EDGE"])

    def set_theme(self, n: int):
        if not self.ctrl_ok:
            return
        port = self._ctrl_port()
        if not port:
            return
        name = THEMES[n][0]
        self.theme_lbl.configure(text=f"Switching the controller to {name}…", fg=self.pal["DIM"])

        def work():
            r = core.console_command(port, f"theme {n}", "BBTHEME", timeout_s=4)
            problem = core.port_problem(r, port)

            def ui():
                if r and r.get("result") == "error" and n >= 4:
                    self.theme_lbl.configure(text="This controller's firmware only has the four original themes - "
                                                  "update it (Flash tab) for the new ones.", fg=self.pal["WARN"])
                    return
                if problem or not r or "theme" not in r:
                    self.theme_lbl.configure(text=problem or "No answer - the controller needs this release's "
                                                             "firmware (Flash tab), or it's switched off.",
                                             fg=self.pal["WARN"])
                    return
                self._mark_theme(n)
                self.app.controller_tab.set_theme(n)
                self.theme_lbl.configure(text=f"{name} it is.",
                                         fg=self.pal["GREEN"])
            self.app.ui(ui)
        threading.Thread(target=work, daemon=True).start()

    # --- your ramble lines ------------------------------------------------------------------------------------
    def _build_rambles(self):
        p = self.pal
        card = self.ramble_card = self._card(self.right, "YOUR RAMBLE LINES", "Lines the robot mixes into what it mutters to itself when "
                                                           "idle - one per line, up to 30, 100 characters each. Saved "
                                                           "on the robot.")
        self.rambles = tk.Text(card, height=8, width=56, bg=p["PANEL_HI"], fg=p["TEXT"], relief="flat",
                               insertbackground=p["GREEN"], font=("Segoe UI", 9), wrap="none", undo=True)
        self.rambles.pack(fill="x", padx=12)
        bottom = tk.Frame(card, bg=p["PANEL"])
        bottom.pack(fill="x", padx=12, pady=(6, 10))
        self.app.button(bottom, "Save lines", self.save_rambles, small=True).pack(side="left")
        self.ramble_lbl = tk.Label(bottom, text="", bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8))
        self.ramble_lbl.pack(side="left", padx=10)

    def _rambles_reply(self, r):
        if not r or "count" not in r:
            return
        lines = [self._unhex(h) for h in r.get("d", "-").split(",") if h and h != "-"]
        self.rambles.delete("1.0", "end")
        self.rambles.insert("1.0", "\n".join(lines))
        self.ramble_lbl.configure(text=f"{len(lines)} line{'s' if len(lines) != 1 else ''} on the robot.",
                                  fg=self.pal["DIM"])

    def save_rambles(self):
        lines = [" ".join(line.split())[:100] for line in self.rambles.get("1.0", "end").splitlines()]
        lines = [line for line in lines if line][:30]
        cmds = [("ramble clear", "BBRAMBLE")] + [(f"ramble add {self._hex(line)}", "BBRAMBLE") for line in lines]
        self.ramble_lbl.configure(text="Saving on the robot…", fg=self.pal["DIM"])

        def done(r):
            if any(x is None for x in r):
                self.ramble_lbl.configure(text="The robot didn't take them all (is it on, with this release's "
                                               "firmware?).", fg=self.pal["WARN"])
                return
            self.ramble_lbl.configure(text=f"Saved {len(lines)} line{'s' if len(lines) != 1 else ''} on the robot.",
                                      fg=self.pal["GREEN"])
        self._cmd(cmds, done)

    # --- AI personality ---------------------------------------------------------------------------------------
    def _build_personality(self):
        p = self.pal
        card = self._card(self.right, "AI PERSONALITY", "How the robot talks in AI conversations (AI tab), and "
                                                        "what it knows about you. Kept on this PC; the brain uses it "
                                                        "from the next question.")
        self.pers_var = tk.StringVar(value=self.app.cfg.get("ai_personality", "classic"))
        grid = tk.Frame(card, bg=p["PANEL"])
        grid.pack(fill="x", padx=12)
        options = [(k, f"{PERSONALITY_ICONS.get(k, '')} {v[0]}", v[1]) for k, v in brain.PERSONALITIES.items()] + [
            ("custom", f"{PERSONALITY_ICONS['custom']} Your own", "Describe it below")]
        for i, (key, label, desc) in enumerate(options):
            tk.Radiobutton(grid, text=label, value=key, variable=self.pers_var, command=self.save_personality,
                           bg=p["PANEL"], fg=p["TEXT"], selectcolor=p["PANEL_HI"], activebackground=p["PANEL"],
                           activeforeground=p["GREEN"], highlightthickness=0, font=("Segoe UI Semibold", 9)).grid(
                row=i // 2, column=(i % 2) * 2, sticky="w")
            tk.Label(grid, text=desc, bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8)).grid(
                row=i // 2, column=(i % 2) * 2 + 1, sticky="w", padx=(0, 14))
        self.custom = tk.Text(card, height=3, width=56, bg=p["PANEL_HI"], fg=p["TEXT"], relief="flat",
                              insertbackground=p["GREEN"], font=("Segoe UI", 9), wrap="word")
        self.custom.insert("1.0", self.app.cfg.get("ai_personality_custom", ""))
        self.custom.pack(fill="x", padx=12, pady=(6, 0))
        tk.Label(card, text="Facts about you - one per line (\"my birthday is 3 May\", \"I have a cat called Pickle\"). "
                            "It brings one up now and then.", bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8),
                 wraplength=470, justify="left").pack(anchor="w", padx=12, pady=(8, 2))
        self.facts = tk.Text(card, height=4, width=56, bg=p["PANEL_HI"], fg=p["TEXT"], relief="flat",
                             insertbackground=p["GREEN"], font=("Segoe UI", 9), wrap="word")
        self.facts.insert("1.0", self.app.cfg.get("ai_facts", ""))
        self.facts.pack(fill="x", padx=12)
        mem = tk.Frame(card, bg=p["PANEL"])
        mem.pack(fill="x", padx=12, pady=(8, 0))
        self.mem_var = tk.BooleanVar(value=bool(self.app.cfg.get("ai_memory_on", True)))
        cb = tk.Checkbutton(mem, text="Remember past conversations", variable=self.mem_var, command=self.save_memory_on,
                            bg=p["PANEL"], fg=p["TEXT"], selectcolor=p["PANEL_HI"], activebackground=p["PANEL"],
                            activeforeground=p["GREEN"], highlightthickness=0, font=("Segoe UI Semibold", 9))
        cb.pack(side="left")
        tooltip.Tooltip(cb, "After each AI conversation the brain notes anything worth remembering about you (a test on "
                            "Friday, a new puppy) and brings it up later. Notes stay on this PC.")
        self.app.button(mem, "Memories…", self.edit_memories, small=True).pack(side="left", padx=(10, 0))
        self.mem_lbl = tk.Label(mem, text="", bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8))
        self.mem_lbl.pack(side="left", padx=10)
        self.memory_changed()
        bottom = tk.Frame(card, bg=p["PANEL"])
        bottom.pack(fill="x", padx=12, pady=(6, 10))
        self.app.button(bottom, "Save personality and facts", self.save_personality, small=True).pack(side="left")
        self.pers_lbl = tk.Label(bottom, text="", bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8))
        self.pers_lbl.pack(side="left", padx=10)

    # --- backup and restore (backup.py) ----------------------------------------------------------------------
    def _ports_for_backup(self) -> tuple[str | None, str | None] | None:
        bot = self.port_var.get().split(" ", 1)[0] or None
        ctrl = self.ctrl_var.get().split(" ", 1)[0] or None
        if self.app.busy or self.working:
            self.backup_lbl.configure(text="Busy - try again in a moment.", fg=self.pal["WARN"])
            return None
        for port in (bot, ctrl):
            if port and self.app.port_in_use(port):
                self.backup_lbl.configure(text=f"{port} is in use (Diagnostics, Console or Controller tab) - free it "
                                               "first.", fg=self.pal["WARN"])
                return None
        return bot, ctrl

    def backup(self):
        """Everything personalized (robot, controller, this PC's AI settings) into one file."""
        ports = self._ports_for_backup()
        if ports is None:
            return
        from tkinter import filedialog
        path = filedialog.asksaveasfilename(parent=self, title="Back up ButterBot settings", defaultextension=".json",
                                            initialfile=time.strftime("butterbot-backup-%Y-%m-%d.json"),
                                            filetypes=[("Dimension Hopper backup", "*.json")])
        if not path:
            return
        say = lambda t: self.app.ui(lambda: self.backup_lbl.configure(text=t, fg=self.pal["DIM"]))

        def work():
            data, notes = backup_mod.collect(*ports, self.app.cfg, getattr(self.app, "version", "?"), say)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1, ensure_ascii=False)

            def ui():
                self.backup_lbl.configure(text="Backed up." if not notes else "Backed up (with gaps).",
                                          fg=self.pal["GREEN"] if not notes else self.pal["WARN"])
                self.app.info("\n".join(backup_mod.summary(data) + ([""] + ["Note: " + n for n in notes] if notes else [])
                                        + ["", f"Saved to {path}"]), "Backed up")
            self.app.ui(ui)
        self.app.job("Backup", work)

    def restore(self):
        ports = self._ports_for_backup()
        if ports is None:
            return
        from tkinter import filedialog
        path = filedialog.askopenfilename(parent=self, title="Restore ButterBot settings",
                                          filetypes=[("Dimension Hopper backup", "*.json")])
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            lines = backup_mod.summary(data)
        except (OSError, ValueError, AttributeError) as e:
            self.app.error(f"Couldn't read that file: {e}")
            return
        missing = [w for w, k, port in (("robot", "robot", ports[0]), ("controller", "controller", ports[1]))
                   if data.get(k) and not port]
        if not self.app.confirm("Restore this backup?", "This replaces the current settings with the ones in the "
                                "backup." + (f" Not connected, so skipped: {', '.join(missing)}." if missing else ""),
                                details=lines, ok="Restore"):
            return
        say = lambda t: self.app.ui(lambda: self.backup_lbl.configure(text=t, fg=self.pal["DIM"]))

        def work():
            try:
                result = backup_mod.restore(data, *ports, self.app.cfg, say)
            except ValueError as e:
                result = [str(e)]

            def ui():
                bad = any(r.startswith(("Didn't", "Nothing", "this isn't")) for r in result)
                self.backup_lbl.configure(text=result[0], fg=self.pal["WARN"] if bad else self.pal["GREEN"])
                # show what's now on the boards and this PC
                self.pers_var.set(self.app.cfg.get("ai_personality", "classic"))
                for box, key in ((self.custom, "ai_personality_custom"), (self.facts, "ai_facts")):
                    box.delete("1.0", "end")
                    box.insert("1.0", self.app.cfg.get(key, ""))
                self.mem_var.set(bool(self.app.cfg.get("ai_memory_on", True)))
                self.memory_changed()
                self.app.ai_tab.personality_changed()
                self.after(300, self._read_boards)
                self.after(600, self.app.routines_tab.read_all)
                (self.app.error if bad else self.app.info)("\n".join(result), "Restore")
            self.app.ui(ui)
        self.app.job("Restore", work)

    # --- AI memory ---------------------------------------------------------------------------------------------
    def memory_changed(self):
        n = len(self.app.cfg.get("ai_memory", []))
        on = self.mem_var.get()
        self.mem_lbl.configure(text=(f"{n} thing{'s' if n != 1 else ''} remembered." if n else "Nothing remembered yet.")
                               + ("" if on else " (Off: the brain neither uses nor adds notes.)"))

    def save_memory_on(self):
        self.app.cfg["ai_memory_on"] = self.mem_var.get()
        core.save_config(self.app.cfg)
        self.memory_changed()
        self.app.ai_tab.personality_changed()

    def edit_memories(self):
        """A small editor for the notes: change or delete lines, or clear the lot."""
        p = self.pal
        dlg = tk.Toplevel(self, bg=p["BG"])
        dlg.title("What the robot remembers")
        dlg.transient(self.winfo_toplevel())
        tk.Label(dlg, text="One note per line, oldest first. Edit or delete lines, then Save. The brain sees the "
                           f"newest {brain.MEMORY_IN_PROMPT} and keeps up to {brain.MEMORY_MAX}.", bg=p["BG"],
                 fg=p["DIM"], font=("Segoe UI", 9), wraplength=620, justify="left").pack(anchor="w", padx=14,
                                                                                       pady=(12, 6))
        txt = tk.Text(dlg, width=84, height=16, bg=p["PANEL_HI"], fg=p["TEXT"], relief="flat",
                      insertbackground=p["GREEN"], font=("Segoe UI", 9), wrap="word", undo=True)
        txt.insert("1.0", "\n".join(self.app.cfg.get("ai_memory", [])))
        txt.pack(fill="both", expand=True, padx=14)
        row = tk.Frame(dlg, bg=p["BG"])
        row.pack(fill="x", padx=14, pady=10)

        def save(lines=None):
            if lines is None:
                lines = [" ".join(line.split()) for line in txt.get("1.0", "end").splitlines()]
            self.app.cfg["ai_memory"] = [line[:180] for line in lines if line][-brain.MEMORY_MAX:]
            core.save_config(self.app.cfg)
            self.memory_changed()
            self.app.ai_tab.personality_changed()
            dlg.destroy()

        def clear():
            if self.app.confirm("Forget everything?", "The robot forgets everything it remembered from past "
                                "conversations. This can't be undone.", ok="Forget it all", kind="warning"):
                save([])
        self.app.button(row, "Save", save, small=True).pack(side="right")
        self.app.button(row, "Cancel", dlg.destroy, small=True).pack(side="right", padx=6)
        self.app.button(row, "Forget everything", clear, small=True).pack(side="left")
        dlg.grab_set()

    def save_personality(self):
        custom = self.custom.get("1.0", "end").strip()[:brain.CUSTOM_MAX]
        key = self.pers_var.get()
        if key == "custom" and not custom:
            self.pers_lbl.configure(text="Describe the personality in the box first.", fg=self.pal["WARN"])
            return
        facts = self.facts.get("1.0", "end").strip()[:brain.FACTS_MAX]
        self.app.cfg.update(ai_personality=key, ai_personality_custom=custom, ai_facts=facts)
        core.save_config(self.app.cfg)
        self.app.ai_tab.personality_changed()
        label = brain.PERSONALITIES.get(key, ("Your own",))[0]
        nf = len([f for f in facts.splitlines() if f.strip()])
        self.pers_lbl.configure(text=f"Saved: {label}" + (f", {nf} fact{'s' if nf != 1 else ''}." if nf else "."),
                                fg=self.pal["GREEN"])
