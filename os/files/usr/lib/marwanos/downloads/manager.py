#!/usr/bin/python3
"""Player-owned native transfers. aria2 owns bytes; MarwanOS owns the queue/UI."""
import base64
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import subprocess
import time
from urllib.parse import parse_qs, unquote, urlsplit
from urllib.request import ProxyHandler, Request, build_opener


class TransferError(Exception):
    pass


def atomic(path, value, durable=False):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False)
        if durable:
            stream.flush()
            os.fsync(stream.fileno())
    temporary.chmod(0o600)
    temporary.replace(path)


def source_info(source):
    if not isinstance(source, str) or not source or len(source) > 16384 or any(ord(c) < 32 for c in source):
        raise TransferError('Enter an HTTP, HTTPS or magnet link.')
    try:
        parts = urlsplit(source)
        if parts.scheme == 'magnet':
            query = parse_qs(parts.query)
            hashes = [v[9:].lower() for v in query.get('xt', []) if v.startswith('urn:btih:')]
            if not hashes or not re.fullmatch(r'(?:[a-f0-9]{40}|[a-z2-7]{32})', hashes[0]):
                raise ValueError()
            return 'torrent', query.get('dn', ['Magnet download'])[0], 'magnet:' + hashes[0]
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
            raise ValueError()
        _ = parts.port
        return 'http', unquote(Path(parts.path).name) or parts.hostname, source
    except ValueError:
        raise TransferError('Enter an HTTP, HTTPS or BitTorrent v1 magnet link.') from None


class Engine:
    def __init__(self, folder, binary='aria2c', extra_options=()):
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.secret = secrets.token_hex(32)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        self.url = f'http://127.0.0.1:{port}/jsonrpc'
        self.opener = build_opener(ProxyHandler({}))
        session = folder / 'aria2.session'
        session.touch(exist_ok=True)
        config = folder / 'aria2.conf'
        config.write_text('\n'.join([
            'enable-rpc=true', 'rpc-listen-all=false', 'rpc-allow-origin-all=false',
            f'rpc-listen-port={port}', f'rpc-secret={self.secret}',
            # Restore only tasks in our queue, never stale/removed session entries.
            f'save-session={session}', 'save-session-interval=15', 'auto-save-interval=15',
            'force-save=false', 'continue=true', 'allow-overwrite=false', 'auto-file-renaming=true',
            'check-integrity=true', 'file-allocation=none', 'max-concurrent-downloads=3',
            'max-connection-per-server=4', 'split=4', 'seed-ratio=1.0',
            'bt-save-metadata=true', 'bt-load-saved-metadata=true', 'follow-torrent=false', 'follow-metalink=false',
            'no-netrc=true', 'enable-color=false', 'console-log-level=warn',
            f'stop-with-process={os.getpid()}', 'summary-interval=0', 'download-result=hide',
            f'dht-file-path={folder / "dht.dat"}', f'dht-file-path6={folder / "dht6.dat"}',
            *extra_options,
        ]) + '\n', encoding='utf-8')
        config.chmod(0o600)
        self.process = subprocess.Popen([binary, f'--conf-path={config}'],
                                        stdin=subprocess.DEVNULL)
        for _ in range(100):
            try:
                self.call('getVersion')
                return
            except TransferError:
                if self.process.poll() is not None:
                    break
                time.sleep(0.05)
        self.close()
        raise TransferError('Downloads engine could not start.')

    def call(self, method, *params):
        data = json.dumps({'jsonrpc': '2.0', 'id': 'marwanos', 'method': 'aria2.' + method,
                           'params': ['token:' + self.secret, *params]}).encode()
        try:
            with self.opener.open(Request(self.url, data, {'Content-Type': 'application/json'}), timeout=3) as response:
                result = json.load(response)
            if 'error' in result:
                raise TransferError(str(result['error'].get('message', 'Transfer action failed.')))
            return result['result']
        except (OSError, ValueError, KeyError) as exc:
            raise TransferError('Downloads engine is unavailable. Try again.') from exc

    def close(self):
        try:
            self.call('saveSession')
            self.call('shutdown')
        except TransferError:
            pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=5)


class Manager:
    def __init__(self, engine, data, runtime, downloads, spool, install_requests=None):
        self.engine, self.data, self.runtime = engine, Path(data), Path(runtime)
        self.downloads, self.spool = Path(downloads), Path(spool)
        self.install_requests = Path(install_requests) if install_requests is not None else self.data.parent / 'windows/requests'
        self.error, self.request_id = '', ''
        try:
            self.tasks = json.loads((self.data / 'queue.json').read_text(encoding='utf-8'))['tasks']
            if not isinstance(self.tasks, list) or any(not isinstance(t, dict) or not {'id', 'gid', 'identity', 'source', 'status', 'kind', 'directory', 'files'}.issubset(t) for t in self.tasks):
                raise ValueError()
        except FileNotFoundError:
            self.tasks = []
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise TransferError('The saved download queue could not be read. Existing files have been kept.') from exc
        for folder in (self.data, self.runtime / 'requests', self.downloads, self.spool):
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)

    def save(self):
        atomic(self.data / 'queue.json', {'tasks': self.tasks}, durable=True)

    def options(self, task):
        result = {'dir': task['directory'], 'pause': 'true' if task['status'] == 'paused' else 'false',
                  'pause-metadata': 'true' if task['status'] == 'paused' else 'false',
                  'gid': task['gid']}
        if task.get('selection'):
            result['select-file'] = ','.join(task['selection'])
        return result

    def enqueue(self, task):
        options = self.options(task)
        if task.get('metadata_path'):
            metadata = base64.b64encode(Path(task['metadata_path']).read_bytes()).decode('ascii')
            return self.engine.call('addTorrent', metadata, [], options)
        return self.engine.call('addUri', [task['source']], options)

    def add(self, request):
        source = request.get('source', '')
        metadata = ''
        if request.get('action') == 'torrent':
            path = Path(source)
            if path.suffix.lower() != '.torrent' or path.is_symlink() or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
                raise TransferError('Choose a readable .torrent file smaller than 8 MB.')
            metadata = base64.b64encode(path.read_bytes()).decode('ascii')
            import hashlib
            kind, name, identity = 'torrent', path.stem, 'torrent:' + hashlib.sha256(metadata.encode()).hexdigest()
        else:
            kind, name, identity = source_info(source)
        if any(t['identity'] == identity for t in self.tasks):
            raise TransferError('This download is already in the queue.')
        if len(self.tasks) >= 200:
            raise TransferError('Queue is full. Remove finished entries first.')
        ident = secrets.token_hex(8)
        metadata_path = ''
        if metadata:
            folder = self.data / 'torrents'
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = folder / (ident + '.torrent')
            path.write_bytes(base64.b64decode(metadata))
            path.chmod(0o600)
            metadata_path = str(path)
        label = re.sub(r'[^\w .()-]', '_', name)[:80].strip(' .') or 'Download'
        directory = self.downloads / (label + '-' + ident[:8])
        directory.mkdir(mode=0o700)
        task = {'id': ident, 'gid': ident, 'identity': identity, 'name': name[:512],
                'source': source, 'metadata_path': metadata_path, 'kind': kind, 'directory': str(directory),
                'status': 'paused' if request.get('paused') is True else 'waiting',
                'files': [], 'total': 0, 'received': 0, 'speed': 0, 'upload_speed': 0,
                'error': '', 'notified': False, 'created_at': time.time()}
        self.enqueue(task)
        self.tasks.append(task)
        self.save()

    def restore(self):
        for task in self.tasks:
            if task['status'] in ('complete', 'error'):
                continue
            try:
                self.engine.call('tellStatus', task['gid'])
            except TransferError:
                try:
                    self.enqueue(task)
                except TransferError as exc:
                    task.update(status='error', error=str(exc))
        self.save()

    def handle(self, request):
        if not isinstance(request, dict):
            raise TransferError('Invalid download request.')
        action = request.get('action')
        if action in ('add', 'torrent'):
            self.add(request)
            return
        if action == 'refresh':
            return
        task = next((t for t in self.tasks if t['id'] == request.get('target')), None)
        if task is None:
            raise TransferError('This download is no longer in the queue.')
        gid, status = task['gid'], task['status']
        if action == 'pause' and status in ('active', 'waiting', 'seeding'):
            self.engine.call('forcePause', gid)
            task['status'] = 'paused'
        elif action == 'resume' and status == 'paused':
            self.engine.call('unpause', gid)
            task['status'] = 'waiting'
        elif action == 'retry' and status == 'error':
            retry = dict(task, gid=secrets.token_hex(8), status='waiting', error='')
            self.enqueue(retry)
            task.update(retry)
        elif action == 'stop-seeding' and status == 'seeding':
            self.engine.call('forceRemove', gid)
            task['status'] = 'complete'
        elif action == 'select' and status == 'paused' and task['kind'] == 'torrent':
            indices = request.get('indices')
            known = {f['index'] for f in task['files']}
            if not isinstance(indices, list) or not indices or any(not isinstance(v, str) or v not in known for v in indices):
                raise TransferError('Keep at least one torrent file selected.')
            self.engine.call('changeOption', gid, {'select-file': ','.join(indices)})
            task['selection'] = indices
        elif action == 'remove':
            if status not in ('complete', 'error'):
                self.engine.call('forceRemove', gid)
            self.tasks.remove(task)
        else:
            raise TransferError('This action is unavailable. Refresh the queue and try again.')
        self.engine.call('saveSession')
        self.save()

    def completed(self, task):
        if task['notified']:
            return
        paths = [f['path'] for f in task['files'] if f['selected']]
        path = paths[0] if len(paths) == 1 else task['directory']
        atomic(self.spool / (task['id'] + '.json'), {'app': 'Downloads', 'summary': 'Download complete',
               'body': task['name'], 'download': {'path': path, 'torrent': task['kind'] == 'torrent'}})
        # The receipt must reach the worker even while the shell is stopped.
        # The worker validates Downloads containment/file identity and deduplicates it.
        atomic(self.install_requests / ('download-' + task['id'] + '.json'),
               {'verb': 'download', 'path': path, 'torrent': task['kind'] == 'torrent'}, durable=True)
        task['notified'] = True

    def update(self):
        before = json.dumps(self.tasks, sort_keys=True)
        for task in self.tasks:
            if task['status'] in ('complete', 'error'):
                continue
            result = self.engine.call('tellStatus', task['gid'])
            if result.get('followedBy'):
                # A magnet's metadata is a different aria2 transfer from its payload.
                task['gid'] = result['followedBy'][0]
                result = self.engine.call('tellStatus', task['gid'])
            files = []
            for item in result.get('files', []):
                path = Path(item.get('path', ''))
                if not path.is_absolute() or not path.is_relative_to(Path(task['directory'])):
                    continue
                files.append({'index': str(item['index']), 'path': str(path),
                              'name': str(path.relative_to(task['directory'])),
                              'total': int(item['length']), 'received': int(item['completedLength']),
                              'selected': item['selected'] == 'true'})
            total = sum(f['total'] for f in files if f['selected'])
            received = sum(f['received'] for f in files if f['selected'])
            status = result['status']
            if result.get('seeder') == 'true' and status == 'active':
                status = 'seeding'
            # Magnet metadata does not represent a completed payload.
            is_metadata = task['source'].startswith('magnet:') and not result.get('bittorrent', {}).get('info')
            if is_metadata and status == 'complete':
                status = 'waiting'
            task.update(status=status, files=files, total=total, received=received,
                        speed=int(result.get('downloadSpeed', 0)), upload_speed=int(result.get('uploadSpeed', 0)),
                        error=str(result.get('errorMessage', ''))[:1024])
            if result.get('bittorrent', {}).get('info', {}).get('name'):
                task['name'] = result['bittorrent']['info']['name'][:512]
            if not is_metadata and status in ('complete', 'seeding'):
                self.completed(task)
        if before != json.dumps(self.tasks, sort_keys=True):
            self.save()
        # Do not expose links with credentials/query tokens or uploaded metadata to UI.
        public = [{k: v for k, v in t.items() if k not in ('source', 'metadata_path', 'identity')} for t in self.tasks]
        atomic(self.runtime / 'state.json', {'available': True, 'updated_at': time.time(),
               'tasks': public, 'error': self.error, 'request_id': self.request_id})

    def requests(self):
        for path in sorted((self.runtime / 'requests').glob('*.json'))[:20]:
            try:
                if path.is_symlink() or path.stat().st_size > 32768:
                    raise TransferError('Invalid download request.')
                request = json.loads(path.read_text(encoding='utf-8'))
                self.request_id = str(request.get('id', '')) if isinstance(request, dict) else ''
                self.error = ''
                self.handle(request)
            except (OSError, ValueError, TransferError, TypeError) as exc:
                self.error = str(exc)[:1024]
            finally:
                path.unlink(missing_ok=True)


def main():
    os.umask(0o077)
    data = Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'marwanos/downloads'
    runtime = Path(os.environ['XDG_RUNTIME_DIR']) / 'marwanos/downloads'
    spool = data.parent / 'notification-events'
    # Validate our queue before aria2 starts restoring any saved transfers.
    windows = Path(os.environ.get('MARWANOS_WINDOWS_HOME', str(Path.home() / '.local/share/marwanos/windows')))
    manager = Manager(None, data, runtime, Path.home() / 'Downloads', spool, windows / 'requests')
    engine = Engine(data)
    manager.engine = engine
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        manager.restore()
        while running:
            manager.requests()
            manager.update()
            time.sleep(0.5)
    finally:
        engine.close()


if __name__ == '__main__':
    main()
