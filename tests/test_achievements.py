import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import urllib.parse

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


if __name__ == "__main__":
    unittest.main()
