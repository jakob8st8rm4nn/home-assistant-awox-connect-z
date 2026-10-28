# Connect.Z protocol reference

Wire formats and value conversions used by this integration. Hardware observations
refer to EGLO `900024/12253`, model `EGLO-ZM-RGB-TW`, firmware `3.0.2`, hardware
`4.62`; other firmware may differ. For observed limitations, see
[Known firmware behavior](known-firmware-behavior.md). Execution rules are defined
in [Command sequences](command-sequences.md).

## BLE transport

Characteristic UUIDs share the prefix `00010203-0405-0607-0809-0a0b0c0d`.

| Suffix | Purpose |
|---|---|
| `1914` | Authentication and session-key establishment |
| `1912` | Encrypted lamp commands and directed status queries |
| `1911` | Status notifications and unencrypted `0x01` activation |

### Command frame layout

Ordinary controls use prefix `0x00` followed by an encrypted 16-byte block.
The plaintext layout is:

| Bytes | Meaning |
|---|---|
| 0 | CRC8 over bytes 1 through `1 + length`, inclusive |
| 1 | Length: six header bytes plus payload length |
| 2–3 | Individual destination mesh ID, **big-endian** |
| 4 | Endpoint `0x01` |
| 5–6 | Cluster, **little-endian** |
| 7 | Command |
| 8 onward | Payload, followed by zero padding to 16 bytes |

Encryption uses the current session key established during authentication and
AwoX/Telink reversed AES byte order. CRCs are calculated for each frame and destination.

## Transition encoding

Color, CCT and Brightness encode transition duration as a little-endian 16-bit
decisecond value: `clamp(round(seconds * 10), 0, 65535)`. The configured default
is 0.2 s, with a settings range of 0–30 s. An explicit HA action transition
overrides the default; the settings range does not clamp explicit action values.
Fade-aware spacing and confirmation timing are defined in
[Command sequences](command-sequences.md#command-spacing).

## Native effect commands

The exact Home Assistant effect values are `Color cycle` and `Candle`.

Payload bytes are hexadecimal. Normal effect controls address the configured
individual lamp, not the mesh broadcast address.

| Action | Cluster | Command | Payload | Frame length byte |
|---|---|---|---|---|
| Start Color Cycle | `0x0300` | `0x41` | `01 00 0C` | `0x09` |
| Stop Color Cycle | `0x0300` | `0x47` | None | `0x06` |
| Start Candle | `0x0008` | `0x10` | `01 01` | `0x08` |
| Stop Candle | `0x0008` | `0x10` | `00 01` | `0x08` |

Color Cycle uses EnhancedMoveHue with mode 1 and rate 3072, little-endian.
The integration emits the tested short StopMoveStep frame; the two effects'
stop commands are not interchangeable. Both effects run inside the lamp.

## 1911 direct-lamp status

After successful authentication, subscribe to 1911 and write the unencrypted byte
`0x01` there once per session. Reuse this setup while the session remains active.

A directed query on 1912 uses prefix `0x02` and an encrypted 16-byte block:

```text
[CRC8(03 0A MID-low MID-high), 03, 0A, MID-low, MID-high, 00 ...]
```

The query destination is **little-endian**, unlike ordinary control destinations.

### Notification layout

| Outer bytes | Meaning |
|---|---|
| 0 | `0x80` direct status or `0x82` mesh status |
| 1–16 | Encrypted status block |
| 17 | Unencrypted raw hue |
| 18 | Unencrypted raw saturation |
| 19 | Terminator `0x60` |

The decrypted block contains:

| Bytes | Meaning |
|---|---|
| 0–1 | Mesh ID, little-endian |
| 5–6 | Marker `96 20` |
| 7–10 | Lower four MAC bytes, reversed |
| 12 | [Mode](#status-and-advertisement-modes) |
| 13 | Raw brightness |
| 14–15 | CCT in mireds, little-endian |

Validation checks the envelope, marker, supported mode and color-field policy.
State acceptance also requires the configured mesh ID, MAC suffix and current
session. The MAC suffix is a consistency check, not separate authentication.
Only direct type `0x80` may establish initial state or confirm commands in 1.12.1.
All accepted replies are stored, but the entity applies only the selected successful
group-confirmation result or explicit UNKNOWN-bootstrap result. Completed groups
update the planner internally; their display result is published only when no
accepted follow-up requests remain open. Individual
notifications do not synchronize the displayed state or planner. Decodable `0x82`
replies are counted and ignored. The direct/mesh
mapping was observed with role-swapped lamps, not established for every firmware.

Hue/saturation come from outer bytes 17/18; they are not derived from CCT.
HS mode ignores the CCT field. CCT values `0x0000` and `0xFFFF` are unavailable.
Outgoing queries have a known CRC; no reply CRC is assumed. Confirmation timing
and same-session correlation limits are defined in
[1911 group confirmation](command-sequences.md#1911-group-confirmation).

### ESPHome notification compatibility

The tested lamps expose 1911 at value handle 18 with a `0x2901` descriptor at
handle 19, but no `0x2902` CCCD. For a notify-capable 1911 on ESPHome without a
CCCD, the client uses the ESPHome GATT subscription API and registers its cleanup
pair in the backend, then writes `0x01`. It does not invent or write a descriptor.
Other backends and characteristics with a CCCD use standard Bleak setup.

This compatibility path accesses private backend members `_notify_cancels`,
`_client` and `_address_as_int` through the Bleak backend. Their shape was checked
in Bleak-ESPHome 4.0.0 and tested in 4.1.0; backend updates require revalidation.
Diagnostics expose `status_notify_method`.

## Advertisement state layout

Offsets are within manufacturer data after removal of company ID `0x0160`.

| Bytes | Meaning |
|---|---|
| 0–1 | Supported prefix `96 20` |
| 2–5 | Lower four MAC bytes, reversed |
| 6–7 | Marker `03 00` |
| 9–10 | Mesh ID, little-endian |
| 11 | [Mode](#status-and-advertisement-modes) |
| 12 | Raw brightness |
| 13–14 | CCT in mireds, little-endian |
| 15 | Raw hue |
| 16 | Raw saturation |

The decoder requires at least 17 bytes. Short discovery packets contain no
complete state. Setup/discovery validates the embedded MAC suffix against the
sender. Runtime advertisement handling is address-scoped and requires the mesh ID
to match the configured destination; a mismatch does not rewrite that ID.
CCT values `0x0000` and `0xFFFF` are unavailable. Hue and saturation are used only
in color mode, subject to the value policy below.

## Status and advertisement modes

<a id="advertisement-modes"></a>

All mode values are hexadecimal. Observed bits are `0x01` Power ON, `0x02` HS
rather than CCT, `0x04` Color Cycle and `0x10` Candle. Effects are identified only
from these supported **complete combinations**:

| Mode | Power | Underlying appearance | Effect |
|---|---|---|---|
| `0x00` | Off | White / CCT | None |
| `0x01` | On | White / CCT | None |
| `0x02` | Off | Hue / saturation | None |
| `0x03` | On | Hue / saturation | None |
| `0x04` | Off | White / CCT | Color-Cycle bit retained during transition |
| `0x05` | On | White / CCT | Color-Cycle bit retained during transition |
| `0x06` | Off | Hue / saturation | Color Cycle remembered |
| `0x07` | On | Hue / saturation | Color Cycle active |
| `0x10` | Off | White / CCT | Candle remembered |
| `0x11` | On | White / CCT | Candle active |
| `0x12` | Off | Hue / saturation | Candle remembered |
| `0x13` | On | Hue / saturation | Candle active |

Modes `0x04`/`0x05` were observed through 1911, not advertisements; the same mapping
is used for either source. See [intermediate status](known-firmware-behavior.md#color-cycle-to-cct-intermediate-status).
Unknown combinations are rejected by 1911. Otherwise valid advertisements still
decode power and color-mode bits but mark effect identity unknown. Bits `0x08`, `0x20`, `0x40` and
`0x80` have no established meaning. OFF with a remembered effect is handled by
[stored-effect recovery](command-sequences.md#stored-effect-recovery).

### Brightness, hue and saturation ranges

| Field | Sending | Receiving / conversion |
|---|---|---|
| Brightness | HA 1–255 maps directly to raw 1–255; HA 0 requests POWER OFF | Raw 0–255 is preserved |
| Hue | `round((degrees % 360) * 254 / 360) % 254`; sends 0–253 | Raw 0–254: `(raw * 360 / 254) % 360`; raw `0xFE` maps to 0° |
| Saturation | `round(percent * 255 / 100)` | Raw 0–255: `raw * 100 / 255` |
| Color temperature | Supported HA range: **2700–6500 K** | Raw mireds; `0x0000`/`0xFFFF` are unavailable |

Exceptions and interpretation:

- White-mode color bytes are placeholders, not an HS color.
- Hue `0xFF` is rejected in static HS status. Known native-effect packets retain
  it as raw data, without a normalized color angle. This policy also applies to
  Candle; the hardware observation of generated hue `0xFF` concerns Color Cycle.
- Received hue `0xFE` is accepted even though it is not sent. Numeric confirmation
  uses one raw step of tolerance, circular hue distance and `0xFE`/`0x00`
  equivalence. An effect hue `0xFF` cannot confirm a fixed hue goal.
- Brightness and saturation do not use hue wraparound. Raw brightness 0 does not
  reliably switch the physical lamp off; the HA-zero target uses POWER OFF.

For measured normalization and history-dependent reports, see
[Hue boundary reporting](known-firmware-behavior.md#hue-boundary-reporting).
The shared [effect display and confirmation rules](command-sequences.md#native-effect-replies-and-displayed-appearance)
define when reported appearance is applied to HA and how unavailable hue values
are handled.

Implementation: [frame builders](../custom_components/awox_connect_z/protocol.py),
[1911 state](../custom_components/awox_connect_z/status_1911.py),
[advertisement decoder](../custom_components/awox_connect_z/advertisement.py).

### Lamp-control write mode

Lamp controls on 1912 use BLE writes with `response=False`, both for the first attempt and confirmation repeats. This transport choice does not confirm the lamp state; group completion still requires a matching 1911 reply. Authentication on 1914 retains its existing write mode.
