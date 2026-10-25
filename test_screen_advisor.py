"""Screen Advisor tests for NOVA — run:  python3 tests/test_screen_advisor.py

Covers: the pure suggestion filter (normalize_suggestion / should_deliver),
the vision-call payloads with the HTTP layer stubbed (no network, and proof
the advisor never pollutes chat history), and the new config keys
(screen_advisor_enabled opt-in default, interval clamped to >= 30s).
customtkinter is stubbed if absent, as in the other test files.
"""
import json, os, sys, tempfile, types, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import backend as nova


class NormalizeSuggestionTest(unittest.TestCase):
    def test_nothing_variants_suppressed(self):
        for raw in ("NOTHING", "nothing", "Nothing.", " NOTHING \n",
                    "Nothing!", "Nothing to suggest.",
                    "Nothing to suggest right now", "No suggestion",
                    "No action needed", "Nothing worth suggesting."):
            self.assertIsNone(nova.normalize_suggestion(raw), raw)

    def test_empty_and_none_suppressed(self):
        self.assertIsNone(nova.normalize_suggestion(None))
        self.assertIsNone(nova.normalize_suggestion(""))
        self.assertIsNone(nova.normalize_suggestion("   \n  "))

    def test_real_suggestion_passes_cleaned(self):
        self.assertEqual(nova.normalize_suggestion("Save the document you are editing."),
                         "Save the document you are editing.")
        self.assertEqual(nova.normalize_suggestion("Suggestion: Reply to the open email."),
                         "Reply to the open email.")
        self.assertEqual(nova.normalize_suggestion("💡 Check the error in the terminal.\n"),
                         "Check the error in the terminal.")


class ShouldDeliverTest(unittest.TestCase):
    def test_nothing_never_delivered(self):
        self.assertFalse(nova.should_deliver(None, None))
        self.assertFalse(nova.should_deliver("", None))

    def test_first_suggestion_delivered(self):
        self.assertTrue(nova.should_deliver("Save your file.", None))

    def test_duplicate_suppressed(self):
        s = "Save your file before the battery runs out."
        self.assertFalse(nova.should_deliver(s, s))
        self.assertFalse(nova.should_deliver("save your file before the battery runs out", s))
        self.assertFalse(nova.should_deliver("Save your file, before the battery runs out!", s))

    def test_different_suggestion_delivered(self):
        self.assertTrue(nova.should_deliver("Reply to the email from your manager.",
                                            "Save your file before the battery runs out."))


class VisionCallTest(unittest.TestCase):
    """HTTP layer stubbed: capture the payload, return a canned reply."""

    def setUp(self):
        self.calls = []
        self._orig = nova._http_json

    def tearDown(self):
        nova._http_json = self._orig
        nova.set_protected_paths([])

    def stub(self, reply_payload):
        def fake(url, headers, payload, timeout=180):
            self.calls.append({"url": url, "headers": headers, "payload": payload})
            return reply_payload
        nova._http_json = fake

    def test_openai_style_vision_message_shape(self):
        agent = nova.Agent({"provider": "openai", "api_key": "k",
                            "model": "gemini-3.8-flash",
                            "base_url": "https://example.test/v1beta/openai/"})
        self.stub({"choices": [{"message": {"content": "Reply to that email."}}]})
        out = agent.screen_suggestion("FAKEB64")
        self.assertEqual(out, "Reply to that email.")
        self.assertEqual(len(self.calls), 1)
        call = self.calls[0]
        self.assertTrue(call["url"].endswith("/chat/completions"))
        self.assertNotIn("tools", call["payload"])           # no tools on advisor calls
        msgs = call["payload"]["messages"]
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["role"], "user")
        parts = msgs[0]["content"]
        self.assertIsInstance(parts, list)
        text_part = [p for p in parts if p.get("type") == "text"]
        img_part = [p for p in parts if p.get("type") == "image_url"]
        self.assertEqual(len(text_part), 1)
        self.assertIn("NOTHING", text_part[0]["text"])
        self.assertEqual(len(img_part), 1)
        self.assertEqual(img_part[0]["image_url"]["url"],
                         "data:image/jpeg;base64,FAKEB64")
        # the advisor must not pollute chat history
        self.assertEqual(agent.history, [])

    def test_anthropic_vision_blocks(self):
        agent = nova.Agent({"provider": "anthropic", "api_key": "k",
                            "model": "claude-sonnet-5"})
        self.stub({"content": [{"type": "text", "text": "NOTHING"}]})
        out = agent.screen_suggestion("FAKEB64")
        self.assertEqual(out, "NOTHING")
        self.assertIsNone(nova.normalize_suggestion(out))
        payload = self.calls[0]["payload"]
        self.assertNotIn("tools", payload)
        blocks = payload["messages"][0]["content"]
        img = [b for b in blocks if b.get("type") == "image"]
        self.assertEqual(len(img), 1)
        self.assertEqual(img[0]["source"]["type"], "base64")
        self.assertEqual(img[0]["source"]["media_type"], "image/jpeg")
        self.assertEqual(img[0]["source"]["data"], "FAKEB64")
        self.assertEqual(agent.history, [])


class AdvisorConfigTest(unittest.TestCase):
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

    def test_defaults_watch_off_interval_90(self):
        self.write_cfg(None)
        cfg = self.app.load_config()
        self.assertFalse(cfg["screen_watch_enabled"])   # opt-in, like wake word
        self.assertEqual(cfg["screen_watch_interval_sec"], 90)

    def test_old_config_without_watch_keys_loads(self):
        self.write_cfg({"provider": "openai", "api_key": "k"})
        cfg = self.app.load_config()
        self.assertFalse(cfg["screen_watch_enabled"])
        self.assertEqual(cfg["screen_watch_interval_sec"], 90)

    def test_interval_clamped_to_minimum_30(self):
        self.write_cfg({"screen_watch_enabled": True, "screen_watch_interval_sec": 5})
        cfg = self.app.load_config()
        self.assertTrue(cfg["screen_watch_enabled"])
        self.assertEqual(cfg["screen_watch_interval_sec"], 30)

    def test_bad_interval_falls_back_to_90(self):
        self.write_cfg({"screen_watch_interval_sec": "soon"})
        cfg = self.app.load_config()
        self.assertEqual(cfg["screen_watch_interval_sec"], 90)

    def test_nova_advisor_keys_map_across_as_alias(self):
        # A config written by NOVA (screen_advisor_*) must carry over:
        # JARVIS reads the same file and honours those keys as its own.
        self.write_cfg({"screen_advisor_enabled": True,
                        "screen_advisor_interval_sec": 45})
        cfg = self.app.load_config()
        self.assertTrue(cfg["screen_watch_enabled"])
        self.assertEqual(cfg["screen_watch_interval_sec"], 45)

    def test_jarvis_keys_win_over_legacy_alias(self):
        self.write_cfg({"screen_watch_enabled": False,
                        "screen_advisor_enabled": True})
        cfg = self.app.load_config()
        self.assertFalse(cfg["screen_watch_enabled"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
