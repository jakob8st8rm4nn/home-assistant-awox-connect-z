# AwoX Connect.Z for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)
[![Home Assistant 2026.3+](https://img.shields.io/badge/Home%20Assistant-2026.3%2B-18BCF2.svg)](https://www.home-assistant.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Unofficial integration for **EGLO / AwoX Connect.Z RGB/TW lights**, controlled locally
over Home Assistant Bluetooth adapters and **ESPHome Bluetooth Proxies**.
Import lamp data from HomeControl or set up a mesh locally using known credentials.
Normal light control does not use the cloud or require modified lamp firmware.

> **Experimental / community-supported:** compatibility beyond the tested hardware
> still needs confirmation.

> **Known Home Assistant issue after idle disconnect:** In HA 2026.9.4, clearing
> advertisement history can remove the Bluetooth device record needed to reconnect.
> A command before the next advertisement may fail or be delayed, causing lamps to
> respond at different times. The upstream fix is included in
> [habluetooth 7.0.0](https://github.com/Bluetooth-Devices/habluetooth/releases/tag/v7.0.0).
> Retest after installing a Home Assistant update that includes this correction;
> no additional integration workaround is planned for now.
> [Upstream issue/fix](https://github.com/Bluetooth-Devices/habluetooth/pull/613).

## AI-assisted development

This project was developed with substantial assistance from **OpenAI ChatGPT**,
including protocol analysis, code generation, debugging and documentation.
Development included real BLE captures and physical lamp testing; further human
review and device compatibility reports are welcome.

## Features and tested hardware

- Power, brightness, hue/saturation color, tunable white and transitions.
- HomeControl import, fully local setup and Reconfigure for lamp/mesh management.
- Live state correction from BLE advertisements and configurable availability.
- On-demand BLE connections, idle disconnect, configurable command concurrency and same-lamp latest-wins command coalescing.
- Bluetooth discovery of additional provisioned lamps plus per-lamp diagnostic sensors and credential-redacted diagnostics.

| Device | Cloud model | Firmware | Hardware | Tested functions |
|---|---|---|---|---|
| EGLO `900024/12253` | `EGLO-ZM-RGB-TW` | `3.0.2` | `4.62` | Power, brightness, HS color, color temperature |

## Installation

Requires **Home Assistant 2026.3+**, its Bluetooth integration, and a connectable
adapter or ESPHome Bluetooth Proxy within connection range of the lamps.

**HACS:** Add `https://github.com/jakob8st8rm4nn/home-assistant-awox-connect-z` under
**Custom repositories**, category **Integration**. Install **AwoX Connect.Z** and
restart Home Assistant.

**Manual:** Copy `custom_components/awox_connect_z` to
`/config/custom_components/awox_connect_z` and restart Home Assistant.

**Updating to 1.9.0:** Update and restart Home Assistant. Existing entries and saved
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
| Signal Strength | Bluetooth signal strength (RSSI). |
| Last Seen | Time of the last received Bluetooth advertisement. |
| Bluetooth Status | Connected, visible or unreachable; initially unknown until Bluetooth liveness is confirmed. |
| Mesh ID | Stored mesh address; attributes show the advertised address and whether it matches. |
| Current Bluetooth Source | Adapter/proxy supplying Home Assistant's currently preferred advertisement. |
| Last Connection | Adapter/proxy used for the last successful connection, when identifiable. |

Diagnostics use existing Bluetooth data without creating additional connections.
The diagnostic download includes command timing and write attempts, historical errors
with command context, connection timestamps and the last observed mesh-ID timestamp.
Command duration covers the client operation, excluding earlier entity waiting.
Mesh-ID mismatches are reported without changing the stored address.

## Settings

Open the integration and choose **Configure**. Options apply to its mesh entry.

| Setting | Default | Purpose |
|---|---|---|
| Transition | 0.2 s | Fade duration; use `0` for immediate changes. Explicit Home Assistant `transition` values override it. |
| Idle disconnect | 10 s | Release the BLE connection after inactivity. A longer value can speed up repeated control but occupies a connection slot longer. |
| Max Concurrent Commands | 2 | Allow 1–32 simultaneous command operations per entry. Available adapter/proxy capacity still limits operation. |
| Availability timeout | 30 s | Mark a lamp unavailable after 10–300 seconds without Bluetooth liveness or a working HA connection. |

- **Connections:** New control connections are established one at a time across AwoX entries. Already-connected lamps can be controlled in parallel, within the configured limit.
- **Rapid changes:** The newest waiting brightness and color/color-temperature values are kept. Off discards older waiting changes; commands already being sent are allowed to finish.
- **Off → on:** After a successful off write, commands that turn the lamp back on wait until 500 ms have elapsed. An off discarded before writing starts no delay.
- **Lamp state:** Bluetooth advertisements correct the displayed state. These updates can pause while Home Assistant or the phone app holds a connection; idle disconnect alone does not make the lamp unavailable.

## Troubleshooting

### The first command after a pause is slower

After idle disconnect, a new BLE connection and authentication are needed.
Another AwoX connection attempt can also delay the start. See the known
Home Assistant advertisement-history issue near the top of this page.

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
