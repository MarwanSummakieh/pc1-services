# Native Downloads

Downloads is a MarwanOS system surface. Open it from the system navigation,
Browser → Downloads, or a `.torrent` file in Files. The shell owns the queue,
controller focus, shared keyboard, file picker, notifications and installation
handoff. The transfer engine runs independently as the unprivileged player.
This source implementation replaces the separate FDM window for new transfers.

## Use

- Add an HTTP/HTTPS link, BitTorrent v1 magnet, or local `.torrent` file. Review
  before starting, or choose Add paused.
- Each native transfer gets its own folder in `~/Downloads`. Existing files in
  other transfers cannot be overwritten by a matching filename.
- Select a row for Pause, Resume, Retry, Stop seeding, Open folder or Remove
  from queue. Pause a torrent to choose its files; at least one must remain selected.
- Removing an entry confirms that the transfer stops and downloaded files stay.
  Delete retained files through Files after stopping their transfer.
- All, Active and Finished filter the shared queue. Seeding appears under both
  Active and Finished because the payload is available while uploads continue.
- Cross opens actions, Square adds a link, Triangle chooses a torrent and Circle
  returns to the originating surface. Progress updates retain controller focus.

Native transfers continue while browsing, playing or leaving the screen. The
queue, paused state, torrent metadata, file selections and partial downloads
survive service restart. Recovery rebuilds transfers from the OS queue and saved
resume files, so finished or removed entries cannot restart from a stale engine
session. Linux shutdown saves the engine session; a hard power
loss can lose recent queue changes or transfer progress since the last save.
Completed installers enter the existing receipt and controller setup flow through
a persistent worker request, including completions while the shell is stopped. Torrent
payloads remain excluded from automatic installer cleanup.

Browser transfers keep Chromium's authenticated session and appear in the same
screen. Their existing lifetime still applies: leaving Browser keeps them alive;
closing their tab or quitting the shell cancels unfinished transfers. Browser
transfers offer cancellation and folder access, and remain session-only. Their
bytes are not handed to aria2, so sign-in cookies never need to be exported.

## Linux service

`marwanos-downloads.service` starts with the player's user session. The image
installs Fedora's `aria2` package. The Python helper starts and supervises a
private aria2 process using an authenticated loopback JSON-RPC endpoint, a
mode-0600 configuration and the parent's process lifetime. It does not open a
desktop window or use Wine, Proton, FDM Classic or its old torrent library.
The RPC contract follows the [aria2 manual](https://aria2.github.io/manual/en/html/aria2c.html).

| Location | Contents |
| --- | --- |
| `$XDG_DATA_HOME/marwanos/downloads/` | Queue, uploaded torrent metadata, engine session and private configuration |
| `$XDG_RUNTIME_DIR/marwanos/downloads/state.json` | Atomic heartbeat, public queue and last action error |
| `$XDG_RUNTIME_DIR/marwanos/downloads/requests/` | Atomic, individually named controller requests |
| `$XDG_DATA_HOME/marwanos/notification-events/` | Completion events consumed by the system inbox |
| `~/.local/share/marwanos/windows/requests/` | Completed-file receipts for the installer worker (`MARWANOS_WINDOWS_HOME` can override the base) |
| `~/Downloads/<name>-<id>/` | Payload and engine resume files |

Missing XDG data configuration defaults to `~/.local/share`. Requests accept
specific verbs and validated sources, rather than shell commands or arbitrary
engine options. The published state omits original source URLs and raw torrent
metadata. Magnet metadata completion follows its payload GID before reporting
completion. Torrent uploads are retained in the queue so moving the original
`.torrent` file cannot break recovery.

## Existing FDM installation

The prior FDM application, Wine prefix, private queue and downloaded files are
retained. They are not imported into the native queue or silently restarted.
Finish or stop existing transfers in that application before removing its
library registration. Re-adding a torrent creates a new native download folder;
it does not adopt or delete the previous payload. FDM's legacy notification
bridge remains supported during this transition.

## Validation

`python3 -m unittest discover -s tests -p 'test_download*.py' -v` covers source
validation, duplicate rejection, independent requests, pause/resume recovery,
torrent selection, retained payloads, completion receipts and magnet GID changes.
Install `aria2c` (or set `MARWANOS_ARIA2_BIN`) to include real loopback HTTP
pause/restart/resume/hash checks, torrent metadata/selection/restart and a local
HTTP webseed torrent payload with SHA-256 verification. Fixtures disable DHT and
local peer discovery and contact no public tracker.

`scripts/check-downloads-shell.sh` checks native navigation, stable row focus,
controller actions, shared browser progress/cancellation, Files torrent review,
service outage recovery and viewport bounds. It is part of `check-shell.sh`.
The optional Windows desk runner is `scripts/check-downloads-local.ps1 -Capture`
with the repository's cached Godot 4.7.1 editor. Its screenshot uses labelled
synthetic review transfers, not actual system downloads.

The Windows desk checks passed with Godot 4.7.1 and aria2 1.37.0 on 2026-10-09.
The source includes Linux image packaging and user-service startup, but this
change has not been baked into an image or deployed on PC1. Linux service boot,
real peer-to-peer magnet discovery, live Chromium integration with the new
screen and physical controller acceptance still require the target environment.
