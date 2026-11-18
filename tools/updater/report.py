"""
Dimension Hopper - the problem report: one text file to attach to a GitHub issue or a forum post.

What goes in: app and system versions, the USB ports seen, each board's status line, the robot's WiFi state and its
recent events (it keeps its last status lines in RAM since it switched on - "events" on its console, so problems that
happened while it was unplugged are there too), the AI brain's settings and the timings / errors of its last turns,
and this session's Dimension Hopper log.

What never goes in: WiFi network names and passwords, IP and MAC addresses, the owner's and robot's names, API keys,
what was said in AI conversations (only timings and errors), memories and facts, and the Windows / Mac user name.
Everything is assembled first and then passed through redact(), which blanks those wherever they appear.
"""
from __future__ import annotations

import json
import os
import platform
import re
import sys
import time
from pathlib import Path
from typing import Callable, Optional

import hopper_core as core

BRAIN_TURNS = 15          # newest AI turns in the report
TURN_FIELDS = ("time", "provider", "model", "seconds", "upload_s", "hear_s", "think_s", "look_s", "looked",
               "audio_s", "audio_bytes", "action", "error")  # never "heard" / "say": what people said is private
SETTINGS_SAFE = ("ai_provider", "ai_claude_model", "ai_ollama_model", "ai_whisper", "ai_personality", "ai_memory_on",
                 "ai_autostart", "ai_wifi_ok")
BOT_STAT_FIELDS = ("fw", "batt", "charge", "link", "up", "ram", "ram_min", "alloc_fail", "reset", "boots", "nvs", "user")


def _ask(port: str, cmd: str, tag: str, timeout_s: float = 6.0) -> Optional[dict]:
    r = core.console_command(port, cmd, tag, timeout_s=timeout_s)
    return None if r is None or core.port_problem(r, port) else r


def _unhex(h: Optional[str]) -> str:
    if not h or h == "-":
        return ""
    try:
        return bytes.fromhex(h).decode("utf-8", "replace")
    except ValueError:
        return ""


def _robot(port: str, out: list[str], held: bool):
    out.append(f"== Robot ({os.path.basename(port)}) ==")
    if held:
        out.append("(its port is open in the Console or Diagnostics tab - disconnect it there and make the report again)")
        return
    st = _ask(port, "stat", "BBSTAT")
    if not st:
        out.append("No answer - switched off, busy, or not running this firmware.")
        return
    out.append("status: " + " ".join(f"{k}={st[k]}" for k in BOT_STAT_FIELDS if k in st))
    w = _ask(port, "wifi", "BBWIFI")
    if w:
        out.append(f"wifi: configured={w.get('configured', '?')} ai={w.get('ai', '?')} last_problem={w.get('last', '?')}")
    ev = _ask(port, "events", "BBEV", timeout_s=10)
    if ev is None:
        out.append("events: not available (firmware older than 1.1.1-exadon.10)")
    else:
        lines = _unhex(ev.get("d")).splitlines()
        out.append(f"events: the last {len(lines)} since it switched on {ev.get('up', '?')} s ago "
                   "(uptime in seconds, oldest first):")
        out += ["  " + line for line in lines] or ["  (none)"]


def _controller(port: str, out: list[str], held: bool):
    out.append(f"== Controller ({os.path.basename(port)}) ==")
    if held:
        out.append("(its port is open in the Console or Diagnostics tab)")
        return
    st = _ask(port, "stat", "BBSTAT")
    out.append("status: " + " ".join(f"{k}={v}" for k, v in st.items()) if st else
               "No answer - switched off, busy, or not running this firmware.")


def _brain(cfg: dict, out: list[str]):
    out.append("== AI brain ==")
    out.append("settings: " + " ".join(f"{k}={cfg[k]}" for k in SETTINGS_SAFE if k in cfg))
    try:
        import brain
        models = brain.ollama_models()
        if models is None:
            out.append("ollama: not running")
        else:
            seeing = [m for m in models if "vision" in brain.ollama_capabilities("http://localhost:11434", m)]
            out.append(f"ollama: models={', '.join(models) or 'none'}; sees with: {', '.join(seeing) or 'none'}")
    except Exception as e:  # noqa: BLE001 - the report must never fail over one section
        out.append(f"ollama: couldn't check ({type(e).__name__})")
    log = core.APP_DIR / "brain_log.jsonl"
    rows = []
    try:
        rows = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()[-BRAIN_TURNS:] if l.strip()]
    except (OSError, ValueError):
        pass
    out.append(f"last {len(rows)} AI turns (timings and errors only - not what was said):")
    for r in rows:
        out.append("  " + " ".join(f"{k}={r[k]}" for k in TURN_FIELDS if r.get(k) not in (None, "", 0, 0.0)))


def build(version: str, bot_port: str, ctrl_port: str, cfg: dict, held: set[str], app_log: str,
          progress: Callable[[str], None] = lambda s: None) -> str:
    out = ["ButterBot problem report", "========================",
           f"made: {time.strftime('%Y-%m-%d %H:%M')}",
           f"Dimension Hopper {version} ({'app' if getattr(sys, 'frozen', False) else 'from source'}) on "
           f"{platform.system()} {platform.release()} ({platform.version()}), {platform.machine()}", ""]
    out.append("== USB ports ==")
    for p in core.list_ports():
        out.append(f"  {os.path.basename(p.device)}: {p.description}" + (f" [{p.bridge}]" if p.bridge else ""))
    out.append("")
    if bot_port:
        progress("Asking the robot…")
        _robot(bot_port, out, bot_port in held)
    else:
        out.append("== Robot ==\nnot selected / not plugged in")
    out.append("")
    if ctrl_port:
        progress("Asking the controller…")
        _controller(ctrl_port, out, ctrl_port in held)
    else:
        out.append("== Controller ==\nnot selected / not plugged in")
    out.append("")
    progress("Collecting the AI settings…")
    _brain(cfg, out)
    out += ["", "== Dimension Hopper log (this session) ==", app_log.strip() or "(empty)"]
    return redact("\n".join(out), cfg)


def redact(text: str, cfg: dict) -> str:
    """Blanks anything personal: names, network names, addresses, keys and the user name."""
    secrets = {cfg.get(k, "") for k in ("owner_name", "robot_name", "ai_ssid")}
    try:
        import keyring  # noqa: F401 - only to know the API key, if one is saved, so it can't slip through
        import brain
        secrets.add(brain.load_api_key())
    except Exception:  # noqa: BLE001
        pass
    for s in sorted((s.strip() for s in secrets if s and len(s.strip()) >= 3), key=len, reverse=True):
        text = re.sub(re.escape(s), "<hidden>", text, flags=re.IGNORECASE)
    text = re.sub(r"\bsk-ant-[A-Za-z0-9_\-]+", "<api key>", text)
    text = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "<address>", text)                    # IPv4
    text = re.sub(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b", "<mac>", text)        # MAC / Bluetooth
    text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "<email>", text)
    home = str(Path.home())
    text = text.replace(home, "~").replace(home.replace("\\", "/"), "~")
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    if len(user) >= 3:
        text = re.sub(rf"(?<![A-Za-z0-9]){re.escape(user)}(?![A-Za-z0-9])", "<user>", text, flags=re.IGNORECASE)
    return text
