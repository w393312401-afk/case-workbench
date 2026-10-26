"""The retrospective funnel judges each step against its peers' medians and says when it cannot."""
from operator import itemgetter
import unittest

from workbench_feedback import draft_funnel, review_draft

MEDIANS = {"finish_rate_pct": {"median": 3.76, "videos": 12, "pool": "direction"}, "ctr_pct": {"median": 1.71, "videos": 12, "pool": "direction"},
           "ctor_pct": {"median": 0.0, "videos": 12, "pool": "direction"}, "ctor_pct_selling": {"median": 1.52, "videos": 40, "pool": "product"},
           "period": {"start": "2026-07-12", "end": "2026-09-06"}, "direction_id": "202H-S1"}
VIEWS = {"window_views": {"median": 2842, "videos": 30, "pool": "direction"}}


class RetroFunnelTests(unittest.TestCase):
    def test_busiest_week_is_judged_and_the_first_weak_rate_is_the_drop(self):
        weeks = [{"start": "2026-08-24", "end": "2026-08-30", "views": 969, "finish_rate_pct": 6.19, "ctr_pct": 0.93, "ctor_pct": 0},
                 {"start": "2026-08-31", "end": "2026-09-06", "views": 62, "finish_rate_pct": 8.06, "ctr_pct": 0, "ctor_pct": 0}]
        funnel = draft_funnel({"window_views": 1031}, weeks, {**MEDIANS, **VIEWS})
        steps = {row["key"]: row for row in funnel["steps"]}
        # Each step names the group it was compared with; conversion fell back to the product here.
        self.assertEqual(("同方向", 30), (steps["views"]["pool"], steps["views"]["peers"]))
        self.assertEqual(("同产品", 40), (steps["ctor"]["pool"], steps["ctor"]["peers"]))
        self.assertEqual(("2026-07-12", "202H-S1"), (funnel["period"]["start"], funnel["direction_id"]))
        self.assertEqual("2026-08-24", funnel["week"]["start"])
        self.assertEqual(("weak", 0.36), (steps["views"]["verdict"], steps["views"]["ratio"]))
        self.assertEqual(("strong", 6.19), (steps["finish"]["verdict"], steps["finish"]["value"]))
        self.assertEqual("weak", steps["ctr"]["verdict"])
        # About 9 clicks at the selling median would bring 0.14 orders: zero orders says nothing.
        self.assertEqual("unknown", steps["ctor"]["verdict"])
        self.assertIn("约 9 次点击，按同类转化预计约 0.1 单", steps["ctor"]["note"])
        # Not judged, but still drawn: the bar shows the figures that exist.
        self.assertEqual(0.0, steps["ctor"]["ratio"])
        self.assertEqual("ctr", funnel["drop"])

    def test_thin_weeks_and_missing_reference_are_unknown_not_guessed(self):
        weeks = [{"start": "a", "end": "b", "views": 120, "finish_rate_pct": 1.0, "ctr_pct": 0.2, "ctor_pct": 0}]
        funnel = draft_funnel({"window_views": 130}, weeks, MEDIANS)
        self.assertTrue(all(row["verdict"] == "unknown" for row in funnel["steps"]))
        self.assertIn("只有 120 次播放", funnel["steps"][1]["note"])
        self.assertEqual("近期还没有可对照的视频", funnel["steps"][0]["note"])
        self.assertIsNone(funnel["drop"])
        empty = draft_funnel({"window_views": None}, [], {})
        self.assertIsNone(empty["week"])
        self.assertEqual("没有每周数据", empty["steps"][2]["note"])

    def test_flat_and_strong_steps_leave_exposure_as_the_only_drop(self):
        weeks = [{"start": "a", "end": "b", "views": 20000, "finish_rate_pct": 3.8, "ctr_pct": 1.8, "ctor_pct": 2.0}]
        funnel = draft_funnel({"window_views": 900}, weeks, {**MEDIANS, **VIEWS})
        self.assertEqual(["weak", "flat", "flat", "strong"], [row["verdict"] for row in funnel["steps"]])
        self.assertEqual("views", funnel["drop"])

    def test_draft_reads_its_card_next_to_the_closing_lines(self):
        output = ('## 一句话\n...\n```retro-card\n{"verdict": "卡在点击", "reasons": [{"at": "21 秒", "point": "没价格", "fix": "加价格"}]}\n```\n'
                  "结论：点击偏弱\n建议动作：换执行")
        draft = review_draft({"rounds": [{"status": "completed", "output": output}]})
        self.assertEqual(("点击偏弱", "execution"), (draft["conclusion"], draft["action"]))
        self.assertEqual("卡在点击", draft["card"]["verdict"])
        self.assertIsNone(review_draft({"rounds": [{"status": "completed", "output": "结论：x\n建议动作：无结论"}]})["card"])

    def test_draft_reads_its_recommended_reference_value(self):
        read = lambda line: review_draft({"rounds": [{"status": "completed", "output": "结论：开头有效，没成交\n" + line}]})
        self.assertEqual(("medium", "播放高于同类，只出 2 单"), itemgetter("reference_value", "reference_reason")(read("**参考价值：** 中等（播放高于同类，只出 2 单）")))
        self.assertEqual(("none", ""), itemgetter("reference_value", "reference_reason")(read("参考价值：无")))
        self.assertNotIn("reference_value", read("参考价值：看情况"))
        self.assertNotIn("reference_value", read(""))


class PeerReferenceTests(unittest.TestCase):
    """Peers are the same direction's videos of the eight weeks up to the window end, per figure falling back to the product."""

    def workbench(self, rows, directions):
        from workbench_feedback import FeedbackWorkbenchMixin
        calls = []

        class Performance:
            def videos(self):
                return rows

            def rate_medians(self, ids, since, until):
                calls.append((sorted(ids), since, until))
                return {"ctr_pct": {"median": float(len(ids)), "videos": len(ids)}}

        class Bench(FeedbackWorkbenchMixin):
            performance = Performance()

            def _video_directions(self, product, scripts=None):
                return directions
        return Bench(), calls

    def test_direction_peers_in_the_recent_window_and_product_fallback(self):
        video = {"video_id": "1", "product": "202H", "window_end": "2026-09-06"}
        row = lambda vid, posted, views=1000, status="watch": {"video_id": vid, "product": "202H", "posted_on": posted,
                                                                "window_views": views, "status": status}
        rows = [row("1", "2026-08-28"), *(row(str(n), "2026-08-01", 500 * n) for n in range(2, 8)),
                row("8", "2026-06-01", 99999), row("9", "2026-08-10", 5, "incomplete")]
        directions = {str(n): "S1" for n in range(1, 9)}
        bench, calls = self.workbench(rows, directions)
        reference = bench.peer_reference(video, "202H")
        self.assertEqual({"start": "2026-07-12", "end": "2026-09-06"}, reference["period"])
        # Six same-direction videos posted in the window (not the video itself, not the June one): their median.
        self.assertEqual({"median": 2250, "videos": 6, "pool": "direction"}, reference["window_views"])
        self.assertEqual(("direction", "2026-07-12", "2026-09-06"), (reference["ctr_pct"]["pool"], calls[-1][1], calls[-1][2]))
        self.assertNotIn("1", calls[-1][0])
        bench, _ = self.workbench(rows, {"1": "S1", "2": "S1"})
        thin = bench.peer_reference(video, "202H")
        self.assertEqual("product", thin["window_views"]["pool"])
        self.assertEqual("S1", thin["direction_id"])
        bench, _ = self.workbench(rows, {})
        self.assertEqual(("", "product"), (bench.peer_reference(video, "202H")["direction_id"], bench.peer_reference(video, "202H")["ctr_pct"]["pool"]))


if __name__ == "__main__":
    unittest.main()
