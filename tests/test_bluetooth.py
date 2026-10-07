import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bluetooth_manager", ROOT / "os/files/usr/lib/marwanos/bluetooth/manager.py")
bluetooth = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bluetooth)
A = "/org/bluez/hci0"
D = A + "/dev_AA_BB_CC_DD_EE_FF"


class FakeBlueZ:
    def __init__(self):
        self.data = {A: {bluetooth.ADAPTER: {"Alias": "Dongle", "Powered": True}},
                     D: {bluetooth.DEVICE: {"Adapter": A, "Alias": "Wireless Controller", "Paired": False, "Trusted": False, "Connected": False}}}
        self.calls, self.pair_success, self.pair_failure = [], None, None

    def objects(self):
        return self.data

    def set(self, path, interface, key, value):
        self.calls.append(("set", path, key, value))
        self.data[path][interface][key] = value

    def call(self, path, interface, method, args, success, failure):
        self.calls.append((method, path, args))
        if method == "Pair":
            self.pair_success, self.pair_failure = success, failure
        else:
            if method == "Connect":
                self.data[path][interface]["Connected"] = True
            elif method == "Disconnect":
                self.data[path][interface]["Connected"] = False
            elif method in ("StartDiscovery", "StopDiscovery"):
                self.data[path][interface]["Discovering"] = method == "StartDiscovery"
            elif method == "RemoveDevice":
                del self.data[args[0]]
            success()


class MissingBlueZ(Exception):
    def __init__(self, name):
        self.name = name

    def get_dbus_name(self):
        return self.name


class BluetoothAvailabilityChecks(unittest.TestCase):
    def test_hardware_condition_skipped_service_reports_no_adapter_then_hotplug(self):
        with tempfile.TemporaryDirectory() as directory:
            hci = Path(directory) / "sys-bluetooth"
            hci.mkdir()
            backend = FakeBlueZ()
            absent = True
            def query():
                if absent:
                    raise MissingBlueZ("org.freedesktop.DBus.Error.ServiceUnknown")
                return backend.data
            backend.objects = lambda: bluetooth.managed_objects(query, hci)
            manager = bluetooth.Manager(backend, Path(directory) / "status")
            manager.tick()
            status = json.loads((Path(directory) / "status/state.json").read_text())
            self.assertEqual(status["status"], "no-adapter")
            self.assertTrue(status["available"])
            self.assertEqual(status["error"], "")
            (hci / "hci0").mkdir()
            manager.tick()
            self.assertEqual(manager.state["status"], "unavailable")
            self.assertIn("Bluetooth change failed", manager.error)
            (hci / "hci0").rmdir()
            manager.tick()
            self.assertEqual(manager.state["status"], "no-adapter")
            self.assertEqual(manager.error, "")
            (hci / "hci0").mkdir()
            absent = False
            manager.tick()
            self.assertEqual(manager.state["status"], "ready")
            self.assertEqual(len(manager.state["adapters"]), 1)

    def test_missing_owner_without_hardware_is_clean_but_other_errors_are_not_hidden(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in ["org.freedesktop.DBus.Error.ServiceUnknown",
                         "org.freedesktop.DBus.Error.NameHasNoOwner"]:
                def query():
                    raise MissingBlueZ(name)
                self.assertEqual(bluetooth.managed_objects(query, directory), {})
            for name in ["org.freedesktop.DBus.Error.AccessDenied", "org.freedesktop.DBus.Error.NoReply"]:
                def query():
                    raise MissingBlueZ(name)
                with self.assertRaises(MissingBlueZ):
                    bluetooth.managed_objects(query, directory)

    def test_missing_service_with_real_adapter_remains_a_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "hci0").mkdir()
            for name in ["org.freedesktop.DBus.Error.ServiceUnknown",
                         "org.freedesktop.DBus.Error.NameHasNoOwner"]:
                def query():
                    raise MissingBlueZ(name)
                with self.assertRaises(MissingBlueZ):
                    bluetooth.managed_objects(query, directory)

    def test_successful_private_bus_adapter_is_not_filtered_by_host_hardware(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = FakeBlueZ()
            objects = bluetooth.managed_objects(backend.objects, directory)
            self.assertEqual(bluetooth.snapshot(objects)["status"], "ready")
            self.assertEqual(len(bluetooth.snapshot(objects)["adapters"]), 1)


class BluetoothChecks(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.backend = FakeBlueZ()
        self.now = 100
        self.manager = bluetooth.Manager(self.backend, self.folder.name, lambda: self.now)

    def request(self, action, target=D, **extra):
        self.manager.apply({"action": action, "target": target, **extra})

    def test_no_adapter_and_hotplug(self):
        original = self.backend.data
        self.backend.data = {}
        self.manager.tick()
        self.assertEqual(self.manager.state["status"], "no-adapter")
        with self.assertRaises(bluetooth.BluetoothError):
            self.request("scan", A)
        self.backend.data = original
        self.manager.tick()
        self.assertEqual(self.manager.state["status"], "ready")

    def test_scan_ends_after_a_minute(self):
        self.request("scan", A)
        self.assertTrue(self.backend.data[A][bluetooth.ADAPTER]["Discovering"])
        self.now += 61
        self.manager.tick()
        self.assertFalse(self.backend.data[A][bluetooth.ADAPTER]["Discovering"])

    def test_pair_confirm_trust_connect(self):
        self.request("pair")
        answers = []
        self.manager.ask(D, "confirm", "012345", lambda approved, code: answers.append(approved))
        token = self.manager.prompt["token"]
        with self.assertRaises(bluetooth.BluetoothError):
            self.request("respond", token="stale", approved=True)
        self.request("respond", token=token, approved=True)
        self.assertEqual(answers, [True])
        self.backend.data[D][bluetooth.DEVICE]["Paired"] = True
        self.backend.pair_success()
        self.assertTrue(self.backend.data[D][bluetooth.DEVICE]["Trusted"])
        self.assertTrue(self.backend.data[D][bluetooth.DEVICE]["Connected"])

    def test_unsolicited_agent_request_rejected(self):
        answers = []
        self.manager.ask(D, "authorize", "", lambda approved, code: answers.append(approved))
        self.assertEqual(answers, [False])
        self.assertFalse(self.manager.prompt)

    def test_controller_numeric_passkey(self):
        self.request("pair")
        answers = []
        self.manager.ask(D, "passkey", "", lambda approved, code: answers.append(code))
        token = self.manager.prompt["token"]
        with self.assertRaises(bluetooth.BluetoothError):
            self.request("respond", token=token, approved=True, value="1234567")
        self.request("respond", token=token, approved=True, value="000123")
        self.assertEqual(answers, [123])

    def test_timeout_rejects_prompt_and_cancels_pair(self):
        self.request("pair")
        answers = []
        self.manager.ask(D, "authorize", "", lambda approved, code: answers.append(approved))
        self.now += 61
        self.manager.tick()
        self.assertEqual(answers, [False])
        self.assertIn("timed out", self.manager.error)
        self.assertIn("CancelPairing", [call[0] for call in self.backend.calls])
        # The old Pair reply cannot trust/connect after cancellation.
        self.backend.pair_success()
        self.assertFalse(self.backend.data[D][bluetooth.DEVICE]["Trusted"])

    def test_reconnect_backoff_and_manual_disconnect(self):
        self.backend.data[D][bluetooth.DEVICE].update(Paired=True, Trusted=True)
        self.manager.tick()
        self.assertTrue(self.backend.data[D][bluetooth.DEVICE]["Connected"])
        self.request("disconnect")
        self.now += 100
        self.manager.tick()
        self.assertFalse(self.backend.data[D][bluetooth.DEVICE]["Connected"])
        self.request("connect")
        self.assertTrue(self.backend.data[D][bluetooth.DEVICE]["Connected"])

    def test_stale_or_unknown_paths_and_actions_rejected(self):
        for action, target in [("pair", "/org/other/service"), ("command", D), ("remove", A)]:
            with self.assertRaises(bluetooth.BluetoothError):
                self.request(action, target)

    def test_close_cancels_and_stops_discovery(self):
        self.request("scan", A)
        self.request("pair")
        self.request("close")
        self.assertFalse(self.backend.data[A][bluetooth.ADAPTER]["Discovering"])
        self.assertFalse(self.manager.pairing)

    def test_atomic_request_and_status(self):
        self.manager.publish()
        path = Path(self.folder.name)
        (path / "request.json").write_text(json.dumps({"action": "scan", "target": A}))
        self.manager.tick()
        self.assertFalse((path / "request.json").exists())
        state = json.loads((path / "state.json").read_text())
        self.assertEqual(state["status"], "ready")
        self.assertFalse((path / "state.json.tmp").exists())


class BluetoothDBusChecks(unittest.TestCase):
    def test_real_agent_roundtrip_on_private_bus(self):
        try:
            import dbus
            from gi.repository import GLib
        except ImportError:
            self.skipTest("Private D-Bus integration requires python3-dbus and python3-gobject")
        if not shutil.which("dbus-daemon"):
            self.skipTest("Private D-Bus integration requires dbus-daemon")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            daemon = subprocess.Popen(["dbus-daemon", "--session", "--nofork", "--print-address=1"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.addCleanup(lambda: daemon.kill() if daemon.poll() is None else None)
            self.addCleanup(daemon.stdout.close)
            self.addCleanup(daemon.stderr.close)
            address = daemon.stdout.readline().strip()
            env = {**os.environ, "DBUS_SYSTEM_BUS_ADDRESS": address, "XDG_RUNTIME_DIR": directory}
            log = (path / "process.log").open("w")
            self.addCleanup(log.close)
            fixture = subprocess.Popen([sys.executable, str(ROOT / "tests/bluetooth_dbus_fixture.py"), str(path / "ready")], env=env, stdout=log, stderr=log)
            self.addCleanup(lambda: fixture.kill() if fixture.poll() is None else None)
            self.wait_for(lambda: (path / "ready").exists(), path)
            worker = subprocess.Popen([sys.executable, str(ROOT / "os/files/usr/lib/marwanos/bluetooth/manager.py")], env=env, stdout=log, stderr=log)
            self.addCleanup(lambda: worker.kill() if worker.poll() is None else None)
            folder = path / "marwanos/bluetooth"
            def state():
                try:
                    return json.loads((folder / "state.json").read_text())
                except (FileNotFoundError, ValueError):
                    return {}
            self.wait_for(lambda: state().get("status") == "ready", path)
            bluetooth.atomic_json(folder / "request.json", {"id": "pair", "action": "pair", "target": D})
            self.wait_for(lambda: state().get("prompt", {}).get("kind") == "confirm", path)
            prompt = state()["prompt"]
            self.assertEqual(prompt["value"], "123456")
            bluetooth.atomic_json(folder / "request.json", {"action": "respond", "token": prompt["token"], "approved": True})
            self.wait_for(lambda: any(item.get("connected") and item.get("trusted") and item.get("paired") for item in state().get("devices", [])), path)
            self.assertEqual(state()["busy"], "")
            worker.terminate()
            fixture.terminate()
            daemon.terminate()
            worker.wait(timeout=5)
            fixture.wait(timeout=5)
            daemon.wait(timeout=5)

    def wait_for(self, predicate, path):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail("D-Bus operation timed out: " + (path / "process.log").read_text())


if __name__ == "__main__":
    unittest.main()
