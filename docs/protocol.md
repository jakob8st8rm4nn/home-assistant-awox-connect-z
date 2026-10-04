# Connect.Z protocol reference

This reference describes the behavior implemented in v1.10.0 and reported in
hardware testing with EGLO `900024/12253`, model `EGLO-ZM-RGB-TW`, firmware
`3.0.2`, hardware `4.62`. It is not a specification for every AwoX device.

## Advertisement modes

The mode is byte 11 of the manufacturer payload after removal of the company ID
(`0x0160`). Values below are hexadecimal. Power and the underlying color mode
are separate from the native effect: an off lamp can remember an effect.

| Mode | Power | Underlying appearance | Effect |
|---|---|---|---|
| `0x00` | Off | White / color temperature | None |
| `0x01` | On | White / color temperature | None |
| `0x02` | Off | Hue / saturation | None |
| `0x03` | On | Hue / saturation | None |
| `0x06` | Off | Hue / saturation | Color cycle remembered |
| `0x07` | On | Hue / saturation | Color cycle active |
| `0x10` | Off | White / color temperature | Candle remembered |
| `0x11` | On | White / color temperature | Candle active |
| `0x12` | Off | Hue / saturation | Candle remembered |
| `0x13` | On | Hue / saturation | Candle active |

Effect decoding uses these complete mode values, not arbitrary combinations of
effect bits. Other values leave effect identity uncertain; power and color mode
are still decoded from bits 0 and 1. Candle advertisements update the underlying
color or temperature as well as the effect state.

### State payload layout

Offsets are zero-based within the company-ID-stripped manufacturer data.

| Bytes | Meaning |
|---|---|
| 0–1 | Supported payload prefix: `96 20` |
| 2–5 | Lower four MAC bytes, reversed |
| 6–7 | Connect.Z marker: `03 00` |
| 9–10 | Mesh ID, little-endian |
| 11 | Mode, as listed above |
| 12 | Brightness; `0xFE` is full scale |
| 13–14 | White color temperature in mireds, little-endian |
| 15 | Hue, scaled over `0x00`–`0xFF` |
| 16 | Saturation; `0xFE` is full scale |

The current decoder requires at least 17 bytes. Short discovery packets do not
provide this complete state. White temperatures `0x0000` and `0xFFFF` are treated
as unavailable. State is applied only when the advertised mesh ID matches the
configured destination; a mismatch is diagnostic and does not rewrite it.

## Native effect commands

All payload bytes are hexadecimal. Commands target the configured individual
mesh ID; normal effect control does not use mesh broadcast.

| Action | Cluster | Command | Payload | Frame length byte |
|---|---|---|---|---|
| Start Color cycle | `0x0300` | `0x41` | `01 00 0C` | `0x09` |
| Stop Color cycle | `0x0300` | `0x47` | None | `0x06` |
| Start Candle | `0x0008` | `0x10` | `01 01` | `0x08` |
| Stop Candle | `0x0008` | `0x10` | `00 01` | `0x08` |

Color cycle uses EnhancedMoveHue with mode 1 and rate 3072, encoded little-endian.
Its stop uses the tested short StopMoveStep frame. The reported app capture also
contained a length-`0x09` stop with three zero payload bytes; that is not the
variant emitted by this integration. Candle uses the device-specific Level
Control command above. The two stop commands are not interchangeable.

### Command frame layout

| Bytes | Meaning |
|---|---|
| 0 | CRC8 over bytes 1 through `1 + length`, inclusive |
| 1 | Length: six header bytes plus payload length |
| 2–3 | Destination mesh ID, **big-endian** |
| 4 | Endpoint `0x01` |
| 5–6 | Cluster, **little-endian** |
| 7 | Command |
| 8 onward | Payload, followed by zero padding to 16 bytes |

The existing authenticated transport encrypts the 16-byte frame before writing
it. CRCs are calculated per frame and destination, not copied from examples.

## Effect lifecycle and command ordering

| Request or event | Handling |
|---|---|
| Effect requested while off | Prepare the effect first, then turn on. A queued brightness command can supply the turn-on step. |
| Effect prepared while off | The effect command alone does not turn on the LEDs; ordinary power-on resumes the remembered effect. |
| Switch effect or select static color/temperature | Stop the previous effect with its matching command, then re-evaluate the newest target. |
| Power off | Send power-off directly. On the tested hardware it clears the effect; update the optimistic state only after a successful write. |
| Rapid off → on | Retain the existing 500 ms guard after a successfully written off. |
| Effect state unknown | Track possible effects and stop conflicting possibilities as needed before completing a target. |
| Ambiguous effect-changing write failure | Preserve uncertainty even if an advertisement arrived between retries. A later advertisement can confirm the state. |

Color, color temperature and effects share one latest-wins target; brightness
remains independent. Each next packet is selected when the connection is ready.
A superseding request can therefore replace a target between preparation and
power-on. Already-started writes retain the existing bounded retry handling.

The effects run inside the lamp: there is no Home Assistant animation loop or
continuous stream of color commands. Effect state is optimistic after successful
writes and corrected by supported advertisements. During uncertainty the displayed
effect may retain its previous value (initially off); `effect_state_known` reports
whether the integration currently considers it known.

Implementation: [advertisement decoder](../custom_components/awox_connect_z/advertisement.py),
[frame builders](../custom_components/awox_connect_z/protocol.py),
[light command handling](../custom_components/awox_connect_z/light.py).
