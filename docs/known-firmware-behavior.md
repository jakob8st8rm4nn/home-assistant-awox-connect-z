# Known firmware behavior

Hardware testing with AwoX/EGLO Connect.Z lamps revealed the behavior below when commands were sent in quick succession **without an intentional delay**. Physical light output and the state reported in Bluetooth advertisements did not always match.

These observations apply to the tested hardware and firmware. Other models or firmware versions may behave differently. The table describes observed outcomes, not guaranteed behavior on every command.

## Observed behavior

| Command sequence or operation | Physical behavior | State reported in Bluetooth advertisements |
| --- | --- | --- |
| Power OFF → Brightness | The lamp may remain off. | The new brightness may already be reported despite the lamp remaining off. |
| Power ON → Brightness | The lamp changes brightness. | The previous brightness may still be reported. |
| Color / color temperature → Brightness | The lamp adopts the requested color or color temperature and brightness. | Brightness may remain at an older value. |
| Brightness change during Color Cycle | Brightness changes as requested. | The previous brightness may remain until the effect stops. |
| Brightness change during Candle | Brightness changes as requested. | The new brightness is reported correctly in the observed tests. |
| Brightness → Effect start | The lamp may settle at an unexpected intermediate brightness. | Candle reports that level while running; Color Cycle may report it correctly only after the effect stops. |

The Candle row records a working comparison case, rather than a reporting failure. Similar problems have also been observed with the official AwoX app, suggesting that lamp firmware contributes to the behavior. This does not establish the internal cause of each case.

## Color-to-effect transition from CCT mode

`Color → Effect` may fail when the lamp was previously in color-temperature (CCT) mode. The command-sequence specification deliberately retains the normal `Color/CCT + Start Effect` sequence without adding a special workaround for this firmware limitation. During physical execution, the command manager still applies the configured minimum spacing between writes; that spacing does not guarantee that this particular transition succeeds.

For CCT + Effect, the same normal `Color/CCT + Start Effect` rules apply. A request containing both Color and CCT is rejected by the conflict rule unless the higher-priority OFF normalization (explicit Power OFF or `Brightness = 0`) has already reduced the request to pure Power OFF; see [Command sequences](command-sequences.md).

## Impact on Home Assistant

The integration uses Bluetooth advertisements to update lamp state. Consequently, Home Assistant may display an outdated brightness value even when the physical light output has changed. Conversely, a reported brightness change does not necessarily mean the lamp has switched on.

An incorrect reported value may persist beyond the initial transition. A successful Bluetooth write alone does not confirm that the physical output and the advertised state agree.

## Command sequencing and spacing

The minimum command interval defaults to **250 ms**, with an allowed range of **0–2000 ms**. A value of **0 ms** disables intentional spacing.

The command sequences and configurable minimum interval between commands are described in [Command sequences](command-sequences.md). Spacing commands and selecting an appropriate sequence are intended to reduce the observed inconsistencies; they are not a guarantee for every hardware or firmware version.

This page records hardware observations. The command-sequence document describes how the integration handles requests; it does not claim that spacing eliminates every firmware issue.
