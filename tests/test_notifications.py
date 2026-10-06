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
    def test_completed_download_payload_survives_notification_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'notifications.json'
            inbox = module.Inbox(path)
            download = {'path': '/var/home/player/Downloads/日本語 game', 'torrent': True,
                        'untrusted_command': 'must not enter the shell handoff'}
            inbox.notify('Downloads', 0, 'Download complete', 'Ready to install', download=download)
            entry = module.Inbox(path).entries[0]
            self.assertEqual(entry['app'], 'Downloads')
            self.assertEqual(entry['download'], {'path': download['path'], 'torrent': True})
            inbox.notify('Downloads', 0, 'Malformed handoff', 'body',
                         download={'path': ['/tmp/game.exe'], 'torrent': True})
            self.assertNotIn('download', module.Inbox(path).entries[-1])
            inbox.notify('Downloads', 0, 'Regular file', 'body',
                         download={'path': '/tmp/game.exe', 'torrent': 'true'})
            self.assertFalse(module.Inbox(path).entries[-1]['download']['torrent'])

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
                # Exercise the same real user-bus server with new provider events,
                # rather than testing an isolated dictionary transformation.
                (spool / '00-invalid-shape.json').write_text('[]')
                (spool / '00-invalid-app.json').write_text(json.dumps(
                    {'app': {'command': 'invalid'}, 'summary': 'Malformed event', 'body': 'body'}))
                (spool / 'achievement-test.json').write_text(json.dumps(
                    {'app': 'Achievements', 'summary': 'Achievement unlocked: First step',
                     'body': 'TEKKEN 8\n日本語 achievement'}))
                download = {'path': '/var/home/player/Downloads/TEKKEN 8', 'torrent': True}
                (spool / 'download-test.json').write_text(json.dumps(
                    {'app': 'Downloads', 'summary': 'Download ready', 'body': 'Ready to install',
                     'download': {**download, 'exec': 'never persisted'}}))
                deadline = time.monotonic() + 5
                pending = ['achievement-test.json', 'download-test.json',
                           '00-invalid-shape.json', '00-invalid-app.json']
                while (len(json.loads(state.read_text())['entries']) < 4 or
                       any((spool / name).exists() for name in pending)):
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.1)
                received = {entry['summary']: entry for entry in json.loads(state.read_text())['entries']}
                unlock = received['Achievement unlocked: First step']
                self.assertEqual(unlock['app'], 'Achievements')
                self.assertEqual(unlock['body'], 'TEKKEN 8\n日本語 achievement')
                self.assertNotIn('download', unlock)
                ready = received['Download ready']
                self.assertEqual(ready['app'], 'Downloads')
                self.assertEqual(ready['download'], download)
                self.assertFalse((spool / 'achievement-test.json').exists())
                self.assertFalse((spool / 'download-test.json').exists())
                self.assertFalse((spool / '00-invalid-shape.json').exists())
                self.assertFalse((spool / '00-invalid-app.json').exists())
                self.assertNotIn('Malformed event', received,
                                 'invalid event shapes must not block subsequent providers')
                time.sleep(.6)
                self.assertEqual(len(json.loads(state.read_text())['entries']), 4,
                                 'consumed achievement/download events must not replay')
            finally:
                process.terminate()
                process.communicate(timeout=5)


if __name__ == '__main__':
    unittest.main()
