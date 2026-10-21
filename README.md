# AwoX Connect.Z for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)
[![Home Assistant 2026.10+](https://img.shields.io/badge/Home%20Assistant-2026.10%2B-18BCF2.svg)](https://www.home-assistant.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Unofficial integration for **EGLO / AwoX Connect.Z RGB/TW lights**, controlled locally
over Home Assistant Bluetooth adapters and **ESPHome Bluetooth Proxies**.
Import lamp data from HomeControl or set up a mesh locally using known credentials.
Normal light control does not use the cloud or require modified lamp firmware.

> **Experimental / community-supported:** compatibility beyond the tested hardware
> still needs confirmation.

## AI-assisted development

This project was developed with substantial assistance from **OpenAI ChatGPT**,
including protocol analysis, code generation, debugging and documentation.
Development included real BLE captures and physical lamp testing; further human
review and device compatibility reports are welcome.

## Features and tested hardware

- Power, brightness, hue/saturation color, tunable white, transitions and the native **Color cycle** / **Candle** effects.
- HomeControl import, fully local setup and Reconfigure for lamp/mesh management.
- Live state correction from BLE advertisements and configurable availability.
- On-demand BLE connections, idle disconnect, configurable command concurrency and same-lamp latest-wins command coalescing.
- Bluetooth discovery of additional provisioned lamps plus per-lamp diagnostic sensors and credential-redacted diagnostics.

| Device | Cloud model | Firmware | Hardware | Tested functions |
|---|---|---|---|---|
| EGLO `900024/12253` | `EGLO-ZM-RGB-TW` | `3.0.2` | `4.62` | Power, brightness, HS color, color temperature, Color cycle, Candle |

## Installation

Requires **Home Assistant 2026.10+**, its Bluetooth integration, and a connectable
adapter or ESPHome Bluetooth Proxy within connection range of the lamps.
Control requires a valid state advertisement before the first command after setup
or reload. The Bluetooth source must supply raw advertisement packets. Requests
are rejected while the lamp state required for command planning is unknown. If
that state becomes unknown during a request, pending work is aborted with an error
instead of waiting for a later advertisement.

**HACS:** Add `https://github.com/jakob8st8rm4nn/home-assistant-awox-connect-z` under
**Custom repositories**, category **Integration**. Install **AwoX Connect.Z** and
restart Home Assistant.

**Manual:** Copy `custom_components/awox_connect_z` to
`/config/custom_components/awox_connect_z` and restart Home Assistant.

**Updating:** Update and restart Home Assistant. Existing entries and saved
options are retained; no reconfiguration is required. See [CHANGELOG.md](CHANGELOG.md).

## Setup and lamp management

Open **Settings → Devices & services → Add integration → AwoX Connect.Z**.

### HomeControl import

Choose **Via AwoX / EGLO HomeControl** and enter your account email and password.
Compatible lamps, individual mesh addresses and local mesh credentials are imported.
The account password is not stored; the local mesh credential is retained for BLE
authentication. Internet access is needed for cloud import and cloud Reconfigure.

### Fully local setup

Choose **Local mesh credentials**. You must already know the mesh name and password;
these are not the HomeControl account login. Each must be non-empty and fit within
16 UTF-8 bytes. The integration cannot extract these credentials or provision a mesh.

1. Run the 30-second search and select the discovered lamps.
2. Use **Search again** if needed, or enter a lamp's Bluetooth MAC address and
   individual 16-bit mesh ID manually (decimal or hexadecimal, e.g. `0xD361`).
3. Selected lamps are authenticated locally before saving, without a light command.

Only fresh, complete advertisements with usable mesh IDs appear in search results.
If a reported mesh ID conflicts with a stored/manual ID, correct it explicitly.
Search and authentication errors can be corrected and retried in the setup dialog.
All selected lamps share one mesh entry. Local mesh credentials are stored for
normal control; no cloud login is used.

### Add lamps or refresh credentials

Open the integration entry menu and choose **Reconfigure**:

- **Cloud entry:** sign in to the same HomeControl account again to refresh lamps,
  mesh credentials and metadata. The account password/session token is not retained.
- **Local entry:** verify mesh credentials, search for more lamps or add them manually.
  Existing lamps remain in the entry.

Home Assistant may also discover newly provisioned lamps through Bluetooth. Confirm
the discovery to verify them against an existing mesh and add them. Factory-reset
lamps or lamps with different mesh credentials cannot join through that flow.

## Per-lamp diagnostics

| Sensor | Meaning |
|---|---|
| Signal Strength | Last-known advertisement RSSI while connected or during post-disconnect grace; otherwise requires a recent packet. |
| Last Seen | Time of the last received Bluetooth advertisement. |
| Bluetooth Status | Connected with active BLE; visible with a recent advertisement or during idle-disconnect grace; unreachable after expiry. Initially unknown without current evidence. |
| Mesh ID | Stored mesh address; attributes show the advertised address and whether it matches. |
| Current Bluetooth Source | Last-known preferred advertisement source while connected or during post-disconnect grace; otherwise requires a recent packet. |
| Last Connection | Adapter/proxy used for the last successful connection, when identifiable. |

Diagnostics use existing Bluetooth data without creating additional connections.
Cached advertisements retain their original reception time across reloads; only
packets within the availability timeout count as visible. Startup grace does not
confirm visibility. While BLE-connected, source and RSSI retain their last-known
values until advertisements update them. This does not refresh Last Seen. After
a regular idle disconnect, these values remain for the configured availability
timeout (30 s by default). A new advertisement resumes normal packet freshness;
without one, source/RSSI become unknown and status becomes unreachable when the
grace period expires. Reloading does not create post-disconnect grace. Last Seen and Last
Connection retain their historical meaning.
The diagnostic download includes command timing and write attempts, historical errors
with command context, connection timestamps and the last observed mesh-ID timestamp.
Command duration covers the client operation, excluding earlier entity waiting.
Mesh-ID mismatches are reported without changing the stored address.

## Settings

Open the integration and choose **Configure**. Options apply to its mesh entry.

| Setting | Default | Purpose |
|---|---|---|
| Transition | 0.2 s | Fade duration; use `0` for immediate changes. Explicit Home Assistant `transition` values override it. |
| Command interval | 250 ms | Minimum spacing between consecutive writes to the same lamp; configurable from 0–2000 ms. |
| Idle disconnect | 10 s | Release the BLE connection after inactivity. A longer value can speed up repeated control but occupies a connection slot longer. |
| Max Concurrent Commands | 2 | Allow 1–32 simultaneous command operations per entry. Available adapter/proxy capacity still limits operation. |
| Availability timeout | 30 s | Mark a lamp unavailable after 10–300 seconds without Bluetooth liveness or a working HA connection. |

- **Connections:** New control connections are established one at a time across AwoX entries. Already-connected lamps can be controlled in parallel, within the configured limit.
- **Rapid changes:** Pending power, brightness, appearance and effect intent are merged by the command manager. Newer values replace only the relevant target slots; unsent work is replanned against the latest known lamp state. A successful brightness write or actual native-effect start creates a manager-owned power-finalizer obligation that survives ordinary replanning.
- **Combined commands:** Brightness, color/CCT and native-effect targets can be submitted together. Requests are normalized first (for example, Power OFF/brightness 0 dominates the same request), then the sequence planner applies the documented order for OFF/ON/effect states.
- **Command spacing:** The command manager enforces the configured minimum interval between every two writes to the same lamp, including manager-approved retries and commands originating from separate HA requests. A value of 0 ms disables only the intentional wait.
- **Native effects:** Color cycle and Candle run in the lamp. Each actual effect start is followed by a required power command, normally ON or OFF if a newer request requires it; this also applies when the lamp is assumed to be ON. The configured command spacing still applies. Brightness changes preserve an active effect, while an unexpected stored-effect state while OFF is treated as a recovery condition: the sequencer clears it with the matching effect-stop command before planning the new request. Unknown effect state is tracked internally until confirmed. See the [protocol reference](docs/protocol.md) for mode tables and command details.
- **Lamp state:** Bluetooth advertisements correct power, brightness, color mode and native effect state. These updates can pause while Home Assistant or the phone app holds a connection; idle disconnect alone does not make the lamp unavailable.

## Technical documentation

- [Command sequences](docs/command-sequences.md): command order, spacing and recovery rules.
- [Known firmware behavior](docs/known-firmware-behavior.md): observed timing and state-reporting limitations.
- [Protocol reference](docs/protocol.md): frames and advertisement formats.

## Troubleshooting

### The first command after a pause is slower

After idle disconnect, a new BLE connection and authentication are needed.
Another AwoX connection attempt can also delay the start.

### Long waits when a lamp is unreachable or powered off

If failed connection attempts delay other lamps, you can reduce the ESPHome proxy's
connection timeout. Add this option to the existing `esp32_ble` section (do not
create a duplicate section), then install the updated firmware on the proxy:

```yaml
esp32_ble:
  connection_timeout: 10s
```

This can shorten unsuccessful connection attempts. It also affects other BLE
devices on that proxy, so check their reliability afterwards. It does not change
idle disconnect or guarantee a ten-second total command duration.
See the [ESPHome BLE documentation](https://esphome.io/components/esp32_ble/).

### A lamp cannot be reached

Check power, usable connection range and free proxy/adapter connection slots.
Disconnect the official AwoX / EGLO phone app if it holds the lamp's BLE connection.
A lamp may advertise even when a controllable connection cannot be established.

### State differs from the physical lamp

Live correction depends on long BLE advertisements, not GATT status notifications.
Allow advertisements to resume after an active BLE connection ends. Model and
firmware differences may also affect state decoding.

## Reports and contributions

Include the lamp model, firmware/hardware versions, Home Assistant version,
proxy firmware or adapter details, concurrency setting and relevant timestamped logs.
Never publish account passwords, mesh credentials or session tokens.
See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## Affiliation and license

Unofficial community project; not affiliated with or endorsed by EGLO, AwoX,
Home Assistant or Nabu Casa. Names and trademarks belong to their owners; included
brand artwork identifies the supported product family. See [NOTICE.md](NOTICE.md).
Source code is licensed under the [MIT License](LICENSE).
