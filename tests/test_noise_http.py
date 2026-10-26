import unittest
from unittest.mock import patch

import test_server as fixture


class NoisePathsHTTPTests(unittest.TestCase):
    setUp = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def test_paths_are_independent_of_case_catalogue_and_do_not_return_tokens(self):
        with patch.object(self.server.app, 'state', side_effect=AssertionError('heavy catalogue must not load')):
            status, _, result = self.request('GET', '/api/settings/paths')
        self.assertEqual(status, 200)
        self.assertEqual(set(result), {'paths'})
        self.assertEqual(set(result['paths']), {'records', 'directions', 'creation', 'evidence', 'library'})
        self.assertEqual(result['paths']['evidence'], str(self.runtime / 'evidence'))
