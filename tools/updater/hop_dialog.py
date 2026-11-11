"""
Themed replacement for tkinter.messagebox: dark panel, portal-green title, the app's buttons, an icon per kind,
and an optional monospace details block. Modal, centred on the main window, Enter = default button,
Esc = cancel button.
"""
from __future__ import annotations

import tkinter as tk


class HopDialog(tk.Toplevel):
    KINDS = ("info", "question", "warning", "error")

    def __init__(self, parent: tk.Tk, pal: dict, make_button, kind: str, title: str, message: str,
                 details: list[str] | None = None, buttons: tuple[str, ...] = ("OK",), default: int = 0,
                 cancel: int | None = None):
        super().__init__(parent, bg=pal["EDGE"])
        self.pal = pal
        self.result: int | None = cancel
        self.withdraw()
        self.overrideredirect(True)  # no Windows title bar; the dialog draws its own
        self.transient(parent)

        p = pal
        accent = {"warning": p["WARN"], "error": p["BAD"]}.get(kind, p["GREEN"])
        body = tk.Frame(self, bg=p["PANEL"], highlightbackground=accent, highlightthickness=1)
        body.pack(fill="both", expand=True, padx=1, pady=1)

        # title bar (also lets you drag the dialog)
        bar = tk.Frame(body, bg=p["PANEL_HI"])
        bar.pack(fill="x")
        tk.Label(bar, text="  DIMENSION HOPPER", bg=p["PANEL_HI"], fg=p["DIM"], font=("Segoe UI Semibold", 8)).pack(
            side="left", pady=4)
        close = tk.Label(bar, text=" ✕ ", bg=p["PANEL_HI"], fg=p["DIM"], font=("Segoe UI", 10), cursor="hand2")
        close.pack(side="right")
        close.bind("<Button-1>", lambda e: self._finish(cancel))
        for w in (bar,):
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)

        main = tk.Frame(body, bg=p["PANEL"])
        main.pack(fill="both", expand=True, padx=18, pady=(14, 8))
        icon = tk.Canvas(main, width=46, height=46, bg=p["PANEL"], highlightthickness=0)
        icon.pack(side="left", anchor="n", padx=(0, 14))
        self._draw_icon(icon, kind)

        text = tk.Frame(main, bg=p["PANEL"])
        text.pack(side="left", fill="both", expand=True)
        tk.Label(text, text=title, bg=p["PANEL"], fg=accent, font=("Segoe UI Black", 14), anchor="w",
                 justify="left").pack(fill="x")
        tk.Label(text, text=message, bg=p["PANEL"], fg=p["TEXT"], font=("Segoe UI", 10), anchor="w", justify="left",
                 wraplength=440).pack(fill="x", pady=(4, 0))
        if details:
            box = tk.Frame(text, bg="#040705", highlightbackground=p["EDGE"], highlightthickness=1)
            box.pack(fill="x", pady=(10, 0))
            for line in details:
                tk.Label(box, text=line, bg="#040705", fg=p["LIME"] if not line.startswith(" ") else p["TEXT"],
                         font=("Cascadia Mono", 9), anchor="w", justify="left").pack(fill="x", padx=10, pady=1)

        row = tk.Frame(body, bg=p["PANEL"])
        row.pack(fill="x", padx=18, pady=(6, 14))
        for i in reversed(range(len(buttons))):
            b = make_button(row, buttons[i], lambda i=i: self._finish(i), small=(i != default))
            b.pack(side="right", padx=(8, 0))
            if i == default:
                b.focus_set()

        self.bind("<Return>", lambda e: self._finish(default))
        self.bind("<Escape>", lambda e: self._finish(cancel))

        # centre over the main window and show modally
        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        self.geometry(f"+{px + (pw - w) // 2}+{py + (ph - h) // 3}")
        self.deiconify()
        self.lift()
        self.grab_set()
        self.focus_force()

    def _draw_icon(self, cv: tk.Canvas, kind: str):
        p = self.pal
        if kind == "warning":
            cv.create_polygon(23, 4, 44, 42, 2, 42, fill="", outline=p["WARN"], width=3)
            cv.create_text(23, 29, text="!", fill=p["WARN"], font=("Segoe UI Black", 16))
        elif kind == "error":
            cv.create_oval(3, 3, 43, 43, outline=p["BAD"], width=3)
            cv.create_line(15, 15, 31, 31, fill=p["BAD"], width=4)
            cv.create_line(31, 15, 15, 31, fill=p["BAD"], width=4)
        else:
            # a little portal: rings + swirl + core
            for r, col, w in ((21, p["EDGE"], 3), (19, p["GREEN"], 2), (15, p["LIME"], 2)):
                cv.create_oval(23 - r, 23 - r * 0.92, 23 + r, 23 + r * 0.92, outline=col, width=w)
            for k in range(5):
                a = k * 72
                cv.create_arc(23 - 11, 23 - 10, 23 + 11, 23 + 10, start=a, extent=40, style="arc",
                              outline=p["TEAL"], width=2)
            cv.create_oval(19, 19, 27, 27, fill=p["GREEN"], outline="")
            if kind == "question":
                cv.create_text(23, 23, text="?", fill=p["BG"], font=("Segoe UI Black", 8))

    def _drag_start(self, e):
        self._dx, self._dy = e.x_root - self.winfo_x(), e.y_root - self.winfo_y()

    def _drag_move(self, e):
        self.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def _finish(self, value):
        self.result = value
        self.grab_release()
        self.destroy()

    def run(self) -> int | None:
        self.wait_window()
        return self.result
