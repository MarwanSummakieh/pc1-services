# Achievements

The player-owned `marwanos-achievements.service` reads achievement data without changing game saves. The shell page displays names, descriptions, cached icons, locked/unlocked state, available progress, unlock timestamps and counts. L1/R1 switches All/Locked/Unlocked; Y requests synchronization; B returns to the previous screen. Game details retain their single Play action; the shell provides a separate controller shortcut to achievements.

State is persisted in `~/.local/share/marwanos/achievements/state.json`. Records are separated by installation ID, provider and profile. Steam accounts never inherit local Windows save achievements. The first successful sync establishes a silent baseline; later locked-to-unlocked changes produce `achievement-*.json` events in the existing notification spool. Restarting does not replay historical unlocks. Failed or partial save reads retain the last valid cache for offline browsing; unavailable providers show an explanation instead of a fictional zero-percent completion.

## Supported providers

- **RUNE/CODEX local saves:** the installed game's `steam_emu.ini` supplies its declared AppId. The adapter reads `drive_c/users/Public/Documents/Steam/{RUNE,CODEX}/{appid}/achievements.ini` inside that installation's Wine prefix. `Achieved`, `UnlockTime`, `CurProgress` and `MaxProgress` remain the source of earned state. `[SteamAchievements] Count` indexes records and is never treated as the game's total achievement count. Missing or incomplete files preserve cached data.
- **Steam account:** the most recent local Steam profile (or explicitly configured SteamID64) is queried through Valve's documented public profile XML feed. Private/unavailable stats remain unavailable. Many games expose only earned achievements in this feed, so its row count is never treated as a full game total without a separate genuine schema. An optional Steam Web API key supports `GetPlayerAchievements` and `GetSchemaForGame` when public XML does not work. Steam Web API data does not expose universal per-achievement numeric progress, so the UI displays numeric progress only when the local provider actually supplies it.
- **Other installations:** no unsupported provider state is invented. GOG/Epic/emulator achievements require their own genuine provider adapter before they can be displayed.

Local achievement details come from a game's `steam_settings/achievements.json` or a schema cached at `achievements/schemas/{appid}.json`. The latter accepts Valve's `GetSchemaForGame` response or `{ "achievements": [...] }` containing internal `name`, `displayName`, `description`, `icon` and `icongray`. A configured Steam key can fetch and cache a genuine schema automatically. Icons are bounded, decoded and stored as PNG; only HTTPS Steam hosts are accepted. Offline use needs no key once the cache exists.

Optional settings live in `~/.config/marwanos/achievements.json`:

```json
{"steam_id": "YOUR_STEAMID64", "api_key": "YOUR_PRIVATE_WEB_API_KEY"}
```

This file must have mode 0600. Never put the key in a command line, screenshot, issue, log or committed file. Restart the user service after changing settings. Error messages intentionally omit URLs and exception details that might contain credentials.

## Tekken 8 evidence and remaining acceptance

Read-only inspection of PC1 on October 6, 2026 found the installed game's declared AppId `1778820` and a genuine RUNE save at:

`~/.local/share/marwanos/windows/prefixes/local-tekken8-fresh-1791234744/drive_c/users/Public/Documents/Steam/RUNE/1778820/achievements.ini`

The file contained only `[SteamAchievements] Count=0`. No achievement schema was installed. This supports zero *recorded* unlocks with unknown total; it does not prove that every game achievement is locked. Valve's global Tekken page lists 47 achievements, but its anonymous `?xml=1` request returns HTML without internal API identifiers, so its names cannot safely be matched to local earned records. No earned state, schema mapping or API credential was fabricated. A genuine schema/authenticated Steam source and an actual new in-game unlock are still needed to finish Tekken's names/icons and physical unlock-toast acceptance.

Backend regression tests cover real-format empty saves, progress, silent baseline, subsequent notification, restart deduplication, profile isolation, missing/partial saves, offline retention, unavailable profiles, XML parsing and unsafe icon hosts. The Godot test checks controller filtering, refresh, offline viewing and Back.

Primary references: [Valve ISteamUserStats Web API](https://partner.steamgames.com/doc/webapi/isteamuserstats), [Valve Community Data](https://partner.steamgames.com/documentation/community_data), [Achievement Watcher's published CODEX format](https://github.com/xan105/Achievement-Watcher), and [Valve's public Tekken 8 achievement page](https://steamcommunity.com/stats/1778820/achievements).
