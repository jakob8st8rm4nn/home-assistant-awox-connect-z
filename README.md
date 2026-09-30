# AwoX Connect.Z for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)
[![Home Assistant 2026.3+](https://img.shields.io/badge/Home%20Assistant-2026.3%2B-18BCF2.svg)](https://www.home-assistant.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Unofficial Home Assistant custom integration for **EGLO / AwoX Connect.Z RGB/TW lights**.

The integration controls supported lamps locally over Bluetooth, including through
Home Assistant's **ESPHome Bluetooth Proxy** routing. Setup can either import the required
mesh data from AwoX / EGLO HomeControl or be completed fully locally with the mesh name,
mesh password and each lamp's Bluetooth address / 16-bit mesh destination. Normal light
control does not use the cloud.

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
- Fully local setup without a cloud login using mesh credentials and Bluetooth-discovered or manually entered lamp data
- Manual account refresh through Home Assistant's **Reconfigure** flow
- Per-device mesh addressing using the imported 16-bit HomeControl address
- Automatic Home Assistant Bluetooth routing
- ESPHome Bluetooth Proxy support
- BLE reconnect and retry logic
- Re-authentication after reconnect
- Configurable idle disconnect to release Bluetooth connection slots
- Configurable concurrent lamp command limit (1-32 per configured mesh entry)
- Optimistic state restore after Home Assistant restarts
- Live state correction from Connect.Z BLE advertisements for already configured lamps
- Configurable Bluetooth liveness availability with automatic recovery
- Discovery of newly provisioned Connect.Z lamps from BLE advertisements
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
- Either an AwoX / EGLO HomeControl account **or** the local mesh name/password plus each lamp's MAC and mesh ID
- Internet access only when using the optional HomeControl cloud import or cloud **Reconfigure** flow

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

## Upgrade notes

Version 1.6.0 adds an optional fully local setup path and local mesh management.
Existing cloud-imported entries, lamp entities and saved options are retained. Update
and restart Home Assistant; existing installations do not need to be removed or
reconfigured, and do not need to switch to local setup.

For a new local setup, the mesh name and password must already be known. They are
not the HomeControl account login. The integration does not read these credentials
from a lamp or provision a new mesh. Each value must fit within 16 UTF-8 bytes.
Lamps can be discovered or entered manually using their MAC address and mesh ID.

The local setup uses a 30-second discovery window. Where supported, Home Assistant's
active-scan API is explicitly requested; older supported versions collect normal
Bluetooth advertisements. Selected lamps are authenticated with up to two checks in
parallel and a 20-second budget per lamp. Normal command timing and parallelism are
unchanged.

- **From 1.4.x or earlier:** version 1.5.0 introduced lamp availability tracking,
  with a default of 30 seconds. The value is configurable from 10 to 300 seconds.
  Automations should account for lamps becoming unavailable when Bluetooth
  liveness is lost.
- **From 1.1.x or earlier:** version 1.2.0 changed the default idle-disconnect timeout
  to **10 seconds**. An explicitly saved timeout is retained.
- **From 1.0.x:** version 1.1.0 introduced mandatory individual 16-bit mesh destinations.
  If a lamp is skipped because its mesh destination is missing or invalid, use
  **Reconfigure** to refresh the HomeControl device metadata.

For release-by-release details, see [CHANGELOG.md](CHANGELOG.md).

## Configuration

Go to:

**Settings → Devices & services → Add integration → AwoX Connect.Z**

Home Assistant offers two setup paths.

### HomeControl cloud import

Choose **Via AwoX / EGLO HomeControl** and enter the HomeControl email address and
password. The integration imports compatible lamps, their individual 16-bit mesh
destinations and the local mesh credential automatically.

The AwoX account password is used only during setup or cloud reconfiguration and is
**not stored** in the Home Assistant config entry.

### Fully local setup

Choose **Local mesh credentials** and enter the local mesh name and mesh password.
These credentials must already be known; the integration cannot read them from a lamp
and does not create or provision a new mesh. Both mesh name and mesh password must be non-empty and each fit within 16 UTF-8 bytes, matching the protocol limit. No AwoX / EGLO cloud login is performed.

Home Assistant then collects Bluetooth advertisements during a 30-second discovery window and lists
every unconfigured Connect.Z lamp for which a complete long advertisement with a valid
16-bit mesh ID is freshly observed during that window. Old cached Bluetooth records are
not treated as discoveries from the new scan. On Home Assistant versions that expose the one-shot active-scan API (introduced in Home Assistant 2026.6), the flow explicitly requests an active Bluetooth sweep for the scan window. Older supported versions fall back to normal callback collection. If the active-scan API exists but fails at runtime, the flow keeps collecting normal advertisements for the remaining window and shows a recoverable error so the search can be retried. When a scan returns no devices, the setup page explicitly shows **No matching Connect.Z lamps were found** instead of an empty device list. The collected lamps are shown together
and selected by default. A lamp that did not send a complete long advertisement can be
added manually from its Bluetooth MAC address and mesh ID (decimal or hexadecimal, for
example `0xD361`); after verification it returns to the same shared selection list. The device-selection page also offers **Search again** to run another scan without leaving the flow. On a rescan, devices that are still freshly visible keep their current checked/unchecked selection state, newly discovered devices are checked automatically, and devices no longer freshly seen disappear.

A manually assigned lamp name is preserved if the same lamp is found by a later **Search again** scan. If a later long advertisement reports a different mesh ID, the integration does not silently overwrite the manual/stored target address: it shows both IDs and requires an explicit manual correction. Local Reconfigure also scans devices already in the current hub, so an incorrectly stored mesh ID can be detected and fixed.

Before the entry is saved, Home Assistant authenticates locally to every selected lamp
with the entered mesh credential without sending a light command. These interactive checks
run with a 20-second setup-specific timeout per lamp and up to two lamps are checked concurrently, so an
offline selection does not wait through the normal command client's full discovery retry
window. If several selected lamps are unreachable, they are reported together. Successful checks are
cached for the lifetime of the setup dialog, so a manually verified lamp is not connected
a second time during final save unless the mesh credentials change.
Immediately before a local entry is created or updated, Home Assistant performs a final ownership check of the selected Bluetooth MAC addresses. This closes the race where another setup flow could claim the same lamp while authentication was running.

A local AwoX mesh is represented by **one Home Assistant config entry / hub containing
all selected lamps**, rather than one hub per lamp. Each new local mesh receives a random,
stable Home Assistant unique ID that is independent of the mutable mesh name/password.
Re-running local setup with the same mesh credentials appends newly selected lamps to the
existing local mesh instead of creating a duplicate hub.

The mesh password is stored in the config entry because it is required for normal local
BLE authentication. Additional provisioned lamps can also be added later through the
normal Bluetooth discovery flow when they accept the same stored mesh credential. If a lamp is added through local setup while a Bluetooth discovery card for the same MAC is still open, that stale discovery card is closed automatically.

### Reconfigure

Open **Settings → Devices & services**, find **AwoX Connect.Z**, open the entry menu
and choose **Reconfigure**.

For a cloud-imported entry, the email address is prefilled. Enter the AwoX / EGLO
HomeControl password again. The integration signs in temporarily, verifies that the
credentials belong to the same configured account, then refreshes:

- compatible lamps,
- per-lamp mesh addresses and cloud metadata,
- the local `service=zigbee` mesh name and password.

The account password and cloud session token are not stored. After the refresh,
the config entry is reloaded automatically. Lamps newly added to the same
HomeControl account can therefore appear in Home Assistant without removing and
re-adding the integration.

For a locally configured entry, **Reconfigure** asks for the mesh name and mesh
password and verifies them against at least one configured lamp. It then performs the
same 30-second discovery window for additional unconfigured lamps and also offers the manual
MAC/mesh-ID fallback. Existing lamps stay in the hub; newly selected lamps are appended.
No cloud login is used.

### Bluetooth discovery of newly added lamps

If an additional Connect.Z lamp is later provisioned in the official AwoX / EGLO
HomeControl app, Home Assistant can discover the lamp from its Connect.Z BLE
manufacturer advertisements.

The discovered lamp is **not added silently**. Home Assistant shows it as a discovered
device first. When you confirm the setup, the integration connects locally and verifies
that the lamp accepts one of the mesh credentials already stored by an existing
AwoX Connect.Z entry (cloud-imported or locally configured). No AwoX account password or
cloud login is used for this verification.

Only after local mesh authentication succeeds is the lamp appended to the matching
existing mesh entry and that entry is reloaded.

This discovery path is intended for lamps that have already been provisioned into the
same AwoX mesh. A factory-reset lamp or a lamp belonging to another mesh may be seen by
Bluetooth, but it cannot be added through this flow because it will not authenticate
with the stored mesh credential.

Advertisement discovery initially uses the BLE local name together with the full Bluetooth
MAC address (for example `EdBmpjEw (A4:C1:38:C6:68:B8)`). If no useful BLE local name is
available, the full MAC address is used on its own. For cloud-imported entries, using
**Reconfigure** later can refresh the official HomeControl name and metadata. Locally
configured entries remain cloud-independent.

### Lamp availability

A configured lamp is considered available while either:

- Home Assistant has a working BLE connection to it, or
- Home Assistant has seen any Bluetooth packet from it within the configured availability timeout.

Short and long Connect.Z advertisements both count as liveness. Identical repeated
advertisements count as well, even when Home Assistant suppresses duplicate
integration callbacks.

The normal BLE idle disconnect does **not** make a lamp unavailable. A successful
HA connection is itself a liveness signal, and the configured timeout begins again
when an intentional idle disconnect releases that known-good session. If no packet is seen and no working HA BLE connection remains for the configured
timeout, the light entity becomes unavailable. It returns to available automatically
when Bluetooth activity is seen again. Recovery from an unchanged advertisement
may take about one second because its timestamp is checked periodically. The default
timeout is **30 seconds** and
can be configured from **10 to 300 seconds** in the integration options.

## Options

The config-entry-wide options include the default transition, BLE idle-disconnect time,
maximum concurrent lamp commands, and the **availability timeout**. The availability
timeout defaults to **30 seconds** and accepts values from **10 to 300 seconds**.

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
