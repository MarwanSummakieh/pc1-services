"""Exercise real save redirection without copying installed game binaries."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / "os/files/usr/lib/marwanos/profiles.py"
SPEC = importlib.util.spec_from_file_location("profiles", PATH)
profiles = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(profiles)
ALICE = "user-" + "a" * 32
BOB = "user-" + "b" * 32


@unittest.skipUnless(os.name == "posix", "Save bindings require Linux symbolic links")
class ProfileSaveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pc1 profile spaces ")
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.store = self.home / "profiles"
        self.store.mkdir()
        self.registry = {"schema_version": 1, "active": "owner", "users": [
            {"id": "owner", "name": "Original"}, {"id": ALICE, "name": "Alice"}, {"id": BOB, "name": "Bob"}]}
        self.write_registry()
        environment = patch.dict(os.environ, {"HOME": str(self.home), "MARWANOS_PROFILES_HOME": str(self.store),
                                              "MARWANOS_PROFILE_ID": "", "MARWANOS_WINDOWS_HOME": str(self.home / "windows")})
        environment.start()
        self.addCleanup(environment.stop)
        self.prefix = self.home / "windows/prefixes/local-fixture"
        self.save = self.prefix / "drive_c/users/steamuser/AppData/Local/Game/save.dat"
        self.save.parent.mkdir(parents=True)
        self.save.write_text("original progress")
        (self.prefix / "user.reg").write_text("original registry")
        self.executable = self.prefix / "drive_c/Games/Game.exe"
        self.executable.parent.mkdir(parents=True)
        self.executable.write_bytes(b"shared game installation")

    def write_registry(self):
        (self.store / "users.json").write_text(json.dumps(self.registry))

    def select(self, key):
        self.registry["active"] = key
        self.write_registry()

    def test_original_saves_survive_two_independent_players_and_repeated_switches(self):
        original_inode = self.executable.stat().st_ino
        profiles.prepare_windows(self.prefix, ALICE)
        self.assertFalse(self.save.exists())
        self.save.parent.mkdir(parents=True)
        self.save.write_text("Alice progress")
        (self.prefix / "user.reg").write_text("Alice registry")
        profiles.prepare_windows(self.prefix, BOB)
        self.assertFalse(self.save.exists())
        self.save.parent.mkdir(parents=True)
        self.save.write_text("Bob progress")
        profiles.prepare_windows(self.prefix, "owner")
        self.assertEqual(self.save.read_text(), "original progress")
        self.assertEqual((self.prefix / "user.reg").read_text(), "original registry")
        profiles.prepare_windows(self.prefix, ALICE)
        self.assertEqual(self.save.read_text(), "Alice progress")
        self.assertEqual((self.prefix / "user.reg").read_text(), "Alice registry")
        profiles.prepare_windows(self.prefix, BOB)
        self.assertEqual(self.save.read_text(), "Bob progress")
        self.assertEqual(self.executable.stat().st_ino, original_inode)
        self.assertEqual(list(self.store.rglob("*.exe")), [])

    def test_wine_atomic_registry_replacement_is_recovered_for_the_correct_player(self):
        profiles.prepare_windows(self.prefix, ALICE)
        registry = self.prefix / "user.reg"
        temporary = registry.with_suffix(".tmp")
        temporary.write_text("Alice latest registry")
        temporary.replace(registry)  # Wine replaces its symlink on commit.
        profiles.prepare_windows(self.prefix, BOB)
        self.assertNotIn("Alice", registry.read_text())
        profiles.prepare_windows(self.prefix, ALICE)
        self.assertEqual(registry.read_text(), "Alice latest registry")

    def test_interrupted_legacy_migration_recovers_without_deleting_saves(self):
        profiles.prepare_windows(self.prefix, "owner")
        (self.prefix / "drive_c/users").unlink()  # Crash before link creation.
        profiles.prepare_windows(self.prefix, "owner")
        self.assertEqual(self.save.read_text(), "original progress")

    def test_unknown_save_link_is_rejected_without_touching_target(self):
        folder = self.home / "outside"
        folder.mkdir()
        source = self.home / "external-save"
        source.symlink_to(folder)
        with self.assertRaisesRegex(ValueError, "outside"):
            profiles.bind_save(source, ALICE, "test")
        self.assertEqual(source.resolve(), folder)

    def test_conflicting_legacy_and_archived_saves_are_preserved(self):
        profiles.prepare_windows(self.prefix, "owner")
        source = self.prefix / "drive_c/users"
        archive = source.resolve()
        source.unlink()
        source.mkdir()
        (source / "unexpected").write_text("new data")
        with self.assertRaisesRegex(ValueError, "Both legacy"):
            profiles.prepare_windows(self.prefix, BOB)
        self.assertTrue(archive.is_dir())
        self.assertEqual((source / "unexpected").read_text(), "new data")

    def test_switching_users_preserves_shared_steam_account_and_external_proton_saves(self):
        steam = self.home / ".local/share/Steam"
        userdata = steam / "userdata/123/42/remote/save.dat"
        userdata.parent.mkdir(parents=True)
        userdata.write_text("original Steam save")
        login = steam / "config/loginusers.vdf"
        login.parent.mkdir()
        login.write_text('"users" { "123" { "RememberPassword" "1" } }')
        library = self.home / "External Games"
        config = steam / "steamapps/libraryfolders.vdf"
        config.parent.mkdir()
        config.write_text('"libraryfolders" { "1" { "path" ' + json.dumps(str(library)) + ' } }')
        prefix = library / "steamapps/compatdata/42/pfx"
        save = prefix / "drive_c/users/steamuser/Documents/save.dat"
        save.parent.mkdir(parents=True)
        save.write_text("original Proton save")
        binary = library / "steamapps/common/Game/Game.exe"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"one install")
        for key in (ALICE, BOB, "owner"):
            profiles.activate(key)
            self.assertEqual(userdata.read_text(), "original Steam save")
            self.assertEqual(save.read_text(), "original Proton save")
            self.assertEqual(binary.read_bytes(), b"one install")
            self.assertIn('"RememberPassword" "1"', login.read_text())
            self.assertFalse((steam / "userdata").is_symlink())
        self.assertEqual(list(self.store.rglob("steam-userdata")), [])

    def test_new_steam_prefix_remains_shared_after_switching_users(self):
        self.select(ALICE)
        prefix = self.home / ".local/share/Steam/steamapps/compatdata/99/pfx"
        save = prefix / "drive_c/users/steamuser/Documents/save.dat"
        save.parent.mkdir(parents=True)
        save.write_text("Alice installed and played this")
        profiles.activate(BOB)
        self.assertEqual(save.read_text(), "Alice installed and played this")
        profiles.activate(ALICE)
        self.assertEqual(save.read_text(), "Alice installed and played this")

    def test_non_steam_native_games_keep_private_home(self):
        env = profiles.game_environment(ALICE)
        self.assertNotEqual(env["HOME"], str(self.home))
        self.assertEqual(env["MARWANOS_PROFILE_ID"], ALICE)
        self.assertFalse((Path(env["HOME"]) / ".local/share/Steam").exists())
        self.assertEqual(profiles.game_environment("owner")["HOME"], str(self.home))

    def test_profile_traversal_and_nonexistent_users_are_rejected(self):
        for value in ("../owner", "user-" + "c" * 32, "", "/tmp/user", None):
            with self.assertRaises(ValueError):
                profiles.validate(value)

    def test_corrupt_registry_cannot_silently_fall_back_to_original_saves(self):
        (self.store / "users.json").write_text("{")
        with self.assertRaises(ValueError):
            profiles.active_id()

    def test_activation_does_not_invoke_steam_or_other_processes(self):
        with patch.object(subprocess, "run", side_effect=AssertionError("Steam must stay running")), patch.object(os, "kill", side_effect=AssertionError("Steam must stay running")):
            profiles.activate(ALICE)
        self.assertEqual(profiles.active_id(), ALICE)

    def register_fixture(self):
        path = self.home / "windows/apps/local-fixture.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"prefix": str(self.prefix), "executable": str(self.executable)}))

    def test_managed_launcher_runs_the_same_install_with_each_players_saves(self):
        self.register_fixture()
        runner = self.home / "fake-runtime"
        runner.write_text("#!" + sys.executable + "\n" + '''import os, pathlib
p = pathlib.Path(os.environ['WINEPREFIX']) / 'drive_c/users/steamuser/Documents/actual-save.dat'
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(os.environ['PROFILE_FIXTURE_SAVE'])
''')
        runner.chmod(0o755)
        for key, progress in [(ALICE, "Alice at level 7"), (BOB, "Bob at level 2")]:
            env = dict(os.environ, MARWANOS_PROFILE_ID=key, MARWANOS_WINDOWS_RUNTIME=str(runner), PROFILE_FIXTURE_SAVE=progress)
            result = subprocess.run([sys.executable, str(PATH.with_name("windows") / "manager.py"), "launch", "local-fixture"],
                                    env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
        for key, progress in [(ALICE, "Alice at level 7"), (BOB, "Bob at level 2")]:
            profiles.prepare_windows(self.prefix, key)
            self.assertEqual((self.prefix / "drive_c/users/steamuser/Documents/actual-save.dat").read_text(), progress)
        self.assertEqual(self.executable.read_bytes(), b"shared game installation")

    def test_activation_commits_selected_user_after_rebinding_installed_saves(self):
        self.register_fixture()
        profiles.activate(ALICE)
        self.assertEqual(profiles.active_id(), ALICE)
        self.assertFalse(self.save.exists())
        profiles.prepare_windows(self.prefix, "owner")
        self.assertEqual(self.save.read_text(), "original progress")

    def test_failed_profile_commit_restores_the_original_save_bindings(self):
        self.register_fixture()
        original_write = profiles.write_json
        def fail_commit(path, value):
            if path.name == "users.json":
                raise OSError("disk full")
            original_write(path, value)
        with patch.object(profiles, "write_json", side_effect=fail_commit):
            with self.assertRaisesRegex(OSError, "disk full"):
                profiles.activate(ALICE)
        self.assertEqual(profiles.active_id(), "owner")
        self.assertEqual(self.save.read_text(), "original progress")


if __name__ == "__main__":
    unittest.main()
