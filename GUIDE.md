# NB6VAC-FXC — User Guide

Complete guide to set up, run, and draw conclusions from the SFR Box monitoring tools.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Configuration](#2-configuration)
3. [API Client](#3-api-client)
4. [Monitor](#4-monitor)
5. [Reboot Tool](#5-reboot-tool)
6. [Understanding the Logs](#6-understanding-the-logs)
7. [First Conclusions (4–7 Days)](#7-first-conclusions-47-days)

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

> **Note:** `dsl.getInfo` and `ppp.getInfo` are dead endpoints for FTTH boxes — they timeout or return empty data. They are excluded from the monitor but remain available in the API client for ad-hoc exploration.

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

### Daemon Mode (macOS launchd)

The project includes a launchd plist template for running the monitor as a macOS background service that auto-starts at login and auto-restarts on crash.

> **Requirement**: Daemon mode uses `config.local.json` for the password (see §2, Method B). There is no `SFR_PASSWORD` environment variable in the plist — launchd does not inject shell env vars.

#### Install

1. Generate the personalized plist from the template (replace paths as needed):

```bash
sed -e "s|__PROJECT_DIR__|$(pwd)|g" \
    -e "s|__PYTHON_PATH__|$(which python)|g" \
    com.shavedtundra.sfr-monitor.plist.template \
    > ~/Library/LaunchAgents/com.shavedtundra.sfr-monitor.plist
```

2. Make sure `config.local.json` exists in the project directory and `logs/` is writable.

3. Load the service:

```bash
launchctl load ~/Library/LaunchAgents/com.shavedtundra.sfr-monitor.plist
```

#### Uninstall

```bash
launchctl unload ~/Library/LaunchAgents/com.shavedtundra.sfr-monitor.plist
```

#### Check status

```bash
launchctl list | grep sfr-monitor
```

The second field is the PID if running, or the last exit code if stopped.

#### Tail logs

```bash
# Monitor JSONL output (via stdout capture)
tail -f logs/monitor_stdout.log

# Check for errors
tail -f logs/monitor_stderr.log

# The monitor's own JSONL logs (recommended)
tail -f logs/monitor_$(date -u +%Y-%m-%d).jsonl
```

#### Notes

- `RunAtLoad=true` — starts automatically at login.
- `KeepAlive=true` — launchd auto-restarts the monitor on crash (with built-in throttling for crash-loops).
- The monitor handles `SIGTERM` cleanly (writes a shutdown entry to JSONL and exits) — `launchctl unload` triggers a graceful shutdown.

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
- **Endpoints polled** (6 active endpoints):

| Endpoint | Auth | What it returns |
|---|---|---|
| `system.getInfo` | public | Uptime, temperature, voltage, firmware version |
| `wan.getInfo` | public | WAN connection status |
| `ftth.getInfo` | public | FTTH status |
| `ont.getInfo` | public | ONT (fiber terminal) status |
| `lan.getHostsList` | public | All LAN hosts |
| `wlan5.getClientList` | private | 5 GHz WiFi clients |

> **Dead endpoints** (not polled — FTTH box has no DSL/PPPoE, and 2.4 GHz has no clients): `dsl.getInfo`, `ppp.getInfo`, `wlan.getClientList`.

- **Auth token refresh**: Every 3600 seconds (1 hour), proactively re-authenticates. If a private endpoint returns an auth error mid-poll, re-authenticates immediately.
- **Log**: Each poll produces one JSONL entry with all 6 endpoint results.

#### Mode 3: Crash detection

- **When**: Uptime drops between two consecutive polls (box rebooted without going fully unreachable).
- **What**: Enters rapid polling at **10-second intervals** for up to **5 minutes**:
  - Polls `system.getInfo` + `wan.getInfo`
  - Declares recovery when uptime < 600s AND `wan.getInfo` reports status `"up"`
  - Once recovered, exits crash mode and returns to normal polling
- **Crash context**: On detection, the last-known-good vitals (temperature, voltage, uptime, WAN status, host count) are captured and logged as structured data in the first CRASH-mode entry.
- **Notifications**: macOS notification on crash detection and on recovery.
- **Log**: Entries have `"crash_detected": true`, `"rapid_mode": true/false`, `"pre_crash_uptime"`. First entry includes `"crash_context"` with last-known-good vitals.

#### Mode 4: Unreachable (dead box)

- **When**: All 6 endpoints fail (connection refused/timeout — the box is completely down or network is cut).
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
[14:32:01] Poll #4 — OK
[14:33:01] Poll #5 — OK
[AUTH] Token refreshed proactively
[14:34:01] Poll #6 — OK [AUTH REFRESHED]
```

---

## 5. Reboot Tool

A CLI tool to reboot the SFR Box via the API (soft reboot) or a smart plug (hard power cycle). Also supports scheduled reboot mode — only reboots if uptime exceeds a threshold.

### Launch

```bash
# Soft reboot (via API)
python reboot.py --hostname 192.168.1.1

# Hard reboot (via smart plug — requires SMART_PLUG_IP)
SMART_PLUG_IP=192.168.1.50 python reboot.py --hard

# Scheduled: only reboot if uptime > 18 hours
python reboot.py --scheduled

# Hard scheduled: power cycle only if uptime > 18 hours
python reboot.py --hard --scheduled --smart-plug-ip 192.168.1.50
```

### CLI Options

| Flag | Default | Description |
|---|---|---|
| `--hostname` | `192.168.1.1` | Box IP or hostname |
| `--username` | `admin` | Auth username |
| `--password` | *(from env/config)* | Box password (default: from `SFR_PASSWORD` or `config.local.json`) |
| `--hard` | off | Hard power cycle via smart plug instead of API reboot |
| `--scheduled` | off | Only reboot if uptime > 18h threshold |
| `--smart-plug-ip` | *(from env)* | Smart plug IP (or set `SMART_PLUG_IP` env var) |

### How It Works

1. **Authentication** — Authenticates with the box and obtains a token (same flow as the monitor).
2. **Uptime check** — Reads current uptime via `system.getInfo`. In `--scheduled` mode, exits early if uptime is below the 18h threshold (or if uptime is unreadable, it skips to be safe).
3. **Reboot** — One of two paths:
   - **Soft reboot** (`--hard` not set): Sends `system.reboot` via POST with the auth token. The box handles the restart internally.
   - **Hard reboot** (`--hard`): Uses the smart plug to cut power for 30 seconds, then restores it. After power-on, waits up to 180 seconds for the box to respond to ping.

### Smart Plug Support

The hard reboot mode uses `smart_plug.py`, which automatically detects the plug brand by trying each protocol in order:

| Brand | Protocol | URL pattern |
|---|---|---|
| Shelly Gen3 | RPC | `http://{ip}/rpc/Switch.Set?id=0&on=true` |
| Shelly Gen1 | REST | `http://{ip}/relay/0?turn=on` |
| Tasmota | HTTP | `http://{ip}/cm?cmnd=Power%20On` |

No configuration needed — just provide the plug's IP address and the script auto-detects which protocol works.

### Scheduled Reboot (Cron)

Combine with cron for automatic preventive reboots:

```bash
# Reboot every 3 days at 04:00 if uptime > 18 hours
0 4 */3 * * cd /path/to/NB6VAC-FXC && python reboot.py --scheduled >> logs/reboot.log 2>&1

# Hard power cycle weekly at 03:00 if uptime > 18 hours
0 3 * * 0 cd /path/to/NB6VAC-FXC && python reboot.py --hard --scheduled --smart-plug-ip 192.168.1.50 >> logs/reboot.log 2>&1
```

---

## 6. Understanding the Logs

Logs are written to `./logs/monitor_YYYY-MM-DD.jsonl` — one file per day, auto-rotated at midnight UTC.

### Structure

Each line is a JSON object. Key fields:

```jsonc
{
  "timestamp": "2026-05-28T14:32:01.234567+00:00",  // UTC ISO 8601
  "poll_count": 42,                                  // sequential counter
  "status": "OK",                                    // "OK", "PARTIAL (N failures)"
  "auth_refreshed": false,
  "system.getInfo": { /* full API response */ },
  "wan.getInfo": { /* ... */ },
  "ftth.getInfo": { /* ... */ },
  "ont.getInfo": { /* ... */ },
  "lan.getHostsList": { /* ... */ },
  "wlan5.getClientList": { /* ... */ }
}
```

### Special Entry Types

| Condition | Extra Fields |
|---|---|
| Baseline | `"baseline": true` |
| Crash detected | `"crash_detected": true`, `"rapid_mode": true/false`, `"pre_crash_uptime": 12345`, `"crash_context": {...}` |
| Box unreachable | `"box_unreachable": true`, `"phase": 1/2/3`, `"error": "..."` |
| Partial failure | `"status": "PARTIAL (2 failures)"`, failed endpoints have `"error": "..."` |

### Useful Queries

```bash
# Count polls per day
wc -l logs/monitor_2026-05-2*.jsonl

# Find all crash events
grep '"crash_detected": true' logs/*.jsonl

# Find all crash context entries (last-known-good vitals)
grep '"crash_context"' logs/*.jsonl

# Find all unreachable periods
grep '"box_unreachable": true' logs/*.jsonl | head -5

# Extract uptime values over time
cat logs/monitor_2026-05-28.jsonl | python -c "
import sys, json
for line in sys.stdin:
    e = json.loads(line)
    if 'system.getInfo' in e and 'error' not in e['system.getInfo']:
        try:
            uptime = e['system.getInfo']['rsp']['system']['@uptime']
            print(f\"{e['timestamp'][:19]}  uptime={uptime}s\")
        except (KeyError, TypeError):
            pass
"

# Extract temperature and voltage over time
cat logs/monitor_2026-05-28.jsonl | python -c "
import sys, json
for line in sys.stdin:
    e = json.loads(line)
    if 'system.getInfo' in e and 'error' not in e['system.getInfo']:
        try:
            sys = e['system.getInfo']['rsp']['system']
            print(f\"{e['timestamp'][:19]}  temp={sys.get('@temperature','?')}°C  volt={sys.get('@alimvoltage','?')}V\")
        except (KeyError, TypeError):
            pass
"

# Extract WAN status over time
cat logs/monitor_2026-05-28.jsonl | python -c "
import sys, json
for line in sys.stdin:
    e = json.loads(line)
    if 'wan.getInfo' in e and 'error' not in e['wan.getInfo']:
        try:
            wan = e['wan.getInfo']['rsp']['wan']['@status']
            print(f\"{e['timestamp'][:19]}  wan={wan}\")
        except (KeyError, TypeError):
            pass
"
```

---

## 7. First Conclusions (4–7 Days)

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

### FTTH/Fibre Line Quality

- **ONT status** (`ont.getInfo`): Should show stable uptime (typically 100+ days). ONT stays up through box crashes.
- **FTTH status** (`ftth.getInfo`): Check for any link status changes.
- > **Note:** DSL/PPPoE endpoints are dead for FTTH boxes and not polled. Use the API client (`api-client/client.py`) for ad-hoc DSL queries if needed.

### WiFi & Clients

- How many clients on average? (`wlan5.getClientList` for 5 GHz + `lan.getHostsList` for all LAN hosts)
- Did any client repeatedly disconnect/reconnect?
- > **Note:** The repeater is invisible to the box API due to MAC address translation on its backhaul. If the TV is online in `lan.getHostsList`, the repeater is working.

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

WiFi clients (5 GHz): avg=___ min=___ max=___
LAN hosts:          avg=___ min=___ max=___

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
| Find crash context | `grep '"crash_context"' logs/*.jsonl` |
| Find outages | `grep '"box_unreachable": true' logs/*.jsonl` |
| Soft reboot | `python reboot.py` |
| Hard reboot (smart plug) | `python reboot.py --hard --smart-plug-ip 192.168.1.50` |
| Scheduled reboot (uptime > 18h) | `python reboot.py --scheduled` |
| Launch API client | `cd api-client && python client.py` |
