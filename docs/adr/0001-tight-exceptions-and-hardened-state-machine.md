# 0001 — Tight Exception Handling and Hardened Monitor State Machine

After 5 days of live monitoring (May 28–Jun 1, 2026), analysis of 5 box-crash/unreachable incidents revealed that every single recovery failed. The monitor has never recovered on its own — each incident required manual process restart. Root cause: a `KeyError('status')` bug in `extract_uptime` and `tick_unreachable` (wrong XML key `"status"` instead of `"system"`), silently swallowed by broad `except Exception` blocks. Additionally, crash detection never fired because `previous_uptime` was always `None` (the broken `extract_uptime` returned `None` on every call), and repeater tracking never worked due to wrong XML key paths and the repeater being invisible to the box API.

## Decisions

### 1. Remove defensive try/except from data extraction helpers

`extract_uptime` returns `int(system_info["rsp"]["system"]["@uptime"])` with no try/except. The box API returns a stable XML shape — if it changes, we want an immediate loud failure, not silent `None` that disables crash detection for days. Callers that need to tolerate a missing value handle it at their level with tight catches.

### 2. Tighten all exception handling to specific error types

Every `except Exception` replaced with the specific errors we actually expect:
- `requests.exceptions.ConnectionError` — box unreachable
- `requests.exceptions.Timeout` — box slow/unreachable
- `ET.ParseError` — garbled XML response (mid-reboot)
- `KeyError`, `ValueError` — programming bugs, re-raised immediately

This ensures code bugs surface as crashes (visible, fixable) rather than silent degradation (invisible for days).

### 3. Drop 3 dead endpoints

`dsl.getInfo`, `ppp.getInfo`, `wlan.getClientList` removed from `ALL_ENDPOINTS` (9 → 6). This box is FTTH-only; DSL and PPP will never be active. 2.4GHz WiFi has no clients. `dsl.getInfo` was the only endpoint that ever timed out during normal operation — removing it eliminates the most common partial-failure case.

### 4. Crash recovery uses uptime window + WAN status

Old: `uptime_climbing AND clients_returning >= 50%` — never worked because client count was always 0 (wrong endpoint). New: `current_uptime < 600 AND wan_status == "up"`. Fresh uptime confirms the reboot happened in our observation window. WAN status confirms the box has fully recovered (not just API-responsive). Client count is unreliable because WiFi clients take time to reconnect after a reboot.

### 5. Crash mode polls system.getInfo + wan.getInfo (not wlan.getClientList)

Matches the new recovery criteria: we need uptime (system) and WAN status (wan). No auth token needed for these public endpoints.

### 6. Crash context enrichment

On crash detection, the first CRASH-mode log entry includes a `crash_context` field with the last-known-good temperature, voltage, uptime, WAN status, and host count. This creates a structured dataset for future root-cause analysis as we accumulate more crash events.

### 7. Remove repeater tracking

The TP-Link RE700X repeater (MAC 6C:4C:BC:91:DF:A9, IP 192.168.1.30) is invisible to the SFR Box API due to MAC address translation on its WiFi backhaul. It has never appeared in any endpoint across 5 days of logs. The repeater's presence can be inferred indirectly: if hosts behind the repeater (e.g., TV) appear in `lan.getHostsList`, the repeater is working. Removed `tag_repeater_in_results`, `find_repeater_status`, `repeater_mac` config, and all `repeater_connected`/`repeater_last_seen` fields.

## Considered Options

- **Exception handling**: Broad catches with logging (Option B) vs tight catches (Option A, chosen). Broad catches created the 5-day blind spot. Logging would have helped diagnose faster but wouldn't have prevented the silent degradation.
- **Crash recovery**: Client count (original), WAN-only, or uptime+WAN (chosen). Client count is unreliable post-reboot. WAN-only doesn't confirm the crash is recent. Uptime window + WAN is precise.
- **Repeater detection**: ICMP ping to 192.168.1.30, API-based tracking, or removal (chosen). Ping ties repeater detection to monitoring-machine network state, which is unreliable (the monitoring machine itself disconnects frequently). API tracking is impossible. Removal is honest.

## Consequences

- The monitor will crash (process exit) on programming bugs instead of silently degrading. This is intentional — a dead monitor is easier to notice and restart than a monitor that's running but producing wrong data.
- If the SFR Box firmware changes its XML response shape, the monitor will crash. This is the correct behavior: the operator needs to know immediately and update the code.
- Future crash analysis will have structured `crash_context` data to correlate with box behavior over time.
