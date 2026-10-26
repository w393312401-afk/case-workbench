from datetime import datetime
from pathlib import Path
import tempfile
import unittest

from scripts.log_summary import summarize


class LogSummaryTests(unittest.TestCase):
    def test_filters_status_time_path_and_never_keeps_query_or_body(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "server.log"
            path.write_text('\n'.join([
                '127.0.0.1 - - [23/Oct/2026 09:00:00] "GET /api/state?token=secret HTTP/1.1" 200 -',
                '127.0.0.1 - - [23/Oct/2026 10:00:00] "POST /api/production?token=secret HTTP/1.1" 409 -',
                '127.0.0.1 - - [23/Oct/2026 11:00:00] "POST /api/production?token=other HTTP/1.1" 409 -',
                '127.0.0.1 - - [23/Oct/2026 12:00:00] "GET /api/other HTTP/1.1" 403 -',
                'body=password=do-not-print',
            ]), encoding='utf-8')
            result = summarize(path, since=datetime(2026, 10, 23, 9, 30), contains='/api/production')
            self.assertEqual(result['matched_requests'], 2)
            self.assertEqual(result['groups'][0]['path'], '/api/production')
            self.assertEqual(result['groups'][0]['status'], 409)
            self.assertNotIn('secret', str(result))
            self.assertNotIn('password', str(result))
            self.assertEqual(summarize(path, status='403')['matched_requests'], 1)
            self.assertEqual(summarize(path, status='all')['matched_requests'], 4)
