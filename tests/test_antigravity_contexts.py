"""Business callers explicitly supply API-only file context without changing native calls."""
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from antigravity_bridge import AntigravityBridge
from opportunity_skeletons import OpportunitySkeletons
from photo_carousel import PhotoCarousels


API_MODEL = "antigravity:claude-sonnet-5-5-medium"


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def skill_tree(root):
    skills = root / "skills"
    write(skills / "tiktok-photo-commerce" / "SKILL.md", "Photo skill")
    write(skills / "tiktok-photo-commerce" / "references" / "quality-standards.md", "Photo quality rules")
    write(skills / "tiktok-photo-commerce" / "references" / "production.md", "Unneeded rendering rules")
    write(skills / "product-video-script-writing" / "SKILL.md", "Writing skill")
    write(skills / "product-video-script-writing" / "references" / "writing-and-native.md", "Current native writing rules")
    write(skills / "product-video-script-writing" / "references" / "guidance.md", "Current writing guidance")
    write(skills / "product-video-script-writing" / "references" / "unrelated.md", "Unrelated private reference")
    return skills


def api_messages(prompt, options):
    messages, _ = AntigravityBridge()._messages(
        prompt, options["skills"], options["references"], options.get("images"), None, False,
        {"vision": True}, threading.Event())
    return messages[0]["content"]


@pytest.mark.parametrize("model", [API_MODEL, "claude-sonnet"])
@pytest.mark.parametrize("operation", ["plan", "rewrite"])
def test_photo_operations_supply_only_authorized_api_context(tmp_path, model, operation):
    skills = skill_tree(tmp_path)
    cases = tmp_path / "cases"
    write(cases / "references" / "products" / "202H.md", "Current 202H product facts")
    write(cases / "references" / "products" / "202H-selling-point-grading.md", "Current 202H grading")
    write(cases / "references" / "products" / "other.md", "Other product facts")
    slide = {"title_lines": ["CURRENT COPY"], "body_lines": ["One current fact."], "frame_time": 0.4}
    output = {"versions": [{"writing": "self", "slides": [slide]}]} if operation == "plan" else {"options": [slide]}
    bridge = SimpleNamespace(run=Mock(return_value=json.dumps(output)))
    sets = PhotoCarousels.__new__(PhotoCarousels)
    sets.root = tmp_path / "photo-sets"
    folder = sets.root / "PHS-0123456789ab"
    folder.mkdir(parents=True)
    picture = folder / "frame.jpg"
    picture.write_bytes(b"test-frame")
    job = {"id": "job", "lines": [{"number": 1, "spoken": "Current script", "zh": "当前稿件"}]}
    sets.app = SimpleNamespace(store=SimpleNamespace(root=cases), production=SimpleNamespace(job=lambda _: job),
                               ai=SimpleNamespace(bridge=bridge, preferences=lambda: {"model": model},
                                                  skill_code_root=str(skills / "product-video-script-review")))
    data = {"id": folder.name, "job_id": "job", "product": "202H", "ai": {"status": "running"},
            "slide_ai": {"status": "running", "key": "work", "number": 1, "snapshot": slide}}
    sets.lock, sets._cancel = threading.RLock(), {}
    sets._read, sets._save = Mock(return_value=data), Mock()
    sets._settle, sets._progress, sets._note = Mock(), Mock(), Mock()
    sets.frames = Mock(return_value={"items": []})
    sets._bases = Mock(return_value=[picture])
    sets._work = Mock(return_value={"writing": "self", "slides": [slide]})
    if operation == "plan":
        sets._plan(folder.name, threading.Event(), "")
        assert data["ai"]["status"] == "done", data["ai"]
    else:
        sets._rewrite(folder.name, threading.Event())
        assert data["slide_ai"]["status"] == "done", data["slide_ai"]
    prompt, options = bridge.run.call_args.args[0], bridge.run.call_args.kwargs
    if model != API_MODEL:
        assert "references" not in options
        return
    paths = {Path(row["path"]) for row in options["references"]}
    assert paths == {
        (folder / "refs" / "202H.md").resolve(),
        (folder / "refs" / "202H-selling-point-grading.md").resolve(),
        (skills / "tiktok-photo-commerce" / "references" / "quality-standards.md").resolve(),
        (skills / "product-video-script-writing" / "references" / "writing-and-native.md").resolve(),
    }
    context = api_messages(prompt, options)
    for text in ("Current 202H product facts", "Current 202H grading", "Photo quality rules", "Current native writing rules"):
        assert text in context
    assert "Other product facts" not in context
    assert "Unneeded rendering rules" not in context
    assert "Unrelated private reference" not in context


def skeleton_manager(tmp_path, model):
    skills = skill_tree(tmp_path)
    points = {"S1": {"id": "p1", "code": "S1", "tier": "S", "status": "testing"},
              "B1": {"id": "p2", "code": "B1", "tier": "B", "status": "testing"}}
    output = {"opening": [{"pain_zh": "角度靠猜", "quotes": ["q1"]}],
              "middle": [{"code": "S1", "answer_zh": "固定角度"}],
              "closing": {"code": "B1", "reason_zh": "质保"}, "verdict": {"kind": "new"}}
    bridge = SimpleNamespace(run=Mock(return_value=json.dumps(output)))
    manager = OpportunitySkeletons.__new__(OpportunitySkeletons)
    manager.path = tmp_path / "reviews" / "opportunity-skeletons.json"
    manager.path.parent.mkdir()
    manager.app = SimpleNamespace(ai=SimpleNamespace(bridge=bridge, preferences=lambda: {"model": model},
                                                     skill_code_root=str(skills / "product-video-script-review")))
    manager._theme = Mock(return_value={"title": "角度", "distinct_reviews": 1, "recent_reviews": 1,
                                        "evidence": [{"id": "q1", "text": "I guess my angle."}]})
    manager._catalog = Mock(return_value=(points, {}))
    manager._update = Mock()
    return manager, bridge, skills, points


def test_skeleton_api_references_match_fingerprinted_snapshot(tmp_path):
    manager, bridge, skills, points = skeleton_manager(tmp_path, API_MODEL)
    original_sources = manager._sources(points, {})
    writing = skills / "product-video-script-writing" / "references" / "writing-and-native.md"
    output = bridge.run.return_value

    def run(prompt, **options):
        writing.write_text("Changed after submission", encoding="utf-8")
        return output

    bridge.run.side_effect = run
    manager._generate("202H", "angle", threading.Event())
    prompt, options = bridge.run.call_args.args[0], bridge.run.call_args.kwargs
    assert {row["label"] for row in options["references"]} == {"references/writing-and-native.md", "references/guidance.md"}
    context = api_messages(prompt, options)
    assert "Current native writing rules" in context
    assert "Current writing guidance" in context
    assert "Changed after submission" not in context
    assert "Unrelated private reference" not in context
    result = manager._update.call_args.kwargs
    assert result["status"] == "done", result
    assert result["source_basis"]["writing_skill"] == original_sources["writing_skill"]
    assert manager._sources(points, {})["writing_skill"] != original_sources["writing_skill"]


@pytest.mark.parametrize("model", [API_MODEL, "claude-sonnet"])
def test_skeleton_missing_reference_keeps_existing_source_policy(tmp_path, model):
    manager, bridge, skills, points = skeleton_manager(tmp_path, model)
    (skills / "product-video-script-writing" / "references" / "guidance.md").unlink()
    expected = manager._sources(points, {})
    manager._generate("202H", "angle", threading.Event())
    options = bridge.run.call_args.kwargs
    if model == API_MODEL:
        assert [row["label"] for row in options["references"]] == ["references/writing-and-native.md"]
    else:
        assert "references" not in options
    result = manager._update.call_args.kwargs
    assert result["status"] == "done", result
    assert result["source_basis"]["writing_skill"] == expected["writing_skill"]


def test_skeleton_without_skill_does_not_load_orphaned_references(tmp_path):
    manager, bridge, skills, points = skeleton_manager(tmp_path, API_MODEL)
    (skills / "product-video-script-writing" / "SKILL.md").unlink()
    manager._generate("202H", "angle", threading.Event())
    options = bridge.run.call_args.kwargs
    assert options["skills"] == []
    assert options["references"] == []
    assert manager._update.call_args.kwargs["source_basis"]["writing_skill"] is None
