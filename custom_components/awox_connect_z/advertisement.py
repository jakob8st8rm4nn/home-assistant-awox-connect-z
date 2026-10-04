"""BLE advertisement state decoding for AwoX Connect.Z lamps."""

from __future__ import annotations

from dataclasses import dataclass

from .const import EFFECT_CANDLE, EFFECT_COLOR_CYCLE

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
    if state.effect is not None or not state.effect_state_known:
        raise RuntimeError("AwoX advertisement self-test invented an effect")

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
    if state.effect is not None or not state.effect_state_known:
        raise RuntimeError("AwoX advertisement self-test invented a white-mode effect")

    # Hardware-confirmed native effect modes on the same lamp family.
    color_cycle = bytes.fromhex(
        "96 20 19 75 41 38 03 00 02 61 D3 07 FE FF FF 18 AE 04 3E 60"
    )
    state = parse_awox_advertisement(color_cycle)
    if (
        state is None
        or state.effect != EFFECT_COLOR_CYCLE
        or not state.effect_state_known
        or not state.is_on
    ):
        raise RuntimeError("AwoX advertisement self-test missed color-cycle mode")

    candle = bytes.fromhex(
        "96 20 19 75 41 38 03 00 02 61 D3 13 FE FF FF 65 AE 04 3E 60"
    )
    state = parse_awox_advertisement(candle)
    if (
        state is None
        or state.effect != EFFECT_CANDLE
        or not state.effect_state_known
        or not state.is_on
    ):
        raise RuntimeError("AwoX advertisement self-test missed candle mode")

    # Hardware-confirmed remembered-effect modes while the lamp is powered off.
    # A subsequent ordinary power-on starts the remembered effect; an explicit
    # power-off command clears it on the tested hardware.
    color_cycle_off = bytearray(color_cycle)
    color_cycle_off[11] = 0x06
    state = parse_awox_advertisement(bytes(color_cycle_off))
    if (
        state is None
        or state.effect != EFFECT_COLOR_CYCLE
        or not state.effect_state_known
        or state.is_on
    ):
        raise RuntimeError(
            "AwoX advertisement self-test missed off color-cycle preset mode"
        )

    candle_off = bytearray(candle)
    candle_off[11] = 0x12
    state = parse_awox_advertisement(bytes(candle_off))
    if (
        state is None
        or state.effect != EFFECT_CANDLE
        or not state.effect_state_known
        or state.is_on
    ):
        raise RuntimeError(
            "AwoX advertisement self-test missed off candle preset mode"
        )

    candle_white = bytes.fromhex(
        "96 20 19 75 41 38 03 00 02 61 D3 11 FE 99 00 FF FF 04 3E 60"
    )
    state = parse_awox_advertisement(candle_white)
    if (
        state is None
        or state.effect != EFFECT_CANDLE
        or not state.effect_state_known
        or not state.is_on
        or state.is_color_mode
        or state.color_temp_mired != 0x0099
    ):
        raise RuntimeError(
            "AwoX advertisement self-test missed white/CCT candle mode"
        )

    candle_white_off = bytes.fromhex(
        "96 20 19 75 41 38 03 00 02 61 D3 10 FE FA 00 FF FF 04 3E 60"
    )
    state = parse_awox_advertisement(candle_white_off)
    if (
        state is None
        or state.effect != EFFECT_CANDLE
        or not state.effect_state_known
        or state.is_on
        or state.is_color_mode
        or state.color_temp_mired != 0x00FA
    ):
        raise RuntimeError(
            "AwoX advertisement self-test missed off white/CCT candle preset mode"
        )

    contradictory = bytearray(color_cycle)
    contradictory[11] = 0x17
    state = parse_awox_advertisement(bytes(contradictory))
    if state is None or state.effect is not None or state.effect_state_known:
        raise RuntimeError(
            "AwoX advertisement self-test trusted an unconfirmed effect mode"
        )

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
