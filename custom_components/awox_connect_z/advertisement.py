"""BLE advertisement state decoding for AwoX Connect.Z lamps."""

from __future__ import annotations

from dataclasses import dataclass

from .const import (CANDLE_STATUS_MODES, COLOR_CYCLE_STATUS_MODES,
                    STATUS_EFFECTS)

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
        [11]    shared mode mapping (04/05 measured through 1911 only):
                00=off/white, 01=on/white,
                02=off/color, 03=on/static color,
                04=off/white with cycle bit, 05=on/white with cycle bit,
                06=off/color cycle preset, 07=on/color cycle,
                10=off/candle preset on white/CCT,
                11=on/candle on white/CCT,
                12=off/candle preset on color,
                13=on/candle on color
        [12]    brightness raw, 0x00..0xFF
        [13:14] white color temperature in mireds, little-endian
        [15]    hue
        [16]    saturation

    Modes 04/05 were measured through 1911, not advertisements; the same
    mapping is used here if received. Short six-byte advertisements have no state.
    """
    if len(data) < 17:
        return None
    if data[6] != 0x03 or data[7] != 0x00:
        return None

    mesh_id = data[9] | (data[10] << 8)
    mode = data[11]
    is_color_mode = bool(mode & 0x02)
    # Dynamic native-effect hues may contain FF. Do not reject their power,
    # brightness and effect fields; no fixed angle is derived from hue FF.
    if (is_color_mode and data[15] == 0xFF
            and mode not in (*COLOR_CYCLE_STATUS_MODES, *CANDLE_STATUS_MODES)):
        return None
    # Complete combinations observed on EGLO-ZM-RGB-TW firmware 3.0.2,
    # including 1911 transitions 04/05. Decode only these combinations instead of
    # treating arbitrary bit combinations as authoritative.  This matters
    # because the effect state also controls which stop command is selected.
    return lamp_state_from_fields(
        mesh_id=mesh_id, mode=mode, brightness=data[12],
        mired=data[13] | (data[14] << 8), hue=data[15], saturation=data[16],
    )


def lamp_state_from_fields(*, mesh_id: int, mode: int, brightness: int,
                           mired: int | None, hue: int | None,
                           saturation: int | None) -> AwoxAdvertisementState:
    """Normalize decoded fields; source-specific validation stays in the parsers."""
    is_on, is_color_mode = bool(mode & 1), bool(mode & 2)
    effect, effect_state_known = STATUS_EFFECTS.get(mode), mode in STATUS_EFFECTS
    brightness_raw = brightness
    if is_color_mode:
        hue_raw = hue
        saturation_raw = saturation
        # Use the same 254-position hue circle as control frames; received
        # hue 0xFE represents the wrap endpoint and maps to zero degrees.
        # Preserve received raw bytes.
        hue_degrees = (hue_raw * 360.0 / 254.0) % 360.0 if hue_raw is not None and hue_raw <= 254 else None
        saturation_percent = saturation_raw * 100.0 / 255.0 if saturation_raw is not None else None
        color_temp_mired = None
        color_temp_kelvin = None
    else:
        hue_raw = None
        saturation_raw = None
        hue_degrees = None
        saturation_percent = None
        if mired in (None, 0, 0xFFFF):
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
