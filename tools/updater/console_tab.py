"""
Dimension Hopper - Console tab: a live, passive view of a board's serial console (it is opened without resetting
the board), with filters, a "what the robot heard" table for voice commands, a command box and Save log.
"""
from __future__ import annotations

import re
import time
import tkinter as tk
from tkinter import filedialog, ttk

import hopper_core as core

MAX_LINES = 20000

# Which lines each filter shows (substring match, case-sensitive like the firmware's tags)
FILTERS = {
    "All": None,
    "Voice": ("AudioFrontend", "ListenState", "LISTEN_T", "MN_COMMAND", "AFE_", "MultiNet", "vadnet"),
    "Bluetooth": ("BLE", "Com:", "ConMan", "GAP", "link=", "pair", "bond", "BBFORGET"),
    "Memory": ("BBMEM", "BBTASK", "ram=", "heap", "alloc", "PSRAM"),
    "Errors": ("E (", "Guru", "abort", "panic", "Backtrace", "Rebooting", "assert", "fail", "Fail", "rror"),
    "Status": ("BBSTAT", "BBDRIVE", "BBFORGET", "BBVOICE"),
}

TOP_RE = re.compile(r"TOP 1, command_id: (\d+).*string:\s*(.*?), prob: ([\d.]+)")
FUZZY_OK_RE = re.compile(r"Fuzzy matched: transcript '([^']*)', phrase '([^']*)'")
FUZZY_NO_RE = re.compile(r"Fuzzy match rejected: transcript '([^']*)', best '([^']*)'")
FUZZY_AMB_RE = re.compile(r"Fuzzy match ambiguous: best '([^']*)'")
TIMEOUT_RE = re.compile(r"timeout, broadcasting transcript: '([^']*)', len: (\d+)")


class ConsoleTab(tk.Frame):
    def __init__(self, master, pal: dict, app):
        super().__init__(master, bg=pal["BG"])
        self.pal, self.app = pal, app
        self.session: core.DiagSession | None = None
        self.open_port: str | None = None
        self.lines: list[str] = []
        self.filter = "All"
        self._last_transcript = ""

        p = pal
        top = tk.Frame(self, bg=p["BG"])
        top.pack(fill="x", padx=18, pady=(10, 6))
        tk.Label(top, text="CONSOLE", bg=p["BG"], fg=p["GREEN"], font=("Segoe UI Black", 13)).pack(side="left")
        tk.Label(top, text="the board's own log, live · opening it doesn't reset anything", bg=p["BG"], fg=p["DIM"],
                 font=("Segoe UI", 9)).pack(side="left", padx=10, pady=(4, 0))

        row = tk.Frame(self, bg=p["BG"])
        row.pack(fill="x", padx=18, pady=(0, 6))
        tk.Label(row, text="Port", bg=p["BG"], fg=p["DIM"]).pack(side="left")
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(row, textvariable=self.port_var, state="readonly", style="Hop.TCombobox", width=46)
        self.port_box.pack(side="left", padx=(8, 6))
        app.button(row, "⟳", self.refresh_ports, small=True).pack(side="left")
        self.connect_btn = app.button(row, "Connect", self.toggle)
        self.connect_btn.pack(side="left", padx=8)
        self.state_lbl = tk.Label(row, text="Not connected", bg=p["BG"], fg=p["DIM"], font=("Segoe UI", 9))
        self.state_lbl.pack(side="left", padx=6)
        app.button(row, "Save log…", self.save, small=True).pack(side="right")
        app.button(row, "Clear", self.clear, small=True).pack(side="right", padx=6)

        frow = tk.Frame(self, bg=p["BG"])
        frow.pack(fill="x", padx=18, pady=(0, 6))
        tk.Label(frow, text="Show", bg=p["BG"], fg=p["DIM"]).pack(side="left", padx=(0, 6))
        self.filter_var = tk.StringVar(value="All")
        for name in FILTERS:
            tk.Radiobutton(frow, text=name, value=name, variable=self.filter_var, command=self._refilter,
                           indicatoron=False, bg=p["PANEL_HI"], fg=p["TEXT"], selectcolor="#1f5a2c",
                           activebackground=p["GREEN"], activeforeground=p["BG"], relief="flat", bd=0,
                           padx=10, pady=3, font=("Segoe UI Semibold", 9),
                           highlightthickness=1, highlightbackground=p["EDGE"]).pack(side="left", padx=2)
        self.count_lbl = tk.Label(frow, text="", bg=p["BG"], fg=p["DIM"], font=("Segoe UI", 8))
        self.count_lbl.pack(side="right")

        body = tk.PanedWindow(self, orient="vertical", bg=p["BG"], sashwidth=6, bd=0)
        body.pack(fill="both", expand=True, padx=18)

        logf = tk.Frame(body, bg=p["PANEL"], highlightbackground=p["EDGE"], highlightthickness=1)
        self.log = tk.Text(logf, bg="#040705", fg="#7dffa0", relief="flat", wrap="none",
                           font=("Cascadia Mono", 9), state="disabled")
        sb = ttk.Scrollbar(logf, command=self.log.yview, style="Hop.Vertical.TScrollbar")
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(fill="both", expand=True, padx=6, pady=6)
        self.log.tag_configure("bad", foreground=p["BAD"])
        self.log.tag_configure("warn", foreground=p["WARN"])
        self.log.tag_configure("hi", foreground=p["LIME"])
        body.add(logf, minsize=160, stretch="always")

        heard = tk.Frame(body, bg=p["PANEL"], highlightbackground=p["EDGE"], highlightthickness=1)
        tk.Label(heard, text="VOICE: WHAT THE ROBOT HEARD", bg=p["PANEL"], fg=p["GREEN"],
                 font=("Segoe UI Black", 10)).pack(anchor="w", padx=10, pady=(6, 0))
        tk.Label(heard, text="Phonetic spelling of what the recogniser got, and what it did with it "
                             "(needs fork firmware with voice logging).", bg=p["PANEL"], fg=p["DIM"],
                 font=("Segoe UI", 8)).pack(anchor="w", padx=10)
        cols = ("time", "heard", "result")
        self.heard = ttk.Treeview(heard, columns=cols, show="headings", height=6, style="Hop.Treeview")
        for c, w, t in (("time", 80, "Time"), ("heard", 260, "Heard"), ("result", 520, "Result")):
            self.heard.heading(c, text=t, anchor="w")
            self.heard.column(c, width=w, anchor="w", stretch=c == "result")
        self.heard.tag_configure("ok", foreground=p["GREEN"])
        self.heard.tag_configure("no", foreground=p["WARN"])
        self.heard.pack(fill="both", expand=True, padx=8, pady=6)
        body.add(heard, minsize=120)

        send = tk.Frame(self, bg=p["BG"])
        send.pack(fill="x", padx=18, pady=(6, 14))
        tk.Label(send, text="Send", bg=p["BG"], fg=p["DIM"]).pack(side="left")
        self.cmd_var = tk.StringVar()
        ent = tk.Entry(send, textvariable=self.cmd_var, bg=p["PANEL_HI"], fg=p["TEXT"], insertbackground=p["GREEN"],
                       relief="flat", highlightthickness=1, highlightbackground=p["EDGE"], highlightcolor=p["GREEN"])
        ent.pack(side="left", fill="x", expand=True, padx=8, ipady=3)
        ent.bind("<Return>", lambda e: self.send())
        app.button(send, "Send", self.send, small=True).pack(side="left")
        tk.Label(send, text="tasks · forget · voice [name] · say <text> · drive <l> <r> · stop", bg=p["BG"], fg=p["DIM"],
                 font=("Segoe UI", 8)).pack(side="left", padx=10)

        self.refresh_ports()

    # --- connection ----------------------------------------------------------------------------------
    def port_open(self) -> str | None:
        return self.open_port

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
        if port == self.app.monitor_port:
            self.state_lbl.configure(text="That port is open in Diagnostics - disconnect it there first",
                                     fg=self.pal["WARN"])
            return
        self.open_port = port
        self.session = core.DiagSession(port, self._on_line, self._on_end, diag=False)
        self.session.start()
        self.connect_btn.configure(text="Disconnect")
        self.state_lbl.configure(text=f"Listening on {port}…", fg=self.pal["GREEN"])
        self._add(f"--- connected to {port} at {time.strftime('%H:%M:%S')} ---")

    def _on_end(self, reason: str):
        def ui():
            self.session = None
            self.open_port = None
            self.connect_btn.configure(text="Connect")
            self.state_lbl.configure(text=reason, fg=self.pal["DIM"] if reason == "Disconnected" else self.pal["BAD"])
            self._add(f"--- {reason} ---")
        self.app.ui(ui)

    def _on_line(self, kind: str, fields: dict, line: str):
        if kind == "BBDIAG":
            return
        self.app.ui(lambda: (self._add(line), self._voice(line)))

    def send(self):
        cmd = self.cmd_var.get().strip()
        if not cmd:
            return
        if self.session is None:
            self.state_lbl.configure(text="Connect first", fg=self.pal["WARN"])
            return
        self.session.send(cmd)
        self._add(f"> {cmd}")
        self.cmd_var.set("")

    # --- log ---------------------------------------------------------------------------------------------
    @staticmethod
    def _tag(line: str):
        if any(k in line for k in ("E (", "Guru", "abort", "panic", "Backtrace", "Rebooting")):
            return "bad"
        if line.startswith("W (") or "⚠" in line:
            return "warn"
        if line.startswith(("[BBSTAT]", "> ", "---")):
            return "hi"
        return None

    def _shown(self, line: str) -> bool:
        keys = FILTERS[self.filter]
        return keys is None or line.startswith(("> ", "---")) or any(k in line for k in keys)

    def _add(self, line: str):
        stamped = time.strftime("%H:%M:%S ") + line
        self.lines.append(stamped)
        if len(self.lines) > MAX_LINES:
            del self.lines[: MAX_LINES // 10]
        if self._shown(line):
            at_end = self.log.yview()[1] > 0.999
            self.log.configure(state="normal")
            self.log.insert("end", stamped + "\n", self._tag(line) or ())
            if int(self.log.index("end-1c").split(".")[0]) > 3000:
                self.log.delete("1.0", "500.0")
            if at_end:
                self.log.see("end")
            self.log.configure(state="disabled")
        self.count_lbl.configure(text=f"{len(self.lines)} lines")

    def _refilter(self):
        self.filter = self.filter_var.get()
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        for stamped in self.lines[-3000:]:
            line = stamped[9:]
            if self._shown(line):
                self.log.insert("end", stamped + "\n", self._tag(line) or ())
        self.log.see("end")
        self.log.configure(state="disabled")

    def clear(self):
        self.lines.clear()
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        for i in self.heard.get_children():
            self.heard.delete(i)
        self.count_lbl.configure(text="")

    def save(self):
        if not self.lines:
            self.state_lbl.configure(text="Nothing to save yet", fg=self.pal["WARN"])
            return
        path = filedialog.asksaveasfilename(title="Save console log", defaultextension=".log",
                                            initialfile=time.strftime("butterbot-%Y%m%d-%H%M%S.log"),
                                            filetypes=[("Log", "*.log"), ("Text", "*.txt"), ("All files", "*.*")])
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(self.lines) + "\n")
            self.state_lbl.configure(text=f"Saved {len(self.lines)} lines to {path}", fg=self.pal["GREEN"])

    # --- voice results -------------------------------------------------------------------------------
    def _heard(self, heard: str, result: str, ok: bool):
        self.heard.insert("", "end", values=(time.strftime("%H:%M:%S"), heard.strip() or "(nothing)", result),
                          tags=("ok" if ok else "no",))
        kids = self.heard.get_children()
        if len(kids) > 200:
            self.heard.delete(kids[0])
        self.heard.see(self.heard.get_children()[-1])

    def _voice(self, line: str):
        if m := TIMEOUT_RE.search(line):
            self._last_transcript = m.group(1)
            if m.group(2) == "0":
                self._heard("", "✖ heard nothing - speak after the prompt, closer to the robot", False)
        elif m := TOP_RE.search(line):
            self._heard(m.group(2), f"✔ understood directly ({float(m.group(3)) * 100:.0f}% sure)", True)
        elif m := FUZZY_OK_RE.search(line):
            self._heard(m.group(1), f"✔ matched \"{m.group(2)}\" (close enough)", True)
        elif m := FUZZY_AMB_RE.search(line):
            self._heard(self._last_transcript,
                        f"✖ too close to call (\"{m.group(1)}\" vs another command) - not acted on", False)
        elif m := FUZZY_NO_RE.search(line):
            self._heard(m.group(1), f"✖ not understood (closest: \"{m.group(2)}\")", False)

    def shutdown(self):
        if self.session is not None:
            self.session.close()
