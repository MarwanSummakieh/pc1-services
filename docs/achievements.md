# Achievements

The player-owned `marwanos-achievements.service` reads achievement data without changing game saves. The shell page displays names, descriptions, cached icons, locked/unlocked state, available progress, unlock timestamps and counts. L1/R1 switches All/Locked/Unlocked; Y requests synchronization; B returns to the previous screen. Game details retain their single Play action; the shell provides a separate controller shortcut to achievements.

State is persisted in `~/.local/share/marwanos/achievements/state.json`. Records are separated by installation ID, provider and profile. Steam accounts never inherit local Windows save achievements. The first successful sync establishes a silent baseline; later locked-to-unlocked changes produce `achievement-*.json` events in the existing notification spool. Restarting does not replay historical unlocks. Failed or partial save reads retain the last valid cache for offline browsing; unavailable providers show an explanation instead of a fictional zero-percent completion.

## Supported providers

- **RUNE/CODEX local saves:** the installed game's `steam_emu.ini` supplies its declared AppId. The adapter reads `drive_c/users/Public/Documents/Steam/{RUNE,CODEX}/{appid}/achievements.ini` inside that installation's Wine prefix. `Achieved`, `UnlockTime`, `CurProgress` and `MaxProgress` remain the source of earned state. `[SteamAchievements] Count` indexes records and is never treated as the game's total achievement count. Missing or incomplete files preserve cached data.
- **Steam account:** the most recent local Steam profile (or explicitly configured SteamID64) is queried through Valve's documented public profile XML feed. Private/unavailable stats remain unavailable. Many games expose only earned achievements in this feed, so its row count is never treated as a full game total without a separate genuine schema. An optional Steam Web API key supports `GetPlayerAchievements` and `GetSchemaForGame` when public XML does not work. Steam Web API data does not expose universal per-achievement numeric progress, so the UI displays numeric progress only when the local provider actually supplies it.
- **Other installations:** no unsupported provider state is invented. GOG/Epic/emulator achievements require their own genuine provider adapter before they can be displayed.

Achievement details use the following precedence: a valid schema cached at `achievements/schemas/{appid}.json`, the game's `steam_settings/achievements.json`, a configured Steam key's `GetSchemaForGame` response, then Valve's public `IPlayerService/GetGameAchievements/v1/?appid={appid}&language=english` catalog. The public fallback needs no key, account or SteamID. Existing local/configured schemas keep their precedence. A missing or invalid schema does not prevent reading genuine local progress. Failed network lookups retry after five minutes; an explicit controller Refresh retries immediately.

The public catalog maps `internal_name` directly to the local achievement ID, plus localized names/descriptions, hidden flags and colored/gray icons from the official Steam CDN. Every row must be valid; empty, duplicate, malformed or unsafe-icon catalogs are rejected rather than silently becoming an incomplete total. Valid catalogs are cached atomically and reused after restart/offline operation. The cache accepts Valve's `GetSchemaForGame` response or `{ "achievements": [...] }` with internal `name`, `displayName`, `description`, `icon` and `icongray`. Icons are bounded, decoded and stored as PNG; only HTTPS Steam hosts are accepted.

Public catalog progress bounds are retained as `catalog_progress` metadata. Global percentages and bounds never create earned flags, unlock timestamps or current progress. Those remain personal provider data; a schema-only enrichment sends no unlock notification. A private/unavailable Steam profile remains unavailable even when the public catalog exists. The public endpoint is available but absent from Valve's current public IPlayerService reference, so response validation and persisted offline cache remain necessary.

Optional settings live in `~/.config/marwanos/achievements.json`:

```json
{"steam_id": "YOUR_STEAMID64", "api_key": "YOUR_PRIVATE_WEB_API_KEY"}
```

This file must have mode 0600. Never put the key in a command line, screenshot, issue, log or committed file. Restart the user service after changing settings. Error messages intentionally omit URLs and exception details that might contain credentials.

## Tekken 8 evidence and remaining acceptance

Read-only inspection of PC1 on October 6, 2026 found the installed game's declared AppId `1778820` and a genuine RUNE save at:

`~/.local/share/marwanos/windows/prefixes/local-tekken8-fresh-1791234744/drive_c/users/Public/Documents/Steam/RUNE/1778820/achievements.ini`

The file contained only `[SteamAchievements] Count=0`. No achievement schema was installed. This supports zero *recorded* unlocks with unknown total; it does not prove that every game achievement is locked. Valve's global Tekken page lists 47 achievements, but its anonymous `?xml=1` response lacks internal API identifiers. A subsequent genuine request to the official public catalog endpoint returned all **47 unique internal IDs**, names, descriptions and colored/gray icon references without credentials. The ID set matched Valve's separate public global-percentages endpoint; no guessed name/percentage/order joins were used.

The installed `steam_emu.ini` independently declares app `1778820`; metadata matching is not the achievement identity source. The automatic keyless fallback now supplies this genuine catalog when no cached/local/configured schema exists, while the RUNE file remains the source of recorded progress. Catalog acquisition and its offline cache are covered by focused regressions. Final-image acceptance must demonstrate automatic fetching from PC1's still-absent schema cache, actual names/icons/total in the controller page, and a genuinely earned new unlock/notification. No schema was manually seeded on PC1, and no earned record, user progress or API credential was fabricated.

Backend regression tests cover real-format empty saves, progress, silent baseline, subsequent notification, restart deduplication, profile isolation, missing/partial saves, offline retention, unavailable profiles, XML parsing and unsafe icon hosts. Additional public-catalog cases cover keyless request parameters, complete normalization, integer/float bounds, malformed/duplicate/unsafe responses, precedence, offline restart, failed authenticated fallback, retry throttling and the separation from earned/current progress. The Godot test checks controller filtering, refresh, offline viewing and Back.

Primary references: [Valve ISteamUserStats Web API](https://partner.steamgames.com/doc/webapi/isteamuserstats), [Valve Community Data](https://partner.steamgames.com/documentation/community_data), [Achievement Watcher's published CODEX format](https://github.com/xan105/Achievement-Watcher), [Valve's public Tekken 8 achievement page](https://steamcommunity.com/stats/1778820/achievements), and the [official public Tekken catalog response](https://api.steampowered.com/IPlayerService/GetGameAchievements/v1/?appid=1778820&language=english).
