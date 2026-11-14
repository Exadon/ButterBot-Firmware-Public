"""
Dimension Hopper - hover tooltips. Every button made with App.button() gets one from TIPS (keyed by its label), so a
new button without an entry shows up in the self-test instead of silently having no help.
"""
from __future__ import annotations

import tkinter as tk

TIPS = {
    # Flash tab
    "⟳  Scan dimensions": "Look for the robot and the controller on USB and identify which is which.",
    "Update both  ▶▶": "Flash the robot and the controller one after the other with the selected firmware.",
    "BEAM IT  ▶": "Flash this board with the selected firmware (only the parts that changed, if that box is ticked).",
    "Probe": "Read the chip and flash size. Puts the board into its bootloader - press Wake (or power-cycle) after.",
    "Status": "Read battery, charging, link, firmware and health from the running board, without restarting it.",
    "Wake": "Restart a board that's sitting in its bootloader (after a probe or backup) back into its firmware.",
    "Forget pairing": "Make this board forget every robot/controller it paired with, so the two pair fresh.",
    "Repos…": "Choose which GitHub repositories firmware releases are offered from.",
    "Use this fork's build": "Flash the firmware built on this PC (the fork's build folder) instead of a downloaded release.",
    "Backups folder": "Open the folder where Dimension Hopper keeps the flash backups it makes before updating.",
    "Add": "Add this repository to the list.",
    "Remove selected": "Remove the selected repository from the list.",
    "Done": "Close this window.",
    # Diagnostics / Console
    "⟳": "Refresh the list of USB ports.",
    "Connect": "Open this USB port to watch the board live. Only one tab or program can use a port at a time.",
    "Disconnect": "Close this USB port again (the board keeps running).",
    "Send": "Send the typed command to the board over USB.",
    "Clear": "Clear the log on screen (the board isn't affected).",
    "Save log…": "Save everything shown in the log to a text file.",
    "▶ Start camera": "Show what the robot's camera sees (about 1-2 frames a second over USB). Works while the robot "
                      "is idle.",
    "■ Stop camera": "Stop the camera view.",
    "▶ Mirror screen": "Show the controller's screen here, live, exactly as the controller shows it. Only what "
                       "changes is sent over USB, so menus and the stick keep up.",
    "■ Stop mirror": "Stop mirroring the controller's screen.",
    "Save screenshot…": "Save the controller's screen as a PNG picture (4x size, sharp pixels).",
    "Save snapshot…": "Save the current camera frame as a picture.",
    # Personalize
    "Find robot": "Look through the USB ports for the robot.",
    "▶ Hear the current voice": "Have the robot say a sample line in the voice it's set to now.",
    # AI tab
    "How it works / setup guide": "Step-by-step setup for AI conversations, and how it all fits together.",
    "Scan": "Ask the robot which WiFi networks it can see (2.4 GHz only - that's all it can use).",
    "Send to robot": "Save the WiFi name and password on the robot (over USB).",
    "Test robot WiFi": "Have the robot join the WiFi now and look for the brain on this PC. Takes up to ~20 s.",
    "Check robot": "Show which WiFi the robot has saved, its AI setting, and what went wrong last time (if anything).",
    "Load": "Load the speech-to-text model on this PC (downloads it the first time).",
    "Source…": "Choose where the speech model comes from: Hugging Face, a mirror, or a folder on this PC.",
    "Save": "Save what you entered.",
    "Get a key": "Open console.anthropic.com in your browser to create a Claude API key.",
    "Check": "See whether Ollama is running and which AI models it has installed.",
    "Get Ollama": "Open ollama.com in your browser to download Ollama (free, runs on this PC).",
    "Try it": "Send the typed message to the AI and show its reply - no robot needed.",
    "▶ Start AI conversations": "Start the brain on this PC. If the robot is plugged in, also set it up to use it.",
    "■ Stop AI conversations": "Stop the brain. If the robot is plugged in, its AI is switched off too, so it simply uses "
                               "its built-in commands.",
    "Browse…": "Pick a folder on this PC.",
    "Cancel": "Close without changing anything.",
    "Close": "Close this window.",
}


class Tooltip:
    """Shows a small hint under a widget after the pointer rests on it briefly. The text can change later (set())."""
    DELAY_MS = 450

    def __init__(self, widget: tk.Widget, text: str):
        self.widget, self.text = widget, text
        self.tip: tk.Toplevel | None = None
        self.job = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def set(self, text: str):
        self.text = text

    def _schedule(self, _e=None):
        self._cancel()
        if self.text:
            self.job = self.widget.after(self.DELAY_MS, self._show)

    def _cancel(self):
        if self.job:
            self.widget.after_cancel(self.job)
            self.job = None

    def _show(self):
        if self.tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 8
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, bg="#0d1a10", fg="#cfe9d3", relief="solid", bd=1, padx=8, pady=4,
                 font=("Segoe UI", 9), wraplength=360, justify="left").pack()

    def _hide(self, _e=None):
        self._cancel()
        if self.tip:
            self.tip.destroy()
            self.tip = None


def attach(widget: tk.Widget, label: str) -> Tooltip:
    """The tooltip for a button, looked up by its label."""
    tip = Tooltip(widget, TIPS.get(label, ""))
    widget._hop_tip = tip  # so a button whose label changes (Start/Stop) can update it
    return tip


def relabel(widget: tk.Widget, label: str):
    """Change a button's label and its tooltip together."""
    widget.configure(text=label)
    tip = getattr(widget, "_hop_tip", None)
    if tip:
        tip.set(TIPS.get(label, ""))
