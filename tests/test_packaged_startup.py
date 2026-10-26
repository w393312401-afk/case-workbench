"""A release must start using only its shipped files and default configuration."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request


APP = Path(__file__).resolve().parents[1]


class PackagedStartupTests(unittest.TestCase):
    def test_fresh_install_starts_and_imports_the_tutorial_example(self):
        with tempfile.TemporaryDirectory(prefix="workbench-fresh-install-") as folder:
            root = Path(folder).resolve()
            for source in APP.glob("*.py"):
                shutil.copy2(source, root / source.name)
            shutil.copy2(APP / "config.json", root / "config.json")
            for name in ("scripts", "static"):
                shutil.copytree(APP / name, root / name, ignore=shutil.ignore_patterns("__pycache__"))
            templates = root / "sources" / "video-cases"
            templates.mkdir(parents=True)
            shutil.copy2(APP / "sources" / "video-cases" / "case_template.md", templates / "case_template.md")
            self.assertFalse((root / "config.local.json").exists())
            log = root / "startup.log"
            with log.open("w", encoding="utf-8") as output:
                child = subprocess.Popen([sys.executable, "-X", "utf8", "-u", "server.py", "--port", "0", "--no-open"],
                                         cwd=root, stdout=output, stderr=output)
            try:
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    text = log.read_text(encoding="utf-8", errors="replace")
                    match = re.search(r"Case workbench: (http://127.0.0.1:\d+)", text)
                    if match:
                        break
                    self.assertIsNone(child.poll(), text)
                    time.sleep(.1)
                else:
                    self.fail("Fresh installation did not start: " + text)
                base = match[1]

                def request(path, body=None):
                    headers = {"Content-Type": "application/json", "X-Workbench-Token": token} if body is not None else {}
                    value = None if body is None else json.dumps(body).encode("utf-8")
                    with urllib.request.urlopen(urllib.request.Request(base + path, data=value, headers=headers), timeout=20) as response:
                        return json.load(response)

                status = request("/api/service/status")
                token = status["token"]
                self.assertEqual(Path(status["paths"]["records"]), root / ".local" / "case-library" / "cases")
                self.assertEqual(request("/api/reviews?product=DEMO")["total"], 0)
                tutorial = (APP / "docs" / "用户声音保姆级教程.md").read_text(encoding="utf-8")
                demo = json.loads(re.search(r"```json\s*([\s\S]*?)```", tutorial)[1])
                sources = root / ".local" / "runtime" / "reviews" / "sources" / "tiktok_video"
                sources.mkdir(parents=True, exist_ok=True)
                (sources / "demo.json").write_text(json.dumps(demo), encoding="utf-8")
                request("/api/reviews/sync", {})
                self.assertEqual(request("/api/reviews?product=DEMO")["total"], 3)
                self.assertEqual(request("/api/reviews?product=DEMO&q=clamp")["total"], 2)
                request("/api/service/shutdown", {})
                child.wait(timeout=15)
                self.assertEqual(child.returncode, 0)
            finally:
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=10)
