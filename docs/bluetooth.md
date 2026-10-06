# Bluetooth controllers

Bluetooth settings provide controller-operated discovery, pairing, connection,
disconnection, trust and removal. The no-adapter state explicitly asks for the
USB dongle. Device selection remains stable across service updates. A pairs or
reconnects a selected controller; X toggles trust, Y offers a separate removal
confirmation, and B cancels pairing before returning. Turn Bluetooth on/off and
Find controllers are visible rows. Scanning stops after one minute or when the
page closes.

The pairing agent uses BlueZ `KeyboardDisplay`: yes/no numeric comparison,
displayed passkeys, and a controller-only numeric editor cover common Bluetooth
controller authentication. Left/right selects a code digit, up/down changes it,
and A confirms. Legacy numeric PINs support 1–16 digits with L1/R1 adjusting the
length. Alphanumeric legacy PIN entry is not supported. Every prompt carries a
random token and expires after a minute; stale answers and unsolicited device
authorization requests are rejected. No physical keyboard is needed.

The player-owned `marwanos-bluetooth.service` uses `python3-dbus`,
`python3-gobject`, the BlueZ system D-Bus service and the installed BlueZ bus
policy. It does not run a root command or request a general privileged shell.
It registers an application pairing agent, rather than becoming the machine's
default pairing agent. A new or hotplugged adapter is powered on once; a user
power-off selection is respected while the adapter remains attached.

Requests and status are private atomic JSON files under
`$XDG_RUNTIME_DIR/marwanos/bluetooth`. Targets must appear in BlueZ's current
object graph, and request files cannot be symlinks. Device pairing information
and trust are persisted by BlueZ under its own system data directory. Successful
pairing trusts and connects the selected device. Trusted, paired controllers
are reconnected with at most one attempt per thirty seconds while the adapter
is powered. A deliberate Disconnect suppresses this retry until Connect or a
new service session. Removing a device removes its BlueZ bond.

Integration requires the `bluez` package, enabled system `bluetooth.service`,
enabled user `marwanos-bluetooth.service`, a `Bluetooth` shell autoload and a
Settings child page using `bluetooth_page.gd` (or a surface calling
`Bluetooth.open()`). Tests cover the simulated BlueZ object graph, controller
prompt flow and a real private D-Bus roundtrip through the agent with a fake
BlueZ service. Physical acceptance remains pending:
plug in the intended dongle, pair the actual controller, confirm player routing
and rumble, restart and suspend/resume, and verify reconnect after hotplug.

API references: [BlueZ Agent example](https://github.com/bluez/bluez/blob/master/test/simple-agent),
[BlueZ bus policy](https://github.com/bluez/bluez/blob/master/src/bluetooth.conf),
[Device API](https://bluez.readthedocs.io/en/latest/device-api/),
[Adapter API](https://bluez.readthedocs.io/en/latest/adapter-api/).
