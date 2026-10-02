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
- Bluetooth discovery of additional provisioned lamps and credential-redacted diagnostics.

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

**Updating to 1.7.0:** Update and restart Home Assistant. Existing entries and saved
options are retained; no reconfiguration is required. Rapid same-lamp changes now
coalesce while they are still waiting, instead of building a FIFO backlog. The
default command concurrency remains **2**; explicitly saved values remain unchanged.
For older installations reporting a missing or invalid mesh destination, use
**Reconfigure** to refresh lamp data. See [CHANGELOG.md](CHANGELOG.md) for details.

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

## Settings

Open the integration and choose **Configure**. Options apply to its mesh entry.

| Setting | Default | Purpose |
|---|---|---|
| Transition | 0.2 s | Fade duration; use `0` for immediate changes. Explicit Home Assistant `transition` values override it. |
| Idle disconnect | 10 s | Release the BLE connection after inactivity. A longer value can speed up repeated control but occupies a connection slot longer. |
| Max Concurrent Commands | 2 | Allow 1–32 simultaneous command operations per entry. Available adapter/proxy capacity still limits operation. |
| Availability timeout | 30 s | Mark a lamp unavailable after 10–300 seconds without Bluetooth liveness or a working HA connection. |

**Connection scheduling:** New runtime connections are established one at a time
across all loaded AwoX entries, including entries using different proxies. Lamps
already connected can still process commands in parallel when command capacity is
available. Setup/Reconfigure and other integrations do not use this runtime gate.

Waiting for the connection gate has a separate 30-second limit and does not consume
the 30-second active command budget. Connect/authentication/write stages have
12/4/4-second limits. Final disconnect cleanup can add up to its own 12-second
budget. These are per-command processing limits, not a guaranteed duration for an
entire user action or a queue of actions. A failed initial connection stops that
command; write failures and recognizable GATT cache problems allow limited recovery.

**Same-lamp command coalescing:** Rapid changes retain the newest waiting brightness
and the newest color or color temperature. An off request discards older waiting
changes. Targets remain replaceable during connection preparation and are selected
immediately before writing. A command already being written finishes its bounded
retry handling before the next target is selected; off cannot interrupt that command.

**Rapid off/on compatibility:** After a successful off write, commands that can turn
the lamp back on wait until **500 ms since that write have elapsed**. The remaining
time is checked again immediately before selection. This addresses the observed
behavior where a rapid brightness command after off updates the value but leaves
the tested lamp physically off. An off discarded before writing starts no delay;
ordinary commands have no artificial debounce. A new off arriving during an already
started settling wait can also wait for the remainder of that pause.

Cancelled calls discard their unsent targets while newer requests are preserved,
including during disconnect cleanup. Fully superseded calls can complete without
their own value being written. Successful writes update state optimistically;
valid advertisements subsequently correct the reported lamp state.

**Availability and state:** A working BLE connection or recent Bluetooth packets
count as liveness. Receiving packets does not guarantee that a GATT connection will
succeed. An intentional idle disconnect does not immediately mark a lamp unavailable.
Commands update state optimistically; valid long advertisements later correct it.
State updates can pause while Home Assistant or another app holds a BLE connection.

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
