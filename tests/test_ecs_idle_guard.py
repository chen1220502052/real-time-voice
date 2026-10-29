import contextlib
import fcntl
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import ecs_idle_guard as guard


class IdleGuardTests(unittest.TestCase):
    def test_only_idle_healthy_server_can_stop(self):
        self.assertTrue(guard.can_stop(2000, 100, False, 0, True)[0])
        for args in [(2000, 201, False, 0, True),
                     (2000, 100, True, 0, True),
                     (2000, 100, False, 1, True),
                     (2000, 100, False, 0, False)]:
            self.assertFalse(guard.can_stop(*args)[0])

    def test_activity_log_parses_latest_and_fails_closed_on_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'log'
            path.write_text('{"t":10}\n{"t":20.5}\npartial')
            os.utime(path, (15, 15))
            self.assertEqual(guard.recent_access(path), 20.5)
            path.write_text('bad data\n')
            with self.assertRaises(ValueError):
                guard.recent_access(path)
            path.write_text('')
            os.utime(path, (100, 100))
            self.assertEqual(guard.recent_access(path), 100)

    def test_maintenance_lease_blocks_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            with (runtime / 'lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_SH)
                with patch.object(guard, 'RUNTIME', runtime), \
                     patch.object(guard, 'request_stop') as stop, \
                     patch.object(guard, 'health') as health, \
                     contextlib.redirect_stdout(io.StringIO()):
                    guard.check()
                    stop.assert_not_called()
                    health.assert_not_called()

    def test_new_traffic_between_checks_aborts_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            with patch.object(guard, 'RUNTIME', runtime), \
                 patch.object(guard.time, 'time', return_value=10000), \
                 patch.object(guard.Path, 'read_text', return_value='10000 0'), \
                 patch.object(guard, 'recent_access', side_effect=[0, 9999]), \
                 patch.object(guard, 'health', return_value={'ok': True, 'resources': {'inUse': 0}}), \
                 patch.object(guard, 'connections', return_value=False), \
                 patch.object(guard, 'request_stop') as stop, \
                 contextlib.redirect_stdout(io.StringIO()):
                guard.check()
                stop.assert_not_called()


if __name__ == '__main__':
    unittest.main()
