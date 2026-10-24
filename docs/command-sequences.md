# Command sequences

This specification describes the implemented behavior of the AwoX/EGLO Connect.Z command pipeline. Commands are symbolic; protocol frames are documented in the [protocol reference](protocol.md).

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

1. **UNKNOWN:** The pure planner rejects unknown state. Before submitting a new HA request, the entity may obtain a bounded initial 1911 status; no lamp control is sent until a valid state is available. This is not per-request readiness when state is already known.
2. **OFF priority:** For a known state, normalize an explicit Power OFF or `Brightness = 0` to a pure Power OFF target. Discard every other target in that request, including Power ON, Color/CCT, Start Effect and Stop Effect. All other brightness cases refer to positive values.
3. **Conflicts:** If OFF normalization does not apply, reject requests containing both Color and CCT or both Start Effect and Stop Effect. Send no command and do not execute a partial sequence.
4. **Redundant Power ON:** When combined with positive Brightness, Color/CCT or Start Effect, discard the separate Power ON target. The remaining sequence already establishes or preserves ON. Keep Power ON when combined only with Stop Effect.
5. **Redundant Stop Effect:** Color/CCT stops an active effect, so discard a separate Stop Effect target when combined with Color/CCT. In normal states without an active effect, ignore Stop Effect and process the remaining targets normally. Stored-effect recovery is handled separately below.

- **Validation before planning:** Only after normalization and conflict validation may the sequencer plan recovery or a normal transition. A rejected request must not trigger a recovery `EFFECT STOP`.
- **Request shape:** A normalized request contains at most one power target, one brightness target and one effect target. Resolve superseded values within the same category before sequence selection. The conflict rules concern contradictory targets within one request, not a newer request replacing an older one.

## State and effect handling

### Planner state

- **Known state:** A valid state advertisement, successful 1911 group-confirmation result or explicit UNKNOWN-bootstrap result establishes known planner state only when power and effect state can be interpreted. Other 1911 notifications do not reconcile the planner.
- **Unknown mode:** An unknown mode must not be treated as “no active effect”.
- **UNKNOWN handling:** Uninterpretable required information makes the planner UNKNOWN; apply the abort rules under [Failure handling and cancellation](#failure-handling-and-cancellation).
- **Persistence:** A known state does not expire merely because no newer advertisement has arrived. Normal BLE disconnect/reconnect does not reset it to UNKNOWN. A newly created entity starts UNKNOWN until an authoritative state is applied.
- **State updates:** Successful local writes update the assumed planner state; advertisements and selected 1911 confirmation/bootstrap results reconcile it outside a batch. Successful writes advance planning but do not by themselves complete a written HA request.
- **Availability:** Availability is evaluated separately.

### Effect semantics and ordering

- **Effect-start power completion:** Every actual Candle or Color Cycle start is followed by a mandatory power command, including when already ON. In an uninterrupted start sequence use `POWER ON`. Once the group starts, a newer OFF request waits for the next group and cannot replace this finalizer. A same-effect no-op or effect stop creates no new power obligation.
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
- **Power target:** Use the target of the running group. New requests received after its first command selection wait separately; they cannot replace a running ON finalizer with OFF.
- **Finalizer completion:** A successful power write fulfills the obligation. Do not skip it merely because the assumed power state already matches the target.
- **Generation tracking:** The manager tracks the originating brightness/effect-start generation and its unfinished finalization separately from replaceable target slots.
- **Replanning:** Ordinary superseding, replanning or clearing unsent goals preserves this obligation. Clearing goals for replanning must remain distinct from aborting a transaction.
- **Request completion:** A brightness/effect-start request is not successfully completed until its power finalizer and remaining requested steps succeed and the executed batch is status-confirmed.
- **Superseding requests:** Before a group starts, latest-wins merging can replace pending targets. After it starts, preserve its complete remaining sequence, power obligations and confirmation; newer OFF and ON requests wait separately.
- **Aborts:** Cancellation, terminal failure, UNKNOWN and unload follow [Failure handling and cancellation](#failure-handling-and-cancellation); they must not leave a deferred automatic power command behind.

## Command execution

### Latest request wins

- **Before first selection:** Replace superseded pending targets, including during connection preparation or an initial spacing wait.
- **Running group:** From its first physical command selection through confirmation and any complete repeat, keep the accepted targets fixed. Finish the remaining sequence and required power finalizers before the next group.
- **Waiting work:** Collect newer requests in separate power, brightness, appearance and effect slots. New values replace relevant older waiting values; preserve independent targets. OFF discards older waiting targets, and a newer static appearance replaces an older waiting effect goal.
- **No unbounded FIFO:** Do not execute every intermediate request. Only the latest surviving waiting slots become the next group, retaining their request owners.
- **Power OFF latency:** A newer OFF also waits for the running group, including any polls and repeat. It cannot interrupt a running finalizer.
- **Completion:** Replacement of an unstarted waiting target is not an execution error or a status-confirmed write. A started group retains its own confirmation barrier. Waiting no-ops cannot complete merely from the running group's optimistic state.
- **Failure:** Final unconfirmed outcome, session loss or ambiguous writes retain the conservative UNKNOWN policy: abort all still-pending work, including waiting requests. Never replay it automatically after recovery.

### Command spacing

- **Responsibility and scope:** The sequencer determines command order; the manager enforces Delay between consecutive writes to the same lamp, within sequences, across requests and before finalizers.
- **Native fades:** After Color, CCT or Brightness, add `1.25 × encoded transition time` to the configured same-lamp interval before the next control write, including mandatory power finalizers and later groups. Use the decisecond duration actually carried by the frame. No intermediate confirmation is required. A matching status does not release the next write before this spacing expires.
- **Elapsed time:** Time already elapsed counts toward the interval: wait only for the remainder. Even a single-command sequence may require a wait after an earlier command.
- **Serialization:** Writes to the same lamp must not overlap.
- **Independent lamps:** Other lamps do not share this delay.
- **No delay commands:** The sequencer does not generate delay commands.
- **Measurement:** Measure Delay from completion of the previous successful GATT write to the start of the next GATT write, using a monotonic clock.
- **Failed attempts:** For a failed or timed-out attempt, use its local completion time as the spacing reference for any subsequent attempt, including a retry. This timestamp does not imply receipt or execution by the lamp.

## 1911 group confirmation

Confirmation requires direct type `0x80`, matching mesh ID/MAC suffix and the current session. Mesh type `0x82` cannot confirm commands. Packet formats and status acceptance are specified in the [protocol reference](protocol.md#1911-direct-lamp-status).

- **Scope:** Direct BLE connection to the configured lamp only. No mesh route selection, MQTT or advertisement-based command confirmation.
- **Session initialization:** Initialize 1911 once per authenticated session as described in the [protocol reference](protocol.md#1911-direct-lamp-status). Do not add the lamp-command gap between authentication/notification operations.
- **Write versus request success:** The manager processes each successful GATT write immediately, preserving its state-dependent planning and required power steps. A separate generation barrier holds written HA requests until confirmation. Every generation consumed by a physical power step within the frozen group is held. Requests received after the group starts wait separately, even when their power target matches a running finalizer.
- **Batch boundary:** Freeze accepted intent at the first physical command selection, after connection preparation and spacing. Record successful logical writes, their generations and plaintext frames. Do not add newer waiting targets to this group.
- **End target:** Derive goals from the actual encoded frames. OFF dominates earlier fields; Color Cycle removes static appearance goals. Numeric tolerance is one raw step, with circular hue comparison. New requests wait outside this goal. Evaluate waiting same-state no-ops only after the running group is confirmed, against the resulting state.
- **Response window:** After the last group write, wait up to 250 ms for a matching status. If that last write is Color, CCT or Brightness, add `1.25 × encoded transition time`, measured from its write completion. Accept a valid fresh matching reply immediately, including during the fade allowance. Earlier fades already consumed their required spacing before subsequent writes; do not add their allowance again. With the default 0.2 s fade and 250 ms command interval, the next control is eligible after 500 ms; a final fading command has a 500 ms maximum automatic response window.
- **Polls:** If not matched, send up to seven directed status queries, with 250 ms response windows. Mismatch and incomplete data do not trigger early resends. Poll writes are not lamp controls and do not inherit the configurable lamp-command gap.
- **Repeat:** If still unmatched, repeat the entire immutable plaintext group once with the same session key. A newer waiting request does not invalidate or change this replay. Retain fade-aware control spacing, mandatory power steps and the same response/poll policy, recalculated from the new write times. The budget is separate from confirmed-non-delivery transport retries. Replay never calls semantic write completion again.
- **New requests:** While a group runs or waits for confirmation, merge newer targets into separate latest-wins waiting slots. Preserve independent surviving slots and effect/power conflict rules. Promote waiting work only after the group finishes. Replaced waiting targets complete without a superseded error; this does not assert that they were executed. Explicit cancellation, session loss and terminal failures still abort as specified below.
- **State sources:** Receive valid 1911 replies into the session store for group confirmation and UNKNOWN bootstrap only. Individual notifications never synchronize the HA display or planner, including after a completed group. Apply the selected successful group-confirmation result after the barrier, or explicitly apply the UNKNOWN-bootstrap result. Advertisements retain their normal state-update behavior outside active groups. No extra polling is introduced.
- **Final failure:** After both sends remain unconfirmed, mark planner state UNKNOWN and fail pending requests, including newer requests if still pending. Never retain work for automatic later replay. A valid state advertisement or the result of a bounded initial-status query permits a new request.
- **Session loss:** Invalidate old status waiters and stop the batch. Do not reconnect to replay an old group. Notification callbacks carry the authentication generation; old callbacks cannot populate a new session. Idle disconnect is held off during group execution/confirmation and restored afterward.
- **Protocol limit:** Replies have no unique query identifier or trustworthy device measurement timestamp. Session and reception-time filtering reject old sessions/pre-write receptions but cannot prove which same-session query produced a delayed reply. A matching state is not proof of physical light output.

### Native-effect replies and displayed appearance

- **Intermediate cycle modes:** `0x04`/`0x05` retain Color Cycle identity with a CCT base; they cannot satisfy `effect=None` or Candle goals. Keep waiting inside the normal response window. Poll only when that window expires without a complete match.
- **Dynamic hue:** Native-effect hue `0xFF` has no fixed angle and cannot satisfy a fixed hue goal. A Color-Cycle goal does not require a static appearance match; accepted field ranges are defined in the [protocol reference](protocol.md#brightness-hue-and-saturation-ranges).
- **HA display:** The final successful 1911 group-confirmation reply updates power, brightness, effect and convertible color/CCT values, including native effects. Display values come from the reply, not the requested command values. Color Cycle appearance is a momentary sample, not a permanent color target. Hue `0xFF` does not overwrite the previous HS value. Other 1911 replies update neither display nor planner. Effect advertisements preserve HA color/CCT and color mode while still updating power, brightness and effect. The explicit UNKNOWN-bootstrap result uses that same appearance-preserving effect path.
- **Confirmation is separate:** Retain appearance raw fields for diagnostics and any goals required by the encoded group, such as Color/CCT + Candle. The display policy does not remove or relax those goals.

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
- **State recovery:** A suitable state advertisement or explicit UNKNOWN-bootstrap result permits new requests again; aborted requests are not retained or resumed. No request waits indefinitely for state recovery.

### Cancellation and transaction aborts

- **Abort conditions:** On explicit cancellation of the owning transaction, terminal finalizer failure, UNKNOWN or unload, stop automatic finalization and discard dependent tails. Record the transaction as cancelled or failed, not successfully finalized.
- **No deferred commands:** Do not resume an old `POWER ON` after a later advertisement, reconnect or reload.
- **In-flight result:** If a write is already in flight, process its actual result without recreating aborted follow-up work.
- **Newer requests:** Independent newer requests remain subject to normal generation handling; UNKNOWN aborts all pending requests.
- **New transactions:** A new accepted request after recovery is planned from the available state as a new transaction.

## Transition and delay settings

| Setting | Default | Allowed range |
| --- | --- | --- |
| Default transition | 0.2 s | 0–30 s |
| Minimum command interval | 250 ms | 0–2000 ms |

- **Default transition:** The options range applies to the configured default. An explicit Home Assistant `transition` overrides it; the protocol encodes fades in deciseconds. This option does not impose a separate 30-second clamp on explicit action values.
- **Command-interval range:** The allowed range is inclusive. A value of **0 ms** disables the base command interval; the additional fade allowance still applies after Color, CCT and Brightness. Command order, write serialization and power finalization remain unchanged.
- **Existing settings:** Preserve saved values within the allowed range, including values below 250 ms, and show them consistently in the options UI.
