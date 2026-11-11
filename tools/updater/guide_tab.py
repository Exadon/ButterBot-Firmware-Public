"""
Dimension Hopper - Guide tab: what every control does (controller diagram + robot parts) and a searchable list of
every voice command. The voice list comes from guide_data.json, generated from the firmware's own Scenarios.h /
Phrases.cpp by gen_guide_data.py, so it always matches what the robot understands.
"""
from __future__ import annotations

import json
import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk


def _asset(name: str) -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / name


# Controller callouts: (label, function, x, y in the art's 50x92 design units, side)
# Positions match the controller art (make_art.controller) and the real board layout.
CONTROLLER_CALLOUTS = [
    ("SCREEN", "menus, replies", 25.0, 30.0, "right"),
    ("ON / OFF", "power switch", 25.0, 53.8, "left"),
    ("MANUAL OVERRIDE", "hold: RC · back · exit RC", 11.5, 57.5, "left"),
    ("POKE", "poke the robot", 11.5, 69.5, "left"),
    ("STICK", "push: menu · move · drive", 25.0, 72.0, "left"),
    ("SHUT UP", "mute · menu: select", 38.5, 57.5, "right"),
    ("SUMMON", "tap: listen · hold: come", 38.5, 69.5, "right"),
    ("USB-C", "charge · flash", 25.0, 88.5, "right"),
]
CONTROLLER_DESIGN = (50.0, 92.0)

ROBOT_PARTS = [
    ("Power button", "hold ~1 s: turn on · tap: listen · hold 3 s: turn off · hold 10 s: force off"),
    ("Camera", "who is this / what is this, object & face recognition, RC video feed"),
    ("Microphone + speaker", "voice commands, replies, sound effects"),
    ("Front sensor", "stops for obstacles (Settings → Front sensor)"),
    ("Bottom sensor", "table-edge stop, pick-up detection (Settings → Bottom sensor)"),
    ("Motion sensor", "notices falls, shakes, being picked up or turned over"),
    ("Module ports", "LED, climate, gas, motion (PIR), IR remote, perfboard modules"),
    ("USB-C", "charging; flashing with Dimension Hopper (robot must be switched on)"),
]

CONTROLLER_NOTES = [
    "Joystick button: no function in this firmware.",
    "In menus the stick moves, Shut up selects, Override goes back.",
    "Settings is the first row of the menu.",
]


class GuideTab(tk.Frame):
    def __init__(self, master, pal: dict, load_art):
        super().__init__(master, bg=pal["BG"])
        self.pal = pal
        self.load_art = load_art
        self.data = self._load_data()

        self.columnconfigure(0, weight=0)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        left = tk.Frame(self, bg=pal["BG"])
        left.grid(row=0, column=0, sticky="nsew", padx=(18, 8), pady=10)
        right = tk.Frame(self, bg=pal["BG"])
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 18), pady=10)

        self._controller_card(left)
        self._robot_card(left)
        self._voice_card(right)

    # --- data --------------------------------------------------------------------------------
    @staticmethod
    def _load_data() -> dict:
        try:
            return json.loads(_asset("guide_data.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"triggerCount": 0, "entries": []}

    def _card(self, parent, title: str) -> tk.Frame:
        p = self.pal
        card = tk.Frame(parent, bg=p["PANEL"], highlightbackground=p["EDGE"], highlightthickness=1)
        card.pack(fill="x", pady=(0, 10))
        tk.Label(card, text=title, bg=p["PANEL"], fg=p["GREEN"], font=("Segoe UI Black", 13)).pack(
            anchor="w", padx=12, pady=(8, 2))
        return card

    # --- controller ------------------------------------------------------------------------------
    def _controller_card(self, parent):
        p = self.pal
        card = self._card(parent, "CONTROLLER")
        W, H, img_w, img_h = 470, 236, 118, 218
        cv = tk.Canvas(card, width=W, height=H, bg=p["PANEL"], highlightthickness=0)
        cv.pack(padx=8)
        self._ctrl_img = self.load_art("controller_guide.png", img_h)
        x0, y0 = (W - img_w) // 2, (H - img_h) // 2
        if self._ctrl_img is not None:
            cv.create_image(x0, y0, image=self._ctrl_img, anchor="nw")
        sx, sy = img_w / CONTROLLER_DESIGN[0], img_h / CONTROLLER_DESIGN[1]

        # spread labels vertically per side so they don't overlap
        for side in ("left", "right"):
            items = sorted([c for c in CONTROLLER_CALLOUTS if c[4] == side], key=lambda c: c[3])
            n = len(items)
            for i, (name, fn, dx, dy, _) in enumerate(items):
                px, py = x0 + dx * sx, y0 + dy * sy
                ly = 22 + i * (H - 44) / max(1, n - 1)
                lx = 8 if side == "left" else W - 8
                anchor = "w" if side == "left" else "e"
                elbow_x = x0 - 12 if side == "left" else x0 + img_w + 12
                cv.create_line(px, py, elbow_x, ly, (lx + (118 if side == "left" else -118)), ly,
                               fill=p["DIM"], width=1)
                cv.create_oval(px - 3, py - 3, px + 3, py + 3, outline=p["LIME"], width=2)
                cv.create_text(lx, ly - 7, text=name, anchor=anchor, fill=p["GREEN"], font=("Segoe UI Black", 8))
                cv.create_text(lx, ly + 7, text=fn, anchor=anchor, fill=p["TEXT"], font=("Segoe UI", 8))

        for note in CONTROLLER_NOTES:
            tk.Label(card, text="• " + note, bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 8), anchor="w").pack(
                fill="x", padx=14)
        tk.Frame(card, bg=p["PANEL"], height=8).pack()

    # --- robot -----------------------------------------------------------------------------------
    def _robot_card(self, parent):
        p = self.pal
        card = self._card(parent, "BUTTERBOT")
        body = tk.Frame(card, bg=p["PANEL"])
        body.pack(fill="x", padx=12, pady=(0, 10))
        self._bot_img = self.load_art("bot_guide.png", 118)
        if self._bot_img is not None:
            tk.Label(body, image=self._bot_img, bg=p["PANEL"]).pack(side="left", anchor="n", padx=(0, 10))
        parts = tk.Frame(body, bg=p["PANEL"])
        parts.pack(side="left", fill="x", expand=True, anchor="n")
        for name, fn in ROBOT_PARTS:
            tk.Label(parts, text=name, bg=p["PANEL"], fg=p["GREEN"], font=("Segoe UI Black", 8), anchor="w").pack(
                fill="x")
            tk.Label(parts, text=fn, bg=p["PANEL"], fg=p["TEXT"], font=("Segoe UI", 8), anchor="w", justify="left",
                     wraplength=320).pack(fill="x", pady=(0, 1))

    # --- voice commands -----------------------------------------------------------------------------
    def _voice_card(self, parent):
        p = self.pal
        card = tk.Frame(parent, bg=p["PANEL"], highlightbackground=p["EDGE"], highlightthickness=1)
        card.pack(fill="both", expand=True)
        head = tk.Frame(card, bg=p["PANEL"])
        head.pack(fill="x", padx=12, pady=(8, 4))
        tk.Label(head, text="VOICE COMMANDS", bg=p["PANEL"], fg=p["GREEN"], font=("Segoe UI Black", 13)).pack(
            side="left")
        n_actions = len(self.data["entries"])
        tk.Label(head, text=f'{self.data.get("triggerCount", 0)} phrases · {n_actions} actions', bg=p["PANEL"],
                 fg=p["DIM"], font=("Segoe UI", 9)).pack(side="left", padx=8, pady=(4, 0))

        tk.Label(card, text="Tap the robot's power button (or Summon on the controller) and say any phrase.",
                 bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 9), anchor="w").pack(fill="x", padx=12)

        srow = tk.Frame(card, bg=p["PANEL"])
        srow.pack(fill="x", padx=12, pady=6)
        tk.Label(srow, text="Search", bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI", 9)).pack(side="left")
        self.search_var = tk.StringVar()
        e = tk.Entry(srow, textvariable=self.search_var, bg=p["PANEL_HI"], fg=p["TEXT"], insertbackground=p["GREEN"],
                     relief="flat", highlightthickness=1, highlightbackground=p["EDGE"], highlightcolor=p["GREEN"])
        e.pack(side="left", fill="x", expand=True, padx=(8, 0), ipady=3)
        self.search_var.trace_add("write", lambda *a: self._fill_tree())

        tframe = tk.Frame(card, bg=p["PANEL"])
        tframe.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        # Three tidy columns: a short action name, ONE example phrase (+ how many more), and the reply / note.
        # Everything else for the selected row goes in the details box below, wrapped instead of cut off.
        self.tree = ttk.Treeview(tframe, columns=("say", "info"), show="tree headings", style="Hop.Treeview")
        self.tree.heading("#0", text="Action", anchor="w")
        self.tree.heading("say", text="Say (example)", anchor="w")
        self.tree.heading("info", text="Reply / note", anchor="w")
        self.tree.column("#0", width=200, minwidth=160, stretch=False, anchor="w")
        self.tree.column("say", width=250, minwidth=180, stretch=False, anchor="w")
        self.tree.column("info", width=300, minwidth=200, stretch=True, anchor="w")
        sb = ttk.Scrollbar(tframe, command=self.tree.yview, style="Hop.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.tag_configure("cat", foreground=p["LIME"], font=("Segoe UI Semibold", 9))
        self.tree.tag_configure("odd", background=p["PANEL"])
        self.tree.tag_configure("even", background="#111b14")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._show_details())

        # Details for the selected action: every phrase, and the full reply
        det = tk.Frame(card, bg=p["PANEL_HI"], highlightbackground=p["EDGE"], highlightthickness=1)
        det.pack(fill="x", padx=12, pady=(0, 12))
        self.det_title = tk.Label(det, text="Select an action to see every phrase for it", bg=p["PANEL_HI"],
                                  fg=p["GREEN"], font=("Segoe UI Semibold", 10), anchor="w")
        self.det_title.pack(fill="x", padx=10, pady=(6, 2))
        self.det_say = tk.Label(det, text="", bg=p["PANEL_HI"], fg=p["TEXT"], font=("Segoe UI", 9), anchor="w",
                                justify="left", wraplength=700)
        self.det_say.pack(fill="x", padx=10)
        self.det_reply = tk.Label(det, text="", bg=p["PANEL_HI"], fg=p["LIME"], font=("Segoe UI", 9, "italic"),
                                  anchor="w", justify="left", wraplength=700)
        self.det_reply.pack(fill="x", padx=10, pady=(2, 8))
        det.bind("<Configure>", lambda e: (self.det_say.configure(wraplength=e.width - 24),
                                           self.det_reply.configure(wraplength=e.width - 24)))
        self._rows: dict[str, dict] = {}
        self._fill_tree()

    def _show_details(self):
        sel = self.tree.selection()
        e = self._rows.get(sel[0]) if sel else None
        if e is None:
            return
        self.det_title.configure(text=f'{e["category"]} › {e["action"] if e["category"] != "Chat" else e["say"][0]}')
        self.det_say.configure(text="Say:  " + "   ·   ".join(f"“{s}”" for s in e["say"]))
        if e["reply"]:
            self.det_reply.configure(text="Replies:  " + e["reply"])
        elif e.get("note"):
            self.det_reply.configure(text="Note:  " + e["note"])
        else:
            self.det_reply.configure(text="")

    def _fill_tree(self):
        q = self.search_var.get().strip().lower() if hasattr(self, "search_var") else ""
        self.tree.delete(*self.tree.get_children())
        self._rows = {}
        cats: dict[str, str] = {}
        n = 0
        for e in self.data["entries"]:
            hay = " ".join([e["action"], e["reply"], e.get("note", ""), *e["say"]]).lower()
            if q and q not in hay:
                continue
            cat = e["category"]
            if cat not in cats:
                cats[cat] = self.tree.insert("", "end", text=cat, open=bool(q), tags=("cat",))
            more = len(e["say"]) - 1
            example = e["say"][0] + (f"   (+{more} more)" if more > 0 else "")
            if cat == "Chat":
                label, example = e["say"][0], f"“{e['say'][0]}”"
            else:
                label = e["action"]
            info = e["reply"] or e.get("note", "")
            iid = self.tree.insert(cats[cat], "end", text=label, values=(example, info),
                                   tags=("even" if n % 2 == 0 else "odd",))
            self._rows[iid] = e
            n += 1
        if not q:
            # keep the big Chat folder collapsed, open the rest
            for cat, iid in cats.items():
                self.tree.item(iid, open=cat != "Chat")
