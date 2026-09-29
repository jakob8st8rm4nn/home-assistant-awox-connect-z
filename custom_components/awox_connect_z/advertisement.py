"""BLE advertisement state decoding for AwoX Connect.Z lamps."""

from __future__ import annotations

from dataclasses import dataclass

AWOX_COMPANY_ID = 0x0160
# Connect.Z manufacturer-data prefix used by the supported device profile.
# Bytes 2-5 contain the lower four MAC bytes in reverse order.
AWOX_CONNECT_Z_DATA_PREFIX = bytes((0x96, 0x20))


@dataclass(frozen=True, slots=True)
class AwoxAdvertisementState:
    """Decoded state from one long Connect.Z manufacturer advertisement."""

    mesh_id: int
    is_on: bool
    is_color_mode: bool
    brightness: int
    brightness_raw: int
    color_temp_mired: int | None
    color_temp_kelvin: int | None
    hue_degrees: float | None
    hue_raw: int | None
    saturation_percent: float | None
    saturation_raw: int | None
    mode_raw: int


def advertisement_matches_address(data: bytes, address: str) -> bool:
    """Return whether the advertisement embeds the sender's MAC suffix.

    Connect.Z advertisements encode the lower four bytes of the physical BLE
    address at data[2:6] in reverse order. The upper two address bytes are not
    present in this manufacturer payload.
    """
    compact = (
        address.strip()
        .replace(":", "")
        .replace("-", "")
        .replace(".", "")
    )
    if len(compact) != 12:
        return False
    try:
        mac = bytes.fromhex(compact)
    except ValueError:
        return False

    return (
        len(data) >= 6
        and data.startswith(AWOX_CONNECT_Z_DATA_PREFIX)
        and bytes(reversed(data[2:6])) == mac[-4:]
    )


def parse_awox_advertisement(data: bytes) -> AwoxAdvertisementState | None:
    """Decode company-id-stripped AwoX 0x0160 manufacturer data.

    Confirmed Connect.Z layout on EGLO-ZM-RGB-TW firmware 3.0.2::

        [0:1]   type (typically 96 20)
        [2:5]   lower MAC bytes, little-endian
        [6:7]   Connect.Z marker 03 00
        [9:10]  16-bit mesh id, little-endian
        [11]    mode bitfield: bit0=power, bit1=color mode
        [12]    brightness, 0x00..0xFE
        [13:14] white color temperature in mireds, little-endian
        [15]    hue
        [16]    saturation

    Short six-byte base advertisements do not contain state and are ignored.
    """
    if len(data) < 17:
        return None
    if data[6] != 0x03 or data[7] != 0x00:
        return None

    mesh_id = data[9] | (data[10] << 8)
    mode = data[11]
    is_on = bool(mode & 0x01)
    is_color_mode = bool(mode & 0x02)

    brightness_raw = data[12]
    # Connect.Z uses 0xFE as the confirmed full-scale brightness value.
    brightness = round(min(brightness_raw, 0xFE) * 255 / 0xFE)

    if is_color_mode:
        hue_raw = data[15]
        saturation_raw = data[16]
        # Connect.Z advertisements use an 8-bit hue wheel and 0xFE as the
        # confirmed full-saturation value. Preserve those scales for HA.
        hue_degrees = (hue_raw * 360.0 / 255.0) % 360.0
        saturation_percent = min(saturation_raw, 0xFE) * 100.0 / 0xFE
        color_temp_mired = None
        color_temp_kelvin = None
    else:
        hue_raw = None
        saturation_raw = None
        hue_degrees = None
        saturation_percent = None
        mired = data[13] | (data[14] << 8)
        if mired in (0, 0xFFFF):
            color_temp_mired = None
            color_temp_kelvin = None
        else:
            color_temp_mired = mired
            color_temp_kelvin = round(1_000_000 / mired)

    return AwoxAdvertisementState(
        mesh_id=mesh_id,
        is_on=is_on,
        is_color_mode=is_color_mode,
        brightness=brightness,
        brightness_raw=brightness_raw,
        color_temp_mired=color_temp_mired,
        color_temp_kelvin=color_temp_kelvin,
        hue_degrees=hue_degrees,
        hue_raw=hue_raw,
        saturation_percent=saturation_percent,
        saturation_raw=saturation_raw,
        mode_raw=mode,
    )


def advertisement_self_test() -> None:
    """Guard the hardware-confirmed advertisement layout against regressions."""
    # Known color-mode frame.
    red = bytes.fromhex(
        "96 20 19 75 41 38 03 00 02 61 D3 03 98 FF FF 00 FE 04 3E 60"
    )
    state = parse_awox_advertisement(red)
    if state is None:
        raise RuntimeError("AwoX advertisement self-test rejected Connect.Z frame")
    if not state.is_on or not state.is_color_mode or state.mesh_id != 0xD361:
        raise RuntimeError("AwoX advertisement self-test decoded red frame incorrectly")
    if state.brightness != 153 or state.hue_degrees != 0.0:
        raise RuntimeError("AwoX advertisement self-test decoded red values incorrectly")
    if round(state.saturation_percent or 0) != 100:
        raise RuntimeError("AwoX advertisement self-test decoded saturation incorrectly")

    # Known white-mode frame.
    white = bytes.fromhex(
        "96 20 19 75 41 38 03 00 02 61 D3 01 30 C4 00 FF FF 04 3E 60"
    )
    state = parse_awox_advertisement(white)
    if state is None or not state.is_on or state.is_color_mode:
        raise RuntimeError("AwoX advertisement self-test decoded white mode incorrectly")
    if state.mesh_id != 0xD361 or state.brightness != 48:
        raise RuntimeError("AwoX advertisement self-test decoded white values incorrectly")
    if state.color_temp_mired != 196 or state.color_temp_kelvin != 5102:
        raise RuntimeError("AwoX advertisement self-test decoded color temperature incorrectly")

    if parse_awox_advertisement(bytes.fromhex("96 20 19 75 41 38")) is not None:
        raise RuntimeError("AwoX advertisement self-test accepted short base advert")

    identity_frame = bytes.fromhex("96 20 19 75 41 38")
    if not advertisement_matches_address(
        identity_frame, "A4:C1:38:41:75:19"
    ):
        raise RuntimeError("AwoX advertisement self-test rejected matching MAC suffix")
    if advertisement_matches_address(
        identity_frame, "A4:C1:38:C6:68:B8"
    ):
        raise RuntimeError("AwoX advertisement self-test accepted wrong MAC suffix")
