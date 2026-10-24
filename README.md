# AwoX Connect.Z for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)
[![Home Assistant 2026.10+](https://img.shields.io/badge/Home%20Assistant-2026.10%2B-18BCF2.svg)](https://www.home-assistant.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Unofficial integration for **EGLO / AwoX Connect.Z RGB/TW lights**, controlled locally
over Home Assistant Bluetooth adapters and **ESPHome Bluetooth Proxies**.
Import lamp data from HomeControl or set up a mesh locally using known credentials.
Normal light control does not use the cloud or require modified lamp firmware.
Each lamp is connected directly; mesh forwarding and route selection are not supported.

> **Experimental / community-supported:** compatibility beyond the tested hardware
> still needs confirmation.

## AI-assisted development

This project was developed with substantial assistance from **OpenAI ChatGPT**,
including protocol analysis, code generation, debugging and documentation.
Development included real BLE captures and physical lamp testing; further human
review and device compatibility reports are welcome.

## Features and tested hardware

- Power, brightness, hue/saturation color, tunable white (**2700–6500 K**), transitions and the native **Color cycle** / **Candle** effects.
- HomeControl import, fully local setup and Reconfigure for lamp/mesh management.
- State updates from BLE advertisements, command-group confirmation through session-scoped 1911 replies, and configurable availability.
- On-demand BLE connections, idle disconnect, configurable command concurrency and same-lamp latest-wins command coalescing.
- Bluetooth discovery of additional provisioned lamps plus per-lamp diagnostic sensors and credential-redacted diagnostics.

| Device | Cloud model | Firmware | Hardware | Tested functions |
|---|---|---|---|---|
| EGLO `900024/12253` | `EGLO-ZM-RGB-TW` | `3.0.2` | `4.62` | Power, brightness, HS color, color temperature, Color cycle, Candle |

## Installation

Requires **Home Assistant 2026.10+**, its Bluetooth integration, and a connectable
adapter or ESPHome Bluetooth Proxy within connection range of the lamps.
Control requires a known lamp state. Fresh advertisements can supply it; if it is
unknown at request time, the integration connects and makes a bounded 1911 status
query before planning. Normal control also requires working 1911 notifications
on the directly connected lamp. Raw advertisements remain an independent state
source. An unresolved unknown state fails the request instead of waiting indefinitely.

**HACS:** Add `https://github.com/jakob8st8rm4nn/home-assistant-awox-connect-z` under
**Custom repositories**, category **Integration**. Install **AwoX Connect.Z** and
restart Home Assistant.

**Manual:** Copy `custom_components/awox_connect_z` to
`/config/custom_components/awox_connect_z` and restart Home Assistant.

**Updating:** Only the current config-entry format is supported. Older entries
are not migrated automatically and may need to be removed and set up again.
See [CHANGELOG.md](CHANGELOG.md).

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
| Signal Strength | RSSI from the last received advertisement; retained as historical data. |
| Last Seen | Time of the last received Bluetooth advertisement. |
| Bluetooth Status | Connected, visible, recently connected, unreachable, or unknown. See the rules below. |
| Mesh ID | Stored mesh address; attributes show the advertised address and whether it matches. |
| Last Bluetooth Source | Bluetooth source of the last received advertisement; retained independently of connection state. |
| Last Connection | Adapter/proxy used for the last successfully authenticated connection, when identifiable. |

The light is available while connected, visible or recently connected. One
**Availability timeout** applies independently to the last advertisement and the
last connection proof. When disconnected and both are still valid, the newer
evidence decides the status: the final connection proof remains recently connected
until a newer advertisement arrives. A regular idle disconnect records its final
known live connection before cleanup; unexpected link loss does not start a new timeout.
With no evidence the status is unknown and the light is unavailable. Once known
evidence expires, status is unreachable and the light is unavailable.

Reload preserves original evidence timestamps within the same HA process, without
resetting evidence ages or restoring BLE sessions. A still-active connection at unload
provides a final connection proof before cleanup. The last advertisement source, RSSI
and Last Seen history are retained across an entry reload. Newer advertisement
history replaces them; equal-time packets may refine source/RSSI. A full HA restart
obtains new evidence. Last Seen, Signal
Strength and Last Bluetooth Source are updated only by advertisements. Diagnostics
create no extra BLE requests. Exact transitions and reload rules: [availability model](docs/availability.md).

The diagnostic download includes the notification setup method (`status_notify_method`), the last session-scoped 1911 status, initial-status poll counts, confirmation polls, passive confirmations, repeats and unconfirmed/superseded batches. Last status remains diagnostic history after disconnect, but cannot confirm a new session. It also includes command timing and write attempts, historical errors
with command context, connection timestamps and the last observed mesh-ID timestamp.
Command duration covers the client operation, excluding earlier entity waiting.
Mesh-ID mismatches are reported without changing the stored address.

## Settings

Open the integration and choose **Configure**. Options apply to its mesh entry.

| Setting | Default | Purpose |
|---|---|---|
| Transition | 0.2 s | Default fade duration, configurable from 0–30 s; use `0` for immediate changes. Explicit Home Assistant `transition` values override it. Subsequent controls allow extra time for the fade; see [command spacing](docs/command-sequences.md#command-spacing). |
| Command interval | 250 ms | Minimum spacing between consecutive writes to the same lamp; configurable from 0–2000 ms. |
| Idle disconnect | 10 s | Release the BLE connection after inactivity. A longer value can speed up repeated control but occupies a connection slot longer. |
| Max Concurrent Commands | 2 | Allow 1–32 simultaneous command operations per entry. Available adapter/proxy capacity still limits operation. |
| Availability timeout | 30 s | Mark a lamp unavailable after 10–300 seconds without Bluetooth liveness or a working HA connection. |

- **Connections:** New connections are established one at a time across AwoX entries. Connected lamps can be controlled in parallel, within the configured limit and available Bluetooth slots.
- **Rapid changes:** A started command group finishes, including confirmation and any repeat. New requests are collected separately; newer values replace older waiting targets. Even OFF waits for the running group. Replaced waiting targets do not produce superseded errors.
- **Combined commands:** The integration orders color/CCT, brightness and effects and adds required power completion. See [Command sequences](docs/command-sequences.md) for exact rules.
- **Native effects:** Candle and Color Cycle run inside the lamp. The successful group-confirmation reply supplies the reported state, including convertible color/CCT. Color Cycle appearance is a momentary sample.
- **Confirmation:** Commands are confirmed through 1911 status, with bounded polling and at most one complete group repeat. Status confirmation does not guarantee physical light output.
- **State updates:** Advertisements provide normal state updates. 1911 updates the displayed state only after successful group confirmation or an initial UNKNOWN-state query; other replies remain diagnostic data. Advertisements can pause during an active connection; this alone does not make the lamp unavailable.

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

State updates use complete BLE advertisements and the selected result of a
successful 1911 group confirmation or initial UNKNOWN-state query. Other 1911
replies do not overwrite the displayed state or planner. Neither source proves
physical light output. Allow advertisements to resume after an active BLE
connection ends. For native effects, a group confirmation may update the displayed
color/CCT; later effect advertisements preserve that appearance.
Model and firmware differences may also affect state decoding.

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
