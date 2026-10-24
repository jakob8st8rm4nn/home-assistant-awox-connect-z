# Contributing

Thanks for helping improve AwoX Connect.Z for Home Assistant.

## Device compatibility reports

Compatibility reports are especially useful. Please include:

- exact product/model number,
- cloud model if visible in AwoX / EGLO HomeControl,
- firmware version,
- hardware version,
- Home Assistant version,
- Bluetooth path used (local adapter or ESPHome Bluetooth Proxy),
- which functions work: power, brightness, color, color temperature and native effects,
- relevant logs if something fails.

## Security and privacy

Do not include any of the following in issues, pull requests or screenshots:

- AwoX account password,
- Parse session token,
- mesh access token / mesh password,
- other account credentials.

## Development guidelines

- Keep the integration asynchronous.
- Avoid blocking network or BLE operations in the Home Assistant event loop.
- Preserve reconnect/retry behavior.
- Keep protocol command generation in `protocol.py`.
- Add or preserve protocol test vectors when changing frame generation.
- Keep all user-facing English strings in `translations/en.json`.
- Additional translations are welcome.

## Local software checks

Run `python tests/run_tests.py` from the repository root. Each suite uses an
isolated interpreter with lightweight Home Assistant/BLE stubs. These checks
cover protocol handling, command sequencing, confirmation, reload and availability
behavior; they do not replace hardware tests or a live Home Assistant check.

## Pull requests

Pull requests should:

1. describe the problem or feature,
2. explain how it was tested,
3. avoid unrelated formatting/code churn,
4. pass the HACS and Hassfest validation workflows.
