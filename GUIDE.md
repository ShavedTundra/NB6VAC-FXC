# NB6VAC-FXC — User Guide

Complete guide to set up, run, and draw conclusions from the SFR Box monitoring tools.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Configuration](#2-configuration)
3. [API Client](#3-api-client)
4. [Monitor](#4-monitor)
5. [Understanding the Logs](#5-understanding-the-logs)
6. [First Conclusions (4–7 Days)](#6-first-conclusions-47-days)

---

## 1. Prerequisites

| Requirement | Version | Install |
|---|---|---|
| Python | 3.12+ | `brew install python` or system package |
| `requests` | any recent | `pip install requests` |
| `pick` | any recent | `pip install pick` (API client only) |

Verify:

```bash
python --version        # 3.12+
python -c "import requests; import pick; print('OK')"
```

You must be on the same local network as the box (default: `192.168.1.1`).

---

## 2. Configuration

The monitor needs the box admin password. Two methods (pick one):

### Method A: Environment variable (recommended)

```bash
export SFR_PASSWORD="your-password"
```

Add to your `~/.zshrc` or `~/.bashrc` to persist across sessions.

### Method B: Config file

Create `config.local.json` at the project root (already in `.gitignore`):

```json
{
  "password": "your-password"
}
```

> ⚠️ Never commit this file. It is excluded from git by default.

---

## 3. API Client

An **interactive** tool to explore the box's REST API by navigating menus. Useful for ad-hoc queries and discovering what the API returns.

### Launch

```bash
cd api-client/
python client.py --hostname 192.168.1.1 --username admin --password admin
```

### CLI Options

| Flag | Default | Description |
|---|---|---|
| `--hostname` | `192.168.1.1` | Box IP or hostname |
| `--username` | `admin` | Auth username |
| `--password` | `admin` | Auth password |
| `--warning-level` | `1` | Prompt before calls at this warning level or above |
| `--disable-level` | `3` | Block calls at this warning level or above |

### How It Works

1. **Authentication** — On startup, the client authenticates via HMAC-SHA256 token exchange and stores the session token.
2. **Menu navigation** — Press Enter to bring up the endpoint picker:
   - **Level 1**: pick a category (e.g. `system`, `wan`, `wlan`, `auth`)
   - **Level 2**: pick an endpoint within that category (e.g. `getInfo`, `getClientList`)
3. **Execution** — The client makes the API call (GET or POST) with the token if the endpoint requires auth. If the call needs parameters, you'll be prompted for each one.
4. **Safety** — Endpoints have warning levels. Destructive calls (reboot, reset) are blocked or require confirmation depending on your `--warning-level` / `--disable-level` settings.
5. **Loop** — After each call, press Enter again to pick another endpoint. Ctrl+C to exit.

### Common Queries

| What you want | Menu path |
|---|---|
| System info (firmware, uptime, version) | `system` → `getInfo` |
| WAN connection status | `wan` → `getInfo` |
| DSL line stats (attenuation, SNR) | `dsl` → `getInfo` |
| FTTH status | `ftth` → `getInfo` |
| WiFi clients (2.4 GHz) | `wlan` → `getClientList` |
| WiFi clients (5 GHz) | `wlan5` → `getClientList` |
| LAN hosts | `lan` → `getHostsList` |
| PPP session info | `ppp` → `getInfo` |

---

## 4. Monitor

A **continuous daemon** that polls the box every 60 seconds, detects crashes and outages, logs everything to JSONL, and sends macOS notifications on events.

### Launch

```bash
# Make sure password is configured (see §2)
python monitor.py --hostname 192.168.1.1
```

### CLI Options

| Flag | Default | Description |
|---|---|---|
| `--hostname` | `192.168.1.1` | Box IP or hostname |
| `--username` | `admin` | Auth username |

The password is read from `SFR_PASSWORD` env var or `config.local.json` — never passed as a CLI arg.

### Run in Background

For long-term monitoring, run in a terminal multiplexer:

```bash
# With tmux
tmux new -s monitor "python monitor.py"

# With screen
screen -dmS monitor python monitor.py
```

Detach with `Ctrl+B, D` (tmux) or `Ctrl+A, D` (screen). Re-attach with `tmux attach -t monitor` or `screen -r monitor`.

### What It Does — The 4 Modes

The monitor operates as a state machine with four modes:

```
STARTUP
  │
  ▼
┌──────────┐   uptime reset   ┌──────────────┐
│ BASELINE │──────────────────▶│ CRASH MODE   │
│ (3 polls)│                  │ (10s, max 5m)│
└────┬─────┘                  └──────┬───────┘
     │                               │
     ▼                               │ recovered
┌──────────┐   all endpoints fail   │
│ NORMAL   │───────────────────────▶┌┴──────────────┐
│ (60s poll)│◀─────────────────────│ UNREACHABLE    │
└──────────┘   box comes back      │ (10s→60s→5min) │
               + uptime reset?     └────────────────┘
               → CRASH MODE
```

#### Mode 1: Baseline (startup)

- **When**: Immediately on launch.
- **What**: 3 rapid polls at 10-second intervals against `system.getInfo`.
- **Why**: Verifies the box is reachable and auth works before entering the main loop. If any baseline poll fails, the monitor exits immediately.
- **Log**: Each entry has `"baseline": true`.

#### Mode 2: Normal polling

- **When**: After baseline succeeds. This is the steady state.
- **Cadence**: Every 60 seconds.
- **Endpoints polled** (9 total):

| Endpoint | Auth | What it returns |
|---|---|---|
| `system.getInfo` | public | Uptime, firmware version, model |
| `wan.getInfo` | public | WAN connection status |
| `ppp.getInfo` | public | PPP session info |
| `dsl.getInfo` | public | DSL line stats (SNR, attenuation) |
| `ftth.getInfo` | public | FTTH status |
| `ont.getInfo` | public | ONT status |
| `lan.getHostsList` | public | All LAN hosts |
| `wlan.getClientList` | private | 2.4 GHz WiFi clients |
| `wlan5.getClientList` | private | 5 GHz WiFi clients |

- **Auth token refresh**: Every 3600 seconds (1 hour), proactively re-authenticates. If a private endpoint returns an auth error mid-poll, re-authenticates immediately.
- **Repeater tagging**: Any client with MAC `6C:4C:BC:91:DF:A9` gets `"repeater": true` in the log entry, and `repeater_connected` / `repeater_last_seen` are set at the top level.
- **Log**: Each poll produces one JSONL entry with all 9 endpoint results.

#### Mode 3: Crash detection

- **When**: Uptime drops between two consecutive polls (box rebooted without going fully unreachable).
- **What**: Enters rapid polling at **10-second intervals** for up to **5 minutes**:
  - Polls `system.getInfo` + `wlan.getClientList`
  - Checks if uptime is climbing and client count is recovering (≥50% of pre-crash count)
  - Once recovered, exits crash mode and returns to normal polling
- **Notifications**: macOS notification on crash detection and on recovery.
- **Log**: Entries have `"crash_detected": true`, `"rapid_mode": true/false`, `"pre_crash_uptime"`.

#### Mode 4: Unreachable (dead box)

- **When**: All 9 endpoints fail (connection refused/timeout — the box is completely down or network is cut).
- **What**: Escalating backoff in 3 phases:

| Phase | Interval | Duration | Cumulative |
|---|---|---|---|
| 1 | 10 seconds | 2 minutes | 0–2 min |
| 2 | 60 seconds | 10 minutes | 2–12 min |
| 3 | 300 seconds | indefinite | 12 min+ |

- Each phase only tries `system.getInfo` (lightweight check).
- When the box responds again, the monitor checks if uptime reset (crash during outage) and enters crash mode if so.
- **Notifications**: macOS notification on unreachable detection and on recovery.
- **Log**: Entries have `"box_unreachable": true`, `"phase": 1|2|3`.

### Console Output

```
[AUTH] OK — initial token obtained
[BASELINE] Starting 3 baseline polls (10s apart)...
[BASELINE] Poll 1/3 OK
[BASELINE] Poll 2/3 OK
[BASELINE] Poll 3/3 OK
[BASELINE] All 3 baseline polls succeeded — entering main loop
[14:32:01] Poll #4 — OK [REPEATER UP]
[14:33:01] Poll #5 — OK [REPEATER UP]
[AUTH] Token refreshed proactively
[14:34:01] Poll #6 — OK [AUTH REFRESHED] [REPEATER DOWN]
```

---

## 5. Understanding the Logs

Logs are written to `./logs/monitor_YYYY-MM-DD.jsonl` — one file per day, auto-rotated at midnight UTC.

### Structure

Each line is a JSON object. Key fields:

```jsonc
{
  "timestamp": "2026-05-28T14:32:01.234567+00:00",  // UTC ISO 8601
  "poll_count": 42,                                  // sequential counter
  "status": "OK",                                    // "OK", "PARTIAL (N failures)"
  "auth_refreshed": false,
  "repeater_connected": true,
  "repeater_last_seen": "12345",
  "system.getInfo": { /* full API response */ },
  "wan.getInfo": { /* ... */ },
  "dsl.getInfo": { /* ... */ },
  // ... all 9 endpoints
}
```

### Special Entry Types

| Condition | Extra Fields |
|---|---|
| Baseline | `"baseline": true` |
| Crash detected | `"crash_detected": true`, `"rapid_mode": true/false`, `"pre_crash_uptime": 12345` |
| Box unreachable | `"box_unreachable": true`, `"phase": 1/2/3`, `"error": "..."` |
| Partial failure | `"status": "PARTIAL (2 failures)"`, failed endpoints have `"error": "..."` |

### Useful Queries

```bash
# Count polls per day
wc -l logs/monitor_2026-05-2*.jsonl

# Find all crash events
grep '"crash_detected": true' logs/*.jsonl

# Find all unreachable periods
grep '"box_unreachable": true' logs/*.jsonl | head -5

# Extract uptime values over time
cat logs/monitor_2026-05-28.jsonl | python -c "
import sys, json
for line in sys.stdin:
    e = json.loads(line)
    if 'system.getInfo' in e and 'error' not in e['system.getInfo']:
        try:
            uptime = e['system.getInfo']['rsp']['status']['@uptime']
            print(f\"{e['timestamp'][:19]}  uptime={uptime}s\")
        except (KeyError, TypeError):
            pass
"

# Extract DSL SNR over time
cat logs/monitor_2026-05-28.jsonl | python -c "
import sys, json
for line in sys.stdin:
    e = json.loads(line)
    if 'dsl.getInfo' in e and 'error' not in e['dsl.getInfo']:
        try:
            dsl = e['dsl.getInfo']['rsp']['dsl']
            print(f\"{e['timestamp'][:19]}  snr_down={dsl.get('@snr_down','?')}  snr_up={dsl.get('@snr_up','?')}\")
        except (KeyError, TypeError):
            pass
"

# Check repeater connectivity
grep '"repeater_connected": true' logs/*.jsonl | wc -l
grep '"repeater_connected": false' logs/*.jsonl | wc -l
```

---

## 6. First Conclusions (4–7 Days)

After running the monitor continuously for 4–7 days, use this framework to draw your first conclusions.

### Stability

- **Total crash events**: `grep '"crash_detected": true' logs/*.jsonl | wc -l`
  - 0 crashes in a week → box is stable
  - 1–2 crashes → occasional reboot (may be ISP-pushed firmware updates)
  - 3+ crashes → investigate further (power supply, overheating, firmware bug)
- **Total unreachable periods**: `grep '"box_unreachable": true' logs/*.jsonl | wc -l`
  - Were these during specific times? (night? peak hours?)
  - Did they coincide with crash reboots or complete power loss?

### Uptime Pattern

- Extract max uptime before any reset. Was it days? Hours?
- Did the box reboot on a schedule? (Some ISPs push updates overnight)

### DSL/Fibre Line Quality

- **SNR margin** (`dsl.getInfo` → `@snr_down` / `@snr_up`): Stable or fluctuating wildly?
  - SNR > 10 dB = good, < 6 dB = marginal, < 3 dB = problematic
- **Attenuation** (`@atten_down` / `@atten_up`): Should be roughly constant. If it changes, something physical moved.
- **Line rate** (`@rate_down` / `@rate_up`): Compare against your subscribed speed.

### WiFi & Clients

- How many clients on average? (`wlan.getClientList` + `wlan5.getClientList`)
- Did any client repeatedly disconnect/reconnect?
- Repeater (`6C:4C:BC:91:DF:A9`): What percentage of time was it connected?

### WAN Connectivity

- Did `wan.getInfo` ever show a status other than "up"?
- Did `ppp.getInfo` show session resets?

### Template

Fill in after your first run:

```
Period:     YYYY-MM-DD to YYYY-MM-DD (N days)
Total polls: _______
Crashes:    _______
Unreachable: _______ (phases: 1=___, 2=___, 3=___)
Max uptime: _______ hours

SNR down:   min=___ avg=___ max=___ dB
SNR up:     min=___ avg=___ max=___ dB
Line rate:  down=___ Mbps, up=___ Mbps

WiFi clients: avg=___ min=___ max=___
Repeater up:  ___% of polls

Notable events:
- [date/time]: description
- ...

Conclusions:
1. ...
2. ...
3. ...
```

---

## Quick Reference Card

| Task | Command |
|---|---|
| Start monitor | `python monitor.py` |
| Start monitor (background) | `tmux new -s monitor "python monitor.py"` |
| Check monitor is running | `tmux attach -t monitor` (Ctrl+B, D to detach) |
| View live log | `tail -f logs/monitor_$(date -u +%Y-%m-%d).jsonl` |
| Count today's polls | `wc -l logs/monitor_$(date -u +%Y-%m-%d).jsonl` |
| Find crashes | `grep '"crash_detected": true' logs/*.jsonl` |
| Find outages | `grep '"box_unreachable": true' logs/*.jsonl` |
| Launch API client | `cd api-client && python client.py` |
