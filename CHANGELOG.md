# Changelog

All notable changes to this project will be documented in this file.

## [1.4.0] - 2026-09-29

### Added

- Home Assistant Bluetooth discovery for new Connect.Z lamps advertising the hardware-confirmed AwoX `0x0160` / `96 20` manufacturer-data pattern.
- Discovered lamps are offered through a normal Home Assistant discovery confirmation flow instead of being added silently.
- Before a discovered lamp is appended to an existing account entry, the integration locally authenticates to the lamp with the already stored mesh credentials and sends no light command.
- Advertisement identity validation checks the embedded lower MAC bytes against the physical BLE advertiser address.

### Changed

- A lamp added through Bluetooth discovery initially uses its BLE local name plus the full Bluetooth MAC address (falling back to the MAC alone); a later **Reconfigure** refresh can replace it with HomeControl cloud metadata.
- Bluetooth discovery now respects Home Assistant's ignored-device entries and excludes ignored entries when matching a discovered lamp to an existing AwoX account.
- The integration continues waiting for a complete long advertisement when discovery starts from an incomplete packet and allows a later packet to retrigger discovery after a timeout.

## [1.3.0] - 2026-09-29

### Added

- Home Assistant **Reconfigure** flow for refreshing the existing AwoX account import without removing the integration.
- Reconfiguration refreshes the current compatible lamp list, per-device mesh addresses, cloud metadata and local `service=zigbee` mesh credentials.
- Reconfiguration verifies that the supplied credentials belong to the same configured AwoX account before updating the entry.

### Changed

- The AwoX account password remains temporary during reconfiguration and is not stored.
- A successful reconfiguration reloads the existing config entry so newly imported lamps are set up immediately.

## [1.2.0] - 2026-09-29

### Added

- Live lamp-state synchronization from AwoX Connect.Z BLE manufacturer advertisements (`0x0160`) for already configured lamps.
- Advertisement decoding for power, brightness, hue/saturation color mode and white color temperature.
- Validation of the advertised mesh ID against the configured lamp before applying state.

### Changed

- Home Assistant now corrects its optimistic light state when a valid hardware advertisement is received, including changes made outside Home Assistant such as through the official app.
- After a Home Assistant GATT session ends, advertisement history is cleared when the running Home Assistant version supports it so the next identical lamp advertisement can be delivered again.
- On integration setup/reload, cached advertisement replay is disabled and advertisement deduplication history is cleared after callback registration where the respective Home Assistant APIs are available. This allows the next real packet to be delivered even when its payload is unchanged.
- Where supported by Home Assistant, advertisement history is also cleared during cleanup when a known BLE session has already dropped unexpectedly.
- The default BLE idle-disconnect timeout is now 10 seconds instead of 20 seconds so lamps can resume advertising sooner after Home Assistant control. Explicitly saved timeout settings remain unchanged.


## [1.1.0] - 2026-09-28

### Added

- Runtime-configurable limit for concurrent lamp commands per AwoX account.
- Shared per-account command semaphore so multiple lamps can be controlled in parallel.
- Per-lamp operation lock so overlapping service calls to the same lamp cannot interleave multi-command state changes.
- Per-device 16-bit mesh destination addressing imported from HomeControl.
- Number-box option for the concurrency limit, with a supported range of 1-32.

### Upgrade note

- Existing cloud-imported entries with valid mesh addresses can be upgraded directly. If logs report a missing or invalid mesh destination, remove and re-add the integration for the affected account to refresh HomeControl metadata. Older manually configured entries may require this step.

### Changed

- All power, brightness, hue/saturation and color-temperature commands now use the lamp's individual big-endian mesh destination instead of `0xFFFF`, preventing warm-session commands from affecting other lamps in the mesh.
- Home Assistant's static light-platform semaphore is disabled (`PARALLEL_UPDATES = 0`);
  the integration now controls command concurrency itself.
- The AI-assisted development notice is shown near the top of the README for clearer
  disclosure.

## [1.0.0] - 2026-09-26

Initial public release.

### Added

- Local BLE control for EGLO / AwoX Connect.Z lamps.
- Power on/off.
- Dynamic brightness control.
- Hue/saturation color control.
- Color-temperature control.
- Automatic import of compatible BLE lamps from an AwoX / EGLO HomeControl account.
- ESPHome Bluetooth Proxy support through Home Assistant Bluetooth routing.
- BLE reconnect, retry and re-authentication logic.
- Configurable default transition time.
- Configurable BLE idle-disconnect timeout.
- Optimistic state restore.
- Diagnostics.
- Local integration branding for Home Assistant 2026.3+.

### Known limitations

- External state changes from the official app are not synchronized back to Home Assistant.
- Status notifications are not fully decoded.
- Hardware compatibility is currently confirmed only for a limited set of lamps.
