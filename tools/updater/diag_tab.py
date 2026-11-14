"""
Dimension Hopper - Diagnostics tab: connect to the robot or controller over USB (without resetting it) and watch
its sensors and buttons live. Needs fork firmware with the diagnostics stream ("diag on" -> [BBDIAG] lines).
"""
from __future__ import annotations

import base64
import io
import time
import tkinter as tk
from tkinter import ttk

import hopper_core as core
import tooltip

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


class _Link:
    """One board's connection on the Diagnostics tab: its port picker, its session and what it reported."""

    def __init__(self, kind: str, title: str):
        self.kind, self.title = kind, title
        self.session: core.DiagSession | None = None
        self.port = ""
        self.dev = ""          # what the board says it is ("bot" / "ctrl"), once it has said
        self.last_diag = 0.0
        self.port_var: tk.StringVar | None = None
        self.port_box: ttk.Combobox | None = None
        self.btn: tk.Button | None = None
        self.state_lbl: tk.Label | None = None


class DiagnosticsTab(tk.Frame):
    def __init__(self, master, pal: dict, app):
        super().__init__(master, bg=pal["BG"])
        self.pal, self.app = pal, app
        # One connection per board, like the Flash tab: the robot on the left, the controller on the right
        self.links = {"bot": _Link("bot", "ButterBot"), "ctrl": _Link("ctrl", "Controller")}

        p = pal
        top = tk.Frame(self, bg=p["BG"])
        top.pack(fill="x", padx=18, pady=(10, 6))
        tk.Label(top, text="DIAGNOSTICS", bg=p["BG"], fg=p["GREEN"], font=("Segoe UI Black", 13)).pack(side="left")
        tk.Label(top, text="live sensors & buttons over USB · the board keeps running", bg=p["BG"], fg=p["DIM"],
                 font=("Segoe UI", 9)).pack(side="left", padx=10, pady=(4, 0))

        body = tk.Frame(self, bg=p["BG"])
        body.pack(fill="both", expand=True, padx=18)
        body.rowconfigure(0, weight=1)

        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)
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

    def _conn_row(self, panel, link: "_Link"):
        p = self.pal
        row = tk.Frame(panel, bg=p["PANEL"])
        row.pack(fill="x", padx=12, pady=(4, 0))
        tk.Label(row, text="Port", bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 9)).pack(side="left")
        link.port_var = tk.StringVar()
        link.port_box = ttk.Combobox(row, textvariable=link.port_var, state="readonly", style="Hop.TCombobox",
                                     width=30)
        link.port_box.pack(side="left", padx=(6, 4))
        self.app.button(row, "⟳", self.refresh_ports, small=True).pack(side="left")
        link.btn = self.app.button(row, "Connect", lambda: self.toggle(link), small=True)
        link.btn.pack(side="left", padx=6)
        link.state_lbl = tk.Label(panel, text="Not connected", bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8),
                                  anchor="w")
        link.state_lbl.pack(fill="x", padx=12, pady=(2, 0))

    def _build_robot(self, parent):
        p = self.pal
        f = self._panel(parent, "BUTTERBOT")
        self._conn_row(f, self.links["bot"])
        inner = tk.Frame(f, bg=p["PANEL"])
        inner.pack(fill="both", expand=True)
        left = tk.Frame(inner, bg=p["PANEL"])
        left.grid(row=0, column=0, sticky="nw")
        right = tk.Frame(inner, bg=p["PANEL"])
        right.grid(row=0, column=1, sticky="nw", padx=(4, 8))
        cv = self.bot_cv = tk.Canvas(left, width=470, height=300, bg=p["PANEL"], highlightthickness=0)
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
        self._build_drive(left)
        self._build_camera(right)
        return f

    # --- driving over USB -------------------------------------------------------------------------------
    # Hold an arrow (or arrow keys / WASD while this tab is open) and the app sends "drive <left> <right>"
    # every SEND_MS; the robot stops by itself 400 ms after the last one, so a dropped cable can't run it away.
    SEND_MS = 150
    KEYS = {"Up": "up", "w": "up", "Down": "down", "s": "down", "Left": "left", "a": "left", "Right": "right",
            "d": "right"}

    def _build_drive(self, parent):
        p = self.pal
        box = tk.Frame(parent, bg=p["PANEL"])
        box.pack(fill="x", padx=12, pady=(0, 10))
        tk.Label(box, text="DRIVE", bg=p["PANEL"], fg=p["GREEN"], font=("Segoe UI Black", 10)).grid(
            row=0, column=0, columnspan=3, sticky="w")
        self.drive_btns = {}
        for name, glyph, r, c in (("up", "▲", 1, 1), ("left", "◀", 2, 0), ("stop", "■", 2, 1), ("right", "▶", 2, 2),
                                  ("down", "▼", 3, 1)):
            b = tk.Button(box, text=glyph, width=4, bg=p["PANEL_HI"], fg=p["LIME"], activebackground=p["GREEN"],
                          activeforeground=p["BG"], relief="flat", bd=0, cursor="hand2",
                          font=("Segoe UI Black", 14), highlightthickness=1, highlightbackground=p["EDGE"])
            b.grid(row=r, column=c, padx=2, pady=2, ipady=2)
            if name == "stop":
                b.configure(command=lambda: self._drive_stop(), fg=p["BAD"])
            else:
                b.bind("<ButtonPress-1>", lambda e, n=name: self._drive_start(n))
                b.bind("<ButtonRelease-1>", lambda e: self._drive_stop())
            tooltip.Tooltip(b, "Stop the motors." if name == "stop" else
                            f"Hold to drive {'forward' if name == 'up' else 'backward' if name == 'down' else 'turning ' + name}"
                            " (or use the arrow keys / WASD). Lets go = stops.")
            self.drive_btns[name] = b

        side = tk.Frame(box, bg=p["PANEL"])
        side.grid(row=1, column=3, rowspan=3, sticky="nw", padx=(18, 0))
        tk.Label(side, text="Speed", bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8)).pack(anchor="w")
        self.speed_var = tk.IntVar(value=60)
        tk.Scale(side, from_=30, to=100, orient="horizontal", variable=self.speed_var, length=170, showvalue=True,
                 bg=p["PANEL"], fg=p["TEXT"], troughcolor="#08100a", highlightthickness=0, activebackground=p["GREEN"],
                 bd=0, sliderrelief="flat").pack(anchor="w")
        self.drive_lbl = tk.Label(side, text="Connect to the robot's port to drive it.", bg=p["PANEL"], fg=p["DIM"],
                                  font=("Segoe UI", 9), wraplength=230, justify="left", anchor="w")
        self.drive_lbl.pack(anchor="w", pady=(4, 0))
        tk.Label(side, text="Hold an arrow, or use arrow keys / WASD. Works while the robot is idle; it won't drive "
                            "forward into an obstacle or off an edge.", bg=p["PANEL"], fg=p["DIM"],
                 font=("Segoe UI", 8), wraplength=230, justify="left").pack(anchor="w", pady=(4, 0))

        self.drive_dir: str | None = None
        self._drive_job = None
        self._key_release_job = None
        self._drive_started_at = 0.0
        self._drive_reply_at = 0.0
        top = self.winfo_toplevel()
        top.bind_all("<KeyPress>", self._key_down, add="+")
        top.bind_all("<KeyRelease>", self._key_up, add="+")

    def _speeds(self, d: str) -> tuple[int, int]:
        s = int(self.speed_var.get())
        t = max(30, int(s * 0.7))
        return {"up": (s, s), "down": (-s, -s), "left": (-t, t), "right": (t, -t)}[d]

    def _drive_start(self, d: str):
        link = self.links["bot"]
        if link.session is None or link.dev not in ("bot", ""):
            self.drive_lbl.configure(text="Connect to the robot's port first.", fg=self.pal["WARN"])
            return
        if link.dev == "":
            self.drive_lbl.configure(text="Still finding out what's on this port…", fg=self.pal["DIM"])
            return
        self.drive_dir = d
        for n, b in self.drive_btns.items():
            b.configure(bg=self.pal["GREEN"] if n == d else self.pal["PANEL_HI"])
        if self._drive_job is None:
            self._drive_tick()

    def _drive_tick(self):
        bot = self.links["bot"].session
        if self.drive_dir is None or bot is None:
            self._drive_job = None
            return
        left, right = self._speeds(self.drive_dir)
        bot.send(f"drive {left} {right}")
        now = time.time()
        if self._drive_started_at == 0.0:
            self._drive_started_at = now
        elif now - self._drive_started_at > 1.2 and self._drive_reply_at < self._drive_started_at:
            self.drive_lbl.configure(text="No answer from the robot. Driving needs the new robot firmware "
                                          "(this Dimension Hopper's release or newer).", fg=self.pal["WARN"])
        self._drive_job = self.after(self.SEND_MS, self._drive_tick)

    def _drive_stop(self):
        self.drive_dir = None
        self._drive_started_at = 0.0
        if self._drive_job is not None:
            self.after_cancel(self._drive_job)
            self._drive_job = None
        for b in self.drive_btns.values():
            b.configure(bg=self.pal["PANEL_HI"])
        bot = self.links["bot"].session
        if bot is not None:
            bot.send("stop")

    def _tab_visible(self) -> bool:
        try:
            return bool(self.winfo_ismapped())
        except tk.TclError:
            return False

    def _key_down(self, e):
        d = self.KEYS.get(e.keysym)
        if d is None or not self._tab_visible() or isinstance(e.widget, (tk.Entry, ttk.Combobox)):
            return
        if self._key_release_job is not None:  # Windows key auto-repeat: release+press pairs while held
            self.after_cancel(self._key_release_job)
            self._key_release_job = None
        if self.drive_dir != d:
            self._drive_start(d)

    def _key_up(self, e):
        if self.KEYS.get(e.keysym) is None or not self._tab_visible():
            return
        if self._key_release_job is not None:
            self.after_cancel(self._key_release_job)
        self._key_release_job = self.after(80, self._key_released)

    def _key_released(self):
        self._key_release_job = None
        self._drive_stop()

    def _drive_reply(self, f: dict):
        p = self.pal
        self._drive_reply_at = time.time()
        res, why = f.get("result", ""), f.get("why", "")
        if res == "ok":
            self.drive_lbl.configure(text=f"Driving (left {f.get('l')}, right {f.get('r')})", fg=p["GREEN"])
        elif res == "blocked":
            text = {"obstacle": "Blocked: something in front of it.", "no_floor": "Blocked: no floor under it.",
                    "busy": f"The robot is busy ({f.get('state', '?')}). Driving works when it's idle.",
                    "no_motors": "The motor board didn't start."}.get(why, f"Blocked ({why})")
            self.drive_lbl.configure(text=text, fg=p["WARN"])
        elif res == "stopped":
            self.drive_lbl.configure(text="Stopped.", fg=p["DIM"])

    def _build_controller(self, parent):
        p = self.pal
        f = self._panel(parent, "CONTROLLER")
        self._conn_row(f, self.links["ctrl"])
        inner = tk.Frame(f, bg=p["PANEL"])
        inner.pack(fill="both", expand=True)
        left = tk.Frame(inner, bg=p["PANEL"])
        left.grid(row=0, column=0, sticky="nw")
        right = tk.Frame(inner, bg=p["PANEL"])
        right.grid(row=0, column=1, sticky="nw", padx=(4, 8))
        self._build_screen(right)
        cv = self.ctrl_cv = tk.Canvas(left, width=470, height=300, bg=p["PANEL"], highlightthickness=0)
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

    # --- robot camera -----------------------------------------------------------------------------------
    # "cam" makes the robot reply with one [BBCAM] line holding a base64 JPEG (128x128). The next request goes out as
    # soon as a frame arrives, so the frame rate is whatever the USB serial link manages.
    CAM_SCALE = 2          # 128 px -> 256 px on screen
    CAM_RETRY_S = 1.0      # after "busy" (a routine is using the camera)
    CAM_TIMEOUT_S = 6.0    # no reply: ask again

    def _build_camera(self, f):
        p = self.pal
        tk.Label(f, text="CAMERA", bg=p["PANEL"], fg=p["GREEN"], font=("Segoe UI Black", 10)).pack(anchor="w",
                                                                                                   pady=(6, 0))
        size = 128 * self.CAM_SCALE
        box = tk.Frame(f, bg="#040705", width=size, height=size)  # fixed pixel size, picture or not
        box.pack(pady=(4, 4))
        box.pack_propagate(False)
        self.cam_img_lbl = tk.Label(box, bg="#040705", text="no picture yet", fg=p["DIM"], font=("Segoe UI", 9))
        self.cam_img_lbl.pack(fill="both", expand=True)
        row = tk.Frame(f, bg=p["PANEL"])
        row.pack(fill="x", pady=(0, 4))
        self.cam_btn = self.app.button(row, "▶ Start camera", self.toggle_camera, small=True)
        self.cam_btn.pack(side="left")
        self.app.button(row, "Save snapshot…", self.save_snapshot, small=True).pack(side="left", padx=6)
        self.cam_status = tk.Label(f, text="Shows what the robot sees, while it's idle. About 1-2 frames a second.",
                                   bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8), anchor="w", justify="left",
                                   wraplength=size)
        self.cam_status.pack(fill="x", pady=(0, 8))
        self.cam_on = False
        self.cam_photo = None       # keep a reference, or Tk drops the image
        self.cam_jpeg = b""         # last frame, for Save snapshot
        self.cam_asked_at = 0.0
        self.cam_frames = []        # arrival times, for the fps readout
        return f

    def toggle_camera(self):
        if self.cam_on:
            self._camera_stop("Stopped.")
            return
        if not self._bot_ready():
            self.cam_status.configure(text="Connect to the robot's port first.", fg=self.pal["WARN"])
            return
        self.cam_on = True
        self.cam_frames = []
        tooltip.relabel(self.cam_btn, "■ Stop camera")
        self.cam_status.configure(text="Waiting for the first frame…", fg=self.pal["DIM"])
        self._camera_ask()
        self.after(1000, self._camera_watch)

    def _camera_stop(self, why: str):
        self.cam_on = False
        tooltip.relabel(self.cam_btn, "▶ Start camera")
        self.cam_status.configure(text=why, fg=self.pal["DIM"])

    def _camera_ask(self):
        bot = self.links["bot"].session
        if self.cam_on and bot is not None:
            bot.send("cam")
            self.cam_asked_at = time.time()

    def _camera_watch(self):
        if not self.cam_on:
            return
        if time.time() - self.cam_asked_at > self.CAM_TIMEOUT_S:
            self._camera_ask()  # a reply got lost (or the firmware has no camera command)
        self.after(1000, self._camera_watch)

    def _camera_reply(self, fields: dict):
        p = self.pal
        if "jpg" in fields:
            try:
                from PIL import Image, ImageTk
                data = base64.b64decode(fields["jpg"])
                img = Image.open(io.BytesIO(data))
                img.load()
                size = 128 * self.CAM_SCALE
                self.cam_photo = ImageTk.PhotoImage(img.resize((size, size), Image.BILINEAR))
                self.cam_img_lbl.configure(image=self.cam_photo, text="")
                self.cam_jpeg = data
                now = time.time()
                self.cam_frames = [t for t in self.cam_frames if now - t < 5] + [now]
                fps = (len(self.cam_frames) - 1) / max(0.001, self.cam_frames[-1] - self.cam_frames[0]) \
                    if len(self.cam_frames) > 1 else 0
                self.cam_status.configure(text=f"Live · {fps:.1f} frames/s · {len(data) / 1024:.1f} KB per frame",
                                          fg=p["GREEN"])
            except Exception as e:  # noqa: BLE001 - a garbled line: just ask for the next one
                self.cam_status.configure(text=f"Couldn't read a frame ({e}) - retrying", fg=p["WARN"])
            if self.cam_on:
                self._camera_ask()
        elif fields.get("result") == "busy":
            why = {"routine": "the robot is doing something (a routine)", "state": "the robot isn't idle",
                   "facedet": "face recognition is using the camera", "working": "still sending the last frame"}.get(
                fields.get("why", ""), fields.get("why", "busy"))
            self.cam_status.configure(text=f"Paused: {why}. Retrying…", fg=p["WARN"])
            if self.cam_on:
                self.after(int(self.CAM_RETRY_S * 1000), self._camera_ask)
        else:
            self.cam_status.configure(text=f"Camera error ({fields.get('why', '?')}) - retrying", fg=p["WARN"])
            if self.cam_on:
                self.after(int(self.CAM_RETRY_S * 1000), self._camera_ask)

    def save_snapshot(self):
        if not self.cam_jpeg:
            self.cam_status.configure(text="No frame yet - start the camera first.", fg=self.pal["WARN"])
            return
        from tkinter import filedialog
        path = filedialog.asksaveasfilename(parent=self, title="Save camera snapshot", defaultextension=".jpg",
                                            initialfile=time.strftime("butterbot-camera-%Y%m%d-%H%M%S.jpg"),
                                            filetypes=[("JPEG picture", "*.jpg")])
        if path:
            with open(path, "wb") as fh:
                fh.write(self.cam_jpeg)
            self.cam_status.configure(text=f"Saved {path}", fg=self.pal["GREEN"])

    # --- controller screen mirror ---------------------------------------------------------------------
    # "scr" makes the controller reply with one [BBSCR] line: a 16-byte row mask, then the changed rows' RGB565
    # pixels run-length coded (see the controller's main.cpp). "scr full" sends every row. Only changes travel, so a
    # still menu costs a few bytes and the view keeps up with the stick.
    SCR_SCALE = 2
    SCR_W = SCR_H = 128
    SCR_POLL_MS = 120       # gap between a reply and the next request
    SCR_TIMEOUT_S = 6.0     # no reply: ask for a full frame again

    def _build_screen(self, f):
        p = self.pal
        tk.Label(f, text="SCREEN", bg=p["PANEL"], fg=p["GREEN"], font=("Segoe UI Black", 10)).pack(anchor="w",
                                                                                                   pady=(6, 0))
        size = self.SCR_W * self.SCR_SCALE
        box = tk.Frame(f, bg="#040705", width=size, height=size)
        box.pack(pady=(4, 4))
        box.pack_propagate(False)
        self.scr_img_lbl = tk.Label(box, bg="#040705", text="not mirroring", fg=p["DIM"], font=("Segoe UI", 9))
        self.scr_img_lbl.pack(fill="both", expand=True)
        row = tk.Frame(f, bg=p["PANEL"])
        row.pack(fill="x", pady=(0, 4))
        self.scr_btn = self.app.button(row, "▶ Mirror screen", self.toggle_screen, small=True)
        self.scr_btn.pack(side="left")
        self.app.button(row, "Save screenshot…", self.save_screenshot, small=True).pack(side="left", padx=6)
        self.scr_status = tk.Label(f, text="Shows exactly what the controller's screen shows, live.",
                                   bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8), anchor="w", justify="left",
                                   wraplength=size)
        self.scr_status.pack(fill="x", pady=(0, 8))
        self.scr_on = False
        self.scr_photo = None
        self.scr_rgb = None          # bytearray, 128*128*3 - the picture as we know it
        self.scr_asked_at = 0.0
        self.scr_updates = []        # arrival times of frames that changed something
        self.scr_last_kb = 0.0

    def _ctrl_ready(self) -> bool:
        link = self.links["ctrl"]
        return link.session is not None and link.dev in ("", "ctrl")

    def toggle_screen(self):
        if self.scr_on:
            self._screen_stop("Stopped.")
            return
        if not self._ctrl_ready():
            self.scr_status.configure(text="Connect to the controller's port first.", fg=self.pal["WARN"])
            return
        self.scr_on = True
        self.scr_updates = []
        tooltip.relabel(self.scr_btn, "■ Stop mirror")
        self.scr_status.configure(text="Waiting for the first picture…", fg=self.pal["DIM"])
        self._screen_ask(full=True)
        self.after(1000, self._screen_watch)

    def _screen_stop(self, why: str):
        self.scr_on = False
        tooltip.relabel(self.scr_btn, "▶ Mirror screen")
        self.scr_status.configure(text=why, fg=self.pal["DIM"])

    def _screen_ask(self, full=False):
        link = self.links["ctrl"]
        if self.scr_on and link.session is not None:
            link.session.send("scr full" if full or self.scr_rgb is None else "scr")
            self.scr_asked_at = time.time()

    def _screen_watch(self):
        if not self.scr_on:
            return
        if time.time() - self.scr_asked_at > self.SCR_TIMEOUT_S:
            self.scr_status.configure(text="No reply - the controller may need the latest fork firmware. Retrying…",
                                      fg=self.pal["WARN"])
            self._screen_ask(full=True)
        self.after(1000, self._screen_watch)

    @staticmethod
    def decode_screen(data: bytes, w: int, h: int) -> tuple[list[int], list[int]]:
        """[BBSCR] payload -> (included row numbers, their RGB565 pixels). Raises ValueError if it doesn't add up."""
        mask_len = h // 8
        if len(data) < mask_len:
            raise ValueError("short frame")
        rows = [y for y in range(h) if data[y // 8] >> (y % 8) & 1]
        px: list[int] = []
        o, n = mask_len, len(data)
        while o < n:
            c = data[o]
            o += 1
            if c >= 0x80:
                if o + 2 > n:
                    raise ValueError("cut-off run")
                px.extend([data[o] | data[o + 1] << 8] * ((c & 0x7F) + 2))
                o += 2
            else:
                cnt = c + 1
                if o + 2 * cnt > n:
                    raise ValueError("cut-off pixels")
                px.extend(data[o + 2 * i] | data[o + 2 * i + 1] << 8 for i in range(cnt))
                o += 2 * cnt
        if len(px) != len(rows) * w:
            raise ValueError(f"{len(px)} pixels for {len(rows)} rows")
        return rows, px

    def _screen_reply(self, fields: dict):
        p = self.pal
        if "d" not in fields:
            why = {"nomem": "the controller is short of memory", "nodisplay": "the screen isn't running"}.get(
                fields.get("busy", ""), fields.get("busy", "busy"))
            self.scr_status.configure(text=f"Paused: {why}. Retrying…", fg=p["WARN"])
            if self.scr_on:
                self.after(1000, lambda: self._screen_ask(full=True))
            return
        try:
            w, h = int(fields.get("w", 128)), int(fields.get("h", 128))
            if (w, h) != (self.SCR_W, self.SCR_H):
                raise ValueError(f"unexpected size {w}x{h}")
            data = base64.b64decode(fields["d"])
            rows, px = self.decode_screen(data, w, h)
            if fields.get("key") != "1" and self.scr_rgb is None:
                raise ValueError("missed the full picture")
            if self.scr_rgb is None:
                self.scr_rgb = bytearray(w * h * 3)
            rgb = self.scr_rgb
            k = 0
            for y in rows:
                o = y * w * 3
                for v in px[k:k + w]:
                    r5, g6, b5 = v >> 11 & 31, v >> 5 & 63, v & 31
                    rgb[o] = r5 << 3 | r5 >> 2
                    rgb[o + 1] = g6 << 2 | g6 >> 4
                    rgb[o + 2] = b5 << 3 | b5 >> 2
                    o += 3
                k += w
            now = time.time()
            if rows:
                from PIL import Image, ImageTk
                img = Image.frombytes("RGB", (w, h), bytes(rgb))
                size = w * self.SCR_SCALE
                self.scr_photo = ImageTk.PhotoImage(img.resize((size, size), Image.NEAREST))
                self.scr_img_lbl.configure(image=self.scr_photo, text="")
                self.scr_updates = [t for t in self.scr_updates if now - t < 5] + [now]
                self.scr_last_kb = len(data) / 1024
            recent = [t for t in self.scr_updates if now - t < 5]
            self.scr_status.configure(
                text=f"Live · {len(recent) / 5:.1f} changes/s · last change {self.scr_last_kb:.1f} KB" if recent
                else "Live · the screen isn't changing", fg=p["GREEN"])
            if self.scr_on:
                self.after(self.SCR_POLL_MS, self._screen_ask)
        except Exception as e:  # noqa: BLE001 - a garbled line: start again from a full picture
            self.scr_status.configure(text=f"Couldn't read the picture ({e}) - retrying", fg=p["WARN"])
            self.scr_rgb = None
            if self.scr_on:
                self.after(self.SCR_POLL_MS, lambda: self._screen_ask(full=True))

    def save_screenshot(self):
        if self.scr_rgb is None:
            self.scr_status.configure(text="No picture yet - start the mirror first.", fg=self.pal["WARN"])
            return
        from tkinter import filedialog
        from PIL import Image
        path = filedialog.asksaveasfilename(parent=self, title="Save controller screenshot", defaultextension=".png",
                                            initialfile=time.strftime("controller-screen-%Y%m%d-%H%M%S.png"),
                                            filetypes=[("PNG picture", "*.png")])
        if path:
            img = Image.frombytes("RGB", (self.SCR_W, self.SCR_H), bytes(self.scr_rgb))
            img.resize((self.SCR_W * 4, self.SCR_H * 4), Image.NEAREST).save(path)
            self.scr_status.configure(text=f"Saved {path} (4x size)", fg=self.pal["GREEN"])

    # --- connection -------------------------------------------------------------------------------------
    def _bot_ready(self) -> bool:
        link = self.links["bot"]
        return link.session is not None and link.dev == "bot"

    def refresh_ports(self):
        ports = core.list_ports()
        labels = [pt.label() for pt in ports]
        for link in self.links.values():
            if link.port_box is not None:
                link.port_box.configure(values=labels)
        # Pre-fill from the Flash tab, which knows which board is on which port after a scan
        slots = getattr(self.app, "slots", {})
        for kind, link in self.links.items():
            if link.port_var is None or link.port_var.get() or link.session is not None:
                continue
            slot = slots.get(kind)  # Flash slots are keyed by Kind.BOT / Kind.CTRL = "bot" / "ctrl"
            port = slot.port() if slot is not None else ""
            match = next((l for l in labels if l.split(" ", 1)[0] == port), "") if port else ""
            if match:
                link.port_var.set(match)

    def toggle(self, link: "_Link"):
        if link.session is not None:
            link.session.close()
            return
        port = link.port_var.get().split(" ", 1)[0]
        if not port:
            link.state_lbl.configure(text="Pick the port first.", fg=self.pal["WARN"])
            return
        other = self.links["ctrl" if link.kind == "bot" else "bot"]
        if other.session is not None and other.port == port:
            link.state_lbl.configure(text=f"{port} is already connected on the {other.title} side.",
                                     fg=self.pal["WARN"])
            return
        if self.app.busy:
            link.state_lbl.configure(text="Busy flashing - try again when it's done", fg=self.pal["WARN"])
            return
        if port == self.app.console_tab.port_open():
            link.state_lbl.configure(text="That port is open in the Console tab - disconnect it there first",
                                     fg=self.pal["WARN"])
            return
        link.dev, link.port, link.last_diag = "", port, 0.0
        self.app.monitor_ports.add(port)
        link.session = core.DiagSession(port, lambda k, f, l: self._on_line(link, k, f, l),
                                        lambda why: self._on_end(link, why))
        link.session.start()
        tooltip.relabel(link.btn, "Disconnect")
        link.state_lbl.configure(text=f"Connected to {port} - waiting for data…", fg=self.pal["DIM"])

    def _on_end(self, link: "_Link", reason: str):
        def ui():
            link.session = None
            self.app.monitor_ports.discard(link.port)
            tooltip.relabel(link.btn, "Connect")
            link.state_lbl.configure(text=reason, fg=self.pal["DIM"] if reason == "Disconnected" else self.pal["BAD"])
            link.dev = ""
            if link.kind == "bot":
                self._drive_stop()
                self.drive_lbl.configure(text="Connect to the robot's port to drive it.", fg=self.pal["DIM"])
                self._set_robot({})
                if self.cam_on:
                    self._camera_stop("Stopped - disconnected.")
            else:
                self._set_ctrl({})
                if self.scr_on:
                    self._screen_stop("Stopped - disconnected.")
                self.scr_rgb = None
        self.app.ui(ui)

    def _on_line(self, link: "_Link", kind: str, fields: dict, line: str):
        def ui():
            tag = "bot " if link.kind == "bot" else "ctrl"
            if kind == "BBCAM":
                if "jpg" not in fields:
                    self._append_log(f"{tag} | {line}")
                if link.kind == "bot":
                    self._camera_reply(fields)
                return
            if kind == "BBSCR":
                if "d" not in fields:
                    self._append_log(f"{tag} | {line}")
                if link.kind == "ctrl" and self.scr_on:
                    self._screen_reply(fields)
                return
            self._append_log(f"{tag} | {line}")
            dev = fields.get("dev", "") if kind in ("BBDIAG", "BBSTAT") else ""
            if dev:
                link.dev = dev
                if dev != link.kind:
                    other = "Controller" if link.kind == "bot" else "ButterBot"
                    link.state_lbl.configure(text=f"This port has the {'controller' if dev == 'ctrl' else 'robot'} on "
                                                  f"it - connect it on the {other} side instead.", fg=self.pal["WARN"])
                    return
            if kind == "BBDIAG":
                link.last_diag = time.time()
                (self._set_robot if link.kind == "bot" else self._set_ctrl)(fields)
            elif kind == "BBDRIVE" and link.kind == "bot":
                self._drive_reply(fields)
            elif kind == "BBSTAT" and dev == link.kind:
                link.state_lbl.configure(text=f"{link.title} on {link.port} · fw {fields.get('fw', '?')}",
                                         fg=self.pal["GREEN"])
                if dev == "bot":
                    self.bot_cv.itemconfigure(self.b_charge, text=f"Charging: {fields.get('charge', '-')}")
        self.app.ui(ui)

    def _watchdog(self):
        for link in self.links.values():
            if link.session is not None and link.dev in ("", link.kind) and time.time() - link.last_diag > STALE_S:
                link.state_lbl.configure(
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
        for link in self.links.values():
            if link.session is not None:
                if link.kind == "bot" and self.drive_dir is not None:
                    link.session.send("stop")
                link.session.close()
