# Changelog

All notable changes to this project will be documented in this file.

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
