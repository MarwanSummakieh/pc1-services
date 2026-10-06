# Automatic game metadata

Implemented and deployed to the PC1 bench on 2026-10-06. The first provider is
Steam Store, used for descriptive metadata for games from any installation source.
Steam metadata does not change a Windows installation into a Steam-owned game.

The player-owned `marwanos-metadata.service` watches installed game records in
`/run/marwanos/apps.tsv` and the Windows manager's `apps/*.json`. Steam games use
their existing app IDs; other installations search by title. One exact normalized
title match is automatic. Ambiguous/inexact matches require an explicit choice.
Utilities and pending Steam downloads in the TSV are excluded. Managed utilities
may receive an unmatched result; they still open normally.

The isolated Steam adapter uses public Store search/details endpoints and Steam
artwork CDN URLs without account credentials. These Store endpoints are not a
versioned partner API; changes to their response format may require adapter updates.
Games absent from Steam retain local presentation and can be searched manually.
Additional metadata providers can implement the same search/details/fetch interface.
Achievement ownership and player progress are separate integrations.

## Controller operation

Select a library card and press **Down** to open details. **Play** is the only
action button. **Back** returns to the selected card, and **L1/R1** scroll longer
game information. The existing Options removal flow remains on the library card.
Metadata refreshes automatically; backend correction requests remain supported,
but the details page has no refresh, matching, title-editing or removal buttons.

Details show title, description, release date, genres, developer/publisher,
installation source, provider platform information and available cover/background/logo.
Cards prefer downloaded cover artwork and fall back to the existing app icon.
The provider currently does not supply a separate game icon; local icon data is
preserved. Metadata never changes executable, stop command, controller mode or game ID.

## Persistence and recovery

`~/.local/share/marwanos/metadata/state.json` is keyed by installation ID, independently
of the selected provider ID. `assets/` stores verified PNG/JPEG/WebP files by SHA-256.
Every asset retains its origin URL, digest, byte count and download timestamp.
Requests are unique atomic JSON files in `requests/`. Only the player service writes
the cache; no root scanner performs network matching or image parsing.

Import automatically downloads metadata and images. Failed requests retain existing
metadata/artwork for the same match, remain independent of launch, and retry after
15 minutes or immediately through Refresh. Optional missing artwork produces a
partial result. Interrupted downloads retry after worker restart. A corrected match
cannot inherit pictures from the previous game; the selected match and manual title
override persist through refresh/restart. A changed import title is rematched unless
the user has explicitly selected a provider ID. Cache writes use temp files, fsync
and atomic rename. Ready records are reused offline without provider calls.

Fixtures can override `MARWANOS_METADATA_HOME`, `MARWANOS_METADATA_APPS` and
`MARWANOS_WINDOWS_HOME`. The worker supports `--once` for isolated acceptance.

## Tekken 8 evidence

The installed entry is `managed.local-tekken8-fresh-1791234744`, registered by the
Windows/FitGirl setup flow. Its launch target exists at
`/var/home/player/Games/local-tekken8-fresh-1791234744/TEKKEN 8.exe`.
Its source remains **Windows**. The automatic exact title search selected Steam
metadata ID **1778820**, with no manual match request.

Real downloads on PC1 produced:

| Asset | Bytes | SHA-256 |
| --- | ---: | --- |
| Cover | 62,008 | `bbb02b6db4fc2ed9990c8bd6710e53fc721760d18b4609bd1fcb1afec36a544d` |
| Background | 1,188,076 | `732d0bb15a99937c1037a345dc7f3b2c994a6a723038a488a399c98bf5d148bd` |
| Logo | 45,331 | `b23a41517bec5b1856cc4e0c29dea66373982867666f638d8b7a6dac23a184de` |
| Header | 49,923 | `616643cb5f415192a3d79f801fa750ad057704a5d4670db38f5c47d755ec1574` |

The metadata record includes description, release date, genre, developer,
publisher and Windows platform. A rendered Xvfb check using these actual cached
files confirmed the details layout. The supervised NVIDIA/gamescope shell then
restarted on PC1 and loaded the Tekken entry with its metadata. A fresh worker
invoked in an isolated network namespace successfully reused the persistent cache.

The bench uses `/var/marwanos/dev-shell/marwanos-shell` with its matching
`libmowser.so`, and the metadata helper under
`/var/marwanos/metadata-bench-20261006/`. A player user-unit override enables the
helper. The pre-existing shell override, when present, was backed up under that
directory's `backup/`. The OS Containerfile now enables the image-owned metadata
unit. Remove the player unit override after installing an image with this worker
to use the image-owned helper.

## Validation boundaries

Twelve metadata backend cases cover matching, Unicode, ambiguity, source/identity,
actual cache files, offline restart, failures/retry, manual corrections, changed
matches, search and interrupted downloads. The complete Python suite passes with
the OS runtime's dependencies plus the existing pefile/ordlookup test modules;
two optional live service checks are skipped. All pinned Godot shell suites pass,
including the Play-only details page, focus across metadata updates, Back and
launch-identity preservation.

The real Tekken launch initially used the general Windows registration's pointer
profile. This started the mouse/keyboard bridge and held the virtual gamepad
neutral. The installed Tekken manifest was backed up and only `input_mode` was
changed to the empty native-gamepad profile. The executable, prefix, installation
ID and launch/stop commands are unchanged. Tekken was relaunched through the shell
with user approval. Its foreground gamepad heartbeat is consistently enabled, and
Wine has the virtual controller `/dev/input/event22` open. The DualSense remains
connected to the broker; the pointer bridge no longer starts for Tekken.

Still pending: physical in-game controller response, complete controller operation
of the details screen, and an actual machine reboot/offline check. The routing,
cached-file and worker/shell restart checks do not substitute for those.

The earlier OS build for `d313bc05231d8d8b04e7e1407c4f90611a152bba` completed
successfully and pushed its image during this work. It predates this metadata
implementation. Release-image boot/hardware acceptance remains open.
