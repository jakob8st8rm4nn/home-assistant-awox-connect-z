"""BLE advertisement state decoding for AwoX Connect.Z lamps."""

from __future__ import annotations

from dataclasses import dataclass

from .const import AWOX_COMPANY_ID, EFFECT_CANDLE, EFFECT_COLOR_CYCLE

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
    effect: str | None
    effect_state_known: bool
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
        [11]    hardware-confirmed mode values:
                00=off/white, 01=on/white,
                02=off/color, 03=on/static color,
                06=off/color cycle preset, 07=on/color cycle,
                10=off/candle preset on white/CCT,
                11=on/candle on white/CCT,
                12=off/candle preset on color,
                13=on/candle on color
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
    # Hardware-confirmed complete mode values on EGLO-ZM-RGB-TW firmware
    # 3.0.2.  Decode effects only from the confirmed combinations instead of
    # treating arbitrary bit combinations as authoritative.  This matters
    # because the effect state also controls which stop command is selected.
    if mode in (0x06, 0x07):
        # 0x06 is the same native color-cycle state while power is off.
        # The lamp remembers the effect and starts it on the next power-on.
        effect = EFFECT_COLOR_CYCLE
        effect_state_known = True
    elif mode in (0x10, 0x11, 0x12, 0x13):
        # Candle preserves the underlying white/CCT or HS appearance. 0x10/0x12
        # are the corresponding remembered-effect states while power is off.
        effect = EFFECT_CANDLE
        effect_state_known = True
    elif mode in (0x00, 0x01, 0x02, 0x03):
        effect = None
        effect_state_known = True
    else:
        effect = None
        effect_state_known = False

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
        effect=effect,
        effect_state_known=effect_state_known,
        mode_raw=mode,
    )
