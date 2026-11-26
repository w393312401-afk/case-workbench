import unittest

from release_naming import parse_timeline, release_filename, release_name


class ReleaseNamingTests(unittest.TestCase):
    def test_video_and_carousel_names(self):
        self.assertEqual("🇺🇸 ▶️ 202-Zack-1124-1", release_name("Zack261124-202-1", "us", "video", creator="Zack"))
        self.assertEqual("🇩🇪 🧩 198-Zack-1124-2B", release_name("Zack261124-198-2", "de", "photo", "B", "Zack"))
        # A foreign version A-DE keeps its letter; the flag says the language.
        self.assertEqual("🇩🇪 🧩 198-Zack-1124-2A", release_name("Zack261124-198-2", "de", "photo", "A-DE"))
        self.assertEqual("🇬🇧 ▶️ 202-Lab-1201-12", release_name("Lab261201-202-12", "uk", "video"))
        # Records from before markets existed are American.
        self.assertEqual("🇺🇸 ▶️ 202-Zack-1124-1", release_name("Zack261124-202-1", None, "video"))

    def test_a_note_after_the_number_stays_after_it(self):
        self.assertEqual("🇺🇸 🧩 202-Zack-1105-1B-家里", release_name("Zack261105-202-1-家里", "us", "photo", "B"))

    def test_file_names_have_no_emoji(self):
        self.assertEqual("DE-198-Zack-1124-2B", release_filename("Zack261124-198-2", "de", "B-DE", "Zack"))
        self.assertEqual("US-202-Zack-1124-1", release_filename("Zack261124-202-1", "us"))

    def test_hand_named_timelines_keep_their_name(self):
        self.assertIsNone(parse_timeline("开箱对比 最终版"))
        self.assertIsNone(parse_timeline("Zack261124-202-1", creator="Lab"))
        self.assertEqual("🇺🇸 🧩 开箱对比 最终版 B", release_name("开箱对比  最终版", "us", "photo", "B"))
        self.assertEqual("ES-a_b-B", release_filename("a/b", "es", "B"))


if __name__ == "__main__":
    unittest.main()
