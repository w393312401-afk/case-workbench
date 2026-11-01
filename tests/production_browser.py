"""Browser fixture for pages of the production area, in an isolated library."""
import unittest
from uuid import uuid4

import test_server as fixture
import test_ui_regressions as browser

CANDIDATE = """**变体 #1 · 送礼**

**口播（配音用）**

```
This kit keeps the angle for you.
Seven plates, one clamp.
```

**对应中文**

```
这套工具替你保持角度。
七块磨板，一个夹具。
```
"""


class ProductionBrowserTests(unittest.TestCase):
    setUpClass = classmethod(browser.WorkbenchBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(browser.WorkbenchBrowserTests.tearDownClass.__func__)
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    open_page = browser.WorkbenchBrowserTests.open_page
    make_playable_video = browser.WorkbenchBrowserTests.make_playable_video

    def setUp(self):
        browser.WorkbenchBrowserTests.setUp(self)
        self.app = self.server.app
        source = {"task_id": "AIT-0123456789ab", "round_id": "AIR-0123456789ab", "task_revision": 1,
                  "product": "202H", "case_id": None, "candidate_text": CANDIDATE,
                  "candidate_sha256": "x", "snapshot": {}}
        self.script = self.app.creation.mutate("script", {
            "request_id": uuid4().hex, "revision": self.app.creation.public()["revision"],
            "script": {"product": "202H", "direction_id": "202H-S2", "title": "送礼",
                       "text": "This kit keeps the angle for you.\nSeven plates, one clamp.", "confirmed": True}}, source)["script"]
