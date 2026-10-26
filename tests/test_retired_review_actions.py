"""Only current review actions can come back from newly generated AI drafts."""
import unittest

import test_feedback_chain as fixture
from workbench_feedback import review_draft


class ReviewDraftActionTests(unittest.TestCase):
    def test_only_exact_current_final_action_is_actionable(self):
        for label, action in (("加码", "scale"), ("换执行", "execution"), ("停测。", "stop"),
                              ("继续观察", ""), ("本轮暂无发现", ""), ("无结论", ""), ("停测失败则继续观察", "")):
            with self.subTest(label=label):
                parsed = review_draft({"rounds": [{"status": "completed", "output": f"结论：保留原判断\n建议动作：{label}"}]})
                self.assertEqual(parsed["action"], action)
                self.assertEqual(parsed["conclusion"], "保留原判断")

    def test_old_or_missing_last_action_never_reactivates_earlier_action(self):
        result = review_draft({"rounds": [{"status": "completed", "output": "建议动作：加码\n结论：尚不足以安排下一轮\n建议动作：继续观察"}]})
        self.assertEqual(result["action"], "")
        self.assertEqual(review_draft({"rounds": [{"status": "completed", "output": "结论：数据待补"}]})["action"], "")


class ReviewDraftPromptTests(unittest.TestCase):
    setUp = fixture.FeedbackChainHTTPTests.setUp
    stop_server = fixture.FeedbackChainHTTPTests.stop_server
    request = fixture.FeedbackChainHTTPTests.request
    no_download = staticmethod(fixture.FeedbackChainHTTPTests.no_download)
    ok = fixture.FeedbackChainHTTPTests.ok
    task = fixture.FeedbackChainHTTPTests.task

    def test_posted_video_and_fallback_prompts_offer_only_current_actions(self):
        task, _ = self.task(review_video={"product": "202H", "video_id": self.video_id, "window_end": self.window})
        app = self.server.app
        media_prompt = app.ai.bridge.calls[-1]["prompt"]
        raw = app.ai._read(task["task_id"])
        fallback_prompt = app.ai._prompt(raw)
        for prompt in (media_prompt, fallback_prompt):
            with self.subTest(kind="media" if prompt == media_prompt else "fallback"):
                self.assertNotIn("继续观察", prompt)
                self.assertNotIn("本轮暂无发现", prompt)
                self.assertIn("加码、换执行、停测三选一", prompt)
                self.assertIn("动作可省略", prompt)
                self.assertIn("不输出建议动作行", prompt)
                self.assertIn("closing_allowed", prompt)


if __name__ == "__main__":
    unittest.main()
