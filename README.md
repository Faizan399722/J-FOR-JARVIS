# JARVIS — Native Edition

The JARVIS HUD with a real body. The demo HUD (a web page) could only
*pretend*: a browser tab stops listening when it is minimized and can't
look at your screen on a timer. This native edition runs the abilities
as **background services inside the .exe itself**:

- 🎧 **Wake word that works minimized** — say **"Jarvis"** (or "Hey
  Jarvis"; "Nova" still works) and JARVIS answers out loud — *"Yes,
  Sir?"* — then takes your command, even with the window minimized or
  hidden in the system tray. Say *"Jarvis, open downloads"* in one
  breath and it just does it.
- 👁 **Screen Watch that really watches** — while ON, JARVIS glances
  at your screen every interval, asks its vision brain for the single
  best next step, and delivers it by tray popup + voice + HUD log.
  Suggestions are deduped (never the same one twice), a busy/quota-hit
  brain just skips that glance, and **screenshots are never saved to
  disk or logged** — they exist in memory only for the one request.
- ⬇️ **Downloads & installs by voice** — *"Jarvis, download <link>"*
  fetches the file straight to `~/Downloads` (no browser, no
  clicking). *"Jarvis, install <file>"* runs an already-downloaded
  installer silently — after your spoken **"confirm"** (see below).
- 🛡 **The Protected List is still the one law** — files/folders in
  `protected_paths` are never read, opened, moved, deleted, downloaded
  into, or installed from. Refusals are instant and total.
- 🧏 **A calmer, deeper voice** — replies are spoken in a British-male
  JARVIS register when Windows offers one (Ryan/George/Daniel/Arthur,
  then David), at a measured rate (≈165). Spoken replies are kept to
  1–4 sentences; the full text is always on screen.
- 🌌 **A living background** — behind the panels, a slow 3D starfield
  with holographic rings drifts at low opacity. If the frame rate ever
  dips, the background sheds detail *first* so the interface itself
  never stutters — and rendering pauses entirely while hidden.

## Your existing setup carries over

JARVIS reads the **same config file as NOVA**:
`%APPDATA%\NovaAgent\config.json`. Your provider, API keys (Gemini /
Groq), model, fallback brain and protected list are already there — no
re-typing. JARVIS adds its own keys next to them:
`wake_word_enabled` (default **false**), `screen_watch_enabled`
(default **false**), `screen_watch_interval_sec` (default 90, min 30),
`voice_name_contains` (default "" — force a specific voice name) and
`voice_rate` (default 165). NOVA's older `screen_advisor_*` keys are
honoured as an alias.

## Turn the two headline features on

Both are **off by default** (your rule: nothing watches or listens
until you say so):

- **Wake word:** right-click the tray icon → *Wake word: On* — or set
  `"wake_word_enabled": true` in the config and restart.
- **Screen Watch:** click the **SCREEN WATCH** chip in the HUD's top
  bar, use the tray menu, or set `"screen_watch_enabled": true`. The
  chip/tray checkmark always shows the true state.
- **Quit:** closing or minimizing the window only hides JARVIS to the
  tray. *Quit JARVIS* in the tray menu is the only real exit.

## Downloads & installs — the honest limits

- Downloads come from the URL **you** give; prefer official sources
  (the maker's own site). The file name comes from the server/URL, an
  existing file is never overwritten (`name (1).ext` instead), and a
  protected destination is refused.
- Installs are **two-step by your rule**: the first request changes
  nothing — JARVIS states the plan (which installer, which silent
  flags, that system files/settings may change) and waits. Only after
  you say **"confirm"** does it run.
- Silent flags are chosen automatically: `.msi` →
  `msiexec /i … /qn /norestart`; Inno Setup → `/VERYSILENT …`;
  NSIS and most other `.exe` installers → `/S`. The real exit code is
  always reported honestly (0 = success, 3010 = success + restart
  needed, 1603 = usually a permissions problem…).
- **UAC:** if Windows shows the admin (UAC) prompt, it appears on the
  secure desktop — *no program can click it for you*. Approve it by
  hand (or it times out) and ask again.
- **GUI-only installers:** a few installers have no silent mode at
  all; they may open their window despite the flags. JARVIS reports
  what actually happened rather than pretending.

## Errors — you will not see them

Every service is wrapped: no tracebacks, no raw JSON error walls.
Failures are written to **`jarvis.log`** (in `%APPDATA%\NovaAgent\`)
and you get **one calm line** in the HUD log instead. Degradation is
graceful: no microphone → wake word turns itself off with one friendly
line and typing keeps working; no API key → one clear instruction;
brain busy/quota/network → the fallback brain or a skipped glance;
TTS engine dead → replies still appear as text; window engine dead →
the HUD opens in your browser while services keep running.
On startup, JARVIS types a **[SELF-CHECK]** report into the HUD log —
config, brain/key, protected list, mic, voice — each green, or with
the exact fix for whatever is missing. It makes **no** model call, so
it never burns quota.

## Build JARVIS.exe (GitHub — the way NOVA was built)

1. Create a **new private repo** on GitHub (suggested name:
   `JARVIS-NATIVE`) and upload **all** files from this folder
   (`jarvis.py`, `backend.py`, `jarvis-hud.html`, `requirements.txt`,
   `build.bat`, `icon.ico`, `README.md`; `tests/` optional).
2. ⚠️ **The workflow file must end up at
   `.github/workflows/build-exe.yml`.** GitHub's web upload has a habit
   of flattening it to the repo root, where Actions ignores it. If
   that happens: **Add file → Create new file**, type the path
   `.github/workflows/build-exe.yml`, paste the file's contents,
   commit.
3. Open the **Actions** tab and wait for the green run
   ("Build JARVIS.exe").
4. Download the **JARVIS-exe** artifact from the run page — use
   **Microsoft Edge** (Chrome tends to block the zip as a "dangerous
   file"; in Edge: ⋯ → **Keep** → **Keep anyway**). Delete any older
   JARVIS zips from Downloads first so there's no mix-up.
5. Unzip, run **JARVIS.exe**. Windows SmartScreen will warn about an
   unsigned app: **More info → Run anyway**. It's your own build.

Prefer building on your own PC? Install Python 3.11+, unzip this
folder, double-click **build.bat** — the exe lands in `dist\`.

## Files

| File | What it is |
|---|---|
| `jarvis.py` | Native launcher: HUD window, tray, wake listener, screen watcher, TTS, self-check, error log |
| `backend.py` | The brain (from NOVA, JARVIS wake words added): chat + tools, protected-paths, downloads/installs, vision, quota/transient policies |
| `jarvis-hud.html` | The HUD face — same demo file, plus a native bridge that activates only inside this shell |
| `tests/` | 131 unit tests (brain logic, wake parsing, watcher decisions, downloads/installs, voice picker, self-check) — no mic/screen/network needed |
| `requirements.txt` | pywebview, pystray, SpeechRecognition, pyaudio, pyttsx3, Pillow, pyinstaller |
| `build.bat` / `.github/workflows/build-exe.yml` | Local / GitHub builds of JARVIS.exe |

## Honest notes

- Screen Watch sends each glance (a screenshot) to your **configured
  cloud brain** while ON — that's how it sees. A vision-capable local
  **Ollama** model keeps screenshots on your PC entirely.
- The wake word needs a **working microphone**; with none present it
  stays off and tells you once, plainly.
- Voice *recognition* uses the free Google speech service (via
  SpeechRecognition), so wake commands need internet; a fully offline
  recognizer is a possible future upgrade.
- Everything here is code-verified and unit-tested, but the real
  Windows test — your mic, your voices, WebView2, your screen — only
  happens on your PC. If anything misbehaves, `jarvis.log` has the
  details; send a screenshot as usual.
