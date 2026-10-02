# Changelog

All notable changes to this project will be documented in this file.

## [1.8.0] - 2026-10-03

### Added

- Add six diagnostic sensors per lamp: signal strength, last seen, Bluetooth status, mesh ID, current Bluetooth source and last connection source, with German and English translations.
- Include Bluetooth diagnostic values in the downloadable integration diagnostics. Values use existing Bluetooth information without additional scans or lamp connections.

### Reliability

- Publish sensor values only when changed; sample small RSSI fluctuations at most every five seconds while reporting changes of at least 5 dBm immediately. Keep Last Seen stable until a newer advertisement timestamp is observed.
- Keep Bluetooth Status unknown during startup without confirmed Bluetooth evidence, then report unreachable when the availability grace period expires without liveness.
- Resolve the last connection source from connection tracking when available, with a best-effort fallback. Preserve the restored historical source until a new successful session is observed; leave an ambiguous new source unknown.
- Isolate diagnostic-listener errors from lamp command handling. Retain the existing command coalescing, connection handling and post-off settling behavior.

## [1.7.0] - 2026-10-02

### Added

- Add per-lamp latest-wins command coalescing: keep the latest waiting brightness and color/color-temperature independently, and discard older waiting changes when off is requested.
- Select the newest pending target immediately before its first GATT write, after command capacity and connection preparation. Already-started commands retain their bounded retry handling.
- Add a 500 ms settling interval after a successfully written off before sending another command that can turn the lamp on, addressing the rapid off-to-brightness behavior observed on tested hardware. Re-check the remaining interval at late selection; discarded unsent off requests do not start it.

### Reliability

- Remove cancelled callers' unsent targets without dropping newer requests. Settle outstanding calls on worker cancellation or failure, including cancellation before the worker first runs.
- Preserve new requests arriving during protected disconnect cleanup and start their successor only after the previous worker ends. Entity unload cancels pending work without restarting it.
- Retain the v1.6.1 runtime connection gate, timeout budgets, bounded cleanup, service-cache recovery and saved concurrency options.

### Documentation

- Explain coalescing, cancellation, optimistic completion and the targeted settling interval. Existing entries require only an update and restart; no reconfiguration is needed.

## [1.6.1] - 2026-10-01

### Fixed

- Serialize new runtime BLE connections across all loaded AwoX entries so a command waiting behind another AwoX connection attempt does not consume its own connection timeout inside the proxy. Already-connected lamps can still process commands in parallel.
- Separate the 30-second connection-gate queue timeout from the 30-second active command budget. Keep 12/4/4-second connect/authentication/write limits and protected disconnect cleanup with its own 12-second budget.
- Use direct runtime connection calls instead of the connector's internal retry loop, and fail immediately when no Bluetooth device entry exists. Setup and Reconfigure keep their existing connection path.
- Preserve BLE service-cache use and allow targeted recovery for recognizable missing/invalid GATT services or characteristics. Generic transport errors and authentication timeouts do not trigger cache recovery; disabling the cache option does not guarantee that all remote cache data is erased.
- Limit command writes to two attempts. A failed recovery connection ends the command; recovery connections use the shared connection gate as well.
- Retain client ownership and locks until bounded disconnect cleanup completes, including failed/pending connects and cancellation of the calling command.

### Changed

- Raise the default Max Concurrent Commands from 1 to 2. Explicitly saved values are retained; entries without a saved value use the new default.
- Explain connection coordination in the German and English options text.

### Documentation

- Shorten the README and upgrade guidance while retaining setup, settings and troubleshooting instructions.
- Document the known advertisement-history issue affecting HA 2026.9.4 and the upstream correction in habluetooth 7.0.0.
- Add conditional ESPHome `connection_timeout: 10s` guidance for long waits involving unreachable lamps, including its effects on other BLE devices.

## [1.6.0] - 2026-09-30

### Added

- Optional fully local setup using known mesh name/password, without a HomeControl cloud login or cloud dependency during setup.
- A 30-second discovery window, repeatable searches, device selection and manual MAC-address / 16-bit mesh-ID entry.
- Explicit active-scan requests when supported by Home Assistant, with normal advertisement collection as the compatibility fallback.
- Local authentication of selected lamps with a 20-second timeout per lamp and at most two simultaneous checks. Successful checks are reused within the setup dialog.
- Local Reconfigure for refreshing mesh credentials, adding lamps and correcting mesh IDs that conflict with observed advertisements.
- Clear no-results, per-device verification and recoverable active-scan error messages in German and English.

### Changed

- Existing cloud-imported entries remain supported without reconfiguration; normal command handling, availability and saved options are unchanged.
- Local meshes use stable entry identifiers independent of their editable credentials. Additional lamps with the same local credentials are added to the existing local entry.
- Cloud import and cloud Reconfigure filter devices already owned by another entry and request confirmation when lamps are skipped.
- Local setup checks device ownership again after authentication and closes matching Bluetooth discovery cards for newly added lamps.
- Fresh advertisement timestamps are required for automatic search results. Rescans preserve selection for retained devices and preserve manually assigned names.
- Known mesh-ID contradictions remain visible until corrected, even without a current long advertisement. Mesh credentials are validated against the protocol's 16-byte UTF-8 limit before connecting.
- Bluetooth discovery supports both cloud-imported and locally configured meshes and can rediscover devices after an initial attempt without a configured entry.

### Fixed

- Active-scan API failures are reported in the setup dialog while normal advertisement collection continues for the remaining search window; searches can be retried and cancellation still propagates.

## [1.5.0] - 2026-09-30

### Added

- Per-lamp availability based on Bluetooth liveness.
- A lamp becomes unavailable after the configured timeout (30 seconds by default) with neither a recent Bluetooth packet nor a working Home Assistant BLE connection, and automatically becomes available again when Bluetooth activity resumes.
- Short, long and byte-identical repeated advertisements all count as liveness; duplicate packets are recognized through Home Assistant's latest Bluetooth service-info timestamp.
- Successful Home Assistant GATT sessions count as liveness. The intentional idle disconnect of a known-good session starts a fresh availability window, while error cleanup does not create a false liveness signal.
- The availability timeout is configurable per account from 10 to 300 seconds, with a default of 30 seconds.

### Fixed

- Integration options are now saved before the automatic reload, so changes take effect without a second reload.
- Failed connection cleanup does not reset the availability timeout.

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
