"""JARVIS — native edition.

Run:  python jarvis.py
Build the .exe:  double-click build.bat  (or see README.md)

Why a native shell at all? A browser tab cannot listen for a wake word
while minimized, and cannot glance at the screen on a timer — the tab
gets throttled or suspended. So this launcher keeps the HUD
(jarvis-hud.html, shown in a pywebview window) as the FACE, and runs the
real abilities as native Python background services in the same process:

  * WakeListener  — SpeechRecognition listen_in_background on a thread;
                    hears "Jarvis" (or "Hey Jarvis" / "Nova") even while
                    the window is minimized or hidden in the tray, asks
                    the brain (backend.Agent) and answers out loud.
  * ScreenWatcher — while Screen Watch is ON (default OFF, visible
                    indicator in the HUD + tray), captures the screen
                    in memory, asks the brain for one suggestion, and
                    delivers it by tray popup + voice + HUD log.
                    Screenshots are NEVER saved to disk or logged.
  * Tray icon     — minimize/close hides JARVIS to the system tray
                    instead of quitting; Quit in the tray menu is the
                    only real exit.

Brain settings are shared with NOVA on purpose: the config file is the
same %APPDATA%\\NovaAgent\\config.json, so the provider / API key /
model / fallback / protected-paths Faizan already set up in NOVA carry
over with no re-typing. JARVIS adds its own keys alongside:
wake_word_enabled, screen_watch_enabled, screen_watch_interval_sec.
"""
import json, os, queue, re, sys, threading, webbrowser
from pathlib import Path

import backend

# ------------------------------------------------------------------ files ---
def bundled_file(filename):
    """Path to a file bundled into the .exe (PyInstaller _MEIPASS) or
    sitting beside this script in a source checkout."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / filename

# ------------------------------------------------------------ config ---
# Same location as NOVA — see the module docstring. JARVIS-specific keys
# live next to NOVA's; the older screen_advisor_* keys are honoured as a
# legacy alias so an existing NOVA config maps across untouched.
CFG_DIR = Path(os.environ.get("APPDATA") or Path.home()) / "NovaAgent"
CFG_FILE = CFG_DIR / "config.json"
DEFAULTS = {"provider": "anthropic", "api_key": "", "model": "", "base_url": "",
            "protected_paths": [], "voice_enabled": True,
            "voice_name_contains": "", "voice_rate": 165,
            "wake_word_enabled": False,
            "screen_watch_enabled": False, "screen_watch_interval_sec": 90}
VOICE_RATE_DEFAULT = 165   # calm JARVIS register (NOVA used a brisker 170)
VOICE_RATE_MIN, VOICE_RATE_MAX = 120, 220
SCREEN_WATCH_MIN_INTERVAL = 30     # seconds — never look more often
SCREEN_WATCH_DEFAULT_INTERVAL = 90
FB_DEFAULTS = {"enabled": True, "provider": "ollama", "api_key": "", "model": "", "base_url": ""}

# Google retired gemini-2.5-flash for new users (404 — Google's own error
# says to use gemini-3.8-flash). Heal stale saved configs on load, exactly
# like NOVA does, so an old model name can't break every request.
RETIRED_MODELS = {"gemini-2.5-flash": "gemini-3.8-flash"}

def migrate_model_name(model):
    """Map a retired model name to its replacement; pass anything else
    through unchanged. Accepts the bare name or the 'models/...' form."""
    m = str(model or "").strip()
    bare = m[len("models/"):] if m.startswith("models/") else m
    return RETIRED_MODELS.get(bare, m)

def load_config():
    try:
        data = json.loads(CFG_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — missing/corrupt config = defaults
        data = {}
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in data.items() if k != "fallback"})
    fb = dict(FB_DEFAULTS)
    fb.update(data.get("fallback") or {})
    fb["model"] = migrate_model_name(fb.get("model"))
    cfg["fallback"] = fb
    cfg["model"] = migrate_model_name(cfg.get("model"))
    cfg["protected_paths"] = [str(x) for x in (cfg.get("protected_paths") or [])]
    cfg["voice_enabled"] = bool(cfg.get("voice_enabled", True))
    cfg["voice_name_contains"] = str(cfg.get("voice_name_contains") or "")
    try:
        rate = int(cfg.get("voice_rate", VOICE_RATE_DEFAULT))
    except (TypeError, ValueError):
        rate = VOICE_RATE_DEFAULT
    cfg["voice_rate"] = max(VOICE_RATE_MIN, min(VOICE_RATE_MAX, rate))
    cfg["wake_word_enabled"] = bool(cfg.get("wake_word_enabled", False))
    # Screen Watch keys, with the NOVA screen_advisor_* names as alias
    if "screen_watch_enabled" not in data and "screen_advisor_enabled" in data:
        cfg["screen_watch_enabled"] = bool(data.get("screen_advisor_enabled"))
    else:
        cfg["screen_watch_enabled"] = bool(cfg.get("screen_watch_enabled", False))
    if "screen_watch_interval_sec" in data:
        raw_interval = data["screen_watch_interval_sec"]
    elif "screen_advisor_interval_sec" in data:
        raw_interval = data["screen_advisor_interval_sec"]
    else:
        raw_interval = SCREEN_WATCH_DEFAULT_INTERVAL
    try:
        interval = int(raw_interval)
    except (TypeError, ValueError):
        interval = SCREEN_WATCH_DEFAULT_INTERVAL
    cfg["screen_watch_interval_sec"] = max(SCREEN_WATCH_MIN_INTERVAL, interval)
    return cfg

def save_config(cfg):
    CFG_DIR.mkdir(parents=True, exist_ok=True)
    CFG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

# ------------------------------------------------------------ error log ---
# The no-raw-errors rule: nothing in JARVIS may die with a traceback or
# dump a raw error wall at the user. Failures are written here
# (jarvis.log in the app config dir) for diagnosis, and the user hears
# exactly one calm human line instead.
LOG_FILE = CFG_DIR / "jarvis.log"
_log_lock = threading.Lock()

def log_error(area, exc):
    """Append a failure to jarvis.log. Never raises."""
    try:
        import datetime, traceback
        CFG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if isinstance(exc, BaseException):
            detail = "".join(traceback.format_exception(exc)).rstrip()
        else:
            detail = str(exc)
        with _log_lock:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"[{stamp}] [{area}] {detail}\n")
    except Exception:  # noqa: BLE001 — logging must never crash the app
        pass

def calm_reply(text):
    """Make a brain reply safe to show a frustrated user: keep the calm
    human message, cut any raw '(Details: ...)' error dump the brain
    attached (the full text still goes to jarvis.log). Pure function."""
    t = str(text or "")
    cut = t.find("\n(Details:")
    if cut != -1:
        t = t[:cut]
    return t.strip()

# ------------------------------------------------- optional native deps ---
# Every heavy/native dependency is optional at import time: the module
# must import (and its pure logic must stay testable) on a machine with
# none of them installed. Features fail safe with a clear log line.
try:
    import webview
except Exception:  # noqa: BLE001
    webview = None

try:
    import pystray
    from PIL import Image, ImageDraw
    TRAY_OK = True
except Exception:  # noqa: BLE001
    pystray = None
    TRAY_OK = False

try:
    import pyttsx3
    TTS_OK = True
except Exception:  # noqa: BLE001
    pyttsx3 = None
    TTS_OK = False

try:
    import speech_recognition as sr
    SR_OK = True
except Exception:  # noqa: BLE001
    sr = None
    SR_OK = False

try:
    import winsound  # Windows beep for the wake-word acknowledgement
except Exception:  # noqa: BLE001
    winsound = None

NO_MIC_MSG = ("🎙 No microphone found/working on this PC — "
              "type commands in the HUD instead.")

# ------------------------------------------------------------- speech ---
def clean_for_speech(text, limit=400):
    """Strip markdown/code so the spoken reply sounds like a human talking
    (same cleaning NOVA applies before speaking)."""
    t = re.sub(r"```.*?```", " ", str(text or ""), flags=re.S)
    t = re.sub(r"`[^`]*`", " ", t)
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"[#>*_~|⛔⚠]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > limit:
        cut = t[:limit]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        t = cut[:end + 1] if end > 120 else cut
    return t

def clamp_for_speech(text, max_sentences=4):
    """Clean a reply for speaking AND clamp it to at most
    `max_sentences` sentences — the user's rule: spoken replies are
    1–4 natural sentences, never a read-out of a whole essay. Pure
    function, unit-testable."""
    t = clean_for_speech(text, limit=1200)
    if not t:
        return ""
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", t) if p.strip()]
    if len(parts) > max_sentences:
        parts = parts[:max_sentences]
    out = " ".join(parts)
    if len(out) > 600:      # a hard ceiling even for run-on sentences
        out = out[:600].rsplit(" ", 1)[0] + "…"
    return out

# Calm, deeper JARVIS register: British male voices first. Name
# fragments in best-first order (Ryan/George/Daniel/Arthur are the
# usual en-GB SAPI names; David is the common en-US male fallback).
VOICE_NAME_PREFERENCE = ("ryan", "george", "daniel", "arthur", "david")
_KNOWN_FEMALE = ("zira", "hazel", "susan", "catherine", "linda", "female")

def _voice_is_british(v):
    t = ((getattr(v, "name", "") or "") + " " + (getattr(v, "lang", "") or "")).lower()
    return "en-gb" in t or "en_gb" in t or "british" in t

def _voice_is_english(v):
    lang = (getattr(v, "lang", "") or "").lower()
    name = (getattr(v, "name", "") or "").lower()
    return lang.startswith("en") or "english" in name or _voice_is_british(v)

def _voice_looks_female(v):
    n = (getattr(v, "name", "") or "").lower()
    return any(f in n for f in _KNOWN_FEMALE)

def pick_voice(voices, name_contains=""):
    """Choose the most JARVIS-like voice from pyttsx3 voice objects
    (anything with .name / .lang / .id). Pure function — no audio — so
    the preference order is unit-testable:

      1. a voice whose name matches the user's voice_name_contains,
      2. British/UK voices matching the calm-male preference list,
      3. any English voice matching the preference list (e.g. David),
      4. any English voice marked male (and not female),
      5. any English voice that doesn't look female,
      6. the first voice there is.
    The familiar default female voice (Zira) is therefore only ever a
    last resort, never the choice while a male English voice exists."""
    voices = list(voices or [])
    if not voices:
        return None
    want = str(name_contains or "").strip().lower()
    if want:
        for v in voices:
            if want in (getattr(v, "name", "") or "").lower():
                return v
    for frag in VOICE_NAME_PREFERENCE:                    # 2: British + named
        for v in voices:
            if frag in (getattr(v, "name", "") or "").lower() and _voice_is_british(v):
                return v
    for frag in VOICE_NAME_PREFERENCE:                    # 3: English + named
        for v in voices:
            if frag in (getattr(v, "name", "") or "").lower() and _voice_is_english(v):
                return v
    for v in voices:                                      # 4: English male
        n = (getattr(v, "name", "") or "").lower()
        if _voice_is_english(v) and "male" in n and not _voice_looks_female(v):
            return v
    for v in voices:                                      # 5: English, not female
        if _voice_is_english(v) and not _voice_looks_female(v):
            return v
    return voices[0]                                      # 6: anything

def _pick_voice(engine, name_contains=""):
    """Apply pick_voice() to a live pyttsx3 engine."""
    try:
        voices = engine.getProperty("voices") or []
    except Exception:  # noqa: BLE001
        return
    pick = pick_voice(voices, name_contains)
    if pick is not None:
        try: engine.setProperty("voice", pick.id)
        except Exception:  # noqa: BLE001
            pass

class Speaker:
    """pyttsx3 text-to-speech on its own thread (the NOVA pattern).

    A FRESH engine is created for every utterance and disposed of right
    after speaking — reusing one SAPI engine across many calls is a
    classic cause of stuttering/breaking-up audio. Latest-wins mailbox:
    a newer reply replaces an older unspoken one. Fully optional: if
    the engine can't start, JARVIS simply stays silent."""
    def __init__(self, on_state=None, voice_name_contains="",
                 voice_rate=VOICE_RATE_DEFAULT):
        self.enabled = True
        self.available = TTS_OK
        self._on_state = on_state or (lambda _speaking: None)
        self.voice_name_contains = str(voice_name_contains or "")
        try:
            rate = int(voice_rate)
        except (TypeError, ValueError):
            rate = VOICE_RATE_DEFAULT
        self.voice_rate = max(VOICE_RATE_MIN, min(VOICE_RATE_MAX, rate))
        self._pending = None
        self._wake = threading.Event()
        self._thread = None

    def say(self, text):
        if not self.available or not self.enabled or not text:
            return
        self._pending = text            # latest wins — drop the older queue
        self._wake.set()
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def _run(self):
        while self.available:
            self._wake.wait()
            self._wake.clear()
            text, self._pending = self._pending, None
            if text is None or not self.enabled:
                continue
            self._on_state(True)
            try:
                engine = pyttsx3.init()          # fresh engine per utterance
                _pick_voice(engine, self.voice_name_contains)
                engine.setProperty("rate", self.voice_rate)
                try:
                    engine.say(text)
                    engine.runAndWait()
                finally:
                    try: engine.stop()
                    except Exception:  # noqa: BLE001
                        pass
            except Exception:  # noqa: BLE001 — engine failed: go silent
                self.available = False
            finally:
                self._on_state(False)

# ------------------------------------------------- wake word listener ---
WAKE_ENERGY_THRESHOLD = 300   # mic sensitivity for the background listener
WAKE_PAUSE_THRESHOLD = 0.8    # seconds of silence that end a phrase

class WakeListener:
    """Always-listening wake-word watcher (opt-in, default OFF).

    SpeechRecognition's listen_in_background runs on its OWN native
    thread — that is precisely why it keeps hearing "Jarvis" while the
    HUD window is minimized or hidden in the tray, which no browser tab
    can do. Phrases are split by backend.parse_wake_phrase and posted
    to the app event queue; recognizer errors are swallowed so a bad
    phrase or a network hiccup never kills the listener. A mic that
    won't open makes start() return False (the caller forces the
    setting back OFF, like NOVA)."""
    def __init__(self, events):
        self.events = events
        self.running = False
        self.awaiting_command = False
        self._stopper = None

    def start(self):
        if self.running:
            return True
        if not SR_OK:
            return False
        try:
            recognizer = sr.Recognizer()
            recognizer.energy_threshold = WAKE_ENERGY_THRESHOLD
            recognizer.pause_threshold = WAKE_PAUSE_THRESHOLD
            mic = sr.Microphone()
            self._stopper = recognizer.listen_in_background(
                mic, self._callback, phrase_time_limit=8)
            self.running = True
            return True
        except Exception:  # noqa: BLE001 — OSError: no working mic
            self.running = False
            self._stopper = None
            return False

    def stop(self):
        self.awaiting_command = False
        if self._stopper is not None:
            try: self._stopper(wait_for_stop=False)
            except Exception:  # noqa: BLE001
                pass
            self._stopper = None
        self.running = False

    def _callback(self, _recognizer, audio):
        try:
            text = _recognizer.recognize_google(audio)
        except Exception:  # noqa: BLE001 — unknown speech / offline hiccup
            return
        try:
            if not text:
                return
            if self.awaiting_command:
                self.awaiting_command = False
                self.events.put(("wake_command", text))
                return
            command = backend.parse_wake_phrase(text)
            if command is None:
                return
            if command == "":
                self.awaiting_command = True
                self.events.put(("wake_ack", ""))
            else:
                self.events.put(("wake_command", command))
        except Exception:  # noqa: BLE001 — the listener must survive anything
            pass

# ------------------------------------------------------ screen watch ---
class ScreenWatcher:
    """Native Screen Watch service (background thread).

    Every interval it captures the screen IN MEMORY (base64 — never
    saved to disk, never logged), asks the brain for one suggestion via
    backend.Agent.screen_suggestion(), and delivers fresh, deduped
    suggestions through the injected deliver() callback. Failure policy
    is the v2.4 one from backend.advisor_failure_action: transient /
    timeout / network / daily-quota failures SKIP the cycle and the
    watcher stays ON; only a hard failure (dead model, bad key, vision
    unsupported) switches it OFF, reported once via on_hard_fail().

    All side effects are injected, so the state/dedupe decisions are
    unit-testable with stubs — no screen, mic or network in tests."""
    def __init__(self, capture, suggest, deliver, on_skip_note=None,
                 on_hard_fail=None, interval=SCREEN_WATCH_DEFAULT_INTERVAL,
                 normalize=None, should_deliver=None, failure_action=None):
        self._capture = capture
        self._suggest = suggest
        self._deliver = deliver
        self._on_skip_note = on_skip_note or (lambda: None)
        self._on_hard_fail = on_hard_fail or (lambda _err: None)
        self._normalize = normalize or backend.normalize_suggestion
        self._should_deliver = should_deliver or backend.should_deliver
        self._failure_action = failure_action or backend.advisor_failure_action
        try:
            interval = int(interval)
        except (TypeError, ValueError):
            interval = SCREEN_WATCH_DEFAULT_INTERVAL
        self.interval = max(SCREEN_WATCH_MIN_INTERVAL, interval)
        self.enabled = False
        self.last_delivered = None
        self._skip_noted = False      # one muted note per ON session
        self._busy = False
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        """Switch ON and (re)start the glance loop. A fresh ON session
        gets a fresh one-time skip note and a cleared stop flag."""
        self.enabled = True
        self._skip_noted = False
        self._stop.clear()
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def stop(self):
        self.enabled = False
        self._stop.set()

    def _loop(self):
        if self._stop.wait(1.5):      # first glance shortly after ON
            return
        while self.enabled and not self._stop.is_set():
            try:
                self.run_cycle()
            except Exception as e:  # noqa: BLE001 — last-resort guard:
                log_error("screen_watch", e)   # the watcher must never
            if self._stop.wait(self.interval):  # take the app down with it
                break

    def run_cycle(self):
        """One glance. Returns the outcome as a short string (this is
        also the unit-test surface):
        'disabled' | 'busy' | 'delivered' | 'quiet' | 'skipped' | 'hard-off'"""
        if not self.enabled:
            return "disabled"
        if self._busy:
            return "busy"
        self._busy = True
        try:
            image_b64 = self._capture()
            raw = self._suggest(image_b64)
        except Exception as e:  # noqa: BLE001 — routed by failure kind
            if self._failure_action(e) == "skip":
                if not self._skip_noted:
                    self._skip_noted = True
                    self._on_skip_note()
                return "skipped"
            self.stop()
            self._on_hard_fail(str(e))
            return "hard-off"
        finally:
            self._busy = False
        suggestion = self._normalize(raw)
        if self._should_deliver(suggestion, self.last_delivered):
            self.last_delivered = suggestion
            self._deliver(suggestion)
            return "delivered"
        return "quiet"

# ------------------------------------------------------------ HUD bridge ---
class HudBridge:
    """Thread-safe pipe into the HUD page. Native services call these;
    each becomes a window.JARVIS_NATIVE.* call via evaluate_js. Every
    call is best-effort — a HUD that isn't loaded yet (or a plain
    browser fallback with no window at all) must never crash a service.
    Lines produced before the window/page exist are BUFFERED and
    flushed when the page proves it is alive (its native_state call),
    so the startup self-check is never silently lost."""
    def __init__(self):
        self.window = None
        self._lock = threading.Lock()
        self._pending = []

    def _call(self, fn, arg):
        win = self.window
        if win is None:
            with self._lock:
                if len(self._pending) < 200:
                    self._pending.append((fn, arg))
            return
        script = ("window.JARVIS_NATIVE && window.JARVIS_NATIVE."
                  + fn + "(" + json.dumps(arg) + ")")
        with self._lock:
            try:
                win.evaluate_js(script)
            except Exception as e:  # noqa: BLE001
                log_error("hud_bridge", e)

    def flush(self):
        """Deliver buffered lines now that the page is alive."""
        with self._lock:
            pending, self._pending = self._pending, []
        for fn, arg in pending:
            self._call(fn, arg)

    def log(self, line):
        self._call("onNativeLog", str(line))

    def suggestion(self, text):
        self._call("onSuggestion", str(text))

    def wake_heard(self, text):
        self._call("onWakeHeard", str(text or ""))

    def set_screen_watch_state(self, on):
        self._call("setScreenWatchState", bool(on))


class JsApi:
    """The API the HUD page sees as window.pywebview.api."""
    def __init__(self, app):
        self._app = app

    def native_chat(self, text):
        """Typed HUD command: route it to the REAL brain (backend.chat),
        the same path voice commands already use. Before this, typed
        HUD commands never reached the brain at all — they ran the
        browser demo's handful of fake routines, so 'open pictures'
        only *said* it opened and anything else fell through to
        'No matching routine'. Returns the calm reply text for the
        HUD to display; speaking is done here so it is not doubled."""
        try:
            reply = self._app._get_agent().chat(str(text))
        except Exception as e:  # noqa: BLE001
            log_error("native_chat", e)
            reply = ("⚠ Something went wrong asking the brain, Sir — "
                     "it's written to jarvis.log. One more try usually settles it.")
        shown = calm_reply(reply)
        if shown != str(reply).strip():
            log_error("native_chat_details", reply)
        try:
            self._app.speaker.say(clamp_for_speech(shown))
        except Exception as e:  # noqa: BLE001
            log_error("native_chat_speak", e)
        return shown

    def native_minimize(self):
        """HUD Minimize button: hide JARVIS to the tray so the user can
        see other windows / tabs. Tray 'Open JARVIS' brings it back."""
        self._app.events.put(("hide_window", ""))
        return True

    def native_get_settings(self):
        """Settings panel: current brain settings, WITHOUT the API
        keys themselves — the panel shows 'key saved: yes/no' and
        only overwrites a key when the user types a new one. This is
        why JARVIS previously had no Settings button at all: the HUD
        never had a settings surface, so brain changes had to be made
        by editing config.json by hand or in NOVA."""
        cfg = self._app.config
        fb = cfg.get("fallback") or {}
        return {
            "provider": str(cfg.get("provider") or ""),
            "base_url": str(cfg.get("base_url") or ""),
            "model": str(cfg.get("model") or ""),
            "api_key_saved": bool(cfg.get("api_key")),
            "fallback_enabled": bool(fb.get("enabled", True)),
            "fallback_provider": str(fb.get("provider") or ""),
            "fallback_base_url": str(fb.get("base_url") or ""),
            "fallback_model": str(fb.get("model") or ""),
            "fallback_api_key_saved": bool(fb.get("api_key")),
        }

    def native_save_settings(self, settings_json):
        """Settings panel Save: update the shared config file
        (%APPDATA%\\NovaAgent\\config.json, same file NOVA reads) and
        return a human message. The next chat recreates the brain
        from the new config automatically, so no restart is needed.
        A blank API-key field keeps the old key — it never erases it."""
        try:
            data = json.loads(str(settings_json))
            if not isinstance(data, dict):
                return "Settings not saved — I did not understand that form, Sir."
            cfg = self._app.config
            for key in ("provider", "base_url", "model"):
                if key in data:
                    cfg[key] = str(data[key] or "").strip()
            if str(data.get("api_key") or "").strip():
                cfg["api_key"] = str(data["api_key"]).strip()
            fb = cfg.get("fallback") or {}
            if "fallback_enabled" in data:
                fb["enabled"] = bool(data["fallback_enabled"])
            for key in ("fallback_provider", "fallback_base_url", "fallback_model"):
                if key in data:
                    fb[key.replace("fallback_", "")] = str(data[key] or "").strip()
            if str(data.get("fallback_api_key") or "").strip():
                fb["api_key"] = str(data["fallback_api_key"]).strip()
            cfg["fallback"] = fb
            cfg["model"] = migrate_model_name(cfg.get("model"))
            save_config(cfg)
            # Apply immediately, without closing JARVIS or quitting
            # from the tray: drop the cached brain so the next chat
            # builds a fresh one from the just-saved config.
            try:
                self._app._agent = None
                self._app._agent_snap = None
            except Exception:  # noqa: BLE001 — cache reset is best-effort
                pass
            return ("Settings saved, Sir — the new brain will be used on your next "
                    "message, no restart needed. Try 'hello' to test it.")
        except Exception as e:  # noqa: BLE001
            log_error("native_save_settings", e)
            return "Settings not saved — something went wrong, Sir. It is in jarvis.log."

    def native_screen_watch(self, on):
        """HUD chip toggled: start/stop the REAL native watcher."""
        try:
            return self._app.set_screen_watch(bool(on), source="hud")
        except Exception as e:  # noqa: BLE001 — one calm line, never a crash
            log_error("screen_watch_toggle", e)
            self._app.hud.log("👁 Screen Watch hit a snag, Sir — it's logged; everything else is unaffected.")
            return False

    def native_state(self):
        # The page calls this on pywebviewready — proof it can receive
        # evaluate_js now, so flush anything buffered during startup.
        try: self._app.hud.flush()
        except Exception as e:  # noqa: BLE001
            log_error("hud_flush", e)
        cfg = self._app.config
        return {"screen_watch": bool(cfg.get("screen_watch_enabled", False)),
                "wake_word": bool(cfg.get("wake_word_enabled", False)),
                "voice": bool(cfg.get("voice_enabled", True))}


# ------------------------------------------------------------------- app ---
class JarvisApp:
    def __init__(self, config):
        self.config = config
        # JARVIS's own computer: create its workspace on startup, so
        # ~/JARVIS-Computer/Work, Downloads and Notes exist whether or
        # not the user has asked about them yet — like Muse's own
        # computer, but on this PC. JARVIS can still access the user's
        # computer through its other tools when asked.
        try:
            self.workspace = backend.jarvis_workspace_path()
        except Exception:  # noqa: BLE001
            self.workspace = None
        self.events = queue.Queue()
        self.hud = HudBridge()
        self.speaker = Speaker(
            voice_name_contains=config.get("voice_name_contains", ""),
            voice_rate=config.get("voice_rate", VOICE_RATE_DEFAULT))
        self.speaker.enabled = bool(config.get("voice_enabled", True))
        self._agent = None
        self._agent_snap = None
        self._wake_listener = None
        self._tray_icon = None
        self._tray_notified = False
        self._really_quit = False
        self._on_restore = None       # window hooks, set by _run_window
        self._on_hide = None
        self._on_quit = None
        self._stop_event = threading.Event()
        self.watcher = ScreenWatcher(
            capture=backend.capture_screen_b64,
            suggest=lambda b64: self._get_agent().screen_suggestion(b64),
            deliver=lambda s: self.events.put(("watch_suggestion", s)),
            on_skip_note=lambda: self.events.put(("watch_skip_note", "")),
            on_hard_fail=lambda err: self.events.put(("watch_hard_fail", err)),
            interval=config.get("screen_watch_interval_sec",
                                SCREEN_WATCH_DEFAULT_INTERVAL))

    # ------------------------------------------------------------- brain ---
    def _agent_snapshot(self):
        c = self.config
        fb = c.get("fallback") or {}
        return (c.get("provider"), c.get("api_key"), c.get("model"),
                c.get("base_url"), fb.get("enabled"), fb.get("provider"),
                fb.get("api_key"), fb.get("model"), fb.get("base_url"),
                tuple(c.get("protected_paths") or []))

    def _get_agent(self):
        snap = self._agent_snapshot()
        if self._agent is None or snap != self._agent_snap:
            self._agent = backend.Agent(self.config, on_tool=self._on_tool)
            self._agent_snap = snap
        return self._agent

    def _on_tool(self, name, detail):
        self.hud.log(f"⚙ {name}  {str(detail)[:160]}")
        if name in ("search_download", "download_file", "youtube_upload"):
            # Completion notification: tray popup when minimized, plus
            # the HUD log above. The chat reply is also spoken, so the
            # user hears it even if the tray is not visible.
            if self._tray_icon is not None:
                try: self._tray_icon.notify(str(detail)[:200], "JARVIS")
                except Exception:  # noqa: BLE001
                    pass
        if name == "open_path":
            # A folder/app just opened, but JARVIS is fullscreen — if it
            # stays on top, the user never sees what opened. Hide to
            # the tray so Explorer / the app comes to the front; the
            # tray icon (or the HUD Minimize button) brings JARVIS back.
            self.events.put(("hide_window", ""))

    # ------------------------------------------------------ screen watch ---
    def set_screen_watch(self, on, source="hud"):
        """Start/stop the native watcher and make every surface (config,
        HUD chip, tray checkmark) agree. Returns the resulting state."""
        on = bool(on)
        self.config["screen_watch_enabled"] = on
        save_config(self.config)
        if on:
            if str(self.config.get("provider", "")).lower() != "ollama":
                # Cloud brains charge one request per glance and free
                # tiers allow very few requests per DAY — warn once per
                # enable so the watcher can't silently eat the quota.
                self.hud.log("ℹ Screen Watch uses one API request per look — "
                             "free cloud quotas are small (Gemini free ≈ 20/day). "
                             "Ollama brains have no quota.")
            self.watcher.start()
        else:
            self.watcher.stop()
        self.hud.set_screen_watch_state(on)
        self._update_tray_menu()
        return on

    def _watch_suggestion(self, text):
        """Main delivery path for a fresh suggestion: tray popup (the
        only surface while minimized), voice, and the HUD log."""
        if not self.config.get("screen_watch_enabled", False):
            return   # switched off while the vision call was in flight
        self.hud.suggestion(text)
        if self._tray_icon is not None:
            try: self._tray_icon.notify(text, "💡 JARVIS suggests")
            except Exception:  # noqa: BLE001
                pass
        self.speaker.say(clamp_for_speech(f"Suggestion: {text}"))

    def _watch_hard_fail(self, err):
        """Vision is genuinely unavailable: switch Screen Watch OFF once
        (with an explanation) instead of failing every interval."""
        self.config["screen_watch_enabled"] = False
        save_config(self.config)
        self.hud.set_screen_watch_state(False)
        self._update_tray_menu()
        self.hud.log("👁 Screen Watch turned OFF — vision isn't available "
                     f"on the current brain ({err}). Pick a vision-capable "
                     "model in the config, then turn it back on.")

    # ---------------------------------------------------------- wake word ---
    def set_wake(self, on):
        """Start/stop the background wake listener. Fails safe like
        NOVA: if the mic stack is missing or the mic won't open, the
        setting is forced back OFF with one clear log line."""
        on = bool(on)
        if on and not SR_OK:
            self.config["wake_word_enabled"] = False
            save_config(self.config)
            self.hud.log("🎧 Wake word unavailable (SpeechRecognition/PyAudio "
                         "not installed) — left OFF.")
            self._update_tray_menu()
            return False
        if on:
            if self._wake_listener is None:
                self._wake_listener = WakeListener(self.events)
            if not self._wake_listener.running and not self._wake_listener.start():
                self.config["wake_word_enabled"] = False
                save_config(self.config)
                self.hud.log("🎧 Wake word couldn't start — no microphone "
                             "found/working on this PC, so it stays OFF. "
                             + NO_MIC_MSG)
                self._update_tray_menu()
                return False
        elif self._wake_listener is not None:
            self._wake_listener.stop()
        self.config["wake_word_enabled"] = on
        save_config(self.config)
        self._update_tray_menu()
        return on

    def set_voice(self, on):
        self.config["voice_enabled"] = bool(on)
        self.speaker.enabled = bool(on)
        save_config(self.config)
        self._update_tray_menu()

    # ------------------------------------------------------------- commands ---
    def _run_command(self, text):
        """A spoken (or otherwise routed) command: ask the brain on a
        worker thread, show the reply in the HUD, speak it clamped to
        at most 4 sentences. The user only ever sees the calm message —
        raw error details go to jarvis.log, never the screen."""
        try:
            reply = self._get_agent().chat(text)
        except Exception as e:  # noqa: BLE001
            log_error("chat", e)
            reply = ("⚠ Something went wrong asking the brain, Sir — "
                     "it's written to jarvis.log. One more try usually settles it.")
        shown = calm_reply(reply)
        if shown != str(reply).strip():
            log_error("chat_details", reply)
        self.hud.log("JARVIS: " + shown)
        self.speaker.say(clamp_for_speech(shown))

    # ---------------------------------------------------------- self-check ---
    def self_check(self):
        """Fast startup health report — deliberately NO model call, so
        it can never burn quota. Returns the lines; each also goes to
        the HUD log (buffered until the page is alive) so Sir sees
        exactly what is ready and the exact fix for anything missing."""
        lines = []
        cfg = self.config
        try:
            CFG_DIR.mkdir(parents=True, exist_ok=True)
            lines.append(f"[SELF-CHECK] Config folder ready — {CFG_DIR}")
        except Exception as e:  # noqa: BLE001
            log_error("self_check", e)
            lines.append("[SELF-CHECK] ⚠ Config folder not writable, Sir — "
                         "settings won't persist. Fix: check permissions on "
                         f"{CFG_DIR}")
        provider = str(cfg.get("provider") or "anthropic")
        if provider.lower() == "ollama":
            lines.append("[SELF-CHECK] Brain: Ollama (local — no API key needed).")
        elif cfg.get("api_key"):
            lines.append(f"[SELF-CHECK] Brain: {provider} · "
                         f"{cfg.get('model') or 'default model'} — key present.")
        else:
            lines.append("[SELF-CHECK] ⚠ Brain has NO API key, Sir — chat "
                         "can't answer until one is set. Fix: add \"api_key\" "
                         f"in {CFG_FILE} (the same file NOVA uses), or switch "
                         "the provider to ollama.")
        n = len(cfg.get("protected_paths") or [])
        lines.append(f"[SELF-CHECK] Protected list loaded — {n} item(s) "
                     "JARVIS will never touch.")
        if SR_OK:
            lines.append("[SELF-CHECK] Microphone stack ready — wake word "
                         "can be switched on from the tray.")
        else:
            lines.append("[SELF-CHECK] Microphone stack not found — wake word "
                         "stays OFF; typed HUD commands still go to the real brain.")
        if TTS_OK and self.speaker.available:
            lines.append("[SELF-CHECK] Voice ready — replies will be spoken.")
        else:
            lines.append("[SELF-CHECK] Voice unavailable — replies will "
                         "appear as text only.")
        lines.append(f"[SELF-CHECK] Screen Watch is "
                     f"{'ON' if cfg.get('screen_watch_enabled') else 'OFF'}; "
                     f"wake word is {'ON' if cfg.get('wake_word_enabled') else 'OFF'}.")
        for ln in lines:
            self.hud.log(ln)
        return lines

    # ------------------------------------------------------------- tray ---
    def _post(self, kind):
        """Tray callbacks do exactly one thing — post an event — and
        even that is guarded: a tray hiccup must never crash the app."""
        try:
            self.events.put((kind, ""))
        except Exception as e:  # noqa: BLE001
            log_error("tray", e)

    def _tray_image(self):
        try:
            return Image.open(bundled_file("icon.ico")).convert("RGBA")
        except Exception:  # noqa: BLE001 — fall back to a drawn reactor dot
            img = Image.new("RGBA", (64, 64), (5, 11, 20, 255))
            d = ImageDraw.Draw(img)
            d.ellipse((8, 8, 56, 56), outline=(0, 229, 255, 255), width=4)
            d.ellipse((24, 24, 40, 40), fill=(0, 229, 255, 255))
            return img

    def _start_tray(self):
        """Create the tray icon once, on its own thread. Callbacks never
        touch services directly — they post events to the app queue."""
        if not TRAY_OK or self._tray_icon is not None:
            return
        cfg = self.config
        menu = pystray.Menu(
            pystray.MenuItem("Open JARVIS",
                             lambda _i, _it: self._post("tray_restore"),
                             default=True),
            pystray.MenuItem(
                lambda _it: "Wake word: " + ("On" if cfg.get("wake_word_enabled") else "Off"),
                lambda _i, _it: self._post("tray_toggle_wake"),
                checked=lambda _it: bool(cfg.get("wake_word_enabled"))),
            pystray.MenuItem(
                lambda _it: "Screen Watch: " + ("On" if cfg.get("screen_watch_enabled") else "Off"),
                lambda _i, _it: self._post("tray_toggle_watch"),
                checked=lambda _it: bool(cfg.get("screen_watch_enabled"))),
            pystray.MenuItem(
                lambda _it: "Voice: " + ("On" if cfg.get("voice_enabled", True) else "Off"),
                lambda _i, _it: self._post("tray_toggle_voice"),
                checked=lambda _it: bool(cfg.get("voice_enabled", True))),
            pystray.MenuItem("Quit JARVIS",
                             lambda _i, _it: self._post("tray_quit")),
        )
        self._tray_icon = pystray.Icon("JARVIS", self._tray_image(),
                                       "JARVIS — Personal AI Agent", menu)
        threading.Thread(target=self._tray_icon.run, daemon=True).start()

    def _update_tray_menu(self):
        if self._tray_icon is not None:
            try: self._tray_icon.update_menu()
            except Exception:  # noqa: BLE001
                pass

    def _notify_hidden(self):
        if self._tray_icon is not None and not self._tray_notified:
            self._tray_notified = True
            try:
                self._tray_icon.notify(
                    "JARVIS is still running here in the tray — right-click "
                    "for options, or just say \"Jarvis\".", "JARVIS")
            except Exception:  # noqa: BLE001
                pass

    # -------------------------------------------------------- event pump ---
    def _dispatch_loop(self):
        """Single consumer for every service event, so tray threads, the
        wake listener and the watcher never race each other."""
        while not self._stop_event.is_set():
            try:
                kind, payload = self.events.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                if kind == "wake_ack":
                    self.hud.wake_heard("")
                    if winsound is not None:
                        try: winsound.Beep(880, 120)
                        except Exception:  # noqa: BLE001
                            pass
                    self.speaker.say("Yes, Sir?")
                elif kind == "wake_command":
                    self.hud.wake_heard(payload)
                    threading.Thread(target=self._run_command,
                                     args=(payload,), daemon=True).start()
                elif kind == "watch_suggestion":
                    self._watch_suggestion(payload)
                elif kind == "watch_skip_note":
                    self.hud.log("👁 Screen Watch: model busy, will look again shortly.")
                elif kind == "watch_hard_fail":
                    self._watch_hard_fail(payload)
                elif kind == "tray_restore":
                    if self._on_restore is not None:
                        self._on_restore()
                elif kind == "hide_window":
                    if self._on_hide is not None:
                        self._on_hide()
                    else:
                        try:
                            self.hud.window.hide()
                        except Exception:  # noqa: BLE001
                            pass
                    self._notify_hidden()
                elif kind == "tray_toggle_wake":
                    self.set_wake(not self.config.get("wake_word_enabled", False))
                elif kind == "tray_toggle_watch":
                    self.set_screen_watch(
                        not self.config.get("screen_watch_enabled", False),
                        source="tray")
                elif kind == "tray_toggle_voice":
                    self.set_voice(not self.config.get("voice_enabled", True))
                elif kind == "tray_quit":
                    self._stop_event.set()
                    if self._on_quit is not None:
                        self._on_quit()
            except Exception:  # noqa: BLE001 — the pump must survive anything
                pass

    # ------------------------------------------------------------ lifecycle ---
    def start_services(self):
        try:
            self.self_check()
        except Exception as e:  # noqa: BLE001 — even the check is guarded
            log_error("self_check", e)
        self._start_tray()
        threading.Thread(target=self._dispatch_loop, daemon=True).start()
        if self.config.get("wake_word_enabled"):
            self.set_wake(True)
        if self.config.get("screen_watch_enabled"):
            self.set_screen_watch(True, source="startup")

    def shutdown(self):
        self._really_quit = True
        self._stop_event.set()
        if self._wake_listener is not None:
            self._wake_listener.stop()
        self.watcher.stop()
        if self._tray_icon is not None:
            try: self._tray_icon.stop()
            except Exception:  # noqa: BLE001
                pass
            self._tray_icon = None


def _run_window(app):
    """The HUD in its own pywebview window. Close/minimize hide to the
    tray — JARVIS keeps listening and watching from there. Returns when
    the window is really destroyed (tray Quit)."""
    hud_url = bundled_file("jarvis-hud.html").as_uri()
    window = webview.create_window("J.A.R.V.I.S.", url=hud_url,
                                   js_api=JsApi(app), fullscreen=True)
    app.hud.window = window

    def on_closing():
        if app._really_quit:
            return True
        try: window.hide()
        except Exception:  # noqa: BLE001
            pass
        app._notify_hidden()
        return False    # veto the close — hide to tray instead

    def on_minimized():
        if not app._really_quit:
            try: window.hide()
            except Exception:  # noqa: BLE001
                pass
            app._notify_hidden()

    try:
        window.events.closing += on_closing
    except Exception:  # noqa: BLE001
        pass
    try:
        window.events.minimized += on_minimized
    except Exception:  # noqa: BLE001
        pass

    def _restore():
        try:
            window.show()
            window.restore()
        except Exception:  # noqa: BLE001
            pass

    def _hide():
        try:
            window.hide()
        except Exception:  # noqa: BLE001
            pass

    def _quit():
        app.shutdown()
        try: window.destroy()
        except Exception:  # noqa: BLE001
            pass

    # Single event consumer (the dispatcher started in start_services);
    # it reaches the window through these hooks.
    app._on_restore = _restore
    app._on_hide = _hide
    app._on_quit = _quit
    webview.start()
    app.shutdown()


def _run_headless(app):
    """Fallback face: the HUD in the default browser (demo mode there),
    while the native services keep running behind the tray icon."""
    webbrowser.open(bundled_file("jarvis-hud.html").as_uri())
    print("HUD opened in your browser. Native services are running; "
          "use the tray icon to quit.")
    app._stop_event.wait()
    app.shutdown()


def main():
    # Last-resort nets: an exception ANYWHERE is written to jarvis.log,
    # never splashed across the user's screen as a traceback.
    sys.excepthook = lambda *a: log_error("uncaught", a[1] if len(a) > 1 else a)
    def _thread_hook(args):
        log_error("thread", getattr(args, "exc_value", None) or str(args))
    threading.excepthook = _thread_hook

    try:
        config = load_config()
    except Exception as e:  # noqa: BLE001 — load_config already defaults
        log_error("config", e)          # on bad files; this is belt+braces
        config = dict(DEFAULTS)
    app = JarvisApp(config)
    try:
        app.start_services()
    except Exception as e:  # noqa: BLE001
        log_error("services", e)
        app.hud.log("⚠ Some background services failed to start, Sir — "
                    "details are in jarvis.log. The HUD still works.")
    if webview is not None:
        try:
            _run_window(app)
        except Exception as e:  # noqa: BLE001 — WebView2 missing/broken:
            log_error("webview", e)     # fall back to the browser face
            print("The app window failed to open — using your browser instead.")
            _run_headless(app)
    else:
        _run_headless(app)


if __name__ == "__main__":
    main()
