#!/usr/bin/python3
"""Shared freedesktop notification inbox, rendered by the controller shell."""
import json
import os
import time
from pathlib import Path

import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

INTERFACE = 'org.freedesktop.Notifications'


class Inbox:
    def __init__(self, path):
        self.path = Path(path)
        try:
            self.entries = json.loads(self.path.read_text())['entries'][-50:]
        except (OSError, ValueError, KeyError, TypeError):
            self.entries = []
        self.next_id = max((int(e['id']) for e in self.entries), default=0) + 1

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'entries': self.entries}, ensure_ascii=False))
        temporary.chmod(0o600)
        temporary.replace(self.path)

    def notify(self, app, replace_id, summary, body, urgency=1, download=None):
        ident = int(replace_id)
        old = next((e for e in self.entries if e['id'] == ident and e['app'] == str(app)), None)
        if old is None:
            ident = self.next_id
            self.next_id += 1
        else:
            self.entries.remove(old)
        self.entries.append({'id': ident, 'app': str(app)[:128],
                             'summary': str(summary)[:512], 'body': str(body)[:4096],
                             'urgency': int(urgency), 'time': int(time.time()), 'closed': False})
        if isinstance(download, dict) and isinstance(download.get('path'), str):
            self.entries[-1]['download'] = {'path': download['path'][:4096],
                                            'torrent': download.get('torrent') is True}
        self.entries = self.entries[-50:]
        self.save()
        return ident

    def close(self, ident):
        for entry in self.entries:
            if entry['id'] == int(ident) and not entry['closed']:
                entry['closed'] = True
                self.save()
                return True
        return False


class Server(dbus.service.Object):
    def __init__(self, bus, inbox, spool):
        self.name = dbus.service.BusName(INTERFACE, bus, do_not_queue=True)
        super().__init__(bus, '/org/freedesktop/Notifications')
        self.inbox, self.spool = inbox, spool
        GLib.timeout_add(500, self.receive_fdm)

    @dbus.service.method(INTERFACE, in_signature='', out_signature='as')
    def GetCapabilities(self):
        return ['body', 'persistence']

    @dbus.service.method(INTERFACE, in_signature='', out_signature='ssss')
    def GetServerInformation(self):
        return ('MarwanOS', 'MarwanOS', '1.0', '1.2')

    @dbus.service.method(INTERFACE, in_signature='susssasa{sv}i', out_signature='u')
    def Notify(self, app, replace_id, icon, summary, body, actions, hints, timeout):
        return self.inbox.notify(app, replace_id, summary, body, hints.get('urgency', 1))

    @dbus.service.method(INTERFACE, in_signature='u', out_signature='')
    def CloseNotification(self, ident):
        if self.inbox.close(ident):
            self.NotificationClosed(ident, 3)

    @dbus.service.signal(INTERFACE, signature='uu')
    def NotificationClosed(self, ident, reason):
        pass

    def receive_fdm(self):
        for path in sorted(self.spool.glob('*.json'))[:100]:
            try:
                if path.is_symlink() or path.stat().st_size > 65536:
                    raise ValueError('Invalid event file')
                event = json.loads(path.read_text())
                if (not isinstance(event, dict) or
                        not all(isinstance(event.get(key, ''), str) for key in ('app', 'summary', 'body'))):
                    raise ValueError('Invalid notification event')
                self.inbox.notify(event.get('app', 'FDM Controller'), 0,
                                  event['summary'], event['body'],
                                  download=event.get('download'))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                print(f'Cannot receive notification: {exc}', flush=True)
            finally:
                path.unlink(missing_ok=True)
        return True


def main():
    DBusGMainLoop(set_as_default=True)
    # Proton exposes the player's home but hides most host /run directories.
    data = Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share')))
    spool = data / 'marwanos' / 'notification-events'
    spool.mkdir(parents=True, exist_ok=True, mode=0o700)
    state = Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state')))
    server = Server(dbus.SessionBus(), Inbox(state / 'marwanos/notifications.json'), spool)
    print('MarwanOS notifications ready', flush=True)
    GLib.MainLoop().run()


if __name__ == '__main__':
    main()
