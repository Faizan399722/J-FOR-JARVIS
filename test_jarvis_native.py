"""Native-edition tests for JARVIS — run:  python3 tests/test_jarvis_native.py

Covers what is new in the native build:
  * wake-phrase parsing for "Jarvis" / "Hey Jarvis" (with "Nova" still
    working, and no false triggers inside other words),
  * the ScreenWatcher decision logic (deliver / dedupe / skip-on-transient
    / switch-off-on-hard-failure) with capture+suggest fully stubbed —
    no screen, no network, no API,
  * clamp_for_speech (spoken replies: cleaned, max 4 sentences),
  * download_file: filename derivation, streamed download with the
    network stubbed, collision naming, protected-destination refusal,
  * install_software: silent-flag selection (.msi / NSIS / Inno /
    unknown .exe), the spoken-"confirm" gate, exit-code reporting —
    with the installer executor stubbed, so nothing is ever installed.
"""
import subprocess, sys, tempfile, unittest, urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import backend
import jarvis


class JarvisWakePhraseTest(unittest.TestCase):
    def parse(self, text):
        return backend.parse_wake_phrase(text)

    def test_jarvis_plus_command_same_utterance(self):
        self.assertEqual(self.parse("jarvis open downloads"), "open downloads")
        self.assertEqual(self.parse("hey jarvis open downloads"), "open downloads")

    def test_jarvis_alone_means_listen_next(self):
        self.assertEqual(self.parse("jarvis"), "")
        self.assertEqual(self.parse("hey jarvis"), "")
        self.assertEqual(self.parse("Jarvis!"), "")

    def test_case_and_punctuation(self):
        self.assertEqual(self.parse("Hey Jarvis, what time is it"), "what time is it")
        self.assertEqual(self.parse("JARVIS download the file"), "download the file")

    def test_nova_still_works(self):
        self.assertEqual(self.parse("hey nova open downloads"), "open downloads")
        self.assertEqual(self.parse("nova"), "")

    def test_unrelated_phrase_ignored(self):
        self.assertIsNone(self.parse("open the downloads folder"))
        self.assertIsNone(self.parse("what a lovely day"))
        self.assertIsNone(self.parse(""))

    def test_no_false_trigger_inside_words(self):
        self.assertIsNone(self.parse("jarvison is a surname"))
        self.assertIsNone(self.parse("supernova explosion"))


class ScreenWatcherTest(unittest.TestCase):
    def make(self, suggest, interval=90):
        calls = {"delivered": [], "notes": 0, "hard": []}
        w = jarvis.ScreenWatcher(
            capture=lambda: "FAKEB64",
            suggest=suggest,
            deliver=lambda s: calls["delivered"].append(s),
            on_skip_note=lambda: calls.__setitem__("notes", calls["notes"] + 1),
            on_hard_fail=lambda e: calls["hard"].append(e),
            interval=interval)
        w.enabled = True
        return w, calls

    def test_disabled_cycle_does_nothing(self):
        w, calls = self.make(lambda b64: "Save your work.")
        w.enabled = False
        self.assertEqual(w.run_cycle(), "disabled")
        self.assertEqual(calls["delivered"], [])

    def test_first_suggestion_delivered(self):
        w, calls = self.make(lambda b64: "Save your work.")
        self.assertEqual(w.run_cycle(), "delivered")
        self.assertEqual(calls["delivered"], ["Save your work."])
        self.assertEqual(w.last_delivered, "Save your work.")

    def test_duplicate_suppressed(self):
        w, calls = self.make(lambda b64: "Save your work.")
        self.assertEqual(w.run_cycle(), "delivered")
        self.assertEqual(w.run_cycle(), "quiet")
        self.assertEqual(len(calls["delivered"]), 1)

    def test_nothing_is_quiet(self):
        w, calls = self.make(lambda b64: "NOTHING")
        self.assertEqual(w.run_cycle(), "quiet")
        self.assertEqual(calls["delivered"], [])

    def test_transient_error_skips_and_stays_on(self):
        def boom(_b64):
            raise backend.TransientModelError(
                "Model API error 503: {\"error\": {\"status\": \"UNAVAILABLE\"}}")
        w, calls = self.make(boom)
        self.assertEqual(w.run_cycle(), "skipped")
        self.assertTrue(w.enabled)                    # never auto-disables
        self.assertEqual(w.run_cycle(), "skipped")
        self.assertEqual(calls["notes"], 1)            # one note per session
        self.assertEqual(calls["hard"], [])

    def test_quota_error_skips_and_stays_on(self):
        def boom(_b64):
            raise RuntimeError(
                "Model API error 429: Quota exceeded for metric: "
                "generativelanguage.googleapis.com/generate_content_free_tier_requests, "
                "limit: 20, per day. Please retry in 15h22m.")
        w, calls = self.make(boom)
        self.assertEqual(w.run_cycle(), "skipped")
        self.assertTrue(w.enabled)

    def test_network_error_skips(self):
        def boom(_b64):
            raise urllib.error.URLError("Name resolution failed")
        w, _calls = self.make(boom)
        self.assertEqual(w.run_cycle(), "skipped")
        self.assertTrue(w.enabled)

    def test_hard_error_switches_off_once(self):
        def boom(_b64):
            raise RuntimeError("Model API error 404: model not found")
        w, calls = self.make(boom)
        self.assertEqual(w.run_cycle(), "hard-off")
        self.assertFalse(w.enabled)
        self.assertEqual(len(calls["hard"]), 1)
        self.assertEqual(w.run_cycle(), "disabled")    # stays off

    def test_interval_clamped_to_minimum(self):
        w, _calls = self.make(lambda b64: "NOTHING", interval=5)
        self.assertEqual(w.interval, 30)


class NativeChatTest(unittest.TestCase):
    """JsApi.native_chat must route a typed HUD command to the real
    brain — the bug where typed tasks never reached backend.chat and
    the demo answered 'No matching routine' / fake 'opened' instead."""

    class _FakeSpeaker:
        def __init__(self):
            self.said = []
        def say(self, text):
            self.said.append(text)

    class _FakeAgent:
        def __init__(self, reply):
            self.reply, self.seen = reply, []
        def chat(self, text):
            self.seen.append(text)
            return self.reply

    def _api(self, reply="Pictures is open, Sir."):
        app = object.__new__(jarvis.JarvisApp)
        app.speaker = self._FakeSpeaker()
        agent = self._FakeAgent(reply)
        app._get_agent = lambda: agent
        class _Hud:
            def __init__(self):
                self.logged = []
            def log(self, line):
                self.logged.append(line)
        app.hud = _Hud()
        api = jarvis.JsApi(app)
        return api, agent, app.speaker

    def test_native_chat_calls_brain_and_returns_reply(self):
        api, agent, speaker = self._api("Opened Pictures, Sir.")
        out = api.native_chat("open the pictures folder")
        self.assertEqual(out, "Opened Pictures, Sir.")
        self.assertEqual(agent.seen, ["open the pictures folder"])
        self.assertEqual(speaker.said, ["Opened Pictures, Sir."])

    def test_native_chat_failure_is_calm_not_a_crash(self):
        api, _agent, _sp = self._api()
        api._app._get_agent = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        out = api.native_chat("do something")
        self.assertIn("Something went wrong", out)
        self.assertNotIn("boom", out)

    def test_native_get_settings_hides_keys(self):
        import json as _json
        api, _agent, _sp = self._api()
        api._app.config = {"provider": "openai", "api_key": "gsk_secret",
                           "base_url": "https://api.groq.com/openai/v1",
                           "model": "llama-3.3-70b-versatile",
                           "fallback": {"enabled": True, "provider": "ollama",
                                        "api_key": "", "model": "", "base_url": ""}}
        s = api.native_get_settings()
        self.assertEqual(s["provider"], "openai")
        self.assertTrue(s["api_key_saved"])
        self.assertNotIn("gsk_secret", _json.dumps(s))

    def test_native_save_settings_keeps_blank_key(self):
        import json as _json, tempfile, os
        api, _agent, _sp = self._api()
        api._app.config = {"provider": "openai", "api_key": "gsk_old",
                           "base_url": "", "model": "old",
                           "fallback": {"enabled": True, "provider": "ollama",
                                        "api_key": "", "model": "", "base_url": ""}}
        # save_config writes to CFG_FILE; point it at a temp dir
        import jarvis as _j
        old_dir, old_file = _j.CFG_DIR, _j.CFG_FILE
        with tempfile.TemporaryDirectory() as td:
            _j.CFG_DIR, _j.CFG_FILE = _j.Path(td), _j.Path(td) / "config.json"
            try:
                msg = api.native_save_settings(_json.dumps({
                    "provider": "openai",
                    "base_url": "https://api.groq.com/openai/v1",
                    "model": "llama-3.1-8b-instant",
                    "api_key": "",
                    "fallback_provider": "openai",
                    "fallback_model": "llama-3.1-8b-instant"}))
            finally:
                _j.CFG_DIR, _j.CFG_FILE = old_dir, old_file
        self.assertIn("Settings saved", msg)
        self.assertEqual(api._app.config["model"], "llama-3.1-8b-instant")
        self.assertEqual(api._app.config["api_key"], "gsk_old")
        self.assertEqual(api._app.config["fallback"]["model"], "llama-3.1-8b-instant")

    def test_native_save_settings_applies_without_restart(self):
        import json as _json, tempfile
        api, _agent, _sp = self._api()
        api._app.config = {"provider": "ollama", "api_key": "",
                           "base_url": "http://localhost:11434/v1",
                           "model": "llama3.1",
                           "fallback": {"enabled": True, "provider": "ollama",
                                        "api_key": "", "model": "", "base_url": ""}}
        api._app._agent = "cached-old-brain"
        api._app._agent_snap = "old-snap"
        import jarvis as _j
        old_dir, old_file = _j.CFG_DIR, _j.CFG_FILE
        with tempfile.TemporaryDirectory() as td:
            _j.CFG_DIR, _j.CFG_FILE = _j.Path(td), _j.Path(td) / "config.json"
            try:
                api.native_save_settings(_json.dumps({
                    "provider": "openai",
                    "base_url": "https://api.groq.com/openai/v1",
                    "model": "llama-3.3-70b-versatile", "api_key": ""}))
            finally:
                _j.CFG_DIR, _j.CFG_FILE = old_dir, old_file
        # Cached brain must be dropped so the next chat builds a fresh
        # one from the saved config — no window close, tray quit, or
        # restart is needed to apply Settings.
        self.assertIsNone(api._app._agent)
        self.assertIsNone(api._app._agent_snap)

    def test_native_minimize_posts_hide_window(self):
        import queue as _q
        api, _agent, _sp = self._api()
        api._app.events = _q.Queue()
        self.assertTrue(api.native_minimize())
        self.assertEqual(api._app.events.get_nowait(), ("hide_window", ""))

    def test_open_path_tool_posts_hide_window(self):
        import queue as _q
        api, _agent, _sp = self._api()
        api._app.events = _q.Queue()
        api._app._on_tool("open_path", '{"path": "C:/Users/x/Pictures"}')
        self.assertEqual(api._app.events.get_nowait(), ("hide_window", ""))
        # other tools must not hide the window
        api._app._on_tool("list_files", '{"path": "C:/"}')
        self.assertTrue(api._app.events.empty())


class ClampForSpeechTest(unittest.TestCase):
    def test_clamps_to_four_sentences(self):
        out = jarvis.clamp_for_speech("One. Two. Three. Four. Five. Six.")
        self.assertEqual(out, "One. Two. Three. Four.")

    def test_short_reply_unchanged(self):
        self.assertEqual(jarvis.clamp_for_speech("Yes, Sir?"), "Yes, Sir?")
        self.assertEqual(jarvis.clamp_for_speech("All done. Anything else?"),
                         "All done. Anything else?")

    def test_markdown_and_links_stripped(self):
        out = jarvis.clamp_for_speech(
            "Run ```rm -rf x``` then see https://example.com/page for **details**.")
        self.assertNotIn("rm -rf", out)
        self.assertNotIn("http", out)
        self.assertNotIn("**", out)

    def test_empty(self):
        self.assertEqual(jarvis.clamp_for_speech(""), "")
        self.assertEqual(jarvis.clamp_for_speech(None), "")


class FakeResponse:
    """Minimal stand-in for an urllib response: chunked reads."""
    def __init__(self, chunks, headers=None, url="https://example.com/app.zip"):
        self._chunks = list(chunks)
        self.headers = headers or {}
        self._url = url

    def read(self, _n):
        return self._chunks.pop(0) if self._chunks else b""

    def geturl(self):
        return self._url

    def close(self):
        pass


class DownloadFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name) / "Downloads"
        self._orig_open = backend._open_url

    def tearDown(self):
        backend._open_url = self._orig_open
        backend.set_protected_paths([])
        self.tmp.cleanup()

    def stub_http(self, resp):
        backend._open_url = lambda _req: resp

    def test_filename_from_url(self):
        self.assertEqual(backend.derive_filename("https://x.com/files/app.zip"), "app.zip")
        self.assertEqual(backend.derive_filename("https://x.com/files/my%20app.zip"),
                         "my app.zip")

    def test_filename_from_content_disposition(self):
        cd = 'attachment; filename="Setup Tool.exe"'
        self.assertEqual(backend.derive_filename("https://x.com/dl?id=9", cd),
                         "Setup Tool.exe")
        cd2 = "attachment; filename*=UTF-8''R%C3%A9sum%C3%A9.pdf"
        self.assertEqual(backend.derive_filename("https://x.com/dl", cd2), "Résumé.pdf")

    def test_filename_fallback_and_sanitize(self):
        self.assertEqual(backend.derive_filename("https://x.com/"), "download.bin")
        self.assertEqual(backend.derive_filename("https://x.com/a/b<>:c.zip"),
                         "b___c.zip")

    def test_download_streams_to_disk(self):
        self.stub_http(FakeResponse([b"hello ", b"world"]))
        out = backend.tool_download_file(
            {"url": "https://example.com/app.zip", "folder": str(self.folder)})
        self.assertTrue(out["ok"], out)
        dest = Path(out["path"])
        self.assertEqual(dest.read_bytes(), b"hello world")
        self.assertEqual(out["bytes"], 11)
        self.assertFalse((self.folder / "app.zip.part").exists())

    def test_download_collision_gets_numbered_name(self):
        self.folder.mkdir(parents=True)
        (self.folder / "app.zip").write_bytes(b"old")
        self.stub_http(FakeResponse([b"new"]))
        out = backend.tool_download_file(
            {"url": "https://example.com/app.zip", "folder": str(self.folder)})
        self.assertTrue(out["ok"], out)
        self.assertEqual(Path(out["path"]).name, "app (1).zip")
        self.assertEqual((self.folder / "app.zip").read_bytes(), b"old")

    def test_download_into_protected_folder_refused(self):
        backend.set_protected_paths([str(self.folder)])
        self.stub_http(FakeResponse([b"x"]))
        out = backend.tool_download_file(
            {"url": "https://example.com/app.zip", "folder": str(self.folder)})
        self.assertFalse(out["ok"])
        self.assertIn("Protected", out["error"])
        self.assertFalse((self.folder / "app.zip").exists())

    def test_download_non_http_refused(self):
        out = backend.tool_download_file(
            {"url": "ftp://example.com/app.zip", "folder": str(self.folder)})
        self.assertFalse(out["ok"])

    def test_failed_download_leaves_no_part_file(self):
        def boom(_req):
            raise urllib.error.URLError("offline")
        backend._open_url = boom
        out = backend.tool_download_file(
            {"url": "https://example.com/app.zip", "folder": str(self.folder)})
        self.assertFalse(out["ok"])
        self.assertEqual(list(self.folder.glob("*.part")), [])


class InstallSoftwareTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._orig_exec = backend._exec_install
        self.exec_calls = []

    def tearDown(self):
        backend._exec_install = self._orig_exec
        backend.set_protected_paths([])
        self.tmp.cleanup()

    def fake_installer(self, name="setup.exe"):
        p = self.root / name
        p.write_bytes(b"MZ")
        return p

    def stub_exec(self, code):
        def run(argv):
            self.exec_calls.append(argv)
            return subprocess.CompletedProcess(argv, code, stdout="done", stderr="")
        backend._exec_install = run

    def test_msi_command(self):
        argv, kind = backend.pick_install_command(r"C:\dl\app.msi")
        self.assertEqual(kind, "msi")
        self.assertEqual(argv, ["msiexec", "/i", r"C:\dl\app.msi", "/qn", "/norestart"])

    def test_nsis_default_for_unknown_exe(self):
        argv, kind = backend.pick_install_command(r"C:\dl\SomeTool.exe")
        self.assertEqual(kind, "nsis")
        self.assertEqual(argv, [r"C:\dl\SomeTool.exe", "/S"])

    def test_inno_by_name_hint_and_explicit(self):
        argv, kind = backend.pick_install_command(r"C:\dl\app-innosetup.exe")
        self.assertEqual(kind, "inno")
        self.assertIn("/VERYSILENT", argv)
        argv2, kind2 = backend.pick_install_command(r"C:\dl\plain.exe", "inno")
        self.assertEqual(kind2, "inno")
        argv3, kind3 = backend.pick_install_command(r"C:\dl\plain.exe", "quiet")
        self.assertEqual(kind3, "generic-quiet")
        self.assertEqual(argv3[1:], ["/quiet", "/norestart"])

    def test_non_installer_rejected(self):
        argv, reason = backend.pick_install_command(r"C:\dl\notes.txt")
        self.assertIsNone(argv)
        self.assertIn("Not an", reason)

    def test_confirm_gate_blocks_until_confirmed(self):
        p = self.fake_installer()
        self.stub_exec(0)
        out = backend.tool_install_software({"path": str(p)})
        self.assertFalse(out["ok"])
        self.assertTrue(out["needs_confirm"])
        self.assertIn("plan", out)
        self.assertEqual(self.exec_calls, [])          # nothing ran

    def test_confirmed_install_runs_and_reports(self):
        p = self.fake_installer()
        self.stub_exec(0)
        out = backend.tool_install_software({"path": str(p), "confirmed": "true"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["exit_code"], 0)
        self.assertIn("successfully", out["note"])
        self.assertEqual(len(self.exec_calls), 1)
        self.assertEqual(self.exec_calls[0][1], "/S")  # silent flag used

    def test_exit_1603_reported_honestly(self):
        p = self.fake_installer()
        self.stub_exec(1603)
        out = backend.tool_install_software({"path": str(p), "confirmed": "true"})
        self.assertTrue(out["ok"])                     # the tool ran fine
        self.assertEqual(out["exit_code"], 1603)
        self.assertIn("1603", out["note"])

    def test_protected_installer_refused(self):
        p = self.fake_installer()
        backend.set_protected_paths([str(self.root)])
        self.stub_exec(0)
        out = backend.tool_install_software({"path": str(p), "confirmed": "true"})
        self.assertFalse(out["ok"])
        self.assertIn("Protected", out["error"])
        self.assertEqual(self.exec_calls, [])

    def test_missing_installer(self):
        out = backend.tool_install_software(
            {"path": str(self.root / "nope.exe"), "confirmed": "true"})
        self.assertFalse(out["ok"])
        self.assertIn("not found", out["error"])


class _V:
    """Fake pyttsx3 voice object."""
    def __init__(self, name, lang, vid=None):
        self.name, self.lang, self.id = name, lang, vid or name


class PickVoiceTest(unittest.TestCase):
    def setUp(self):
        self.zira = _V("Microsoft Zira - English (United States)", "en-US")
        self.david = _V("Microsoft David - English (United States)", "en-US")
        self.george = _V("Microsoft George - English (United Kingdom)", "en-GB")
        self.ryan = _V("Microsoft Ryan - English (United Kingdom)", "en-GB")
        self.hazel = _V("Microsoft Hazel - English (United Kingdom)", "en-GB")

    def test_british_male_wins_over_us_voices(self):
        pick = jarvis.pick_voice([self.zira, self.david, self.george])
        self.assertIs(pick, self.george)

    def test_preference_order_ryan_before_george(self):
        pick = jarvis.pick_voice([self.george, self.ryan, self.zira])
        self.assertIs(pick, self.ryan)

    def test_david_when_no_british_male(self):
        pick = jarvis.pick_voice([self.zira, self.david])
        self.assertIs(pick, self.david)          # never Zira while David exists

    def test_female_only_falls_back_to_first(self):
        pick = jarvis.pick_voice([self.zira, self.hazel])
        self.assertIs(pick, self.zira)

    def test_user_name_override_wins(self):
        pick = jarvis.pick_voice([self.george, self.hazel], "hazel")
        self.assertIs(pick, self.hazel)

    def test_generic_male_marked_voice(self):
        male = _V("Generic Male Voice", "en-US")
        self.assertIs(jarvis.pick_voice([self.zira, male]), male)

    def test_empty_and_none(self):
        self.assertIsNone(jarvis.pick_voice([]))
        self.assertIsNone(jarvis.pick_voice(None))


class CalmReplyTest(unittest.TestCase):
    def test_details_dump_stripped(self):
        raw = ("⚠ This brain's free daily quota is finished.\n"
               "(Details: Model API error 429: [{\"error\": {\"code\": 429}}])")
        out = jarvis.calm_reply(raw)
        self.assertNotIn("Details", out)
        self.assertNotIn("429", out)
        self.assertIn("quota is finished", out)

    def test_plain_reply_unchanged(self):
        self.assertEqual(jarvis.calm_reply("All done, Sir."), "All done, Sir.")


class SelfCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_dir, self._old_file = jarvis.CFG_DIR, jarvis.CFG_FILE
        jarvis.CFG_DIR = Path(self.tmp.name) / "NovaAgent"
        jarvis.CFG_FILE = jarvis.CFG_DIR / "config.json"

    def tearDown(self):
        jarvis.CFG_DIR, jarvis.CFG_FILE = self._old_dir, self._old_file
        self.tmp.cleanup()

    def check(self, cfg):
        return jarvis.JarvisApp(cfg).self_check()

    def test_ollama_needs_no_key(self):
        lines = self.check({"provider": "ollama", "protected_paths": []})
        self.assertTrue(all(ln.startswith("[SELF-CHECK]") for ln in lines))
        self.assertTrue(any("Ollama" in ln for ln in lines))
        self.assertFalse(any("NO API key" in ln for ln in lines))

    def test_missing_key_gets_exact_fix_line(self):
        lines = self.check({"provider": "openai", "api_key": "",
                            "protected_paths": ["a", "b"]})
        warn = [ln for ln in lines if "NO API key" in ln]
        self.assertEqual(len(warn), 1)
        self.assertIn("api_key", warn[0])
        self.assertTrue(any("2 item(s)" in ln for ln in lines))

    def test_key_present_no_warning(self):
        lines = self.check({"provider": "openai", "api_key": "sk-x",
                            "model": "gpt-4o", "protected_paths": []})
        self.assertFalse(any("NO API key" in ln for ln in lines))
        self.assertTrue(any("key present" in ln for ln in lines))


class SearchAndOwnComputerTest(unittest.TestCase):
    """Built-in search + own-computer workspace: JARVIS can search the
    web itself (no API key, no separate download) and has its own
    workspace, while still reaching the user's computer via tools."""

    def test_search_html_parses_results(self):
        import backend as _b
        html = ('<a class="result__a" href="https://example.com/kali">Kali Linux</a>'
                '<a class="result__snippet" href="#">Official Kali download page</a>')
        res = _b.parse_search_html(html)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["title"], "Kali Linux")
        self.assertEqual(res[0]["url"], "https://example.com/kali")

    def test_search_tool_empty_query_fails_cleanly(self):
        import backend as _b
        self.assertFalse(_b.tool_search_web({})["ok"])

    def test_tools_include_search_and_own_computer(self):
        import backend as _b
        self.assertIn("search_web", _b.TOOLS)
        self.assertIn("jarvis_computer", _b.TOOLS)
        self.assertIn("search_web", _b.SYSTEM_PROMPT)
        self.assertIn("JARVIS-Computer", _b.SYSTEM_PROMPT)

    def test_own_computer_workspace_created(self):
        import backend as _b, tempfile, os
        old_home = os.environ.get("HOME")
        with tempfile.TemporaryDirectory() as td:
            os.environ["HOME"] = td
            # Path.home() caches on some platforms; patch it directly
            orig = _b.Path.home
            _b.Path.home = classmethod(lambda cls: _b.Path(td))
            try:
                base = _b.jarvis_workspace_path()
                self.assertTrue((base / "Work").exists())
                self.assertTrue((base / "Downloads").exists())
                out = _b.tool_jarvis_computer({})
                self.assertTrue(out["ok"])
                self.assertIn("JARVIS-Computer", out["path"])
            finally:
                _b.Path.home = orig
                if old_home is not None:
                    os.environ["HOME"] = old_home

class SearchDownloadAndYoutubeTest(unittest.TestCase):
    """New workflows: search→download→open→notify, and YouTube upload
    via the official API only, with a confirm gate and no passwords."""

    def test_pick_direct_file_url(self):
        import backend as _b
        res = [{"url": "https://example.com/page", "title": "Page"},
               {"url": "https://example.com/app.zip", "title": "App"}]
        self.assertEqual(_b.pick_direct_file_url(res), "https://example.com/app.zip")
        self.assertIsNone(_b.pick_direct_file_url([{"url": "https://example.com/page"}]))

    def test_search_download_direct_url_flow(self):
        import backend as _b
        # Stub download/open so no network or GUI is touched.
        orig_dl, orig_open = _b.tool_download_file, _b.tool_open_path
        _b.tool_download_file = lambda a: {"ok": True, "path": "/tmp/app.zip", "size_mb": 1.2}
        _b.tool_open_path = lambda a: {"ok": True, "path": a["path"]}
        try:
            out = _b.tool_search_download({"query": "https://example.com/app.zip"})
        finally:
            _b.tool_download_file, _b.tool_open_path = orig_dl, orig_open
        self.assertTrue(out["ok"])
        self.assertEqual(out["downloaded_path"], "/tmp/app.zip")
        self.assertTrue(out["opened"])
        self.assertIn("Downloaded and opened", out["notification"])

    def test_youtube_upload_needs_confirm_and_existing_file(self):
        import backend as _b, tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            vid = f.name
        try:
            out = _b.tool_youtube_upload({"video_path": vid, "title": "Test"})
            self.assertFalse(out["ok"])
            self.assertTrue(out.get("needs_confirm"))
            self.assertIn("Not uploaded yet", out["error"])
            out2 = _b.tool_youtube_upload({"video_path": "/tmp/does-not-exist-xyz.mp4", "title": "Test", "confirmed": "true"})
            self.assertFalse(out2["ok"])
            self.assertIn("not found", out2["error"])
        finally:
            os.unlink(vid)

    def test_youtube_upload_confirmed_without_api_is_setup_not_crash(self):
        import backend as _b, tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            vid = f.name
        try:
            out = _b.tool_youtube_upload({"video_path": vid, "title": "Test", "confirmed": "true"})
        finally:
            os.unlink(vid)
        # Either the API client is missing (setup message) or the token
        # is missing — in both cases no upload happened and no password
        # was asked for, and the message must not crash.
        self.assertFalse(out["ok"])
        self.assertIn("YouTube", out["error"])

    def test_tools_include_new_workflows(self):
        import backend as _b
        self.assertIn("search_download", _b.TOOLS)
        self.assertIn("youtube_upload", _b.TOOLS)

class HudFileTest(unittest.TestCase):
    """The shipped HUD must carry the native bridge and the 3D
    background layer (grep-verified, no browser needed)."""
    @classmethod
    def setUpClass(cls):
        cls.html = (Path(__file__).resolve().parent.parent
                    / "jarvis-hud.html").read_text(encoding="utf-8")

    def test_native_bridge_present(self):
        for token in ("JARVIS_NATIVE", "onSuggestion", "onWakeHeard",
                      "onNativeLog", "native_screen_watch", "pywebviewready",
                      "setScreenWatchState", "native_chat", "native_minimize",
                      "native_get_settings", "native_save_settings",
                      "settingsBtn", "settingsPanel", "setSaveBtn",
                      "minBtn", "lastSearchQuestion", "isLocalQuestion"):
            self.assertIn(token, self.html)

    def test_background_layer_present(self):
        for token in ('id="bg3d"', "initBackground", "__JARVIS_BG__",
                      "TorusGeometry", "setLevel"):
            self.assertIn(token, self.html)

    def test_settings_panel_is_body_level_not_inside_app(self):
        # Regression: when settingsPanel lived inside #topbar, any
        # ancestor panel transform made the fixed modal clip behind
        # the arc reactor. It must come after the #app close, at body
        # level, with a z-index above every HUD layer.
        html = self.html
        self.assertIn("z-index:9999", html)
        app_close = html.find('<div id="footerNote"')
        panel_pos = html.find('id="settingsPanel"')
        self.assertTrue(app_close > 0 and panel_pos > app_close,
                        "settingsPanel must be after the app footer, not inside #app")

    def test_settings_build3_marker_and_inline_frontmost(self):
        # BUILD 6 (supersedes BUILD 5/3): user must be able to tell
        # old vs new exe at a glance, and the panel must be frontmost
        # via inline style too, not CSS alone.
        self.assertIn("BUILD 6", self.html)
        self.assertIn('style.zIndex = "999999"', self.html)

    def test_settings_close_clears_inline_display(self):
        # Regression: opening the panel sets inline display="flex".
        # Cancel / Esc / Save only removed the "open" class, leaving
        # that inline style in place, so the panel stayed visible and
        # the only way out looked like closing JARVIS itself. The
        # close helper must clear the inline display too, and all
        # three exits (Cancel, Esc, Save, plus overlay click) must
        # use it.
        self.assertIn("function closeSettingsPanel()", self.html)
        self.assertIn('style.display = "none"', self.html)
        self.assertIn('$("setCancelBtn").addEventListener("click", closeSettingsPanel)', self.html)
        self.assertIn("closeSettingsPanel()", self.html)
        # Save must close immediately, not via a delayed class-only removal
        self.assertNotIn('setTimeout(() => { $("settingsPanel").classList.remove("open")', self.html)

    def test_settings_panel_is_frontmost_not_hiding_animation(self):
        # User correction: Settings must sit IN FRONT of the animation,
        # the animation must NOT disappear. So the panel must be
        # z-index 9999, solid, body-level — and there must be NO rule
        # hiding #app / #bg3d when Settings opens.
        self.assertIn("z-index:9999", self.html)
        self.assertNotIn("body.settings-open #app", self.html)
        self.assertNotIn("body.settings-open #bg3d", self.html)

    def test_background_sheds_before_reactor(self):
        # governor order: background steps (1-2) come before reactor
        # steps — the stepDown body mentions bgSet(1) at quality 1.
        self.assertIn("quality === 1){ bgSet(1); }", self.html)
        self.assertIn("quality < 5", self.html)


if __name__ == "__main__":
    unittest.main(verbosity=2)

