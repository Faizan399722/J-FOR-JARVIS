"""Transient-error handling tests for NOVA — run:  python3 tests/test_transient.py

Covers the Gemini-503 lesson from Faizan's PC: provider overload spikes
(HTTP 429/500/503, RESOURCE_EXHAUSTED / UNAVAILABLE / INTERNAL bodies)
are TRANSIENT — retried with backoff in _http_json, answered with a
friendly message in chat(), and treated by the Screen Advisor as 'skip
this cycle' instead of 'vision is broken, switch off forever'. Hard
errors (400/401/403/404) behave exactly as before. urllib is stubbed,
and the retry sleep is faked — no network, no real waiting.

Also covers the v2.4 lesson from the same PC: a 429 whose body says
the free DAILY quota is finished ("per day limit exceeded", "retry in
15h22m") is a QUOTA error, not overload — never retried with backoff,
answered with the quota message (or a fallback-brain switch) in
chat(), and only a 'skip' for the Screen Advisor — as are network-
level failures like an SSL EOF, which used to switch the Advisor OFF.
"""
import io, json, socket, sys, unittest, urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import backend as nova

GEMINI_503 = {"error": {"code": 503,
                        "message": "This model is currently experiencing high demand. "
                                   "Spikes in demand are usually temporary. Please try again later.",
                        "status": "UNAVAILABLE"}}
GEMINI_404 = {"error": {"code": 404,
                        "message": "This model models/gemini-2.5-flash is no longer available to new users.",
                        "status": "NOT_FOUND"}}
# The REAL error behind Faizan's v2.3 screenshot: HTTP 429, but the body
# says the free DAILY quota is gone (limit 20/day, retry in ~15 hours).
GEMINI_429_QUOTA = {"error": {"code": 429,
                        "message": "You exceeded your current quota, please check your plan and "
                                   "billing details. Quota exceeded for metric: "
                                   "generativelanguage.googleapis.com/generate_content_free_tier_requests, "
                                   "limit: 20, model: gemini-3.6-flash, description: Generate content "
                                   "requests per day limit exceeded on Free Tier. "
                                   "Please retry in 15h22m26.339407577s",
                        "status": "RESOURCE_EXHAUSTED"}}
# Same 429 + RESOURCE_EXHAUSTED shape, but PER-MINUTE: a rate limit that
# clears in seconds. Note the metric name CONTAINS the daily one.
GEMINI_429_PER_MIN = {"error": {"code": 429,
                        "message": "Quota exceeded for metric: "
                                   "generativelanguage.googleapis.com/generate_content_free_tier_requests_per_minute, "
                                   "limit: 15. Please retry in 30s",
                        "status": "RESOURCE_EXHAUSTED"}}


def quota_runtime_error():
    """The quota failure exactly as chat()/the advisor receive it."""
    return RuntimeError("Model API error 429: " + json.dumps(GEMINI_429_QUOTA))


def http_error(code, body):
    return urllib.error.HTTPError("https://example.test/v1", code, "err", None,
                                  io.BytesIO(json.dumps(body).encode()))


class _Resp:
    """Minimal stand-in for a urlopen response (context manager + read)."""
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TransientClassificationTest(unittest.TestCase):
    def test_http_codes(self):
        for code in (429, 500, 503):
            self.assertTrue(nova.is_transient_error(f"Model API error {code}: {{}}"), code)
        for code in (400, 401, 403, 404):
            self.assertFalse(nova.is_transient_error(f"Model API error {code}: {{}}"), code)

    def test_status_strings_in_body(self):
        self.assertTrue(nova.is_transient_error(
            'Model API error 400: {"error": {"status": "RESOURCE_EXHAUSTED"}}'))
        self.assertTrue(nova.is_transient_error(
            'Model API error 503: {"error": {"status": "UNAVAILABLE"}}'))
        self.assertTrue(nova.is_transient_error(
            'Model API error 500: {"error": {"status": "INTERNAL"}}'))
        self.assertFalse(nova.is_transient_error(
            'Model API error 404: {"error": {"status": "NOT_FOUND"}}'))

    def test_typed_error_is_transient_network_error_is_not(self):
        self.assertTrue(nova.is_transient_error(
            nova.TransientModelError("Model API error 503: x")))
        # the fallback trigger text must NOT be classified transient
        self.assertFalse(nova.is_transient_error(
            RuntimeError("Could not reach the model API: down")))
        self.assertFalse(nova.is_transient_error(RuntimeError("No API key set.")))


class RetryTest(unittest.TestCase):
    def setUp(self):
        self.sleeps = []
        self._orig_sleep = nova._sleep
        nova._sleep = self.sleeps.append

    def tearDown(self):
        nova._sleep = self._orig_sleep
        nova.set_protected_paths([])

    def run_http(self, effects):
        """Call _http_json against a scripted urlopen. Returns
        (result_or_exception, list_of_requests)."""
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req)
            eff = effects[len(calls) - 1]
            if isinstance(eff, Exception):
                raise eff
            return eff

        with mock.patch.object(nova.urllib.request, "urlopen", fake_urlopen):
            try:
                return nova._http_json("https://example.test/v1", {}, {"x": 1}), calls
            except Exception as e:  # noqa: BLE001 — returned for assertions
                return e, calls

    def test_retry_succeeds_on_second_attempt(self):
        result, calls = self.run_http([http_error(503, GEMINI_503), _Resp({"ok": True})])
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.sleeps, [2])

    def test_retry_waits_grow_2_5_10(self):
        result, calls = self.run_http([
            http_error(429, {"error": {"status": "RESOURCE_EXHAUSTED"}}),
            http_error(500, {"error": {"status": "INTERNAL"}}),
            _Resp({"ok": 1})])
        self.assertEqual(result, {"ok": 1})
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.sleeps, [2, 5])

    def test_retries_exhausted_raise_transient(self):
        result, calls = self.run_http([http_error(503, GEMINI_503) for _ in range(6)])
        self.assertIsInstance(result, nova.TransientModelError)
        self.assertIsInstance(result, RuntimeError)   # old handlers still catch it
        self.assertIn("UNAVAILABLE", str(result))
        self.assertEqual(len(calls), 4)               # 1 try + 3 retries
        self.assertEqual(self.sleeps, [2, 5, 10])

    def test_hard_error_fails_immediately(self):
        result, calls = self.run_http([http_error(404, GEMINI_404)])
        self.assertIsInstance(result, RuntimeError)
        self.assertNotIsInstance(result, nova.TransientModelError)
        self.assertIn("Model API error 404", str(result))
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.sleeps, [])

    def test_network_error_not_retried(self):
        result, calls = self.run_http(
            [urllib.error.URLError("no route") for _ in range(3)])
        self.assertIn("Could not reach the model API", str(result))
        self.assertEqual(len(calls), 1)               # fallback path unchanged
        self.assertEqual(self.sleeps, [])


class ChatOverloadTest(unittest.TestCase):
    def tearDown(self):
        nova.set_protected_paths([])

    def make_agent(self):
        return nova.Agent({"provider": "openai", "api_key": "k",
                           "model": "gemini-3.8-flash",
                           "base_url": "https://example.test/v1",
                           "fallback": {"enabled": True, "provider": "ollama",
                                        "api_key": "", "model": "", "base_url": ""}})

    def test_exhausted_overload_gives_friendly_message_not_fallback(self):
        agent = self.make_agent()

        def boom():
            raise nova.TransientModelError(
                'Model API error 503: {"error": {"status": "UNAVAILABLE"}}')
        agent._run_provider_loop = boom
        reply = agent.chat("open the downloads folder")
        self.assertIn("overloaded right now", reply)
        self.assertIn("high demand", reply)
        self.assertIn("gemini-3.5-flash", reply)
        self.assertIn("Details:", reply)              # raw error kept as a details line
        self.assertNotIn("switched to the fallback brain", reply)
        self.assertEqual(agent.provider, "openai")    # primary never swapped


class TimeoutClassificationTest(unittest.TestCase):
    """A stalled HTTPS read ('The read operation timed out') is a
    timeout, not 'vision unavailable' — the v2.3 lesson from Faizan's
    PC, where it wrongly switched the Screen Advisor OFF."""

    def test_timeout_instances_and_text(self):
        self.assertTrue(nova.is_timeout_error(
            TimeoutError("The read operation timed out")))
        self.assertTrue(nova.is_timeout_error(socket.timeout("timed out")))
        self.assertTrue(nova.is_timeout_error(
            RuntimeError("The read operation timed out")))
        self.assertTrue(nova.is_timeout_error(
            RuntimeError("Could not reach the model API: The read operation timed out")))

    def test_non_timeouts(self):
        self.assertFalse(nova.is_timeout_error(
            RuntimeError("Could not reach the model API: down")))
        self.assertFalse(nova.is_timeout_error(
            RuntimeError('Model API error 404: {"error": {"status": "NOT_FOUND"}}')))
        self.assertFalse(nova.is_timeout_error(
            nova.TransientModelError("Model API error 503: x")))
        self.assertFalse(nova.is_timeout_error(RuntimeError("No API key set.")))


class AdvisorFailureActionTest(unittest.TestCase):
    def test_timeout_failure_skips_cycle_advisor_stays_on(self):
        # timeouts are transient for the ADVISOR: skip, never disable
        self.assertEqual(nova.advisor_failure_action(
            TimeoutError("The read operation timed out")), "skip")
        self.assertEqual(nova.advisor_failure_action(
            socket.timeout("timed out")), "skip")
        self.assertEqual(nova.advisor_failure_action(
            RuntimeError("The read operation timed out")), "skip")

    def test_transient_failure_skips_cycle_advisor_stays_on(self):
        self.assertEqual(nova.advisor_failure_action(
            nova.TransientModelError("Model API error 503: x")), "skip")
        self.assertEqual(nova.advisor_failure_action(
            RuntimeError('Model API error 503: {"error": {"status": "UNAVAILABLE"}}')),
            "skip")
        self.assertEqual(nova.advisor_failure_action(
            RuntimeError('Model API error 429: {"error": {"status": "RESOURCE_EXHAUSTED"}}')),
            "skip")

    def test_hard_failures_disable_advisor(self):
        for err in ('Model API error 404: {"error": {"status": "NOT_FOUND"}}',
                    "Model API error 401: bad key",
                    "Model API error 403: forbidden",
                    "Model API error 400: image content not supported",
                    "Pillow is not installed, screenshots unavailable."):
            self.assertEqual(nova.advisor_failure_action(RuntimeError(err)),
                             "disable", err)

    def test_network_failures_skip_cycle_advisor_stays_on(self):
        # v2.4: network-level failures (incl. the SSL EOF from Faizan's
        # screenshot) are 'skip', NOT 'vision unavailable, switch off'.
        # NOTE: "Could not reach the model API: down" used to be in the
        # disable list above — the v2.4 policy deliberately moves ALL
        # network-level failures to 'skip'.
        self.assertEqual(nova.advisor_failure_action(
            urllib.error.URLError("EOF occurred in violation of protocol "
                                   "(_ssl.c:2427)")), "skip")
        self.assertEqual(nova.advisor_failure_action(RuntimeError(
            "Could not reach the model API: EOF occurred in violation of "
            "protocol (_ssl.c:2427)")), "skip")
        self.assertEqual(nova.advisor_failure_action(RuntimeError(
            "Could not reach the model API: [Errno 111] Connection refused")), "skip")
        self.assertEqual(nova.advisor_failure_action(RuntimeError(
            "Could not reach the model API: down")), "skip")
        self.assertEqual(nova.advisor_failure_action(RuntimeError(
            "Could not reach the model API: [SSL: CERTIFICATE_VERIFY_FAILED] "
            "certificate verify failed")), "skip")

    def test_quota_failure_skips_cycle_advisor_stays_on(self):
        # Daily quota finished: the advisor can't get a vision answer
        # right now, but vision itself is fine — skip, don't disable.
        self.assertEqual(nova.advisor_failure_action(quota_runtime_error()), "skip")


class AdvisorVisionRetryTest(unittest.TestCase):
    """End-to-end through screen_suggestion with urlopen stubbed:
    a 503 spike is retried and a later success is returned; persistent
    503 surfaces as TransientModelError (advisor will skip, not die)."""

    def setUp(self):
        self.sleeps = []
        self._orig_sleep = nova._sleep
        nova._sleep = self.sleeps.append
        self.agent = nova.Agent({"provider": "openai", "api_key": "k",
                                 "model": "gemini-3.8-flash",
                                 "base_url": "https://example.test/v1"})

    def tearDown(self):
        nova._sleep = self._orig_sleep
        nova.set_protected_paths([])

    def run_vision(self, effects):
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req)
            eff = effects[len(calls) - 1]
            if isinstance(eff, Exception):
                raise eff
            return eff

        ok = {"choices": [{"message": {"content": "Reply to that email."}}]}
        with mock.patch.object(nova.urllib.request, "urlopen", fake_urlopen):
            try:
                return self.agent.screen_suggestion("FAKEB64"), calls
            except Exception as e:  # noqa: BLE001 — returned for assertions
                return e, calls

    def test_vision_recovers_after_503(self):
        out, calls = self.run_vision(
            [http_error(503, GEMINI_503),
             _Resp({"choices": [{"message": {"content": "Reply to that email."}}]})])
        self.assertEqual(out, "Reply to that email.")
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.sleeps, [2])
        self.assertEqual(self.agent.history, [])      # advisor still history-free

    def test_vision_persistent_503_is_transient_not_hard(self):
        out, calls = self.run_vision([http_error(503, GEMINI_503) for _ in range(6)])
        self.assertIsInstance(out, nova.TransientModelError)
        self.assertEqual(nova.advisor_failure_action(out), "skip")
        self.assertEqual(len(calls), 4)

    def test_vision_404_is_hard_failure(self):
        out, calls = self.run_vision([http_error(404, GEMINI_404)])
        self.assertIsInstance(out, RuntimeError)
        self.assertNotIsInstance(out, nova.TransientModelError)
        self.assertEqual(nova.advisor_failure_action(out), "disable")
        self.assertEqual(len(calls), 1)

    def test_vision_read_timeout_skips_and_is_not_retried(self):
        # A stalled read surfaces as a raw TimeoutError (the HTTPS layer
        # raises it mid-body). It must NOT be retried by _http_json —
        # chat's network-error behaviour stays immediate-fallback — and
        # the advisor must treat it as 'skip this cycle', not 'disable'.
        out, calls = self.run_vision(
            [TimeoutError("The read operation timed out") for _ in range(3)])
        self.assertIsInstance(out, TimeoutError)
        self.assertEqual(nova.advisor_failure_action(out), "skip")
        self.assertEqual(len(calls), 1)               # no retry on timeouts
        self.assertEqual(self.sleeps, [])

    def test_vision_call_uses_the_longer_vision_timeout(self):
        timeouts = []

        def fake_urlopen(req, timeout=None):
            timeouts.append(timeout)
            return _Resp({"choices": [{"message": {"content": "Save the file."}}]})

        with mock.patch.object(nova.urllib.request, "urlopen", fake_urlopen):
            out = self.agent.screen_suggestion("FAKEB64")
        self.assertEqual(out, "Save the file.")
        self.assertEqual(timeouts, [nova.VISION_HTTP_TIMEOUT])
        self.assertGreater(nova.VISION_HTTP_TIMEOUT, 180)   # chat's default


class QuotaClassificationTest(unittest.TestCase):
    """Daily-quota exhaustion (429 + 'per day' body) is its own kind of
    failure: is_quota_error yes, is_transient_error NO — even though it
    arrives wearing the transient costume (429 + RESOURCE_EXHAUSTED)."""

    def test_daily_quota_body_is_quota_not_transient(self):
        err = quota_runtime_error()
        self.assertTrue(nova.is_quota_error(err))
        self.assertFalse(nova.is_transient_error(err))
        typed = nova.TransientModelError(str(err))   # as _http_json raises it
        self.assertTrue(nova.is_quota_error(typed))
        self.assertFalse(nova.is_transient_error(typed))

    def test_quota_signals_individually(self):
        self.assertTrue(nova.is_quota_error(
            RuntimeError('Model API error 429: {"error": {"message": '
                         '"Generate content requests per day limit exceeded"}}')))
        self.assertTrue(nova.is_quota_error(
            RuntimeError("Model API error 429: Please retry in 15h22m26.3s")))
        self.assertTrue(nova.is_quota_error(
            RuntimeError("Quota exceeded for metric: generativelanguage."
                         "googleapis.com/generate_content_free_tier_requests")))

    def test_plain_503_is_transient_not_quota(self):
        err = RuntimeError("Model API error 503: " + json.dumps(GEMINI_503))
        self.assertFalse(nova.is_quota_error(err))
        self.assertTrue(nova.is_transient_error(err))

    def test_per_minute_429_is_transient_not_quota(self):
        # Same code + status as the daily error, but the metric is the
        # PER-MINUTE one and the retry delay is seconds: this is a rate
        # limit that clears quickly, so it stays on the transient path.
        err = RuntimeError("Model API error 429: " + json.dumps(GEMINI_429_PER_MIN))
        self.assertFalse(nova.is_quota_error(err))
        self.assertTrue(nova.is_transient_error(err))

    def test_bare_resource_exhausted_is_not_quota(self):
        err = RuntimeError('Model API error 400: {"error": {"status": "RESOURCE_EXHAUSTED"}}')
        self.assertFalse(nova.is_quota_error(err))
        self.assertFalse(nova.is_quota_error(RuntimeError("No API key set.")))
        self.assertFalse(nova.is_quota_error(
            RuntimeError("Could not reach the model API: down")))


class QuotaRetryTest(unittest.TestCase):
    """_http_json policy: a daily-quota 429 is raised at once (no
    backoff — waiting seconds can't refill it); a per-minute 429 gets
    exactly ONE short retry, unlike the 3 retries a 500/503 gets."""

    def setUp(self):
        self.sleeps = []
        self._orig_sleep = nova._sleep
        nova._sleep = self.sleeps.append

    def tearDown(self):
        nova._sleep = self._orig_sleep
        nova.set_protected_paths([])

    def run_http(self, effects):
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req)
            eff = effects[len(calls) - 1]
            if isinstance(eff, Exception):
                raise eff
            return eff

        with mock.patch.object(nova.urllib.request, "urlopen", fake_urlopen):
            try:
                return nova._http_json("https://example.test/v1", {}, {"x": 1}), calls
            except Exception as e:  # noqa: BLE001 — returned for assertions
                return e, calls

    def test_quota_429_never_retried(self):
        result, calls = self.run_http(
            [http_error(429, GEMINI_429_QUOTA), _Resp({"ok": True})])
        self.assertIsInstance(result, nova.TransientModelError)
        self.assertTrue(nova.is_quota_error(result))
        self.assertEqual(len(calls), 1)               # raised immediately
        self.assertEqual(self.sleeps, [])

    def test_per_minute_429_gets_one_short_retry(self):
        result, calls = self.run_http(
            [http_error(429, GEMINI_429_PER_MIN), _Resp({"ok": 1})])
        self.assertEqual(result, {"ok": 1})
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.sleeps, [2])

    def test_per_minute_429_stops_after_one_retry(self):
        result, calls = self.run_http(
            [http_error(429, GEMINI_429_PER_MIN) for _ in range(5)])
        self.assertIsInstance(result, nova.TransientModelError)
        self.assertFalse(nova.is_quota_error(result))
        self.assertEqual(len(calls), 2)               # 1 try + 1 retry only
        self.assertEqual(self.sleeps, [2])


class ChatQuotaTest(unittest.TestCase):
    """chat() on daily-quota exhaustion: same one-shot fallback switch
    as an unreachable brain (with its own notice), or the plain quota
    message when no fallback applies — never the 503 overload text."""

    def tearDown(self):
        nova.set_protected_paths([])

    def make_agent(self, **fb_over):
        fb = {"enabled": True, "provider": "ollama", "api_key": "",
              "model": "", "base_url": ""}
        fb.update(fb_over)
        return nova.Agent({"provider": "openai", "api_key": "k",
                           "model": "gemini-3.5-flash",
                           "base_url": "https://example.test/v1",
                           "fallback": fb})

    def quota_boom(self, agent, fallback_reply="fallback reply"):
        def fake():
            if agent.provider == "ollama":
                return fallback_reply
            raise quota_runtime_error()
        return fake

    def test_quota_falls_back_and_restores_primary(self):
        agent = self.make_agent()
        agent._run_provider_loop = self.quota_boom(agent)
        reply = agent.chat("open download folder")
        self.assertIn("Main brain's daily quota is finished", reply)
        self.assertIn("switched to your fallback brain (ollama)", reply)
        self.assertIn("fallback reply", reply)
        self.assertNotIn("overloaded right now", reply)
        self.assertEqual(agent.provider, "openai")    # primary restored

    def test_quota_without_fallback_gives_quota_message(self):
        agent = self.make_agent(enabled=False)
        agent._run_provider_loop = self.quota_boom(agent)
        reply = agent.chat("open download folder")
        self.assertIn("free daily quota is finished", reply)
        self.assertIn("resets on Google's timer", reply)
        self.assertIn("Details:", reply)              # raw error kept as details
        self.assertNotIn("overloaded right now", reply)
        self.assertNotIn("switched to your fallback brain", reply)

    def test_ollama_fallback_failure_gives_install_or_groq_fix(self):
        agent = self.make_agent()
        def fake():
            raise RuntimeError("Could not reach the model API: connection refused")
        agent._run_provider_loop = fake
        reply = agent.chat("hello")
        self.assertIn("Ollama does not look installed or running", reply)
        self.assertIn("ollama pull llama3.1", reply)
        self.assertIn("Groq", reply)
        self.assertIn("llama-3.3-70b-versatile", reply)

    def test_quota_with_same_provider_fallback_gives_quota_message(self):
        agent = self.make_agent(provider="openai")    # fallback == primary
        def fake():
            raise quota_runtime_error()
        agent._run_provider_loop = fake
        reply = agent.chat("open download folder")
        self.assertIn("free daily quota is finished", reply)
        self.assertNotIn("switched to your fallback brain", reply)
        self.assertEqual(agent.provider, "openai")


class UserAgentTest(unittest.TestCase):
    """Model API calls must not send Python-urllib's default
    User-Agent: Cloudflare (in front of Groq) blocks that signature
    with 403 error code 1010 before the request reaches the API, so
    a correct key still fails. _http_json must send JARVIS/1.0."""

    def _capture_ua(self, headers):
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req)
            return _Resp({"ok": True})

        with mock.patch.object(nova.urllib.request, "urlopen", fake_urlopen):
            nova._http_json_once("https://example.test/v1", headers, {"x": 1})
        req = calls[0]
        merged = {}
        merged.update(getattr(req, "headers", {}))
        merged.update(getattr(req, "unredirected_hdrs", {}))
        for k, v in merged.items():
            if k.lower() == "user-agent":
                return v
        return None

    def test_default_user_agent_is_jarvis_not_python_urllib(self):
        ua = self._capture_ua({})
        self.assertEqual(ua, "JARVIS/1.0")
        self.assertNotIn("Python-urllib", ua)

    def test_caller_supplied_user_agent_is_preserved(self):
        ua = self._capture_ua({"User-Agent": "Custom/9.9"})
        self.assertEqual(ua, "Custom/9.9")


if __name__ == "__main__":
    unittest.main(verbosity=2)
