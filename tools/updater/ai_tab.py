"""
Dimension Hopper - AI tab: guided setup for ButterBot's AI conversation mode, and the brain itself.

Four steps, each with a status lamp:
  1. Robot WiFi    - name + password typed here and sent to the robot over USB (never on the controller)
  2. Hearing       - Whisper speech-to-text model, runs on this PC
  3. Thinking      - Claude (your own API key, kept in Windows Credential Manager) or Ollama (free, local)
  4. Turn it on    - start the brain here, switch AI mode on in the robot, test

The brain only runs while Dimension Hopper is open. Without it the robot uses its offline voice commands.
"""
from __future__ import annotations

import sys
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import ttk

import brain
import hopper_core as core

HELP = """HOW AI MODE WORKS
When you press listen, ButterBot joins your WiFi, records what you say and sends it to this PC. The PC turns it into \
text (Whisper, on this PC), asks the AI for a short in-character answer and, if it fits, one of the robot's own \
actions ("go dance" -> it dances). The robot says the answer in its own voice. Typical wait: 1-3 seconds.

WHAT YOU NEED
- A 2.4 GHz WiFi network (the robot can't use 5 GHz). This PC must be on the same network.
- Dimension Hopper open with the brain started. When it isn't, ButterBot says its brain is offline and uses its \
built-in commands - nothing breaks.
- For thinking, ONE of:
  - Claude: an API key from console.anthropic.com (pay-as-you-go; a conversation costs a fraction of a cent). The \
key is stored in Windows Credential Manager and never sent to the robot.
  - Ollama: free, runs on this PC. Install it from ollama.com, then in a terminal run:  ollama pull llama3.2

FIRST-TIME SETUP
1. Plug the robot in by USB and switch it on. Pick its port at the top of this tab.
2. Type your WiFi name and password and hit "Send to robot", then "Test robot WiFi".
3. Pick a Whisper model and hit "Load" (it downloads once).
4. Choose Claude (paste your key, Save) or Ollama (Check), then "Try it" with a typed message.
5. With the robot still plugged in, press "Start AI conversations": it starts the brain and sets the robot up to use \
it (its AI setting and this PC's address). Then unplug it, press listen and talk. From then on Start works without the \
robot plugged in - unless this PC's address changes (the status line says so). To stop that ever happening, give this \
PC a reserved / fixed address in your router's app. The first time, Windows may ask whether Dimension Hopper may \
use the network: allow it on PRIVATE networks, or the robot can't reach this PC.

PRIVACY
Your voice is turned into text on this PC. Only the text goes to the AI you picked (or stays on this PC with Ollama). \
Your WiFi password is stored on the robot, not on this PC."""


class Lamp:
    """A status dot plus a word, so a step never looks 'on' just because its dot has a colour."""
    WORDS = {"ok": "READY", "warn": "TO DO", "bad": "PROBLEM", "busy": "WORKING…", "": "NOT SET UP"}

    def __init__(self, parent, pal):
        self.pal = pal
        self.cv = tk.Canvas(parent, width=16, height=16, bg=pal["PANEL"], highlightthickness=0)
        self.dot = self.cv.create_oval(2, 2, 14, 14, fill=pal["PANEL_HI"], outline=pal["EDGE"])
        self.word = tk.Label(parent, text=self.WORDS[""], bg=pal["PANEL"], fg=pal["DIM"], font=("Segoe UI Semibold", 8))

    def set(self, state: str):
        p = self.pal
        colour = {"ok": p["GREEN"], "warn": p["WARN"], "bad": p["BAD"], "busy": p["TEAL"]}.get(state)
        # Only READY is a filled dot; anything else is an outline in its colour
        self.cv.itemconfigure(self.dot, fill=colour if state == "ok" else p["PANEL_HI"], outline=colour or p["EDGE"],
                              width=1 if state == "ok" else 2)
        self.word.configure(text=self.WORDS.get(state, self.WORDS[""]), fg=colour or p["DIM"])


class AITab(tk.Frame):
    def __init__(self, master, pal: dict, app):
        super().__init__(master, bg=pal["BG"])
        self.pal, self.app = pal, app
        self.cfg = app.cfg
        self.server: brain.BrainServer | None = None
        p = pal

        top = tk.Frame(self, bg=p["BG"])
        top.pack(fill="x", padx=18, pady=(10, 6))
        tk.Label(top, text="AI MODE", bg=p["BG"], fg=p["GREEN"], font=("Segoe UI Black", 13)).pack(side="left")
        tk.Label(top, text="real conversation · optional · the robot falls back to its built-in commands",
                 bg=p["BG"], fg=p["DIM"], font=("Segoe UI", 9)).pack(side="left", padx=10, pady=(4, 0))
        app.button(top, "How it works / setup guide", self._show_help, small=True).pack(side="right")

        row = tk.Frame(self, bg=p["BG"])
        row.pack(fill="x", padx=18, pady=(0, 6))
        tk.Label(row, text="Robot port", bg=p["BG"], fg=p["DIM"]).pack(side="left")
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(row, textvariable=self.port_var, state="readonly", style="Hop.TCombobox", width=40)
        self.port_box.pack(side="left", padx=(8, 6))
        app.button(row, "⟳", self.refresh_ports, small=True).pack(side="left")
        tk.Label(row, text="(only for steps 1 and 4 - the brain itself works over WiFi)", bg=p["BG"], fg=p["DIM"],
                 font=("Segoe UI", 8)).pack(side="left", padx=8)

        # Step 1 (WiFi) sits on its own at the top and gates the rest: without it the robot can't reach the brain
        wifi_row = tk.Frame(self, bg=p["BG"])
        wifi_row.pack(fill="x", padx=18)
        self._build_wifi(wifi_row)
        self.gate_lbl = tk.Label(self, text="", bg=p["BG"], fg=p["WARN"], font=("Segoe UI Semibold", 9), anchor="w")
        self.gate_lbl.pack(fill="x", padx=20, pady=(0, 4))

        # Steps 2-4 under it, in order: all four are needed for a conversation
        steps = tk.Frame(self, bg=p["BG"])
        steps.pack(fill="x", padx=18)
        self.gated = steps
        self._build_hearing(steps)
        self._build_thinking(steps)
        self._build_run(steps)

        logf = tk.Frame(self, bg=p["PANEL"], highlightbackground=p["EDGE"], highlightthickness=1)
        logf.pack(fill="both", expand=True, padx=18, pady=(8, 14))
        tk.Label(logf, text="CONVERSATIONS", bg=p["PANEL"], fg=p["GREEN"], font=("Segoe UI Black", 10)).pack(
            anchor="w", padx=10, pady=(6, 0))
        cols_ = ("time", "heard", "reply", "action", "upload", "hear", "think", "secs")
        self.turns = ttk.Treeview(logf, columns=cols_, show="headings", height=3, style="Hop.Treeview")
        for c, w, t in (("time", 70, "Time"), ("heard", 260, "You said"), ("reply", 380, "ButterBot said"),
                        ("action", 130, "Action"), ("upload", 58, "Upload"), ("hear", 50, "Hear"),
                        ("think", 50, "Think"), ("secs", 50, "Total")):
            self.turns.heading(c, text=t, anchor="w")
            self.turns.column(c, width=w, anchor="w", stretch=c in ("heard", "reply"))
        self.turns.tag_configure("bad", foreground=p["BAD"])
        self.turns.pack(fill="both", expand=True, padx=8, pady=6)

        self.refresh_ports()
        self._refresh_thinking_state()
        self._set_gate(bool(self.cfg.get("ai_wifi_ok")))
        if self.cfg.get("ai_autostart") and self.cfg.get("ai_wifi_ok"):
            self.after(800, self.start_brain)

    # --- WiFi gate ------------------------------------------------------------------------------------
    def _set_gate(self, open_: bool):
        """Steps 2-4 only work once the robot has joined WiFi (step 1), so until then they're greyed out."""
        if open_ != bool(self.cfg.get("ai_wifi_ok")):
            self.cfg["ai_wifi_ok"] = open_
            core.save_config(self.cfg)
        self.gate_lbl.configure(text="" if open_ else
                                "🔒  Steps 2-4 unlock once the robot has joined your WiFi: fill in step 1, "
                                "hit Send to robot, then Test robot WiFi.")

        def walk(w):
            for c in w.winfo_children():
                if isinstance(c, ttk.Combobox):
                    if not hasattr(c, "_hop_state"):
                        c._hop_state = str(c.cget("state"))
                    c.configure(state=c._hop_state if open_ else "disabled")
                elif isinstance(c, tk.Entry):
                    c.configure(state="normal" if open_ else "disabled", disabledbackground=self.pal["PANEL"],
                                disabledforeground=self.pal["EDGE"])
                elif isinstance(c, (tk.Button, tk.Checkbutton, tk.Radiobutton)):
                    c.configure(state="normal" if open_ else "disabled", disabledforeground=self.pal["EDGE"])
                elif isinstance(c, tk.Label) and not open_:
                    c._hop_fg = c._hop_fg if hasattr(c, "_hop_fg") else c.cget("fg")
                    c.configure(fg=self.pal["EDGE"])
                elif isinstance(c, tk.Label) and hasattr(c, "_hop_fg"):
                    c.configure(fg=c._hop_fg)
                walk(c)
        walk(self.gated)

    # --- layout helpers -----------------------------------------------------------------------------
    def _card(self, parent, step: str, title: str):
        p = self.pal
        f = tk.Frame(parent, bg=p["PANEL"], highlightbackground=p["EDGE"], highlightthickness=1)
        f.pack(fill="x", pady=(0, 8))
        head = tk.Frame(f, bg=p["PANEL"])
        head.pack(fill="x", padx=10, pady=(8, 2))
        lamp = Lamp(head, p)
        lamp.cv.pack(side="left")
        tk.Label(head, text=f"{step}  {title}", bg=p["PANEL"], fg=p["GREEN"], font=("Segoe UI Black", 11)).pack(
            side="left", padx=6)
        lamp.word.pack(side="right")
        row = tk.Frame(f, bg=p["PANEL"])
        row.pack(fill="x", padx=10, pady=(0, 8))
        body = tk.Frame(row, bg=p["PANEL"])
        body.pack(side="left", anchor="nw")
        status = tk.Label(row, text="", bg=p["PANEL"], fg=p["DIM"], anchor="nw", justify="left", wraplength=360,
                          font=("Segoe UI", 8))
        status.pack(side="left", fill="x", expand=True, anchor="nw", padx=(24, 0))
        return body, lamp, status

    def _entry(self, parent, var, show=None, width=28):
        p = self.pal
        return tk.Entry(parent, textvariable=var, show=show or "", width=width, bg=p["PANEL_HI"], fg=p["TEXT"],
                        insertbackground=p["GREEN"], relief="flat", highlightthickness=1,
                        highlightbackground=p["EDGE"], highlightcolor=p["GREEN"])

    def _label(self, parent, text):
        return tk.Label(parent, text=text, bg=self.pal["PANEL"], fg=self.pal["DIM"], font=("Segoe UI", 9))

    @staticmethod
    def _say(lbl, lamp, text, state):
        lbl.configure(text=text)
        lamp.set(state)

    # --- step 1: WiFi ---------------------------------------------------------------------------------
    def _build_wifi(self, parent):
        body, self.wifi_lamp, self.wifi_status = self._card(parent, "1", "ROBOT WIFI")
        self.ssid_var = tk.StringVar(value=self.cfg.get("ai_ssid", ""))
        self.pass_var = tk.StringVar()
        self._label(body, "WiFi name").grid(row=0, column=0, sticky="w")
        # Editable dropdown: type a name, or press Scan and pick from what the robot can see
        self.ssid_box = ttk.Combobox(body, textvariable=self.ssid_var, width=26, style="Hop.TCombobox")
        self.ssid_box.grid(row=0, column=1, sticky="w", padx=6, pady=2)
        self.app.button(body, "Scan", self.scan_wifi, small=True).grid(row=0, column=2, sticky="w")
        self._label(body, "Password").grid(row=1, column=0, sticky="w")
        self.pass_entry = self._entry(body, self.pass_var, show="•")
        self.pass_entry.grid(row=1, column=1, sticky="w", padx=6, pady=2)
        self.show_pass = tk.BooleanVar(value=False)
        tk.Checkbutton(body, text="show", variable=self.show_pass, bg=self.pal["PANEL"], fg=self.pal["DIM"],
                       selectcolor=self.pal["PANEL_HI"], activebackground=self.pal["PANEL"], highlightthickness=0,
                       command=lambda: self.pass_entry.configure(show="" if self.show_pass.get() else "•")).grid(
            row=1, column=2, sticky="w")
        # Remember per network, in Windows Credential Manager (never in Dimension Hopper's settings file)
        self.remember_pass = tk.BooleanVar(value=bool(self.cfg.get("ai_remember_wifi", True)))
        tk.Checkbutton(body, text="remember on this PC", variable=self.remember_pass, bg=self.pal["PANEL"],
                       fg=self.pal["DIM"], selectcolor=self.pal["PANEL_HI"], activebackground=self.pal["PANEL"],
                       highlightthickness=0, command=self._remember_changed).grid(row=1, column=3, sticky="w", padx=(6, 0))
        self.ssid_box.bind("<<ComboboxSelected>>", lambda e: self._fill_saved_password())
        self.ssid_box.bind("<FocusOut>", lambda e: self._fill_saved_password())
        btns = tk.Frame(body, bg=self.pal["PANEL"])
        btns.grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.app.button(btns, "Send to robot", self.send_wifi, small=True).pack(side="left")
        self.app.button(btns, "Test robot WiFi", self.test_wifi, small=True).pack(side="left", padx=6)
        self.app.button(btns, "Check robot", self.check_robot, small=True).pack(side="left")
        self.wifi_status.configure(text="2.4 GHz networks only. The password goes to the robot over USB; with 'remember' "
                                        "ticked it's also kept in Windows Credential Manager on this PC.")
        # The address the robot is given for the brain (Test robot WiFi and Start send it over USB)
        self.pc_ip_lbl = tk.Label(body, text="", bg=self.pal["PANEL"], fg=self.pal["DIM"], font=("Segoe UI", 8))
        self.pc_ip_lbl.grid(row=3, column=0, columnspan=4, sticky="w", pady=(6, 0))
        self._show_pc_ip()
        self._fill_saved_password()

    def _fill_saved_password(self):
        """Puts the remembered password for the chosen network in the box (only if the box is empty)."""
        if self.pass_var.get():
            return
        saved = brain.load_wifi_password(self.ssid_var.get().strip())
        if saved:
            self.pass_var.set(saved)

    def _remember_changed(self):
        self.cfg["ai_remember_wifi"] = bool(self.remember_pass.get())
        core.save_config(self.cfg)
        if not self.remember_pass.get():
            ssid = self.ssid_var.get().strip()
            brain.forget_wifi_password(ssid)
            self._say(self.wifi_status, self.wifi_lamp,
                      f"Won't remember WiFi passwords" + (f" - the saved one for {ssid} is deleted." if ssid else "."), "")

    def _show_pc_ip(self, robot_ip: str | None = None):
        pc = brain.local_ip()
        text = f"This PC: {pc}:{brain.TALK_PORT} - sent to the robot so it can find the brain"
        if robot_ip:
            same = pc.rsplit(".", 1)[0] == robot_ip.rsplit(".", 1)[0]
            text += (f" · robot: {robot_ip} (same network)" if same else
                     f" · robot: {robot_ip} (different sub-network - fine, it uses this address directly)")
        self.pc_ip_lbl.configure(text=text)

    def _robot_port(self) -> str | None:
        port = self.port_var.get().split(" ", 1)[0]
        if not port:
            self._say(self.wifi_status, self.wifi_lamp, "Pick the robot's USB port above (plug it in and switch it on).",
                      "warn")
            return None
        if self.app.port_in_use(port) or self.app.busy:
            return None
        return port

    def _robot_cmd(self, cmd: str, tag: str, timeout: float, done, status=None):
        port = self._robot_port()
        if not port:
            return

        def work():
            r = core.console_command(port, cmd, tag, timeout_s=timeout)
            self._deliver(r, port, done, status)
        threading.Thread(target=work, daemon=True).start()

    def _deliver(self, r, port: str, done, status=None):
        """Hands a reply to done() on the UI thread - or, if the port itself was the problem, says so instead."""
        problem = core.port_problem(r, port)
        lbl, lamp = status or (self.wifi_status, self.wifi_lamp)
        if problem:
            self.app.ui(lambda: self._say(lbl, lamp, problem, "bad"))
        else:
            self.app.ui(lambda: done(r))

    def _push_brain_address(self, port: str) -> None:
        """Tells the robot this PC's address, so it finds the brain even across sub-networks (worker thread)."""
        ip = brain.local_ip()
        try:
            r = core.console_command(port, f"brain {ip} {brain.TALK_PORT}", "BBWIFI", 4)
        except Exception:  # noqa: BLE001 - the broadcast search still works on simple networks
            r = None
        if r and r.get("brain") == ip:
            self.cfg["ai_robot_brain_ip"] = ip
            core.save_config(self.cfg)
            self.app.ui(self._show_state)

    def send_wifi(self):
        ssid, pw = self.ssid_var.get().strip(), self.pass_var.get()
        if not ssid:
            self._say(self.wifi_status, self.wifi_lamp, "Type the WiFi name first.", "warn")
            return
        if pw and len(pw) < 8:
            self._say(self.wifi_status, self.wifi_lamp, "WiFi passwords are at least 8 characters.", "warn")
            return
        self.cfg["ai_ssid"] = ssid
        core.save_config(self.cfg)
        self._say(self.wifi_status, self.wifi_lamp, "Sending to the robot…", "busy")

        def done(r):
            if r is None:
                self._say(self.wifi_status, self.wifi_lamp, "No answer - the robot needs this release's firmware "
                          "(flash it from the Flash tab) and must be switched on.", "bad")
            elif r.get("result") == "saved" or r.get("configured") == "1":
                if self.remember_pass.get():
                    try:
                        brain.save_wifi_password(ssid, pw)
                    except Exception:  # noqa: BLE001 - remembering is a convenience; the robot has it either way
                        pass
                else:
                    self.pass_var.set("")
                self._say(self.wifi_status, self.wifi_lamp, f"Saved on the robot. Now hit \"Test robot WiFi\".", "ok")
            else:
                self._say(self.wifi_status, self.wifi_lamp, f"The robot refused it ({r.get('why', '?')}).", "bad")
        self._robot_cmd(f"wifi set {ssid.encode().hex()} {pw.encode().hex()}".rstrip(), "BBWIFI", 4, done)

    def scan_wifi(self):
        self._say(self.wifi_status, self.wifi_lamp, "The robot is looking for networks (a few seconds)…", "busy")

        def done(r):
            if r is None:
                self._say(self.wifi_status, self.wifi_lamp, "No answer - the robot needs this release's firmware "
                          "(flash it from the Flash tab) and must be switched on.", "bad")
                return
            if r.get("result") == "busy":
                self._say(self.wifi_status, self.wifi_lamp, f"The robot is busy ({r.get('state')}). Try when it's idle.",
                          "warn")
                return
            if r.get("result") != "scan":
                self._say(self.wifi_status, self.wifi_lamp, "The scan didn't work - try again.", "bad")
                return
            nets = []
            for item in (r.get("nets", "-").split(",") if r.get("nets", "-") != "-" else []):
                try:
                    hx, rssi, open_ = item.rsplit(":", 2)
                    nets.append((bytes.fromhex(hx).decode("utf-8", "replace"), int(rssi), open_ == "1"))
                except ValueError:
                    continue
            if not nets:
                self._say(self.wifi_status, self.wifi_lamp, "The robot can't see any networks. It only sees 2.4 GHz "
                          "WiFi - check your router has 2.4 GHz switched on.", "warn")
                return
            self.ssid_box.configure(values=[n for n, _, _ in nets])
            if not self.ssid_var.get().strip():
                self.ssid_var.set(nets[0][0])
            def bars(rssi):
                return "strong" if rssi > -60 else ("ok" if rssi > -72 else "weak")
            listing = ",  ".join(f"{n} ({bars(r_)}{', open' if o else ''})" for n, r_, o in nets[:8])
            self._say(self.wifi_status, self.wifi_lamp, f"Found {len(nets)}: {listing}. Pick yours in WiFi name, "
                      "type the password and hit Send to robot.", "warn")
        self._robot_cmd("wifi scan", "BBWIFI", 15, done)

    def test_wifi(self):
        self._say(self.wifi_status, self.wifi_lamp, "The robot is joining the WiFi (up to 15 s)…", "busy")

        def done(r):
            if r is None:
                self._say(self.wifi_status, self.wifi_lamp, "No answer from the robot.", "bad")
                return
            res = r.get("result")
            if res == "connected":
                brain_ip = r.get("brain", "none")
                rssi = int(r.get("rssi", "0") or 0)
                signal = "strong" if rssi > -60 else ("ok" if rssi > -72 else "weak")
                # WiFi itself is done either way; finding the brain is step 4's business
                text = (f"Connected: robot at {r.get('ip')}, {signal} signal ({rssi} dBm), {r.get('ram')} KB RAM free. "
                        + (f"It also found the brain on {brain_ip}." if brain_ip != "none" else "WiFi is done."))
                self._say(self.wifi_status, self.wifi_lamp, text, "ok")
                self._show_pc_ip(r.get("ip"))
                self._set_gate(True)
            elif res == "busy":
                self._say(self.wifi_status, self.wifi_lamp, f"The robot is busy ({r.get('state')}). Try when it's idle.",
                          "warn")
            else:
                why = {"network_not_found": "network not found (is it 2.4 GHz and in range?)",
                       "wrong_password": "wrong password", "not_configured": "no WiFi saved yet - Send to robot first",
                       "timeout": "timed out"}.get(r.get("why", ""), r.get("why", "failed"))
                self._say(self.wifi_status, self.wifi_lamp, f"Couldn't connect: {why}.", "bad")
        port = self._robot_port()
        if not port:
            return

        def work():
            self._push_brain_address(port)
            r = core.console_command(port, "wifi test", "BBWIFI", timeout_s=20)
            self._deliver(r, port, done)
        threading.Thread(target=work, daemon=True).start()

    def check_robot(self):
        def done(r):
            if r is None:
                self._say(self.wifi_status, self.wifi_lamp, "No answer - needs this release's robot firmware.", "bad")
                return
            ssid = bytes.fromhex(r["ssid_hex"]).decode("utf-8", "replace") if r.get("ssid_hex", "-") != "-" else ""
            text = (f"Robot WiFi: {ssid or 'not set'} · AI mode {r.get('ai', '?').upper()}"
                    + (f" · last brain {r['brain']}" if r.get("brain", "none") != "none" else ""))
            self._say(self.wifi_status, self.wifi_lamp, text, "ok" if ssid else "warn")
            self._report_last_problem(r)
            if not ssid:
                self._set_gate(False)
            self._set_ai_label(r.get("ai") == "on")
        self._robot_cmd("wifi", "BBWIFI", 4, done)

    # --- step 2: hearing ------------------------------------------------------------------------------
    def _build_hearing(self, parent):
        body, self.hear_lamp, self.hear_status = self._card(parent, "2", "HEARING (SPEECH TO TEXT)")
        self._label(body, "Whisper model").grid(row=0, column=0, sticky="w")
        names = [n for n, _ in brain.WHISPER_MODELS]
        self.whisper_var = tk.StringVar(value=self.cfg.get("ai_whisper", "base.en"))
        box = ttk.Combobox(body, textvariable=self.whisper_var, values=names, state="readonly", width=12,
                           style="Hop.TCombobox")
        box.grid(row=0, column=1, sticky="w", padx=6)
        self.app.button(body, "Load", self.load_whisper, small=True).grid(row=0, column=2, sticky="w")
        self.app.button(body, "Source…", self.pick_whisper_source, small=True).grid(row=0, column=3, sticky="w", padx=6)
        box.bind("<<ComboboxSelected>>", lambda e: self._show_whisper_source())
        self.source_lbl = tk.Label(body, text="", bg=self.pal["PANEL"], fg=self.pal["DIM"], font=("Segoe UI", 8),
                                   anchor="w", justify="left", wraplength=560)
        self.source_lbl.grid(row=1, column=0, columnspan=4, sticky="w", pady=(4, 0))
        self._show_whisper_source()
        self.hear_status.configure(text="Runs on this PC, so your voice never leaves it. "
                                        + "  ·  ".join(d for _, d in brain.WHISPER_MODELS))

    def _show_whisper_source(self):
        self.source_lbl.configure(text=brain.describe_whisper_source(self.whisper_var.get(),
                                                                     self.cfg.get("ai_whisper_source", "")))

    def pick_whisper_source(self):
        """Where the speech model comes from: Hugging Face (default), a mirror, or a folder already on this PC."""
        p = self.pal
        dlg = tk.Toplevel(self, bg=p["BG"])
        dlg.title("Whisper model source")
        dlg.transient(self.winfo_toplevel())
        dlg.grab_set()
        cur = self.cfg.get("ai_whisper_source", "")
        kind = tk.StringVar(value="hf" if not cur else ("mirror" if cur.startswith(("http://", "https://")) else "folder"))
        mirror = tk.StringVar(value=cur if kind.get() == "mirror" else "https://")
        folder = tk.StringVar(value=cur if kind.get() == "folder" else "")
        name = self.whisper_var.get()
        tk.Label(dlg, text="WHERE THE SPEECH MODEL COMES FROM", bg=p["BG"], fg=p["GREEN"],
                 font=("Segoe UI Black", 10)).grid(row=0, column=0, columnspan=3, sticky="w", padx=14, pady=(12, 6))

        def radio(r, val, text):
            tk.Radiobutton(dlg, text=text, value=val, variable=kind, bg=p["BG"], fg=p["TEXT"], selectcolor=p["PANEL_HI"],
                           activebackground=p["BG"], highlightthickness=0, font=("Segoe UI", 9), anchor="w",
                           justify="left").grid(row=r, column=0, columnspan=3, sticky="w", padx=14, pady=(6, 0))

        def note(r, text):
            tk.Label(dlg, text=text, bg=p["BG"], fg=p["DIM"], font=("Segoe UI", 8), justify="left", wraplength=520).grid(
                row=r, column=0, columnspan=3, sticky="w", padx=36)

        radio(1, "hf", "Hugging Face (recommended)")
        note(2, f"Downloads huggingface.co/{brain.whisper_repo(name)} once (public, MIT licence), "
                f"then keeps it in {brain.whisper_cache_dir()}.")
        radio(3, "mirror", "A Hugging Face mirror")
        self._entry(dlg, mirror, width=50).grid(row=4, column=0, columnspan=2, sticky="w", padx=36, pady=2)
        note(5, "For networks where huggingface.co is blocked, e.g. a company or regional mirror.")
        radio(6, "folder", "A folder on this PC (offline)")
        self._entry(dlg, folder, width=50).grid(row=7, column=0, columnspan=2, sticky="w", padx=36, pady=2)

        def browse():
            from tkinter import filedialog
            d = filedialog.askdirectory(parent=dlg, title="Folder with model.bin")
            if d:
                folder.set(d)
                kind.set("folder")
        self.app.button(dlg, "Browse…", browse, small=True).grid(row=7, column=2, sticky="w")
        note(8, "A faster-whisper model folder (model.bin, config.json, tokenizer.json, vocabulary...), "
                "e.g. copied from another PC's cache.")
        err = tk.Label(dlg, text="", bg=p["BG"], fg=p["WARN"], font=("Segoe UI", 8))
        err.grid(row=9, column=0, columnspan=3, sticky="w", padx=14, pady=(8, 0))

        def save():
            k = kind.get()
            if k == "mirror":
                v = mirror.get().strip()
                if not v.startswith(("http://", "https://")) or len(v) < 12:
                    err.configure(text="Type the mirror's address, starting with https://")
                    return
            elif k == "folder":
                v = folder.get().strip()
                if not v:
                    err.configure(text="Pick the folder.")
                    return
            else:
                v = ""
            self.cfg["ai_whisper_source"] = v
            core.save_config(self.cfg)
            self._show_whisper_source()
            self._say(self.hear_status, self.hear_lamp, "Source changed - hit Load to use it.", "warn")
            dlg.destroy()
        row = tk.Frame(dlg, bg=p["BG"])
        row.grid(row=10, column=0, columnspan=3, sticky="e", padx=14, pady=12)
        self.app.button(row, "Cancel", dlg.destroy, small=True).pack(side="right")
        self.app.button(row, "Save", save, small=True).pack(side="right", padx=6)

    def load_whisper(self):
        name = self.whisper_var.get()
        self.cfg["ai_whisper"] = name
        core.save_config(self.cfg)
        self._say(self.hear_status, self.hear_lamp, f"Loading {name} - downloads once (can take a minute)…", "busy")
        ears = self.server.ears if self.server else brain.Ears()
        self._ears = ears

        def work():
            try:
                t0 = time.time()
                ears.load(name, source=self.cfg.get("ai_whisper_source", ""))
                msg = f"Whisper {name} ready ({time.time() - t0:.0f} s)."
                self.app.ui(lambda: self._say(self.hear_status, self.hear_lamp, msg, "ok"))
            except Exception as e:  # noqa: BLE001
                self.app.ui(lambda: self._say(self.hear_status, self.hear_lamp, f"Couldn't load Whisper: {e}", "bad"))
        threading.Thread(target=work, daemon=True).start()

    # --- step 3: thinking -----------------------------------------------------------------------------
    def _build_thinking(self, parent):
        p = self.pal
        body, self.think_lamp, self.think_status = self._card(parent, "3", "THINKING (THE AI)")
        self.provider_var = tk.StringVar(value=self.cfg.get("ai_provider", "claude"))
        for i, (val, text) in enumerate((("claude", "Claude - your own API key"), ("ollama", "Ollama - free, on this PC"))):
            tk.Radiobutton(body, text=text, value=val, variable=self.provider_var, command=self._refresh_thinking_state,
                           bg=p["PANEL"], fg=p["TEXT"], selectcolor=p["PANEL_HI"], activebackground=p["PANEL"],
                           highlightthickness=0, font=("Segoe UI", 9)).grid(row=0, column=i, sticky="w")

        self.claude_frame = tk.Frame(body, bg=p["PANEL"])
        self.claude_frame.grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self._label(self.claude_frame, "API key").grid(row=0, column=0, sticky="w")
        self.key_var = tk.StringVar()
        self._entry(self.claude_frame, self.key_var, show="•", width=34).grid(row=0, column=1, sticky="w", padx=6)
        self.app.button(self.claude_frame, "Save", self.save_key, small=True).grid(row=0, column=2, sticky="w")
        self.app.button(self.claude_frame, "Get a key", lambda: webbrowser.open("https://console.anthropic.com/"),
                        small=True).grid(row=0, column=3, sticky="w", padx=6)
        self._label(self.claude_frame, "Model").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.model_names = dict(brain.CLAUDE_MODELS)
        self.model_var = tk.StringVar(value=self.model_names.get(self.cfg.get("ai_claude_model", "claude-opus-5-5"),
                                                                 brain.CLAUDE_MODELS[0][1]))
        ttk.Combobox(self.claude_frame, textvariable=self.model_var, values=list(self.model_names.values()),
                     state="readonly", width=34, style="Hop.TCombobox").grid(row=1, column=1, columnspan=3, sticky="w",
                                                                              padx=6, pady=(4, 0))

        self.ollama_frame = tk.Frame(body, bg=p["PANEL"])
        self._label(self.ollama_frame, "Model").grid(row=0, column=0, sticky="w")
        self.ollama_var = tk.StringVar(value=self.cfg.get("ai_ollama_model", "llama3.2"))
        self.ollama_box = ttk.Combobox(self.ollama_frame, textvariable=self.ollama_var, width=22, style="Hop.TCombobox")
        self.ollama_box.grid(row=0, column=1, sticky="w", padx=6)
        self.app.button(self.ollama_frame, "Check", self.check_ollama, small=True).grid(row=0, column=2, sticky="w")
        self.app.button(self.ollama_frame, "Get Ollama", lambda: webbrowser.open("https://ollama.com/download"),
                        small=True).grid(row=0, column=3, sticky="w", padx=6)

        trow = tk.Frame(body, bg=p["PANEL"])
        trow.grid(row=0, column=2, sticky="w", padx=(24, 0))
        self.try_var = tk.StringVar(value="Hey ButterBot, what is your purpose?")
        self._entry(trow, self.try_var, width=34).pack(side="left")
        self.app.button(trow, "Try it", self.try_it, small=True).pack(side="left", padx=6)

    def _refresh_thinking_state(self):
        claude = self.provider_var.get() == "claude"
        (self.ollama_frame.grid_remove if claude else self.claude_frame.grid_remove)()
        (self.claude_frame if claude else self.ollama_frame).grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.cfg["ai_provider"] = self.provider_var.get()
        core.save_config(self.cfg)
        if claude:
            has = bool(brain.load_api_key())
            self._say(self.think_status, self.think_lamp,
                      "API key saved (in Windows Credential Manager)." if has else
                      "Paste an API key from console.anthropic.com and hit Save. Pay-as-you-go; a chat costs a "
                      "fraction of a cent. Opus is the smartest; Haiku answers fastest.", "ok" if has else "warn")
        else:
            self._say(self.think_status, self.think_lamp, "Free and private, but needs a reasonably fast PC. Install "
                      "Ollama, run  ollama pull llama3.2  once, then hit Check.", "")
            self.check_ollama()

    def save_key(self):
        key = self.key_var.get().strip()
        if key and not key.startswith("sk-ant-"):
            self._say(self.think_status, self.think_lamp, "That doesn't look like an Anthropic API key (sk-ant-…).",
                      "warn")
            return
        try:
            brain.save_api_key(key)
        except Exception as e:  # noqa: BLE001
            self._say(self.think_status, self.think_lamp, f"Couldn't store the key: {e}", "bad")
            return
        self.key_var.set("")
        self._refresh_thinking_state()
        if not key:
            self._say(self.think_status, self.think_lamp, "Key removed.", "warn")

    def check_ollama(self):
        def work():
            models = brain.ollama_models()
            def ui():
                if models is None:
                    self._say(self.think_status, self.think_lamp, "Ollama isn't running. Install it from ollama.com "
                              "(it starts by itself), then hit Check again.", "bad")
                elif not models:
                    self._say(self.think_status, self.think_lamp, "Ollama is running but has no models. In a terminal "
                              "run:  ollama pull llama3.2", "warn")
                else:
                    self.ollama_box.configure(values=models)
                    if self.ollama_var.get() not in models:
                        self.ollama_var.set(models[0])
                    self._say(self.think_status, self.think_lamp, f"Ollama ready: {', '.join(models[:6])}", "ok")
            self.app.ui(ui)
        threading.Thread(target=work, daemon=True).start()

    def settings(self) -> brain.BrainSettings:
        rev = {v: k for k, v in self.model_names.items()}
        s = brain.BrainSettings(provider=self.provider_var.get(),
                                claude_model=rev.get(self.model_var.get(), "claude-opus-5-5"),
                                ollama_model=self.ollama_var.get().strip() or "llama3.2",
                                whisper_model=self.whisper_var.get(),
                                whisper_source=self.cfg.get("ai_whisper_source", ""))
        self.cfg.update(ai_provider=s.provider, ai_claude_model=s.claude_model, ai_ollama_model=s.ollama_model,
                        ai_whisper=s.whisper_model)
        core.save_config(self.cfg)
        return s

    def try_it(self):
        text = self.try_var.get().strip()
        if not text:
            return
        s = self.settings()
        self._say(self.think_status, self.think_lamp, "Thinking…", "busy")

        def work():
            t0 = time.time()
            try:
                thinker = brain.Thinker(s, brain.load_actions(_guide_path()))
                r = thinker.think(text)
                msg = (f"ButterBot: \"{r['say'] or '…'}\"" + (f"  ->  action: {r['action']}" if r["action"] else "")
                       + f"   ({time.time() - t0:.1f} s)")
                self.app.ui(lambda: self._say(self.think_status, self.think_lamp, msg, "ok"))
            except Exception as e:  # noqa: BLE001
                self.app.ui(lambda: self._say(self.think_status, self.think_lamp, str(e), "bad"))
        threading.Thread(target=work, daemon=True).start()

    # --- step 4: run ----------------------------------------------------------------------------------
    def _build_run(self, parent):
        p = self.pal
        body, self.run_lamp, self.run_status = self._card(parent, "4", "TURN IT ON")
        self.brain_btn = self.app.button(body, "▶ Start AI conversations", self.toggle_brain)
        self.brain_btn.grid(row=0, column=0, sticky="w")
        self.auto_var = tk.BooleanVar(value=bool(self.cfg.get("ai_autostart", False)))
        tk.Checkbutton(body, text="Start automatically when Dimension Hopper opens", variable=self.auto_var,
                       command=self._save_auto, bg=p["PANEL"], fg=p["TEXT"], selectcolor=p["PANEL_HI"],
                       activebackground=p["PANEL"], highlightthickness=0, font=("Segoe UI", 9)).grid(
            row=0, column=1, sticky="w", padx=10)
        # The robot's own setting (saved on it). Rarely changed: on = try the brain first, off = built-in commands only
        self.robot_ai_var = tk.BooleanVar(value=bool(self.cfg.get("ai_robot_ai", True)))
        tk.Checkbutton(body, text="Robot uses AI when the brain is available (a setting on the robot, sent over USB)",
                       variable=self.robot_ai_var, command=self._robot_ai_changed, bg=p["PANEL"], fg=p["DIM"],
                       selectcolor=p["PANEL_HI"], activebackground=p["PANEL"], highlightthickness=0,
                       font=("Segoe UI", 8)).grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.state_lbl = tk.Label(body, text="", bg=p["PANEL"], fg=p["DIM"], font=("Segoe UI Semibold", 9), anchor="w")
        self.state_lbl.grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.robot_ai: tuple[bool, str] | None = None   # (on, "HH:MM") as last reported by the robot
        self._show_state()
        self.run_status.configure(text="Starts the brain on this PC and, if the robot is plugged in, makes sure it's "
                                       "set up to use it. The brain only runs while this app is open; when it's off, "
                                       "the robot says so and uses its built-in commands.")

    LAST_PROBLEM = {
        "network_not_found": "it couldn't find your WiFi network - check the name in step 1 (2.4 GHz only)",
        "not_configured": "it has no WiFi saved - fill in step 1",
        "wrong_password": "the WiFi password was rejected - re-send it in step 1",
        "timeout": "joining the WiFi timed out - try Test robot WiFi in step 1",
        "failed": "WiFi didn't start - try Test robot WiFi in step 1",
        "brain_not_found": "it couldn't find the brain - press Start with the robot plugged in",
        "brain_unreachable": "it couldn't reach the brain - press Start with the robot plugged in",
        "send_failed": "the upload to the brain broke off - usually a one-off",
        "no_reply": "the brain didn't reply - check step 3 works with Try it",
    }

    def _report_last_problem(self, r: dict | None):
        """When the robot is plugged in, say what went wrong on its last AI listen (if anything)."""
        last = (r or {}).get("last", "none")
        if last and last != "none":
            why = self.LAST_PROBLEM.get(last, last.replace("_", " "))
            self._say(self.run_status, self.run_lamp, f"Last time the robot tried the brain, {why}.", "warn")

    def _show_state(self):
        running = bool(self.server and self.server.running)
        ready = running and self.server.ready.is_set()
        brain_txt = "ready" if ready else ("warming up…" if running else "stopped")
        if self.robot_ai is None:
            robot_txt = "plug it in to check its AI setting"
        else:
            robot_txt = f"AI {'on' if self.robot_ai[0] else 'off'} (checked {self.robot_ai[1]})"
        told = self.cfg.get("ai_robot_brain_ip", "")
        now = brain.local_ip()
        if told and told != now:
            self.state_lbl.configure(text=f"This PC's address changed since the robot was set up (was {told}, now {now}). "
                                          "Plug the robot in once and press Start again.", fg=self.pal["WARN"])
            return
        good = ready and self.robot_ai is not None and self.robot_ai[0]
        self.state_lbl.configure(text=f"Brain: {brain_txt}   ·   Robot: {robot_txt}"
                                      + ("   →   talk to it!" if good else ""),
                                 fg=self.pal["GREEN"] if good else self.pal["DIM"])

    def _save_auto(self):
        self.cfg["ai_autostart"] = bool(self.auto_var.get())
        core.save_config(self.cfg)

    def _set_ai_label(self, on: bool):
        self.robot_ai = (on, time.strftime("%H:%M"))
        self._show_state()

    def _robot_ai_changed(self):
        self.cfg["ai_robot_ai"] = bool(self.robot_ai_var.get())
        core.save_config(self.cfg)
        self.set_ai(self.robot_ai_var.get())

    def set_ai(self, on: bool, quiet: bool = False):
        port = self.port_var.get().split(" ", 1)[0]
        if quiet:
            # From Start: only if the robot is plugged in and the port is free; otherwise it keeps its last setting
            if not port or not core.port_exists(port) or port in self.app.held_ports() or self.app.busy:
                return
        else:
            if not port:
                self._say(self.run_status, self.run_lamp, "Plug the robot in and pick its USB port at the top first.",
                          "warn")
                return
            if self.app.port_in_use(port) or self.app.busy:
                return

        def work():
            if on:
                self._push_brain_address(port)
            r = core.console_command(port, "ai on" if on else "ai off", "BBWIFI", 4)
            problem = core.port_problem(r, port)
            if problem:
                if not quiet:
                    self.app.ui(lambda: self._say(self.run_status, self.run_lamp, problem, "bad"))
                return
            if r is None and quiet:
                return  # not the robot on that port, or switched off - nothing to report from Start

            def ui():
                if r is None:
                    self._say(self.run_status, self.run_lamp, "No answer from the robot (needs this release's firmware).",
                              "bad")
                else:
                    self._set_ai_label(r.get("ai") == "on")
                    self._report_last_problem(r)
                    if r.get("configured") != "1" and on:
                        self._say(self.run_status, self.run_lamp, "AI mode is on, but the robot has no WiFi saved yet "
                                  "(step 1).", "warn")
            self.app.ui(ui)
        threading.Thread(target=work, daemon=True).start()

    def toggle_brain(self):
        if self.server and self.server.running:
            self.server.stop()
            self.brain_btn.configure(text="▶ Start AI conversations")
            self._show_state()
            return
        self.start_brain()

    def start_brain(self):
        s = self.settings()
        if s.provider == "claude" and not brain.load_api_key():
            self._say(self.run_status, self.run_lamp, "Save a Claude API key in step 3 first (or pick Ollama).", "warn")
            return
        try:
            self.server = brain.BrainServer(s, _guide_path(), lambda k, v: self.app.ui(lambda: self._event(k, v)),
                                            log_path=core.APP_DIR / "brain_log.jsonl")
            self.server.start()
        except OSError as e:
            self._say(self.run_status, self.run_lamp, f"Couldn't start: {e} (is another Dimension Hopper running?)", "bad")
            self.server = None
            return
        self.brain_btn.configure(text="■ Stop AI conversations")
        self._show_state()
        # If the robot is plugged in: make sure it's set up to use the brain (and has this PC's current address)
        if self.robot_ai_var.get():
            self.set_ai(True, quiet=True)

    def _event(self, kind: str, value):
        if kind == "status":
            running = bool(self.server and self.server.running)
            # READY only once warm-up has finished; until then the step shows WORKING…
            state = ("ok" if self.server.ready.is_set() else "busy") if running else ""
            self._say(self.run_status, self.run_lamp, str(value), state)
            self._show_state()
            if "Whisper" in str(value):
                self._say(self.hear_status, self.hear_lamp, str(value), "ok" if "ready" in str(value) else "busy")
        elif kind == "warming":
            self._show_state()
            if value:
                self._say(self.run_status, self.run_lamp, "Warming up the brain - wait for READY before talking…",
                          "busy")
        elif kind == "error":
            self._say(self.run_status, self.run_lamp, str(value), "bad")
        elif kind == "robot":
            self.run_status.configure(text=f"Brain running on {brain.local_ip()}:{brain.TALK_PORT} · robot seen at {value}")
        elif kind == "turn":
            t: brain.Turn = value
            self.turns.insert("", "end", values=(time.strftime("%H:%M:%S"), t.heard or "(nothing)",
                                                 t.error or t.say, t.action, f"{t.upload_s:.1f}", f"{t.hear_s:.1f}",
                                                 f"{t.think_s:.1f}", f"{t.seconds:.1f}"),
                              tags=("bad",) if t.error else ())
            self.turns.see(self.turns.get_children()[-1])

    def _show_help(self):
        p = self.pal
        dlg = tk.Toplevel(self, bg=p["BG"])
        dlg.title("AI mode - setup guide")
        dlg.transient(self.winfo_toplevel())
        txt = tk.Text(dlg, wrap="word", width=96, height=34, bg=p["PANEL"], fg=p["TEXT"], relief="flat",
                      font=("Segoe UI", 10), padx=14, pady=12)
        txt.insert("1.0", HELP)
        txt.configure(state="disabled")
        txt.pack(fill="both", expand=True, padx=12, pady=12)
        self.app.button(dlg, "Close", dlg.destroy).pack(anchor="e", padx=12, pady=(0, 12))

    # --- misc ---------------------------------------------------------------------------------------------
    def refresh_ports(self):
        ports = core.list_ports()
        self.port_box.configure(values=[pt.label() for pt in ports])
        if not self.port_var.get():
            usb = [pt for pt in ports if pt.is_usb]
            if usb:
                self.port_var.set(usb[0].label())

    def shutdown(self):
        if self.server and self.server.running:
            self.server.stop()


def _guide_path() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / "guide_data.json"
