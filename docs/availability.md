# Bluetooth reachability and availability

One state determines light availability. An active connection always wins. Once
disconnected, the newest still-valid evidence decides whether the lamp is shown as
visible or recently connected:

| State | Condition | Light available |
|---|---|---|
| `connected` / Verbunden | BLE backend reports an active connection | Yes |
| `recently_connected` / Zuletzt verbunden | The newest still-valid evidence is a connection proof | Yes |
| `visible` / Sichtbar | The newest still-valid evidence is an advertisement | Yes |
| `unreachable` / Nicht erreichbar | Evidence exists, but all deadlines have expired | No |
| Unknown | Neither advertisement nor connection evidence exists | No |

At exactly the timeout, evidence is expired. Advertisements and connection proofs
have independent timestamps but use the same **Unavailable after** option. If both
are still valid, the newer timestamp wins; a connection proof wins an exact tie.
Power ON/OFF is unrelated to reachability; an OFF lamp can remain available.

## Evidence and disconnects

- Advertisements retain their actual reception time. Older timestamps or repeated reads of the same cached packet do
  not renew it. A newly received packet with identical payload still counts
  through its newer reception timestamp. Advertisements never update the connection-proof timestamp.
- Successful authentication and accepted session-scoped 1911 replies provide
  connection evidence. A control write without response does not establish a new
  received proof on its own.
- An active backend connection keeps the light available without advertisements.
- A regular idle disconnect records the final known active connection immediately
  before starting backend cleanup. Cleanup completion, errors, timeouts and caller
  cancellation do not renew that timestamp.
- Unexpected connection loss starts no new deadline. Existing proofs retain only
  their remaining lifetime. The backend flag is checked locally every second;
  the physical outage cannot be recognized before the backend reports it.
- The worker publishes availability before diagnostics. One monotonic-time
  reachability snapshot is held for the complete publication, so the timeout cannot
  expire between the light availability read and the Bluetooth-status sensor read.
  Callback failures are isolated and logged.

## Reload and restart

Entry unload captures advertisement and connection timestamps before closing its
clients. A still-active connection supplies a final known-live timestamp. Reload
restores the original times and Last Seen wall-clock value from a process-local
cache keyed by config entry and MAC address. New HA advertisement history can
supersede an older restored timestamp.

Reload restores no BLE client, session key, notification subscription, command or
pending confirmation. Repeated reloads of a disconnected lamp do not extend its
deadline. Changed timeout settings apply to the original evidence age.

Failed unload retains the live instance. Failed setup does not overwrite saved
evidence. Permanent entry removal deletes its cached evidence. The cache is only
in `hass.data`: a full HA restart does not restore monotonic timestamps. Without
new or current-process HA packet history, startup remains unknown and unavailable.

Accepted 1911 replies can renew connection evidence even when they are not selected
for a state update. Reachability tracking is separate from the confirmation-only
HA/planner state policy.

## Diagnostic meanings

- **Last Bluetooth Source:** source of the last received advertisement. It is historical
  advertisement data and therefore does not switch to the active connection proxy.
  The value remains available through `recently_connected` and `unreachable` until a
  newer advertisement replaces it (equal-time source refinements are allowed).
- **Signal Strength:** RSSI from the same advertisement history. It is retained
  after the advertisement expires for reachability purposes and is not a measurement
  of the active GATT connection's signal.
- **Last Seen:** time of the last advertisement reception, preserved on reload.
- **Last Connection:** historical proxy/adapter of the last successful authenticated
  connection. It is independent of the advertisement source.

For example, idle disconnect after 20 seconds and timeout 10 seconds gives
`connected → recently_connected → unreachable` if no new advertisement arrives.
The last step is 10 seconds after the regular disconnect begins. A new
advertisement changes the state to `visible` and starts its own 10-second window.

## Internal structure

`availability.py` owns advertisement history, connection evidence, reachability
and deadline calculations. `client.py` supplies HA/BLE observations and manages
the existing worker and entity publications. This separation does not change
status priority, reload evidence, historical diagnostics or command behavior.
