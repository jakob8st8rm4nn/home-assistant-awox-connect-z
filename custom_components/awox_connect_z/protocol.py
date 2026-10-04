"""Reverse-engineered AwoX Connect.Z light protocol."""

from __future__ import annotations

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .const import MAX_COLOR_TEMP_KELVIN, MIN_COLOR_TEMP_KELVIN


def fit16(value: bytes) -> bytes:
    """Pad an AwoX credential to one AES block."""
    if len(value) > 16:
        raise ValueError("AwoX credential exceeds 16 bytes")
    return value.ljust(16, b"\x00")


def aes_encrypt_reversed(key: bytes, value: bytes) -> bytes:
    """AwoX/Telink AES byte order."""
    if len(key) != 16 or len(value) != 16:
        raise ValueError("AES key and input must both be 16 bytes")
    encryptor = Cipher(algorithms.AES(key[::-1]), modes.ECB()).encryptor()
    return encryptor.update(value[::-1])[::-1]


def make_pair_packet(
    mesh_name: bytes, mesh_password: bytes, session_random: bytes
) -> bytes:
    """Build the 17-byte login packet written to UUID 1914."""
    mixed = bytes(
        a ^ b for a, b in zip(fit16(mesh_name), fit16(mesh_password), strict=True)
    )
    encrypted = aes_encrypt_reversed(session_random.ljust(16, b"\x00"), mixed)
    return b"\x0c" + session_random + encrypted[:8]


def make_session_key(
    mesh_name: bytes,
    mesh_password: bytes,
    session_random: bytes,
    response_random: bytes,
) -> bytes:
    """Build the per-connection session key after a successful 0x0D login."""
    mixed = bytes(
        a ^ b for a, b in zip(fit16(mesh_name), fit16(mesh_password), strict=True)
    )
    return aes_encrypt_reversed(mixed, session_random + response_random)


def encrypt_command(session_key: bytes, plain16: bytes) -> bytes:
    """Encrypt a 16-byte Connect.Z command and prepend the 0x00 prefix."""
    if len(plain16) != 16:
        raise ValueError("Connect.Z command must be exactly 16 bytes")
    return b"\x00" + aes_encrypt_reversed(session_key, plain16)


def crc8_awox(data: bytes) -> int:
    """CRC-8 used as byte 0 in Connect.Z command blocks."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x01:
                crc = ((crc >> 1) ^ 0x6C) & 0xFF
            else:
                crc = (crc >> 1) & 0xFF
    return crc


def finalize_block(block: bytearray) -> bytes:
    """Calculate byte 0 from the length marker in byte 1."""
    if len(block) != 16:
        raise ValueError("Connect.Z plaintext must be 16 bytes")
    length = block[1]
    end = 2 + length
    if end > 16:
        raise ValueError(f"Invalid Connect.Z length marker 0x{length:02X}")
    block[0] = crc8_awox(bytes(block[1:end]))
    return bytes(block)


def _transition_ds(seconds: float | int | None) -> int:
    """Convert HA seconds to Zigbee transition time in deciseconds."""
    if seconds is None:
        seconds = 0.2
    return max(0, min(0xFFFF, round(float(seconds) * 10.0)))


def _put_u16_le(block: bytearray, offset: int, value: int) -> None:
    block[offset] = value & 0xFF
    block[offset + 1] = (value >> 8) & 0xFF


def _put_destination(block: bytearray, mesh_id: int) -> None:
    """Write the confirmed 16-bit Connect.Z destination in big-endian order."""
    mesh_id = int(mesh_id)
    if not 1 <= mesh_id <= 0xFFFE:
        raise ValueError(
            f"Invalid Connect.Z mesh destination 0x{mesh_id & 0xFFFF:04X}"
        )
    block[2] = (mesh_id >> 8) & 0xFF
    block[3] = mesh_id & 0xFF


def make_power(on: bool, *, mesh_id: int) -> bytes:
    """Power command addressed to one Connect.Z mesh device."""
    block = bytearray.fromhex(
        "00 06 00 00 01 06 00 00 00 00 00 00 00 00 00 00"
    )
    _put_destination(block, mesh_id)
    block[7] = 1 if on else 0
    return finalize_block(block)


def make_brightness(
    level: int, transition: float = 0.2, *, mesh_id: int
) -> bytes:
    """Brightness 1..254, addressed to one device."""
    level = max(1, min(254, int(level)))
    block = bytearray.fromhex(
        "00 0c 00 00 01 08 00 04 00 02 00 00 00 00 00 00"
    )
    _put_destination(block, mesh_id)
    block[8] = level
    _put_u16_le(block, 9, _transition_ds(transition))
    return finalize_block(block)


def make_hs_color(
    hue_degrees: float,
    saturation_percent: float,
    transition: float = 0.2,
    *,
    mesh_id: int,
) -> bytes:
    """Hue/Saturation command addressed to one device."""
    hue = round((float(hue_degrees) % 360.0) * 254.0 / 360.0)
    saturation = round(
        max(0.0, min(100.0, float(saturation_percent))) * 254.0 / 100.0
    )

    block = bytearray.fromhex(
        "00 0a 00 00 01 00 03 06 00 00 02 00 00 00 00 00"
    )
    _put_destination(block, mesh_id)
    block[8] = max(0, min(254, hue))
    block[9] = max(0, min(254, saturation))
    _put_u16_le(block, 10, _transition_ds(transition))
    return finalize_block(block)


def make_color_temp_kelvin(
    kelvin: int, transition: float = 0.2, *, mesh_id: int
) -> bytes:
    """Color-temperature command addressed to one device."""
    kelvin = max(MIN_COLOR_TEMP_KELVIN, min(MAX_COLOR_TEMP_KELVIN, int(kelvin)))
    mired = round(1_000_000 / kelvin)
    mired = max(
        round(1_000_000 / MAX_COLOR_TEMP_KELVIN),
        min(round(1_000_000 / MIN_COLOR_TEMP_KELVIN), mired),
    )

    block = bytearray.fromhex(
        "00 0a 00 00 01 00 03 0a 00 00 02 00 00 00 00 00"
    )
    _put_destination(block, mesh_id)
    _put_u16_le(block, 8, mired)
    _put_u16_le(block, 10, _transition_ds(transition))
    return finalize_block(block)


def make_color_cycle_start(*, mesh_id: int) -> bytes:
    """Start the native lamp-side color cycle on one device."""
    block = bytearray.fromhex(
        "00 09 00 00 01 00 03 41 01 00 0c 00 00 00 00 00"
    )
    _put_destination(block, mesh_id)
    return finalize_block(block)


def make_color_cycle_stop(*, mesh_id: int) -> bytes:
    """Stop the native color cycle with the tested StopMoveStep variant."""
    # The tested per-lamp command uses length 0x06 and no payload. The AwoX app
    # capture also contained a longer 0x09 variant with three zero payload bytes;
    # keep the hardware-tested Python variant here deliberately.
    block = bytearray.fromhex(
        "00 06 00 00 01 00 03 47 00 00 00 00 00 00 00 00"
    )
    _put_destination(block, mesh_id)
    return finalize_block(block)


def make_candle_effect(enabled: bool, *, mesh_id: int) -> bytes:
    """Start or stop the native lamp-side candle effect on one device."""
    block = bytearray.fromhex(
        "00 08 00 00 01 08 00 10 00 01 00 00 00 00 00 00"
    )
    _put_destination(block, mesh_id)
    block[8] = 1 if enabled else 0
    return finalize_block(block)


def ha_brightness_to_device(brightness: int) -> int:
    """Map Home Assistant 1..255 to the device's 1..254."""
    return max(1, min(254, round(int(brightness) * 254 / 255)))


def protocol_self_test() -> None:
    """Guard the reverse-engineered protocol against accidental regressions."""
    # 0xD361 -> D3 61 and 0x8430 -> 84 30 were confirmed on real lamps.
    checks = (
        (
            make_power(True, mesh_id=0xD361),
            bytes.fromhex("36 06 d3 61 01 06 00 01 00 00 00 00 00 00 00 00"),
        ),
        (
            make_power(False, mesh_id=0xD361),
            bytes.fromhex("51 06 d3 61 01 06 00 00 00 00 00 00 00 00 00 00"),
        ),
        (
            make_power(True, mesh_id=0x8430),
            bytes.fromhex("6e 06 84 30 01 06 00 01 00 00 00 00 00 00 00 00"),
        ),
        (
            make_brightness(254, 0.2, mesh_id=0xD361),
            bytes.fromhex("11 0c d3 61 01 08 00 04 fe 02 00 00 00 00 00 00"),
        ),
        (
            make_hs_color(
                24 * 360 / 254,
                184 * 100 / 254,
                0.2,
                mesh_id=0xD361,
            ),
            bytes.fromhex("26 0a d3 61 01 00 03 06 18 b8 02 00 00 00 00 00"),
        ),
        (
            make_color_temp_kelvin(
                round(1_000_000 / 252),
                0.2,
                mesh_id=0xD361,
            ),
            bytes.fromhex("08 0a d3 61 01 00 03 0a fc 00 02 00 00 00 00 00"),
        ),
        (
            make_color_cycle_start(mesh_id=0xD361),
            bytes.fromhex("18 09 d3 61 01 00 03 41 01 00 0c 00 00 00 00 00"),
        ),
        (
            make_color_cycle_stop(mesh_id=0xD361),
            bytes.fromhex("24 06 d3 61 01 00 03 47 00 00 00 00 00 00 00 00"),
        ),
        (
            make_candle_effect(True, mesh_id=0xD361),
            bytes.fromhex("30 08 d3 61 01 08 00 10 01 01 00 00 00 00 00 00"),
        ),
        (
            make_candle_effect(False, mesh_id=0xD361),
            bytes.fromhex("43 08 d3 61 01 08 00 10 00 01 00 00 00 00 00 00"),
        ),
    )
    for actual, expected in checks:
        if actual != expected:
            raise RuntimeError(
                "AwoX protocol self-test failed: "
                f"{actual.hex(' ')} != {expected.hex(' ')}"
            )

    for invalid in (0, 0xFFFF):
        try:
            make_power(True, mesh_id=invalid)
        except ValueError:
            pass
        else:
            raise RuntimeError(
                f"AwoX protocol self-test accepted invalid mesh id {invalid}"
            )
