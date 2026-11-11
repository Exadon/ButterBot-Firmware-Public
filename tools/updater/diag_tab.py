"""
Dimension Hopper - Diagnostics tab: connect to the robot or controller over USB (without resetting it) and watch
its sensors and buttons live. Needs fork firmware with the diagnostics stream ("diag on" -> [BBDIAG] lines).
"""
from __future__ import annotations

import time
import tkinter as tk
from tkinter import ttk

import hopper_core as core

STALE_S = 2.0  # no [BBDIAG] for this long -> "waiting for data"


class Light:
    """A labelled indicator lamp on a canvas."""

    def __init__(self, cv: tk.Canvas, x: int, y: int, label: str, pal: dict):
        self.cv, self.pal = cv, pal
        self.dot = cv.create_oval(x, y, x + 16, y + 16, fill=pal["PANEL_HI"], outline=pal["EDGE"], width=2)
        cv.create_text(x + 24, y + 8, text=label, anchor="w", fill=pal["TEXT"], font=("Segoe UI", 9))

    def set(self, state: int | None, on_color: str | None = None, off_color: str | None = None):
        p = self.pal
        if state is None or state < 0:
            fill = p["PANEL_HI"]
        elif state:
            fill = on_color or p["GREEN"]
        else:
            fill = off_color or "#24302a"
        self.cv.itemconfigure(self.dot, fill=fill)


class Bar:
    """Horizontal bar; centred=True draws -100..100 from the middle (motor speeds)."""

    def __init__(self, cv: tk.Canvas, x: int, y: int, w: int, label: str, pal: dict, centred=False):
        self.cv, self.pal, self.x, self.y, self.w, self.centred = cv, pal, x, y, w, centred
        cv.create_text(x, y - 9, text=label, anchor="w", fill=pal["DIM"], font=("Segoe UI", 8))
        cv.create_rectangle(x, y, x + w, y + 12, outline=pal["EDGE"], fill="#08100a")
        if centred:
            cv.create_line(x + w / 2, y - 2, x + w / 2, y + 14, fill=pal["DIM"])
        self.fill = cv.create_rectangle(x, y, x, y + 12, outline="", fill=pal["GREEN"])
        self.text = cv.create_text(x + w + 8, y + 6, anchor="w", fill=pal["TEXT"], font=("Segoe UI", 9), text="-")

    def set(self, value: int | None, lo=0, hi=100, color: str | None = None):
        if value is None:
            self.cv.coords(self.fill, self.x, self.y, self.x, self.y + 12)
            self.cv.itemconfigure(self.text, text="-")
            return
        v = max(lo, min(hi, value))
        if self.centred:
            mid = self.x + self.w / 2
            end = mid + (v / max(abs(lo), abs(hi))) * self.w / 2
            self.cv.coords(self.fill, min(mid, end), self.y, max(mid, end), self.y + 12)
        else:
            self.cv.coords(self.fill, self.x, self.y, self.x + (v - lo) / (hi - lo) * self.w, self.y + 12)
        self.cv.itemconfigure(self.fill, fill=color or self.pal["GREEN"])
        self.cv.itemconfigure(self.text, text=str(value))


class DiagnosticsTab(tk.Frame):
    def __init__(self, master, pal: dict, app):
        super().__init__(master, bg=pal["BG"])
        self.pal, self.app = pal, app
        self.session: core.DiagSession | None = None
        self.last_diag = 0.0
        self.dev = ""

        p = pal
        top = tk.Frame(self, bg=p["BG"])
        top.pack(fill="x", padx=18, pady=(10, 6))
        tk.Label(top, text="DIAGNOSTICS", bg=p["BG"], fg=p["GREEN"], font=("Segoe UI Black", 13)).pack(side="left")
        tk.Label(top, text="live sensors & buttons over USB · the board keeps running", bg=p["BG"], fg=p["DIM"],
                 font=("Segoe UI", 9)).pack(side="left", padx=10, pady=(4, 0))

        row = tk.Frame(self, bg=p["BG"])
        row.pack(fill="x", padx=18, pady=(0, 8))
        tk.Label(row, text="Port", bg=p["BG"], fg=p["DIM"]).pack(side="left")
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(row, textvariable=self.port_var, state="readonly", style="Hop.TCombobox", width=46)
        self.port_box.pack(side="left", padx=(8, 6))
        app.button(row, "⟳", self.refresh_ports, small=True).pack(side="left")
        self.connect_btn = app.button(row, "Connect", self.toggle)
        self.connect_btn.pack(side="left", padx=8)
        self.state_lbl = tk.Label(row, text="Not connected", bg=p["BG"], fg=p["DIM"], font=("Segoe UI", 9))
        self.state_lbl.pack(side="left", padx=6)

        body = tk.Frame(self, bg=p["BG"])
        body.pack(fill="both", expand=True, padx=18)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        self._build_robot(body).grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self._build_controller(body).grid(row=0, column=1, sticky="nsew", padx=(8, 0))

        logf = tk.Frame(self, bg=p["PANEL"], highlightbackground=p["EDGE"], highlightthickness=1)
        logf.pack(fill="both", expand=True, padx=18, pady=(10, 14))
        tk.Label(logf, text="Serial console", bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8), anchor="w").pack(
            fill="x", padx=8, pady=(4, 0))
        self.log = tk.Text(logf, bg="#040705", fg="#7dffa0", relief="flat", height=7, wrap="none",
                           font=("Cascadia Mono", 8), state="disabled")
        self.log.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.show_hide_logs = False

        self.refresh_ports()
        self.after(500, self._watchdog)

    # --- panels -------------------------------------------------------------------------------------
    def _panel(self, parent, title):
        p = self.pal
        f = tk.Frame(parent, bg=p["PANEL"], highlightbackground=p["EDGE"], highlightthickness=1)
        tk.Label(f, text=title, bg=p["PANEL"], fg=p["GREEN"], font=("Segoe UI Black", 12)).pack(anchor="w", padx=12,
                                                                                                 pady=(8, 0))
        return f

    def _build_robot(self, parent):
        p = self.pal
        f = self._panel(parent, "BUTTERBOT")
        cv = self.bot_cv = tk.Canvas(f, width=470, height=300, bg=p["PANEL"], highlightthickness=0)
        cv.pack(padx=8, pady=6)
        self.b_front_raw = Light(cv, 14, 14, "Front sensor (raw)", p)
        self.b_front = Light(cv, 14, 42, "Obstacle (after filter)", p)
        self.b_floor = Light(cv, 14, 70, "Floor detected (bottom sensor)", p)
        self.b_btn = Light(cv, 14, 98, "Power button", p)
        self.b_link = Light(cv, 14, 126, "Controller link", p)
        self.b_ml = Bar(cv, 250, 30, 150, "Motor left", p, centred=True)
        self.b_mr = Bar(cv, 250, 74, 150, "Motor right", p, centred=True)
        self.b_batt = Bar(cv, 250, 118, 150, "Battery %", p)
        self.b_state = cv.create_text(14, 170, anchor="w", fill=p["TEXT"], font=("Segoe UI", 10), text="Mode: -")
        self.b_motion = cv.create_text(14, 196, anchor="w", fill=p["TEXT"], font=("Segoe UI", 10),
                                       text="Last motion event: -")
        self.b_charge = cv.create_text(14, 222, anchor="w", fill=p["TEXT"], font=("Segoe UI", 10), text="Charging: -")
        cv.create_text(14, 262, anchor="w", fill=p["DIM"], font=("Segoe UI", 8),
                       text="Raw lamp flickering while 'Obstacle' stays off = glitches being ignored.")
        cv.create_text(14, 280, anchor="w", fill=p["DIM"], font=("Segoe UI", 8),
                       text="Sensor lamps update on changes and on the robot's own checks before it moves.")
        return f

    def _build_controller(self, parent):
        p = self.pal
        f = self._panel(parent, "CONTROLLER")
        cv = self.ctrl_cv = tk.Canvas(f, width=470, height=300, bg=p["PANEL"], highlightthickness=0)
        cv.pack(padx=8, pady=6)
        # joystick plot: processed position (-100..100) with the 50% menu threshold ring
        self.jx0, self.jy0, self.jr = 110, 140, 100
        cx, cy, r = self.jx0, self.jy0, self.jr
        cv.create_rectangle(cx - r, cy - r, cx + r, cy + r, outline=p["EDGE"], fill="#08100a")
        cv.create_oval(cx - r / 2, cy - r / 2, cx + r / 2, cy + r / 2, outline=p["DIM"], dash=(3, 3))
        cv.create_line(cx - r, cy, cx + r, cy, fill="#1c2a20")
        cv.create_line(cx, cy - r, cx, cy + r, fill="#1c2a20")
        cv.create_text(cx, cy - r - 10, text="STICK", fill=p["DIM"], font=("Segoe UI", 8))
        self.j_dot = cv.create_oval(cx - 7, cy - 7, cx + 7, cy + 7, fill=p["LIME"], outline="")
        self.j_text = cv.create_text(cx, cy + r + 14, text="x - · y -", fill=p["TEXT"], font=("Segoe UI", 9))
        self.j_raw = cv.create_text(cx, cy + r + 30, text="raw - / centre -", fill=p["DIM"], font=("Segoe UI", 8))

        self.c_override = Light(cv, 250, 30, "Manual Override", p)
        self.c_poke = Light(cv, 250, 58, "Poke", p)
        self.c_shutup = Light(cv, 250, 86, "Shut Up", p)
        self.c_summon = Light(cv, 250, 114, "Summon", p)
        self.c_joybtn = Light(cv, 250, 142, "Joystick button (unused)", p)
        self.c_link = Light(cv, 250, 182, "Robot link", p)
        self.c_batt = Bar(cv, 250, 228, 150, "Battery %", p)
        return f

    # --- connection -------------------------------------------------------------------------------------
    def refresh_ports(self):
        ports = core.list_ports()
        self.port_box.configure(values=[pt.label() for pt in ports])
        if not self.port_var.get():
            usb = [pt for pt in ports if pt.is_usb]
            if usb:
                self.port_var.set(usb[0].label())

    def port(self) -> str:
        return self.port_var.get().split(" ", 1)[0]

    def toggle(self):
        if self.session is not None:
            self.session.close()
            return
        port = self.port()
        if not port:
            return
        if self.app.busy:
            self.state_lbl.configure(text="Busy flashing - try again when it's done", fg=self.pal["WARN"])
            return
        self.app.monitor_port = port
        self.session = core.DiagSession(port, self._on_line, self._on_end)
        self.session.start()
        self.last_diag = 0.0
        self.connect_btn.configure(text="Disconnect")
        self.state_lbl.configure(text=f"Connected to {port} - waiting for data…", fg=self.pal["DIM"])

    def _on_end(self, reason: str):
        def ui():
            self.session = None
            self.app.monitor_port = None
            self.connect_btn.configure(text="Connect")
            self.state_lbl.configure(text=reason, fg=self.pal["DIM"] if reason == "Disconnected" else self.pal["BAD"])
            self._set_robot({})
            self._set_ctrl({})
        self.app.ui(ui)

    def _on_line(self, kind: str, fields: dict, line: str):
        def ui():
            self._append_log(line)
            if kind == "BBDIAG":
                self.last_diag = time.time()
                self.dev = fields.get("dev", "")
                (self._set_robot if self.dev == "bot" else self._set_ctrl)(fields)
            elif kind == "BBSTAT":
                fw = fields.get("fw", "?")
                who = "ButterBot" if fields.get("dev") == "bot" else "Controller"
                self.state_lbl.configure(text=f"{who} on {self.port()} · fw {fw}", fg=self.pal["GREEN"])
                if fields.get("dev") == "bot":
                    self.bot_cv.itemconfigure(self.b_charge, text=f"Charging: {fields.get('charge', '-')}")
        self.app.ui(ui)

    def _watchdog(self):
        if self.session is not None and time.time() - self.last_diag > STALE_S:
            self.state_lbl.configure(
                text="No live data - needs fork firmware with diagnostics (and the board switched on)",
                fg=self.pal["WARN"])
        self.after(500, self._watchdog)

    def _append_log(self, line: str):
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        if int(self.log.index("end-1c").split(".")[0]) > 300:
            self.log.delete("1.0", "50.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    # --- readings -----------------------------------------------------------------------------------------
    @staticmethod
    def _int(f: dict, key: str):
        try:
            return int(f[key])
        except (KeyError, ValueError):
            return None

    def _set_robot(self, f: dict):
        g = self._int
        p = self.pal
        self.b_front_raw.set(g(f, "front_raw"), on_color=p["WARN"])
        self.b_front.set(g(f, "front"), on_color=p["BAD"])
        self.b_floor.set(g(f, "bottom"), off_color=p["BAD"])
        self.b_btn.set(g(f, "btn"))
        self.b_link.set(g(f, "link"))
        self.b_ml.set(g(f, "motor_l"), -100, 100)
        self.b_mr.set(g(f, "motor_r"), -100, 100)
        batt = g(f, "batt")
        self.b_batt.set(batt if batt is not None and batt >= 0 else None,
                        color=p["BAD"] if batt is not None and 0 <= batt <= 15 else None)
        self.bot_cv.itemconfigure(self.b_state, text=f"Mode: {f.get('state', '-')}")
        motion = f.get("motion", "none")
        age = f.get("motion_age", "0")
        self.bot_cv.itemconfigure(self.b_motion,
                                  text="Last motion event: " + ("-" if motion in ("none", "") else f"{motion} ({age}s ago)"))

    def _set_ctrl(self, f: dict):
        g = self._int
        p = self.pal
        x, y = g(f, "joy_x"), g(f, "joy_y")
        cx, cy, r = self.jx0, self.jy0, self.jr
        if x is None or y is None:
            self.ctrl_cv.coords(self.j_dot, cx - 7, cy - 7, cx + 7, cy + 7)
            self.ctrl_cv.itemconfigure(self.j_text, text="x - · y -")
            self.ctrl_cv.itemconfigure(self.j_raw, text="raw - / centre -")
        else:
            px, py = cx + max(-100, min(100, x)) / 100 * r, cy - max(-100, min(100, y)) / 100 * r
            self.ctrl_cv.coords(self.j_dot, px - 7, py - 7, px + 7, py + 7)
            self.ctrl_cv.itemconfigure(self.j_text, text=f"x {x} · y {y}")
            self.ctrl_cv.itemconfigure(
                self.j_raw, text=f"raw {f.get('raw_x')}, {f.get('raw_y')} / centre {f.get('center_x')}, {f.get('center_y')}")
        self.c_override.set(g(f, "override"))
        self.c_poke.set(g(f, "poke"))
        self.c_shutup.set(g(f, "shutup"))
        self.c_summon.set(g(f, "summon"))
        self.c_joybtn.set(g(f, "joybtn"), on_color=p["DIM"])
        self.c_link.set(g(f, "link"))
        batt = g(f, "batt")
        self.c_batt.set(batt if batt is not None and batt >= 0 else None,
                        color=p["BAD"] if batt is not None and 0 <= batt <= 15 else None)

    def shutdown(self):
        if self.session is not None:
            self.session.close()
