"""Config + wake-word tests for NOVA — run:  python3 tests/test_app_config.py

Covers: load_config defaults (incl. wake_word_enabled=False, the opt-in
switch for the always-listening microphone), the retired-model self-heal
(gemini-2.5-flash -> gemini-3.8-flash on load), and the pure wake-phrase
parser in agent.py that decides whether a recognized phrase addresses
NOVA, and what command followed the wake word. No audio or GUI needed:
customtkinter is stubbed if absent, exactly as in test_agent_logic.py.
"""
import json, os, sys, tempfile, types, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import backend as nova


class WakePhraseTest(unittest.TestCase):
    def parse(self, text):
        return nova.parse_wake_phrase(text)

    def test_wake_plus_command_same_utterance(self):
        self.assertEqual(self.parse("hey nova open downloads"), "open downloads")

    def test_wake_word_alone_means_listen_next(self):
        self.assertEqual(self.parse("nova"), "")
        self.assertEqual(self.parse("hey nova"), "")
        self.assertEqual(self.parse("Nova!"), "")

    def test_case_and_punctuation(self):
        self.assertEqual(self.parse("Hey Nova, open my Downloads"), "open my Downloads")
        self.assertEqual(self.parse("NOVA what time is it"), "what time is it")

    def test_unrelated_phrase_ignored(self):
        self.assertIsNone(self.parse("open the downloads folder"))
        self.assertIsNone(self.parse("what a lovely day"))
        self.assertIsNone(self.parse(""))

    def test_no_false_trigger_inside_words(self):
        self.assertIsNone(self.parse("supernova explosion"))
        self.assertIsNone(self.parse("renovation plans"))


class WakeConfigTest(unittest.TestCase):
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
        if data is None:
            p = d / "config.json"
            if p.exists(): p.unlink()
        else:
            (d / "config.json").write_text(json.dumps(data))

    def test_no_config_file_defaults_wake_off(self):
        self.write_cfg(None)
        cfg = self.app.load_config()
        self.assertFalse(cfg["wake_word_enabled"])   # opt-in, never on by surprise
        self.assertTrue(cfg["voice_enabled"])

    def test_old_config_without_wake_key_defaults_off(self):
        self.write_cfg({"provider": "openai", "api_key": "k",
                        "protected_paths": [], "voice_enabled": True})
        cfg = self.app.load_config()
        self.assertFalse(cfg["wake_word_enabled"])

    def test_wake_enabled_roundtrips(self):
        self.write_cfg({"provider": "ollama", "wake_word_enabled": True})
        cfg = self.app.load_config()
        self.assertTrue(cfg["wake_word_enabled"])

    def test_retired_gemini_model_auto_upgraded_on_load(self):
        # Faizan's saved config still said gemini-2.5-flash, which Google
        # retired (404) — load_config must silently heal it.
        self.write_cfg({"provider": "openai", "model": "gemini-2.5-flash"})
        self.assertEqual(self.app.load_config()["model"], "gemini-3.8-flash")

    def test_retired_gemini_model_with_models_prefix(self):
        self.write_cfg({"provider": "openai", "model": "models/gemini-2.5-flash"})
        self.assertEqual(self.app.load_config()["model"], "gemini-3.8-flash")

    def test_other_models_pass_through_unchanged(self):
        self.write_cfg({"provider": "openai", "model": "gpt-4o"})
        self.assertEqual(self.app.load_config()["model"], "gpt-4o")
        self.assertEqual(self.app.migrate_model_name("gemini-3.8-flash"), "gemini-3.8-flash")
        self.assertEqual(self.app.migrate_model_name("claude-sonnet-5"), "claude-sonnet-5")
        self.assertEqual(self.app.migrate_model_name(""), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
