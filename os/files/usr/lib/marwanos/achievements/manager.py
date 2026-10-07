#!/usr/bin/env python3
"""Read genuine achievements, cache per installation/profile, announce new unlocks."""
import argparse
import configparser
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

# The worker is also loaded by path by isolated regression/acceptance runners.
_provider_spec = importlib.util.spec_from_file_location("pc1_achievement_providers", Path(__file__).with_name("providers.py"))
_providers = importlib.util.module_from_spec(_provider_spec)
_provider_spec.loader.exec_module(_providers)


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        file.write((json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())
        file.flush()
        os.fsync(file.fileno())
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def number(value, fallback=0):
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else fallback
    except (ValueError, TypeError):
        return fallback


def ini(path):
    result = configparser.ConfigParser(interpolation=None, strict=False)
    result.optionxform = str
    with Path(path).open(encoding="utf-8-sig") as file:
        result.read_file(file)
    return result


def local_state(path):
    """CODEX/RUNE format; absent achievements are locked only with a full schema."""
    data = ini(path)
    if not data.has_section("SteamAchievements") or "Count" not in data["SteamAchievements"]:
        raise ValueError("Local save is incomplete")
    count = data["SteamAchievements"]["Count"]
    if not count.isdigit():
        raise ValueError("Local save is incomplete")
    indexed = [value for key, value in data["SteamAchievements"].items() if key.isdigit()]
    if len(indexed) < int(count) or any(not data.has_section(name) for name in indexed):
        raise ValueError("Local save is incomplete")
    values = {}
    for name in data.sections():
        if name == "SteamAchievements":
            continue
        section = data[name]
        if not any(key in section for key in ("Achieved", "UnlockTime", "CurProgress")):
            continue
        values[name] = {"unlocked": section.get("Achieved", "0") == "1",
                        "unlock_time": int(number(section.get("UnlockTime"))),
                        "progress_current": number(section.get("CurProgress")),
                        "progress_max": number(section.get("MaxProgress"))}
    # Count indexes known records, not the game's total schema size.
    return values


def schema_rows(value):
    if isinstance(value, dict):
        game = value.get("game", {})
        game = game if isinstance(game, dict) else {}
        stats = game.get("availableGameStats", {})
        stats = stats if isinstance(stats, dict) else {}
        value = stats.get("achievements", value.get("achievements", []))
    if not isinstance(value, list):
        raise ValueError("Invalid achievement schema")
    result = {}
    for row in value[:5000]:
        if not isinstance(row, dict) or not row.get("name"):
            continue
        result[str(row["name"])] = {"id": str(row["name"]),
            "name": str(row.get("displayName", row.get("display_name", row["name"]))),
            "description": str(row.get("description", "")),
            "hidden": bool(row.get("hidden", False)),
            "icon_url": str(row.get("icon", "")),
            "locked_icon_url": str(row.get("icongray", row.get("icon_gray", "")))}
        if "catalog_progress" in row:
            result[str(row["name"])]["catalog_progress"] = _providers.catalog_progress(row["catalog_progress"])
    return result


def community_rows(raw):
    if len(raw) > 4 * 1024 * 1024 or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("Invalid public achievement response")
    root = ET.fromstring(raw)
    if root.tag != "playerstats" or root.find("achievements") is None:
        raise ValueError("Steam profile stats are private or unavailable")
    schema, state = {}, {}
    for row in root.findall("achievements/achievement")[:5000]:
        name = row.findtext("apiname", "").strip()
        if not name:
            continue
        schema[name] = {"id": name, "name": row.findtext("name", name),
            "description": row.findtext("description", ""), "hidden": False,
            "icon_url": row.findtext("iconClosed", ""),
            "locked_icon_url": row.findtext("iconOpen", "")}
        state[name] = {"unlocked": row.get("closed") == "1",
                       "unlock_time": int(number(row.findtext("unlockTimestamp")))}
    return schema, state


class Steam:
    def fetch(self, url, limit=4 * 1024 * 1024):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or not (parsed.hostname in ("api.steampowered.com", "steamcommunity.com") or (parsed.hostname or "").endswith(".steamstatic.com")):
            raise ValueError("Invalid achievement host")
        request = urllib.request.Request(url, headers={"User-Agent": "MarwanOS-Achievements/1.0"})
        with urllib.request.urlopen(request, timeout=12) as response:
            final = urllib.parse.urlparse(response.url)
            if final.scheme != "https" or final.hostname != parsed.hostname:
                raise ValueError("Unexpected achievement redirect")
            raw = response.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("Achievement response too large")
        return raw

    def api(self, method, version, **params):
        url = "https://api.steampowered.com/ISteamUserStats/" + method + "/" + version + "/?" + urllib.parse.urlencode(params)
        response = json.loads(self.fetch(url))
        if not isinstance(response, dict):
            raise ValueError("Invalid Steam achievement response")
        return response

    def schema(self, appid, key):
        return schema_rows(self.api("GetSchemaForGame", "v2", appid=appid, key=key, l="english"))

    def schema_public(self, appid):
        if not isinstance(appid, str) or not re.fullmatch(r"[1-9][0-9]{0,9}", appid) or int(appid) > 0xffffffff:
            raise ValueError("Invalid achievement app ID")
        url = "https://api.steampowered.com/IPlayerService/GetGameAchievements/v1/?" + urllib.parse.urlencode({"appid": appid, "language": "english"})
        return _providers.public_schema_rows(json.loads(self.fetch(url)), appid)

    def state(self, appid, profile, key):
        response = self.api("GetPlayerAchievements", "v1", appid=appid, steamid=profile, key=key, l="english").get("playerstats", {})
        if not isinstance(response, dict) or not response.get("success") or not isinstance(response.get("achievements"), list):
            raise ValueError("Steam profile stats are private or unavailable")
        return {str(row["apiname"]): {"unlocked": row.get("achieved") == 1,
            "unlock_time": int(number(row.get("unlocktime")))} for row in response["achievements"] if isinstance(row, dict) and row.get("apiname")}

    def public(self, appid, profile):
        return community_rows(self.fetch(f"https://steamcommunity.com/profiles/{profile}/stats/{appid}/achievements/?xml=1"))


def config(path):
    path = Path(path)
    if os.name != "nt" and path.exists() and path.stat().st_mode & 0o077:
        return {}  # Never use keys in a group/world-readable file.
    value = read_json(path, {})
    return value if isinstance(value, dict) else {}


def active_steam_profile(home):
    for path in [home / ".local/share/Steam/config/loginusers.vdf", home / ".steam/steam/config/loginusers.vdf"]:
        try:
            raw = path.read_text()
        except OSError:
            continue
        for steamid, block in re.findall(r'"(7656119\d{10})"\s*\{([^}]+)\}', raw):
            if re.search(r'"MostRecent"\s*"1"', block, re.I):
                return steamid
    return ""


def installation_appid(entry):
    game_id = str(entry.get("id", ""))
    if re.fullmatch(r"steam\.\d+", game_id):
        return game_id.split(".", 1)[1]
    executable = Path(entry.get("executable", ""))
    if not executable.is_file():
        return ""
    # Read the installed game's own declared app ID, never a fuzzy metadata match.
    candidates = list(executable.parent.glob("steam_emu.ini"))
    for folder in [executable.parent / "Polaris/Binaries/Win64", executable.parent / "Engine/Binaries/ThirdParty/Steamworks"]:
        if folder.is_dir():
            candidates.extend(folder.glob("**/steam_emu.ini"))
    candidates.extend(executable.parent.glob("*/Binaries/Win64/steam_emu.ini"))
    for path in candidates[:20]:
        try:
            settings = ini(path)["Settings"]
            value = next((v for k, v in settings.items() if k.lower() == "appid"), "")
            if re.fullmatch(r"\d+", value):
                return value
        except (OSError, ValueError, configparser.Error, KeyError):
            continue
    return ""


def local_source(entry, appid):
    prefix = Path(entry.get("prefix", ""))
    if not prefix.is_dir() or not appid:
        return None
    for provider in ["RUNE", "CODEX"]:
        path = prefix / f"drive_c/users/Public/Documents/Steam/{provider}/{appid}/achievements.ini"
        if path.is_file() or path.parent.is_dir():
            return {"provider": provider + " local", "path": path,
                    "profile": "local:" + hashlib.sha256(str(prefix.resolve()).encode()).hexdigest()[:16]}
    return None


class Manager:
    def __init__(self, base, home=None, steam=None, settings=None, spool=None, clock=time.time):
        self.base, self.home = Path(base), Path(home or Path.home())
        self.steam, self.settings = steam or Steam(), settings if settings is not None else config(self.home / ".config/marwanos/achievements.json")
        self.spool = Path(spool or self.home / ".local/share/marwanos/notification-events")
        self.clock = clock
        self.data = read_json(self.base / "state.json", {"version": 1, "games": {}, "profiles": {}})
        if not isinstance(self.data, dict) or not isinstance(self.data.get("profiles"), dict) or not isinstance(self.data.get("games"), dict):
            self.data = {"version": 1, "games": {}, "profiles": {}}
        self.data.setdefault("games", {})
        self.next_poll = {}
        self.next_schema_poll = {}
        self.icon_budget = 4

    def publish(self):
        write_json(self.base / "state.json", self.data)

    def load_schema(self, appid, entry, force=False):
        path = self.base / "schemas" / (appid + ".json")
        root = Path(entry.get("executable", "")).parent
        for candidate in [path, root / "steam_settings/achievements.json", root / "Polaris/Binaries/Win64/steam_settings/achievements.json"]:
            if candidate.is_file():
                try:
                    rows = schema_rows(read_json(candidate, {}))
                    if rows:
                        return rows
                except (ValueError, KeyError, TypeError):
                    pass
        if not force and self.next_schema_poll.get(appid, 0) > self.clock():
            return {}
        # A failed network catalog must not stall every local-save observation.
        self.next_schema_poll[appid] = self.clock() + 300
        key = str(self.settings.get("api_key", ""))
        sources = [lambda: self.steam.schema(appid, key)] if key else []
        sources.append(lambda: self.steam.schema_public(appid))
        for fetch in sources:
            try:
                rows = fetch()
                if rows:
                    write_json(path, {"achievements": [{**row, "name": row["id"], "displayName": row["name"], "icon": row.get("icon_url", ""), "icongray": row.get("locked_icon_url", "")} for row in rows.values()]})
                    return rows
            except (OSError, ValueError, KeyError, TypeError):
                pass  # A failed schema fetch must not hide genuine local progress.
        return {}

    def cache_icon(self, url):
        if not url:
            return ""
        digest = hashlib.sha256(url.encode()).hexdigest()
        path = self.base / "icons" / (digest + ".png")
        if path.is_file():
            return str(path)
        if self.icon_budget <= 0:
            return ""
        self.icon_budget -= 1
        try:
            raw = self.steam.fetch(url, 2 * 1024 * 1024)
            # Validate image before persisting it for Godot's decoder.
            from PIL import Image
            import io
            with Image.open(io.BytesIO(raw)) as image:
                if image.width > 2048 or image.height > 2048:
                    return ""
                image.verify()
            with Image.open(io.BytesIO(raw)) as image:
                output = io.BytesIO()
                image.convert("RGBA").save(output, format="PNG")
                raw = output.getvalue()
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
                temporary = Path(file.name)
                file.write(raw)
            temporary.replace(path)
            return str(path)
        except (OSError, ValueError, ImportError):
            return ""

    def sync(self, entry, force=False):
        game_id = str(entry.get("id", ""))
        if not game_id:
            return
        now = int(self.clock())
        if not force and self.next_poll.get(game_id, 0) > now:
            return
        self.next_poll[game_id] = now + 10
        self.icon_budget = 4  # Bound image requests; remaining icons load on later polls.
        appid = installation_appid(entry)
        local = local_source(entry, appid)
        profile = str(self.settings.get("steam_id", "")) or active_steam_profile(self.home)
        provider = local["provider"] if local else "Steam" if game_id.startswith("steam.") else "Unavailable"
        profile = local["profile"] if local else profile if provider == "Steam" else "unavailable"
        key = game_id + "|" + provider + "|" + profile
        previous = self.data["profiles"].get(key, {})
        previous = previous if isinstance(previous, dict) else {}
        record = dict(previous)
        record.update({"game_id": game_id, "title": str(entry.get("title", game_id)), "provider": provider,
                       "profile": profile, "app_id": appid, "checked_at": now})
        try:
            if local:
                states = local_state(local["path"])
                schema = self.load_schema(appid, entry, force)
                schema_complete = bool(schema)
                record["source_file"] = str(local["path"])
            elif provider == "Steam" and re.fullmatch(r"7656119\d{10}", profile) and appid:
                # Network Steam accounts poll less often; local records remain fast.
                self.next_poll[game_id] = now + 60
                api_key = str(self.settings.get("api_key", ""))
                if api_key:
                    states = self.steam.state(appid, profile, api_key)
                    schema = self.load_schema(appid, entry, force)
                    schema_complete = bool(schema)
                else:
                    public_schema, states = self.steam.public(appid, profile)
                    schema = self.load_schema(appid, entry, force)
                    schema_complete = bool(schema)
                    schema.update(public_schema)
            else:
                raise ValueError("Unsupported provider or no Steam account")
            old_rows = {row["id"]: row for row in previous.get("achievements", []) if isinstance(row, dict) and row.get("id")}
            rows = []
            for name in sorted(set(schema) | set(states)):
                row = dict(schema.get(name, {"id": name, "name": name, "description": "Achievement details are unavailable.", "hidden": False}))
                row.update(states.get(name, {"unlocked": False, "unlock_time": 0}))
                row["icon"] = self.cache_icon(row.get("icon_url" if row.get("unlocked") else "locked_icon_url", "")) or old_rows.get(name, {}).get("icon", "")
                rows.append(row)
            message = "" if schema_complete else "Local achievement state is available; names, icons and total require the game's achievement schema."
            if not local and not schema_complete:
                message = "Public Steam achievements are available; the full total requires the game's achievement schema."
            record.update({"status": "ready" if schema_complete else "partial", "achievements": rows,
                           "unlocked_count": sum(bool(row.get("unlocked")) for row in rows),
                           "total_count": len(schema) if schema_complete else None,
                           "synced_at": now, "initialized": True,
                           "message": message})
            # Persist the first successful baseline before sending any notification.
            # A schema-only refresh cannot create earned achievements.
            new_unlocks = [row for row in rows if row.get("unlocked") and not old_rows.get(row["id"], {}).get("unlocked")]
            self.data["profiles"][key] = record
            self.data["games"][game_id] = record
            self.publish()
            if previous.get("initialized"):
                for row in new_unlocks:
                    token = hashlib.sha256((key + "|" + row["id"] + "|" + str(row.get("unlock_time", 0))).encode()).hexdigest()
                    write_json(self.spool / ("achievement-" + token + ".json"), {"app": "Achievements", "summary": "Achievement unlocked: " + row["name"], "body": record["title"] + "\n" + row.get("description", "")})
        except (OSError, ValueError, KeyError, TypeError, configparser.Error, ET.ParseError):
            # URLs can contain keys, so exception text is never logged or published.
            record.update({"status": "offline" if previous.get("initialized") else "unavailable",
                           "message": "Cached achievements are available offline." if previous.get("initialized") else "Achievements are unavailable for this installation or profile. A supported local save or public/authenticated Steam profile is required."})
            self.data["profiles"][key] = record
            self.data["games"][game_id] = record
            self.publish()


def library(windows, apps):
    result = {}
    for path in (Path(windows) / "apps").glob("*.json"):
        row = read_json(path, {})
        if isinstance(row, dict) and row.get("state") == "installed" and row.get("id"):
            result[row["id"]] = row
    try:
        for line in Path(apps).read_text().splitlines():
            fields = line.split("\t")
            if len(fields) >= 6 and fields[0].startswith("steam.") and fields[5] == "installed":
                result[fields[0]] = {"id": fields[0], "title": fields[1]}
    except OSError:
        pass
    return list(result.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    home = Path.home()
    base = Path(os.environ.get("MARWANOS_ACHIEVEMENTS_HOME", str(home / ".local/share/marwanos/achievements")))
    spool = Path(os.environ.get("XDG_DATA_HOME", str(home / ".local/share"))) / "marwanos/notification-events"
    manager = Manager(base, spool=spool)
    while True:
        entries = library(os.environ.get("MARWANOS_WINDOWS_HOME", str(home / ".local/share/marwanos/windows")), os.environ.get("MARWANOS_ACHIEVEMENTS_APPS", "/run/marwanos/apps.tsv"))
        requests = list((base / "requests").glob("*.json"))[:100]
        refresh = set()
        for path in requests:
            row = read_json(path, {})
            if isinstance(row, dict) and row.get("action") == "refresh":
                refresh.add(str(row.get("game_id", "")))
            path.unlink(missing_ok=True)
        for entry in entries:
            manager.sync(entry, entry["id"] in refresh)
        if args.once:
            return
        time.sleep(5)


if __name__ == "__main__":
    main()
