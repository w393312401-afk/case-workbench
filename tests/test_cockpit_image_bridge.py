"""Direct gateway requests, isolated images and credentials; no live generation."""
import base64
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from cockpit_image_bridge import CockpitImageBridge, read_cockpit_key
from codex_bridge import BridgeCancelled, BridgeError


class CockpitImageBridgeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.job = self.root / "job"
        self.job.mkdir()
        self.parameters = self.root / "parameters.json"
        self.document = {"通道": {"Cockpit本机API": {"基址": "http://127.0.0.1:12359/v1",
                         "参考图编辑": {"method": "POST", "path": "/images/edits"}}},
                         "可复用请求形式": {"本机参考图编辑JSON": {"model": "gpt-image-2.5", "size": "1024x1536",
                         "quality": "low", "background": "opaque", "output_format": "webp", "output_compression": 85}},
                         "已实测请求参数": {"size": {"tested_values": ["1024x1536", "1152x2048"]}}}
        self.save_parameters()
        self.base = self.job / "base.jpg"
        Image.new("RGB", (16, 24), "red").save(self.base)
        (self.job / "input.json").write_text(json.dumps({"aspect": "9:16", "private": "do-not-send"}))
        buffer = io.BytesIO()
        Image.new("RGB", (31, 55), "blue").save(buffer, format="WEBP")
        self.image = buffer.getvalue()
        self.calls = []
        self.response = {"data": [{"b64_json": base64.b64encode(self.image).decode(), "revised_prompt": "do-not-keep"}]}
        self.status = 200
        self.cancel = threading.Event()
        self.bridge = CockpitImageBridge(parameters_file=self.parameters, key_provider=lambda: "private-test-key", transport=self.transport)

    def save_parameters(self):
        self.parameters.write_text(json.dumps(self.document, ensure_ascii=False), encoding="utf-8")

    def transport(self, method, url, headers, payload, timeout):
        self.calls.append((method, url, headers, json.loads(payload), timeout))
        return self.status, {}, json.dumps(self.response).encode()

    def run_image(self, **kwargs):
        return json.loads(self.bridge.run("Keep the knife; add the supplied English words.", cwd=self.job,
                          images=[self.base], cancel_event=self.cancel, on_progress=lambda text: None, **kwargs))

    def test_edit_request_uses_verified_fields_and_preserves_original_result_bytes(self):
        result = self.run_image()
        self.assertEqual(len(self.calls), 1)
        method, url, headers, body, _ = self.calls[0]
        self.assertEqual((method, url), ("POST", "http://127.0.0.1:12359/v1/images/edits"))
        self.assertEqual(headers["Authorization"], "Bearer private-test-key")
        self.assertEqual((body["model"], body["size"], body["n"], body["stream"]), ("gpt-image-2.5", "1152x2048", 1, False))
        self.assertEqual((body["quality"], body["output_format"], body["output_compression"]), ("low", "webp", 85))
        self.assertTrue(body["images"][0]["image_url"].startswith("data:image/png;base64,"))
        self.assertNotIn("do-not-send", json.dumps(body))
        self.assertEqual(result, {"file": "render.webp", "summary": ""})
        self.assertEqual((self.job / result["file"]).read_bytes(), self.image)
        self.assertFalse(list(self.job.glob("*.tmp")))
        self.assertNotIn("do-not-keep", json.dumps(result))

    def test_two_references_stay_in_order(self):
        style = self.job / "style.png"
        Image.new("RGB", (8, 8), "green").save(style)
        self.bridge.run("Use the second image only for style.", cwd=self.job, images=[self.base, style],
                        cancel_event=self.cancel, on_progress=lambda text: None)
        images = self.calls[0][3]["images"]
        self.assertEqual(len(images), 2)
        self.assertNotEqual(images[0], images[1])

    def test_provider_errors_never_retry_or_expose_credentials(self):
        for status in [400, 401, 403, 429, 500]:
            with self.subTest(status=status):
                self.calls.clear()
                self.status = status
                self.response = {"error": {"message": "private-test-key internal credentials"}}
                with self.assertRaises(BridgeError) as raised:
                    self.run_image()
                self.assertNotIn("private-test-key", str(raised.exception))
                self.assertEqual(len(self.calls), 1)
        self.assertFalse((self.job / "render.webp").exists())

    def test_unusable_response_does_not_create_images_or_retry(self):
        for response in [{"data": []}, {"data": [{"b64_json": "bad-base64"}]}, {"data": [{"url": "data:image/png"}]}]:
            with self.subTest(response=response):
                self.calls.clear()
                self.response = response
                with self.assertRaises(BridgeError):
                    self.run_image()
                self.assertEqual(len(self.calls), 1)
                self.assertFalse((self.job / "render.webp").exists())

    def test_cancelled_request_never_saves_late_result_or_retries(self):
        entered, release = threading.Event(), threading.Event()
        original = self.bridge.transport
        def blocking(*args):
            entered.set()
            release.wait(2)
            return original(*args)
        self.bridge.transport = blocking
        failures = []
        def call():
            try:
                self.run_image()
            except Exception as error:
                failures.append(error)
        worker = threading.Thread(target=call)
        worker.start()
        self.assertTrue(entered.wait(1))
        self.cancel.set()
        worker.join(1)
        release.set()
        self.assertFalse(worker.is_alive())
        self.assertIsInstance(failures[0], BridgeCancelled)
        time.sleep(.05)
        self.assertLessEqual(len(self.calls), 1)
        self.assertFalse((self.job / "render.webp").exists())

    def test_timeout_never_retries(self):
        release = threading.Event()
        def slow(*args):
            release.wait(1)
            return self.transport(*args)
        self.bridge.transport = slow
        with self.assertRaises(BridgeError):
            self.run_image(timeout=.02)
        release.set()
        self.assertFalse((self.job / "render.webp").exists())

    def test_non_loopback_configuration_never_receives_local_key(self):
        self.document["通道"]["Cockpit本机API"]["基址"] = "http://example.com:12359/v1"
        self.save_parameters()
        with self.assertRaises(BridgeError):
            self.run_image()
        self.assertEqual(self.calls, [])

    def test_outside_reference_and_read_only_requests_do_not_call_provider(self):
        outside = self.root / "outside.png"
        Image.new("RGB", (4, 4)).save(outside)
        for args in [{"images": [outside]}, {"images": [self.base], "read_only": True}]:
            with self.subTest(args=args), self.assertRaises(BridgeError):
                self.bridge.run("Edit.", cwd=self.job, cancel_event=self.cancel, on_progress=lambda text: None, **args)
        self.assertEqual(self.calls, [])

    def test_progress_display_failure_does_not_lose_completed_image(self):
        def broken(text):
            raise OSError("display unavailable")
        result = json.loads(self.bridge.run("Edit.", cwd=self.job, images=[self.base], cancel_event=self.cancel, on_progress=broken))
        self.assertEqual((self.job / result["file"]).read_bytes(), self.image)

    def test_key_is_read_from_enabled_cockpit_entry_each_time(self):
        config = self.root / "auth.json"
        with patch.dict(os.environ, {"COCKPIT_API_KEY": ""}):
            config.write_text(json.dumps({"enabled": True, "apiKey": "stale", "apiKeys": [
                {"enabled": False, "key": "disabled"}, {"enabled": True, "key": "current"}]}))
            self.assertEqual(read_cockpit_key(config), "current")
            config.write_text(json.dumps({"enabled": True, "apiKeys": [{"enabled": True, "key": "rotated"}]}))
            self.assertEqual(read_cockpit_key(config), "rotated")
            config.write_text(json.dumps({"enabled": False, "apiKey": "current"}))
            self.assertEqual(read_cockpit_key(config), "")


if __name__ == "__main__":
    unittest.main()
