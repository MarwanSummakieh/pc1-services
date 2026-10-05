# Audio manager

Open **Settings → Audio**, or press **Home → Audio** while an application is
running. Audio in the Home menu leaves the application running and retains
the shell's controller ownership until you return to it.

| Control | Controller action |
| --- | --- |
| Output / microphone device | Cross cycles through connected devices |
| Port | Cross cycles available ports, including HDMI connections |
| Volume | Left / right changes volume by 5%; cross toggles mute |
| Application | Left / right changes that playback stream's volume; cross toggles mute |
| Refresh | Requests a fresh device snapshot and retries the connection |
| Back | Circle returns to Settings or the Home menu |

All volume controls are limited to 0–100%. The application mixer lists current
playback streams; an app with several streams can appear more than once. Output
selection changes the default and moves existing playback to it. Microphone
selection moves existing microphone recordings while preserving speaker-loopback
recordings. Disconnected devices and exited streams disappear from the list;
focus recovers to an available row. Microphone mute controls the selected device.

## Runtime

The image installs PipeWire, pipewire-pulse, WirePlumber, ALSA compatibility,
and `pactl`. Image-owned user-service links start the stack and
`marwanos-audio.service`. WirePlumber manages device policy and its normal saved
defaults, routes, and volumes in the player's persistent home.

`os/files/usr/lib/marwanos/audio/manager.py` runs as the session user. It publishes
`$XDG_RUNTIME_DIR/marwanos/audio/state.json` atomically once a second and consumes
`request.json`, which the shell writes through a temporary file and rename.
The files are private to the session user. Requests include a unique ID; state
acknowledges the request with refreshed device state or an error. The shell polls
every 250 ms, allows one outstanding request, and reports an acknowledgement
timeout or stale service state after 12 seconds. It never calls system commands
on the render thread. Stream requests include PipeWire's object serial to reject
a recycled stream index. The helper validates devices, ports, actions, and volume
values before invoking `pactl` with an argument array.

For shell fixtures, `MARWANOS_SHELL_STATUS_DIR` redirects the audio folder to
`<fixture>/audio`. Fixture state needs an `updated_at` Unix timestamp.

## Verification

```bash
python3 -m unittest discover -s tests -p 'test_audio*.py' -v
GODOT_BIN=/path/to/Godot-4.7.1 bash scripts/check-audio-shell.sh

# Requires pipewire, pipewire-pulse, wireplumber, pactl, and pacat.
# Creates a private audio server with virtual devices and policy-only WirePlumber.
MARWANOS_AUDIO_INTEGRATION=1 python3 -m unittest discover -s tests -p test_audio_integration.py -v
```

The service tests cover validation, acknowledgements, partial routing failures,
timeouts, and reconnects. The controller suite covers both entry points, output
and port selection, microphone mute, application volume, stale state, and focus
recovery. The isolated integration test exercises real playback and recording,
volume/mute, default selection, live routing, and device removal.

Physical HDMI, USB and controller-headset playback and recording still need
acceptance on the target PC. Bluetooth devices already connected to the session
can appear as outputs; pairing is outside this screen.

The runtime follows the official [PipeWire PulseAudio compatibility contract](https://docs.pipewire.org/page_man_pipewire-pulse_1.html)
and [pactl command reference](https://manpages.debian.org/trixie/pulseaudio-utils/pactl.1.en.html).
