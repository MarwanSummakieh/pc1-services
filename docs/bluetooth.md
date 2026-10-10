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

Before a dongle is attached, BlueZ may skip its hardware-conditioned system
service. Missing-service/owner replies become the clean `no-adapter` state only
when no kernel HCI device exists. Agent registration waits for an adapter, and
normal pairing resumes after hotplug. Access failures and missing BlueZ with a
real adapter remain visible errors.

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

## PC1 adapter compatibility

On 8 October 2026, the owner confirmed two wireless DualSense controllers and
vibration with a UGREEN BT6.0 adapter (`33fa:0012`). BlueZ 5.87 then destroyed both
UHID input devices after transient output-send errors while continuing to report
the controllers connected. Reconnecting restored both players in Tekken.

PC1 now has a persistent local `UserspaceHID=false` setting in
`/etc/bluetooth/input.conf` to use kernel HIDP, whose send path retries `EAGAIN`.
Both controller slots reattached under HIDP. The owner confirmed controls and
PS/Home still work after the transport change; rumble retesting and sustained
stability remain pending. The owner reported a four-light flicker while input
continued working, then confirmed that restarting PC1 cleared it. Live inspection
verified that the kernel HID setting survived the restart. The incident record
tracks these physical reports separately from long-duration stability.
This setting is not a general image default. See the
[incident record](bluetooth-controller-recovery-20261008.md) for evidence,
configuration backup and rollback instructions.

On 10 October 2026, wake inspection found that this adapter exposes no USB
Remote Wakeup flag or `power/wakeup` attribute, despite the connected DualSense
already having `WakeAllowed: yes`. Controller wake from deep sleep cannot be
enabled through the current adapter's available settings. See the
[wake capability record](controller-wake-20261010.md).
The owner selected display-only Rest mode as the workaround and confirmed
screen-off and wireless PS wake on PC1. Rest keeps the PC and Bluetooth running;
Sleep has been removed from the power menu.

Controller battery tracking is installed on the bench and included in source.
The status corner shows available percentages; Bluetooth shows charging, USB
connection, and dated last readings after disconnect. Low-battery warnings and
persistent battery/connection history are available. Player 1 reported 5% and
charging during verification. See the
[battery tracking record](controller-battery-20261010.md).

API references: [BlueZ Agent example](https://github.com/bluez/bluez/blob/master/test/simple-agent),
[BlueZ bus policy](https://github.com/bluez/bluez/blob/master/src/bluetooth.conf),
[Device API](https://bluez.readthedocs.io/en/latest/device-api/),
[Adapter API](https://bluez.readthedocs.io/en/latest/adapter-api/).
