"""Namespaced API routing and durable task metadata use disposable data only."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from ai_tasks import AITaskError, AITaskManager, _hash
from case_store import CaseStore
from claude_bridge import ModelRouter
from codex_bridge import BridgeUnavailable


MODEL = "antigravity:claude-sonnet-5-5-medium"


class FakeBridge:
    def __init__(self, provider, model, available=True):
        self.provider, self.model, self.available = provider, model, available
        self.calls = []
        self.refreshes = self.closes = 0

    def status(self):
        return {"available": self.available, "label": self.provider,
                "error": None if self.available else f"{self.provider} 连接未就绪"}

    def models(self):
        return {"available": self.available, "error": self.status()["error"], "models": [
            {"id": self.model, "provider": self.provider, "available": self.available, "reasoning_efforts": []}]}

    def refresh_models(self):
        self.refreshes += 1

    def close(self):
        self.closes += 1

    def run(self, prompt, **options):
        self.calls.append({"prompt": prompt, **options})
        if not self.available:
            raise BridgeUnavailable(self.status()["error"])
        emit = options.get("on_event")
        if emit:
            emit({"type": "session", "model": self.model, "reasoning_effort": None})
            emit({"type": "skills_submitted", "skills": options.get("skills") or []})
            emit({"type": "usage", "input_tokens": 10, "output_tokens": 3, "total_tokens": 13,
                  "private": "not-to-be-persisted", "cost_usd": "unknown"})
        return "候选内容，等待人工核对。"


def make_router(api_available=True):
    return ModelRouter(FakeBridge("codex", "gpt-test"), FakeBridge("claude", "claude-default"),
                       FakeBridge("antigravity", MODEL, api_available))


class AntigravityRouterTests(unittest.TestCase):
    def test_namespaced_model_only_reaches_api_and_keeps_media_arguments(self):
        router = make_router()
        options = {"model": MODEL, "video_path": "registered.mp4", "images": ["registered.jpg"],
                   "skills": [{"name": "registered", "path": "SKILL.md"}], "require_image_view": True}
        self.assertEqual(router.run("prompt", **options), "候选内容，等待人工核对。")
        for key, value in options.items():
            self.assertEqual(router.antigravity.calls[-1][key], value)
        self.assertEqual(router.codex.calls, [])
        self.assertEqual(router.claude.calls, [])

    def test_unavailable_or_missing_api_never_falls_back_to_a_client(self):
        router = make_router(False)
        with self.assertRaisesRegex(BridgeUnavailable, "antigravity"):
            router.run("prompt", model=MODEL)
        self.assertEqual(router.codex.calls + router.claude.calls, [])
        router.antigravity = None
        with self.assertRaisesRegex(BridgeUnavailable, "Antigravity"):
            router.run("prompt", model=MODEL)

    def test_client_auto_order_and_legacy_two_argument_constructor_are_preserved(self):
        router = make_router()
        router.run("prompt", model=None)
        self.assertEqual(len(router.codex.calls), 1)
        router.codex.available = False
        router.run("prompt", model=None)
        self.assertEqual(len(router.claude.calls), 1)
        self.assertEqual(router.antigravity.calls, [])
        legacy = ModelRouter(router.codex, router.claude)
        self.assertNotIn("antigravity", legacy.status()["providers"])

    def test_api_only_status_models_refresh_and_close_are_independent(self):
        router = make_router()
        router.codex.available = router.claude.available = False
        self.assertTrue(router.status()["available"])
        self.assertTrue(router.status()["providers"]["antigravity"]["available"])
        self.assertIn(MODEL, [item["id"] for item in router.models()["models"]])
        router.refresh_models()
        self.assertEqual([bridge.refreshes for bridge in (router.codex, router.claude, router.antigravity)], [1, 1, 1])
        router.close()
        self.assertEqual((router.claude.closes, router.antigravity.closes), (1, 1))

    def test_model_validation_reports_the_selected_provider_error(self):
        manager = object.__new__(AITaskManager)
        manager.bridge = make_router(False)
        with self.assertRaisesRegex(AITaskError, "antigravity 连接未就绪"):
            manager._validate_model_settings(MODEL, None)
        manager.bridge.antigravity.available = True
        with self.assertRaisesRegex(AITaskError, "思考强度"):
            manager._validate_model_settings(MODEL, "medium")


class AntigravityTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="antigravity-routing-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = CaseStore(self.root / "skill")
        product_root = self.store.root / "references" / "products"
        product_root.mkdir(parents=True, exist_ok=True)
        (product_root / "source-map.json").write_text(json.dumps({"sku_to_reference": {"202H": "202H.md"}}), encoding="utf-8")
        self.product = product_root / "202H.md"
        self.product.write_text("ONLY_REGISTERED_PRODUCT_FACT", encoding="utf-8")
        self.case = self.store.add({"title": "隔离案例", "product": "SHARPAL 202H",
            "original": {"text": "Keep the original wording."}, "feedback": {"kind": "editorial", "text": "测试"},
            "current": {"title": "隔离案例", "category": "待验证", "kind": "editorial", "conclusion": "仅测试", "assessments": []}})
        self.router = make_router()
        self.manager = AITaskManager(self.store, self.root / "runtime", threading.RLock(), self.router)
        self.addCleanup(self.manager.shutdown)

    def create(self, action="review", model=MODEL):
        return self.manager.create(self.case["case_id"], {"request_id": uuid4().hex,
            "revision": _hash(self.store.read_case(self.case["case_id"])), "action": action,
            "instruction": "只处理本次输入。", "model": model, "skill_selection": "none"})

    def wait(self, identifier):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            task = self.manager.get(identifier)
            if not task["busy"]:
                return task
            time.sleep(0.01)
        self.fail("隔离任务未按时结束")

    def test_api_defaults_persist_and_variants_keep_two_calls_and_total_usage(self):
        defaults = {"model": MODEL, "reasoning_effort": None}
        self.manager.save_preferences(defaults)
        self.assertEqual(self.manager.resolve_default_settings({}), defaults)
        done = self.wait(self.create("variants")["task_id"])
        self.assertEqual(done["status"], "completed", done)
        turn = done["rounds"][-1]
        self.assertEqual(len(self.router.antigravity.calls), 2)
        self.assertEqual(turn["model"], MODEL)
        self.assertEqual(turn["usage"], {"input_tokens": 20, "output_tokens": 6, "total_tokens": 26})
        for stage in turn["stages"].values():
            self.assertEqual(stage["usage"], {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13})
        raw = self.manager._read(done["task_id"])
        self.assertEqual(raw["rounds"][-1]["usage"], turn["usage"])
        self.assertNotIn("not-to-be-persisted", json.dumps(raw))
        self.assertEqual(self.router.codex.calls + self.router.claude.calls, [])

    def test_api_receives_registered_product_body_while_native_input_keeps_paths(self):
        api = self.wait(self.create()["task_id"])
        self.assertEqual(api["status"], "completed", api)
        self.assertIn("ONLY_REGISTERED_PRODUCT_FACT", self.router.antigravity.calls[-1]["prompt"])
        native = self.wait(self.create(model="claude-default")["task_id"])
        self.assertEqual(native["status"], "completed", native)
        self.assertNotIn("ONLY_REGISTERED_PRODUCT_FACT", self.router.claude.calls[-1]["prompt"])

    def test_product_packing_refuses_paths_outside_registered_product_root(self):
        done = self.wait(self.create()["task_id"])
        raw = self.manager._read(done["task_id"])
        outside = self.root / "unregistered.md"
        outside.write_text("NOT_REGISTERED", encoding="utf-8")
        with self.assertRaisesRegex(AITaskError, "超出"):
            self.manager._prompt(raw, [{"label": "产品资料 · 202H", "path": str(outside)}])

    def test_usage_events_replace_snapshots_without_counting_them_twice(self):
        task_id = "AIT-000000000000"
        live = {"round_id": "AIR-test", "messages": [], "stages": {"draft": {}, "review": {}}}
        self.manager._live[task_id] = live
        with patch.object(self.manager, "_checkpoint_live"):
            for event in ({"type": "usage", "input_tokens": 5, "output_tokens": 2},
                          {"type": "usage", "input_tokens": 5, "output_tokens": 2},
                          {"type": "usage", "input_tokens": True, "output_tokens": -1}):
                self.manager._on_event(task_id, event, "draft")
            self.manager._on_event(task_id, {"type": "usage", "input_tokens": 3, "output_tokens": 1}, "review")
        self.assertEqual(live["usage"], {"input_tokens": 8, "output_tokens": 3})


if __name__ == "__main__":
    unittest.main()
