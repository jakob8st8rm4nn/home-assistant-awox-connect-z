# AwoX Connect.Z for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)
[![Home Assistant 2026.3+](https://img.shields.io/badge/Home%20Assistant-2026.3%2B-18BCF2.svg)](https://www.home-assistant.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Unofficial Home Assistant custom integration for **EGLO / AwoX Connect.Z RGB/TW lights**.

The integration controls supported lamps locally over Bluetooth, including through
Home Assistant's **ESPHome Bluetooth Proxy** routing. The AwoX / EGLO HomeControl
cloud is used only during initial setup to import the account's lamp metadata and
the local `service=zigbee` mesh credential required for BLE authentication.

> **Status:** experimental / community-supported. It works reliably on the tested
> hardware listed below, but other Connect.Z models and firmware versions still need
> community testing.

## Features

- Power on/off
- Brightness control
- Hue / saturation color control
- Tunable white / color temperature
- Home Assistant `transition` support
- Automatic import of compatible BLE lights from an AwoX / EGLO HomeControl account
- Automatic Home Assistant Bluetooth routing
- ESPHome Bluetooth Proxy support
- BLE reconnect and retry logic
- Re-authentication after reconnect
- Configurable idle disconnect to release Bluetooth connection slots
- Optimistic state restore after Home Assistant restarts
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
- Internet access during initial setup only, for the one-time account import

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

## Configuration

Go to:

**Settings → Devices & services → Add integration → AwoX Connect.Z**

Enter only:

- AwoX / EGLO HomeControl email address
- AwoX / EGLO HomeControl password

The integration then imports compatible BLE lights from the account automatically.

### Credential handling

The AwoX account password is used only during the setup flow and is **not stored**
in the Home Assistant config entry.

The integration stores the local `service=zigbee` mesh credential needed to
authenticate directly to the lamps over BLE. This is what allows normal operation
to remain local after setup.

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

Default: **20 seconds**

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

## Important Bluetooth note

A Connect.Z lamp may not be connectable by Home Assistant while another device,
such as the official AwoX / EGLO app on a phone, already holds the BLE connection.

If Home Assistant reports errors such as:

```text
No backend with an available connection slot ...
only in non-connectable history ...
```

close/disconnect the official app and wait for the lamp to advertise again.

## Known limitations

- Lamp state is currently **optimistic** in Home Assistant.
- Changes made in the official app are not yet synchronized back into Home Assistant.
- The 20-byte status notifications from the lamp are not fully decoded yet.
- Device compatibility outside the tested model is not yet known.
- Bluetooth connection availability depends on adapter/proxy range and free connection slots.

## How it works

The local protocol was reconstructed for interoperability from BLE HCI traffic
generated by the official HomeControl application.

The implementation includes:

- the Connect.Z pairing/authentication exchange,
- per-connection session-key generation,
- command encryption,
- the Connect.Z CRC-8 command checksum,
- power commands,
- Zigbee Level Control commands,
- Zigbee Color Control hue/saturation commands,
- Zigbee Color Control color-temperature commands.

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

State changes made outside Home Assistant are not yet decoded from lamp
notifications. Send a command from Home Assistant to update the optimistic state.

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

## AI-assisted development

This project was developed with substantial assistance from **OpenAI ChatGPT**,
including protocol analysis, code generation, debugging and documentation.

The generated code was iteratively validated against real BLE captures and tested on
physical lamps, but the project should still be treated as experimental community
software. Human review, additional device testing and external contributions are
welcome.

## Trademark / affiliation notice

This is an **unofficial community project** and is not affiliated with, endorsed by,
or supported by EGLO, AwoX, Home Assistant or Nabu Casa.

EGLO, AwoX, Connect.Z and related names, logos and trademarks belong to their
respective owners. Brand artwork included with the integration is used only to help
users identify the supported product family.

## License

The source code is licensed under the [MIT License](LICENSE).
