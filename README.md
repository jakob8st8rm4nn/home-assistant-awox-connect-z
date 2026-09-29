# AwoX Connect.Z for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)
[![Home Assistant 2026.3+](https://img.shields.io/badge/Home%20Assistant-2026.3%2B-18BCF2.svg)](https://www.home-assistant.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Unofficial Home Assistant custom integration for **EGLO / AwoX Connect.Z RGB/TW lights**.

The integration controls supported lamps locally over Bluetooth, including through
Home Assistant's **ESPHome Bluetooth Proxy** routing. The AwoX / EGLO HomeControl
cloud is used during initial setup and only when the user manually chooses **Reconfigure**
to refresh the account's lamp metadata and the local `service=zigbee` mesh credential
required for BLE authentication.

> **Status:** experimental / community-supported. It works reliably on the tested
> hardware listed below, but other Connect.Z models and firmware versions still need
> community testing.

## AI-assisted development

This project was developed with substantial assistance from **OpenAI ChatGPT**,
including protocol analysis, code generation, debugging and documentation.

The generated code was iteratively validated against real BLE captures and tested on
physical lamps, but the project should still be treated as experimental community
software. Human review, additional device testing and external contributions are
welcome.

## Features

- Power on/off
- Brightness control
- Hue / saturation color control
- Tunable white / color temperature
- Home Assistant `transition` support
- Automatic import of compatible BLE lights from an AwoX / EGLO HomeControl account
- Manual account refresh through Home Assistant's **Reconfigure** flow
- Per-device mesh addressing using the imported 16-bit HomeControl address
- Automatic Home Assistant Bluetooth routing
- ESPHome Bluetooth Proxy support
- BLE reconnect and retry logic
- Re-authentication after reconnect
- Configurable idle disconnect to release Bluetooth connection slots
- Configurable concurrent lamp command limit (1-32 per AwoX account)
- Optimistic state restore after Home Assistant restarts
- Live state correction from Connect.Z BLE advertisements for already configured lamps
- Diagnostics without exposing the AwoX account password or local mesh credential
- Local integration branding on Home Assistant 2026.3+

## Tested hardware

| Device | Cloud model | Firmware | Hardware | Result |
|---|---|---:|---:|---|
| EGLO `900024/12253` | `EGLO-ZM-RGB-TW` | `3.0.2` | `4.62` | Power, brightness, HS color and color temperature tested |

If your model works, please open a compatibility report so the table can be expanded.

## Requirements

- Home Assistant **2026.3 or newer**
- Home Assistant Bluetooth integration
- A connectable Bluetooth adapter or ESPHome Bluetooth Proxy that can reach the lamp
- An AwoX / EGLO HomeControl account containing the Connect.Z lamps
- Internet access during initial setup and when manually using **Reconfigure**

## Installation

### HACS custom repository

Until this repository is included in the default HACS catalog:

1. Open **HACS** in Home Assistant.
2. Open the menu and choose **Custom repositories**.
3. Add this GitHub repository URL.
4. Select **Integration** as the category.
5. Install **AwoX Connect.Z**.
6. Restart Home Assistant.

### Manual installation

Copy:

```text
custom_components/awox_connect_z
```

to:

```text
/config/custom_components/awox_connect_z
```

and restart Home Assistant.

## Upgrading from 1.2.0

Update the integration and restart Home Assistant. Existing account, lamp and
option settings are retained. Use **Reconfigure** whenever you want to refresh
the imported account data or add newly registered HomeControl lamps.

## Upgrading from 1.1.0

Update the integration and restart Home Assistant. Existing account and lamp
configuration can be kept.

Version 1.2.0 changes the default idle-disconnect timeout to **10 seconds**. An
explicitly saved timeout remains unchanged; the new default applies when no value
has been saved. You can adjust it in the integration options.

## Upgrading from 1.0.0

Versions 1.1.0 and later require a valid individual 16-bit mesh destination for every lamp.
Existing cloud imports from version 1.0.0 already store this value, so installations
with valid mesh addresses can be upgraded without removing the integration.

If Home Assistant logs report that a lamp was skipped because it has no valid mesh
destination, use **Reconfigure** on the existing **AwoX Connect.Z** entry and sign
in again with the same account. This reruns the HomeControl import and refreshes
the device metadata without deleting the integration.

## Configuration

Go to:

**Settings → Devices & services → Add integration → AwoX Connect.Z**

Enter only:

- AwoX / EGLO HomeControl email address
- AwoX / EGLO HomeControl password

The integration then imports compatible BLE lights from the account automatically.

### Credential handling

The AwoX account password is used only during initial setup or manual
reconfiguration and is **not stored** in the Home Assistant config entry.

The integration stores the local `service=zigbee` mesh credential needed to
authenticate directly to the lamps over BLE. This is what allows normal operation
to remain local after setup.


### Reconfigure / refresh account data

Open **Settings → Devices & services**, find **AwoX Connect.Z**, open the entry menu
and choose **Reconfigure**.

The email address is prefilled. Enter the AwoX / EGLO HomeControl password again.
The integration signs in temporarily, verifies that the credentials belong to the
same configured account, then refreshes:

- compatible lamps,
- per-lamp mesh addresses and cloud metadata,
- the local `service=zigbee` mesh name and password.

The account password and cloud session token are not stored. After the refresh,
the config entry is reloaded automatically. Lamps newly added to the same
HomeControl account can therefore appear in Home Assistant without removing and
re-adding the integration.

## Options

Open the integration and choose **Configure**.

### Default transition

Default: **0.2 seconds**

The reverse-engineered commands from the official app use a 0.2-second Zigbee
transition by default. Very short intermediate colors can therefore be visible
when moving between distant colors. This also occurs with the official app.

Set the value to `0` if you prefer immediate changes.

Home Assistant service calls that explicitly include `transition:` override the
integration default.

### Idle disconnect

Default: **10 seconds**

After the lamp has been idle for this period, the BLE connection is closed to free
a connection slot on the Bluetooth adapter or ESPHome proxy.

The first command after an idle disconnect can therefore take longer because the
integration must:

1. find a connectable Bluetooth path,
2. establish the BLE connection,
3. authenticate to the lamp,
4. derive a new session key,
5. send the command.

Increase the idle timeout if you prefer faster repeated control and have enough BLE
connection slots available.


### Maximum concurrent lamp commands

Default: **1**

This setting controls how many AwoX lamp commands from the same account may execute at
the same time. Enter a value from **1 to 32**.

Use `1` for the most conservative behavior. Higher values can make group and scene
changes more synchronous when enough Bluetooth adapters/proxies and connection slots are
available. Setting the value higher than the available Bluetooth capacity can cause more
connection retries or temporary connection-slot errors.

This setting limits concurrent command operations. Existing BLE connections can remain
open until the configured idle-disconnect timeout expires.

## Important Bluetooth note

A Connect.Z lamp may not be connectable by Home Assistant while another device,
such as the official AwoX / EGLO app on a phone, already holds the BLE connection.

If Home Assistant reports errors such as:

```text
No backend with an available connection slot ...
only in non-connectable history ...
```

close/disconnect the official app and wait for the lamp to advertise again.

## Device addressing

Connect.Z command bytes 2-3 are treated as the 16-bit mesh destination in
**big-endian** order. The destination is imported from the lamp's HomeControl
device metadata. The integration deliberately does not use `0xFFFF` for normal
device control because that value can be forwarded as a mesh-wide command once
a session is warm.

## Known limitations

- Home Assistant commands are applied optimistically first; a later valid BLE advertisement corrects the entity to the hardware-reported state.
- A lamp may stop sending long state advertisements while another device holds its direct BLE connection, so external changes can appear only after advertisements resume.
- The GATT status characteristic/notification path is not used for live state; state synchronization currently relies on BLE advertisements.
- Device compatibility outside the tested model is not yet known.
- Bluetooth connection availability depends on adapter/proxy range and free connection slots.

## How it works

The local protocol was reconstructed for interoperability from BLE HCI traffic
generated by the official HomeControl application.

The implementation includes:

- the Connect.Z pairing/authentication exchange,
- per-connection session-key generation,
- command encryption,
- per-device big-endian mesh destination addressing,
- the Connect.Z CRC-8 command checksum,
- power commands,
- Zigbee Level Control commands,
- Zigbee Color Control hue/saturation commands,
- Zigbee Color Control color-temperature commands,
- Connect.Z `0x0160` advertisement state decoding.

No firmware modification is required.

## Troubleshooting

### The first command is slow

This is normally expected after the configured idle disconnect timeout. A fresh BLE
connection and AwoX authentication must be established first.

### The lamp cannot be reached

Check that:

- the lamp is powered,
- the official phone app is not currently connected,
- a connectable Home Assistant Bluetooth adapter or ESPHome proxy can hear the lamp,
- the proxy has a free BLE connection slot.

### Home Assistant shows an old optimistic state

The integration corrects light state when it receives a valid long Connect.Z
manufacturer advertisement. If another device currently holds the lamp's direct BLE
connection, long state advertisements may pause. Wait for advertisements to resume
or close the other BLE connection.

Home Assistant's own BLE connection can also pause these reports until the
idle-disconnect timeout expires. On older Home Assistant versions without
advertisement-history controls, state correction may have to wait until the
advertised data changes.

## Contributing

Reports from other EGLO / AwoX Connect.Z models are especially valuable.

When opening an issue, please include:

- exact EGLO/AwoX model number,
- HomeControl cloud model if known,
- firmware version,
- hardware version,
- Home Assistant version,
- whether a local Bluetooth adapter or ESPHome Bluetooth Proxy is used,
- relevant Home Assistant log output.

**Never post your AwoX password, Parse session token, mesh access token or other
account secrets.**

See [CONTRIBUTING.md](CONTRIBUTING.md) for more details.


## Trademark / affiliation notice

This is an **unofficial community project** and is not affiliated with, endorsed by,
or supported by EGLO, AwoX, Home Assistant or Nabu Casa.

EGLO, AwoX, Connect.Z and related names, logos and trademarks belong to their
respective owners. Brand artwork included with the integration is used only to help
users identify the supported product family.

## License

The source code is licensed under the [MIT License](LICENSE).
