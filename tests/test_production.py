"""Production phase one: variant parsing, voice library, readings and timed voiceover takes (no network)."""
import base64
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "scripts"))

from creation_store import CreationStore
from elevenlabs_client import ElevenLabsClient, VoiceError, clean_settings
from production_store import ProductionStore, apply_readings
from variant_parse import parse_variants, spoken_lines
from workbench_production import ProductionWorkbenchMixin, map_groups, word_times

CANDIDATE = """核对范围：示例。

---

**变体 #7 · 逐件对照**

**口播(配音用)**

```
One kit, replacing this jig.
Out goes the jig.
Full 202H kit's below.
```

**读法:** 202H 读法待用户确认。

**对应中文**

```
一套，换掉这个夹具。
夹具拿走。
整套 202H 在下方。
```

**意群表**

| # | 口播 | 画面要证明什么 | 素材标签 | 证据 |
|---|---|---|---|---|
| 1 | One kit… | 旧夹具被 202H 箱替换 | 开箱 | 展示(新拍) |
| 2 | Out goes the jig. | 旧夹具移走 | 组装 | 动作演示 |
| 3 | Full 202H kit's below. | 结尾 CTA，全片唯一一次号召手势 | 闭合工具箱 | 展示 |

**发布文案**

```
🔴One Kit#SHARPAL202H #knifesharpening
```

---

**变体 #9 · 送礼**

**口播（配音用）**

```
Gift for the knife nerd?
Open this case.
```

**意群表**

| # | 口播 | 画面要证明什么 | 素材标签 | 证据 |
|---|---|---|---|---|
| 1 | Gift for the knife nerd? | 箱子推到镜头前 | 开箱 | 展示 |
"""


def alignment_for(text, gap=0.05):
    chars, starts, ends, clock = [], [], [], 0.0
    for char in text:
        chars.append(char)
        starts.append(round(clock, 3))
        clock += 0.4 if char == "\n" else gap
        ends.append(round(clock, 3))
    return {"characters": chars, "character_start_times_seconds": starts, "character_end_times_seconds": ends}


class FakeEleven:
    """Transport double for ElevenLabsClient: records calls and answers like the documented endpoints."""

    def __init__(self, *, alignment=True, voices=None):
        self.calls, self.alignment, self.voices = [], alignment, voices or {"VOICE123456789": "Zack"}

    def __call__(self, method, url, headers, data, timeout):
        path = urlparse(url).path
        self.calls.append((method, path, headers.get("xi-api-key")))
        if path.startswith("/v1/voices/"):
            voice = path.rsplit("/", 1)[-1]
            if voice not in self.voices:
                return 404, {}, json.dumps({"detail": {"status": "voice_not_found", "message": "missing"}}).encode()
            return 200, {}, json.dumps({"name": self.voices[voice], "category": "cloned", "labels": {"accent": "american"}}).encode()
        if path == "/v1/shared-voices":
            return 200, {}, b'{"voices": [], "has_more": false}'
        if path == "/v1/user/subscription":
            return 200, {}, json.dumps({"tier": "creator", "character_count": 1200, "character_limit": 100000}).encode()
        if path.endswith("/with-timestamps"):
            text = json.loads(data)["text"]
            body = {"audio_base64": base64.b64encode(b"ID3fake-audio").decode(),
                    "alignment": alignment_for(text) if self.alignment else None, "normalized_alignment": None}
            return 200, {"request-id": "req-1"}, json.dumps(body).encode()
        if path == "/v1/forced-alignment":
            text = data.split(b'name="text"\r\n\r\n', 1)[1].split(b"\r\n--", 1)[0].decode()
            raw = alignment_for(text)
            return 200, {}, json.dumps({"characters": [{"text": c, "start": s, "end": e} for c, s, e in
                                        zip(raw["characters"], raw["character_start_times_seconds"], raw["character_end_times_seconds"])],
                                        "words": [], "loss": 0.1}).encode()
        return 500, {}, b"{}"


class SharedVoiceEleven(FakeEleven):
    """An official library voice becomes visible to the account only after it is added."""

    voice_id = "goT3UYdM9bhm0n2lmKQx"
    owner = "sharedOwner123"
    metadata = {"name": "Edward - British", "category": "professional", "labels": {"accent": "british", "language": "en"},
                "preview_url": "https://example.invalid/edward.mp3"}

    def __init__(self, *, pages=None, lookup_error=None, shared_error=None, add_error=None,
                 added_id=None, visible_after_add=True):
        super().__init__()
        self.entry = {"voice_id": self.voice_id, "public_owner_id": self.owner, "name": self.metadata["name"],
                      "is_added_by_user": False, "rate": 1.0}
        self.pages = pages if pages is not None else [[self.entry]]
        self.lookup_error, self.shared_error, self.add_error = lookup_error, shared_error, add_error
        self.added_id, self.visible_after_add = added_id or self.voice_id, visible_after_add
        self.added, self.shared_queries, self.add_payloads = False, [], []

    @staticmethod
    def response(status, body):
        return status, {}, json.dumps(body).encode()

    def __call__(self, method, url, headers, data, timeout):
        parsed = urlparse(url)
        path = parsed.path
        if path in {f"/v1/voices/{self.voice_id}", f"/v1/voices/{self.added_id}"}:
            self.calls.append((method, path, headers.get("xi-api-key")))
            if self.added and self.visible_after_add and path == f"/v1/voices/{self.added_id}":
                return self.response(200, self.metadata)
            status, code = self.lookup_error or (400, "voice_not_found")
            return self.response(status, {"detail": {"status": code, "message": "voice unavailable"}})
        if path == "/v1/shared-voices":
            self.calls.append((method, path, headers.get("xi-api-key")))
            query = parse_qs(parsed.query)
            self.shared_queries.append(query)
            if self.shared_error:
                status, code = self.shared_error
                return self.response(status, {"detail": {"status": code, "message": "library unavailable"}})
            page = int(query.get("page", ["0"])[0])
            return self.response(200, {"voices": self.pages[page] if page < len(self.pages) else [],
                                       "has_more": page + 1 < len(self.pages)})
        if path == f"/v1/voices/add/{self.owner}/{self.voice_id}":
            self.calls.append((method, path, headers.get("xi-api-key")))
            self.add_payloads.append(json.loads(data))
            if self.add_error:
                status, code = self.add_error
                return self.response(status, {"detail": {"status": code, "message": "cannot add voice"}})
            self.added = True
            return self.response(200, {"voice_id": self.added_id})
        return super().__call__(method, url, headers, data, timeout)


class Bench(ProductionWorkbenchMixin):
    def __init__(self, root, transport, key="sk_test"):
        self.lock = threading.RLock()
        self.creation = CreationStore(root, self.lock)
        self.production = ProductionStore(root, self.lock)
        self.voices = ElevenLabsClient(key_provider=lambda: key, transport=transport)
        self.ai = None


class ParseTests(unittest.TestCase):
    def test_variants_split_into_lines_with_chinese_and_meaning_groups(self):
        variants = parse_variants(CANDIDATE)
        self.assertEqual([item["label"] for item in variants], ["变体 #7 · 逐件对照", "变体 #9 · 送礼"])
        first = variants[0]
        self.assertEqual(first["problems"], [])
        self.assertEqual(first["lines"][2], {"number": 3, "spoken": "Full 202H kit's below.", "zh": "整套 202H 在下方。",
                                             "proof": "结尾 CTA，全片唯一一次号召手势", "tags": "闭合工具箱", "evidence": "展示"})
        second = variants[1]
        self.assertEqual(len(second["lines"]), 2)
        self.assertTrue(any("中文" in problem for problem in second["problems"]))
        self.assertTrue(any("意群表 1 行" in problem for problem in second["problems"]))

    def test_publish_lines_and_old_formats_are_not_voiced(self):
        self.assertFalse(any("🔴" in line["spoken"] for item in parse_variants(CANDIDATE) for line in item["lines"]))
        self.assertEqual(parse_variants("**英文口播稿**\n> One line. *(拿走旧夹具)*"), [])
        self.assertEqual(spoken_lines("Line one.\n\nLine two.\n"), ["Line one.", "Line two."])

    def test_word_times_fill_unmatched_words_between_neighbours(self):
        voiced = "One kit, two oh two H.\nDone."
        exact = alignment_for(voiced)
        triples = list(zip(exact["characters"], exact["character_start_times_seconds"], exact["character_end_times_seconds"]))
        first, second = word_times(["One kit, 202H.", "Done."], triples)
        self.assertEqual(len(first), 3)
        self.assertLessEqual(first[0], first[1])
        self.assertLessEqual(first[1], first[2])
        self.assertGreater(second[0], first[2])
        self.assertEqual(word_times(["Nothing matches"], []), [None])

    def test_timing_maps_each_line_even_when_alignment_text_differs(self):
        lines = ["One kit, replacing this jig.", "Out goes the jig."]
        exact = alignment_for("\n".join(lines))
        triples = list(zip(exact["characters"], exact["character_start_times_seconds"], exact["character_end_times_seconds"]))
        groups = map_groups(lines, triples)
        self.assertEqual([group["coverage"] for group in groups], [1.0, 1.0])
        self.assertLess(groups[0]["end"], groups[1]["start"])
        normalized = [(c, s, e) for c, s, e in triples if c not in ",."]
        self.assertTrue(all(group["coverage"] > 0.9 for group in map_groups(lines, normalized)))


class StoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="production-test-")
        self.addCleanup(temp.cleanup)
        self.store = ProductionStore(temp.name)

    def body(self, **values):
        return {"request_id": uuid4().hex, "revision": self.store.public()["revision"], **values}

    def test_voice_library_needs_lookup_keeps_notes_defaults_and_disable(self):
        with self.assertRaises(VoiceError):
            self.store.mutate("voice", self.body(voice={"voice_id": "VOICE123456789", "note": "Zack 男声"}))
        saved = self.store.mutate("voice", self.body(voice={"voice_id": "VOICE123456789", "note": "Zack 男声"}), {"name": "Zack"})
        self.assertEqual((saved["voice"]["note"], saved["voice"]["name"], saved["voice"]["model_id"]), ("Zack 男声", "Zack", "eleven_v4"))
        self.assertEqual(saved["voice"]["settings"], {"stability": 0.5, "similarity_boost": 0.75})
        default = self.store.mutate("voice", self.body(voice={"voice_id": "VOICE123456789"}, operation="default", product="SHARPAL 202H"))
        self.assertEqual(default["defaults"], {"202H": "VOICE123456789"})
        disabled = self.store.mutate("voice", self.body(voice={"voice_id": "VOICE123456789"}, operation="disable"))
        self.assertEqual(disabled["defaults"], {})
        self.assertTrue(ProductionStore(self.store.root.parent).public()["voices"][0]["disabled"])

    def test_v4_rejects_speed_and_clamps_nothing(self):
        self.assertEqual(clean_settings("eleven_v4", {"stability": 0.4, "speed": 1.1}), {"stability": 0.4, "similarity_boost": 0.75})
        self.assertIn("speed", clean_settings("eleven_multilingual_v2", {"speed": 1.1}))
        with self.assertRaises(VoiceError):
            clean_settings("eleven_v4", {"stability": 2})

    def test_readings_apply_whole_words_only_and_reject_duplicates(self):
        self.store.mutate("reading", self.body(reading={"product": "202H", "written": "202H", "spoken": "two oh two H"}))
        with self.assertRaises(VoiceError):
            self.store.mutate("reading", self.body(reading={"product": "202H", "written": "202h", "spoken": "x"}))
        lines, count = apply_readings(["The 202H kit.", "Model 202HX stays."], self.store.readings("202H"))
        self.assertEqual((lines, count), (["The two oh two H kit.", "Model 202HX stays."], 1))

    def test_stale_revision_and_replayed_request(self):
        body = self.body(reading={"product": "202H", "written": "A", "spoken": "B"})
        first = self.store.mutate("reading", body)
        self.assertEqual(self.store.mutate("reading", body), first)
        with self.assertRaises(VoiceError) as caught:
            self.store.mutate("reading", {**body, "request_id": uuid4().hex})
        self.assertEqual(caught.exception.status, 409)


class VoiceLookupTests(unittest.TestCase):
    def client(self, fake):
        return ElevenLabsClient(key_provider=lambda: "sk_test", transport=fake)

    def test_account_voice_does_not_search_or_add_library_voice(self):
        fake = FakeEleven()
        self.assertEqual(self.client(fake).voice("VOICE123456789")["name"], "Zack")
        self.assertEqual([(method, path) for method, path, _ in fake.calls], [("GET", "/v1/voices/VOICE123456789")])

    def test_library_search_uses_exact_id_across_pages_before_adding(self):
        fake = SharedVoiceEleven()
        near = {**fake.entry, "voice_id": fake.voice_id + "0", "name": fake.metadata["name"]}
        fake.pages = [[near], [fake.entry]]
        voice = self.client(fake).voice(fake.voice_id)
        self.assertEqual((voice["voice_id"], voice["name"], voice["labels"]),
                         (fake.voice_id, fake.metadata["name"], fake.metadata["labels"]))
        self.assertEqual([query["page"] for query in fake.shared_queries], [["0"], ["1"]])
        for query in fake.shared_queries:
            self.assertEqual(query["search"], [fake.voice_id])
            self.assertEqual(query["page_size"], ["100"])
            self.assertEqual(query["include_custom_rates"], ["true"])
            self.assertEqual(query["include_live_moderated"], ["true"])
        self.assertEqual(fake.add_payloads, [{"new_name": fake.metadata["name"]}])
        self.assertEqual(fake.calls[-1][1], f"/v1/voices/{fake.voice_id}")

    def test_similar_name_or_id_is_not_accepted_as_the_requested_voice(self):
        fake = SharedVoiceEleven()
        fake.pages = [[{**fake.entry, "voice_id": fake.voice_id + "0"}]]
        with self.assertRaises(VoiceError) as caught:
            self.client(fake).voice(fake.voice_id)
        self.assertEqual(caught.exception.status, 404)
        self.assertTrue(any(word in str(caught.exception) for word in ("未加入", "下架", "账号")))
        self.assertEqual(fake.add_payloads, [])

    def test_search_failure_is_not_relabelled_as_an_invalid_voice_id(self):
        fake = SharedVoiceEleven(shared_error=(500, "internal_error"))
        with self.assertRaises(VoiceError) as caught:
            self.client(fake).voice(fake.voice_id)
        self.assertEqual(caught.exception.provider_status, 500)
        self.assertNotIn("找不到这个音色", str(caught.exception))
        self.assertEqual(fake.add_payloads, [])

    def test_unfinished_search_is_not_reported_as_no_matching_voice(self):
        fake = SharedVoiceEleven(pages=[[] for _ in range(11)])
        with self.assertRaises(VoiceError) as caught:
            self.client(fake).voice(fake.voice_id)
        self.assertEqual(caught.exception.status, 502)
        self.assertEqual(len(fake.shared_queries), 10)
        self.assertEqual(fake.add_payloads, [])

    def test_account_access_failures_do_not_search_or_add_shared_voices(self):
        for status, code, expected, word in ((401, "invalid_api_key", 503, "密钥"),
                                             (403, "missing_permissions", 403, "权限")):
            with self.subTest(status=status):
                fake = SharedVoiceEleven(lookup_error=(status, code))
                with self.assertRaises(VoiceError) as caught:
                    self.client(fake).voice(fake.voice_id)
                self.assertEqual(caught.exception.status, expected)
                self.assertIn(word, str(caught.exception))
                self.assertEqual(fake.shared_queries, [])
                self.assertEqual(fake.add_payloads, [])

    def test_voice_slots_and_add_permissions_have_actionable_errors(self):
        for status, code, expected, word in ((400, "maximum_voice_limit_reached", 409, "音色名额"),
                                             (400, "voice_limit_reached", 409, "音色名额"),
                                             (403, "missing_permissions", 403, "权限")):
            with self.subTest(code=code):
                fake = SharedVoiceEleven(add_error=(status, code))
                with self.assertRaises(VoiceError) as caught:
                    self.client(fake).voice(fake.voice_id)
                self.assertEqual(caught.exception.status, expected)
                self.assertIn(word, str(caught.exception))
                self.assertEqual(len(fake.add_payloads), 1)
                self.assertFalse(fake.added)

    def test_different_account_id_is_verified_and_reported_without_silent_substitution(self):
        fake = SharedVoiceEleven(added_id="differentVoice123456")
        with self.assertRaises(VoiceError) as caught:
            self.client(fake).voice(fake.voice_id)
        self.assertEqual(caught.exception.status, 409)
        self.assertIn(fake.added_id, str(caught.exception))
        self.assertEqual(fake.calls[-1][1], f"/v1/voices/{fake.added_id}")
        self.assertEqual(len(fake.add_payloads), 1)

    def test_already_added_but_inaccessible_voice_is_not_added_again(self):
        fake = SharedVoiceEleven()
        fake.pages = [[{**fake.entry, "is_added_by_user": True}]]
        with self.assertRaises(VoiceError) as caught:
            self.client(fake).voice(fake.voice_id)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(fake.add_payloads, [])

    def test_other_resource_404_does_not_claim_the_voice_id_is_wrong(self):
        client = self.client(lambda *args: (404, {}, b'{"detail": "not found"}'))
        with self.assertRaises(VoiceError) as caught:
            client.subscription()
        self.assertEqual(caught.exception.provider_status, 404)
        self.assertNotIn("音色 ID", str(caught.exception))


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="production-bench-")
        self.addCleanup(temp.cleanup)
        self.fake = FakeEleven()
        self.bench = Bench(temp.name, self.fake)

    def adopt(self, text, variant_text=CANDIDATE):
        source = {"task_id": "AIT-0123456789ab", "round_id": "AIR-0123456789ab", "task_revision": 3, "product": "202H",
                  "case_id": None, "candidate_text": variant_text, "candidate_sha256": "x", "snapshot": {}}
        body = {"request_id": uuid4().hex, "revision": self.bench.creation.public()["revision"],
                "script": {"product": "202H", "direction_id": "202H-S2", "title": "逐件对照", "text": text,
                           "translation_zh": "", "confirmed": True}}
        return self.bench.creation.mutate("script", body, source)["script"]

    def job(self, script, index=None):
        body = {"request_id": uuid4().hex, "revision": self.bench.production.public()["revision"],
                "job": {"script_id": script["id"], "variant_index": index}}
        return self.bench.create_production_job(body)["job"]

    def test_job_freezes_matching_variant_lines_and_meaning_groups(self):
        script = self.adopt("One kit, replacing this jig.\nOut goes the jig.\nFull 202H kit's below.")
        job = self.job(script, 1)  # wrong hint: the matching variant is still found by its lines
        self.assertEqual((job["variant_label"], job["source"]["variant_index"], job["status"]), ("变体 #7 · 逐件对照", 0, "draft"))
        self.assertEqual(job["lines"][0]["tags"], "开箱")
        manual = self.job(self.adopt("A brand new line."))
        self.assertEqual(manual["lines"][0]["proof"], "")
        self.assertTrue(manual["problems"])

    def test_generate_take_times_groups_adds_voice_and_select(self):
        self.bench.production.mutate("reading", {"request_id": uuid4().hex, "revision": self.bench.production.public()["revision"],
                                                 "reading": {"product": "202H", "written": "202H", "spoken": "two oh two H"}})
        job = self.job(self.adopt("One kit, replacing this jig.\nOut goes the jig.\nFull 202H kit's below."))
        body = {"request_id": uuid4().hex, "voice_id": "VOICE123456789", "model_id": "eleven_v4", "settings": {"stability": 0.45}}
        result = self.bench.generate_take(job["id"], body)
        take = result["take"]
        self.assertEqual(take["tts_lines"][2], "Full two oh two H kit's below.")
        self.assertEqual(take["alignment_source"], "tts")
        self.assertEqual([group["number"] for group in take["groups"]], [1, 2, 3])
        self.assertTrue(all(group["coverage"] == 1.0 for group in take["groups"]))
        self.assertTrue((self.bench.production.audio / take["audio"]).is_file())
        self.assertEqual(self.bench.generate_take(job["id"], body), result)
        self.assertEqual(sum(1 for call in self.fake.calls if call[1].endswith("/with-timestamps")), 1)
        voice = self.bench.production.voice("VOICE123456789")
        self.assertEqual((voice["name"], voice["use_count"]), ("Zack", 1))
        selected = self.bench.select_take(job["id"], {"request_id": uuid4().hex, "revision": self.bench.production.public()["revision"],
                                                      "take_id": take["id"]})
        self.assertEqual((selected["job"]["status"], selected["job"]["selected_take"]), ("voiced", take["id"]))
        self.assertTrue(self.bench.production_audio(job["id"], take["id"], "mp3").is_file())
        words = self.bench.production_take_words(job["id"], take["id"])
        self.assertEqual(words["source"], "tts")
        self.assertEqual([len(line["words"]) for line in words["lines"]], [5, 4, 4])
        starts = [time for line in words["lines"] for time in line["words"]]
        self.assertEqual(starts, sorted(starts))
        group = take["groups"][2]
        # "202H" is voiced as "two oh two H"; its word still lands inside its own line.
        self.assertTrue(group["start"] <= words["lines"][2]["words"][1] <= group["end"])

    def test_missing_timestamps_fall_back_to_forced_alignment(self):
        self.fake.alignment = False
        job = self.job(self.adopt("One kit, replacing this jig.\nOut goes the jig.\nFull 202H kit's below."))
        take = self.bench.generate_take(job["id"], {"request_id": uuid4().hex, "voice_id": "VOICE123456789"})["take"]
        self.assertEqual(take["alignment_source"], "forced")
        self.assertTrue(all(line["words"] for line in self.bench.production_take_words(job["id"], take["id"])["lines"]))
        self.assertTrue(all(group["start"] is not None for group in take["groups"]))

    def test_key_problems_are_reported_without_calling_speech(self):
        job = self.job(self.adopt("One line."))
        for key, words in (("", "没有找到"), ("91d0a0b0c0", "sk_")):
            self.bench.voices.key_provider = lambda key=key: key
            with self.assertRaises(VoiceError) as caught:
                self.bench.generate_take(job["id"], {"request_id": uuid4().hex, "voice_id": "VOICE123456789"})
            self.assertIn(words, str(caught.exception))
        self.assertEqual(self.fake.calls, [])
        self.assertFalse(self.bench.eleven_status()["ok"])

    def test_unknown_voice_is_rejected_before_spending_characters(self):
        job = self.job(self.adopt("One line."))
        with self.assertRaises(VoiceError) as caught:
            self.bench.generate_take(job["id"], {"request_id": uuid4().hex, "voice_id": "UNKNOWN1234567"})
        self.assertEqual(caught.exception.status, 404)
        self.assertFalse(any(call[1].endswith("/with-timestamps") for call in self.fake.calls))
        self.assertEqual(self.bench.production.job(job["id"])["takes"], [])

    def test_shared_british_voice_is_added_saved_and_reused_for_a_take(self):
        fake = SharedVoiceEleven()
        self.bench.voices = ElevenLabsClient(key_provider=lambda: "sk_test", transport=fake)
        saved = self.bench.save_voice({"request_id": uuid4().hex, "revision": self.bench.production.public()["revision"],
                                       "voice": {"voice_id": fake.voice_id, "note": "英国男声"}})["voice"]
        self.assertEqual((saved["voice_id"], saved["name"], saved["labels"], saved["preview_url"]),
                         (fake.voice_id, fake.metadata["name"], fake.metadata["labels"], fake.metadata["preview_url"]))
        self.assertEqual(saved["note"], "英国男声")
        job = self.job(self.adopt("One line."))
        take = self.bench.generate_take(job["id"], {"request_id": uuid4().hex, "voice_id": fake.voice_id, "market": "uk"})["take"]
        self.assertEqual((take["voice_id"], take["market"]), (fake.voice_id, "uk"))
        self.assertEqual(len(fake.add_payloads), 1)
        self.assertEqual(len(fake.shared_queries), 1)
        self.assertEqual(sum(path.endswith("/with-timestamps") for _, path, _ in fake.calls), 1)

    def test_added_voice_must_be_accessible_before_spending_characters(self):
        fake = SharedVoiceEleven(visible_after_add=False)
        self.bench.voices = ElevenLabsClient(key_provider=lambda: "sk_test", transport=fake)
        job = self.job(self.adopt("One line."))
        with self.assertRaises(VoiceError):
            self.bench.generate_take(job["id"], {"request_id": uuid4().hex, "voice_id": fake.voice_id})
        self.assertEqual(len(fake.add_payloads), 1)
        self.assertFalse(any(path.endswith("/with-timestamps") for _, path, _ in fake.calls))
        self.assertEqual(self.bench.production.job(job["id"])["takes"], [])


if __name__ == "__main__":
    unittest.main()
