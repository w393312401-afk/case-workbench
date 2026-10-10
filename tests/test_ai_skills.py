"""Skill preparation is read-only and tested with disposable local fixtures."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
import ai_skills
from ai_skills import ANALYSIS, AUDIT, GENERATION, MEDIA, REVIEW, WRITING, SkillRuntime, SkillRuntimeError


def skill_dir(root, owner):
    """Where a skill lives: the case-library root, or a sibling directory beside it."""
    root = Path(root)
    return root if owner == REVIEW else root.parent / owner


def install_review_fixture(root):
    """Populate the case-library root plus the sibling skills the runtime loads."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    for owner in (REVIEW, WRITING, AUDIT, ANALYSIS, MEDIA):
        folder = skill_dir(root, owner)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "SKILL.md").write_text(f"---\nname: {owner}\ndescription: Local fixture\n---\n# MAIN CONTENT {owner}\n", encoding="utf-8")
    specs = (ai_skills.WRITING_REFERENCES + ai_skills.AUDIT_REFERENCES + ai_skills.SCORING_REFERENCES
             + ai_skills.RETROSPECTIVE_REFERENCES)
    for owner, relative, label, section in specs:
        path = skill_dir(root, owner) / relative
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if relative.endswith("media-tools.md"):
            text = "# 媒体\n## 1. 观察原片\n只读观察。\n## 3. 配音\n不要加载配音流程。\n"
        elif relative.endswith("feedback-boundaries.md"):
            text = "# 反馈\n事实与表现分开。\n不自动升级规则。\n"
        elif relative.endswith("writing-and-native.md"):
            text = "# 写作\n完整参考文本，保留事实和范围。\n## 说话任务与现场口吻\n按场景复核口吻。\n## 起稿\n评分引用不含起稿。\n"
        elif relative.endswith("native-scoring.md"):
            text = "# 原生度文本评分规范\n版本：v1\nNATIVE_SCORING_FIXTURE_V1\n评分只是内容判断，不保证真人口吻。\n"
        else:
            text = "# " + label + "\n完整参考文本，保留事实和范围。\n"
        path.write_text(text, encoding="utf-8")
    products = root / "references" / "products"
    products.mkdir(parents=True, exist_ok=True)
    (products / "source-map.json").write_text(json.dumps({"sku_to_reference": {"202H": "202H.md"}}), encoding="utf-8")


class SkillRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ai-skill-runtime-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.review = self.base / REVIEW
        self.generation = self.base / GENERATION
        install_review_fixture(self.review)
        self.write(self.base / WRITING / "agents" / "openai.yaml", 'interface:\n  display_name: "05 带货创作"\n')
        self.write(self.generation / "SKILL.md", f"---\nname: {GENERATION}\n---\nDO_NOT_LOAD_LEGACY_RULES\n")
        self.runtime = SkillRuntime(self.review)

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def snapshot(self):
        return {str(path): path.read_bytes() for path in self.base.rglob("*") if path.is_file()}

    def test_catalog_reports_actual_main_files_names_labels_and_hashes(self):
        before = self.snapshot()
        catalog = self.runtime.catalog()
        self.assertEqual([item["name"] for item in catalog], [REVIEW])
        for item in catalog:
            self.assertTrue(item["available"])
            self.assertIsNone(item["reason"])
            self.assertEqual(item["sha256"], hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest())
        self.assertEqual(catalog[0]["label"], "带货文案创作与审查")
        self.assertEqual(catalog[0]["members"], [WRITING, AUDIT, ANALYSIS])
        self.assertEqual(self.snapshot(), before)

    def test_auto_selects_action_skill_and_native_main_is_not_duplicated(self):
        expected = {"variants": WRITING, "review": AUDIT, "retrospective": ANALYSIS}
        for action, name in expected.items():
            result = self.runtime.prepare(action)
            self.assertTrue(result["ready"])
            self.assertEqual(result["selection"], "auto")
            self.assertEqual([skill["name"] for skill in result["skills"]], [name])
            self.assertTrue(all(Path(skill["path"]).parent.parent == self.base for skill in result["skills"]))
            self.assertNotIn("text", result["skills"][0])
            self.assertIn("原生 skill 输入", result["instructions"])
            self.assertNotIn("# MAIN CONTENT", json.dumps(result, ensure_ascii=False))
            self.assertEqual(result["state"], "prepared")

    def test_none_loads_no_main_or_references_even_when_skills_are_missing(self):
        missing = SkillRuntime(self.base / "missing-review")
        result = missing.prepare("review", "none")
        self.assertTrue(result["ready"])
        self.assertEqual(result["skills"], [])
        self.assertEqual(result["references"], [])
        self.assertIn("不调用技能", result["instructions"])

    def test_legacy_selections_map_to_review_without_loading_old_rules(self):
        before = self.snapshot()
        for selection in ("both", GENERATION, REVIEW):
            result = self.runtime.prepare("variants", selection)
            self.assertEqual([item["name"] for item in result["skills"]], [WRITING])
            self.assertEqual(result["skills"][0]["label"], "05 带货创作")
            self.assertEqual(result["effective_selection"], REVIEW)
            self.assertEqual(bool(result["migration_notice"]), selection != REVIEW)
            self.assertTrue(all(Path(item["path"]).is_relative_to(self.base) for item in result["references"]))
            paths = {Path(item["path"]).name for item in result["references"]}
            self.assertTrue({"writing-and-native.md", "native-expression-library.md", "media-tools.md"} <= paths)
            self.assertFalse({"output-template.md", "script-duration-policy.md", "banned_global.json"} & paths)
            self.assertNotIn("DO_NOT_LOAD_LEGACY_RULES", json.dumps(result))
        self.assertEqual(self.snapshot(), before)

    def test_variant_review_stage_keeps_writing_rules_without_diagnostic_review(self):
        before = self.snapshot()
        draft = self.runtime.prepare("variants")
        review = self.runtime.prepare("variants", stage="review")
        diagnostic = self.runtime.prepare("review")
        self.assertEqual((draft["stage"], review["stage"]), ("main", "review"))
        self.assertEqual([item["path"] for item in review["references"]], [item["path"] for item in draft["references"]])
        self.assertEqual([skill["name"] for skill in draft["skills"]], [WRITING])
        self.assertEqual([skill["name"] for skill in review["skills"]], [WRITING])
        self.assertNotIn("native-scoring.md", {Path(item["path"]).name for item in review["references"]})
        self.assertNotIn("默认做简明原生度文本评估", review["instructions"])
        self.assertIn("意群表", review["instructions"])
        self.assertNotIn("visual-handoff.md", {Path(item["path"]).name for item in review["references"] + draft["references"]})
        self.assertNotIn("review-guide.md", {Path(item["path"]).name for item in review["references"]})
        self.assertIn("review-guide.md", {Path(item["path"]).name for item in diagnostic["references"]})
        self.assertIn("保留画面留白和指代悬念", review["instructions"])
        self.assertIn("不是审稿诊断", review["instructions"])
        self.assertIn("评价力度不得低于初稿", review["instructions"])
        self.assertNotIn("自动重写", review["instructions"])
        self.assertNotIn("不是审稿诊断", draft["instructions"])
        self.assertIn("交付正文无需重复", review["instructions"])
        self.assertNotIn("交付正文无需重复", diagnostic["instructions"])
        none = self.runtime.prepare("variants", "none", stage="review")
        self.assertEqual((none["skills"], none["references"]), ([], []))
        for action, stage in (("review", "review"), ("retrospective", "review"), ("variants", "draft"), ("variants", "../x")):
            with self.subTest(action=action, stage=stage):
                with self.assertRaises(SkillRuntimeError) as error:
                    self.runtime.prepare(action, stage=stage)
                self.assertEqual(error.exception.status, 400)
        self.assertEqual(self.snapshot(), before)

    def test_review_digest_rules_load_for_writing_and_review_but_not_retrospective(self):
        def names(action, stage=None):
            return {Path(item["path"]).name for item in self.runtime.prepare(action, "auto", stage=stage)["references"]}
        for action, stage in (("variants", None), ("variants", "review"), ("review", None)):
            self.assertIn("user-reviews-in-writing.md", names(action, stage), (action, stage))
        self.assertNotIn("user-reviews-in-writing.md", names("retrospective"))
        # the rules are read as a whole file from the case-library skill, with its hash recorded like every other reference
        item = next(r for r in self.runtime.prepare("review")["references"] if r["path"].endswith("user-reviews-in-writing.md"))
        self.assertEqual((item["label"], item["skill_name"]), ("用户评论库在写稿与审稿中的用法", REVIEW))
        self.assertNotIn("section", item)

    def test_scoring_file_is_actually_read_only_for_reviewing_stages(self):
        path = self.base / AUDIT / "references" / "native-scoring.md"
        original_read = ai_skills._read
        for action, stage, expected in (("variants", None, False), ("variants", "review", False),
                                        ("review", None, True), ("retrospective", None, False)):
            with self.subTest(action=action, stage=stage), patch.object(ai_skills, "_read", wraps=original_read) as reading:
                result = self.runtime.prepare(action, stage=stage)
                actual_reads = [Path(call.args[0]) for call in reading.call_args_list]
                self.assertEqual(actual_reads.count(path), int(expected))
                matches = [item for item in result["references"] if Path(item["path"]) == path]
                self.assertEqual(len(matches), int(expected))
                if expected:
                    self.assertEqual(matches[0]["text"], path.read_bytes().decode("utf-8-sig"))
                    self.assertEqual(matches[0]["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
                else:
                    self.assertNotIn("NATIVE_SCORING_FIXTURE_V1", json.dumps(result))

    def test_scoring_hash_refreshes_for_review_and_preserves_prepared_snapshot(self):
        path = self.base / AUDIT / "references" / "native-scoring.md"
        prepared = self.runtime.prepare("review")
        first = next(item for item in prepared["references"] if Path(item["path"]) == path)
        original_text, original_hash = first["text"], first["sha256"]
        replacement = "# Revised fixture\n版本：v1\nA changed explanatory example.\n"
        self.write(path, replacement)
        expected_text = path.read_bytes().decode("utf-8-sig")
        for action, stage in (("review", None),):
            updated = self.runtime.prepare(action, stage=stage)
            item = next(item for item in updated["references"] if Path(item["path"]) == path)
            self.assertEqual(item["text"], expected_text)
            self.assertNotEqual(item["sha256"], original_hash)
            self.assertEqual(item["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(item["text_sha256"], hashlib.sha256(expected_text.encode("utf-8")).hexdigest())
        self.assertEqual((first["text"], first["sha256"]), (original_text, original_hash))

    def test_missing_scoring_file_does_not_break_drafting_or_retrospective_or_leak_partial_bundle(self):
        (self.base / AUDIT / "references" / "native-scoring.md").unlink()
        for action, stage in (("variants", None), ("variants", "review"), ("retrospective", None)):
            self.assertTrue(self.runtime.prepare(action, stage=stage)["ready"])
        for action, stage in (("review", None),):
            result = self.runtime.prepare(action, stage=stage)
            self.assertFalse(result["ready"])
            self.assertEqual((result["skills"], result["references"]), ([], []))
            self.assertIn("native-scoring.md", result["unavailable"][0]["reason"])
            self.assertNotIn("默认做简明原生度文本评估", result["instructions"])
            with self.assertRaisesRegex(SkillRuntimeError, "native-scoring.md"):
                self.runtime.prepare(action, REVIEW, stage=stage)

    def test_none_does_not_read_scoring_or_other_files_even_when_present(self):
        with patch.object(ai_skills, "_read", side_effect=AssertionError("none must not read skill files")):
            for action, stage in (("review", None), ("variants", None), ("variants", "review"), ("retrospective", None)):
                result = self.runtime.prepare(action, "none", stage=stage)
                self.assertTrue(result["ready"])
                self.assertEqual((result["skills"], result["references"]), ([], []))
                self.assertNotIn("NATIVE_SCORING_FIXTURE_V1", json.dumps(result))

    def test_reference_content_is_real_latest_and_full_hash_matches_file(self):
        first = self.runtime.prepare("review")
        path = self.base / AUDIT / "references" / "guidance.md"
        original = next(item for item in first["references"] if item["path"] == str(path))
        self.write(path, "# 最新补充指导\n用户新采用的规则。\n")
        second = self.runtime.prepare("review")
        changed = next(item for item in second["references"] if item["path"] == str(path))
        self.assertEqual(changed["text"], path.read_bytes().decode("utf-8-sig"))
        self.assertNotEqual(changed["sha256"], original["sha256"])
        self.assertEqual(changed["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(changed["text_sha256"], hashlib.sha256(changed["text"].encode("utf-8")).hexdigest())

    def test_section_excerpts_keep_subsections_and_code_but_exclude_unrelated_steps(self):
        excerpt = ai_skills._section("# Fixture\n## 1. 检查\n本节完整内容\n```text\n## 代码块内标题\n```\n### 子标题\n子节完整内容\n## 2. 其他\n不属于所选章节\n", "## 1.")
        self.assertIn("本节完整内容", excerpt)
        self.assertIn("代码块内标题", excerpt)
        self.assertIn("子节完整内容", excerpt)
        self.assertNotIn("不属于所选章节", excerpt)
        retrospective = self.runtime.prepare("retrospective")
        text = "\n".join(item["text"] for item in retrospective["references"])
        self.assertIn("事实与表现分开", text)
        self.assertIn("不自动升级规则", text)
        self.assertIn("只读观察", text)
        self.assertNotIn("不要加载配音流程", text)

    def test_preparation_never_claims_compliance_or_changes_files(self):
        before = self.snapshot()
        for selection in ("auto", "both", REVIEW, GENERATION, "none"):
            result = self.runtime.prepare("variants", selection)
            self.assertIn("保持只读", result["instructions"])
            self.assertIn("不证明模型完整遵守", result["instructions"])
            self.assertIn("禁止执行", result["instructions"])
        self.assertEqual(self.snapshot(), before)

    def test_missing_main_is_explicit_for_selection_and_not_misreported_by_auto(self):
        path = self.base / AUDIT / "SKILL.md"
        path.unlink()
        auto = self.runtime.prepare("review")
        self.assertFalse(auto["ready"])
        self.assertEqual(auto["skills"], [])
        self.assertEqual(auto["unavailable"][0]["name"], REVIEW)
        self.assertIn("未使用", auto["instructions"])
        with self.assertRaises(SkillRuntimeError) as error:
            self.runtime.prepare("review", REVIEW)
        self.assertEqual(error.exception.status, 503)
        self.assertFalse(self.runtime.catalog()[0]["available"])

    def test_missing_required_reference_is_reported_without_partial_skill_claim(self):
        (self.base / AUDIT / "references" / "guidance.md").unlink()
        result = self.runtime.prepare("review")
        self.assertFalse(result["ready"])
        self.assertEqual(result["skills"], [])
        self.assertEqual(result["references"], [])
        self.assertIn("guidance.md", result["unavailable"][0]["reason"])
        with self.assertRaises(SkillRuntimeError):
            self.runtime.prepare("review", "both")

    def test_invalid_main_name_and_changed_section_fail_visibly(self):
        self.write(self.base / AUDIT / "SKILL.md", "---\nname: another-skill\n---\n")
        with self.assertRaises(SkillRuntimeError):
            self.runtime.prepare("review", REVIEW)
        self.write(self.base / AUDIT / "SKILL.md", f"---\nname: {AUDIT}\n---\n")
        self.write(self.base / MEDIA / "references" / "media-tools.md", "# 没有观察章节\n")
        with self.assertRaisesRegex(SkillRuntimeError, "章节已变化"):
            self.runtime.prepare("variants", GENERATION)

    def test_selection_cannot_inject_arbitrary_paths_or_unknown_actions(self):
        for action, selection in (("delete", "auto"), ("review", str(self.base / "secret.md")), ("review", "../SKILL.md"), ([], "auto"), ("review", [])):
            with self.subTest(action=action, selection=selection):
                with self.assertRaises(SkillRuntimeError) as error:
                    self.runtime.prepare(action, selection)
                self.assertEqual(error.exception.status, 400)

    def test_hardlinked_skill_file_is_not_loaded(self):
        path = self.base / AUDIT / "SKILL.md"
        alias = self.base / "alias.md"
        try:
            os.link(path, alias)
        except OSError as exc:
            self.skipTest(str(exc))
        self.assertFalse(self.runtime.catalog()[0]["available"])
        with self.assertRaises(SkillRuntimeError):
            self.runtime.prepare("review", REVIEW)

    def test_member_skill_missing_makes_unified_selection_unavailable_without_partial_bundle(self):
        (self.base / WRITING / "SKILL.md").unlink()
        self.assertFalse(self.runtime.catalog()[0]["available"])
        result = self.runtime.prepare("variants")
        self.assertFalse(result["ready"])
        self.assertEqual((result["skills"], result["references"]), ([], []))
        self.assertEqual(result["unavailable"][0]["name"], REVIEW)
        self.assertTrue(self.runtime.prepare("review")["ready"])

    @unittest.skipUnless(os.name == "nt", "Windows directory junction")
    def test_linked_reference_directory_is_not_followed(self):
        root = self.base / "linked-review"
        self.write(root / "SKILL.md", f"---\nname: {REVIEW}\n---\n")
        junction = root / "references"
        source = self.review / "references"
        def quote(value):
            return "'" + str(value).replace("'", "''") + "'"
        process = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  f"New-Item -ItemType Junction -Path {quote(junction)} -Target {quote(source)} | Out-Null"],
                                 capture_output=True, text=True)
        if process.returncode:
            self.skipTest(process.stderr)
        try:
            with self.assertRaises(SkillRuntimeError):
                SkillRuntime(root).prepare("review", REVIEW)
        finally:
            self.assertTrue(junction.absolute().is_relative_to(self.base.absolute()))
            junction.rmdir()

    def test_size_limits_fail_instead_of_truncating_required_content(self):
        with patch.object(ai_skills, "MAX_FILE_BYTES", 30):
            with self.assertRaises(SkillRuntimeError) as error:
                self.runtime.prepare("review", REVIEW)
            self.assertEqual(error.exception.status, 413)
        with patch.object(ai_skills, "MAX_REFERENCE_CHARS", 10):
            with self.assertRaises(SkillRuntimeError) as error:
                self.runtime.prepare("review", REVIEW)
            self.assertEqual(error.exception.status, 413)


if __name__ == "__main__":
    unittest.main()
