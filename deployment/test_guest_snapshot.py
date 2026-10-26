"""Isolated guest publication checks; no real workbench or public service used."""
from contextlib import redirect_stdout
import importlib.util
import io
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit
import uuid
import zipfile


spec = importlib.util.spec_from_file_location("guest_snapshot_under_test", Path(__file__).with_name("guest_snapshot.py"))
guest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guest)


def bundle():
    return {
        "meta": {"exported_at": "2026-10-16T08:00:00+00:00", "schema": 1},
        "state": {"app": "video-case-workbench", "token": "fixture-write-token", "cases": [], "paths": {}},
        "cases": {},
        "reviews": {"summary": {}, "items": [], "picks": [], "tag_edits": [], "videos": {}},
        "performance": {"summary": {}, "analytics": {}, "videos": [], "details": {}},
        "direction_catalog": {"directions": [], "selling_points": [], "video_links": []},
        "directions": {"groups": []}, "directions_all": {"groups": []}, "direction_details": {},
        "creation": {"scripts": [], "materials": [], "plans": [], "theme_links": []},
        "materials": {"items": []}, "scripts": {},
        "ai": {"status": {"available": False}, "options": {}, "tasks": [], "details": {}},
    }


class Scanner:
    def __init__(self, marks=None):
        self.marks = marks or {"fixture-business-record": guest.SYNC.FileMark(5, 10)}
        self.calls = 0

    def take(self):
        self.calls += 1
        return dict(self.marks)


class GuestSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="workbench-guest-fixture-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runtime = self.root / "private-runtime"
        self.root.joinpath("config.json").write_text(json.dumps({"runtime_root": str(self.runtime),
                                                                "skill_root": str(self.root / "skill")}), encoding="utf-8")
        self.static = self.root / "static"
        self.static.mkdir()
        self.source_page = ('<!doctype html><html><head><title>TK工作台</title>'
                            '<script defer src="/demo.js"></script><script src=\'/demo.js?test=1\' defer></script>'
                            '<link href="/style.css" rel="stylesheet"><script src="/app.js" defer></script>'
                            '</head><body>fixture</body></html>')
        self.static.joinpath("index.html").write_text(self.source_page, encoding="utf-8")
        self.static.joinpath("app.js").write_text("function productFilterGroup(item) { return item.product; }", encoding="utf-8")
        for name, value in {"demo.js": "/* private demo excluded from guest HTML */", "style.css": "body{}",
                            "guest-mode.js": "window.WorkbenchGuest=true;", "guest-mode.css": "body{}"}.items():
            self.static.joinpath(name).write_text(value, encoding="utf-8")
        self.support = self.root / "deployment" / "text-only"
        self.support.mkdir(parents=True)
        for name, value in {"_worker.js": "export default {};", "public-mode.js": "window.PublicTextMode={};",
                            "public-mode.css": ".public-text-mode{}"}.items():
            self.support.joinpath(name).write_text(value, encoding="utf-8")
        old = self.root / "deployment" / "site"
        old.mkdir()
        old.joinpath("index.html").write_text("retired sentinel", encoding="utf-8")

    def exporter(self):
        name = "guest_export_fixture_" + uuid.uuid4().hex
        module = guest.load_module(Path(__file__).with_name("export_text_site.py"), name)
        self.addCleanup(guest.sys.modules.pop, name, None)
        module.ROOT, module.SUPPORT = self.root, self.support
        module.export = lambda base: module.build_site(bundle())
        return module

    def build(self, **options):
        exporter = options.pop("exporter", None) or self.exporter()
        output = io.StringIO()
        with redirect_stdout(output):
            value = guest.build_snapshot(self.root, guest.BASE, exporter=exporter, **options)
        self.assertEqual(output.getvalue(), "")
        return value

    def contents(self):
        return {str(path.relative_to(self.runtime)): path.read_bytes()
                for path in self.runtime.rglob("*") if path.is_file()}

    def test_build_namespaces_guest_assets_before_install_without_touching_retired_site(self):
        module = self.exporter()
        self.build(exporter=module)
        site = self.runtime / "remote-access" / "guest" / "site"
        html = site.joinpath("index.html").read_text(encoding="utf-8")
        self.assertNotIn("demo.js", html)
        for name in ("app.js", "guest-styles-0.css"):
            digest = hashlib.sha256(site.joinpath(name).read_bytes()).hexdigest()[:12]
            self.assertIn("/guest-assets/" + name + "?v=" + digest, html)
        self.assertLess(html.index('data-guest-bootstrap="guest-mode.js"'), html.index('data-guest-bootstrap="public-mode.js"'))
        self.assertLess(html.index('data-guest-bootstrap="public-mode.js"'), html.index("app.js"))
        self.assertNotIn('src="/guest-assets/guest-mode.js', html)
        self.assertNotIn('src="/guest-assets/public-mode.js', html)
        css = site.joinpath("guest-styles-0.css").read_text(encoding="utf-8")
        self.assertLess(css.index("/* style.css */"), css.index("/* public-mode.css */"))
        self.assertLess(css.index("/* public-mode.css */"), css.index("/* guest-mode.css */"))
        self.assertNotIn("private demo", "".join(path.read_text(encoding="utf-8") for path in site.glob("guest-program-*.js")))
        self.assertIn('<meta name="workbench-guest" content="true">', html)
        self.assertEqual(self.static.joinpath("index.html").read_text(encoding="utf-8"), self.source_page)
        self.assertEqual(self.root.joinpath("deployment", "site", "index.html").read_text(), "retired sentinel")
        self.assertTrue(module.RETIRED)
        state = json.loads(site.joinpath("public-data.json").read_text(encoding="utf-8"))["state"]
        self.assertTrue(state["guest_mode"])
        self.assertTrue(state["public_mode"])
        self.assertEqual(state["token"], "")
        with zipfile.ZipFile(site.parent / "case-workbench-text.zip") as archive:
            self.assertEqual(archive.read("index.html").decode(), html)

    def test_content_versions_follow_sanitized_bytes_and_only_changed_assets_expire(self):
        source = 'const placeholder = `<input placeholder="C:/Users/private/Desktop/fixture">`;'
        self.static.joinpath("placeholder.js").write_text(source, encoding="utf-8")
        page = self.static / "index.html"
        page.write_text(self.source_page.replace('</head>', '<script src="/placeholder.js?old=1&v=outdated#ready" defer data-unbundled="true"></script></head>'), encoding="utf-8")
        self.build()
        site = self.runtime / "remote-access" / "guest" / "site"
        def versions():
            html = site.joinpath("index.html").read_text(encoding="utf-8")
            return {urlsplit(value).path: urlsplit(value) for value in guest.re.findall(r'(?:src|href)=[\"\'](/guest-assets/[^\"\']+)[\"\']', html)}
        first = versions()
        copied = site.joinpath("placeholder.js").read_bytes()
        sanitized_digest = hashlib.sha256(copied).hexdigest()[:12]
        self.assertNotEqual(sanitized_digest, hashlib.sha256(source.encode()).hexdigest()[:12])
        target = first["/guest-assets/placeholder.js"]
        self.assertEqual(parse_qs(target.query), {"old": ["1"], "v": [sanitized_digest]})
        self.assertEqual(target.fragment, "ready")
        self.build()
        self.assertEqual(versions(), first)
        self.static.joinpath("style.css").write_text("body{color:red}", encoding="utf-8")
        self.build()
        second = versions()
        changed = [name for name in first if first[name] != second[name]]
        self.assertEqual(changed, ["/guest-assets/guest-styles-0.css"])

    def test_missing_guest_asset_never_replaces_last_successful_copy(self):
        self.build()
        previous = self.contents()
        self.static.joinpath("guest-mode.css").unlink()
        with self.assertRaisesRegex(ValueError, "guest_assets_unavailable"):
            self.build()
        self.assertEqual(self.contents(), previous)

    def test_generated_frontend_local_paths_are_cleaned_without_changing_source(self):
        source = ('const placeholder = `<input placeholder="C:/Users/private/Desktop/fixture">`;\n'
                  'const link = "https://example.test/public";\n'
                  'const windows = `<input placeholder="C:\\\\Users\\\\private\\\\fixture.txt">`;\n')
        source_path = self.static / "placeholder.js"
        source_path.write_text(source, encoding="utf-8")
        module = self.exporter()
        self.build(exporter=module)
        copied = (self.runtime / "remote-access" / "guest" / "site" / "placeholder.js").read_text(encoding="utf-8")
        self.assertEqual(source_path.read_text(encoding="utf-8"), source)
        self.assertIn('const placeholder = `<input placeholder="[本机文件]">`;', copied)
        self.assertIn('const windows = `<input placeholder="[本机文件]">`;', copied)
        self.assertIn("https://example.test/public", copied)
        self.assertIsNone(module.LOCAL_PATH.search(copied))

    def test_frontend_credential_detection_preserves_last_successful_copy(self):
        self.build()
        previous = self.contents()
        self.static.joinpath("unsafe.js").write_text('const value = "Bearer ' + "x" * 32 + '";', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "guest_content_rejected"):
            self.build()
        self.assertEqual(self.contents(), previous)

    def test_json_escaped_markup_and_js_backslash_regex_are_not_paths(self):
        source = r'''const split = value => value.split(/(?<!\\)\|/).map(cell => cell.replace(/\\\|/g, "|"));'''
        self.static.joinpath("regex.js").write_text(source, encoding="utf-8")
        self.static.joinpath("index.html").write_text(self.source_page.replace(
            "</head>", '<script src="/regex.js" defer></script></head>'), encoding="utf-8")
        module = self.exporter()
        data = bundle()
        data["state"]["description"] = r'Escaped markup \| column \| value'
        module.export = lambda base: module.build_site(data)
        self.build(exporter=module)
        self.assertEqual((self.runtime / "remote-access" / "guest" / "site" / "regex.js").read_text(), source)
        self.assertTrue((self.runtime / "remote-access" / "guest" / "site" / "guest-program-0.js").is_file())

    def test_non_placeholder_source_path_is_rejected_instead_of_rewriting_code(self):
        self.static.joinpath("unsafe-path.js").write_text('const source = "C:/Users/private/fixture";', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "guest_content_rejected"):
            self.build()

    def test_invalid_javascript_prevents_installation(self):
        self.build()
        previous = self.contents()
        self.static.joinpath("invalid.js").write_text('const invalid = ;', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "guest_javascript_invalid"):
            self.build()
        self.assertEqual(self.contents(), previous)

    def test_source_change_before_install_preserves_previous_copy(self):
        self.build()
        previous = self.contents()
        scanner = Scanner({"fixture-business-record": guest.SYNC.FileMark(6, 20)})
        expected = {"fixture-business-record": guest.SYNC.FileMark(5, 10)}
        with self.assertRaises(guest.SourceChanged):
            self.build(scanner=scanner, expected_marks=expected)
        self.assertEqual(self.contents(), previous)

    def test_derived_review_index_changes_do_not_abort_a_read_only_export(self):
        scanner = Scanner({"derived-index": guest.SYNC.FileMark(6, 20, True)})
        expected = {"derived-index": guest.SYNC.FileMark(5, 10, True)}
        self.assertEqual(self.build(scanner=scanner, expected_marks=expected), scanner.marks)

    def test_fresh_build_failure_leaves_no_site_and_only_generic_status(self):
        def fail(*args, **kwargs):
            raise OSError("fixture-private-password-and-path")
        service = guest.SnapshotService(self.root, scanner=Scanner(), builder=fail, ready=lambda base: True)
        with self.assertLogs("workbench.guest_snapshot", level="INFO") as logs:
            self.assertFalse(service.tick())
        self.assertFalse((service.guest / "site").exists())
        value = json.loads((service.guest / "status.json").read_text())
        self.assertEqual(set(value), {"state", "updated_at", "published_at"})
        self.assertEqual(value["state"], "build_failed")
        self.assertIsNone(value["published_at"])
        self.assertNotIn("fixture-private", " ".join(logs.output) + json.dumps(value))

    def test_scans_are_throttled_and_unchanged_files_skip_export(self):
        clock, calls = [0], []
        scanner = Scanner()
        def build(*args, **kwargs):
            calls.append(True)
            return kwargs["expected_marks"]
        service = guest.SnapshotService(self.root, interval=1, scanner=scanner, builder=build,
                                        ready=lambda base: True, clock=lambda: clock[0])
        self.assertTrue(service.tick())
        self.assertEqual(len(calls), 1)
        clock[0] = 29
        self.assertFalse(service.tick())
        self.assertEqual(scanner.calls, 1)
        clock[0] = 30
        self.assertFalse(service.tick())
        self.assertEqual(scanner.calls, 2)
        self.assertEqual(len(calls), 1)
        scanner.marks["fixture-business-record"] = guest.SYNC.FileMark(7, 30)
        clock[0] = 60
        self.assertTrue(service.tick())
        self.assertEqual(len(calls), 2)

    def test_unavailable_local_service_does_not_attempt_export(self):
        calls = []
        service = guest.SnapshotService(self.root, scanner=Scanner(), builder=lambda *a, **k: calls.append(1),
                                        ready=lambda base: False)
        self.assertFalse(service.tick())
        self.assertEqual(service.state, "waiting_local_service")
        self.assertEqual(calls, [])

    def test_shared_scanner_also_watches_followup_decisions(self):
        scanner = guest.GuestScanner(self.root)
        before = scanner.take()
        followups = self.runtime / "creation" / "followups" / "catalog.json"
        followups.parent.mkdir(parents=True)
        followups.write_text('{"entries":[]}', encoding="utf-8")
        after = scanner.take()
        self.assertIn(str(followups.absolute()), guest.SYNC.changed_files(before, after, business_only=True))

    def test_scan_failure_preserves_published_timestamp(self):
        self.build()
        class FailedScanner:
            def take(self):
                raise OSError("private-details")
        service = guest.SnapshotService(self.root, scanner=FailedScanner())
        self.assertEqual(service.published_at, "2026-10-16T08:00:00+00:00")
        self.assertFalse(service.tick())
        value = json.loads((service.guest / "status.json").read_text())
        self.assertEqual(value["published_at"], "2026-10-16T08:00:00+00:00")
        self.assertEqual(value["state"], "scan_failed")

    def test_non_loopback_or_non_origin_base_is_rejected(self):
        for base in ("https://127.0.0.1:8767", "http://example.test:8767", "http://user:pass@127.0.0.1:8767",
                     "http://127.0.0.1:8767/private", "http://127.0.0.1:8767/?key=fixture", "http://localhost"):
            with self.subTest(base=base), self.assertRaises(ValueError):
                guest.validate_base(base)
        self.assertEqual(guest.validate_base(guest.BASE + "/"), guest.BASE)

    def test_missing_public_guard_template_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "guest_template_invalid"):
            guest.guest_page("<html><head></head></html>")

    def media_fixture(self):
        folder = self.root / "media"
        folder.mkdir(exist_ok=True)
        video = folder / "fixture.mp4"
        video.write_bytes(b"\0\0\0\x18ftypisom" + b"\0" * 20)
        image = folder / "fixture.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 20)
        private = folder / "private.json"
        private.write_text('{"private":"do-not-publish"}')
        case_id = "PVR-0123456789ab"
        current = {"title": "Fixture", "assets": {"video": str(video), "transcript": str(private)}}
        history = {"title": "Previous", "assets": {"screenshot": str(image), "source_manifest": str(private)}}
        case = {"case_id": case_id, "current_event_id": "20261016T000000.000000Z-current",
                "current": current,
                "assets": {"video": {"path": str(video), "name": "unsafe-private-name", "exists": True,
                                      "bytes": 999, "owned": True, "managed": True, "extra_private": "secret"},
                           "transcript": {"path": str(private)}},
                "events": [{"event_id": "20261015T000000.000000Z-history", "current": history},
                           {"event_id": "20261016T000000.000000Z-current", "current": current}]}
        data = bundle()
        data["cases"] = {case_id: case}
        data["state"]["cases"] = [json.loads(json.dumps(case))]
        return data, case_id, video, image, private

    def test_media_metadata_and_allowlist_are_path_free_and_installed_together(self):
        data, case_id, video, image, private = self.media_fixture()
        module = self.exporter()
        original_clean = module.clean
        module.export = lambda base: module.build_site(module.clean(data))
        self.build(exporter=module)
        self.assertIs(module.clean, original_clean)
        site = self.runtime / "remote-access" / "guest" / "site"
        index = json.loads(site.joinpath("media-index.json").read_text())
        expected = {"schema": 1, "cases": {case_id: {
            "current_event_id": "20261016T000000.000000Z-current",
            "events": {"20261015T000000.000000Z-history": ["screenshot"],
                       "20261016T000000.000000Z-current": ["video"]}}}}
        self.assertEqual(index, expected)
        summary = json.loads(site.joinpath("public-data.json").read_text(encoding="utf-8"))["state"]["cases"][0]
        detail = json.loads(site.joinpath("data", "case-" + case_id + ".json").read_text(encoding="utf-8"))["cases"][case_id]
        expected_metadata = {"video": {"name": video.name, "exists": True, "bytes": video.stat().st_size,
                                       "owned": True, "managed": True}}
        for case in (summary, detail):
            self.assertEqual(case["assets"], expected_metadata)
            self.assertEqual(case["current"]["assets"], {"video": True})
        self.assertEqual(detail["events"][0]["current"]["assets"], {"screenshot": True})
        self.assertEqual(detail["events"][1]["current"]["assets"], {"video": True})
        serialized = json.dumps([index, summary, detail])
        for forbidden in (str(self.root), "unsafe-private-name", "extra_private", "do-not-publish", '"path"'):
            self.assertNotIn(forbidden, serialized)
        with zipfile.ZipFile(site.parent / "case-workbench-text.zip") as archive:
            self.assertEqual(json.loads(archive.read("media-index.json")), index)
        self.assertEqual(data["cases"][case_id]["current"]["assets"]["video"], str(video))

    def test_non_media_missing_wrong_suffix_and_invalid_file_headers_are_denied(self):
        data, case_id, video, image, private = self.media_fixture()
        invalid = self.root / "renamed.mp4"
        invalid.write_text("<html>not a video</html>")
        current = data["cases"][case_id]["events"][-1]["current"]
        current["assets"] = {"video": str(invalid), "screenshot": str(video),
                             "contact_sheet": str(self.root / "missing.jpg"), "performance": str(image),
                             "source_manifest": str(private), "transcript": str(private)}
        module = self.exporter()
        clean = module.clean(data)
        index = guest.MEDIA.restore_media(data, clean, module.clean)
        self.assertEqual(index["cases"][case_id]["events"], {"20261015T000000.000000Z-history": ["screenshot"]})
        self.assertEqual(clean["cases"][case_id]["assets"], {})
        self.assertEqual(clean["cases"][case_id]["current"]["assets"], {})
        self.assertEqual(clean["cases"][case_id]["events"][-1]["current"]["assets"], {})
        self.assertIsNone(guest.MEDIA.inspect_media("video", "relative.mp4", {}))
        self.assertIsNone(guest.MEDIA.inspect_media("video", str(self.root / "\0.mp4"), {}))

    def test_current_and_history_permissions_require_matching_case_and_event_ids(self):
        data, case_id, *_ = self.media_fixture()
        module = self.exporter()
        for mutation in ("unknown_current", "different_case", "duplicate_event", "invalid_event"):
            with self.subTest(mutation=mutation):
                changed = json.loads(json.dumps(data))
                case = changed["cases"][case_id]
                if mutation == "unknown_current":
                    case["current_event_id"] = "other-case-event"
                elif mutation == "different_case":
                    case["case_id"] = "PVR-abcdef012345"
                elif mutation == "duplicate_event":
                    case["events"].append(case["events"][0])
                else:
                    case["events"][0]["event_id"] = "../outside"
                clean = module.clean(changed)
                self.assertEqual(guest.MEDIA.restore_media(changed, clean, module.clean), {"schema": 1, "cases": {}})
                self.assertEqual(clean["cases"][case_id]["assets"], {})

    def test_media_scanner_watches_only_registered_image_and_video_files(self):
        data, case_id, video, image, private = self.media_fixture()
        folder = self.root / "skill" / "cases" / case_id / "events"
        folder.mkdir(parents=True)
        event = data["cases"][case_id]["events"][-1]
        folder.joinpath("event.json").write_text(json.dumps(event), encoding="utf-8")
        scanner = guest.GuestScanner(self.root)
        before = scanner.take()
        self.assertIn(str(video.absolute()), before)
        self.assertNotIn(str(private.absolute()), before)
        self.assertNotIn(str(image.absolute()), before)
        video.write_bytes(video.read_bytes() + b"changed")
        after = scanner.take()
        self.assertIn(str(video.absolute()), guest.SYNC.changed_files(before, after, business_only=True))
        video.unlink()
        self.assertIn(str(video.absolute()), guest.SYNC.changed_files(after, scanner.take(), business_only=True))


if __name__ == "__main__":
    unittest.main()
