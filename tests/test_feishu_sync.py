"""飞书同步：lark-cli 全部用假的命令替代，表格在内存里，文件只在临时目录。"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from feishu_sync import FeishuError, FeishuSync, find_cli
from notion_bridge import notion_id
from test_notion_upload import FILE, PAGE, FakeNotion, NotionUploadTests, PhotoNotionUploadTests
from test_video_publication import JOB


SETTINGS = {"cli": "", "base_token": "BASE", "table_id": "tblTEST",
            "accounts": {"video": {"Fixture": "zack"}, "photo": {"Fixture": "zack post"}}}
FIELDS = [
    {"name": "视频 ID / 编号", "type": "text"}, {"name": "视频标题 + Hashtag", "type": "text"},
    {"name": "发布日期", "type": "datetime"}, {"name": "脚本文案", "type": "text"}, {"name": "成片视频", "type": "attachment"},
    {"name": "发布账号", "type": "select", "options": [{"name": n} for n in ("zack", "zack post", "伍Lab", "bush")]},
    {"name": "产品型号", "type": "select", "options": [{"name": n} for n in ("202H", "198H")]},
    {"name": "视频状态", "type": "select", "options": [{"name": n} for n in ("就绪", "已发布")]},
]


class Done:
    def __init__(self, data, returncode=0):
        self.returncode, self.stdout, self.stderr = returncode, json.dumps(data, ensure_ascii=False).encode("utf-8"), b""


class FakeLark:
    """Answers the handful of `lark-cli base` commands feishu_sync uses, against an in-memory table."""
    def __init__(self):
        self.records, self.calls, self.uploaded = {}, [], []
        self.fail = ""
        self.count = 0

    def add(self, name, status="已发布", tokens=()):
        self.count += 1
        rid = f"rec{self.count:04d}"
        self.records[rid] = {"视频 ID / 编号": name, "视频状态": [status], "成片视频": [{"file_token": t, "name": t} for t in tokens]}
        return rid

    def __call__(self, command, capture_output=True, timeout=None, cwd=None, creationflags=0):
        action, args = command[2], command[3:]
        self.calls.append((action, args, cwd))
        if action == self.fail:
            return Done({"ok": False, "error": {"message": "测试里飞书拒绝了"}}, 1)
        value = lambda flag: args[args.index(flag) + 1] if flag in args else None
        values = lambda flag: [args[i + 1] for i, item in enumerate(args) if item == flag]
        matrix = lambda ids, fields: {"ok": True, "data": {"fields": fields, "record_id_list": ids,
                                                           "data": [[self.records[r].get(f) for f in fields] for r in ids]}}
        if action == "+field-list":
            return Done({"ok": True, "data": {"fields": FIELDS}})
        if action == "+record-get":
            rid = value("--record-id")
            if rid not in self.records:
                return Done({"ok": False, "error": {"message": "record not found"}}, 1)
            return Done(matrix([rid], values("--field-id")))
        if action == "+record-list":
            names = [c[2] for c in json.loads(value("--filter-json"))["conditions"]]
            return Done(matrix([r for r, row in self.records.items() if row["视频 ID / 编号"] in names], values("--field-id")))
        if action == "+record-upsert":
            fields = json.loads(value("--json"))
            rid = value("--record-id")
            if rid:
                self.records[rid].update(fields)
                return Done({"ok": True, "data": {"record": {"record_id": rid}}})
            self.count += 1
            rid = f"rec{self.count:04d}"
            self.records[rid] = {"成片视频": [], **fields}
            return Done({"ok": True, "data": {"record": {"record_id": rid, "fields": fields}}})
        if action == "+record-upload-attachment":
            for name in values("--file"):
                assert (Path(cwd) / name).is_file(), name
                self.uploaded.append((name, (Path(cwd) / name).read_bytes()))
                self.count += 1  # 真飞书每次上传都给新的 file_token
                self.records[value("--record-id")]["成片视频"].append({"file_token": f"tok{self.count}", "name": name})
            return Done({"ok": True, "data": {}})
        if action == "+record-remove-attachment":
            assert "--yes" in args
            gone = set(values("--file-token"))
            row = self.records[value("--record-id")]
            row["成片视频"] = [item for item in row["成片视频"] if item["file_token"] not in gone]
            return Done({"ok": True, "data": {}})
        raise AssertionError(action)


class FeishuSyncTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.video = self.root / "upload.mp4"
        self.video.write_bytes(b"video-bytes")
        self.lark = FakeLark()
        self.sync = FeishuSync(SETTINGS, runner=self.lark, cli_finder=lambda configured: "lark-cli.exe")

    def row(self, **extra):
        return {"name": "🇺🇸 ▶️ 202-Fixture-1103-1", "legacy_name": "Fixture261103-202-1", "title": "🔴Lock the angle#SHARPAL202H",
                "published_on": "2026-11-03", "account": "fixture", "source": {"en": "Use this angle.", "zh": "用这个角度。"}, **extra}

    def test_a_new_record_is_created_ready_with_mapped_account_product_copy_and_the_named_file(self):
        result = self.sync.sync("video", self.row(), [(self.video, "US-202-Fixture-1103-1.mp4")], creator="Fixture", product="202H")
        record = self.lark.records[result["record_id"]]
        self.assertEqual(("synced", ""), (result["status"], result["warning"]))
        self.assertEqual("🇺🇸 ▶️ 202-Fixture-1103-1", record["视频 ID / 编号"])
        self.assertEqual((["zack"], ["202H"], ["就绪"], "2026-11-03"), (record["发布账号"], record["产品型号"], record["视频状态"], record["发布日期"]))
        self.assertEqual("Use this angle.\n\n用这个角度。", record["脚本文案"])
        self.assertEqual("🔴Lock the angle#SHARPAL202H", record["视频标题 + Hashtag"])
        self.assertEqual([("US-202-Fixture-1103-1.mp4", b"video-bytes")], self.lark.uploaded)
        self.assertNotIn("+record-remove-attachment", [call[0] for call in self.lark.calls])
        # 每次都用用户身份调到配置的表。
        self.assertTrue(all(call[1][-6:] == ["--base-token", "BASE", "--table-id", "tblTEST", "--as", "user"] for call in self.lark.calls))

    def test_an_old_named_record_is_found_renamed_and_its_file_replaced_without_touching_status(self):
        rid = self.lark.add("Fixture261103-202-1", status="已发布", tokens=["old-token"])
        result = self.sync.sync("video", self.row(), [(self.video, "US-202-Fixture-1103-1.mp4")], creator="Fixture", product="202H")
        record = self.lark.records[rid]
        self.assertEqual(rid, result["record_id"])
        self.assertEqual(1, len(self.lark.records))
        self.assertEqual(("🇺🇸 ▶️ 202-Fixture-1103-1", ["已发布"]), (record["视频 ID / 编号"], record["视频状态"]))
        self.assertEqual(["US-202-Fixture-1103-1.mp4"], [item["name"] for item in record["成片视频"]])
        actions = [call[0] for call in self.lark.calls]
        self.assertLess(actions.index("+record-upload-attachment"), actions.index("+record-remove-attachment"))

    def test_the_new_name_wins_over_the_old_one(self):
        self.lark.add("Fixture261103-202-1")
        current = self.lark.add("🇺🇸 ▶️ 202-Fixture-1103-1")
        self.assertEqual(current, self.sync.sync("video", self.row(), [])["record_id"])

    def test_two_records_with_the_same_name_are_not_guessed(self):
        self.lark.add("Fixture261103-202-1")
        self.lark.add("Fixture261103-202-1")
        with self.assertRaisesRegex(FeishuError, "2 条"):
            self.sync.sync("video", self.row(), [])

    def test_a_saved_record_id_is_updated_directly(self):
        rid = self.lark.add("改过名的记录", tokens=["old"])
        result = self.sync.sync("video", self.row(feishu_record_id=rid), [(self.video, "US.mp4")])
        self.assertEqual(rid, result["record_id"])
        self.assertNotIn("+record-list", [call[0] for call in self.lark.calls])
        self.assertEqual("🇺🇸 ▶️ 202-Fixture-1103-1", self.lark.records[rid]["视频 ID / 编号"])

    def test_a_deleted_saved_record_falls_back_to_the_name_search(self):
        result = self.sync.sync("video", self.row(feishu_record_id="recGONE"), [])
        self.assertIn(result["record_id"], self.lark.records)
        self.assertIn("+record-list", [call[0] for call in self.lark.calls])

    def test_account_mapping_falls_back_to_an_existing_option_and_never_invents_one(self):
        options = ["zack", "zack post", "伍Lab", "bush"]
        self.assertEqual(("zack post", ""), self.sync.account_for("photo", "fixture", "x", options))  # 不分大小写
        self.assertEqual(("bush", ""), self.sync.account_for("video", "Other", "Bush", options))
        account, warning = self.sync.account_for("video", "Other", "brand_new", options)
        self.assertEqual("", account)
        self.assertIn("brand_new", warning)
        result = self.sync.sync("video", self.row(account="brand_new"), [], creator="Other", product="999X")
        record = self.lark.records[result["record_id"]]
        self.assertNotIn("发布账号", record)
        self.assertNotIn("产品型号", record)
        self.assertIn("brand_new", result["warning"])

    def test_photo_copy_and_all_pictures_go_up(self):
        pictures = []
        for number in (1, 2):
            path = self.root / f"{number:02d}.jpg"
            path.write_bytes(bytes([number]))
            pictures.append((path, f"US-202-Fixture-1103-1A-{number:02d}.jpg"))
        row = self.row(name="🇺🇸 🧩 202-Fixture-1103-1A", source={"caption_en": "Seven plates.", "caption_zh": "七块磨板。"})
        result = self.sync.sync("photo", row, pictures, creator="Fixture")
        record = self.lark.records[result["record_id"]]
        self.assertEqual((["zack post"], "Seven plates.\n\n七块磨板。"), (record["发布账号"], record["脚本文案"]))
        self.assertEqual([name for _path, name in pictures], [name for name, _ in self.lark.uploaded])

    def test_without_a_cli_nothing_is_called(self):
        sync = FeishuSync(SETTINGS, runner=self.lark, cli_finder=lambda configured: "")
        result = sync.sync("video", self.row(), [])
        self.assertEqual("skipped", result["status"])
        self.assertIn("lark-cli", result["warning"])
        self.assertEqual([], self.lark.calls)

    def test_a_cli_error_is_reported_with_its_message(self):
        self.lark.fail = "+record-upsert"
        with self.assertRaisesRegex(FeishuError, "测试里飞书拒绝了"):
            self.sync.sync("video", self.row(), [])

    def test_the_highest_jianying_version_is_picked(self):
        apps = self.root / "JianyingPro" / "Apps"
        for version in ("9.10.0.1", "11.8.0.14778", "11.10.0.2"):
            cli = apps / version / "Resources/agent_runtime_env/cli/npm-tools/node_modules/@larksuite/cli/bin/lark-cli.exe"
            cli.parent.mkdir(parents=True)
            cli.write_bytes(b"")
        with patch.dict("os.environ", {"LOCALAPPDATA": str(self.root)}):
            self.assertIn("11.10.0.2", find_cli(""))
            self.assertEqual(str(self.video), find_cli(str(self.video)))
        with patch.dict("os.environ", {"LOCALAPPDATA": str(self.root / "none")}), patch("feishu_sync.shutil.which", return_value=None):
            self.assertEqual("", find_cli(""))


class NotionThenFeishuTests(unittest.TestCase):
    """Wired into the Notion upload: runs after Notion succeeded and never changes the Notion result."""
    def setUp(self):
        NotionUploadTests.setUp(self)
        self.lark = FakeLark()
        self.manager.feishu = FeishuSync(SETTINGS, runner=self.lark, cli_finder=lambda configured: "lark-cli.exe")

    body = NotionUploadTests.body
    run_upload = NotionUploadTests.run_upload

    def test_a_completed_upload_writes_feishu_and_a_reupload_reuses_the_record(self):
        row = self.run_upload()
        self.assertEqual(("completed", "synced"), (row["status"], row["feishu_status"]))
        record = self.lark.records[row["feishu_record_id"]]
        self.assertEqual(("🇺🇸 ▶️ 202-Fixture-1103-1", ["zack"], ["202H"]), (record["视频 ID / 编号"], record["发布账号"], record["产品型号"]))
        self.assertEqual(["US-202-Fixture-1103-1.mp4"], [item["name"] for item in record["成片视频"]])
        self.assertEqual("synced", self.manager.public(row)["feishu_status"])
        self.lark.calls.clear()
        again = self.run_upload(self.body(title="🔴New title#SHARPAL202H"))
        self.assertEqual(row["feishu_record_id"], again["feishu_record_id"])
        self.assertEqual(1, len(self.lark.records))
        self.assertEqual("🔴New title#SHARPAL202H", record["视频标题 + Hashtag"])
        self.assertEqual(1, len(record["成片视频"]))  # 替换，不重复追加
        self.assertNotIn("+record-list", [call[0] for call in self.lark.calls])

    def test_a_feishu_failure_leaves_the_notion_upload_completed(self):
        self.lark.fail = "+field-list"
        row = self.run_upload()
        self.assertEqual(("completed", "failed"), (row["status"], row["feishu_status"]))
        self.assertIn("测试里飞书拒绝了", row["feishu_error"])
        self.assertTrue(self.bridge.pages[notion_id(PAGE)]["properties"]["文件和媒体"])
        self.assertTrue(self.app.calendar["video:" + JOB]["calendar_uploaded"])

    def test_an_unexpected_error_is_also_contained(self):
        self.manager.feishu.sync = lambda *args, **kwargs: 1 / 0
        row = self.run_upload()
        self.assertEqual(("completed", "failed"), (row["status"], row["feishu_status"]))

    def test_a_record_created_before_a_failed_attachment_is_kept_for_the_retry(self):
        self.lark.fail = "+record-upload-attachment"
        row = self.run_upload()
        self.assertEqual("failed", row["feishu_status"])
        self.assertIn(row["feishu_record_id"], self.lark.records)
        self.lark.fail = ""
        again = self.run_upload()
        self.assertEqual((row["feishu_record_id"], "synced"), (again["feishu_record_id"], again["feishu_status"]))
        self.assertEqual(1, len(self.lark.records))

    def test_a_notion_failure_never_reaches_feishu(self):
        self.bridge.fail = "create_pages"
        row = self.run_upload()
        self.assertEqual("failed", row["status"])
        self.assertEqual([], self.lark.calls)

    def test_no_feishu_settings_means_no_feishu_fields(self):
        self.manager.feishu = None
        row = self.run_upload()
        self.assertEqual("completed", row["status"])
        self.assertFalse(row.get("feishu_status"))


class PhotoNotionThenFeishuTests(unittest.TestCase):
    SET = PhotoNotionUploadTests.SET

    def setUp(self):
        PhotoNotionUploadTests.setUp(self)
        self.lark = FakeLark()
        self.manager.feishu = FeishuSync(SETTINGS, runner=self.lark, cli_finder=lambda configured: "lark-cli.exe")

    export = PhotoNotionUploadTests.export
    photo_body = PhotoNotionUploadTests.photo_body
    run_photo = PhotoNotionUploadTests.run_photo

    def test_carousel_pictures_go_to_the_photo_account(self):
        self.export("C1-20261108-A达人自述", slides=3)
        row = self.run_photo()
        self.assertEqual(("completed", "synced"), (row["status"], row["feishu_status"]))
        record = self.lark.records[row["feishu_record_id"]]
        self.assertEqual(("🇺🇸 🧩 202-Fixture-1103-1A", ["zack post"]), (record["视频 ID / 编号"], record["发布账号"]))
        self.assertEqual([f"US-202-Fixture-1103-1A-{n:02d}.jpg" for n in (1, 2, 3)], [item["name"] for item in record["成片视频"]])
        self.assertEqual("Seven plates in one case.\n\n一个盒子装七块磨板。", record["脚本文案"])


if __name__ == "__main__":
    unittest.main()
