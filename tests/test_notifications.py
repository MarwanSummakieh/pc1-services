"""Run under dbus-run-session with the Linux notification dependencies installed."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

try:
    import dbus
    import gi
except ImportError:
    raise unittest.SkipTest('Notification checks require Linux python3-dbus and python3-gobject')

SERVER = Path(os.environ.get('MARWANOS_NOTIFICATION_SERVER',
    str(Path(__file__).parents[1] / 'os/files/usr/lib/marwanos/notifications/server.py')))
spec = importlib.util.spec_from_file_location('notification_server', SERVER)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class InboxTests(unittest.TestCase):
    def test_replace_close_restart_and_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'notifications.json'
            inbox = module.Inbox(path)
            ident = inbox.notify('FDM', 0, 'Complete', '日本語 "file"')
            self.assertEqual(inbox.notify('FDM', ident, 'Updated', 'new'), ident)
            self.assertEqual(len(inbox.entries), 1)
            other = inbox.notify('Other', ident, 'Other app', 'body')
            self.assertNotEqual(other, ident)
            self.assertTrue(inbox.close(ident))
            self.assertFalse(inbox.close(ident))
            restored = module.Inbox(path)
            self.assertTrue(restored.entries[0]['closed'])
            for number in range(60):
                restored.notify('FDM', 0, str(number), 'body')
            self.assertEqual(len(restored.entries), 50)
            self.assertEqual(module.Inbox(path).next_id, restored.next_id)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    @unittest.skipUnless(os.environ.get('MARWANOS_NOTIFICATION_TEST_BUS') == '1',
                         'Run with MARWANOS_NOTIFICATION_TEST_BUS=1 under dbus-run-session')
    def test_dbus_and_wine_spool(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, XDG_RUNTIME_DIR=directory, XDG_STATE_HOME=directory, XDG_DATA_HOME=directory)
            process = subprocess.Popen(['/usr/bin/python3', str(SERVER)], env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                bus = dbus.SessionBus()
                deadline = time.monotonic() + 10
                while not bus.name_has_owner(module.INTERFACE):
                    if process.poll() is not None:
                        self.fail(process.stderr.read().decode())
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.1)
                service = dbus.Interface(bus.get_object(module.INTERFACE,
                    '/org/freedesktop/Notifications'), module.INTERFACE)
                self.assertIn('persistence', service.GetCapabilities())
                ident = service.Notify('Test', 0, '', 'Test complete', '日本語', [], {}, 0)
                service.CloseNotification(ident)
                spool = Path(directory) / 'marwanos/notification-events'
                (spool / 'fdm-test.json.tmp').write_text('{partial')
                (spool / 'fdm-test.json').write_text(json.dumps(
                    {'summary': 'Download complete', 'body': 'alpha.bin'}))
                state = Path(directory) / 'marwanos/notifications.json'
                deadline = time.monotonic() + 5
                while len(json.loads(state.read_text())['entries']) < 2:
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.1)
                entries = json.loads(state.read_text())['entries']
                self.assertTrue(entries[0]['closed'])
                self.assertEqual(entries[1]['app'], 'FDM Controller')
                self.assertEqual(entries[1]['body'], 'alpha.bin')
                self.assertTrue((spool / 'fdm-test.json.tmp').exists())
                self.assertFalse((spool / 'fdm-test.json').exists())
            finally:
                process.terminate()
                process.communicate(timeout=5)


if __name__ == '__main__':
    unittest.main()
