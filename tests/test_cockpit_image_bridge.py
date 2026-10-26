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
from cockpit_image_bridge import CockpitImageBridge, read_cockpit_key, _known_channel_error
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
        self.bridge = CockpitImageBridge(parameters_file=self.parameters, key_provider=lambda: "private-test-key", transport=self.transport,
                                         retry_delays=(0, 0))

    def save_parameters(self):
        self.parameters.write_text(json.dumps(self.document, ensure_ascii=False), encoding="utf-8")

    def transport(self, method, url, headers, payload, timeout):
        self.calls.append((method, url, headers, json.loads(payload), timeout))
        return self.status, {}, json.dumps(self.response).encode()

    def run_image(self, on_progress=None, **kwargs):
        return json.loads(self.bridge.run("Keep the knife; add the supplied English words.", cwd=self.job,
                          images=[self.base], cancel_event=self.cancel, on_progress=on_progress or (lambda text: None), **kwargs))

    def test_edit_request_uses_verified_fields_and_preserves_original_result_bytes(self):
        result = self.run_image()
        self.assertEqual(len(self.calls), 1)
        method, url, headers, body, _ = self.calls[0]
        self.assertEqual((method, url), ("POST", "http://127.0.0.1:12359/v1/images/edits"))
        self.assertEqual(headers["Authorization"], "Bearer private-test-key")
        self.assertEqual((body["model"], body["size"], body["n"], body["stream"]), ("gpt-image-2.5", "1152x2048", 1, False))
        self.assertEqual((body["quality"], body["output_format"], body["output_compression"]), ("low", "webp", 85))
        self.assertTrue(body["images"][0]["image_url"].startswith("data:image/png;base64,"))
        self.assertNotIn("mask", body)
        self.assertNotIn("input_fidelity", body)
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

    def make_mask(self, path=None, *, mode="RGBA", size=(16, 24), alpha=255):
        path = path or self.job / "mask.png"
        image = Image.new(mode, size, (0, 0, 0, alpha) if mode == "RGBA" else 0)
        if mode == "RGBA" and alpha == 255:
            image.putpixel((0, 0), (0, 0, 0, 0))
            image.putpixel((1, 0), (0, 0, 0, 128))
        image.save(path, format="PNG")
        return path

    def test_subject_locked_mask_uses_native_json_alpha_and_requests_lossless_png(self):
        mask = self.make_mask()
        (self.job / "input.json").write_text(json.dumps({"aspect": "9:16", "workflow": "subject_locked"}))
        notes = []
        result = self.run_image(mask=mask.name, on_progress=notes.append)
        body = self.calls[0][3]
        self.assertEqual(body["output_format"], "png")
        self.assertNotIn("output_compression", body)
        self.assertNotIn("input_fidelity", body)
        self.assertEqual(body["size"], "1152x2048")
        self.assertEqual(len(body["images"]), 1)
        raw = base64.b64decode(body["mask"]["image_url"].split(",", 1)[1])
        with Image.open(io.BytesIO(raw)) as encoded:
            self.assertEqual((encoded.format, encoded.mode, encoded.size), ("PNG", "RGBA", (16, 24)))
            self.assertEqual(encoded.getpixel((0, 0))[3], 0)
            self.assertEqual(encoded.getpixel((1, 0))[3], 128)
            self.assertEqual(encoded.getpixel((15, 23))[3], 255)
        for secret in ("private-test-key", str(mask), "do-not-send"):
            self.assertNotIn(secret, json.dumps(result) + " ".join(notes))
        self.assertNotIn("private-test-key", json.dumps(body))

    def test_subject_locked_nested_requirement_requests_png_and_matching_three_by_four_size(self):
        mask = self.make_mask()
        (self.job / "input.json").write_text(json.dumps({"aspect": "3:4", "requirements": {"workflow": "subject_locked"}}))
        self.run_image(mask=mask)
        body = self.calls[0][3]
        self.assertEqual((body["output_format"], body["size"]), ("png", "1536x2048"))
        self.assertNotIn("output_compression", body)

    def test_subject_locked_without_gateway_mask_still_requests_png_and_canvas_size(self):
        for aspect, size in (("3:4", "1536x2048"), ("9:16", "1152x2048")):
            with self.subTest(aspect=aspect):
                self.calls.clear()
                (self.job / "input.json").write_text(json.dumps({"aspect": aspect, "workflow": "subject_locked"}))
                self.run_image()
                body = self.calls[0][3]
                self.assertEqual((body["output_format"], body["size"]), ("png", size))
                self.assertNotIn("mask", body)
                self.assertNotIn("output_compression", body)
                self.assertNotIn("input_fidelity", body)

    def test_three_by_four_size_does_not_depend_on_historical_tested_values(self):
        self.document["已实测请求参数"]["size"]["tested_values"] = []
        self.save_parameters()
        (self.job / "input.json").write_text(json.dumps({"aspect": "3:4"}))
        self.run_image()
        body = self.calls[0][3]
        self.assertEqual(body["size"], "1536x2048")
        self.assertEqual((body["output_format"], body["output_compression"]), ("webp", 85))

    def test_mask_without_subject_locked_workflow_keeps_existing_output_preferences(self):
        self.run_image(mask=self.make_mask())
        body = self.calls[0][3]
        self.assertEqual((body["output_format"], body["output_compression"]), ("webp", 85))

    def test_invalid_masks_do_not_reach_provider_or_expose_private_paths(self):
        candidates = [self.make_mask(self.root / "private-test-key-outside.png"),
                      self.make_mask(self.job / "rgb.png", mode="RGB"),
                      self.make_mask(self.job / "wrong-size.png", size=(16, 23)),
                      self.make_mask(self.job / "transparent.png", alpha=0),
                      self.job / "missing.png", [], {"path": "private-test-key"}]
        opaque = self.job / "opaque.png"
        Image.new("RGBA", (16, 24), (0, 0, 0, 255)).save(opaque)
        candidates.append(opaque)
        disguised = self.job / "disguised.png"
        Image.new("RGB", (16, 24), "black").save(disguised, format="JPEG")
        candidates.append(disguised)
        for mask in candidates:
            with self.subTest(mask=mask), self.assertRaises(BridgeError) as raised:
                self.run_image(mask=mask)
            self.assertNotIn("private-test-key", str(raised.exception))
            self.assertNotIn(str(self.root), str(raised.exception))
        self.assertEqual(self.calls, [])

    def test_rejected_requests_are_not_retried_and_passing_trouble_is_retried_twice(self):
        for status, expected in [(400, 1), (401, 1), (403, 1), (429, 3), (500, 3), (502, 3), (503, 3)]:
            with self.subTest(status=status):
                self.calls.clear()
                self.status = status
                self.response = {"error": {"message": "private-test-key internal credentials"}}
                with self.assertRaises(BridgeError) as raised:
                    self.run_image()
                self.assertNotIn("private-test-key", str(raised.exception))
                self.assertEqual(len(self.calls), expected)
                if expected > 1:
                    self.assertIn("已自动重试 2 次仍未成功", str(raised.exception))
        self.assertFalse((self.job / "render.webp").exists())

    def test_unsupported_model_in_nested_detail_explains_cockpit_setting_without_retry(self):
        detail = "The 'gpt-5.5' model is not supported when using Codex with a ChatGPT account."
        sensitive = "private-test-key C:\\private\\account.json private@example.test"
        for status, error in [
            (400, {"message": "Error #01: " + json.dumps({"detail": detail, "debug": sensitive})}),
            (503, {"message": {"detail": detail}, "debug": sensitive}),
        ]:
            with self.subTest(status=status):
                self.calls.clear()
                self.status, self.response = status, {"error": error}
                notes = []
                with self.assertRaises(BridgeError) as raised:
                    self.run_image(on_progress=notes.append)
                message = str(raised.exception)
                self.assertIn("图片编排模型不受当前 ChatGPT 账号支持", message)
                self.assertIn("在 Cockpit 中调整“生图主模型”", message)
                self.assertIn("已停止自动重试", message)
                self.assertEqual(len(self.calls), 1)
                shown = message + " ".join(notes)
                for secret in ("private-test-key", "account.json", "private@example.test", "Error #01", detail):
                    self.assertNotIn(secret, shown)
                self.assertFalse(any("秒后自动重试" in note for note in notes))
        self.assertFalse((self.job / "render.webp").exists())

    def test_account_pool_unavailable_stops_retry_and_only_displays_safe_guidance(self):
        sensitive = "private-test-key C:\\private\\account.json private@example.test"
        for error in [
            {"message": "auth_unavailable: 账号池没有可用账号 " + sensitive},
            {"message": "Error #01: auth_unavailable: 账号池没有可用账号 " + sensitive},
            {"code": "auth_unavailable", "message": sensitive},
        ]:
            with self.subTest(error=error):
                self.calls.clear()
                self.status, self.response = 503, {"error": error}
                notes = []
                with self.assertRaises(BridgeError) as raised:
                    self.run_image(on_progress=notes.append)
                message = str(raised.exception)
                self.assertEqual(message, "Cockpit 账号池不可用，请在 Cockpit 中检查账号池诊断。已停止自动重试。")
                self.assertEqual(len(self.calls), 1)
                for secret in ("private-test-key", "account.json", "private@example.test", "auth_unavailable"):
                    self.assertNotIn(secret, message + " ".join(notes))
                self.assertFalse(any("秒后自动重试" in note for note in notes))
        self.assertFalse((self.job / "render.webp").exists())

    def test_unrelated_upstream_text_does_not_classify_as_deterministic_channel_failure(self):
        for response in [
            {"error": {"message": "auth_unavailable_suffix: image worker busy"}},
            {"error": {"message": "The 'gpt-5.5' model is not supported."}},
            {"error": {"message": "Do not treat auth_unavailable as a code."}},
            {"error": {"message": "temporary failure"}, "detail": "auth_unavailable: unrelated field"},
        ]:
            with self.subTest(response=response):
                self.calls.clear()
                self.status, self.response = 503, response
                with self.assertRaises(BridgeError) as raised:
                    self.run_image()
                self.assertIn("已自动重试 2 次仍未成功", str(raised.exception))
                self.assertEqual(len(self.calls), 3)

    def test_transient_503_and_429_still_retry_and_save_later_success(self):
        for status in (503, 429):
            with self.subTest(status=status):
                self.calls.clear()
                def transient(*args):
                    if not self.calls:
                        self.calls.append(("temporary",))
                        return status, {}, b'{"error":{"message":"temporary failure"}}'
                    return self.transport(*args)
                self.bridge.transport = transient
                result = self.run_image()
                self.assertEqual(len(self.calls), 2)
                self.assertEqual((self.job / result["file"]).read_bytes(), self.image)

    def test_deep_error_json_falls_back_to_regular_http_error_handling(self):
        nested = "[" * 5000 + "0" + "]" * 5000
        self.assertIsNone(_known_channel_error(nested.encode()))
        wrapped = json.dumps({"error": {"message": "Error #01: " + nested}}).encode()
        self.assertIsNone(_known_channel_error(wrapped))

    def test_unusable_responses_are_retried_and_never_leave_an_image(self):
        for response in [{"data": []}, {"data": [{"b64_json": "bad-base64"}]}, {"data": [{"url": "data:image/png"}]}]:
            with self.subTest(response=response):
                self.calls.clear()
                self.response = response
                with self.assertRaises(BridgeError):
                    self.run_image()
                self.assertEqual(len(self.calls), 3)
                self.assertFalse((self.job / "render.webp").exists())

    def test_dropped_connection_is_retried_and_the_later_image_is_saved(self):
        notes = []
        def flaky(*args):
            if not self.calls:
                self.calls.append(("dropped",))
                raise ConnectionResetError("reset by peer")
            return self.transport(*args)
        self.bridge.transport = flaky
        result = json.loads(self.bridge.run("Keep the knife.", cwd=self.job, images=[self.base], cancel_event=self.cancel,
                                            on_progress=notes.append))
        self.assertEqual(result["file"], "render.webp")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual((self.job / "render.webp").read_bytes(), self.image)
        self.assertTrue(any("自动重试（第 2/3 次）" in note for note in notes))

    def test_cancel_during_the_retry_pause_stops_without_another_request(self):
        self.bridge.retry_delays = (5, 5)
        self.status = 503
        worker_errors = []
        def call():
            try:
                self.run_image()
            except Exception as error:
                worker_errors.append(error)
        worker = threading.Thread(target=call)
        worker.start()
        deadline = time.monotonic() + 2
        while not self.calls and time.monotonic() < deadline:
            time.sleep(.01)
        time.sleep(.1)
        self.cancel.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertIsInstance(worker_errors[0], BridgeCancelled)
        self.assertEqual(len(self.calls), 1)

    def test_five_requests_reach_gateway_before_any_image_finishes(self):
        guard, release, all_arrived = threading.Lock(), threading.Event(), threading.Event()
        active, peak, arrived = 0, 0, 0
        failures = []
        def counted(*args):
            nonlocal active, peak, arrived
            with guard:
                active += 1
                arrived += 1
                peak = max(peak, active)
                if arrived == 5:
                    all_arrived.set()
            if not release.wait(10):
                raise TimeoutError("The test did not release the five gateway requests.")
            with guard:
                active -= 1
            return self.transport(*args)
        self.bridge.transport = counted
        jobs = []
        for number in range(5):
            folder = self.job / f"{number:02d}"
            folder.mkdir()
            Image.new("RGB", (16, 24), "red").save(folder / "base.jpg")
            jobs.append(folder)
        def render(folder):
            try:
                self.bridge.run("Keep the knife.", cwd=folder, images=[folder / "base.jpg"],
                                cancel_event=self.cancel, on_progress=lambda text: None)
            except Exception as error:
                with guard:
                    failures.append(error)
        workers = [threading.Thread(target=render, args=(folder,)) for folder in jobs]
        try:
            for worker in workers:
                worker.start()
            self.assertTrue(all_arrived.wait(5), "All five requests must start before the first image finishes.")
            with guard:
                self.assertEqual((active, peak, arrived), (5, 5, 5))
            self.assertFalse(any((folder / "render.webp").exists() for folder in jobs))
        finally:
            release.set()
            for worker in workers:
                if worker.ident is not None:
                    worker.join(5)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(failures, [])
        self.assertEqual(len(self.calls), 5)
        self.assertTrue(all((folder / "render.webp").is_file() for folder in jobs))

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
