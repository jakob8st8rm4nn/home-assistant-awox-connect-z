"""Reverse-engineered AwoX Connect.Z light protocol."""

from __future__ import annotations

from typing import Literal

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .const import (
    DEFAULT_TRANSITION,
    STATUS_EFFECTS,
    MAX_COLOR_TEMP_KELVIN,
    MAX_MESH_ID,
    MESH_CREDENTIAL_MAX_BYTES,
    MIN_COLOR_TEMP_KELVIN,
    MIN_MESH_ID,
)


_BRIGHTNESS_TRANSITION_OFFSET = 9
_APPEARANCE_TRANSITION_OFFSET = 10


def fit16(value: bytes) -> bytes:
    """Pad an AwoX credential to one AES block."""
    if len(value) > MESH_CREDENTIAL_MAX_BYTES:
        raise ValueError(
            f"AwoX credential exceeds {MESH_CREDENTIAL_MAX_BYTES} bytes"
        )
    return value.ljust(16, b"\x00")


def is_valid_device_mesh_id(mesh_id: int) -> bool:
    """Return whether an integer is a valid individual lamp mesh address."""
    return MIN_MESH_ID <= mesh_id <= MAX_MESH_ID


def aes_encrypt_reversed(key: bytes, value: bytes) -> bytes:
    """AwoX/Telink AES byte order."""
    if len(key) != 16 or len(value) != 16:
        raise ValueError("AES key and input must both be 16 bytes")
    encryptor = Cipher(algorithms.AES(key[::-1]), modes.ECB()).encryptor()
    return encryptor.update(value[::-1])[::-1]


def _mesh_credential_xor(mesh_name: bytes, mesh_password: bytes) -> bytes:
    """Combine padded mesh credentials for login and session-key derivation."""
    return bytes(
        a ^ b for a, b in zip(fit16(mesh_name), fit16(mesh_password), strict=True)
    )


def make_pair_packet(
    mesh_name: bytes, mesh_password: bytes, session_random: bytes
) -> bytes:
    """Build the 17-byte login packet written to UUID 1914."""
    mixed = _mesh_credential_xor(mesh_name, mesh_password)
    encrypted = aes_encrypt_reversed(session_random.ljust(16, b"\x00"), mixed)
    return b"\x0c" + session_random + encrypted[:8]


def make_session_key(
    mesh_name: bytes,
    mesh_password: bytes,
    session_random: bytes,
    response_random: bytes,
) -> bytes:
    """Build the per-connection session key after a successful 0x0D login."""
    mixed = _mesh_credential_xor(mesh_name, mesh_password)
    return aes_encrypt_reversed(mixed, session_random + response_random)


def encrypt_command(session_key: bytes, plain16: bytes) -> bytes:
    """Encrypt a 16-byte Connect.Z command and prepend the 0x00 prefix."""
    if len(plain16) != 16:
        raise ValueError("Connect.Z command must be exactly 16 bytes")
    return b"\x00" + aes_encrypt_reversed(session_key, plain16)


def crc8_awox(data: bytes | bytearray) -> int:
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
    block[0] = crc8_awox(block[1:end])
    return bytes(block)


def _transition_ds(seconds: float | int | None) -> int:
    """Convert HA seconds to Zigbee transition time in deciseconds."""
    if seconds is None:
        seconds = DEFAULT_TRANSITION
    return max(0, min(0xFFFF, round(float(seconds) * 10.0)))


def encoded_transition_seconds(
    frame: bytes, *, layout: Literal["brightness", "appearance"],
) -> float:
    """Read the encoded fade duration; appearance covers HS color and CCT."""
    offset = _BRIGHTNESS_TRANSITION_OFFSET if layout == "brightness" else _APPEARANCE_TRANSITION_OFFSET
    return int.from_bytes(frame[offset:offset + 2], "little") / 10


def _put_u16_le(block: bytearray, offset: int, value: int) -> None:
    block[offset] = value & 0xFF
    block[offset + 1] = (value >> 8) & 0xFF


def _put_destination(block: bytearray, mesh_id: int) -> None:
    """Write the confirmed 16-bit Connect.Z destination in big-endian order."""
    mesh_id = int(mesh_id)
    if not is_valid_device_mesh_id(mesh_id):
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
    level: int, transition: float = DEFAULT_TRANSITION, *, mesh_id: int
) -> bytes:
    """Brightness 1..255, addressed to one device."""
    level = ha_brightness_to_device(level)
    block = bytearray.fromhex(
        "00 0c 00 00 01 08 00 04 00 02 00 00 00 00 00 00"
    )
    _put_destination(block, mesh_id)
    block[8] = level
    _put_u16_le(block, _BRIGHTNESS_TRANSITION_OFFSET, _transition_ds(transition))
    return finalize_block(block)


def make_hs_color(
    hue_degrees: float,
    saturation_percent: float,
    transition: float = DEFAULT_TRANSITION,
    *,
    mesh_id: int,
) -> bytes:
    """Hue/Saturation command addressed to one device."""
    hue = round((float(hue_degrees) % 360.0) * 254.0 / 360.0) % 254
    saturation = round(
        max(0.0, min(100.0, float(saturation_percent))) * 255.0 / 100.0
    )

    block = bytearray.fromhex(
        "00 0a 00 00 01 00 03 06 00 00 02 00 00 00 00 00"
    )
    _put_destination(block, mesh_id)
    block[8] = hue
    block[9] = saturation
    _put_u16_le(block, _APPEARANCE_TRANSITION_OFFSET, _transition_ds(transition))
    return finalize_block(block)


def make_color_temp_kelvin(
    kelvin: int, transition: float = DEFAULT_TRANSITION, *, mesh_id: int
) -> bytes:
    """Color-temperature command addressed to one device."""
    kelvin = max(MIN_COLOR_TEMP_KELVIN, min(MAX_COLOR_TEMP_KELVIN, int(kelvin)))
    mired = round(1_000_000 / kelvin)

    block = bytearray.fromhex(
        "00 0a 00 00 01 00 03 0a 00 00 02 00 00 00 00 00"
    )
    _put_destination(block, mesh_id)
    _put_u16_le(block, 8, mired)
    _put_u16_le(block, _APPEARANCE_TRANSITION_OFFSET, _transition_ds(transition))
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
    """Preserve Home Assistant brightness 1..255; zero is planned as POWER OFF."""
    return max(1, min(255, int(brightness)))


def aes_decrypt_reversed(key: bytes, value: bytes) -> bytes:
    """Inverse of the authenticated transport's reversed AES block."""
    if len(key) != 16 or len(value) != 16:
        raise ValueError("AES key and input must both be 16 bytes")
    decryptor = Cipher(algorithms.AES(key[::-1]), modes.ECB()).decryptor()
    return decryptor.update(value[::-1])[::-1]


def make_status_query(session_key: bytes, mesh_id: int) -> bytes:
    """1911-directed query: 02 + AES(CRC,03,0A,MID-low,MID-high,zeroes)."""
    if not is_valid_device_mesh_id(mesh_id):
        raise ValueError("Invalid individual status target")
    body = bytes((3, 10, mesh_id & 255, mesh_id >> 8))
    block = (bytes((crc8_awox(body),)) + body).ljust(16, bytes(1))
    return bytes((2,)) + aes_encrypt_reversed(session_key, block)


def parse_status_1911(session_key: bytes, payload: bytes) -> dict:
    """Parse the confirmed 20-byte status envelope; no invented reply CRC.

    CRC validation is established for outbound queries, not for these replies.
    Header/footer, decrypted marker and supported mode are validated here;
    identity and session freshness are validated by Status1911Store.
    """
    if len(payload) != 20 or payload[0] not in (0x80, 0x82) or payload[19] != 0x60:
        raise ValueError("Unsupported 1911 envelope")
    block = aes_decrypt_reversed(session_key, payload[1:17])
    if block[5:7] != b"\x96\x20" or block[12] not in STATUS_EFFECTS:
        raise ValueError("Unsupported 1911 state")
    # Color Cycle can report FF as a dynamic hue. Preserve native-effect
    # packets (including Candle) without treating FF as a static color angle.
    # White-mode color bytes remain placeholders; static HS still rejects FF.
    if (block[12] & 0x02 and payload[17] == 0xFF
            and STATUS_EFFECTS.get(block[12]) is None):
        raise ValueError("Unsupported 1911 hue FF in static color mode")
    mesh_id = int.from_bytes(block[:2], "little")
    if not is_valid_device_mesh_id(mesh_id):
        raise ValueError("Invalid 1911 field")
    return dict(outer_type=payload[0], mesh_id=mesh_id, mac_suffix=block[7:11], mode=block[12],
                brightness=block[13], mired=int.from_bytes(block[14:16], "little"),
                hue=payload[17], saturation=payload[18])
