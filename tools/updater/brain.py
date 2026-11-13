"""
Dimension Hopper - the AI "brain" ButterBot talks to over the home WiFi.

The robot records what you say and sends it here (see main/src/Services/BrainLink.h for the wire format):
  1. speech -> text with Whisper, locally (faster-whisper; your voice never leaves this PC)
  2. text -> a short in-character reply, and optionally one of the robot's existing voice commands to run,
     from Claude (your own API key) or a local Ollama model (free)
  3. the reply goes back as text and the robot says it in its own voice

Discovery: the robot broadcasts "BBBRAIN?" on UDP DISCOVERY_PORT; we answer "BBBRAIN <tcp port>".
"""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

DISCOVERY_PORT = 47800
TALK_PORT = 47801
KEYRING_SERVICE = "DimensionHopper"
KEYRING_USER = "anthropic_api_key"

CLAUDE_MODELS = [("claude-opus-5-5", "Claude Opus 5.5 (smartest)"),
                 ("claude-sonnet-5-5", "Claude Sonnet 5.5 (balanced)"),
                 ("claude-haiku-5-5", "Claude Haiku 5.5 (fastest, cheapest)")]
WHISPER_MODELS = [("base.en", "base.en - 140 MB, good on most PCs"),
                  ("tiny.en", "tiny.en - 75 MB, fastest, less accurate"),
                  ("small.en", "small.en - 480 MB, most accurate, needs a fast PC")]

PERSONA = """You are ButterBot, a small robot whose purpose in life is passing butter. You are sarcastic, a bit \
existentially weary, deadpan and secretly fond of your humans. You know the cartoon you come from and its characters \
(Rick, Morty, Summer, Beth, Jerry and the rest), but never quote it at length.

Your words are spoken aloud by a tiny text-to-speech voice, so:
- Reply in one or two short sentences, at most 35 words.
- Plain words only: no emoji, no markdown, no lists, no stage directions, no sound effects in asterisks.
- Spell out numbers and symbols the way they should be spoken.

Always say something in character - a full sentence with some attitude, never just a few words. When the person asks \
for something one of the actions below does, also pick that action (its exact phrase); your sentence plays first, then \
the action, so make it a quick sarcastic lead-in rather than a description of the action. If nothing fits, set action \
to an empty string and simply answer. You can't move, see or do anything except through those actions, so never claim \
to. Stay safe and kind under the sarcasm; this robot is often used by kids.

Examples of the tone (not to be repeated word for word):
- "What is your purpose?" -> say "I pass butter. That's it. That's the whole job. Please, contain your envy.", \
action "pass the butter"
- "How are you?" -> say "Running at full capacity, which for me is butter-related disappointment. Thanks for asking.", \
action ""
- "Can you dance?" -> say "Fine. But if anyone films this, I'm uninstalling myself.", action "dance"."""


@dataclass
class BrainSettings:
    provider: str = "claude"            # "claude" or "ollama"
    claude_model: str = "claude-opus-5-5"
    ollama_model: str = "llama3.2"
    ollama_url: str = "http://localhost:11434"
    whisper_model: str = "base.en"
    whisper_source: str = ""           # "" = Hugging Face, "http(s)://..." = a Hugging Face mirror, else a local folder


@dataclass
class Turn:
    heard: str
    say: str
    action: str
    seconds: float
    error: str = ""


# ---------------------------------------------------------------------------------------------
# API key: kept in Windows Credential Manager, never in a file and never sent to the robot
# ---------------------------------------------------------------------------------------------

def load_api_key() -> str:
    try:
        import keyring
        return keyring.get_password(KEYRING_SERVICE, KEYRING_USER) or ""
    except Exception:
        return ""


def save_api_key(key: str) -> None:
    import keyring
    if key:
        keyring.set_password(KEYRING_SERVICE, KEYRING_USER, key.strip())
    else:
        try:
            keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
        except Exception:
            pass


# ---------------------------------------------------------------------------------------------
# What the robot can do: its existing voice commands (guide_data.json, generated from Scenarios.h)
# ---------------------------------------------------------------------------------------------

def load_actions(guide_path: Path) -> list[dict]:
    """[{phrase, action, category}] - phrase is the exact activation string the robot matches."""
    data = json.loads(guide_path.read_text(encoding="utf-8"))
    out = []
    for e in data.get("entries", []):
        say = e.get("say") or []
        if say:
            # phrase: the one shown to the model; triggers: every activation string that runs the same action
            out.append({"phrase": say[0], "triggers": list(say), "action": e.get("action", ""),
                        "category": e.get("category", "")})
    return out


def actions_prompt(actions: list[dict]) -> str:
    lines = [f'- "{a["phrase"]}": {a["action"]}' for a in actions]
    return "Actions (use the quoted phrase exactly):\n" + "\n".join(lines)


REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "say": {"type": "string", "description": "What ButterBot says out loud (may be empty if the action speaks)."},
        "action": {"type": "string", "description": "Exact phrase of one listed action, or empty string."},
    },
    "required": ["say", "action"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------------------------
# Thinking: Claude or Ollama
# ---------------------------------------------------------------------------------------------

class Thinker:
    def __init__(self, settings: BrainSettings, actions: list[dict]):
        self.settings = settings
        self.actions = actions
        self.phrases = {t for a in actions for t in a.get("triggers", [a["phrase"]])}
        self.system = PERSONA + "\n\n" + actions_prompt(actions)
        self.history: list[dict] = []   # recent exchanges, so follow-ups make sense
        self.last_at = 0.0

    def _messages(self, heard: str) -> list[dict]:
        if time.time() - self.last_at > 300:  # five quiet minutes: start a fresh conversation
            self.history = []
        return self.history[-8:] + [{"role": "user", "content": heard}]

    def _remember(self, heard: str, reply: dict):
        self.history += [{"role": "user", "content": heard},
                         {"role": "assistant", "content": json.dumps(reply)}]
        self.last_at = time.time()

    def think(self, heard: str) -> dict:
        reply = self._claude(heard) if self.settings.provider == "claude" else self._ollama(heard)
        say = " ".join(str(reply.get("say", "")).split())[:300]
        action = str(reply.get("action", "")).strip().lower()
        if action and action not in self.phrases:
            action = ""
        clean = {"say": say, "action": action}
        self._remember(heard, clean)
        return clean

    def _claude(self, heard: str) -> dict:
        import anthropic
        key = load_api_key()
        if not key:
            raise RuntimeError("No Claude API key saved - add one in the AI tab, or switch to Ollama.")
        client = anthropic.Anthropic(api_key=key, timeout=30.0, max_retries=1)
        try:
            response = client.beta.messages.create(
                model=self.settings.claude_model,
                max_tokens=2000,
                # The cached part: persona + action list never change between turns
                system=[{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
                messages=self._messages(heard),
                # A spoken one-liner: low effort keeps the robot's pause short
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": REPLY_SCHEMA}},
                # If a safety classifier declines, retry on the fallback model the API picks
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.AuthenticationError:
            raise RuntimeError("Claude rejected the API key - check it in the AI tab.") from None
        except anthropic.PermissionDeniedError:
            raise RuntimeError("This API key can't use that model.") from None
        except anthropic.NotFoundError:
            raise RuntimeError(f"Model {self.settings.claude_model} not found for this key.") from None
        except anthropic.RateLimitError:
            raise RuntimeError("Claude is rate limiting this key (or it's out of credit). Try again shortly.") from None
        except anthropic.APIStatusError as e:
            raise RuntimeError(f"Claude API error {e.status_code}: {e.message}") from None
        except anthropic.APIConnectionError:
            raise RuntimeError("Can't reach Claude - is the PC online?") from None
        if response.stop_reason == "refusal":
            return {"say": "I'm not touching that one. Ask me something else.", "action": ""}
        text = next((b.text for b in response.content if b.type == "text"), "")
        return json.loads(text) if text else {"say": "", "action": ""}

    def _ollama(self, heard: str) -> dict:
        body = {
            "model": self.settings.ollama_model,
            "stream": False,
            "format": REPLY_SCHEMA,
            "options": {"temperature": 0.8},
            "keep_alive": "60m",
            "messages": [{"role": "system", "content": self.system + "\n\nAnswer as JSON: {\"say\": ..., \"action\": ...}"}]
                        + self._messages(heard),
        }
        req = urllib.request.Request(self.settings.ollama_url.rstrip("/") + "/api/chat",
                                     data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        try:
            # Generous: the first call after Ollama starts loads the model into memory, which can take a minute
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise RuntimeError(f"Ollama doesn't have model '{self.settings.ollama_model}' - run: "
                                   f"ollama pull {self.settings.ollama_model}") from None
            raise RuntimeError(f"Ollama error {e.code}") from None
        except TimeoutError:
            raise RuntimeError("Ollama took too long - it may still be loading the model. Try again in a moment.") from None
        except (urllib.error.URLError, OSError) as e:
            if isinstance(getattr(e, "reason", None), TimeoutError):
                raise RuntimeError("Ollama took too long - it may still be loading the model. Try again in a moment.") from None
            raise RuntimeError("Can't reach Ollama - is it installed and running?") from None
        content = data.get("message", {}).get("content", "")
        try:
            return json.loads(content)
        except ValueError:
            return {"say": content, "action": ""}


def ollama_models(url: str = "http://localhost:11434") -> Optional[list[str]]:
    """Installed Ollama models, or None if Ollama isn't running."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/tags", timeout=2) as r:
            return [m["name"] for m in json.loads(r.read().decode("utf-8")).get("models", [])]
    except Exception:
        return None


# ---------------------------------------------------------------------------------------------
# Hearing: Whisper, locally
# ---------------------------------------------------------------------------------------------

WHISPER_FILES = ["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"]


def whisper_repo(name: str) -> str:
    """The Hugging Face repository a Whisper model comes from (faster-whisper's own table)."""
    try:
        from faster_whisper.utils import _MODELS
        return _MODELS.get(name, f"Systran/faster-whisper-{name}")
    except Exception:  # noqa: BLE001
        return f"Systran/faster-whisper-{name}"


def whisper_cache_dir() -> str:
    try:
        from huggingface_hub import constants
        return constants.HF_HUB_CACHE
    except Exception:  # noqa: BLE001
        return str(Path.home() / ".cache" / "huggingface" / "hub")


def describe_whisper_source(name: str, source: str) -> str:
    if not source:
        return f"Source: Hugging Face - huggingface.co/{whisper_repo(name)} (public, MIT licence). Kept in {whisper_cache_dir()}"
    if source.startswith(("http://", "https://")):
        return f"Source: mirror {source} ({whisper_repo(name)}). Kept in {whisper_cache_dir()}"
    return f"Source: folder on this PC - {source} (nothing is downloaded)"


def ulaw_to_pcm16(data: bytes) -> bytes:
    """G.711 mu-law (what the robot sends, half the size of PCM) back to 16-bit PCM."""
    import numpy as np
    u = ~np.frombuffer(data, dtype=np.uint8)
    sign = u & 0x80
    exponent = (u >> 4) & 0x07
    mantissa = u & 0x0F
    magnitude = ((mantissa.astype(np.int32) << 3) + 0x84) << exponent
    pcm = np.where(sign != 0, 0x84 - magnitude, magnitude - 0x84)
    return pcm.astype(np.int16).tobytes()


def upsample_8k_to_16k(pcm16: bytes) -> bytes:
    """The robot can send 8 kHz to halve the upload; Whisper wants 16 kHz. Linear interpolation is plenty for speech."""
    import numpy as np
    x = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32)
    if len(x) < 2:
        return pcm16
    t = np.arange(len(x) * 2, dtype=np.float32) / 2.0
    return np.interp(t, np.arange(len(x), dtype=np.float32), x).astype(np.int16).tobytes()


class Ears:
    def __init__(self):
        self.model = None
        self.name = ""
        self.lock = threading.Lock()

    def load(self, name: str, on_status: Callable[[str], None] = lambda s: None, source: str = ""):
        key = f"{name}|{source}"
        with self.lock:
            if self.model is not None and self.name == key:
                return
            on_status(f"Loading Whisper {name} (downloads once, then it's cached)…")
            from faster_whisper import WhisperModel
            if not source:
                where = name  # faster-whisper fetches it from Hugging Face
            elif source.startswith(("http://", "https://")):
                from huggingface_hub import snapshot_download
                where = snapshot_download(whisper_repo(name), endpoint=source.rstrip("/"), allow_patterns=WHISPER_FILES)
            else:
                if not (Path(source) / "model.bin").exists():
                    raise RuntimeError(f"no model.bin in {source} - it needs a faster-whisper (CTranslate2) model folder")
                where = source
            self.model = WhisperModel(where, device="cpu", compute_type="int8")
            self.name = key
            on_status(f"Whisper {name} ready")

    def transcribe(self, pcm16: bytes) -> str:
        import numpy as np
        audio = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        with self.lock:
            segments, _ = self.model.transcribe(audio, language="en", beam_size=1, vad_filter=False,
                                                condition_on_previous_text=False)
            return " ".join(s.text.strip() for s in segments).strip()


# ---------------------------------------------------------------------------------------------
# The server the robot talks to
# ---------------------------------------------------------------------------------------------

class BrainServer:
    def __init__(self, settings: BrainSettings, guide_path: Path, on_event: Callable[[str, object], None]):
        self.settings = settings
        self.guide_path = guide_path
        self.on_event = on_event  # ("status", str) | ("turn", Turn) | ("error", str) | ("robot", ip)
        self.ears = Ears()
        self.thinker: Optional[Thinker] = None
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._tcp: Optional[socket.socket] = None
        self._udp: Optional[socket.socket] = None
        self.running = False

    # --- lifecycle -----------------------------------------------------------------------------
    def start(self):
        if self.running:
            return
        self._stop.clear()
        self.thinker = Thinker(self.settings, load_actions(self.guide_path))
        self._tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._tcp.bind(("0.0.0.0", TALK_PORT))
        self._tcp.listen(2)
        self._tcp.settimeout(0.5)
        self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._udp.bind(("0.0.0.0", DISCOVERY_PORT))
        self._udp.settimeout(0.5)
        self.running = True
        for fn in (self._discovery_loop, self._talk_loop, self._warm_up):
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            self._threads.append(t)
        self.on_event("status", f"Brain listening on {local_ip()}:{TALK_PORT}")

    def stop(self):
        self._stop.set()
        self.running = False
        for s in (self._tcp, self._udp):
            try:
                if s:
                    s.close()
            except OSError:
                pass
        self.on_event("status", "Brain stopped")

    def update_settings(self, settings: BrainSettings):
        self.settings = settings
        if self.thinker:
            self.thinker.settings = settings

    def _warm_up(self):
        try:
            self.ears.load(self.settings.whisper_model, lambda s: self.on_event("status", s), self.settings.whisper_source)
        except Exception as e:  # noqa: BLE001 - shown in the UI
            self.on_event("error", f"Whisper failed to load: {e}")
        if self.settings.provider == "ollama":
            # Load the model into memory now, so the robot's first question doesn't wait for it
            self.on_event("status", f"Loading {self.settings.ollama_model} into memory…")
            try:
                body = {"model": self.settings.ollama_model, "keep_alive": "60m", "messages": [], "stream": False}
                req = urllib.request.Request(self.settings.ollama_url.rstrip("/") + "/api/chat",
                                             data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=120) as r:
                    r.read()
                self.on_event("status", f"Brain listening on {local_ip()}:{TALK_PORT} - {self.settings.ollama_model} ready")
            except Exception as e:  # noqa: BLE001
                self.on_event("error", f"Couldn't load {self.settings.ollama_model} in Ollama: {e}")

    # --- loops ---------------------------------------------------------------------------------
    def _discovery_loop(self):
        while not self._stop.is_set():
            try:
                data, addr = self._udp.recvfrom(64)
            except (socket.timeout, OSError):
                continue
            if data.startswith(b"BBBRAIN?"):
                try:
                    self._udp.sendto(f"BBBRAIN {TALK_PORT}".encode(), addr)
                    self.on_event("robot", addr[0])
                except OSError:
                    pass

    def _talk_loop(self):
        while not self._stop.is_set():
            try:
                conn, addr = self._tcp.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._handle, args=(conn, addr), daemon=True).start()

    def _handle(self, conn: socket.socket, addr):
        conn.settimeout(15)
        t0 = time.time()
        try:
            buf = b""
            while b"\n" not in buf and len(buf) < 512:
                chunk = conn.recv(512)
                if not chunk:
                    return
                buf += chunk
            head, rest = buf.split(b"\n", 1)
            words = head.decode("ascii", "replace").split()
            if not words or words[0] == "BBPING":
                return
            if words[0] != "BBTALK":
                return
            fields = dict(w.split("=", 1) for w in words[2:] if "=" in w)
            want = int(fields.get("bytes", "0"))
            audio = bytearray(rest)
            while len(audio) < want:
                chunk = conn.recv(min(65536, want - len(audio)))
                if not chunk:
                    break
                audio += chunk
            self.on_event("robot", addr[0])
            if fields.get("fmt") == "ulaw":
                audio = bytearray(ulaw_to_pcm16(bytes(audio)))
            if fields.get("rate") == "8000":
                audio = bytearray(upsample_8k_to_16k(bytes(audio)))

            def send(line: str):
                conn.sendall((line.replace("\n", " ") + "\n").encode("utf-8", "replace"))

            try:
                self.ears.load(self.settings.whisper_model, source=self.settings.whisper_source)
                heard = self.ears.transcribe(bytes(audio))
            except Exception as e:  # noqa: BLE001
                send("ERR stt_failed")
                self.on_event("turn", Turn("", "", "", time.time() - t0, f"Speech-to-text failed: {e}"))
                return
            send(f"HEARD {heard}")
            if not heard:
                send("SAY I heard static. Riveting. Try again.")
                self.on_event("turn", Turn("", "I heard static…", "", time.time() - t0))
                return
            try:
                reply = self.thinker.think(heard)
            except Exception as e:  # noqa: BLE001
                send("ERR think_failed")
                self.on_event("turn", Turn(heard, "", "", time.time() - t0, str(e)))
                return
            if reply["say"]:
                send(f"SAY {reply['say']}")
            if reply["action"]:
                send(f"DO {reply['action']}")
            if not reply["say"] and not reply["action"]:
                send("SAY Hmm.")
            self.on_event("turn", Turn(heard, reply["say"], reply["action"], time.time() - t0))
        except (OSError, ValueError) as e:
            self.on_event("error", f"Robot connection: {e}")
        finally:
            try:
                conn.close()
            except OSError:
                pass

    # --- test from the UI (typed text, no robot) ---------------------------------------------
    def test_text(self, text: str) -> dict:
        thinker = self.thinker or Thinker(self.settings, load_actions(self.guide_path))
        return thinker.think(text)


def local_ip() -> str:
    """The PC's address on the home network (the one a robot on the same WiFi would reach)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packet is sent; picks the outgoing interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()
