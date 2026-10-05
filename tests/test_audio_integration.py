"""Opt-in, isolated PipeWire test; never connects to the user's audio server."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("audio_integration_manager", ROOT / "os/files/usr/lib/marwanos/audio/manager.py")
audio = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audio)


@unittest.skipUnless(os.environ.get("MARWANOS_AUDIO_INTEGRATION") == "1", "Opt-in isolated PipeWire integration test")
class PipeWireTests(unittest.TestCase):
    def test_live_volume_mute_switch_and_unplug(self):
        pactl = os.environ.get("MARWANOS_AUDIO_TEST_PACTL") or shutil.which("pactl")
        pacat = os.environ.get("MARWANOS_AUDIO_TEST_PACAT") or shutil.which("pacat")
        self.assertTrue(pactl and pacat and shutil.which("pipewire") and shutil.which("pipewire-pulse") and shutil.which("wireplumber"))
        with tempfile.TemporaryDirectory(prefix="pc1-audio-live-") as folder:
            runtime = Path(folder)
            runtime.chmod(0o700)
            env = {**os.environ, "XDG_RUNTIME_DIR": folder, "PIPEWIRE_RUNTIME_DIR": folder,
                   "XDG_CONFIG_HOME": str(runtime / "config"), "PULSE_RUNTIME_PATH": str(runtime / "pulse"),
                   "XDG_STATE_HOME": str(runtime / "state"), "XDG_DATA_HOME": str(runtime / "data"),
                   "PULSE_SERVER": "unix:" + str(runtime / "pulse/native"), "LC_ALL": "C"}
            env.pop("PIPEWIRE_REMOTE", None)
            processes = []
            logs = []

            def run(*args):
                result = subprocess.run([pactl, *args], env=env, capture_output=True, text=True, timeout=3)
                if result.returncode:
                    raise audio.AudioError(result.stderr.strip())
                return result.stdout

            def wait_for(predicate):
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    try:
                        state = audio.snapshot(run)
                        if predicate(state):
                            return state
                    except audio.AudioError:
                        pass
                    time.sleep(0.05)
                self.fail("PipeWire did not reach expected state")

            try:
                # The policy profile manages defaults and streams without probing hardware.
                for binary in ("pipewire", "pipewire-pulse", "wireplumber"):
                    log = open(runtime / (binary + ".log"), "w+")
                    logs.append(log)
                    args = [shutil.which(binary)]
                    if binary == "wireplumber":
                        args += ["--profile=policy"]
                    processes.append(subprocess.Popen(args, env=env, stdout=log, stderr=log))
                wait_for(lambda state: state["available"])
                module_a = run("load-module", "module-null-sink", "sink_name=bench_a").strip()
                run("load-module", "module-null-sink", "sink_name=bench_b")
                state = wait_for(lambda state: len(state["outputs"]) == 2)
                self.assertEqual(state["inputs"], [], "speaker monitors are excluded")
                audio.apply({"kind": "output", "action": "default", "target": "bench_a"}, state, run)
                log = open(runtime / "pacat.log", "w+")
                logs.append(log)
                with open("/dev/zero", "rb") as silence:
                    processes.append(subprocess.Popen(
                        [pacat, "--playback", "--raw", "--rate=44100", "--channels=2", "--format=s16le",
                         "--property=application.name=Audio integration"], env=env, stdin=silence, stdout=log, stderr=log))
                state = wait_for(lambda state: len(state["streams"]) == 1)
                stream = state["streams"][0]
                target = {"kind": "stream", "target": stream["id"], "serial": stream["serial"]}
                audio.apply({**target, "action": "volume", "value": 35}, state, run)
                state = wait_for(lambda state: state["streams"][0]["volume"] == 35)
                audio.apply({**target, "action": "mute", "value": True}, state, run)
                state = wait_for(lambda state: state["streams"][0]["muted"])
                audio.apply({"kind": "output", "action": "volume", "target": "bench_b", "value": 65}, state, run)
                state = wait_for(lambda state: next(item for item in state["outputs"] if item["name"] == "bench_b")["volume"] == 65)
                audio.apply({"kind": "output", "action": "default", "target": "bench_b"}, state, run)
                state = wait_for(lambda state: state["default_output"] == "bench_b")
                output = next(item for item in state["outputs"] if item["name"] == "bench_b")
                self.assertEqual(state["streams"][0]["output"], output["id"], "existing playback moves to selected output")
                for suffix in ("a", "b"):
                    run("load-module", "module-remap-source", "master=bench_" + suffix + ".monitor", "source_name=mic_" + suffix)
                state = wait_for(lambda state: len(state["inputs"]) == 2)
                audio.apply({"kind": "input", "action": "default", "target": "mic_a"}, state, run)
                audio.apply({"kind": "input", "action": "volume", "target": "mic_b", "value": 80}, state, run)
                state = wait_for(lambda state: next(item for item in state["inputs"] if item["name"] == "mic_b")["volume"] == 80)
                audio.apply({"kind": "input", "action": "mute", "target": "mic_b", "value": True}, state, run)
                state = wait_for(lambda state: next(item for item in state["inputs"] if item["name"] == "mic_b")["muted"])
                processes.append(subprocess.Popen([pacat, "--record", "--raw", "--device=mic_a"],
                                                   env=env, stdout=subprocess.DEVNULL, stderr=log))
                mic_a = next(item for item in state["inputs"] if item["name"] == "mic_a")
                state = wait_for(lambda state: any(item["source"] == mic_a["id"] for item in state["recordings"]))
                recording = next(item for item in state["recordings"] if item["source"] == mic_a["id"])
                audio.apply({"kind": "input", "action": "default", "target": "mic_b"}, state, run)
                mic_b = next(item for item in state["inputs"] if item["name"] == "mic_b")
                wait_for(lambda state: any(item["id"] == recording["id"] and item["source"] == mic_b["id"] for item in state["recordings"]))
                run("unload-module", module_a)
                state = wait_for(lambda state: len(state["outputs"]) == 1)
                with self.assertRaises(audio.AudioError):
                    audio.apply({"kind": "output", "action": "volume", "target": "bench_a", "value": 50}, state, run)
                processes[-2].terminate()
                processes[-2].wait(timeout=5)
                wait_for(lambda state: not state["streams"])
            except Exception:
                for log in logs:
                    log.flush()
                    log.seek(0)
                    print(log.read())
                raise
            finally:
                for process in reversed(processes):
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
                for log in logs:
                    log.close()
