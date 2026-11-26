"""「开始制作」 strings voice → select → timeline → edit together once, and stops where a person is needed."""
import unittest

from elevenlabs_client import VoiceError
from produce_chain import ProduceChain

VOICE = "A" * 20
REQUEST = "request-0123456789"


class FakeStore:
    def __init__(self, job, creators=("Lab", "Zack")):
        self.data = {"jobs": [job], "settings": {"creators": list(creators)}}

    def records(self):
        return self.data

    def job(self, identifier):
        return next(row for row in self.data["jobs"] if row["id"] == identifier)

    @staticmethod
    def revision(data):
        return "rev"


class FakeApp:
    def __init__(self, job, *, quality="passed", queued=False, edit_id="EDT-000000000001"):
        self.production = FakeStore(job)
        self.calls, self.quality, self.queued, self.edit_id = [], quality, queued, edit_id

    def _voicing(self):
        return set()

    def generate_take(self, identifier, body):
        self.calls.append(("take", body))
        self.production.job(identifier).setdefault("takes", []).append(
            {"id": "TAK-new", "created_at": "2026-11-25", "quality": {"status": self.quality}})

    def select_take(self, identifier, body):
        self.calls.append(("select", body))
        self.production.job(identifier)["selected_take"] = body["take_id"]

    def build_timeline(self, identifier, body):
        self.calls.append(("timeline", body))
        return {"queued": True} if self.queued else {"timeline": {"unique_id": "uid-new"}}

    def start_edit(self, identifier, body):
        self.calls.append(("edit", body))
        return {"edit": {"id": self.edit_id}} if self.edit_id else {}


def job(**extra):
    return {"id": "PRD-000000000001", "product": "202H", "takes": [], "timelines": [], "edits": [], **extra}


def body(**extra):
    return {"request_id": REQUEST, "creator": "Lab", "voice_id": VOICE, "model_id": "eleven_v4", "settings": {},
            "materials": ["蓝点折刀成品素材"], **extra}


class ProduceChainTests(unittest.TestCase):
    def chain(self, app):
        return ProduceChain(app, start_thread=False)

    def test_runs_all_four_steps_once_and_clears_itself(self):
        app = FakeApp(job())
        chain = self.chain(app)
        chain.start("PRD-000000000001", body())
        self.assertEqual([name for name, _ in app.calls], ["take", "select", "timeline", "edit"])
        self.assertEqual(app.calls[1][1]["take_id"], "TAK-new")
        self.assertEqual(app.calls[2][1]["creator"], "Lab")
        self.assertEqual(app.calls[3][1]["timeline"], "uid-new")
        self.assertEqual(app.calls[3][1]["materials"], ["蓝点折刀成品素材"])
        self.assertEqual(app.calls[3][1]["mode"], "explain")
        # Once the edit is queued it reports for itself; the chain leaves nothing behind.
        self.assertIsNone(chain.status("PRD-000000000001"))

    def test_a_queued_build_hands_its_request_to_the_edit(self):
        app = FakeApp(job(), queued=True)
        self.chain(app).start("PRD-000000000001", body())
        self.assertEqual(app.calls[3][1]["timeline_request_id"], f"{REQUEST}-build")
        self.assertNotIn("timeline", app.calls[3][1])

    def test_a_take_that_failed_the_check_is_not_selected(self):
        app = FakeApp(job(), quality="failed")
        chain = self.chain(app)
        chain.start("PRD-000000000001", body())
        self.assertEqual([name for name, _ in app.calls], ["take"])
        state = chain.status("PRD-000000000001")
        self.assertFalse(state["running"])
        self.assertIn("没有自动选用", state["note"])
        self.assertEqual(state["error"], "")

    def test_without_material_the_edit_waits_for_a_person(self):
        app = FakeApp(job())
        chain = self.chain(app)
        chain.start("PRD-000000000001", body(materials=[]))
        self.assertEqual([name for name, _ in app.calls], ["take", "select", "timeline"])
        self.assertIn("勾选素材", chain.status("PRD-000000000001")["note"])

    def test_a_selected_voiceover_is_not_paid_for_again(self):
        app = FakeApp(job(selected_take="TAK-old", takes=[{"id": "TAK-old"}]))
        self.chain(app).start("PRD-000000000001", {"request_id": REQUEST, "creator": "Zack", "materials": ["混剪素材"]})
        self.assertEqual([name for name, _ in app.calls], ["timeline", "edit"])

    def test_an_uncut_timeline_of_this_voiceover_is_reused(self):
        timeline = {"unique_id": "uid-built", "take_id": "TAK-old", "creator": "Lab", "name": "Lab-1"}
        app = FakeApp(job(selected_take="TAK-old", takes=[{"id": "TAK-old"}], timelines=[timeline]))
        self.chain(app).start("PRD-000000000001", {"request_id": REQUEST, "creator": "Lab", "materials": ["混剪素材"]})
        self.assertEqual([name for name, _ in app.calls], ["edit"])
        self.assertEqual(app.calls[0][1]["timeline"], "uid-built")

    def test_a_failure_says_which_step_and_can_be_dismissed(self):
        app = FakeApp(job())
        app.build_timeline = lambda identifier, request: (_ for _ in ()).throw(VoiceError(409, "达芬奇开着别的项目"))
        chain = self.chain(app)
        chain.start("PRD-000000000001", body())
        state = chain.status("PRD-000000000001")
        self.assertEqual(state["stage"], "timeline")
        self.assertIn("建时间线没走完", state["error"])
        self.assertIn("达芬奇开着别的项目", state["error"])
        chain.dismiss("PRD-000000000001")
        self.assertIsNone(chain.status("PRD-000000000001"))

    def test_refuses_bad_requests_before_spending_anything(self):
        app = FakeApp(job())
        chain = self.chain(app)
        for wrong in (body(creator="Nobody"), body(voice_id="short"), body(request_id="x"), body(materials="a"),
                      body(mode="replica")):
            with self.assertRaises(VoiceError):
                chain.start("PRD-000000000001", wrong)
        with self.assertRaises(VoiceError):
            self.chain(FakeApp(job(archived=True))).start("PRD-000000000001", body())
        self.assertEqual(app.calls, [])

    def test_a_running_chain_is_not_started_twice(self):
        app = FakeApp(job())
        chain = ProduceChain(app, start_thread=False)
        chain.runs["PRD-000000000001"] = {"stage": "voice", "running": True, "error": "", "note": "", "since": "", "request_id": "other-request-1"}
        with self.assertRaises(VoiceError) as caught:
            chain.start("PRD-000000000001", body())
        self.assertEqual(caught.exception.status, 409)
        # The same request asked again reads the state instead of starting over.
        chain.runs["PRD-000000000001"]["request_id"] = REQUEST
        self.assertEqual(chain.start("PRD-000000000001", body())["producing"]["stage"], "voice")
        self.assertEqual(app.calls, [])


if __name__ == "__main__":
    unittest.main()
