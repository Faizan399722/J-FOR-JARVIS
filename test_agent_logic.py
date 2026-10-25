"""Logic tests for NOVA (JARVIS edition) — run:  python3 tests/test_agent_logic.py

Covers: protected-paths enforcement (the one restriction), no-approval
execution, auto-fallback behaviour, and config backwards compatibility.
Uses only the stdlib (unittest). customtkinter is stubbed if absent so
app.py's config loader can be exercised on a headless machine.
"""
import inspect, json, os, sys, tempfile, types, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import backend as nova


class ProtectedFolderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "vault").mkdir()
        (root / "vault" / "secret.txt").write_text("top secret")
        (root / "open").mkdir()
        (root / "open" / "note.txt").write_text("hello")
        (root / "open" / "report.txt").write_text("numbers")
        self.root, self.vault = root, root / "vault"
        self.agent = nova.Agent({"provider": "ollama",
                                 "protected_paths": [str(self.vault)]})

    def tearDown(self):
        nova.set_protected_paths([])
        self.tmp.cleanup()

    def run_tool(self, name, args):
        return self.agent._run_tool(name, args)

    def test_read_inside_protected_refused(self):
        r = self.run_tool("read_file", {"path": str(self.vault / "secret.txt")})
        self.assertFalse(r["ok"]); self.assertIn("⛔", r["error"])

    def test_list_protected_refused(self):
        r = self.run_tool("list_files", {"path": str(self.vault)})
        self.assertFalse(r["ok"]); self.assertIn("⛔", r["error"])

    def test_write_into_protected_refused_and_not_created(self):
        target = self.vault / "new.txt"
        r = self.run_tool("write_file", {"path": str(target), "content": "x"})
        self.assertFalse(r["ok"]); self.assertFalse(target.exists())

    def test_delete_protected_refused_and_file_survives(self):
        target = self.vault / "secret.txt"
        r = self.run_tool("delete_path", {"path": str(target)})
        self.assertFalse(r["ok"]); self.assertTrue(target.exists())

    def test_open_protected_refused(self):
        r = self.run_tool("open_path", {"path": str(self.vault / "secret.txt")})
        self.assertFalse(r["ok"]); self.assertIn("⛔", r["error"])

    def test_search_never_reveals_protected(self):
        r = self.run_tool("search_files", {"name_contains": "secret", "root": str(self.root)})
        self.assertTrue(r["ok"]); self.assertEqual(r["matches"], [])
        r2 = self.run_tool("search_files", {"name_contains": "report", "root": str(self.root)})
        self.assertTrue(any("report.txt" in m for m in r2["matches"]))

    def test_run_command_mentioning_protected_refused(self):
        r = self.run_tool("run_command", {"command": f"cat {self.vault}/secret.txt"})
        self.assertFalse(r["ok"]); self.assertIn("⛔", r["error"])

    def test_run_command_unprotected_allowed(self):
        r = self.run_tool("run_command", {"command": "echo nova-ok"})
        self.assertTrue(r["ok"]); self.assertIn("nova-ok", r["stdout"])

    def test_unprotected_read_write_delete_allowed(self):
        note = self.root / "open" / "note.txt"
        self.assertTrue(self.run_tool("read_file", {"path": str(note)})["ok"])
        new = self.root / "open" / "fresh.txt"
        self.assertTrue(self.run_tool("write_file", {"path": str(new), "content": "hi"})["ok"])
        self.assertTrue(new.exists())
        self.assertTrue(self.run_tool("delete_path", {"path": str(new)})["ok"])
        self.assertFalse(new.exists())


class GlobAndBareNameTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "keys").mkdir()
        (root / "keys" / "private.key").write_text("KEY")
        (root / "keys" / "readme.txt").write_text("ok")
        (root / "elsewhere" / "vault").mkdir(parents=True)
        (root / "elsewhere" / "vault" / "deep.txt").write_text("deep")
        self.root = root
        self.agent = nova.Agent({"provider": "ollama",
                                 "protected_paths": ["*.key", "vault"]})

    def tearDown(self):
        nova.set_protected_paths([])
        self.tmp.cleanup()

    def test_glob_pattern_blocks_key_files(self):
        r = self.agent._run_tool("read_file", {"path": str(self.root / "keys" / "private.key")})
        self.assertFalse(r["ok"]); self.assertIn("⛔", r["error"])
        r2 = self.agent._run_tool("read_file", {"path": str(self.root / "keys" / "readme.txt")})
        self.assertTrue(r2["ok"])

    def test_bare_folder_name_blocks_anywhere(self):
        r = self.agent._run_tool("read_file",
                                 {"path": str(self.root / "elsewhere" / "vault" / "deep.txt")})
        self.assertFalse(r["ok"]); self.assertIn("⛔", r["error"])

    def test_glob_blocks_command_mention(self):
        r = self.agent._run_tool("run_command", {"command": "cat private.key"})
        self.assertFalse(r["ok"]); self.assertIn("⛔", r["error"])


class NoApprovalTest(unittest.TestCase):
    def tearDown(self):
        nova.set_protected_paths([])

    def test_agent_has_no_confirm_parameter(self):
        params = inspect.signature(nova.Agent.__init__).parameters
        self.assertNotIn("confirm", params)
        self.assertFalse(hasattr(nova, "RISKY"))

    def test_risky_tools_execute_directly(self):
        with tempfile.TemporaryDirectory() as td:
            agent = nova.Agent({"provider": "ollama", "protected_paths": []})
            f = Path(td) / "auto.txt"
            r = agent._run_tool("write_file", {"path": str(f), "content": "no prompt"})
            self.assertTrue(r["ok"]); self.assertTrue(f.exists())
            r = agent._run_tool("run_command", {"command": "echo direct"})
            self.assertTrue(r["ok"]); self.assertIn("direct", r["stdout"])
            r = agent._run_tool("delete_path", {"path": str(f)})
            self.assertTrue(r["ok"]); self.assertFalse(f.exists())

    def test_protected_paths_shown_in_system_prompt(self):
        agent = nova.Agent({"provider": "ollama", "protected_paths": ["/somewhere/vault"]})
        self.assertIn("/somewhere/vault", agent._system_prompt())
        self.assertIn("off-limits", agent._system_prompt())

    def test_retired_gemini_model_is_migrated(self):
        # Google retired gemini-2.5-flash (404) — a stale saved config must
        # be upgraded automatically, in both bare and "models/" forms.
        a = nova.Agent({"provider": "openai", "api_key": "k", "model": "gemini-2.5-flash"})
        self.assertEqual(a.model, "gemini-3.8-flash")
        a = nova.Agent({"provider": "openai", "api_key": "k", "model": "models/gemini-2.5-flash"})
        self.assertEqual(a.model, "gemini-3.8-flash")
        a = nova.Agent({"provider": "openai", "api_key": "k", "model": "gpt-4o"})
        self.assertEqual(a.model, "gpt-4o")   # anything else passes through


class FallbackTest(unittest.TestCase):
    def make_agent(self, **fb_over):
        fb = {"enabled": True, "provider": "ollama", "api_key": "", "model": "", "base_url": ""}
        fb.update(fb_over)
        return nova.Agent({"provider": "openai", "api_key": "k", "model": "m",
                           "base_url": "http://127.0.0.1:9/v1", "fallback": fb})

    def tearDown(self):
        nova.set_protected_paths([])

    def test_network_error_falls_back_and_restores_primary(self):
        agent = self.make_agent()
        def fake():
            if agent.provider == "ollama":
                return "fallback reply"
            raise RuntimeError("Could not reach the model API: down")
        agent._run_provider_loop = fake
        reply = agent.chat("hello")
        self.assertIn("switched to the fallback brain", reply)
        self.assertIn("fallback reply", reply)
        self.assertEqual(agent.provider, "openai")  # primary restored

    def test_non_network_error_does_not_fall_back(self):
        agent = self.make_agent()
        def fake():
            raise RuntimeError("Model API error 401: bad key")
        agent._run_provider_loop = fake
        reply = agent.chat("hello")
        self.assertTrue(reply.startswith("⚠"))
        self.assertNotIn("fallback brain", reply)

    def test_fallback_disabled_shows_error(self):
        agent = self.make_agent(enabled=False)
        def fake():
            raise RuntimeError("Could not reach the model API: down")
        agent._run_provider_loop = fake
        reply = agent.chat("hello")
        self.assertTrue(reply.startswith("⚠"))
        self.assertNotIn("switched", reply)


class ConfigCompatTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import customtkinter  # noqa: F401
        except ImportError:
            stub = types.ModuleType("customtkinter")
            class _W:
                def __init__(self, *a, **k): pass
            stub.CTk = _W
            sys.modules["customtkinter"] = stub
        cls._tmp = tempfile.TemporaryDirectory()
        cls._old_env = os.environ.get("APPDATA")
        os.environ["APPDATA"] = cls._tmp.name
        import jarvis as app
        cls.app = app

    @classmethod
    def tearDownClass(cls):
        if cls._old_env is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = cls._old_env
        cls._tmp.cleanup()

    def write_cfg(self, data):
        d = Path(self._tmp.name) / "NovaAgent"
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.json").write_text(json.dumps(data))

    def test_old_config_without_new_keys_loads_with_defaults(self):
        self.write_cfg({"provider": "openai", "api_key": "k", "model": "", "base_url": ""})
        cfg = self.app.load_config()
        self.assertEqual(cfg["protected_paths"], [])
        self.assertTrue(cfg["voice_enabled"])
        self.assertTrue(cfg["fallback"]["enabled"])
        self.assertEqual(cfg["fallback"]["provider"], "ollama")

    def test_new_config_values_roundtrip(self):
        self.write_cfg({"provider": "ollama", "protected_paths": ["C:\\Secrets", "*.key"],
                        "voice_enabled": False})
        cfg = self.app.load_config()
        self.assertEqual(cfg["protected_paths"], ["C:\\Secrets", "*.key"])
        self.assertFalse(cfg["voice_enabled"])

    def test_speech_cleaner_strips_code_and_caps_length(self):
        out = self.app.clean_for_speech("Run this: ```rm -rf x``` then **check** the `file`.")
        self.assertNotIn("rm -rf", out)
        self.assertNotIn("**", out)
        long_out = self.app.clean_for_speech("word " * 400)
        self.assertLessEqual(len(long_out), 601)


if __name__ == "__main__":
    unittest.main(verbosity=2)
