"""Settings → 密钥与令牌: keys are validated, stored as user env vars, and never handed back."""
import os
import unittest
from unittest import mock

import api_keys


class ApiKeyTests(unittest.TestCase):
    def setUp(self):
        self.registry = {}
        patches = [mock.patch.object(api_keys, "_registry", side_effect=lambda name: self.registry.get(name)),
                   mock.patch.object(api_keys, "_write", side_effect=self.write),
                   mock.patch.object(api_keys, "_broadcast"),
                   mock.patch.dict(os.environ, {}, clear=False)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        for name in api_keys.KEYS:
            os.environ.pop(name, None)

    def write(self, name, value):
        if value is None:
            self.registry.pop(name, None)
        else:
            self.registry[name] = value

    def row(self, data, name):
        return next(row for row in data["keys"] if row["name"] == name)

    def test_save_shows_only_tail_and_feature_reads_it(self):
        data = api_keys.save({"name": "APIFY_TOKEN", "value": "  apify_api_abcdefghijklmnopWXYZ "})
        row = self.row(data, "APIFY_TOKEN")
        self.assertEqual((row["set"], row["tail"], row["looks_right"]), (True, "WXYZ", True))
        self.assertNotIn("abcdefghijklmnop", repr(data))
        self.assertEqual(api_keys.read("APIFY_TOKEN"), "apify_api_abcdefghijklmnopWXYZ")
        import review_scrape
        self.assertEqual(review_scrape.read_token(), "apify_api_abcdefghijklmnopWXYZ")
        data = api_keys.remove({"name": "APIFY_TOKEN"})
        self.assertFalse(self.row(data, "APIFY_TOKEN")["set"])
        self.assertEqual(api_keys.read("APIFY_TOKEN"), "")

    def test_bad_values_are_refused(self):
        for body in ({"name": "APIFY_TOKEN", "value": ""}, {"name": "APIFY_TOKEN", "value": "apify_api_ab cd"},
                     {"name": "ELEVENLABS_API_KEY", "value": "key-id-123"}, {"name": "OTHER", "value": "sk_x"}):
            with self.assertRaises(api_keys.KeyError_):
                api_keys.save(body)
        self.assertEqual(self.registry, {})

    def test_hand_set_wrong_value_is_flagged(self):
        self.registry["ELEVENLABS_API_KEY"] = "notakey1234567890"
        row = self.row(api_keys.status(), "ELEVENLABS_API_KEY")
        self.assertEqual((row["set"], row["looks_right"]), (True, False))


if __name__ == "__main__":
    unittest.main()
