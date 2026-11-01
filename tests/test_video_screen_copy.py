"""Publication screen copy follows the adopted delivery shown in the job."""
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from elevenlabs_client import VoiceError
from variant_parse import screen_copy
from workbench_video_publish import VideoPublicationStore, VideoPublishMixin


def delivery(screen, spoken="Try this."):
    return f"""开头画面：展示产品
**开头屏显：** {screen}

**口播（配音用）**
```
{spoken}
```
**对应中文**
```
试试这个。
```
"""


def title_bench(folder, titles):
    bench = VideoPublishMixin()
    bench.production = SimpleNamespace(root=folder, job=lambda _: {})
    bench._video_publish_copy = lambda _: {'titles': titles}
    return bench


class VideoScreenCopyTests(unittest.TestCase):
    def test_extracts_screen_and_translation_without_copying_chinese(self):
        self.assertEqual(screen_copy(delivery('`Care for your EDC / A mirror from one kit`（照顾你的 EDC / 一套磨出镜面）')),
                         ('Care for your EDC / A mirror from one kit', '照顾你的 EDC / 一套磨出镜面'))
        self.assertEqual(screen_copy('开头屏显：Sharp (really sharp)（锋利）'), ('Sharp (really sharp)', '锋利'))
        self.assertEqual(screen_copy('开头屏显：中文和 EDC 混在一起'), ('', '中文和 EDC 混在一起'))
        self.assertEqual(screen_copy('\n'.join(['```', '开头屏显：do not extract', '```'])), ('', ''))
        self.assertEqual(screen_copy('**开头屏显**：First（一）\n屏显：Second（二）'), ('First\nSecond', '一\n二'))

    def test_writer_notes_never_pass_as_screen_text(self):
        self.assertEqual(screen_copy('开头屏显：`Knife nerd on your list?`(你的清单上有刀迷吗?)。提问型,单行。'),
                         ('Knife nerd on your list?', '你的清单上有刀迷吗?'))
        self.assertEqual(screen_copy('开头屏显：无开头屏显（原片只有逐句字幕，没有另放标题）。'), ('', ''))
        self.assertEqual(screen_copy('开头屏显：无开头屏显。原片靠口播字幕和动作开场。'), ('', ''))

    def test_title_chinese_is_translated_once_and_keeps_tags_out(self):
        with tempfile.TemporaryDirectory() as folder:
            titles = ['🔴Coarse to Fine#SHARPAL202H #knife', '🔴Your Pace#SHARPAL202H']
            calls = []
            bench = title_bench(folder, titles)
            def translated(_, lines, __):
                calls.append(lines)
                return ['从粗到细', '你的节奏']
            with patch('case_breakdown_runner.translate_lines', translated):
                first = bench.translate_video_titles('PRD-test', {})
                second = bench.translate_video_titles('PRD-test', {})
            self.assertEqual(calls, [['Coarse to Fine', 'Your Pace']])
            self.assertEqual(first, {'titles_zh': ['从粗到细', '你的节奏']})
            self.assertEqual(second, first)

    def test_failed_title_translation_reports_and_saves_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            bench = title_bench(folder, ['🔴Your Pace#tag'])
            with patch('case_breakdown_runner.translate_lines', lambda *_: None), self.assertRaises(VoiceError):
                bench.translate_video_titles('PRD-test', {})
            self.assertFalse((VideoPublicationStore(folder).root / 'title-translations.json').exists())

    def test_source_follows_original_script_and_rejects_stale_candidate(self):
        script = {'id': 'SCR-test', 'version': 1, 'text': 'Try this.', 'translation_zh': '试试。',
                  'source': {'candidate_text': delivery('`Candidate`（候选）')}}
        bench = SimpleNamespace(creation=SimpleNamespace(get_script=lambda _: script))
        job = {'script_id': 'SCR-test', 'source': {'variant_index': 0},
               'lines': [{'spoken': 'Try this.', 'zh': '试试。'}],
               'supplement': {'hook_text': 'Manual', 'hook_text_zh': '手动'}}
        get = lambda: VideoPublishMixin._video_publish_copy(bench, job)
        self.assertEqual((get()['screen_en'], get()['screen_zh']), ('Candidate', '候选'))
        job['fill_markdown'] = delivery('`Filled`（补全）')
        self.assertEqual((get()['screen_en'], get()['screen_zh']), ('Filled', '补全'))
        job['fill_markdown'] = delivery('无')
        self.assertEqual((get()['screen_en'], get()['screen_zh']), ('', ''))
        job['fill_markdown'] = ''
        job['lines'][0]['spoken'] = 'Changed.'
        self.assertEqual((get()['screen_en'], get()['screen_zh']), ('Manual', '手动'))


if __name__ == '__main__':
    unittest.main()
