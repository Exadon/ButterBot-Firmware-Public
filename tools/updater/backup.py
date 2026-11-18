"""
Dimension Hopper - back up and restore everything the owner has personalized, as one JSON file on the PC.

Robot (over USB): voice + pitch/speed, names, ramble lines, start-up line, chat replies, routines, voice commands,
schedules and quiet hours. Controller (over USB): theme, brightness, sleep time. This PC: AI personality, facts and
memories. Not included: WiFi passwords and the Claude API key (they stay in Windows' credential store), and the faces
and IR remotes the robot learned (they stay on the robot).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

import hopper_core as core

FORMAT = 1
PC_KEYS = ("ai_personality", "ai_personality_custom", "ai_facts", "ai_memory", "ai_memory_on", "voice_commands",
           "owner_name", "robot_name")
CHAT_LINES = 48


def _hex(text: str) -> str:
    return text.encode("utf-8").hex() or "-"


def _unhex(h: str | None) -> str:
    try:
        return bytes.fromhex(h).decode("utf-8", "replace") if h and h != "-" else ""
    except ValueError:
        return ""


def collect(bot: str | None, ctrl: str | None, cfg: dict, app_version: str,
            progress: Callable[[str], None] = lambda s: None) -> tuple[dict, list[str]]:
    """Reads everything. Returns (backup, notes): notes say what was left out (a board that didn't answer)."""
    out: dict = {"format": FORMAT, "app": f"Dimension Hopper {app_version}", "made": time.strftime("%Y-%m-%d %H:%M"),
                 "pc": {k: cfg[k] for k in PC_KEYS if k in cfg}}
    notes = []

    def ask(port, c, tag):
        return core.console_command(port, c, tag, timeout_s=5)

    if bot:
        progress("Reading the robot…")
        v = ask(bot, "voice", "BBVOICE")
        if not v or "preset" not in v:
            notes.append(core.port_problem(v, bot) or "the robot didn't answer (is it on?) - its settings aren't in "
                                                       "the backup")
        else:
            r = {"voice": v["preset"], "pitch": int(v.get("pitch", 0) or 0), "speed": int(v.get("speed", 0) or 0)}
            n = ask(bot, "names", "BBNAMES") or {}
            r["owner"], r["robot_name"] = _unhex(n.get("owner")), _unhex(n.get("robot"))
            rm = ask(bot, "ramble", "BBRAMBLE") or {}
            r["ramble"] = [_unhex(h) for h in rm.get("d", "-").split(",") if h and h != "-"]
            r["start_line"] = _unhex((ask(bot, "startline", "BBSTART") or {}).get("d"))
            ch = (ask(bot, "chat", "BBCHAT") or {}).get("d", "-")
            r["chat"] = {i.split(":", 1)[0]: _unhex(i.split(":", 1)[1]) for i in ch.split(",") if ":" in i}
            sm = ask(bot, "summon", "BBSUMMON")  # Summon lines and beeps: robots from 1.1.1-exadon.9 on
            if sm and "d" in sm:
                r["summon"] = {i.split(":", 1)[0]: _unhex(i.split(":", 1)[1]) for i in sm["d"].split(",") if ":" in i}
            cue = ask(bot, "cue", "BBCUE")
            if cue and "on" in cue:
                r["beeps"] = cue["on"] != "0"
            progress("Reading the robot's routines…")
            rt = ask(bot, "rout", "BBROUT")
            if rt and "max" in rt:
                r["routines"] = {}
                for item in rt.get("d", "-").split(","):
                    if ":" in item:
                        i = item.split(":", 1)[0]
                        g = ask(bot, f"rout get {i}", "BBROUT") or {}
                        if _unhex(g.get("d")):
                            r["routines"][i] = _unhex(g.get("d"))
                cm = (ask(bot, "cmds", "BBCMDS") or {}).get("d", "-")
                r["commands"] = [{"routine": int(p[0]), "phrase": _unhex(p[1]), "phonemes": _unhex(p[2])}
                                 for p in (i.split(":") for i in cm.split(",")) if len(p) == 3]
                sc = ask(bot, "sched", "BBSCHED") or {}
                r["schedules"] = [{"days": int(p[0]), "minute": int(p[1]), "routine": int(p[2]), "on": p[3] == "1"}
                                  for p in (i.split(":") for i in sc.get("d", "-").split(",")) if len(p) == 4]
                q = sc.get("quiet", "0:1320:420").split(":")
                r["quiet"] = {"on": q[0] == "1", "start": int(q[1]), "end": int(q[2])} if len(q) == 3 else None
            else:
                notes.append("the robot's firmware has no routines yet - update it to back those up too")
            out["robot"] = r
    else:
        notes.append("no robot port - its settings aren't in the backup")

    if ctrl:
        progress("Reading the controller…")
        t = ask(ctrl, "theme", "BBTHEME")
        if t and "theme" in t:
            c = {"theme": int(t["theme"])}
            s = ask(ctrl, "screen", "BBSCREEN")
            if s and "bright" in s:
                c["bright"], c["sleep"] = int(s["bright"]), int(s["sleep"])
            h = ask(ctrl, "home", "BBHOME")  # the home screen's hints / robot (controller firmware 1.1-exadon.5 on)
            if h and "view" in h:
                c["home"] = h["view"]
            out["controller"] = c
        else:
            notes.append(core.port_problem(t, ctrl) or "the controller didn't answer (is it on?) - its theme isn't in "
                                                         "the backup")
    return out, notes


AUTO_DIR = core.APP_DIR / "auto-backups"   # made by the Flash tab just before each flash
AUTO_KEEP = 10                              # per board


def has_settings(data: dict, kind: str) -> bool:
    """True if the backup holds anything the owner set up on that board (not just the defaults)."""
    if kind in ("ctrl", "controller"):
        c = data.get("controller") or {}
        return bool(c.get("theme"))
    r = data.get("robot") or {}
    return any(r.get(k) for k in ("owner", "robot_name", "ramble", "start_line", "chat", "summon", "routines",
                                  "commands", "schedules")) or r.get("beeps") is False or bool(r.get("pitch") or r.get("speed")) or         bool((r.get("quiet") or {}).get("on"))


def auto_save(data: dict, kind: str) -> Path:
    """Saves a pre-flash backup and keeps the newest AUTO_KEEP for that board."""
    AUTO_DIR.mkdir(parents=True, exist_ok=True)
    path = AUTO_DIR / f"{kind}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    for old in sorted(AUTO_DIR.glob(f"{kind}-*.json"))[:-AUTO_KEEP]:
        try:
            old.unlink()
        except OSError:
            pass
    return path


def summary(data: dict) -> list[str]:
    """What a backup holds, for the confirm dialog."""
    lines = [f"Made {data.get('made', '?')} by {data.get('app', '?')}."]
    r = data.get("robot")
    if r:
        bits = [f"voice {r.get('voice')}", f"{len(r.get('ramble', []))} ramble lines",
                f"{len(r.get('chat', {}))} chat replies"]
        if "routines" in r:
            bits += [f"{len(r['routines'])} routines", f"{len(r.get('commands', []))} voice commands",
                     f"{len(r.get('schedules', []))} schedules"]
        lines.append("Robot: " + ", ".join(bits) + ".")
    if data.get("controller"):
        lines.append(f"Controller: theme {data['controller'].get('theme')}, screen settings.")
    pc = data.get("pc", {})
    if pc:
        n = len(pc.get("ai_memory", []))
        lines.append(f"This PC: AI personality and facts, {n} memor{'y' if n == 1 else 'ies'}.")
    return lines


def restore(data: dict, bot: str | None, ctrl: str | None, cfg: dict,
            progress: Callable[[str], None] = lambda s: None) -> list[str]:
    """Writes a backup back. Returns what happened, one line per part."""
    if data.get("format") != FORMAT:
        raise ValueError("this isn't a Dimension Hopper backup (or it's from a newer version)")
    done = []
    failed: list[str] = []

    worked = [0]

    def do(port, c, tag, part):
        rep = core.console_command(port, c, tag, timeout_s=6)
        if not rep or rep.get("result") == "error" or core.port_problem(rep, port):
            if part not in failed:
                failed.append(part)
        else:
            worked[0] += 1
        return rep

    def finished(board: str, failed_before: int):
        """Counts a board as restored only for what actually went through."""
        if len(failed) == failed_before:
            done.append(board)
        elif worked[0]:
            done.append(f"part of the {board}")
        worked[0] = 0

    r = data.get("robot")
    if r and bot:
        before = len(failed)
        progress("Restoring the robot's voice and lines…")
        do(bot, f"voice {r['voice']}", "BBVOICE", "voice")
        do(bot, f"voice tune {int(r.get('pitch', 0))} {int(r.get('speed', 0))}", "BBVOICE", "voice")
        do(bot, f"names {_hex(r.get('owner', ''))} {_hex(r.get('robot_name', ''))}", "BBNAMES", "names")
        do(bot, "ramble clear", "BBRAMBLE", "ramble lines")
        for line in r.get("ramble", []):
            do(bot, f"ramble add {_hex(line[:100])}", "BBRAMBLE", "ramble lines")
        do(bot, f"startline {_hex(r.get('start_line', ''))}", "BBSTART", "start-up line")
        progress("Restoring the chat replies…")
        now = (core.console_command(bot, "chat", "BBCHAT", timeout_s=5) or {}).get("d", "-")
        have = {i.split(":", 1)[0] for i in now.split(",") if ":" in i}
        chat = r.get("chat", {})
        for i in sorted(have | set(chat), key=int):
            do(bot, f"chat set {i} {_hex(chat.get(i, ''))}", "BBCHAT", "chat replies")
        if "summon" in r or "beeps" in r:  # backups from before 1.1.1-exadon.9 don't have them: leave the robot's
            now = core.console_command(bot, "summon", "BBSUMMON", timeout_s=5)
            if not now or "d" not in now:
                failed.append("Summon lines and beeps (update the robot's firmware first)")
            else:
                have = {i.split(":", 1)[0] for i in now["d"].split(",") if ":" in i}
                summon = r.get("summon", {})
                for i in sorted(have | set(summon), key=int):
                    do(bot, f"summon set {i} {_hex(summon.get(i, ''))}", "BBSUMMON", "Summon lines")
                if "beeps" in r:
                    do(bot, f"cue set {1 if r['beeps'] else 0}", "BBCUE", "beeps")
        if "routines" in r:
            progress("Restoring the routines…")
            rt = core.console_command(bot, "rout", "BBROUT", timeout_s=5)
            if not rt or "max" not in rt:
                failed.append("routines (update the robot's firmware first)")
            else:
                for i in range(int(rt.get("max", 12))):
                    text = r["routines"].get(str(i), "")
                    present = any(item.split(":", 1)[0] == str(i) for item in rt.get("d", "-").split(","))
                    if not text and not present:
                        continue
                    do(bot, f"rout begin {i}", "BBROUT", "routines")
                    hexed = text.encode().hex()
                    for j in range(0, len(hexed), 180):
                        do(bot, "rout put " + hexed[j:j + 180], "BBROUT", "routines")
                    do(bot, "rout save", "BBROUT", "routines")
                cm = (core.console_command(bot, "cmds", "BBCMDS", timeout_s=5) or {}).get("d", "-")
                for _ in [x for x in cm.split(",") if x.count(":") == 2]:
                    do(bot, "cmds del 0", "BBCMDS", "voice commands")
                for k, c in enumerate(r.get("commands", [])):
                    do(bot, f"cmds set {k} {c['routine']} {_hex(c['phrase'])} {_hex(c['phonemes'])}", "BBCMDS",
                       "voice commands")
                sc = (core.console_command(bot, "sched", "BBSCHED", timeout_s=5) or {}).get("d", "-")
                for _ in [x for x in sc.split(",") if x.count(":") == 3]:
                    do(bot, "sched del 0", "BBSCHED", "schedules")
                for k, s in enumerate(r.get("schedules", [])):
                    do(bot, f"sched set {k} {s['days']} {s['minute']} {s['routine']} {1 if s['on'] else 0}",
                       "BBSCHED", "schedules")
                q = r.get("quiet")
                if q:
                    do(bot, f"quiet {1 if q['on'] else 0} {q['start']} {q['end']}", "BBSCHED", "quiet hours")
        finished("robot", before)
    elif r:
        failed.append("robot (not connected)")

    c = data.get("controller")
    if c and ctrl:
        progress("Restoring the controller…")
        before = len(failed)
        worked[0] = 0
        do(ctrl, f"theme {c['theme']}", "BBTHEME", "controller theme")
        if "bright" in c:
            do(ctrl, f"screen {c['bright']} {c['sleep']}", "BBSCREEN", "controller screen")
        if c.get("home") in ("hints", "robot"):
            do(ctrl, f"home {c['home']}", "BBHOME", "controller home screen")
        finished("controller", before)
    elif c:
        failed.append("controller (not connected)")

    pc = data.get("pc", {})
    if pc:
        cfg.update({k: v for k, v in pc.items() if k in PC_KEYS})
        core.save_config(cfg)
        done.append("this PC's AI settings")
    out = [("Restored: " + ", ".join(done) + ".") if done else "Nothing was restored."]
    if failed:
        out.append("Didn't work: " + ", ".join(failed) + ".")
    return out
