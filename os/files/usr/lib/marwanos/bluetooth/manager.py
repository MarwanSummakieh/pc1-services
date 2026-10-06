#!/usr/bin/env python3
"""Player-owned BlueZ pairing agent and narrow atomic request/state seam."""
import json
import os
from pathlib import Path
import secrets
import time

ADAPTER = "org.bluez.Adapter1"
DEVICE = "org.bluez.Device1"
AGENT = "org.bluez.Agent1"
PROPERTIES = "org.freedesktop.DBus.Properties"


class BluetoothError(Exception):
    pass


def managed_objects(query, hci_root=Path("/sys/class/bluetooth")):
    """A hardware-conditioned BlueZ service may correctly be absent before hotplug."""
    try:
        return query()
    except Exception as exc:
        error_name = getattr(exc, "get_dbus_name", lambda: "")()
        absent = error_name in ("org.freedesktop.DBus.Error.ServiceUnknown",
                                "org.freedesktop.DBus.Error.NameHasNoOwner")
        if absent and not any(Path(hci_root).glob("hci*")):
            return {}
        raise


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def snapshot(objects):
    adapters, devices = [], []
    for path, interfaces in objects.items():
        if ADAPTER in interfaces:
            props = interfaces[ADAPTER]
            adapters.append({"id": str(path), "label": str(props.get("Alias", "Bluetooth")),
                             "powered": bool(props.get("Powered")), "discovering": bool(props.get("Discovering"))})
    known = {item["id"] for item in adapters}
    for path, interfaces in objects.items():
        props = interfaces.get(DEVICE)
        if props and str(props.get("Adapter", "")) in known:
            devices.append({"id": str(path), "adapter": str(props["Adapter"]),
                            "label": str(props.get("Alias") or props.get("Name") or props.get("Address", "Device")),
                            "address": str(props.get("Address", "")), "paired": bool(props.get("Paired")),
                            "trusted": bool(props.get("Trusted")), "connected": bool(props.get("Connected")),
                            "icon": str(props.get("Icon", ""))})
    devices.sort(key=lambda item: (not item["connected"], not item["paired"], item["label"].casefold(), item["id"]))
    return {"available": True, "status": "ready" if adapters else "no-adapter", "adapters": adapters, "devices": devices}


class Manager:
    def __init__(self, backend, folder, clock=time.monotonic):
        self.backend, self.folder, self.clock = backend, Path(folder), clock
        self.state = {"available": False, "status": "unavailable", "adapters": [], "devices": []}
        self.error, self.busy, self.request_id, self.pairing = "", "", "", ""
        self.prompt, self.reply = {}, None
        self.deadline, self.scan_deadline = 0, 0
        self.reconnect_after, self.inhibited, self.seen_adapters = {}, set(), set()
        self.generation = 0

    def call(self, target, interface, method, args=(), success=None):
        generation = self.generation
        def done(*values):
            if generation == self.generation:
                (success or self.complete)(*values)
        def failed(error):
            if generation == self.generation:
                self.failed(error)
        self.backend.call(target, interface, method, args, done, failed)

    def refresh(self):
        was_unavailable = self.state["status"] == "unavailable"
        self.state = snapshot(self.backend.objects())
        if was_unavailable:
            self.error = ""

    def publish(self):
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_json(self.folder / "state.json", {**self.state, "updated_at": time.time(),
                    "error": self.error, "busy": self.busy, "request_id": self.request_id, "prompt": self.prompt})

    def failed(self, error):
        self.error = "Bluetooth change failed: " + str(error)[:240]
        self.busy = ""
        self.pairing = ""
        self.clear_prompt(False)

    def complete(self, *_args):
        self.busy = ""
        self.error = ""
        self.pairing = ""
        self.clear_prompt(False)

    def clear_prompt(self, reject=True):
        reply, self.reply = self.reply, None
        self.prompt = {}
        if reject and reply:
            reply(False, None)

    def ask(self, device, kind, value, reply=None):
        if device != self.pairing:
            if reply:
                reply(False, None)
            return
        self.clear_prompt()
        self.reply = reply
        self.prompt = {"token": secrets.token_hex(16), "device": str(device), "kind": kind, "value": str(value)}
        self.deadline = self.clock() + 60
        self.publish()

    def respond(self, request):
        if not self.prompt or request.get("token") != self.prompt["token"]:
            raise BluetoothError("That pairing prompt expired.")
        approved = request.get("approved") is True
        value = request.get("value", "")
        kind = self.prompt["kind"]
        if approved and kind in ("pin", "passkey"):
            if not isinstance(value, str) or not value.isascii() or not value.isdigit() or not (1 <= len(value) <= (6 if kind == "passkey" else 16)):
                raise BluetoothError("Enter the numeric pairing code shown by the device.")
            if kind == "passkey":
                value = int(value)
        reply, self.reply = self.reply, None
        self.prompt = {}
        if reply:
            reply(approved, value)
        if not approved:
            self.cancel()

    def cancel(self):
        self.generation += 1
        target = self.pairing
        self.clear_prompt()
        if target:
            self.backend.call(target, DEVICE, "CancelPairing", (), lambda: None, lambda error: None)
        self.busy, self.pairing = "", ""

    def apply(self, request):
        if not isinstance(request, dict):
            raise BluetoothError("Invalid Bluetooth request.")
        action = request.get("action")
        if action == "close":
            self.cancel()
            self.refresh()
            for item in self.state["adapters"]:
                if item["discovering"]:
                    self.backend.call(item["id"], ADAPTER, "StopDiscovery", (), lambda: None, lambda error: None)
            return
        if action == "respond":
            self.respond(request)
            return
        if action == "cancel":
            self.cancel()
            return
        if self.busy:
            raise BluetoothError("Finish or cancel the current Bluetooth change first.")
        self.refresh()
        self.request_id = str(request.get("id", ""))[:128]
        self.generation += 1
        self.error = ""
        if action == "refresh":
            return
        target = request.get("target")
        adapters = {item["id"]: item for item in self.state["adapters"]}
        devices = {item["id"]: item for item in self.state["devices"]}
        if action in ("scan", "stop-scan", "power"):
            if target not in adapters:
                raise BluetoothError("No Bluetooth adapter found. Plug in your dongle.")
            if action == "power":
                self.backend.set(target, ADAPTER, "Powered", request.get("value") is True)
                if request.get("value") is not True:
                    self.seen_adapters.add(target)
            else:
                self.busy = action
                self.deadline = self.clock() + 15
                if action == "scan":
                    self.backend.set(target, ADAPTER, "Powered", True)
                    self.backend.set(target, ADAPTER, "Pairable", True)
                    self.scan_deadline = self.clock() + 60
                self.call(target, ADAPTER, "StartDiscovery" if action == "scan" else "StopDiscovery")
            return
        if action not in ("pair", "connect", "disconnect", "remove", "trust") or target not in devices:
            raise BluetoothError("That Bluetooth device is no longer available.")
        item = devices[target]
        if action == "trust":
            self.backend.set(target, DEVICE, "Trusted", request.get("value") is True)
            return
        if action == "remove":
            self.inhibited.add(target)
            self.busy = action
            self.deadline = self.clock() + 15
            self.call(item["adapter"], ADAPTER, "RemoveDevice", (target,))
            return
        if action == "disconnect":
            self.inhibited.add(target)
        else:
            self.inhibited.discard(target)
        self.busy = action
        self.deadline = self.clock() + 90
        if action == "pair" and not item["paired"]:
            self.pairing = target
            self.call(target, DEVICE, "Pair", success=lambda: self.paired(target))
        else:
            self.call(target, DEVICE, "Disconnect" if action == "disconnect" else "Connect")

    def paired(self, target):
        try:
            self.backend.set(target, DEVICE, "Trusted", True)
            self.call(target, DEVICE, "Connect")
        except Exception as error:
            self.failed(error)

    def tick(self):
        try:
            self.refresh()
            now = self.clock()
            if (self.busy or self.prompt) and now > self.deadline:
                self.cancel()
                self.error = "Bluetooth pairing timed out. Put the device in pairing mode and try again."
            for adapter in self.state["adapters"]:
                if adapter["id"] not in self.seen_adapters:
                    self.backend.set(adapter["id"], ADAPTER, "Powered", True)
                    self.seen_adapters.add(adapter["id"])
                if adapter["discovering"] and self.scan_deadline and now > self.scan_deadline:
                    self.backend.call(adapter["id"], ADAPTER, "StopDiscovery", (), lambda: None, lambda error: None)
            if not self.busy:
                for device in self.state["devices"]:
                    target = device["id"]
                    powered = any(item["id"] == device["adapter"] and item["powered"] for item in self.state["adapters"])
                    if powered and device["paired"] and device["trusted"] and not device["connected"] and target not in self.inhibited and now >= self.reconnect_after.get(target, 0):
                        self.reconnect_after[target] = now + 30
                        self.backend.call(target, DEVICE, "Connect", (), lambda: None, lambda error: None)
            self.seen_adapters.intersection_update(item["id"] for item in self.state["adapters"])
        except Exception as error:
            self.state = {"available": False, "status": "unavailable", "adapters": [], "devices": []}
            self.failed(error)
        self.consume()
        self.publish()
        return True

    def consume(self):
        path = self.folder / "request.json"
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return
        except OSError:
            path.unlink(missing_ok=True)
            self.error = "Invalid Bluetooth request file."
            return
        try:
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                text = stream.read(65537)
            path.unlink(missing_ok=True)
            if len(text) > 65536:
                raise BluetoothError("Bluetooth request too large.")
            self.apply(json.loads(text))
        except (ValueError, OSError, BluetoothError) as error:
            self.error = str(error)[:240]
        except Exception as error:
            self.failed(error)


def run():
    import dbus
    import dbus.service
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib
    DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()

    class Rejected(dbus.DBusException):
        _dbus_error_name = "org.bluez.Error.Rejected"

    class Backend:
        registered = False

        def interface(self, path, interface):
            return dbus.Interface(bus.get_object("org.bluez", path), interface)

        def objects(self):
            result = managed_objects(lambda: self.interface("/", "org.freedesktop.DBus.ObjectManager").GetManagedObjects(timeout=3))
            if not self.registered and any(ADAPTER in interfaces for interfaces in result.values()):
                self.interface("/org/bluez", "org.bluez.AgentManager1").RegisterAgent("/org/marwanos/bluetooth_agent", "KeyboardDisplay", timeout=3)
                self.registered = True
            return result

        def set(self, path, interface, name, value):
            self.interface(path, PROPERTIES).Set(interface, name, dbus.Boolean(value), timeout=3)

        def call(self, path, interface, method, args, success, failure):
            if method == "RemoveDevice":
                args = (dbus.ObjectPath(args[0]),)
            getattr(self.interface(path, interface), method)(*args, reply_handler=success, error_handler=failure, timeout=90)

    backend = Backend()
    folder = Path(os.environ["XDG_RUNTIME_DIR"]) / "marwanos/bluetooth"
    manager = Manager(backend, folder)

    class Agent(dbus.service.Object):
        def prompt(self, device, kind, value, success, failure):
            def answer(approved, code):
                if not approved:
                    failure(Rejected("Pairing rejected by player"))
                elif kind == "passkey":
                    success(dbus.UInt32(code))
                elif kind == "pin":
                    success(str(code))
                else:
                    success()
            manager.ask(str(device), kind, value, answer)

        @dbus.service.method(AGENT, in_signature="", out_signature="")
        def Release(self):
            backend.registered = False
            manager.cancel()

        @dbus.service.method(AGENT, in_signature="", out_signature="")
        def Cancel(self):
            manager.clear_prompt()

        @dbus.service.method(AGENT, in_signature="ou", out_signature="", async_callbacks=("success", "failure"))
        def RequestConfirmation(self, device, passkey, success, failure):
            self.prompt(device, "confirm", "%06d" % passkey, success, failure)

        @dbus.service.method(AGENT, in_signature="o", out_signature="", async_callbacks=("success", "failure"))
        def RequestAuthorization(self, device, success, failure):
            self.prompt(device, "authorize", "", success, failure)

        @dbus.service.method(AGENT, in_signature="o", out_signature="u", async_callbacks=("success", "failure"))
        def RequestPasskey(self, device, success, failure):
            self.prompt(device, "passkey", "", success, failure)

        @dbus.service.method(AGENT, in_signature="o", out_signature="s", async_callbacks=("success", "failure"))
        def RequestPinCode(self, device, success, failure):
            self.prompt(device, "pin", "", success, failure)

        @dbus.service.method(AGENT, in_signature="ouq", out_signature="")
        def DisplayPasskey(self, device, passkey, entered):
            manager.ask(str(device), "display", "%06d (%d entered)" % (passkey, entered))

        @dbus.service.method(AGENT, in_signature="os", out_signature="")
        def DisplayPinCode(self, device, pincode):
            manager.ask(str(device), "display", str(pincode))

        @dbus.service.method(AGENT, in_signature="os", out_signature="")
        def AuthorizeService(self, device, uuid):
            manager.refresh()
            if not any(item["id"] == str(device) and item["trusted"] for item in manager.state["devices"]):
                raise Rejected("Device is not trusted")

    agent = Agent(bus, "/org/marwanos/bluetooth_agent")
    bus.add_signal_receiver(lambda *_: setattr(backend, "registered", False), signal_name="NameOwnerChanged", dbus_interface="org.freedesktop.DBus", arg0="org.bluez")
    manager.tick()
    GLib.timeout_add(500, manager.tick)
    GLib.MainLoop().run()


if __name__ == "__main__":
    run()
