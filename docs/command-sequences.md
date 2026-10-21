# Command sequences

This specification defines the target behavior of the AwoX/EGLO Connect.Z command pipeline. Commands are symbolic; protocol frames are documented in the [protocol reference](protocol.md).

## Terms

- **UNKNOWN:** The lamp state needed to select a command sequence is not yet known.
- **OFF:** The lamp is switched off and has no stored effect.
- **ON:** The lamp is switched on with no active effect.
- **ON (any):** The lamp is switched on, with or without an active effect.
- **OFF, stored effect:** The lamp is switched off and retains a stored effect. This is an exceptional recovery state, not a normal transition-table state.
- **Effect active:** A native effect, such as Candle or Color Cycle, is running. This implies that the lamp is ON.
- **Color/CCT:** Either a color command or a color-temperature command.
- **BRIGHTNESS_ON:** Sets brightness and switches the lamp on.
- **Delay:** The configurable minimum interval enforced by the command manager between writes to the same lamp.
- **EFFECT START:** Starts an effect; the effect identity is selected according to the state and effect rules below.

## Transition table

The table shows the complete intended logical command sequence for an uninterrupted request, including the required power steps after brightness and each actual effect start. The sequencer plans the requested changes; the command manager owns and enforces power finalization and command spacing. Delays are not commands and are not shown. Request normalization and the rules below take precedence over individual rows.

| Initial state | Requested target | Command sequence |
| --- | --- | --- |
| **UNKNOWN** | Any target | **Reject request** |
| **OFF** | Power ON | `POWER ON` |
| **ON (any)** | Power ON | **No command needed** |
| **OFF** | Power OFF | **No command needed** |
| **ON (any)** | Power OFF | `POWER OFF` |
| **ON (any) / OFF** | Brightness | `BRIGHTNESS_ON → POWER ON` |
| **OFF** | Color/CCT | `COLOR/CCT → POWER ON` |
| **ON (any)** | Color/CCT | `COLOR/CCT` |
| **OFF** | Start Effect | `EFFECT START → POWER ON` |
| **ON** | Start Effect | `EFFECT START → POWER ON` |
| **Effect active** | Start Same Effect | **No command needed** |
| **Effect active** | Start Different Effect | `EFFECT START → POWER ON` |
| **Effect active** | Stop Effect | `EFFECT STOP` |
| **ON (any) / OFF** | Color/CCT + Brightness | `COLOR/CCT → BRIGHTNESS_ON → POWER ON` |
| **OFF** | Color/CCT + Start Effect | `COLOR/CCT → EFFECT START → POWER ON` |
| **ON (any)** | Color/CCT + Start Effect | `COLOR/CCT → EFFECT START → POWER ON` |
| **ON / OFF** | Brightness + Start Effect | `BRIGHTNESS_ON → POWER ON → EFFECT START → POWER ON` |
| **Effect active** | Brightness + Start Same Effect | `BRIGHTNESS_ON → POWER ON` |
| **Effect active** | Brightness + Start Different Effect | `BRIGHTNESS_ON → POWER ON → EFFECT START → POWER ON` |
| **Effect active** | Stop Effect + Brightness | `EFFECT STOP → BRIGHTNESS_ON → POWER ON` |
| **ON (any) / OFF** | Color/CCT + Brightness + Start Effect | `COLOR/CCT → BRIGHTNESS_ON → POWER ON → EFFECT START → POWER ON` |

**Known firmware limitations:** Tested lamp firmware can exhibit command-sequencing and advertisement-reporting inconsistencies outside the logical sequence rules in this document. See [Known firmware behavior](known-firmware-behavior.md).

## Request normalization

Apply these rules before selecting a sequence:

1. **UNKNOWN:** Reject new requests without queuing them.
2. **OFF priority:** For a known state, normalize an explicit Power OFF or `Brightness = 0` to a pure Power OFF target. Discard every other target in that request, including Power ON, Color/CCT, Start Effect and Stop Effect. All other brightness cases refer to positive values.
3. **Conflicts:** If OFF normalization does not apply, reject requests containing both Color and CCT or both Start Effect and Stop Effect. Send no command and do not execute a partial sequence.
4. **Redundant Power ON:** When combined with positive Brightness, Color/CCT or Start Effect, discard the separate Power ON target. The remaining sequence already establishes or preserves ON. Keep Power ON when combined only with Stop Effect.
5. **Redundant Stop Effect:** Color/CCT stops an active effect, so discard a separate Stop Effect target when combined with Color/CCT. In normal states without an active effect, ignore Stop Effect and process the remaining targets normally. Stored-effect recovery is handled separately below.
- **Validation before planning:** Only after normalization and conflict validation may the sequencer plan recovery or a normal transition. A rejected request must not trigger a recovery `EFFECT STOP`.
- **Request shape:** A normalized request contains at most one power target, one brightness target and one effect target. Resolve superseded values within the same category before sequence selection. The conflict rules concern contradictory targets within one request, not a newer request replacing an older one.

## State and effect handling

### Planner state

- **Known state:** A state advertisement establishes a known planner state only when the information needed to select a sequence can be interpreted, including power and effect state or identity where applicable.
- **Unknown mode:** An unknown mode must not be treated as “no active effect”.
- **UNKNOWN handling:** Uninterpretable required information makes the planner UNKNOWN; apply the abort rules under [Failure handling and cancellation](#failure-handling-and-cancellation).
- **Persistence:** A known state does not expire merely because no newer advertisement has arrived.
- **State updates:** Successful local writes update the assumed planner state; valid advertisements reconcile it with the reported state.
- **Availability:** Availability is evaluated separately.

### Effect semantics and ordering

- **Effect-start power completion:** Every actual Candle or Color Cycle start is followed by a mandatory power command, even if the assumed lamp state is already ON. In an uninterrupted start sequence this is `POWER ON`; a newer OFF target may replace the unsent finalizer with `POWER OFF`. A same-effect no-op creates no new obligation. `EFFECT STOP` alone does not create one.
- **Effect identity:** `EFFECT START` starts the requested effect. “Same Effect” and “Different Effect” are classifications based on the currently active effect.
- **Brightness and effects:** Brightness alone preserves an active effect. Starting that same effect together with brightness does not require a redundant `EFFECT START`. Starting a different effect, or an effect when none is active, follows the brightness finalization shown in the table.
- **Color/CCT:** Color/CCT stops an active effect. Requests containing Color/CCT or Stop Effect do not implicitly restart the previous effect.
- **Power OFF:** `POWER OFF` stops an active effect and leaves normal OFF with no stored effect. Update the assumed state after a successful write.
- **Stop Effect with power:** `EFFECT STOP` preserves power state. For Stop Effect + Power ON, an active effect needs only `EFFECT STOP`; normal OFF ignores the redundant stop and uses `POWER ON`.
- **Combined requests:** When Color/CCT and brightness are combined, Color/CCT comes first. Combined appearance rows take precedence over effect-only shortcuts; any requested effect is started in the position shown in the table. This ordering does not guarantee flicker-free transitions.

## Stored-effect recovery

- **Cause:** `OFF, stored effect` is an exceptional recovery state. It may remain after a partially completed sequence, such as a successful `EFFECT START` while OFF followed by a `POWER ON` that does not complete.
- **Planning:** For a new, normalized and validated request starting in this state, the sequencer plans `EFFECT STOP` as a recovery prefix. It plans the remaining target as if the prefix will leave normal OFF.
- **Successful stop:** Update the assumed state only after the stop succeeds; dependent commands must not proceed if it fails.
- **Mandatory prefix:** Do not optimize this prefix away for Power OFF, Color/CCT, Start Effect, the same effect or a different effect.
- **Power OFF:** For Power OFF, the recovery stop is sufficient: normal OFF then needs no further write.
- **Stop and power ON:** For Stop Effect + Power ON, recovery is followed by `POWER ON`.
- **Ongoing sequences:** An expected intermediate state within an ongoing sequence does not trigger recovery. After `EFFECT START` succeeds while OFF, continue with the planned `POWER ON` rather than inserting a stop.
- **Ownership and priority:** Recovery belongs to the sequencer. An outstanding manager-owned [power finalizer](#power-finalization) has priority over any non-power recovery step. This exceptional state must not be treated as an ordinary no-command case.

## Power finalization

- **Trigger and next command:** A successful positive `BRIGHTNESS_ON` or actual `EFFECT START` creates a manager-owned obligation: the next physical command to that lamp must be a power command. No Color/CCT, effect, brightness, recovery stop or other non-power command may intervene.
- **Power target:** Normally use `POWER ON`. If the latest accepted target requires OFF, use `POWER OFF` instead; an unsent ON finalizer may therefore be replaced by OFF.
- **Finalizer completion:** A successful power write fulfills the obligation. Do not skip it merely because the assumed power state already matches the target.
- **Generation tracking:** The manager tracks the originating brightness/effect-start generation and its unfinished finalization separately from replaceable target slots.
- **Replanning:** Ordinary superseding, replanning or clearing unsent goals preserves this obligation. Clearing goals for replanning must remain distinct from aborting a transaction.
- **Request completion:** A brightness/effect-start request is not successfully completed until its power finalizer and remaining requested steps succeed.
- **Superseding requests:** A later accepted OFF request may provide the finalizer and supersede obsolete tails. Superseded requests may finish according to the normal superseding policy, but must not falsely confirm execution of discarded targets.
- **Aborts:** Cancellation, terminal failure, UNKNOWN and unload follow [Failure handling and cancellation](#failure-handling-and-cancellation); they must not leave a deferred automatic power command behind.

## Command execution

### Latest request wins

- **Pending steps:** Replace superseded unsent steps, including during Delay.
- **In-flight writes:** A command already being written cannot be withdrawn.
- **Finalization:** Preserve outstanding power finalization as specified above.
- **No-command decisions:** Before deciding that no write is needed, account for pending, in-flight and finalization work.
- **OFF requests:** An OFF request cancels older unsent ON targets even when the lamp is already OFF.

### Command spacing

- **Responsibility and scope:** The sequencer determines command order; the manager enforces Delay between consecutive writes to the same lamp, within sequences, across requests and before finalizers.
- **Elapsed time:** Time already elapsed counts toward the interval: wait only for the remainder. Even a single-command sequence may require a wait after an earlier command.
- **Serialization:** Writes to the same lamp must not overlap.
- **Independent lamps:** Other lamps do not share this delay.
- **No delay commands:** The sequencer does not generate delay commands.
- **Measurement:** Measure Delay from completion of the previous successful GATT write to the start of the next GATT write, using a monotonic clock.
- **Failed attempts:** For a failed or timed-out attempt, use its local completion time as the spacing reference for any subsequent attempt, including a retry. This timestamp does not imply receipt or execution by the lamp.

## Failure handling and cancellation

### Confirmed failures and retries

- **State and dependent steps:** A failed write must not update assumed state as though it succeeded. Dependent steps proceed only after success. Do not roll back completed writes.
- **Retry limit:** The manager allows at most two attempts for the same logical command and target generation when non-delivery is confirmed and the failure is potentially recoverable.
- **Final errors:** Deterministic packet-construction errors are final.
- **Budget lifetime:** Advertisements and replanning do not reset the budget; a newer request replacing that target receives its own budget.
- **Non-delivery:** Confirmed non-delivery of `BRIGHTNESS_ON` or `EFFECT START` creates no finalizer obligation.
- **Recoverable finalizer failure:** A recoverable finalizer failure leaves the obligation open within its attempt budget.
- **Terminal failure:** A terminal failure aborts the affected transaction with an error and does not count as successful finalization. Replanning must not reset an exhausted finalizer budget.

### Uncertain outcomes and UNKNOWN

- **Ambiguous outcome:** A timeout or ambiguous write failure may occur even if the lamp received the command. Do not assume either the previous or requested state is confirmed.
- **Abort policy:** Do not automatically retry: mark the planner UNKNOWN and abort all pending requests with an error, including newer requests accepted during the write.
- **Uninterpretable advertisements:** The same UNKNOWN abort policy applies when required advertisement state cannot be interpreted.
- **State recovery:** A suitable state advertisement permits new requests again; aborted requests are not retained or resumed. No request waits indefinitely for state recovery.

### Cancellation and transaction aborts

- **Abort conditions:** On explicit cancellation of the owning transaction, terminal finalizer failure, UNKNOWN or unload, stop automatic finalization and discard dependent tails. Record the transaction as cancelled or failed, not successfully finalized.
- **No deferred commands:** Do not resume an old `POWER ON` after a later advertisement, reconnect or reload.
- **In-flight result:** If a write is already in flight, process its actual result without recreating aborted follow-up work.
- **Newer requests:** Independent newer requests remain subject to normal generation handling; UNKNOWN aborts all pending requests.
- **New transactions:** A new accepted request after recovery is planned from the available state as a new transaction.

## Delay setting

| Setting | Default | Allowed range |
| --- | --- | --- |
| Minimum command interval | 250 ms | 0–2000 ms |

- **Range:** The allowed range is inclusive. A value of **0 ms** disables intentional spacing; command order, write serialization and power finalization still apply.
- **Existing settings:** Preserve saved values within the allowed range, including values below 250 ms, and show them consistently in the options UI.
