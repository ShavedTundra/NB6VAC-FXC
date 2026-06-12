# NB6VAC-FXC Monitor

Continuous monitoring of an SFR Box NB6VAC-FXC (FTTH/router mode, firmware R4.0.47h5) via its REST API. Logs to JSONL, detects crashes, tracks recovery.

## Language

**Box crash**:
The SFR Box kernel/firmware reboots spontaneously, observable as a reset of the `@uptime` counter in `system.getInfo`.
_Avoid_: reboot, restart, hang (those describe symptoms, not the event)

**Crash detection**:
The monitor observes `current_uptime < previous_uptime` between consecutive polls, indicating the box rebooted.
_Avoid_: uptime reset (describes the mechanism, not the detection)

**Recovery**:
The box has finished booting and WAN is operational. Criteria: `current_uptime < 600` AND `wan.getInfo.@status == "up"`.
_Avoid_: back online, fixed (too vague)

**Unreachable**:
All API endpoints fail with `ConnectionError` or `Timeout` — the box is completely non-responsive on the network.
_Avoid_: down, dead, offline

**Crash context**:
A snapshot of the last-known-good box vitals (temperature, voltage, uptime, WAN status, host count) captured at the moment crash detection fires. Logged as structured data in the first CRASH-mode entry.
_Avoid_: crash report (that implies a file, not a log field)

**Dead endpoint**:
An API endpoint that consistently returns empty or meaningless data for this box's configuration (FTTH mode). Not polled to reduce HTTP overhead and timeout risk.

**Active endpoints** (6):
`system.getInfo`, `wan.getInfo`, `ftth.getInfo`, `ont.getInfo`, `lan.getHostsList`, `wlan5.getClientList`
_Avoid_: live endpoints, useful endpoints

**Dead endpoints** (3, not polled):
`dsl.getInfo` (FTTH box has no DSL line), `ppp.getInfo` (PPPoE not used in FTTH mode), `wlan.getClientList` (2.4GHz has no clients)

**ONT**:
The fiber optic terminal (I-010G-Q, SN PTIN91430165), separate from the SFR Box. Has its own uptime counter (`ont.getInfo.@uptime` in seconds, typically >150 days). Not affected by box crashes.

## Relationships

- A **box crash** is detected by **crash detection** (uptime comparison)
- **Crash detection** produces a **crash context** (last-known-good vitals)
- After **crash detection**, the monitor enters CRASH mode and polls `system.getInfo` + `wan.getInfo` every 10s
- **Recovery** is declared when uptime is fresh (<600s) AND WAN is up
- If the box is **unreachable**, the monitor probes `system.getInfo` with escalating backoff; on reconnection it checks uptime to determine box crash vs network blip
- **Dead endpoints** are excluded from all polling; their data is not logged
- The **ONT** uptime is independent from box uptime and continues climbing through box crashes

## Example dialogue

> **Dev:** "The monitor entered UNREACHABLE at 14:19 and never recovered. The error says `'status'`."
> **Domain expert:** "That's a `KeyError` — the code reads `rsp['status']` but the box returns `rsp['system']`. The broad `except Exception` in `tick_unreachable` caught it and treated it as 'still unreachable' forever. The fix is tight exception handling: catch network errors, let `KeyError` propagate."

> **Dev:** "Why don't we track the repeater?"
> **Domain expert:** "The RE700X uses MAC address translation on its backhaul. The SFR Box API never sees the repeater's real MAC. We can't detect it through the API. If the TV is online in `lan.getHostsList`, the repeater is working — that's indirect but reliable."

## Flagged ambiguities

- "Repeater connected" was logged as `false` for 5 days — resolved: not because the repeater was absent, but because the code had wrong key paths (`rsp["clients"]["client"]` instead of `rsp["client"]`) AND the repeater is invisible to the box API. Repeater tracking removed entirely.
- `extract_uptime` used key `"status"` — resolved: the actual XML path is `rsp.system.@uptime`. The `"status"` key never existed in any response; the `except (KeyError, ...)` swallowed the error silently, returning `None` on every call. This disabled crash detection and unreachable recovery.
- Temperature (54.4–60.5°C) and voltage (12.167–12.251V) showed no correlation with crashes — resolved: they are logged for raw data collection but not used for alerting or prediction.
- Crash uptimes (16.2h, 19.3h, 49.8h) show no consistent periodicity — resolved: root cause is likely random firmware exception, not memory leak.
