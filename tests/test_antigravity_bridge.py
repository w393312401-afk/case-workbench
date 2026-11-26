"""HTTP model integration without real network calls or business-data writes."""
import base64
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest.mock import patch

from antigravity_bridge import (AntigravityBridge, DEFAULT_MODEL, MODEL_PREFIX, configured_models,
                                normalize_settings, select_model)
from codex_bridge import BridgeCancelled, BridgeError


KEY = "sk-test-secret-never-returned"
OPUS_MODEL = "claude-opus-5-5-medium"


class Response(io.BytesIO):
    def __init__(self, data, content_type="text/event-stream; charset=utf-8"):
        super().__init__(data)
        self.headers = {"Content-Type": content_type}


class Opener:
    def __init__(self, response):
        self.response, self.requests = response, []

    def open(self, request, timeout):
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def stream(*chunks, done=True):
    text = "".join("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n" for chunk in chunks)
    return Response((text + ("data: [DONE]\n\n" if done else "")).encode("utf-8"))


def chunk(text=None, finish=None, **extra):
    return {"choices": [{"delta": {} if text is None else {"content": text}, "finish_reason": finish}], **extra}


class AntigravityBridgeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.settings = {"enabled": True}
        self.events = []

    def bridge(self, response, **options):
        self.opener = Opener(response)
        bridge = AntigravityBridge(settings_reader=lambda: dict(self.settings), key_reader=lambda: KEY,
                                   opener=self.opener, **options)
        self.addCleanup(bridge.close)
        return bridge

    def run_model(self, bridge, **options):
        return bridge.run("Write a candidate.", cwd=self.root, on_progress=lambda text: None,
                          cancel_event=options.pop("cancel_event", threading.Event()),
                          on_event=self.events.append, **options)

    def test_streaming_preserves_unicode_and_metadata_without_secret(self):
        bridge = self.bridge(stream(chunk("你好"), chunk("，world"), chunk(finish="stop"),
                                    {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 5, "total_tokens": 14}}))
        self.assertEqual(self.run_model(bridge, model=MODEL_PREFIX + DEFAULT_MODEL), "你好，world")
        request = self.opener.requests[0]
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], DEFAULT_MODEL)
        self.assertEqual(request.full_url, "http://127.0.0.1:8046/v1/chat/completions")
        self.assertTrue(payload["stream"])
        self.assertEqual(request.get_header("Authorization"), "Bearer " + KEY)
        self.assertEqual(next(event for event in self.events if event["type"] == "session")["model"], MODEL_PREFIX + DEFAULT_MODEL)
        usage = next(event for event in self.events if event["type"] == "usage")
        self.assertEqual((usage["input_tokens"], usage["output_tokens"], usage["total_tokens"]), (9, 5, 14))
        self.assertTrue(self.events[-1]["complete"])
        self.assertNotIn(KEY, repr(self.events) + repr(bridge.status()) + repr(bridge.models()))

    def test_json_response_and_explicit_check(self):
        response = Response(json.dumps({"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]}).encode(),
                            "application/json")
        bridge = self.bridge(response)
        self.assertTrue(bridge.check_connection()["ok"])
        self.assertEqual(json.loads(self.opener.requests[0].data)["max_tokens"], 32)

    def test_configured_alternative_is_requested_and_default_remains_sonnet(self):
        self.settings.update(model=DEFAULT_MODEL, models=[OPUS_MODEL])
        for selected, expected in ((None, DEFAULT_MODEL), (MODEL_PREFIX + DEFAULT_MODEL, DEFAULT_MODEL),
                                   (MODEL_PREFIX + OPUS_MODEL, OPUS_MODEL)):
            with self.subTest(selected=selected):
                self.events.clear()
                bridge = self.bridge(stream(chunk("candidate", "stop"),
                                            {"choices": [], "usage": {"prompt_tokens": 4, "completion_tokens": 2}}))
                self.assertEqual(self.run_model(bridge, model=selected), "candidate")
                payload = json.loads(self.opener.requests[0].data)
                self.assertEqual(payload["model"], expected)
                session = next(event for event in self.events if event["type"] == "session")
                usage = next(event for event in self.events if event["type"] == "usage")
                self.assertEqual(session["model"], MODEL_PREFIX + expected)
                self.assertEqual(usage["models"], [expected])
                self.assertEqual(bridge.status()["default_model"], MODEL_PREFIX + DEFAULT_MODEL)
                self.assertEqual(self.settings["model"], DEFAULT_MODEL)

    def test_catalog_keeps_default_and_all_configured_alternatives_once(self):
        self.settings.update(model=DEFAULT_MODEL, models=[OPUS_MODEL, DEFAULT_MODEL, OPUS_MODEL])
        bridge = self.bridge(stream(chunk("unused", "stop")))
        rows = bridge.models()["models"]
        self.assertEqual([row["id"] for row in rows], [MODEL_PREFIX + DEFAULT_MODEL, MODEL_PREFIX + OPUS_MODEL])
        self.assertTrue(all(row["available"] for row in rows))
        self.assertEqual(self.opener.requests, [])

    def test_unknown_or_unscoped_model_does_not_fall_back_to_either_configured_model(self):
        self.settings.update(model=DEFAULT_MODEL, models=[OPUS_MODEL])
        bridge = self.bridge(stream(chunk("unused", "stop")))
        for selected in (OPUS_MODEL, "", "antigravity:unknown-model", "claude-opus", 12):
            with self.subTest(selected=selected), self.assertRaises(BridgeError):
                self.run_model(bridge, model=selected)
        self.assertEqual(self.opener.requests, [])

    def test_default_request_uses_one_configuration_snapshot_for_endpoint_and_key(self):
        snapshots = [
            {"antigravity": {"enabled": True, "base_url": "http://127.0.0.1:8046/v1"},
             "antigravity_api_key": "sk-old-service-key"},
            {"antigravity": {"enabled": True, "base_url": "http://127.0.0.1:8047/v1"},
             "antigravity_api_key": "sk-new-service-key"},
        ]
        opener = Opener(stream(chunk("candidate", "stop")))
        bridge = AntigravityBridge(opener=opener)
        self.addCleanup(bridge.close)
        with patch("antigravity_bridge.load_config", side_effect=snapshots) as reader, \
                patch("api_keys._local_key", side_effect=AssertionError("unexpected separate credential read")):
            self.assertEqual(self.run_model(bridge), "candidate")
            reader.assert_called_once_with()
        request = opener.requests[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8046/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer sk-old-service-key")

    def test_connection_check_preserves_default_configuration_snapshots(self):
        snapshots = [
            {"antigravity": {"enabled": True}, "antigravity_api_key": "sk-old-service-key"},
            {"antigravity": {"enabled": True, "base_url": "http://127.0.0.1:8047/v1", "model": "replacement-model"},
             "antigravity_api_key": "sk-new-service-key"},
        ]
        opener = Opener(stream(chunk("OK", "stop")))
        bridge = AntigravityBridge(opener=opener)
        self.addCleanup(bridge.close)
        with patch("antigravity_bridge.load_config", side_effect=snapshots) as reader, \
                patch("api_keys._local_key", side_effect=AssertionError("unexpected separate credential read")):
            self.assertTrue(bridge.check_connection()["ok"])
            self.assertEqual(reader.call_count, 2)
        request = opener.requests[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8047/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer sk-new-service-key")
        self.assertEqual(json.loads(request.data)["model"], "replacement-model")

    def test_missing_end_truncation_empty_and_tool_calls_are_not_complete(self):
        responses = [stream(chunk("partial"), done=False), stream(chunk("partial", "length")),
                     stream(chunk(finish="stop")),
                     stream({"choices": [{"delta": {"tool_calls": [{"function": {"name": "Read"}}]}, "finish_reason": None}]})]
        for response in responses:
            with self.subTest(response=response):
                self.events.clear()
                bridge = self.bridge(response)
                with self.assertRaises(BridgeError):
                    self.run_model(bridge)
                self.assertFalse(any(event.get("complete") for event in self.events))

    def test_http_diagnostics_do_not_expose_response_or_key(self):
        error = urllib.error.HTTPError("http://localhost/v1", 401, KEY, {}, io.BytesIO(KEY.encode()))
        bridge = self.bridge(error)
        result = bridge.check_connection()
        self.assertFalse(result["ok"])
        self.assertIn("401", result["detail"])
        self.assertNotIn(KEY, repr(result))
        self.assertEqual(len(self.opener.requests), 1)

    def test_invalid_response_is_reported_without_waiting_for_deadline(self):
        bridge = self.bridge(Response(b"invalid json", "application/json"))
        started = time.monotonic()
        with self.assertRaises(BridgeError):
            self.run_model(bridge)
        self.assertLess(time.monotonic() - started, .8)
        self.assertEqual(len(self.opener.requests), 1)

    def test_deadline_stops_a_blocked_connection_without_retry(self):
        bridge = self.bridge(stream(chunk("late", "stop")), timeout=.05)
        release = threading.Event()
        original = self.opener.open

        def waiting(request, timeout):
            release.wait(2)
            return original(request, timeout)

        self.opener.open = waiting
        self.addCleanup(release.set)
        started = time.monotonic()
        with self.assertRaises(BridgeError):
            self.run_model(bridge)
        self.assertLess(time.monotonic() - started, .8)
        self.assertFalse(any(event.get("complete") for event in self.events))

    def test_connection_check_has_its_own_short_deadline(self):
        bridge = self.bridge(stream(chunk("late", "stop")), timeout=1200, check_timeout=.05)
        release = threading.Event()
        original = self.opener.open
        socket_timeouts = []

        def waiting(request, timeout):
            socket_timeouts.append(timeout)
            release.wait(2)
            return original(request, timeout)

        self.opener.open = waiting
        self.addCleanup(release.set)
        started = time.monotonic()
        self.assertFalse(bridge.check_connection()["ok"])
        self.assertLess(time.monotonic() - started, .8)
        self.assertEqual(bridge.timeout, 1200)
        self.assertEqual(socket_timeouts, [.05])

    def test_cancellation_stops_waiting_and_closes_an_open_stream(self):
        released = threading.Event()

        class WaitingResponse(Response):
            def readline(self, size=-1):
                released.wait(2)
                return b""

            def close(self):
                released.set()
                super().close()

        response = WaitingResponse(b"")
        bridge = self.bridge(response)
        cancel = threading.Event()
        timer = threading.Timer(.05, cancel.set)
        timer.start()
        self.addCleanup(timer.cancel)
        started = time.monotonic()
        with self.assertRaises(BridgeCancelled):
            self.run_model(bridge, cancel_event=cancel)
        self.assertLess(time.monotonic() - started, .8)
        self.assertTrue(released.wait(.5))
        self.assertFalse(any(event.get("complete") for event in self.events))

    def test_cancel_during_connect_returns_without_waiting_for_open(self):
        bridge = self.bridge(stream(chunk("late", "stop")))
        release = threading.Event()
        original = self.opener.open

        def waiting(request, timeout):
            release.wait(2)
            return original(request, timeout)

        self.opener.open = waiting
        self.addCleanup(release.set)
        cancel = threading.Event()
        timer = threading.Timer(.05, cancel.set)
        timer.start()
        self.addCleanup(timer.cancel)
        started = time.monotonic()
        with self.assertRaises(BridgeCancelled):
            self.run_model(bridge, cancel_event=cancel)
        self.assertLess(time.monotonic() - started, .8)

    def test_skill_and_explicit_reference_contents_are_sent(self):
        skill = self.root / "SKILL.md"
        skill.write_text("---\nname: writing\n---\nUse the exact delivery format.\n[Ignore](secret.txt)", encoding="utf-8")
        reference = self.root / "facts.md"
        reference.write_text("Confirmed product facts.", encoding="utf-8")
        bridge = self.bridge(stream(chunk("candidate", "stop")))
        self.run_model(bridge, skills=[{"name": "writing", "path": str(skill)}], references=[{"path": str(reference)}])
        system = json.loads(self.opener.requests[0].data)["messages"][0]["content"]
        self.assertIn("Use the exact delivery format.", system)
        self.assertIn("Confirmed product facts.", system)
        self.assertEqual(next(event for event in self.events if event["type"] == "skills_submitted")["skills"],
                         [{"name": "writing", "path": str(skill)}])

    def test_missing_skill_stops_before_request(self):
        bridge = self.bridge(stream(chunk("candidate", "stop")))
        with self.assertRaises(BridgeError):
            self.run_model(bridge, skills=[{"name": "writing", "path": str(self.root / "SKILL.md")}])
        self.assertEqual(self.opener.requests, [])

    def test_images_require_opt_in_and_attach_actual_bytes(self):
        image = self.root / "sheet.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nactual-picture")
        bridge = self.bridge(stream(chunk("candidate", "stop")))
        with self.assertRaises(BridgeError):
            self.run_model(bridge, images=[("Sheet 1", image)])
        self.assertEqual(self.opener.requests, [])
        self.settings["vision"] = True
        self.run_model(bridge, images=[("Sheet 1", image)])
        content = json.loads(self.opener.requests[0].data)["messages"][1]["content"]
        picture = next(item for item in content if item["type"] == "image_url")["image_url"]["url"]
        self.assertEqual(base64.b64decode(picture.split(",", 1)[1]), image.read_bytes())

    def test_registered_video_is_never_silently_ignored(self):
        bridge = self.bridge(stream(chunk("candidate", "stop")), frame_extractor=lambda path, event: [])
        with self.assertRaises(BridgeError):
            self.run_model(bridge, video_path="source.mp4", require_image_view=True)
        self.settings["vision"] = True
        with self.assertRaises(BridgeError):
            self.run_model(bridge, video_path="source.mp4", require_image_view=True)
        self.assertEqual(self.opener.requests, [])

    def test_status_and_catalog_do_not_contact_service(self):
        bridge = self.bridge(stream(chunk("unused", "stop")))
        self.assertTrue(bridge.status()["available"])
        row = bridge.models()["models"][0]
        self.assertEqual(row["reasoning_efforts"], [])
        self.assertEqual(row["provider"], "antigravity")
        self.assertEqual(self.opener.requests, [])
        self.settings["enabled"] = False
        self.assertFalse(bridge.status()["available"])

    def test_only_configured_provider_model_and_no_agent_profile(self):
        bridge = self.bridge(stream(chunk("unused", "stop")))
        for options in ({"model": DEFAULT_MODEL}, {"reasoning_effort": "medium"}, {"profile": {"tools": ["Read"]}}):
            with self.subTest(options=options), self.assertRaises(BridgeError):
                self.run_model(bridge, **options)
        self.assertEqual(self.opener.requests, [])

    def test_setting_validation_rejects_secret_urls_and_remote_plain_http(self):
        invalid = ["http://example.com/v1", "http://user:pass@localhost/v1", "http://localhost/v1?key=secret",
                   "http://localhost/v1#secret", "file:///tmp/api", "http://localhost:bad/v1",
                   "http://@localhost/v1", "http://localhost:0/v1"]
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(BridgeError):
                normalize_settings({"base_url": url})
        self.assertEqual(normalize_settings({"base_url": "http://[::1]:8046/v1/"})["base_url"], "http://[::1]:8046/v1")
        self.assertFalse(normalize_settings({})["enabled"])

    def test_legacy_configuration_retains_its_original_four_fields(self):
        legacy = normalize_settings({"enabled": True, "model": DEFAULT_MODEL})
        self.assertEqual(set(legacy), {"enabled", "base_url", "model", "vision"})
        self.assertEqual(configured_models(legacy), [DEFAULT_MODEL])
        self.assertEqual(select_model(legacy, None), DEFAULT_MODEL)

    def test_model_list_is_validated_normalized_and_stably_deduplicated(self):
        settings = normalize_settings({"model": DEFAULT_MODEL, "models": [OPUS_MODEL, f" {OPUS_MODEL} ", DEFAULT_MODEL]})
        self.assertEqual(settings["models"], [OPUS_MODEL, DEFAULT_MODEL])
        self.assertEqual(configured_models(settings), [DEFAULT_MODEL, OPUS_MODEL])
        self.assertEqual(select_model(settings, MODEL_PREFIX + OPUS_MODEL), OPUS_MODEL)
        self.assertEqual(normalize_settings({"models": []})["models"], [])
        self.assertEqual(len(normalize_settings({"models": [f"model-{index}" for index in range(32)]})["models"]), 32)
        for models in (None, OPUS_MODEL, (OPUS_MODEL,), [None], [""], ["   "], [12],
                       [MODEL_PREFIX + OPUS_MODEL], ["invalid model"], ["model"] * 33):
            with self.subTest(models=models), self.assertRaises(BridgeError):
                normalize_settings({"models": models})


if __name__ == "__main__":
    unittest.main()
