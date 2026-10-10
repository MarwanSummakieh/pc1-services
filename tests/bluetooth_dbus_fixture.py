"""A private, hardware-free BlueZ D-Bus service for the integration regression."""
import os
from pathlib import Path
import sys
import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

DBusGMainLoop(set_as_default=True)
bus = dbus.SystemBus()
name = dbus.service.BusName("org.bluez", bus)
ADAPTER = "org.bluez.Adapter1"
DEVICE = "org.bluez.Device1"
PROPERTIES = "org.freedesktop.DBus.Properties"
A = "/org/bluez/hci0"
D = A + "/dev_AA_BB_CC_DD_EE_FF"
data = {A: {ADAPTER: {"Alias": dbus.String("Test dongle"), "Powered": dbus.Boolean(True), "Discovering": dbus.Boolean(False)}},
        D: {DEVICE: {"Adapter": dbus.ObjectPath(A), "Alias": dbus.String("Test controller"), "Paired": dbus.Boolean(False), "Trusted": dbus.Boolean(False), "Connected": dbus.Boolean(False)}}}
agent_name, agent_path = "", ""


class Objects(dbus.service.Object):
    @dbus.service.method("org.freedesktop.DBus.ObjectManager", in_signature="", out_signature="a{oa{sa{sv}}}")
    def GetManagedObjects(self):
        return data


class AgentManager(dbus.service.Object):
    @dbus.service.method("org.bluez.AgentManager1", in_signature="os", out_signature="", sender_keyword="sender")
    def RegisterAgent(self, path, capability, sender=None):
        global agent_name, agent_path
        assert capability == "KeyboardDisplay"
        agent_name, agent_path = sender, str(path)


class Adapter(dbus.service.Object):
    @dbus.service.method(PROPERTIES, in_signature="ssv", out_signature="")
    def Set(self, interface, key, value):
        data[A][interface][key] = value

    @dbus.service.method(ADAPTER, in_signature="", out_signature="")
    def StartDiscovery(self):
        data[A][ADAPTER]["Discovering"] = dbus.Boolean(True)

    @dbus.service.method(ADAPTER, in_signature="", out_signature="")
    def StopDiscovery(self):
        data[A][ADAPTER]["Discovering"] = dbus.Boolean(False)

    @dbus.service.method(ADAPTER, in_signature="o", out_signature="")
    def RemoveDevice(self, path):
        data.pop(str(path), None)


class Device(dbus.service.Object):
    @dbus.service.method(PROPERTIES, in_signature="ssv", out_signature="")
    def Set(self, interface, key, value):
        data[D][interface][key] = value

    @dbus.service.method(DEVICE, in_signature="", out_signature="", async_callbacks=("success", "failure"))
    def Pair(self, success, failure):
        def approved():
            data[D][DEVICE]["Paired"] = dbus.Boolean(True)
            success()
        agent = dbus.Interface(bus.get_object(agent_name, agent_path), "org.bluez.Agent1")
        agent.RequestConfirmation(dbus.ObjectPath(D), dbus.UInt32(123456), reply_handler=approved, error_handler=failure)

    @dbus.service.method(DEVICE, in_signature="", out_signature="")
    def CancelPairing(self):
        return

    @dbus.service.method(DEVICE, in_signature="", out_signature="")
    def Connect(self):
        data[D][DEVICE]["Connected"] = dbus.Boolean(True)

    @dbus.service.method(DEVICE, in_signature="", out_signature="")
    def Disconnect(self):
        data[D][DEVICE]["Connected"] = dbus.Boolean(False)


objects = [Objects(bus, "/"), AgentManager(bus, "/org/bluez"), Adapter(bus, A), Device(bus, D)]
Path(sys.argv[1]).write_text("ready")
GLib.MainLoop().run()
