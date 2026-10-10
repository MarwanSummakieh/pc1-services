"""Native queue policy and real aria2 HTTP/restart/torrent-metadata checks."""
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'os/files/usr/lib/marwanos/downloads/manager.py'
spec = importlib.util.spec_from_file_location('native_downloads', SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class FakeEngine:
    def __init__(self):
        self.calls, self.results = [], {}

    def call(self, name, *args):
        self.calls.append((name, args))
        if name == 'tellStatus':
            if args[0] not in self.results:
                raise module.TransferError('Unknown GID')
            return self.results[args[0]]
        return args[-1].get('gid', '') if name in ('addUri', 'addTorrent') else 'OK'


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.engine = FakeEngine()
        self.manager = module.Manager(self.engine, self.root / 'data', self.root / 'runtime',
                                      self.root / 'Downloads', self.root / 'spool')

    def add(self, **extra):
        self.manager.handle({'action': 'add', 'source': 'https://example.com/game.exe', **extra})
        return self.manager.tasks[-1]

    def test_sources_and_duplicates(self):
        for source in ['file:///etc/passwd', 'ftp://example.com/a', 'https://a:b@example.com/a',
                       'magnet:?xt=urn:btih:invalid', 'https://example.com/a\n--out=other']:
            with self.subTest(source=source), self.assertRaises(module.TransferError):
                module.source_info(source)
        task = self.add()
        self.assertTrue(Path(task['directory']).is_relative_to(self.root / 'Downloads'))
        with self.assertRaisesRegex(module.TransferError, 'already'):
            self.add()

    def test_pause_resume_and_persistence(self):
        task = self.add(paused=True)
        self.assertEqual(self.engine.calls[0][1][-1]['pause'], 'true')
        self.manager.handle({'action': 'resume', 'target': task['id']})
        self.assertEqual(task['status'], 'waiting')
        self.manager.handle({'action': 'pause', 'target': task['id']})
        restored = module.Manager(self.engine, self.root / 'data', self.root / 'runtime',
                                  self.root / 'Downloads', self.root / 'spool')
        restored.restore()
        self.assertEqual(restored.tasks[0]['status'], 'paused')
        self.assertEqual(self.engine.calls[-1][1][-1]['pause'], 'true')

    def test_torrent_selection_requires_pause_and_one_file(self):
        task = self.add(paused=True)
        task.update(kind='torrent', files=[{'index': '1'}, {'index': '2'}])
        for indices in [[], ['3'], [1], '1']:
            with self.assertRaises(module.TransferError):
                self.manager.handle({'action': 'select', 'target': task['id'], 'indices': indices})
        self.manager.handle({'action': 'select', 'target': task['id'], 'indices': ['2']})
        self.assertEqual(task['selection'], ['2'])
        task['status'] = 'active'
        with self.assertRaises(module.TransferError):
            self.manager.handle({'action': 'select', 'target': task['id'], 'indices': ['1']})

    def test_remove_retains_payload(self):
        task = self.add()
        payload = Path(task['directory']) / 'partial.bin'
        payload.write_bytes(b'keep me')
        self.manager.handle({'action': 'remove', 'target': task['id']})
        self.assertEqual(payload.read_bytes(), b'keep me')
        self.assertEqual(self.manager.tasks, [])

    def test_unreadable_queue_is_not_silently_replaced(self):
        path = self.root / 'data/queue.json'
        path.write_text('{damaged')
        with self.assertRaises(module.TransferError):
            module.Manager(self.engine, self.root / 'data', self.root / 'runtime', self.root / 'Downloads', self.root / 'spool')
        self.assertEqual(path.read_text(), '{damaged')

    def result(self, task, **extra):
        return {'status': 'active', 'downloadSpeed': '128', 'files': [
            {'index': '1', 'path': str(Path(task['directory']) / 'game.exe'), 'length': '10',
             'completedLength': '10', 'selected': 'true'}], **extra}

    def test_completion_spool_once_and_private_sources(self):
        task = self.add(source='https://example.com/game.exe?token=private')
        self.engine.results[task['gid']] = self.result(task, status='complete')
        self.manager.update()
        event = json.loads(next((self.root / 'spool').glob('*.json')).read_text())
        self.assertEqual(event['app'], 'Downloads')
        self.assertFalse(event['download']['torrent'])
        receipt = json.loads(next((self.root / 'windows/requests').glob('*.json')).read_text())
        self.assertEqual(receipt['verb'], 'download')
        self.assertEqual(receipt['path'], event['download']['path'])
        next((self.root / 'spool').glob('*.json')).unlink()
        self.manager.update()
        self.assertEqual(list((self.root / 'spool').glob('*.json')), [])
        self.assertNotIn('private', (self.root / 'runtime/state.json').read_text())

    def test_magnet_follows_payload_and_seeding_notification(self):
        task = self.add(source='magnet:?xt=urn:btih:' + 'a' * 40)
        self.engine.results[task['gid']] = {'status': 'complete', 'followedBy': ['payload']}
        self.engine.results['payload'] = self.result(task, seeder='true', bittorrent={'info': {'name': 'Game'}})
        self.manager.update()
        self.assertEqual(task['gid'], 'payload')
        self.assertEqual(task['status'], 'seeding')
        self.assertEqual(task['name'], 'Game')
        event = json.loads(next((self.root / 'spool').glob('*.json')).read_text())
        self.assertTrue(event['download']['torrent'])
        self.manager.handle({'action': 'stop-seeding', 'target': task['id']})
        self.assertEqual(task['status'], 'complete')

    def test_request_spool_keeps_distinct_actions_and_reports_invalid(self):
        folder = self.root / 'runtime/requests'
        module.atomic(folder / '1.json', {'id': '1', 'action': 'add', 'source': 'https://example.com/one'})
        module.atomic(folder / '2.json', {'id': '2', 'action': 'add', 'source': 'https://example.com/two'})
        module.atomic(folder / '3.json', {'id': '3', 'action': 'add', 'source': 'bad'})
        (folder / 'partial.tmp').write_text('{')
        self.manager.requests()
        self.assertEqual(len(self.manager.tasks), 2)
        self.assertEqual(self.manager.request_id, '3')
        self.assertIn('link', self.manager.error)
        self.assertTrue((folder / 'partial.tmp').exists())


ARIA2 = os.environ.get('MARWANOS_ARIA2_BIN') or shutil.which('aria2c')
LOCAL_ONLY = ('enable-dht=false', 'enable-dht6=false', 'bt-enable-lpd=false')


@unittest.skipUnless(ARIA2, 'aria2c not installed')
class RealEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.payload = b'native MarwanOS download\0' * 65536
        payload = self.payload

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                start = 0
                if self.headers.get('Range'):
                    start = int(self.headers['Range'].split('=')[1].split('-')[0])
                self.send_response(206 if start else 200)
                self.send_header('Content-Length', str(len(payload) - start))
                self.send_header('Accept-Ranges', 'bytes')
                if start:
                    self.send_header('Content-Range', f'bytes {start}-{len(payload)-1}/{len(payload)}')
                self.end_headers()
                try:
                    for i in range(start, len(payload), 16384):
                        self.wfile.write(payload[i:i + 16384])
                        self.wfile.flush()
                        time.sleep(0.01)
                except (OSError, ConnectionError):
                    pass

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.engine = module.Engine(self.root / 'data', ARIA2, LOCAL_ONLY)
        self.manager = module.Manager(self.engine, self.root / 'data', self.root / 'runtime', self.root / 'Downloads', self.root / 'spool')

    def tearDown(self):
        self.engine.close()
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            self.manager.update()
            if predicate(): return
            time.sleep(0.05)
        self.fail('Transfer did not reach the expected state: ' + str(self.manager.tasks))

    def test_http_pause_restart_resume_hash_and_completion(self):
        self.manager.handle({'action': 'add', 'source': f'http://127.0.0.1:{self.server.server_port}/fixture.bin'})
        task = self.manager.tasks[0]
        self.wait_for(lambda: task['received'] > 0)
        self.manager.handle({'action': 'pause', 'target': task['id']})
        self.wait_for(lambda: task['status'] == 'paused')
        self.engine.close()
        self.engine = module.Engine(self.root / 'data', ARIA2, LOCAL_ONLY)
        self.manager = module.Manager(self.engine, self.root / 'data', self.root / 'runtime', self.root / 'Downloads', self.root / 'spool')
        self.manager.restore()
        task = self.manager.tasks[0]
        self.manager.update()
        self.assertEqual(task['status'], 'paused')
        self.manager.handle({'action': 'resume', 'target': task['id']})
        self.wait_for(lambda: task['status'] == 'complete')
        path = Path(task['files'][0]['path'])
        self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), hashlib.sha256(self.payload).digest())
        self.assertTrue(task['notified'])
        self.engine.close()
        self.engine = module.Engine(self.root / 'data', ARIA2, LOCAL_ONLY)
        self.manager = module.Manager(self.engine, self.root / 'data', self.root / 'runtime', self.root / 'Downloads', self.root / 'spool')
        self.manager.restore()
        self.assertEqual(self.engine.call('tellActive'), [])
        self.assertEqual(self.engine.call('tellWaiting', 0, 100), [])

    def test_local_torrent_import_selection_and_restart(self):
        # Two local synthetic files: only metadata is loaded, no public tracker.
        info = b'd5:filesld6:lengthi4e4:pathl5:a.bineed6:lengthi4e4:pathl5:b.bineee4:name7:fixture12:piece lengthi16384e6:pieces20:' + hashlib.sha1(b'aaaabbbb').digest() + b'e'
        path = self.root / 'fixture.torrent'
        path.write_bytes(b'd4:info' + info + b'e')
        self.manager.handle({'action': 'torrent', 'source': str(path), 'paused': True})
        task = self.manager.tasks[0]
        self.manager.update()
        self.assertEqual(len(task['files']), 2)
        self.manager.handle({'action': 'select', 'target': task['id'], 'indices': ['2']})
        self.engine.close()
        self.engine = module.Engine(self.root / 'data', ARIA2, LOCAL_ONLY)
        self.manager = module.Manager(self.engine, self.root / 'data', self.root / 'runtime', self.root / 'Downloads', self.root / 'spool')
        self.manager.restore()
        self.manager.update()
        self.assertEqual([f['selected'] for f in self.manager.tasks[0]['files']], [False, True])

    def test_torrent_webseed_transfers_and_verifies_payload(self):
        name = b'fixture.bin'
        pieces = b''.join(hashlib.sha1(self.payload[i:i + 16384]).digest() for i in range(0, len(self.payload), 16384))
        info = (b'd6:lengthi' + str(len(self.payload)).encode() + b'e4:name' + str(len(name)).encode() + b':' + name +
                b'12:piece lengthi16384e6:pieces' + str(len(pieces)).encode() + b':' + pieces + b'e')
        seed = f'http://127.0.0.1:{self.server.server_port}/fixture.bin'.encode()
        path = self.root / 'webseed.torrent'
        path.write_bytes(b'd4:info' + info + b'8:url-list' + str(len(seed)).encode() + b':' + seed + b'e')
        self.manager.handle({'action': 'torrent', 'source': str(path)})
        task = self.manager.tasks[0]
        self.wait_for(lambda: task['status'] in ('complete', 'seeding'))
        self.assertEqual(hashlib.sha256(Path(task['files'][0]['path']).read_bytes()).digest(), hashlib.sha256(self.payload).digest())
        self.assertTrue(task['notified'])
        self.manager.handle({'action': 'remove', 'target': task['id']})
        self.engine.close()
        self.engine = module.Engine(self.root / 'data', ARIA2, LOCAL_ONLY)
        self.manager = module.Manager(self.engine, self.root / 'data', self.root / 'runtime', self.root / 'Downloads', self.root / 'spool')
        self.manager.restore()
        self.assertEqual(self.engine.call('tellActive'), [])
        self.assertEqual(self.engine.call('tellWaiting', 0, 100), [])


if __name__ == '__main__':
    unittest.main()
