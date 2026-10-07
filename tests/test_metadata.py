import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1] / "os/files/usr/lib/marwanos/metadata/manager.py"
spec = importlib.util.spec_from_file_location("metadata_manager", MODULE)
metadata = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metadata)


class Provider:
    def __init__(self):
        self.candidates = [{"provider_id": "1778820", "title": "TEKKEN 8"}]
        self.calls = []
        self.offline = False

    def search(self, title):
        self.calls.append(("search", title))
        if self.offline:
            raise OSError("offline")
        return self.candidates

    def details(self, appid):
        self.calls.append(("details", appid))
        if self.offline:
            raise OSError("offline")
        return {"provider": "fixture", "provider_id": appid, "title": "TEKKEN 8",
                "description": "A fighting game", "release_date": "2024", "genres": ["Action"],
                "developers": ["Bandai Namco"], "publishers": ["Bandai Namco"],
                "platforms": ["windows"], "art_urls": {"cover": "https://fixture/" + appid}}

    def fetch(self, url, limit):
        from PIL import Image
        if self.offline:
            raise OSError("offline")
        data = io.BytesIO()
        Image.new("RGB", (8, 12), "red").save(data, "PNG")
        return data.getvalue()


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.provider = Provider()
        self.manager = metadata.Manager(Path(self.temp.name), self.provider)
        self.entry = {"id": "managed.tekken", "title": "TEKKEN 8", "exec": ["original"]}

    def record(self):
        return self.manager.state["games"][self.entry["id"]]

    def test_unicode_names_are_not_empty_or_equated(self):
        self.assertNotEqual(metadata.normalized("鉄拳"), "")
        self.assertNotEqual(metadata.normalized("鉄拳"), metadata.normalized("幻想"))
        self.assertEqual(metadata.normalized("Pokémon"), metadata.normalized("Pokemon"))

    def test_import_download_and_offline_restart_keep_identity(self):
        self.manager.enrich(self.entry)
        item = self.record()
        self.assertEqual(item["status"], "ready")
        self.assertEqual(item["source"], "Windows")
        self.assertTrue(Path(item["assets"]["cover"]["path"]).is_file())
        self.assertEqual(item["assets"]["cover"]["sha256"], metadata.hashlib.sha256(
            Path(item["assets"]["cover"]["path"]).read_bytes()).hexdigest())
        self.assertEqual(self.entry["exec"], ["original"])
        self.provider.offline = True
        self.provider.calls.clear()
        restarted = metadata.Manager(self.manager.base, self.provider)
        restarted.enrich(self.entry)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(restarted.state["games"][self.entry["id"]]["assets"], item["assets"])

    def test_ambiguous_and_inexact_names_require_correction(self):
        self.provider.candidates.append({"provider_id": "22", "title": "TEKKEN 8"})
        self.manager.enrich(self.entry)
        self.assertEqual(self.record()["status"], "needs-match")
        self.assertFalse(any(call[0] == "details" for call in self.provider.calls))
        self.provider.candidates = [{"provider_id": "2", "title": "TEKKEN 8 Demo"}]
        self.manager.enrich(self.entry, force=True)
        self.assertEqual(self.record()["status"], "needs-match")

    def test_manual_match_and_title_survive_refresh(self):
        self.manager.request(dict(action="match", game_id=self.entry["id"], provider_id="22"), [self.entry])
        self.manager.request(dict(action="title", game_id=self.entry["id"], title="My Tekken"), [self.entry])
        self.manager.enrich(self.entry, force=True)
        self.assertEqual(self.record()["manual_match"], "22")
        self.assertEqual(self.record()["overrides"]["title"], "My Tekken")
        self.assertEqual(self.provider.calls[-1], ("details", "22"))

    def test_failed_refresh_retains_cache_and_can_retry(self):
        self.manager.enrich(self.entry)
        assets = self.record()["assets"].copy()
        self.provider.offline = True
        self.manager.enrich(self.entry, force=True)
        self.assertEqual(self.record()["status"], "error")
        self.assertEqual(self.record()["assets"], assets)
        self.provider.offline = False
        self.manager.enrich(self.entry, force=True)
        self.assertEqual(self.record()["status"], "ready")

    def test_missing_art_is_partial_and_bad_bytes_never_cached(self):
        self.provider.fetch = lambda *args: b"not an image"
        self.manager.enrich(self.entry)
        self.assertEqual(self.record()["status"], "partial")
        self.assertEqual(self.record()["assets"], {})
        self.assertEqual(self.record()["title"], "TEKKEN 8")

    def test_corrected_match_never_uses_wrong_art(self):
        self.manager.enrich(self.entry)
        self.provider.fetch = lambda *args: b"invalid"
        self.manager.enrich(self.entry, force=True, match="22")
        self.assertEqual(self.record()["assets"], {})
        self.assertEqual(self.record()["manual_match"], "22")

    def test_steam_id_bypasses_title_search(self):
        self.entry["id"] = "steam.1778820"
        self.manager.enrich(self.entry)
        self.assertEqual(self.provider.calls, [("details", "1778820")])
        self.assertEqual(self.record()["source"], "Steam")

    def test_library_ignores_installers_pending_and_utilities(self):
        apps = Path(self.temp.name) / "apps.tsv"
        apps.write_text("steam\tSteam\t\t\tsteam\tinstalled\nsteam.22\tGame\t\t\tlaunch\tinstalled\nsteam.23\tPending\t\t\t\tdownloading\n")
        windows = Path(self.temp.name) / "windows"
        (windows / "apps").mkdir(parents=True)
        (windows / "apps/game.json").write_text(json.dumps(dict(self.entry, state="installed")))
        entries = metadata.library(apps, windows)
        self.assertEqual({entry["id"] for entry in entries}, {"steam.22", "managed.tekken"})

    def test_search_and_no_results_do_not_replace_metadata(self):
        self.manager.enrich(self.entry)
        self.provider.candidates = []
        self.manager.request(dict(action="search", game_id=self.entry["id"], query="bad"), [self.entry])
        self.assertEqual(self.record()["title"], "TEKKEN 8")
        self.assertTrue(self.record()["search_error"])

    def test_crash_during_download_retries_on_worker_restart(self):
        self.manager.state["games"][self.entry["id"]] = {"status": "loading", "import_title": "TEKKEN 8", "attempted_at": metadata.time.time()}
        self.manager.publish()
        restarted = metadata.Manager(self.manager.base, self.provider)
        restarted.enrich(self.entry)
        self.assertEqual(restarted.state["games"][self.entry["id"]]["status"], "ready")

    def test_failed_match_correction_retains_choice_and_discards_old_game(self):
        self.manager.enrich(self.entry)
        self.provider.offline = True
        self.manager.enrich(self.entry, force=True, match="22")
        self.assertEqual(self.record()["manual_match"], "22")
        self.assertNotIn("assets", self.record())
        self.provider.offline = False
        self.manager.enrich(self.entry, force=True)
        self.assertEqual(self.record()["provider_id"], "22")


if __name__ == "__main__":
    unittest.main()
