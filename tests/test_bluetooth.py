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


class ControllerBatteryChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.power = self.root / "power"
        self.power.mkdir()
        self.address = "aa:bb:cc:dd:ee:ff"
        self.supply = self.power / ("ps-controller-battery-" + self.address)
        self.supply.mkdir()
        self.backend = FakeBlueZ()
        self.backend.data[D][bluetooth.DEVICE].update({"Address": self.address.upper(), "Connected": True})
        self.tracker = bluetooth.BatteryTracker(self.root / "history", self.power, self.root / "spool")

    def sample(self, at=100):
        devices = bluetooth.snapshot(self.backend.data)["devices"]
        self.tracker.update(devices, self.backend.data, at)
        return devices[0]["battery"]

    def report(self, capacity, status="Discharging"):
        (self.supply / "capacity").write_text(str(capacity))
        (self.supply / "status").write_text(status)

    def test_native_report_then_disconnect_retains_dated_battery_after_restart(self):
        self.report(15)
        self.assertEqual(self.sample()["percent"], 15)
        (self.supply / "capacity").unlink()
        self.backend.data[D][bluetooth.DEVICE]["Connected"] = False
        battery = self.sample(110)
        self.assertIsNone(battery["percent"])
        self.assertEqual((battery["last_percent"], battery["last_seen"]), (15, 100))
        self.tracker = bluetooth.BatteryTracker(self.root / "history", self.power)
        self.assertEqual(self.sample(120)["last_percent"], 15)
        self.assertEqual(len(self.tracker.data["events"]), 2)

    def test_missing_or_invalid_report_is_unknown_never_zero_or_initial_full(self):
        self.assertIsNone(self.sample()["percent"])
        for percent, status in [(101, "Discharging"), (-1, "Discharging"), (100, "Unknown"), ("bad", "Charging")]:
            self.report(percent, status)
            self.assertIsNone(self.sample()["percent"])
        self.report(0)
        self.assertEqual(self.sample()["percent"], 0)

    def test_low_warnings_once_per_threshold_and_rearm_on_charge(self):
        self.report(15)
        self.sample()
        event = next((self.root / "spool").glob("*.json"))
        self.assertIn("15%", event.read_text())
        event.unlink()
        self.sample(101)
        self.assertFalse(event.exists())
        self.report(5)
        self.sample(102)
        self.assertIn("5%", event.read_text())
        event.unlink()
        self.report(5, "Charging")
        self.sample(103)
        self.assertFalse(event.exists())
        self.report(5)
        self.sample(104)
        self.assertTrue(event.exists())

    def test_bluez_fallback_is_ignored_after_disconnect_and_native_takes_priority(self):
        self.backend.data[D]["org.bluez.Battery1"] = {"Percentage": 45}
        self.assertEqual(self.sample()["percent"], 45)
        self.report(85)
        self.assertEqual(self.sample()["percent"], 85)
        (self.supply / "capacity").unlink()
        self.backend.data[D][bluetooth.DEVICE]["Connected"] = False
        self.assertIsNone(self.sample()["percent"])

    def test_usb_transport_is_reported_without_bluetooth_connection(self):
        real = self.root / "0003:054C:0CE6.0007/power_supply" / self.supply.name
        real.mkdir(parents=True)
        self.supply.rmdir()
        self.supply.symlink_to(real, target_is_directory=True)
        self.report(5, "Charging")
        self.backend.data[D][bluetooth.DEVICE]["Connected"] = False
        battery = self.sample()
        self.assertEqual((battery["transport"], battery["percent"]), ("usb", 5))

    def test_controllers_do_not_share_readings_and_history_is_bounded(self):
        second = D + "_2"
        self.backend.data[second] = {bluetooth.DEVICE: {"Adapter": A, "Address": "11:22:33:44:55:66", "Connected": True}}
        self.report(75)
        devices = bluetooth.snapshot(self.backend.data)["devices"]
        self.tracker.update(devices, self.backend.data, 100)
        self.assertIsNone(next(d for d in devices if d["id"] == second)["battery"]["percent"])
        for i in range(520):
            self.report(75 if i % 2 else 85)
            self.sample(101 + i)
        self.assertEqual(len(self.tracker.data["events"]), 512)


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
