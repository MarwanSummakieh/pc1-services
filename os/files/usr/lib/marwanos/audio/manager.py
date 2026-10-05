#!/usr/bin/env python3
"""Session-owned audio controls. Requests are data; pactl always receives argv."""

import json
import math
import os
from pathlib import Path
import subprocess
import time


class AudioError(Exception):
    pass


def pactl(*args):
    try:
        result = subprocess.run(
            ["/usr/bin/pactl", *args], capture_output=True, text=True,
            timeout=3, env={**os.environ, "LC_ALL": "C"}, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AudioError("Audio service unavailable. Try again.") from exc
    if result.returncode:
        raise AudioError("Audio change failed. The device or app may have disconnected.")
    return result.stdout


def volume(item):
    channels = item.get("volume", {}).values()
    values = [float(channel["value"]) / 65536 * 100 for channel in channels]
    return round(max(values, default=0))


def device(item):
    ports = item.get("ports", [])
    if isinstance(ports, dict):
        ports = [{"name": key, **value} for key, value in ports.items()]
    return {
        "id": str(item["index"]), "name": item["name"],
        "label": item.get("description") or item["name"],
        "volume": volume(item), "muted": bool(item.get("mute")),
        "port": item.get("active_port") or "",
        "ports": [{"name": port["name"], "label": port.get("description") or port["name"]}
                  for port in ports if port.get("availability") not in ("not available", "no")],
    }


def snapshot(run=pactl):
    listing = json.loads(run("--format=json", "list"))
    info = json.loads(run("--format=json", "info"))
    outputs = [device(item) for item in listing.get("sinks", [])]
    # Loopback monitors capture speaker audio, not a microphone.
    inputs = [device(item) for item in listing.get("sources", [])
              if item.get("monitor_of_sink") in (None, 4294967295, "4294967295")
              and not item["name"].endswith(".monitor")]
    streams = []
    for item in listing.get("sink_inputs", []):
        props = item.get("properties", {})
        streams.append({
            "id": str(item["index"]), "serial": str(props.get("object.serial", "")),
            "label": props.get("application.name") or props.get("media.name") or "Application",
            "volume": volume(item), "muted": bool(item.get("mute")),
            "output": str(item["sink"]),
            "adjustable": item.get("has_volume", True) and item.get("volume_writable", True),
        })
    return {
        "available": True, "outputs": outputs, "inputs": inputs, "streams": streams,
        "default_output": info.get("default_sink_name", ""),
        "default_input": info.get("default_source_name", ""),
        "recordings": [{"id": str(item["index"]), "source": str(item["source"])}
                       for item in listing.get("source_outputs", [])],
    }


def apply(request, state, run=pactl):
    if not isinstance(request, dict):
        raise AudioError("Invalid audio request.")
    if request.get("action") == "refresh":
        return
    if not state.get("available"):
        raise AudioError("Audio service unavailable. Try again.")
    kind, action = request.get("kind"), request.get("action")
    groups = {"output": ("outputs", "sink"), "input": ("inputs", "source"),
              "stream": ("streams", "sink-input")}
    if kind not in groups or action not in ("volume", "mute", "default", "port"):
        raise AudioError("Unsupported audio request.")
    group, noun = groups[kind]
    target = next((item for item in state[group]
                   if item["name" if kind != "stream" else "id"] == request.get("target")), None)
    if target is None or (kind == "stream" and target["serial"] != request.get("serial", "")):
        raise AudioError("The device or app disconnected. Choose another one.")
    identity = target["name" if kind != "stream" else "id"]
    value = request.get("value")
    if action == "volume":
        if (type(value) not in (int, float) or not math.isfinite(value)
                or not 0 <= value <= 100):
            raise AudioError("Volume must be between 0 and 100%.")
        if kind == "stream" and not target["adjustable"]:
            raise AudioError("This app controls its own volume.")
        run("set-" + noun + "-volume", identity, str(round(value)) + "%")
    elif action == "mute":
        if type(value) is not bool:
            raise AudioError("Invalid mute request.")
        run("set-" + noun + "-mute", identity, "1" if value else "0")
    elif action == "port":
        if kind == "stream" or value not in [port["name"] for port in target["ports"]]:
            raise AudioError("Audio port unavailable.")
        run("set-" + noun + "-port", identity, value)
    else:
        if kind == "stream":
            raise AudioError("Choose an output or microphone.")
        run("set-default-" + noun, identity)
        # Default selection alone does not reroute existing streams.
        streams = state["streams"] if kind == "output" else state["recordings"]
        physical_inputs = {item["id"] for item in state["inputs"]}
        failed = False
        for stream in streams:
            if kind == "input" and stream["source"] not in physical_inputs:
                continue
            try:
                run("move-sink-input" if kind == "output" else "move-source-output",
                    stream["id"], identity)
            except AudioError:
                failed = True
        if failed:
            raise AudioError("Device selected; an app could not switch. Restart that app to use it.")


def publish(path, state):
    payload = {**state, "updated_at": time.time()}
    raw = json.dumps(payload, ensure_ascii=False)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(raw, encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


class Manager:
    def __init__(self, folder, run=pactl):
        self.folder = Path(folder)
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.run = run
        self.reply = {"request_id": "", "error": ""}

    def tick(self):
        request_path = self.folder / "request.json"
        request = None
        malformed = False
        status_error = ""
        if request_path.exists():
            try:
                if request_path.stat().st_size > 8192:
                    raise ValueError("request too large")
                request = json.loads(request_path.read_text(encoding="utf-8"))
                if not isinstance(request, dict):
                    raise ValueError("request must be an object")
            except (ValueError, OSError):
                request = None
                malformed = True
                self.reply = {"request_id": "", "error": "Invalid audio request. Try again."}
            finally:
                request_path.unlink(missing_ok=True)
        try:
            state = snapshot(self.run)
            if request is not None:
                self.reply = {"request_id": str(request.get("id", "")), "error": ""}
                try:
                    apply(request, state, self.run)
                except AudioError as exc:
                    self.reply["error"] = str(exc)
                state = snapshot(self.run)
        except (AudioError, ValueError, KeyError, TypeError) as exc:
            state = {"available": False, "outputs": [], "inputs": [], "streams": [],
                     "default_output": "", "default_input": ""}
            status_error = str(exc) if isinstance(exc, AudioError) else "Could not read audio devices. Try again."
            if request is not None:
                self.reply["request_id"] = str(request.get("id", ""))
                self.reply["error"] = status_error
        else:
            if request is None and not self.reply["request_id"] and not malformed:
                self.reply["error"] = ""
        publish(self.folder / "state.json", {**state, **self.reply, "error": status_error or self.reply["error"]})

    def serve(self):
        next_refresh = 0
        while True:
            if (self.folder / "request.json").exists() or time.monotonic() >= next_refresh:
                self.tick()
                next_refresh = time.monotonic() + 1
            time.sleep(0.1)


if __name__ == "__main__":
    runtime = os.environ.get("XDG_RUNTIME_DIR", "/run/user/" + str(os.getuid()))
    Manager(Path(runtime) / "marwanos" / "audio").serve()
