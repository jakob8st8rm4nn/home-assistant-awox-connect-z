# Known firmware behavior

Observed on the tested AwoX/EGLO Connect.Z lamps. These outcomes are not guarantees
for every command or firmware version. Reported state and physical light output
can disagree; a matching advertisement or 1911 reply does not prove visible light.

## Observed behavior

The following cases were investigated with commands in quick succession,
**without intentional spacing**:

| Command sequence or operation | Physical behavior | State reported in advertisements |
|---|---|---|
| Power OFF → Brightness | The lamp may remain off. | New brightness may already be reported. |
| Power ON → Brightness | Brightness changes. | Previous brightness may remain reported. |
| Color / CCT → Brightness | Requested appearance and brightness are applied. | Brightness may remain at an older value. |
| Brightness change during Color Cycle | Brightness changes. | Previous brightness may remain until the effect stops. |
| Brightness change during Candle | Brightness changes. | New brightness was reported correctly in the observed tests. |
| Brightness → Effect start | The lamp may settle at an unexpected intermediate brightness. | Candle reports that level; Color Cycle may report it correctly only after stopping. |

The Candle comparison is not a reporting failure. Similar inconsistencies were
observed with the official app, suggesting firmware involvement without proving
each internal cause. The integration uses mandatory power completion after
brightness and actual effect starts; see [Power finalization](command-sequences.md#power-finalization).

## Color-to-effect transition from CCT mode

A `Color → Effect` sequence may fail when the lamp was previously in CCT mode.
The integration sends appearance before effect start and requires a power
completion. Neither this sequence nor configured command spacing guarantees that
every transition succeeds. Combined-target rules are defined in
[Command sequences](command-sequences.md).

## Native fades followed by effects

An effect started before a preceding Color/CCT/Brightness fade finishes can
interrupt the transition or retain an intermediate value. This was investigated
with CCT 6500 K followed by Candle at a 0.2 s fade and with longer fades.

The command pipeline adds `1.25 × encoded transition time` to the configured
control interval after these three command types. Targeted direct-connection
tests of CCT-to-Candle at 0.2, 1, 3, 10 and 30 s confirmed the requested CCT and
effect on both lamps (10/10); this limited sample does not establish complete
prevention or physical light output. Required power completion remains in place.
See [Command spacing](command-sequences.md#command-spacing).

## Candle: physical light turns off while status still reports ON

A previously lit lamp could turn physically off after rapidly consecutive
color/CCT and Candle-start commands, while both advertisements and 1911 still
reported Power ON.

- A **250 ms** command interval substantially reduced occurrences but did not
  demonstrably eliminate them.
- An additional `POWER ON` immediately next in command order after Candle start
  substantially improved agreement with physical output. Normal command spacing
  still applies; complete prevention has not been established.

The internal trigger is unknown. This physical-off observation concerns Candle.
Using the same mandatory power completion for Color Cycle is a conservative
sequencing rule, not evidence of the identical physical failure for Color Cycle.
The finalizer's handling of newer OFF requests and effect stops is specified in
[Power finalization](command-sequences.md#power-finalization).

## Hue boundary reporting

| Situation | Observed report |
|---|---|
| Static hue `0xFE` or `0xFF` sent | Hue `0x00` in 1911 and advertisements |
| Hue `0xFD` / saturation `0xFF` after deliberately sent hue `0xFE`/`0xFF` | Hue `0xFE`, including after a new connection; neutral-start controls returned `0xFD` |
| Raw hue 252 after the normal red/zero endpoint | Raw hue 253 in a sequential conversion comparison |
| Running Color Cycle, mode `0x07` | Dynamically generated hue `0xFF` on both lamps, without a hue-`0xFF` control during the effect |

These observations show history-dependent and effect-specific behavior, not a
colorimetric calibration. A symmetric 254/255 conversion comparison found no
reported-accuracy benefit from changing the existing static 254-position scale.
Avoiding sent `0xFE`/`0xFF` does not eliminate every one-step boundary shift.

The integration preserves received raw values, emits only hue 0–253 and adds no
special recovery command or delay for these cases. See
[Value ranges](protocol.md#brightness-hue-and-saturation-ranges) for conversion
and accepted-field rules. White-mode `0xFF` color bytes are ordinary placeholders.

## Color Cycle to CCT intermediate status

Both lamps reported these 1911 sequences during a CCT command:

| Power | Before | Intermediate | Final |
|---|---|---|---|
| OFF | `0x06` — Color Cycle, HS base | `0x04` — cycle bit, CCT base | `0x00` — no effect, CCT |
| ON | `0x07` — Color Cycle, HS base | `0x05` — cycle bit, CCT base | `0x01` — no effect, CCT |

The final replies arrived automatically before the first manual poll. The
intermediates were reproduced with and without intentional command spacing;
they were not observed in the opposite CCT → Color Cycle direction or in
advertisements. This does not exclude other behavior on other firmware.

The integration retains the reported effect identity instead of treating the
intermediate as a completed stop; see
[Native-effect replies](command-sequences.md#native-effect-replies-and-displayed-appearance).

## Impact on Home Assistant

Reported brightness can remain outdated after a transition, and reported ON does
not prove physical light output. A successful group-confirmation reply may update
the native-effect color/CCT display; other 1911 replies do not synchronize HA.
Effect advertisements preserve the displayed appearance. See
[the display rules](command-sequences.md#native-effect-replies-and-displayed-appearance).

## Command sequencing and spacing

Request normalization, power completion, confirmation and configurable spacing
are defined in [Command sequences](command-sequences.md). These measures reduce
observed inconsistencies but do not guarantee correct physical output.
