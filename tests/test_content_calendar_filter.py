"""Reviewed-video calendar filters exclude incomplete and unknown calendar records."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import test_content_filters as filter_fixture
from content_views import ContentViewsStore
from creation_store import CreationError
from workbench_content import _parse_content_filter


rule = filter_fixture.rule
custom = filter_fixture.custom


class ContentCalendarFilterTests(unittest.TestCase):
    catalog = filter_fixture.ContentFilterTests.catalog
    ids = filter_fixture.ContentFilterTests.ids

    def setUp(self):
        self.app = filter_fixture.MemoryContent()

        def video(identifier, calendar, **changes):
            return {"id": identifier, "kind": "video", "stage": "awaiting_publication",
                    "title": identifier, "product": "202H", "updated_at": "2026-10-23",
                    "workflow_steps": [{"key": "edit", "done": True}],
                    "calendar_uploaded": calendar, **changes}

        self.app.items = [
            video("video:added", True),
            video("video:pending", False),
            video("video:unknown", None),
            video("video:string", "false"),
            video("video:number", 0),
            video("video:unfinished", True, workflow_steps=[{"key": "edit", "done": False}]),
            video("video:expired", False, workflow_steps=[{"key": "edit", "done": False}]),
            video("video:missing", False, workflow_steps=[{"key": "timeline", "done": True}]),
            video("carousel:approved", False, kind="carousel"),
            video("script:pending", False, kind="script"),
            video("video:archived", True, archived=True),
            video("video:history", False, history=True, stage="published"),
        ]

    def test_calendar_choices_require_current_completed_video_review_and_known_boolean(self):
        self.assertEqual(self.ids(self.catalog(custom(rule("review_calendar", "added")))),
                         {"video:added", "video:archived"})
        self.assertEqual(self.ids(self.catalog(custom(rule("review_calendar", "not_added")))),
                         {"video:pending", "video:history"})

    def test_multiple_calendar_values_and_all_any_compose_with_existing_conditions(self):
        self.assertEqual(self.ids(self.catalog(custom(rule("review_calendar", "added", "not_added")))),
                         {"video:added", "video:pending", "video:archived", "video:history"})
        self.assertEqual(self.ids(self.catalog(custom(rule("review_calendar", "added"),
                                                     rule("status", "archived")))), {"video:archived"})
        self.assertEqual(self.ids(self.catalog(custom(rule("review_calendar", "not_added"),
                                                     rule("kind", "carousel"), match="any"))),
                         {"video:pending", "video:history", "carousel:approved"})

    def test_calendar_condition_persists_and_invalid_values_cannot_replace_saved_view(self):
        with tempfile.TemporaryDirectory(prefix="calendar-filter-test-") as temporary:
            store = ContentViewsStore(Path(temporary), filter_validator=_parse_content_filter)
            record = {"id": "review-calendar", "name": "审查后待添加日历", "q": "",
                      "filter": custom(rule("review_calendar", "not_added"))}
            saved = store.mutate({"product": "202H", "action": "save", "view": record,
                                  "revision": store.public("202H")["revision"]})
            reopened = ContentViewsStore(Path(temporary), filter_validator=_parse_content_filter)
            self.assertEqual(reopened.public("202H"), saved)
            self.assertEqual(saved["views"], [record])
            for invalid in ("unknown", "", False, None):
                with self.subTest(invalid=invalid), self.assertRaises(CreationError) as error:
                    store.mutate({"product": "202H", "action": "save", "revision": saved["revision"],
                                  "view": {**record, "filter": custom(rule("review_calendar", invalid))}})
                self.assertEqual(error.exception.status, 400)
            self.assertEqual(reopened.public("202H"), saved)


if __name__ == "__main__":
    unittest.main()
