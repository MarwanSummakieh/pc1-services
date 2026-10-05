import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("audio_manager", ROOT / "os/files/usr/lib/marwanos/audio/manager.py")
audio = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audio)


def node(index, name, **extra):
    return {"index": index, "name": name, "description": name, "mute": False,
            "volume": {"left": {"value": 32768}, "right": {"value": 32768}}, **extra}


class Server:
    def __init__(self):
        self.calls = []
        self.fail = None
        self.listing = {
            "sinks": [node(1, "TV HDMI", ports=[{"name": "hdmi", "description": "HDMI", "availability": "available"},
                                                {"name": "unplugged", "availability": "not available"}], active_port="hdmi"),
                      node(2, "USB Headphones")],
            "sources": [node(3, "USB Microphone", monitor_of_sink=None),
                        node(4, "TV.monitor", monitor_of_sink=1)],
            "sink_inputs": [node(7, "stream", sink=1, properties={"application.name": "Game", "object.serial": "22"},
                                 has_volume=True, volume_writable=True)],
            "source_outputs": [{"index": 8, "source": 3}, {"index": 9, "source": 4}],
        }
        self.info = {"default_sink_name": "TV HDMI", "default_source_name": "USB Microphone"}

    def run(self, *args):
        self.calls.append(args)
        if self.fail and self.fail(args):
            raise audio.AudioError("Disconnected")
        if args == ("--format=json", "list"):
            return json.dumps(self.listing)
        if args == ("--format=json", "info"):
            return json.dumps(self.info)
        if args[0] == "set-default-sink":
            self.info["default_sink_name"] = args[1]
        if args[0] == "set-sink-volume":
            for item in self.listing["sinks"]:
                if item["name"] == args[1]:
                    item["volume"] = {"mono": {"value": round(int(args[2][:-1]) * 65536 / 100)}}
        return ""


class AudioTests(unittest.TestCase):
    def setUp(self):
        self.server = Server()
        self.state = audio.snapshot(self.server.run)
        self.server.calls.clear()

    def request(self, kind="output", action="volume", target="TV HDMI", value=65, **extra):
        return {"id": "test-1", "kind": kind, "action": action, "target": target, "value": value, **extra}

    def apply(self, request):
        audio.apply(request, self.state, self.server.run)

    def test_snapshot_labels_channels_ports_and_monitors(self):
        self.assertEqual(self.state["outputs"][0]["volume"], 50)
        self.assertEqual(len(self.state["inputs"]), 1)
        self.assertEqual(self.state["outputs"][0]["ports"], [{"name": "hdmi", "label": "HDMI"}])
        self.assertEqual(self.state["streams"][0]["label"], "Game")
        self.assertEqual(self.state["streams"][0]["serial"], "22")

    def test_volume_and_mute_argv(self):
        self.apply(self.request())
        self.apply(self.request("input", "mute", "USB Microphone", True))
        self.apply(self.request("stream", "volume", "7", 0, serial="22"))
        self.assertEqual(self.server.calls, [("set-sink-volume", "TV HDMI", "65%"),
            ("set-source-mute", "USB Microphone", "1"), ("set-sink-input-volume", "7", "0%")])

    def test_invalid_requests_do_not_execute(self):
        bad = [self.request(value=value) for value in (-1, 101, True, "65%", float("nan"), float("inf"), None)]
        bad += [self.request(action="exec"), self.request(kind="card"), self.request(action="mute", value="toggle"),
                self.request(action="port", value="unplugged"), self.request(target="$(touch /tmp/unwanted)"),
                self.request("stream", "default", "7", serial="22")]
        for request in bad:
            with self.subTest(request=request), self.assertRaises(audio.AudioError):
                self.apply(request)
        self.assertEqual(self.server.calls, [])

    def test_default_output_moves_live_apps(self):
        self.apply(self.request(action="default", target="USB Headphones"))
        self.assertEqual(self.server.calls, [("set-default-sink", "USB Headphones"),
                                           ("move-sink-input", "7", "USB Headphones")])

    def test_input_switch_preserves_loopback_recordings(self):
        self.apply(self.request("input", "default", "USB Microphone"))
        self.assertEqual(self.server.calls, [("set-default-source", "USB Microphone"),
                                           ("move-source-output", "8", "USB Microphone")])

    def test_partial_switch_reports_failure(self):
        self.server.fail = lambda args: args[0] == "move-sink-input"
        with self.assertRaisesRegex(audio.AudioError, "Device selected"):
            self.apply(self.request(action="default", target="USB Headphones"))
        self.assertEqual(self.server.info["default_sink_name"], "USB Headphones")

    def test_unplugged_or_reused_stream_is_rejected(self):
        with self.assertRaises(audio.AudioError):
            self.apply(self.request("stream", "mute", "7", True, serial="old"))
        self.state["outputs"].clear()
        with self.assertRaises(audio.AudioError):
            self.apply(self.request())
        self.assertEqual(self.server.calls, [])

    def test_read_only_stream_and_available_port(self):
        self.state["streams"][0]["adjustable"] = False
        with self.assertRaises(audio.AudioError):
            self.apply(self.request("stream", "volume", "7", 50, serial="22"))
        self.apply(self.request(action="port", value="hdmi"))
        self.assertEqual(self.server.calls, [("set-sink-port", "TV HDMI", "hdmi")])

    def test_acknowledgement_reads_actual_volume_and_atomic_publish(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = audio.Manager(folder, self.server.run)
            path = Path(folder)
            (path / "request.json").write_text(json.dumps(self.request()))
            manager.tick()
            state = json.loads((path / "state.json").read_text())
            self.assertEqual(state["request_id"], "test-1")
            self.assertEqual(state["error"], "")
            self.assertEqual(state["outputs"][0]["volume"], 65)
            self.assertFalse((path / "request.json").exists())
            self.assertFalse((path / "state.tmp").exists())
            self.assertGreater(state["updated_at"], 0)
            if os.name != "nt":
                self.assertEqual((path / "state.json").stat().st_mode & 0o777, 0o600)

    def test_server_loss_acknowledges_and_recovers(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = audio.Manager(folder, self.server.run)
            path = Path(folder)
            (path / "request.json").write_text(json.dumps(self.request()))
            self.server.fail = lambda args: True
            manager.tick()
            state = json.loads((path / "state.json").read_text())
            self.assertFalse(state["available"])
            self.assertEqual(state["request_id"], "test-1")
            self.assertTrue(state["error"])
            self.server.fail = None
            manager.tick()
            self.assertTrue(json.loads((path / "state.json").read_text())["available"])

    def test_malformed_and_partial_requests_do_not_crash(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = audio.Manager(folder, self.server.run)
            path = Path(folder)
            for raw in ("{partial", "[]", '"command"', "x" * 8193):
                (path / "request.json").write_text(raw)
                manager.tick()
                result = json.loads((path / "state.json").read_text())
                self.assertTrue(result["available"])
                self.assertIn("Invalid audio request", result["error"])
            (path / "request.json.tmp").write_text("{partial")
            manager.tick()
            self.assertTrue((path / "request.json.tmp").exists())

    def test_refresh_acknowledges_and_clears_previous_error(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = audio.Manager(folder, self.server.run)
            manager.reply = {"request_id": "old", "error": "Previous failure"}
            path = Path(folder)
            (path / "request.json").write_text(json.dumps({"id": "refresh", "action": "refresh"}))
            manager.tick()
            state = json.loads((path / "state.json").read_text())
            self.assertEqual(state["request_id"], "refresh")
            self.assertEqual(state["error"], "")

    def test_idle_server_outage_clears_on_recovery(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = audio.Manager(folder, self.server.run)
            path = Path(folder)
            (path / "request.json").write_text(json.dumps(self.request()))
            manager.tick()
            self.server.fail = lambda args: True
            manager.tick()
            self.assertFalse(json.loads((path / "state.json").read_text())["available"])
            self.server.fail = None
            manager.tick()
            result = json.loads((path / "state.json").read_text())
            self.assertTrue(result["available"])
            self.assertEqual(result["error"], "")

    def test_pactl_timeout_and_failure_are_errors(self):
        with patch.object(audio.subprocess, "run", side_effect=subprocess.TimeoutExpired("pactl", 3)):
            with self.assertRaisesRegex(audio.AudioError, "unavailable"):
                audio.pactl("list")
        with patch.object(audio.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "internal detail")):
            with self.assertRaisesRegex(audio.AudioError, "disconnected"):
                audio.pactl("set-sink-volume", "TV", "50%")


if __name__ == "__main__":
    unittest.main()
