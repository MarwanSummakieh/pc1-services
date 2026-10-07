import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import urllib.parse
from copy import deepcopy

PATH = Path(__file__).resolve().parents[1] / "os/files/usr/lib/marwanos/achievements/manager.py"
SPEC = importlib.util.spec_from_file_location("pc1_achievements", PATH)
achievements = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(achievements)


class SteamFixture:
    def __init__(self):
        self.offline = False
        self.unlocked = False

    def public(self, appid, profile):
        if self.offline:
            raise OSError("offline secret must never appear")
        return {"ONE": {"id": "ONE", "name": "First step", "description": "Do a thing"}}, {"ONE": {"unlocked": self.unlocked, "unlock_time": 100 if self.unlocked else 0}}

    def fetch(self, url, limit):
        raise OSError("offline")

    def schema_public(self, appid):
        return {}


def public_catalog():
    return {"response": {"achievements": [
        {"internal_name": "ONE", "localized_name": "First step", "localized_desc": "Do a thing",
         "icon": "aabb.jpg", "icon_gray": "ccdd.jpg", "hidden": False,
         "progress_type": 1, "min_progress_int": 0, "max_progress_int": 10,
         "player_percent_unlocked": "99.0", "unlocked": True, "progress_current": 9},
        {"internal_name": "TWO", "localized_name": "Next step", "localized_desc": "Do another thing",
         "icon": "eeff.jpg", "icon_gray": "aacc.jpg", "hidden": True,
         "progress_type": 2, "min_progress_float": 0.5, "max_progress_float": 2.5}
    ]}}


class CatalogSteam(achievements.Steam):
    def __init__(self):
        self.calls = []
        self.catalog = public_catalog()
        self.offline = False
        self.auth_rows = None

    def fetch(self, url, limit=0):
        if "IPlayerService/GetGameAchievements/v1/" not in url:
            raise OSError("fixture icon unavailable")
        self.calls.append(url)
        if self.offline:
            raise OSError("fixture offline")
        return json.dumps(self.catalog).encode()

    def schema(self, appid, key):
        self.calls.append("authenticated:" + appid)
        if self.auth_rows is None:
            raise ValueError("configured schema unavailable")
        return self.auth_rows


class AchievementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.base = self.home / "achievements"
        self.prefix = self.home / "prefix"
        self.state = self.prefix / "drive_c/users/Public/Documents/Steam/RUNE/1778820/achievements.ini"
        self.state.parent.mkdir(parents=True)
        self.state.write_text("[SteamAchievements]\nCount=0\n")
        game = self.home / "game"
        game.mkdir()
        (game / "TEKKEN 8.exe").touch()
        (game / "steam_emu.ini").write_text("[Settings]\nAppId=1778820\n")
        self.entry = {"id": "managed.tekken", "title": "TEKKEN 8", "prefix": str(self.prefix), "executable": str(game / "TEKKEN 8.exe")}
        self.steam = SteamFixture()
        self.manager = achievements.Manager(self.base, self.home, self.steam, settings={})

    def schema(self):
        achievements.write_json(self.base / "schemas/1778820.json", {"achievements": [{"name": "ONE", "displayName": "First step", "description": "Do a thing"}, {"name": "TWO", "displayName": "Next step"}]})

    def unlock(self, first=True):
        self.state.write_text("[SteamAchievements]\nCount=1\n00000=ONE\n[ONE]\nAchieved=" + ("1" if first else "0") + "\nCurProgress=2\nMaxProgress=10\nUnlockTime=1791230000\n")

    def events(self):
        return list(self.manager.spool.glob("achievement-*.json"))

    def test_real_empty_rune_save_is_not_fabricated(self):
        self.manager.sync(self.entry)
        row = self.manager.data["games"][self.entry["id"]]
        self.assertEqual(row["status"], "partial")
        self.assertEqual(row["unlocked_count"], 0)
        self.assertIsNone(row["total_count"])
        self.assertEqual(self.events(), [])

    def test_schema_and_progress_are_read_only(self):
        self.schema()
        self.unlock(False)
        original = self.state.read_bytes()
        self.manager.sync(self.entry)
        row = self.manager.data["games"][self.entry["id"]]
        self.assertEqual(row["total_count"], 2)
        self.assertEqual(row["unlocked_count"], 0)
        self.assertEqual(row["achievements"][0]["progress_current"], 2)
        self.assertEqual(row["achievements"][0]["progress_max"], 10)
        self.assertFalse(row["achievements"][1]["unlocked"])
        self.assertEqual(self.state.read_bytes(), original)

    def test_first_sync_no_replay_followed_by_real_change_once(self):
        self.schema()
        self.unlock()
        self.manager.sync(self.entry)
        self.assertEqual(self.events(), [])
        self.unlock(False)
        self.manager.sync(self.entry, True)
        self.unlock()
        self.manager.sync(self.entry, True)
        self.assertEqual(len(self.events()), 1)
        self.manager.sync(self.entry, True)
        restarted = achievements.Manager(self.base, self.home, self.steam, settings={})
        restarted.sync(self.entry, True)
        self.assertEqual(len(self.events()), 1)
        event = json.loads(self.events()[0].read_text())
        self.assertEqual(event["app"], "Achievements")
        self.assertIn("First step", event["summary"])

    def test_restart_missing_source_retains_offline_cache(self):
        self.schema()
        self.unlock()
        self.manager.sync(self.entry)
        # File exists but a partial concurrent rewrite is invalid. Retain last valid state.
        self.state.write_text("not an INI")
        restarted = achievements.Manager(self.base, self.home, self.steam, settings={})
        restarted.sync(self.entry)
        row = restarted.data["games"][self.entry["id"]]
        self.assertEqual(row["status"], "offline")
        self.assertTrue(row["achievements"][0]["unlocked"])
        self.assertEqual(self.events(), [])

    def test_installations_and_steam_profiles_do_not_share_earned_state(self):
        steam_entry = {"id": "steam.1778820", "title": "TEKKEN 8"}
        self.manager.settings = {"steam_id": "76561198000000000"}
        self.steam.unlocked = True
        self.manager.sync(steam_entry)
        self.manager.settings = {"steam_id": "76561198000000001"}
        self.steam.unlocked = False
        self.manager.sync(steam_entry, True)
        self.assertFalse(self.manager.data["games"][steam_entry["id"]]["achievements"][0]["unlocked"])
        self.manager.sync(self.entry, True)
        self.assertEqual(self.manager.data["games"][self.entry["id"]]["unlocked_count"], 0)
        self.assertEqual(len(self.manager.data["profiles"]), 3)
        self.assertEqual(self.events(), [])

    def test_private_or_unavailable_profile_never_becomes_zero_percent(self):
        entry = {"id": "steam.1", "title": "Game"}
        self.manager.settings = {"steam_id": "76561198000000000"}
        self.steam.offline = True
        self.manager.sync(entry)
        row = self.manager.data["games"][entry["id"]]
        self.assertEqual(row["status"], "unavailable")
        self.assertNotIn("total_count", row)
        self.assertNotIn("secret", json.dumps(row))

    def test_earned_only_public_feed_cannot_be_mistaken_for_full_completion(self):
        self.manager.settings = {"steam_id": "76561198000000000"}
        self.steam.unlocked = True
        self.manager.sync({"id": "steam.1778820", "title": "TEKKEN 8"})
        record = self.manager.data["games"]["steam.1778820"]
        self.assertEqual(record["status"], "partial")
        self.assertIsNone(record["total_count"])
        self.assertEqual(record["unlocked_count"], 1)
        self.assertEqual(record["achievements"][0]["name"], "First step")

    def test_cached_full_schema_completes_public_feed_without_transferring_unlocks(self):
        self.schema()
        self.manager.settings = {"steam_id": "76561198000000000"}
        self.steam.unlocked = True
        self.manager.sync({"id": "steam.1778820", "title": "TEKKEN 8"})
        record = self.manager.data["games"]["steam.1778820"]
        self.assertEqual(record["status"], "ready")
        self.assertEqual(record["total_count"], 2)
        self.assertEqual(record["unlocked_count"], 1)
        self.assertFalse(record["achievements"][1]["unlocked"])

    def test_missing_save_retains_same_local_profile_cache(self):
        self.schema()
        self.unlock()
        self.manager.sync(self.entry)
        self.state.unlink()
        self.manager.sync(self.entry, True)
        row = self.manager.data["games"][self.entry["id"]]
        self.assertEqual(row["status"], "offline")
        self.assertEqual(row["unlocked_count"], 1)

    def test_partial_file_rewrite_cannot_erase_earned_state(self):
        self.schema()
        self.unlock()
        self.manager.sync(self.entry)
        for raw in ["", "[SteamAchievements]\nCount=1\n00000=ONE\n", "[SteamAchievements]\nCount=no\n"]:
            self.state.write_text(raw)
            self.manager.sync(self.entry, True)
            self.assertEqual(self.manager.data["games"][self.entry["id"]]["unlocked_count"], 1)
            self.assertEqual(self.events(), [])

    def test_public_xml_uses_personal_unlock_flags_not_global_rarity(self):
        raw = b'<playerstats><achievements><achievement closed="1"><apiname>ONE</apiname><name>First</name><description>Do it</description><unlockTimestamp>123</unlockTimestamp><iconClosed>https://icon</iconClosed></achievement><achievement closed="0"><apiname>TWO</apiname><name>Second</name></achievement></achievements></playerstats>'
        schema, state = achievements.community_rows(raw)
        self.assertEqual(schema["ONE"]["name"], "First")
        self.assertTrue(state["ONE"]["unlocked"])
        self.assertFalse(state["TWO"]["unlocked"])
        self.assertEqual(state["ONE"]["unlock_time"], 123)
        with self.assertRaises(ValueError):
            achievements.community_rows(b'<!DOCTYPE html><html>private</html>')

    def test_partial_state_without_schema_keeps_genuine_ids_and_timestamps(self):
        self.unlock()
        self.manager.sync(self.entry)
        row = self.manager.data["games"][self.entry["id"]]
        self.assertEqual(row["unlocked_count"], 1)
        self.assertEqual(row["achievements"][0]["name"], "ONE")
        self.assertEqual(row["achievements"][0]["unlock_time"], 1791230000)
        self.assertIsNone(row["total_count"])

    def test_metadata_title_match_does_not_grant_an_achievement_identity(self):
        (Path(self.entry["executable"]).parent / "steam_emu.ini").unlink()
        self.entry["metadata"] = {"provider_id": "1778820"}
        self.assertEqual(achievements.installation_appid(self.entry), "")
        self.manager.sync(self.entry)
        self.assertEqual(self.manager.data["games"][self.entry["id"]]["status"], "unavailable")

    def test_unsafe_icon_host_is_rejected(self):
        with self.assertRaises(ValueError):
            achievements.Steam().fetch("https://example.com/icon.png")

    def test_malformed_cache_recovers_with_silent_first_sync(self):
        self.schema()
        self.unlock()
        profile_key = self.entry["id"] + "|RUNE local|" + achievements.local_source(self.entry, "1778820")["profile"]
        for damaged in [{"games": [], "profiles": {}}, {"games": {}, "profiles": {profile_key: []}}]:
            achievements.write_json(self.base / "state.json", damaged)
            manager = achievements.Manager(self.base, self.home, self.steam, settings={})
            manager.sync(self.entry)
            self.assertEqual(manager.data["games"][self.entry["id"]]["unlocked_count"], 1)
            self.assertEqual(list(manager.spool.glob("*.json")), [])

    def test_authenticated_schema_and_player_stats_are_joined_without_key_in_cache(self):
        class AuthSteam(achievements.Steam):
            def fetch(self, url, limit=0):
                query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
                if query.get("key") != ["private-fixture-key"]:
                    raise ValueError("missing fixture key")
                if "GetSchemaForGame" in url:
                    return json.dumps({"game": {"availableGameStats": {"achievements": [{"name": "ONE", "displayName": "First step", "description": "Do a thing"}]}}}).encode()
                return json.dumps({"playerstats": {"success": True, "achievements": [{"apiname": "ONE", "achieved": 1, "unlocktime": 123}]}}).encode()
        manager = achievements.Manager(self.base, self.home, AuthSteam(), settings={"steam_id": "76561198000000000", "api_key": "private-fixture-key"})
        manager.sync({"id": "steam.1778820", "title": "TEKKEN 8"})
        record = manager.data["games"]["steam.1778820"]
        self.assertEqual(record["status"], "ready")
        self.assertEqual(record["achievements"][0]["name"], "First step")
        self.assertEqual(record["achievements"][0]["unlock_time"], 123)
        self.assertNotIn("private-fixture-key", (self.base / "state.json").read_text())
        self.assertNotIn("private-fixture-key", (self.base / "schemas/1778820.json").read_text())
        self.assertEqual(list(manager.spool.glob("*.json")), [])

    def test_public_catalog_normalizes_full_schema_without_player_state(self):
        steam = CatalogSteam()
        rows = steam.schema_public("1778820")
        self.assertEqual(set(rows), {"ONE", "TWO"})
        self.assertEqual(rows["ONE"]["name"], "First step")
        self.assertEqual(rows["ONE"]["description"], "Do a thing")
        self.assertEqual(rows["ONE"]["icon_url"], "https://shared.akamai.steamstatic.com/community_assets/images/apps/1778820/aabb.jpg")
        self.assertTrue(rows["TWO"]["hidden"])
        self.assertTrue(rows["TWO"]["locked_icon_url"].endswith("/aacc.jpg"))
        self.assertEqual(rows["ONE"]["catalog_progress"], {"type": 1, "minimum": 0, "maximum": 10})
        self.assertEqual(rows["TWO"]["catalog_progress"], {"type": 2, "minimum": 0.5, "maximum": 2.5})
        self.assertFalse(any(field in row for row in rows.values() for field in ("unlocked", "unlock_time", "progress_current", "progress_max", "player_percent_unlocked")))
        query = urllib.parse.parse_qs(urllib.parse.urlparse(steam.calls[0]).query)
        self.assertEqual(query, {"appid": ["1778820"], "language": ["english"]})
        self.assertNotIn("key", query)
        self.assertNotIn("steamid", query)

    def test_public_catalog_rejects_incomplete_duplicate_or_unsafe_schema(self):
        steam = CatalogSteam()
        invalid = [{}, {"response": {}}, {"response": {"achievements": []}}, {"response": {"achievements": [None]}}]
        for field, value in [("internal_name", ""), ("localized_name", None), ("icon", "../outside.jpg"),
                             ("icon_gray", "https://outside/icon.png"), ("hidden", "false"),
                             ("max_progress_int", -1), ("max_progress_int", float("inf")),
                             ("progress_type", True), ("progress_type", 1.0)]:
            catalog = public_catalog()
            catalog["response"]["achievements"][0][field] = value
            invalid.append(catalog)
        duplicate = public_catalog()
        duplicate["response"]["achievements"].append(deepcopy(duplicate["response"]["achievements"][0]))
        invalid.append(duplicate)
        invalid.append({"response": {"achievements": [public_catalog()["response"]["achievements"][0]] * 5001}})
        for catalog in invalid:
            with self.subTest(catalog=catalog):
                steam.catalog = catalog
                with self.assertRaises(ValueError):
                    steam.schema_public("1778820")
        for appid in ("../1", "0", "4294967296", "1\u0667", 1778820):
            with self.subTest(appid=appid), self.assertRaises(ValueError):
                steam.schema_public(appid)

    def test_public_catalog_declared_group_total_rejects_valid_but_truncated_rows(self):
        steam = CatalogSteam()
        steam.catalog["response"]["groups"] = [{"groupid": 0, "total_achievements": 2, "completion_achievements": 1}]
        self.assertEqual(len(steam.schema_public("1778820")), 2)
        # Remaining row is otherwise valid: silently accepting it would report
        # an incorrect full total and mark the missing achievement absent.
        steam.catalog["response"]["achievements"].pop()
        with self.assertRaises(ValueError):
            steam.schema_public("1778820")

    def test_public_catalog_checks_each_group_and_preserves_archived_rows(self):
        steam = CatalogSteam()
        steam.catalog["response"]["achievements"][0].update(groupid=0, archived=False)
        steam.catalog["response"]["achievements"][1].update(groupid=7, archived=True)
        steam.catalog["response"]["groups"] = [
            {"groupid": 0, "total_achievements": 1, "completion_achievements": 0, "archived": False},
            {"groupid": 7, "total_achievements": 1, "completion_achievements": 0, "archived": True}]
        self.assertEqual(set(steam.schema_public("1778820")), {"ONE", "TWO"})
        # Overall sum is still two, but the per-group declaration is wrong.
        steam.catalog["response"]["groups"][0]["total_achievements"] = 2
        steam.catalog["response"]["groups"][1]["total_achievements"] = 0
        with self.assertRaises(ValueError):
            steam.schema_public("1778820")

    def test_public_catalog_supports_legacy_groups_without_declared_totals(self):
        steam = CatalogSteam()
        self.assertEqual(len(steam.schema_public("1778820")), 2)
        steam.catalog["response"]["groups"] = [{"groupid": 0}]
        self.assertEqual(len(steam.schema_public("1778820")), 2)
        steam.catalog["response"]["groups"] = []
        self.assertEqual(len(steam.schema_public("1778820")), 2)

    def test_empty_local_save_automatically_fetches_and_caches_public_schema(self):
        steam = CatalogSteam()
        manager = achievements.Manager(self.base, self.home, steam, settings={})
        original = self.state.read_bytes()
        manager.sync(self.entry)
        record = manager.data["games"][self.entry["id"]]
        self.assertEqual(record["status"], "ready")
        self.assertEqual(record["provider"], "RUNE local")
        self.assertEqual(record["total_count"], 2)
        self.assertEqual(record["unlocked_count"], 0)
        self.assertFalse(any(row["unlocked"] for row in record["achievements"]))
        self.assertFalse(any("progress_current" in row or "progress_max" in row for row in record["achievements"]))
        self.assertEqual(self.state.read_bytes(), original)
        self.assertEqual(list(manager.spool.glob("*.json")), [])
        self.assertEqual(len(steam.calls), 1)
        cached = json.loads((self.base / "schemas/1778820.json").read_text())
        self.assertEqual(len(cached["achievements"]), 2)
        self.assertEqual(cached["achievements"][0]["catalog_progress"]["maximum"], 10)
        steam.offline = True
        restarted = achievements.Manager(self.base, self.home, steam, settings={})
        restarted.sync(self.entry)
        self.assertEqual(restarted.data["games"][self.entry["id"]]["status"], "ready")
        self.assertEqual(len(steam.calls), 1, "persisted schema must not require networking after restart")
        self.assertEqual(restarted.data["games"][self.entry["id"]]["achievements"][0]["catalog_progress"]["maximum"], 10)

    def test_public_catalog_enrichment_cannot_create_unlock_or_replace_local_progress(self):
        self.manager.sync(self.entry)  # Already initialized with genuine Count=0.
        steam = CatalogSteam()
        manager = achievements.Manager(self.base, self.home, steam, settings={})
        manager.sync(self.entry, True)
        self.assertEqual(list(manager.spool.glob("*.json")), [])
        self.unlock(False)
        manager.sync(self.entry, True)
        record = manager.data["games"][self.entry["id"]]
        self.assertEqual(record["achievements"][0]["progress_current"], 2)
        self.assertEqual(record["achievements"][0]["progress_max"], 10)
        self.unlock()
        manager.sync(self.entry, True)
        self.assertEqual(len(list(manager.spool.glob("*.json"))), 1)
        manager.sync(self.entry, True)
        self.assertEqual(len(list(manager.spool.glob("*.json"))), 1)

    def test_existing_installed_cached_and_authenticated_schemas_have_precedence(self):
        steam = CatalogSteam()
        manager = achievements.Manager(self.base, self.home, steam, settings={"api_key": "fixture-key"})
        installed = Path(self.entry["executable"]).parent / "steam_settings/achievements.json"
        achievements.write_json(installed, {"achievements": [{"name": "LOCAL", "displayName": "Installed schema"}]})
        self.assertEqual(set(manager.load_schema("1778820", self.entry)), {"LOCAL"})
        self.assertEqual(steam.calls, [])
        self.schema()
        self.assertEqual(set(manager.load_schema("1778820", self.entry)), {"ONE", "TWO"})
        self.assertEqual(steam.calls, [])
        (self.base / "schemas/1778820.json").unlink()
        installed.unlink()
        steam.auth_rows = achievements.schema_rows({"achievements": [{"name": "AUTH", "displayName": "Authenticated schema"}]})
        self.assertEqual(set(manager.load_schema("1778820", self.entry)), {"AUTH"})
        self.assertEqual(steam.calls, ["authenticated:1778820"])

    def test_invalid_cache_and_failed_configured_schema_fall_back_to_public_catalog(self):
        achievements.write_json(self.base / "schemas/1778820.json", {"achievements": []})
        steam = CatalogSteam()
        manager = achievements.Manager(self.base, self.home, steam, settings={"api_key": "private-fixture-key"})
        manager.sync(self.entry)
        self.assertEqual(manager.data["games"][self.entry["id"]]["total_count"], 2)
        self.assertEqual(len(steam.calls), 2)
        self.assertTrue(steam.calls[1].startswith("https://api.steampowered.com/IPlayerService/"))
        self.assertNotIn("private-fixture-key", (self.base / "schemas/1778820.json").read_text())

    def test_bad_public_catalog_keeps_genuine_progress_and_bounds_retries(self):
        self.unlock()
        steam = CatalogSteam()
        steam.catalog = {"response": {"achievements": []}}
        moment = [1000]
        manager = achievements.Manager(self.base, self.home, steam, settings={}, clock=lambda: moment[0])
        manager.sync(self.entry)
        record = manager.data["games"][self.entry["id"]]
        self.assertEqual(record["status"], "partial")
        self.assertEqual(record["unlocked_count"], 1)
        self.assertEqual(record["achievements"][0]["unlock_time"], 1791230000)
        self.assertIsNone(record["total_count"])
        self.assertFalse((self.base / "schemas/1778820.json").exists())
        self.assertEqual(list(manager.spool.glob("*.json")), [])
        moment[0] += 11
        manager.sync(self.entry)
        self.assertEqual(len(steam.calls), 1, "failed schema must not stall each local poll")
        steam.catalog = public_catalog()
        manager.sync(self.entry, True)
        self.assertEqual(len(steam.calls), 2, "explicit refresh retries before cooldown expires")
        self.assertEqual(manager.data["games"][self.entry["id"]]["status"], "ready")
        self.assertEqual(manager.data["games"][self.entry["id"]]["unlocked_count"], 1)
        self.assertEqual(list(manager.spool.glob("*.json")), [], "schema-only enrichment must not announce historical unlocks")

    def test_public_catalog_does_not_turn_private_steam_profile_into_locked_state(self):
        steam = CatalogSteam()
        steam.public = lambda appid, profile: (_ for _ in ()).throw(ValueError("private profile"))
        manager = achievements.Manager(self.base, self.home, steam, settings={"steam_id": "76561198000000000"})
        manager.sync({"id": "steam.1778820", "title": "TEKKEN 8"})
        record = manager.data["games"]["steam.1778820"]
        self.assertEqual(record["status"], "unavailable")
        self.assertNotIn("total_count", record)
        self.assertEqual(steam.calls, [])

    def test_unavailable_public_catalog_does_not_hide_local_earned_state(self):
        self.unlock()
        steam = CatalogSteam()
        steam.offline = True
        manager = achievements.Manager(self.base, self.home, steam, settings={})
        manager.sync(self.entry)
        record = manager.data["games"][self.entry["id"]]
        self.assertEqual(record["status"], "partial")
        self.assertEqual(record["unlocked_count"], 1)
        self.assertEqual(record["achievements"][0]["unlock_time"], 1791230000)
        self.assertIsNone(record["total_count"])
        self.assertFalse((self.base / "schemas/1778820.json").exists())
        self.assertEqual(list(manager.spool.glob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
