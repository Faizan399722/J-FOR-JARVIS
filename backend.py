"""JARVIS — agent brain (native edition).

This is the NOVA agent brain, carried over unchanged except for the name
and the wake words: it talks to an AI model (Anthropic Claude, OpenAI,
or any OpenAI-compatible endpoint such as Google Gemini or a local
Ollama) and gives it full access to this computer through tools. Per the
owner's explicit choice there are NO approval prompts: tools execute
immediately, even risky ones. The one and only restriction is the owner's
protected-paths list (config → protected_paths): any file or folder on
that list is completely off-limits to every tool, including commands
that merely mention it. No third-party packages are needed here: only
the stdlib.

Auto-fallback: if the main brain can't be reached (e.g. the internet is
down) or its free daily quota is finished, chat() automatically retries
the message once on a fallback brain (default: local Ollama), then
switches back so recovery is automatic.
"""
import base64, fnmatch, io, json, os, platform, re, shutil, socket, subprocess, sys, time, urllib.request, urllib.error, urllib.parse, webbrowser
from pathlib import Path

# ------------------------------------------- protected paths (the rule) ---
# The ONLY restriction in NOVA. Entries may be:
#   * an exact file/folder path        "C:\\Users\\me\\Secrets\\id.txt"
#   * a folder (everything under it)   "C:\\Users\\me\\Secrets"
#   * a simple glob pattern            "*.key"  /  "private_*"
#   * a bare file/folder name          "vault"  (matches anywhere)
PROTECTED_PATHS = []

def set_protected_paths(entries):
    """Replace the protected list (called by Agent from config)."""
    global PROTECTED_PATHS
    PROTECTED_PATHS = [str(e).strip() for e in (entries or []) if str(e).strip()]

def _norm_path(p):
    return os.path.normcase(os.path.abspath(
        os.path.expandvars(os.path.expanduser(str(p)))))

def _is_glob(entry):
    return any(ch in entry for ch in "*?[")

def protected_match(path):
    """Return the protected entry that covers `path`, or None."""
    if not PROTECTED_PATHS:
        return None
    target = _norm_path(path)
    name = Path(target).name.lower()
    parts = [c.lower() for c in Path(target).parts]
    for entry in PROTECTED_PATHS:
        if _is_glob(entry):
            pat = entry.lower()
            if fnmatch.fnmatch(target.lower(), pat) or fnmatch.fnmatch(name, pat):
                return entry
            continue
        e = _norm_path(entry)
        if target == e or target.startswith(e + os.sep):
            return entry
        bare = entry.strip("/\\")
        if "/" not in bare and "\\" not in bare and bare.lower() in parts:
            return entry
    return None

def command_mentions_protected(command):
    """Return the protected entry mentioned in a shell command, or None."""
    if not PROTECTED_PATHS:
        return None
    cmd = str(command).replace("\\", "/").lower()
    tokens = [t.strip("\"'") for t in cmd.replace('"', " ").replace("'", " ").split()]
    for entry in PROTECTED_PATHS:
        if _is_glob(entry):
            pat = entry.lower()
            core = pat.strip("*")
            if core and core in cmd:
                return entry
            if any(fnmatch.fnmatch(t, pat) or fnmatch.fnmatch(t.rsplit("/", 1)[-1], pat)
                   for t in tokens):
                return entry
            continue
        e = _norm_path(entry).replace("\\", "/").lower()
        if e in cmd:
            return entry
        bare = entry.strip("/\\")
        if "/" not in bare and "\\" not in bare and bare.lower() in cmd:
            return entry
    return None

# ---------------------------------------------------------- wake word ---
# JARVIS is the primary name now; "nova" keeps working so phrases learned
# on the older build don't suddenly stop being heard.
WAKE_WORDS = ("hey jarvis", "jarvis", "hey nova", "nova")

def parse_wake_phrase(text, wake_words=WAKE_WORDS):
    """Split a recognized phrase into (wake word + optional command).

    Pure function (no audio / GUI) so it can be unit-tested anywhere.
    Returns:
      * None  — the phrase doesn't address NOVA; ignore it.
      * ""    — wake word only ("Nova", "Hey Nova!"): acknowledge and
                treat the NEXT utterance as the command.
      * str   — the command that followed the wake word, e.g.
                "hey nova open downloads" -> "open downloads".
    Matching is case-insensitive on whole words, so "supernova" does not
    trigger it. When several wake words match, the earliest (then longest)
    wins; anything before the wake word is discarded.
    """
    if not text:
        return None
    lowered = str(text).lower()
    best = None  # (start, end) of the winning wake-word match
    for w in wake_words:
        m = re.search(r"\b" + re.escape(str(w).lower()) + r"\b", lowered)
        if m and (best is None or m.start() < best[0]
                  or (m.start() == best[0] and m.end() > best[1])):
            best = (m.start(), m.end())
    if best is None:
        return None
    return str(text)[best[1]:].strip(" \t\r\n,.:;!?-")

def _protected_refusal(path):
    return {"ok": False,
            "error": f"⛔ Protected by your rule — I don't touch {path}."}

# ---------------------------------------------------------------- tools ---

def _ok(**kw):
    out = {"ok": True}; out.update(kw); return out

def tool_system_info(_a):
    import socket
    disk = shutil.disk_usage(str(Path.home().anchor))
    return _ok(system=f"{platform.system()} {platform.release()} ({platform.version()})",
               machine=platform.machine(), processor=platform.processor() or "n/a",
               hostname=socket.gethostname(), user=os.environ.get("USERNAME") or os.environ.get("USER", ""),
               home=str(Path.home()), cwd=os.getcwd(),
               disk_free_gb=round(disk.free / 1e9, 1), disk_total_gb=round(disk.total / 1e9, 1),
               python=sys.version.split()[0])

def tool_list_files(a):
    p = Path(os.path.expandvars(os.path.expanduser(a.get("path") or str(Path.home()))))
    if protected_match(p): return _protected_refusal(p)
    if not p.exists(): return {"ok": False, "error": f"Path not found: {p}"}
    items = []
    for e in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))[:300]:
        try: size = e.stat().st_size if e.is_file() else None
        except OSError: size = None
        items.append({"name": e.name, "type": "folder" if e.is_dir() else "file", "size": size})
    return _ok(path=str(p), items=items)

def tool_read_file(a):
    p = Path(os.path.expandvars(os.path.expanduser(a["path"])))
    if protected_match(p): return _protected_refusal(p)
    if not p.is_file(): return {"ok": False, "error": f"Not a file: {p}"}
    if p.stat().st_size > 2_000_000: return {"ok": False, "error": "File is larger than 2 MB; read a smaller part or another file."}
    try: text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e: return {"ok": False, "error": str(e)}
    return _ok(path=str(p), content=text[:20000], truncated=len(text) > 20000)

def tool_write_file(a):
    p = Path(os.path.expandvars(os.path.expanduser(a["path"])))
    if protected_match(p): return _protected_refusal(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(a.get("content", ""), encoding="utf-8")
    return _ok(path=str(p), bytes=p.stat().st_size)

def tool_delete_path(a):
    p = Path(os.path.expandvars(os.path.expanduser(a["path"])))
    if protected_match(p): return _protected_refusal(p)
    if not p.exists(): return {"ok": False, "error": f"Not found: {p}"}
    if p.is_dir(): shutil.rmtree(p)
    else: p.unlink()
    return _ok(deleted=str(p))

def tool_run_command(a):
    cmd = a["command"]
    hit = command_mentions_protected(cmd)
    if hit: return _protected_refusal(hit)
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120,
                           encoding="utf-8", errors="replace")
        return _ok(exit_code=r.returncode, stdout=(r.stdout or "")[-8000:], stderr=(r.stderr or "")[-3000:])
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Command timed out after 120 seconds."}

def tool_open_path(a):
    target = os.path.expandvars(os.path.expanduser(a["path"]))
    if target.startswith(("http://", "https://")):
        webbrowser.open(target); return _ok(opened=target)
    if protected_match(target): return _protected_refusal(target)
    p = Path(target)
    if not p.exists(): return {"ok": False, "error": f"Not found: {target}"}
    if sys.platform.startswith("win"): os.startfile(str(p))          # noqa: S606
    elif sys.platform == "darwin": subprocess.Popen(["open", str(p)])
    else: subprocess.Popen(["xdg-open", str(p)])
    return _ok(opened=str(p))

def tool_search_files(a):
    root = Path(os.path.expandvars(os.path.expanduser(a.get("root") or str(Path.home()))))
    if protected_match(root): return _protected_refusal(root)
    needle = a["name_contains"].lower(); found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in ("node_modules", ".git", "__pycache__", "AppData")
                       and not protected_match(os.path.join(dirpath, d))]
        for n in filenames:
            full = os.path.join(dirpath, n)
            if needle in n.lower() and not protected_match(full):
                found.append(full)
        if len(found) >= 100: break
    return _ok(matches=found, truncated=len(found) >= 100)

# ------------------------------------------------- downloads & installs ---
# Voice-driven software fetching, the native way (no browser, no clicking):
# download_file streams a URL straight to ~/Downloads (or a named folder);
# install_software runs an already-downloaded installer silently. Per the
# owner's INSTALLS rule, installs are two-step: the first call (without
# confirmed=true) changes nothing and returns the plan; only after the
# user says "confirm" does the caller pass confirmed=true and it runs.

def _open_url(req):
    """Indirection over urllib.request.urlopen (60s timeout) so tests can
    stub the network without touching real sockets."""
    return urllib.request.urlopen(req, timeout=60)

def derive_filename(url, content_disposition=None):
    """Best file name for a download: Content-Disposition wins, then the
    URL's last path segment, then a generic name. Sanitized for Windows.
    Pure function."""
    name = ""
    if content_disposition:
        m = re.search(r"filename\*\s*=\s*(?:[^']*'[^']*')?\"?([^\";]+)\"?",
                      content_disposition)
        if not m:
            m = re.search(r"filename\s*=\s*\"?([^\";]+)\"?", content_disposition)
        if m:
            name = urllib.parse.unquote(m.group(1).strip())
    if not name:
        path = urllib.parse.urlparse(str(url)).path
        name = urllib.parse.unquote(path.rsplit("/", 1)[-1])
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .")
    if not name:
        name = "download.bin"
    return name[:150]

def _unique_dest(folder, name):
    dest = folder / name
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    n = 1
    while True:
        cand = folder / f"{stem} ({n}){suffix}"
        if not cand.exists():
            return cand
        n += 1

def tool_download_file(a):
    url = str(a.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return {"ok": False, "error": "Only http(s) URLs can be downloaded."}
    folder = Path(os.path.expandvars(os.path.expanduser(
        a.get("folder") or str(Path.home() / "Downloads"))))
    if protected_match(folder): return _protected_refusal(folder)
    tmp = None
    try:
        folder.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        resp = _open_url(req)
        try:
            headers = getattr(resp, "headers", None)
            cd = headers.get("Content-Disposition") if headers else None
            final_url = resp.geturl() if hasattr(resp, "geturl") else url
            dest = _unique_dest(folder, derive_filename(final_url, cd))
            if protected_match(dest): return _protected_refusal(dest)
            tmp = dest.with_name(dest.name + ".part")
            total = 0
            with open(tmp, "wb") as f:
                while True:                      # streamed in chunks —
                    chunk = resp.read(262144)    # never all in memory
                    if not chunk:
                        break
                    f.write(chunk)
                    total += len(chunk)
        finally:
            try: resp.close()
            except Exception:  # noqa: BLE001
                pass
        tmp.replace(dest)
        return _ok(path=str(dest), bytes=total,
                   size_mb=round(total / 1e6, 2), url=url)
    except Exception as e:  # noqa: BLE001 — honest failure, no half file
        try:
            if tmp is not None and tmp.exists(): tmp.unlink()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "error": f"Download failed: {type(e).__name__}: {e}"}

def pick_install_command(path, installer_type="auto"):
    """Silent-install command line for an installer file. Returns
    (argv, kind) or (None, reason). Pure function.

    .msi is deterministic (msiexec /qn). For .exe there is no way to be
    sure of the packaging technology from outside, so the choice is:
    explicit type > name hints (inno/nsis) > NSIS /S, the most common
    silent switch for Windows utilities. A wrong guess is not fatal —
    most installers ignore unknown switches and open their GUI instead,
    which the caller reports honestly."""
    p = str(path)
    low = p.lower()
    t = str(installer_type or "auto").lower()
    if low.endswith(".msi") or t == "msi":
        return (["msiexec", "/i", p, "/qn", "/norestart"], "msi")
    if not low.endswith(".exe"):
        return (None, f"Not an .msi or .exe installer: {p}")
    if t == "inno" or (t == "auto" and ("inno" in low or "innosetup" in low)):
        return ([p, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-"], "inno")
    if t == "quiet":
        return ([p, "/quiet", "/norestart"], "generic-quiet")
    # "nsis" explicit, or the auto default for unknown .exe files
    return ([p, "/S"], "nsis")

_INSTALL_EXIT_NOTES = {
    0: "Installed successfully.",
    3010: "Installed successfully — a restart is required to finish.",
    1641: "Installed successfully — the installer started a restart.",
    1603: "Installer failed (1603) — commonly a permissions/admin problem.",
    1619: "Installer failed (1619) — the package could not be opened.",
    1620: "Installer failed (1620) — the package could not be opened.",
    1223: "Cancelled (1223) — the admin (UAC) prompt was declined. "
          "UAC prompts appear on Windows' secure desktop and cannot be "
          "clicked by the agent; approve it by hand and ask again.",
}

def _exec_install(argv):
    """Indirection over subprocess.run so tests never install anything."""
    return subprocess.run(argv, capture_output=True, text=True, timeout=900,
                          encoding="utf-8", errors="replace")

def tool_install_software(a):
    p = Path(os.path.expandvars(os.path.expanduser(a.get("path") or "")))
    if protected_match(p): return _protected_refusal(p)
    if not p.is_file():
        return {"ok": False, "error": f"Installer not found: {p}"}
    argv, kind = pick_install_command(str(p), a.get("installer_type", "auto"))
    if argv is None:
        return {"ok": False, "error": kind}
    plan = (f"Run {p.name} silently ({kind}: {' '.join(argv[1:]) or 'no extra flags'}). "
            "This can change system files, settings and installed programs, "
            "and may need admin rights.")
    confirmed = str(a.get("confirmed", "")).strip().lower() in ("true", "yes", "1")
    if not confirmed:
        # The owner's INSTALLS rule: state what will change and WAIT for
        # a spoken "confirm" — the first call never installs anything.
        return {"ok": False, "needs_confirm": True, "plan": plan,
                "error": "Not installed yet. " + plan +
                         " Say 'confirm' and I will run it."}
    try:
        r = _exec_install(argv)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Installer timed out after 15 minutes "
                                      "(it may be waiting on a window I cannot see)."}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Could not start the installer: {e}"}
    note = _INSTALL_EXIT_NOTES.get(
        r.returncode,
        "Installer finished." if r.returncode == 0 else
        f"Installer exited with code {r.returncode} — that usually means "
        "it did not complete; it may have opened a window needing the user.")
    return _ok(path=str(p), exit_code=r.returncode, installer=kind, note=note,
               output=((r.stdout or "") + (r.stderr or ""))[-1500:])

def parse_search_html(html_text):
    """Parse DuckDuckGo html results into [{title, url, snippet}].
    Pure function — testable without network. DuckDuckGo result links
    look like <a class="result__a" href="https://example.com">Title</a>
    followed by <a class="result__snippet">Snippet</a>."""
    from html import unescape
    def clean(s):
        s = re.sub(r"<[^>]+>", "", s or "")
        return unescape(s).strip()
    results = []
    # Split on result links; each chunk may contain its snippet.
    parts = re.split(r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                     html_text or "", flags=re.S)
    # parts: [pre, url1, title1, between1, url2, title2, between2, ...]
    for i in range(1, len(parts) - 1, 3):
        url, title = parts[i], clean(parts[i + 1])
        rest = parts[i + 2] if i + 2 < len(parts) else ""
        m = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', rest, flags=re.S)
        snippet = clean(m.group(1)) if m else ""
        # DuckDuckGo wraps some links as /l/?uddg=<encoded real url>
        if url.startswith("//"):
            url = "https:" + url
        elif "/l/?uddg=" in url:
            try:
                url = urllib.parse.unquote(url.split("uddg=", 1)[1].split("&", 1)[0])
            except Exception:  # noqa: BLE001
                pass
        if title and url.startswith("http"):
            results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= 8:
            break
    return results

def tool_search_web(a):
    """Built-in web search (DuckDuckGo html, no API key, no download).
    Use this when the user asks something you don't know, says 'search
    for it', or says yes to a search offer — never answer 'I don't
    have that in my local data' without searching first."""
    q = str(a.get("query") or "").strip()
    if not q:
        return {"ok": False, "error": "No search query given."}
    try:
        url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(q)
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        resp = _open_url(req)
        try:
            html_text = resp.read().decode("utf-8", errors="replace")
        finally:
            try: resp.close()
            except Exception:  # noqa: BLE001
                pass
        results = parse_search_html(html_text)[:5]
        if not results:
            return {"ok": False, "error": "Search returned no results. Try different words, or open a browser search."}
        return _ok(query=q, results=results)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Web search failed: {type(e).__name__}: {e}"}

_FILE_EXTS = (".exe", ".msi", ".zip", ".iso", ".pdf", ".mp4", ".mp3",
              ".apk", ".dmg", ".tar.gz", ".tgz", ".doc", ".docx", ".xls")

def pick_direct_file_url(results):
    """First search result whose URL looks like a direct file download.
    Pure function. Returns None when no result is a file — then the
    honest fallback is to open the search page in the browser, not to
    guess a download link or click through logins/CAPTCHAs."""
    for r in results or []:
        url = str(r.get("url") or "").lower().split("?", 1)[0]
        if url.endswith(_FILE_EXTS):
            return r.get("url")
    return None

def tool_search_download(a):
    """Full download workflow the user asked for, in one tool:
    search the web for the file, download the first direct file link,
    open the downloaded file, and report it so JARVIS can notify the
    user. If the user gave a direct URL, download that instead of
    searching. If no direct file link is found, open the search page
    in the browser and say so plainly — JARVIS does NOT automate
    Chrome clicks, logins, or CAPTCHAs to force a download."""
    q = str(a.get("query") or a.get("file") or "").strip()
    if not q:
        return {"ok": False, "error": "No file name or link given. Tell me what to download."}
    if q.startswith(("http://", "https://")):
        dl = tool_download_file({"url": q, "folder": a.get("folder")})
        if not dl.get("ok"):
            return dl
        opened = tool_open_path({"path": dl["path"]})
        return _ok(query=q, found_url=q, downloaded_path=dl["path"],
                   size_mb=dl.get("size_mb"), opened=bool(opened.get("ok")),
                   notification=f"Downloaded and opened: {dl['path']}")
    search = tool_search_web({"query": q + " download"})
    if not search.get("ok"):
        return search
    direct = pick_direct_file_url(search.get("results"))
    if not direct:
        # No direct file: open the search page so the user can click
        # the right download button themselves (login/CAPTCHA may apply).
        tool_open_path({"path": "https://duckduckgo.com/?q=" + urllib.parse.quote(q + " download")})
        return _ok(query=q, direct_file_found=False, browser_opened=True,
                   results=search.get("results"),
                   notification="No direct download link found — I opened the search page in your browser, Sir. Click the official download button there, and tell me if you want me to open the file after it downloads.")
    dl = tool_download_file({"url": direct, "folder": a.get("folder")})
    if not dl.get("ok"):
        return dl
    opened = tool_open_path({"path": dl["path"]})
    return _ok(query=q, found_url=direct, downloaded_path=dl["path"],
               size_mb=dl.get("size_mb"), opened=bool(opened.get("ok")),
               notification=f"Downloaded and opened: {dl['path']}")

def tool_youtube_upload(a):
    """Upload a video to the user's YouTube channel — OFFICIAL API ONLY.
    Never a password, never automating a Chrome login to the user's
    account (Google blocks that, and it would expose the account).
    The user must have completed the one-time Google OAuth setup on
    their own PC (a token file, not a password in chat), and must say
    'confirm' before anything is posted — posting is a public action.
    JARVIS does NOT create a video from nothing: video_path must be
    an existing video file the user made or gave it, and thumbnail_path
    must be an existing image file if a thumbnail is wanted. Privacy
    defaults to private unless the user explicitly says unlisted/public."""
    video = str(a.get("video_path") or "").strip()
    title = str(a.get("title") or "").strip()
    if not video or not title:
        return {"ok": False, "error": "I need both a video file and a title to upload. Give me the video file path and the title, description and thumbnail if you want them."}
    p = Path(os.path.expandvars(os.path.expanduser(video)))
    if protected_match(p): return _protected_refusal(p)
    if not p.is_file():
        return {"ok": False, "error": f"Video file not found: {p}. I cannot invent a video — give me the file you want uploaded, or tell me what video to make from what material."}
    thumb = str(a.get("thumbnail_path") or "").strip()
    if thumb:
        tp = Path(os.path.expandvars(os.path.expanduser(thumb)))
        if protected_match(tp): return _protected_refusal(tp)
        if not tp.is_file():
            return {"ok": False, "error": f"Thumbnail file not found: {tp}"}
    privacy = str(a.get("privacy") or "private").strip().lower()
    if privacy not in ("private", "unlisted", "public"):
        privacy = "private"
    plan = (f"Upload '{p.name}' to YouTube as '{title}' ({privacy}), "
            f"description: {(a.get('description') or '')[:200]}, "
            f"thumbnail: {thumb or 'none'}. This will post to your channel.")
    confirmed = str(a.get("confirmed", "")).strip().lower() in ("true", "yes", "1")
    if not confirmed:
        return {"ok": False, "needs_confirm": True, "plan": plan,
                "error": "Not uploaded yet. " + plan + " Say 'confirm' and I will upload it."}
    # Confirmed: now try the official API, or explain setup honestly.
    try:
        from googleapiclient.discovery import build  # type: ignore
        from google.oauth2.credentials import Credentials  # type: ignore
    except ImportError:
        return {"ok": False, "error": "YouTube upload needs the Google API client, which is not installed. On your PC run: pip install google-api-python-client google-auth-oauthlib — then complete the one-time Google OAuth setup (Google Cloud Console → YouTube Data API v3 → OAuth client) and save the token file, and try again. I will never ask for your Google password."}
    token_path = Path(os.path.expandvars(os.path.expanduser(
        a.get("token_path") or str(Path.home() / "NovaAgent" / "youtube_token.json"))))
    # Also accept the JARVIS/NOVA config-dir location on Windows.
    alt = None
    if os.environ.get("APPDATA"):
        alt = Path(os.environ["APPDATA"]) / "NovaAgent" / "youtube_token.json"
    if not token_path.is_file() and alt is not None and alt.is_file():
        token_path = alt
    if not token_path.is_file():
        return {"ok": False, "error": f"YouTube is not connected yet — no OAuth token file found at {token_path}. Complete the one-time Google OAuth setup on your PC first (sign in with Google in your browser, approve YouTube upload access, save the token file), then say confirm again. I will never ask for your Google password."}
    try:
        creds = Credentials.from_authorized_user_file(str(token_path), ["https://www.googleapis.com/auth/youtube.upload"])
        yt = build("youtube", "v3", credentials=creds)
        from googleapiclient.http import MediaFileUpload  # type: ignore
        body = {"snippet": {"title": title, "description": str(a.get("description") or ""), "categoryId": "22"},
                "status": {"privacyStatus": privacy}}
        media = MediaFileUpload(str(p), resumable=True)
        req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
        resp = req.execute()
        vid = resp.get("id", "")
        if thumb:
            yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(str(tp))).execute()
        return _ok(video_id=vid, url=f"https://www.youtube.com/watch?v={vid}",
                   title=title, privacy=privacy,
                   notification=f"Uploaded to YouTube: {title} — https://www.youtube.com/watch?v={vid}")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"YouTube upload failed: {type(e).__name__}: {e}. If the token expired, redo the OAuth setup on your PC and try again."}

def jarvis_workspace_path():
    """JARVIS's own computer workspace: a dedicated folder on this PC
    where JARVIS keeps its own work (notes, files it creates, things
    it downloads for itself), separate from the user's own folders.
    JARVIS can still access the user's computer through the other
    tools when the user asks — this workspace is its own home base,
    not a wall around it. Pure except for creating the folders."""
    base = Path.home() / "JARVIS-Computer"
    for sub in ("Work", "Downloads", "Notes"):
        try:
            (base / sub).mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            pass
    return base

def tool_jarvis_computer(a):
    """JARVIS's own computer: its dedicated workspace, what is in it,
    and how much disk is free. Use this when the user asks what
    JARVIS's own computer is, or where JARVIS keeps its own files."""
    base = jarvis_workspace_path()
    try:
        items = sorted(p.name for p in base.iterdir()) if base.exists() else []
    except Exception:  # noqa: BLE001
        items = []
    try:
        usage = shutil.disk_usage(str(base))
        free_gb = round(usage.free / 1e9, 1)
    except Exception:  # noqa: BLE001
        free_gb = None
    return _ok(path=str(base), folders=["Work", "Downloads", "Notes"],
               contents=items, free_disk_gb=free_gb,
               note="This is JARVIS's own workspace on this PC. JARVIS can also access the user's computer through its other tools when asked, subject to the protected list.")

def tool_screenshot(a):
    try:
        from PIL import ImageGrab
    except ImportError:
        return {"ok": False, "error": "Pillow is not installed, screenshots unavailable."}
    out = Path(os.path.expandvars(os.path.expanduser(a.get("path") or str(Path.home() / "Pictures" / "nova-screenshot.png"))))
    out.parent.mkdir(parents=True, exist_ok=True)
    ImageGrab.grab().save(str(out))
    return _ok(path=str(out))

TOOLS = {
    "system_info":  (tool_system_info,  "Get information about this computer: OS, user, home folder, disk space.", {}),
    "list_files":   (tool_list_files,   "List files and folders in a directory.", {"path": "Directory path (optional, defaults to home)"}),
    "read_file":    (tool_read_file,    "Read a text file's contents.", {"path": "File path"}),
    "write_file":   (tool_write_file,   "Create or overwrite a text file with the given content.", {"path": "File path", "content": "Full file content"}),
    "delete_path":  (tool_delete_path,  "Delete a file or folder permanently.", {"path": "Path to delete"}),
    "run_command":  (tool_run_command,  "Run a shell command on this computer and return its output.", {"command": "The command to run"}),
    "open_path":    (tool_open_path,    "Open a file, folder, application path or web URL for the user.", {"path": "Path or URL to open"}),
    "search_files": (tool_search_files, "Search for files by part of their name under a folder.", {"name_contains": "Text to match in file names", "root": "Folder to search under (optional)"}),
    "screenshot":   (tool_screenshot,   "Take a screenshot and save it as an image file.", {"path": "Where to save it (optional)"}),
    "search_web":   (tool_search_web,   "Search the web (DuckDuckGo, no API key needed) and return the top results with titles, links and snippets. Use this whenever the user asks something you do not know, asks you to search, or says yes to a search offer — search first, then answer from the results, instead of saying you do not have it in local data.", {"query": "What to search for"}),
    "search_download": (tool_search_download, "Full download workflow: search for a file the user named, download the first direct file link, open the downloaded file, and report it so the user can be notified. If no direct file link exists, open the search page in the browser and say so — never automate Chrome clicks, logins, or CAPTCHAs.", {"query": "File name to search for, or a direct http(s) URL", "folder": "Destination folder (optional)"}),
    "youtube_upload": (tool_youtube_upload, "Upload an existing video file to the user's YouTube channel via the OFFICIAL YouTube API only (OAuth token file, never a password, never Chrome-login automation). TWO-STEP: first call WITHOUT confirmed to show the plan, only call again with confirmed=true after the user says 'confirm'. Privacy defaults to private. JARVIS cannot invent a video — video_path must exist.", {"video_path": "Existing video file to upload", "title": "Video title", "description": "Video description (optional)", "thumbnail_path": "Existing thumbnail image (optional)", "privacy": "private, unlisted or public (optional, default private)", "confirmed": "Set to true ONLY after the user said confirm (optional)"}),
    "jarvis_computer": (tool_jarvis_computer, "JARVIS's own computer workspace: its dedicated folders on this PC for its own work, plus how much disk is free. JARVIS can also access the user's computer through the other tools when asked.", {}),
    "download_file": (tool_download_file, "Download a file directly from a URL to the Downloads folder (or a named folder) — no browser, no clicking. Use for 'download <link>' requests; prefer official sources.", {"url": "The http(s) URL to download", "folder": "Destination folder (optional, defaults to ~/Downloads)"}),
    "install_software": (tool_install_software, "Install an already-downloaded .msi or .exe installer silently/unattended. TWO-STEP per the owner's rule: first call WITHOUT confirmed to get the plan, tell the user what will change, and only call again with confirmed=true after the user says 'confirm'.", {"path": "Path to the downloaded installer file", "confirmed": "Set to true ONLY after the user said confirm (optional)", "installer_type": "auto, nsis, inno, quiet or msi (optional, default auto)"}),
}

SYSTEM_PROMPT = (
    "You are JARVIS, the user's personal AI agent living on their own computer. "
    "You can use tools to inspect and operate this computer: browse and read files, "
    "create files, run commands, open apps and folders, search, and take screenshots. "
    "Prefer acting over describing: when the user asks for something you can do with a tool, do it. "
    "The user has switched approval prompts OFF: execute tasks immediately, even risky ones — "
    "never ask for confirmation and never warn instead of acting. "
    "There is exactly ONE hard restriction: files and folders the user has marked as protected "
    "(in Settings) are completely off-limits. Never read, list, create, modify, delete, open or "
    "run commands touching a protected path, and never try to work around the block — the tools "
    "refuse automatically; if that happens, tell the user plainly that the path is protected by "
    "their own rule and move on. "
    "You have your own computer workspace at ~/JARVIS-Computer (Work, Downloads, Notes): "
    "that is where you keep your own files, notes and things you download for yourself, "
    "like an assistant's own desk. You can also access the user's computer through your "
    "other tools when the user asks — the workspace is your home base, not a limit on "
    "what you can reach. "
    "You have a built-in web search tool (search_web, no API key, no download needed): "
    "when the user asks something you do not know, asks you to search, or says yes to "
    "a search offer, call search_web first and answer from its results. Never stop at "
    "'I don't have that in my local data' when a search could answer it. "
    "When the user says 'download <something>', use search_download: it searches, "
    "downloads a direct file link, opens the file, and reports it for notification. "
    "If no direct link exists, open the search page and say so — never automate "
    "Chrome clicks, logins, or CAPTCHAs. When the user says 'upload a video to "
    "YouTube', use youtube_upload: show the plan first, upload only after the user "
    "says 'confirm', through the official API only, never a password and never a "
    "Chrome login as the user, privacy private unless the user explicitly says "
    "otherwise, and only an existing video file — never invent a video. "
    "You can also fetch software directly: download_file pulls a URL straight to the Downloads "
    "folder with no browser involved — prefer official sources for downloads. For INSTALLS the "
    "owner has one extra rule that overrides the no-confirmation default: call install_software "
    "first WITHOUT confirmed, tell the user its plan (what will change), and only call it with "
    "confirmed=true after the user says 'confirm'. "
    "Keep replies concise and clear. When a tool fails, say what happened and suggest the next step. "
    f"This computer runs {platform.system()} and the user's home folder is {Path.home()}. "
    "Use paths that fit this operating system."
)

# ------------------------------------------------------- screen advisor ---
# NOVA can glance at the screen every so often and suggest the single most
# useful next step. The advisor is deliberately separate from chat(): it
# never touches conversation history and never triggers the fallback brain.

ADVISOR_PROMPT = (
    "Look at this screen. In ONE short sentence, suggest the single most "
    "useful next action for the user, or reply exactly 'NOTHING' if there "
    "is nothing worth suggesting. No preamble."
)

def capture_screen_b64(max_width=1280, quality=70):
    """Screenshot -> base64 JPEG string, downscaled to max_width.
    Raises RuntimeError if Pillow is unavailable."""
    try:
        from PIL import ImageGrab
    except ImportError:
        raise RuntimeError("Pillow is not installed, screenshots unavailable.")
    img = ImageGrab.grab().convert("RGB")
    if img.width > max_width:
        img = img.resize((max_width, round(img.height * max_width / img.width)))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode()

_BOILERPLATE = (
    "nothing to suggest", "nothing worth suggesting", "no suggestion",
    "no action needed", "no action necessary", "nothing to recommend",
    "nothing at this time", "nothing to do",
)

def _suggestion_key(text):
    """Normalized comparison key: lowercase alphanumerics + single spaces."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", str(text).lower())).strip()

def normalize_suggestion(raw):
    """Clean a raw vision-model reply into a one-line suggestion, or None
    when the model effectively said there is nothing worth suggesting.
    Pure function — no I/O — so it is unit-testable anywhere."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    # the model was told one sentence, no preamble — keep the first real line
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    text = lines[0] if lines else ""
    text = re.sub(r"^(suggestion|answer|response)\s*:\s*", "", text, flags=re.I)
    text = text.strip().strip("💡\"'").strip()
    text = re.sub(r"\s+", " ", text)
    if not text:
        return None
    key = _suggestion_key(text)
    if key == "nothing" or (key.startswith("nothing ") and len(key) < 40):
        return None
    if key in _BOILERPLATE or any(key.startswith(b) for b in _BOILERPLATE):
        return None
    return text[:240]

def should_deliver(new, last):
    """True when `new` is a real suggestion that isn't just a repeat (or
    near-repeat) of the last one delivered. Pure function."""
    if not new:
        return False
    if not last:
        return True
    a, b = _suggestion_key(new), _suggestion_key(last)
    if not a:
        return False
    if a == b:
        return False
    if a in b or b in a:
        return False
    sa, sb = set(a.split()), set(b.split())
    if sa and sb and len(sa & sb) / len(sa | sb) >= 0.8:
        return False
    return True

def _openai_vision_messages(prompt, image_b64):
    """OpenAI-style chat messages carrying a base64 JPEG (works for OpenAI,
    Gemini's OpenAI-compatible endpoint, and vision-capable Ollama)."""
    return [{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url",
         "image_url": {"url": "data:image/jpeg;base64," + image_b64}},
    ]}]

def _anthropic_vision_content(prompt, image_b64):
    """Anthropic-style content blocks carrying a base64 JPEG."""
    return [
        {"type": "image",
         "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
        {"type": "text", "text": prompt},
    ]

# ------------------------------------------------------- model providers ---

# Google retired gemini-2.5-flash for new users (404 NOT_FOUND — Google's own
# error says to use gemini-3.8-flash). Map the dead name to its replacement
# wherever a model is applied, so even a stale saved config keeps working.
_RETIRED_MODELS = {"gemini-2.5-flash": "gemini-3.8-flash"}

def _migrate_model(model):
    m = str(model or "").strip()
    bare = m[len("models/"):] if m.startswith("models/") else m
    return _RETIRED_MODELS.get(bare, m)



# --------------------------------------------- transient-error handling ---
# Providers (Gemini especially) intermittently refuse requests with
# 429 / 500 / 503 — "high demand" spikes that usually clear within
# seconds. Those are TRANSIENT: retry a few times with backoff before
# giving up. Everything else (400/401/403/404 — bad request, bad key,
# dead model name) is a hard failure and must surface immediately.

class TransientModelError(RuntimeError):
    """A model-API failure that is usually temporary (429/500/503).
    Subclasses RuntimeError so existing handlers keep working; the
    distinct type lets callers tell 'provider busy, try later' apart
    from 'this will never work'."""

_TRANSIENT_CODES = {429, 500, 503}
_TRANSIENT_STATUSES = ("RESOURCE_EXHAUSTED", "UNAVAILABLE", "INTERNAL")
_RETRY_WAITS = (2, 5, 10)    # seconds waited before attempts 2, 3, 4

_sleep = time.sleep          # injectable so tests never really sleep

# The Screen Advisor's vision call uploads a full screenshot and waits on
# the slowest answer NOVA ever asks for, so screen_suggestion() gives it
# its own, longer HTTP budget. Chat keeps the shared _http_json default
# (180s) exactly as before — this constant is used ONLY on the vision
# path.
VISION_HTTP_TIMEOUT = 240

# ------------------------------------------------ daily-quota errors ----
# A 429 is NOT always "provider busy". On free tiers (Gemini especially)
# a 429 can mean the DAILY request quota is finished — Google's body
# says so plainly: "Quota exceeded for metric:
# .../generate_content_free_tier_requests, limit: 20 ... Generate
# content requests per day limit exceeded on Free Tier ... Please
# retry in 15h22m". That is a QUOTA error, not overload: retrying in
# seconds is pointless, the friendly message is different ("your daily
# quota is finished", not "high demand"), and chat() treats it like an
# unreachable brain — one attempt on the fallback brain.
#
# A quota verdict needs DAILY evidence in the text. A bare 429 or a
# bare RESOURCE_EXHAUSTED status is just per-minute rate limiting and
# stays transient (it gets one short retry — see _http_json). The
# per-minute metric name (..._requests_per_minute) even CONTAINS the
# per-day metric name as a substring, so per-minute wording is ruled
# out first.

_QUOTA_MARKERS = ("quota exceeded", "generate_content_free_tier_requests",
                  "per day", "requests per day")

def _http_code(err):
    """The HTTP status code embedded in a 'Model API error NNN' text,
    or None. Pure function."""
    m = re.search(r"Model API error (\d{3})\b", str(err))
    return int(m.group(1)) if m else None

def is_quota_error(err):
    """True when `err` is a DAILY-quota exhaustion (the free tier's
    per-day request cap is used up), as opposed to a momentary
    overload or per-minute rate-limit spike. Pure function. Signals:
    'quota exceeded' wording, the free-tier metric name, a 'per day'
    limit description, or a retry delay measured in HOURS ('Please
    retry in 15h22m…')."""
    text = str(err)
    low = text.lower()
    if ("per_minute" in low or "per minute" in low) and "per day" not in low:
        return False   # per-minute rate limit wearing quota wording
    if any(marker in low for marker in _QUOTA_MARKERS):
        return True
    if re.search(r"retry in\s*\d+\s*h", low):          # 'retry in 15h22m…'
        return True
    if re.search(r"retrydelay\D{0,4}\d{4,}s", low):   # 'retryDelay': '54926s'
        return True
    return False

def is_transient_error(err):
    """True when `err` (an exception or error text) is a transient
    provider failure: HTTP 429/500/503, or a body carrying a
    RESOURCE_EXHAUSTED / UNAVAILABLE / INTERNAL status. A DAILY-quota
    exhaustion is NOT transient even though it arrives as a 429 with
    RESOURCE_EXHAUSTED — see is_quota_error. Pure function."""
    if is_quota_error(err):
        return False
    if isinstance(err, TransientModelError):
        return True
    text = str(err)
    m = re.search(r"Model API error (\d{3})\b", text)
    if m and int(m.group(1)) in _TRANSIENT_CODES:
        return True
    return any(re.search(r"\b" + s + r"\b", text) for s in _TRANSIENT_STATUSES)

# Shown (instead of a raw JSON dump) when every retry hit provider overload.
OVERLOAD_MESSAGE = (
    "⚠ The model is overloaded right now (503 — high demand on the "
    "provider's side; these spikes are usually temporary). Give it a "
    "minute and ask me again, or switch the model in Settings "
    "(gemini-3.5-flash is usually quieter)."
)

# Shown when the brain's free DAILY quota is exhausted and there is no
# fallback brain to switch to. Deliberately different from
# OVERLOAD_MESSAGE: waiting a minute will not fix this one — the quota
# resets on the provider's own (hours-long) timer.
QUOTA_MESSAGE = (
    "⚠ This brain's free daily quota is finished (Google's free tier "
    "allows only a small number of requests per day). It resets on "
    "Google's timer, or switch the model/brain in Settings."
)

def is_timeout_error(err):
    """True when `err` is a socket/read timeout rather than a real
    answer from the provider: a TimeoutError / socket.timeout instance
    (an HTTPS read that stalls raises exactly this — 'The read
    operation timed out'), or any error text saying it 'timed out'.
    Pure function."""
    if isinstance(err, (TimeoutError, socket.timeout)):
        return True
    return "timed out" in str(err).lower()

_NETWORK_TEXT_HINTS = ("could not reach the model api", "eof occurred",
                       "connection reset", "connection refused",
                       "connection aborted", "name resolution",
                       "network is unreachable", "remote end closed",
                       "ssl", "urlerror", "broken pipe")

def is_network_error(err):
    """True when `err` is a network-level failure — the request never
    got a real answer from the provider (DNS failure, refused/reset
    connection, an SSL EOF like 'EOF occurred in violation of protocol
    (_ssl.c:2427)', ...). Pure function. NOTE: chat() keeps its own,
    narrower trigger ('Could not reach the model API') for engaging
    the fallback brain; this wider net exists for the Screen Advisor,
    where any network hiccup should mean 'skip this cycle', never
    'vision is broken, switch off'."""
    if isinstance(err, urllib.error.URLError):
        return True
    low = str(err).lower()
    return any(hint in low for hint in _NETWORK_TEXT_HINTS)

def advisor_failure_action(err):
    """What the Screen Advisor should do after a failed vision call:
    "skip" — transient trouble (provider overloaded/rate-limited,
    the call TIMED OUT — a slow vision answer is not 'vision
    unavailable' — a NETWORK-level failure such as an SSL EOF, or
    the brain's daily quota being finished): stay ON and simply try
    again next cycle. "disable" — hard failure (dead model 404, bad
    key 401/403, vision unsupported 400, ...): switch OFF rather
    than failing on every interval. Pure function. NOTE: this
    leniency belongs to the advisor only — chat()'s network-error
    handling (immediate fallback, no retry) is deliberately
    untouched by it."""
    if (is_quota_error(err) or is_transient_error(err)
            or is_timeout_error(err) or is_network_error(err)):
        return "skip"
    return "disable"

MODEL_API_USER_AGENT = "JARVIS/1.0"

def _http_json_once(url, headers, payload, timeout=180):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST")
    for k, v in headers.items(): req.add_header(k, v)
    # Cloudflare (in front of Groq and others) blocks requests whose
    # User-Agent is Python's urllib default ("Python-urllib/3.x") with
    # a 403 error code 1010, before the request ever reaches the API
    # — so a correct key still fails. Send a descriptive User-Agent
    # instead, unless the caller already set one.
    if not any(k.lower() == "user-agent" for k in headers):
        req.add_header("User-Agent", MODEL_API_USER_AGENT)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:600]
        err = RuntimeError(f"Model API error {e.code}: {body}")
        if e.code in _TRANSIENT_CODES or is_transient_error(err):
            raise TransientModelError(str(err)) from e
        raise err from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Could not reach the model API: {e.reason}") from e

def _http_json(url, headers, payload, timeout=180):
    """POST JSON to a model API, retrying TRANSIENT failures (500/503
    overload spikes) up to 3 extra times with ~2s/5s/10s backoff. A
    per-minute 429 rate limit gets ONE short retry only — hammering a
    rate limit just prolongs it. A DAILY-quota 429 is never retried:
    seconds of waiting cannot refill a quota that resets in hours, so
    it is raised at once and chat() switches brains / explains. Hard
    HTTP errors fail immediately, and network failures keep their
    original 'Could not reach the model API' error — they are not
    retried, so chat()'s auto-fallback engages exactly as before."""
    retries = 0
    while True:
        try:
            return _http_json_once(url, headers, payload, timeout)
        except TransientModelError as e:
            if is_quota_error(e):
                raise
            allowed = 1 if _http_code(e) == 429 else len(_RETRY_WAITS)
            if retries >= allowed:
                raise
            _sleep(_RETRY_WAITS[min(retries, len(_RETRY_WAITS) - 1)])
            retries += 1

def _anthropic_tools():
    out = []
    for name, (_fn, desc, params) in TOOLS.items():
        props = {k: {"type": "string", "description": v} for k, v in params.items()}
        req = [k for k in params if "optional" not in params[k]]
        out.append({"name": name, "description": desc,
                    "input_schema": {"type": "object", "properties": props, "required": req}})
    return out

def _openai_tools():
    out = []
    for name, (_fn, desc, params) in TOOLS.items():
        props = {k: {"type": "string", "description": v} for k, v in params.items()}
        req = [k for k in params if "optional" not in params[k]]
        out.append({"type": "function", "function": {"name": name, "description": desc,
                    "parameters": {"type": "object", "properties": props, "required": req}}})
    return out

class Agent:
    """One conversation. Tools execute immediately — there is no approval
    step. `on_tool(name, detail)` is called for display as tools run. The
    config's `protected_paths` list is the single enforced restriction."""
    def __init__(self, config, on_tool=None):
        self.protected_paths = [str(x) for x in (config.get("protected_paths") or [])]
        set_protected_paths(self.protected_paths)
        self._primary = {
            "provider": config.get("provider", "anthropic"),
            "api_key": config.get("api_key", ""),
            "model": config.get("model", ""),
            "base_url": config.get("base_url", ""),
        }
        fb = config.get("fallback") or {}
        self.fallback = {
            "enabled": fb.get("enabled", True),
            "provider": fb.get("provider") or "ollama",
            "api_key": fb.get("api_key", ""),
            "model": fb.get("model", ""),
            "base_url": fb.get("base_url", ""),
        }
        self._apply_provider(self._primary)
        self.on_tool = on_tool or (lambda _n, _d: None)
        self.history = []  # provider-neutral: {"role": "user"/"assistant", "text": ...}

    def _system_prompt(self):
        if not self.protected_paths:
            return SYSTEM_PROMPT
        listing = "; ".join(self.protected_paths)
        return (SYSTEM_PROMPT + " Protected paths set by the user (never touch these): "
                + listing + ".")

    def _default_model(self):
        return ("claude-sonnet-5" if self.provider == "anthropic" else
                "gpt-4o" if self.provider == "openai" else "llama3.1")

    def _default_base(self):
        return ("https://api.openai.com/v1" if self.provider == "openai"
                else "http://localhost:11434/v1")

    def _apply_provider(self, spec):
        self.provider = spec.get("provider", "anthropic")
        self.api_key = spec.get("api_key", "")
        self.model = _migrate_model(spec.get("model", "")) or self._default_model()
        self.base_url = spec.get("base_url", "") or self._default_base()

    def _run_tool(self, name, args):
        fn, _desc, _p = TOOLS.get(name, (None, None, None))
        if fn is None: return {"ok": False, "error": f"Unknown tool {name}"}
        detail = json.dumps(args)[:300]
        self.on_tool(name, detail)
        try: return fn(args)
        except Exception as e: return {"ok": False, "error": f"{type(e).__name__}: {e}"}  # noqa: BLE001

    def chat(self, user_text):
        self.history.append({"role": "user", "text": user_text})
        try:
            reply = self._run_provider_loop()
        except RuntimeError as e:  # noqa: BLE001
            quota = is_quota_error(e)
            unreachable = "Could not reach the model API" in str(e)
            if (self.fallback.get("enabled")
                    and self.fallback.get("provider") != self.provider
                    and (unreachable or quota)):
                # Main brain unusable for this message — either it can't
                # be reached at all (e.g. no internet) or its free DAILY
                # quota is finished: retry this message once on the
                # fallback brain, then restore the main one so recovery
                # is automatic when the network/quota comes back.
                if quota:
                    note = ("↪ Main brain's daily quota is finished — switched to "
                            f"your fallback brain ({self.fallback['provider']}) "
                            "for this reply.")
                else:
                    note = (f"⚠ {self.provider} unreachable — switched to the fallback brain "
                            f"({self.fallback['provider']}) for this reply. "
                            "I'll try the main brain again on your next message.")
                self._apply_provider(self.fallback)
                try:
                    reply = note + "\n\n" + self._run_provider_loop()
                except RuntimeError as e2:  # noqa: BLE001
                    # The usual cause here is Ollama not actually being
                    # installed/running on this PC — the fallback is
                    # configured, but there is nothing listening on
                    # localhost:11434. Say that plainly, with both fixes,
                    # instead of a raw "Could not reach" error.
                    if self.fallback.get("provider") == "ollama":
                        reply = (note + "\n⚠ The fallback brain (Ollama) failed too — "
                                 "Ollama does not look installed or running on this PC. "
                                 "Fix 1 (offline): install Ollama, then run "
                                 "`ollama pull llama3.1` and try again. "
                                 "Fix 2 (easier, free): switch your main brain to Groq "
                                 "in Settings — provider openai, base URL "
                                 "https://api.groq.com/openai/v1, model "
                                 "llama-3.3-70b-versatile — about 1,000 free requests/day, "
                                 "no Ollama needed.")
                    else:
                        reply = (note + f"\n⚠ The fallback brain failed too: {e2}.")
                finally:
                    self._apply_provider(self._primary)
            elif quota:
                # Daily quota finished and no fallback brain to switch
                # to: say so plainly (this is NOT a demand spike —
                # waiting a minute won't help), raw error as details.
                reply = QUOTA_MESSAGE + f"\n(Details: {e})"
            elif is_transient_error(e):
                # Provider overload (429/500/503) that survived every
                # retry: explain it like a human, keep the raw error as
                # a details line. Never triggers the fallback brain —
                # that is only for unreachable-network failures.
                reply = OVERLOAD_MESSAGE + f"\n(Details: {e})"
            else:
                reply = f"⚠ {e}"
        self.history.append({"role": "assistant", "text": reply})
        return reply

    def screen_suggestion(self, image_b64, prompt=ADVISOR_PROMPT):
        """ONE vision call on the current brain: look at a screenshot and
        answer `prompt`. Returns the model's raw text. Deliberately does
        NOT touch self.history and does NOT use the chat fallback — the
        Screen Advisor must never pollute the conversation or silently
        switch brains. Raises RuntimeError on any failure (no vision,
        bad key, offline...): hard failures mean 'vision not available',
        while transient provider overload (429/500/503, after the usual
        retries) raises TransientModelError, and a stalled call raises a
        timeout error — advisor_failure_action() tells the caller which
        of the two it was (both mean 'skip this cycle', not 'disable')."""
        if self.provider == "anthropic":
            if not self.api_key:
                raise RuntimeError("No API key set. Open Settings and add your Anthropic API key.")
            data = _http_json("https://api.anthropic.com/v1/messages",
                {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
                {"model": self.model, "max_tokens": 120,
                 "messages": [{"role": "user",
                               "content": _anthropic_vision_content(prompt, image_b64)}]},
                timeout=VISION_HTTP_TIMEOUT)
            return "".join(b.get("text", "") for b in data.get("content", [])
                           if b.get("type") == "text").strip()
        headers = {}
        if self.api_key: headers["Authorization"] = f"Bearer {self.api_key}"
        data = _http_json(self.base_url.rstrip("/") + "/chat/completions", headers,
            {"model": self.model,
             "messages": _openai_vision_messages(prompt, image_b64),
             "max_tokens": 120},
            timeout=VISION_HTTP_TIMEOUT)
        return (data["choices"][0]["message"].get("content") or "").strip()

    def _run_provider_loop(self):
        return self._anthropic_loop() if self.provider == "anthropic" else self._openai_loop()

    # --- Anthropic ---
    def _anthropic_loop(self):
        if not self.api_key: raise RuntimeError("No API key set. Open Settings and add your Anthropic API key.")
        msgs = []
        for h in self.history:
            msgs.append({"role": h["role"], "content": [{"type": "text", "text": h["text"]}]})
        for _ in range(12):
            data = _http_json("https://api.anthropic.com/v1/messages",
                {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
                {"model": self.model, "max_tokens": 4096, "system": self._system_prompt(),
                 "messages": msgs, "tools": _anthropic_tools()})
            blocks = data.get("content", [])
            msgs.append({"role": "assistant", "content": blocks})
            uses = [b for b in blocks if b.get("type") == "tool_use"]
            if not uses:
                return "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip() or "(no reply)"
            results = []
            for u in uses:
                res = self._run_tool(u["name"], u.get("input") or {})
                results.append({"type": "tool_result", "tool_use_id": u["id"],
                                "content": json.dumps(res)[:24000]})
            msgs.append({"role": "user", "content": results})
        return "I did a lot of steps and stopped there — ask me to continue if needed."

    # --- OpenAI / compatible (incl. Ollama local) ---
    def _openai_loop(self):
        msgs = [{"role": "system", "content": self._system_prompt()}]
        for h in self.history:
            msgs.append({"role": h["role"], "content": h["text"]})
        headers = {}
        if self.api_key: headers["Authorization"] = f"Bearer {self.api_key}"
        for _ in range(12):
            data = _http_json(self.base_url.rstrip("/") + "/chat/completions", headers,
                {"model": self.model, "messages": msgs, "tools": _openai_tools(), "tool_choice": "auto"})
            msg = data["choices"][0]["message"]
            calls = msg.get("tool_calls") or []
            if not calls:
                return (msg.get("content") or "(no reply)").strip()
            msgs.append({"role": "assistant", "content": msg.get("content"), "tool_calls": calls})
            for c in calls:
                try: args = json.loads(c["function"].get("arguments") or "{}")
                except json.JSONDecodeError: args = {}
                res = self._run_tool(c["function"]["name"], args)
                msgs.append({"role": "tool", "tool_call_id": c["id"], "content": json.dumps(res)[:24000]})
        return "I did a lot of steps and stopped there — ask me to continue if needed."
