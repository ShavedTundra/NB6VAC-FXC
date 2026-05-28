#!/usr/bin/env python3
"""SFR Box monitor — continuous polling with explicit state machine and pure-function tick() dispatch."""

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

import requests

import sfr_box

ALL_ENDPOINTS = [
    "system.getInfo", "wan.getInfo", "ppp.getInfo", "dsl.getInfo",
    "ftth.getInfo", "ont.getInfo", "lan.getHostsList",
    "wlan.getClientList", "wlan5.getClientList",
]

CLIENT_LIST_ENDPOINTS = {"wlan.getClientList", "wlan5.getClientList", "lan.getHostsList"}


class Mode(Enum):
    BASELINE = "baseline"
    NORMAL = "normal"
    CRASH = "crash"
    UNREACHABLE = "unreachable"


@dataclass
class MonitorConfig:
    hostname: str
    username: str
    password: str
    poll_interval: int = 60
    crash_poll_interval: int = 10
    crash_mode_max_duration: int = 300
    baseline_polls: int = 3
    baseline_interval: int = 10
    auth_refresh_interval: int = 3600
    repeater_mac: str = "6C:4C:BC:91:DF:A9"
    public_endpoints: list[str] = field(default_factory=lambda: [
        "system.getInfo", "wan.getInfo", "ppp.getInfo", "dsl.getInfo",
        "ftth.getInfo", "ont.getInfo", "lan.getHostsList",
    ])
    private_endpoints: list[str] = field(default_factory=lambda: [
        "wlan.getClientList", "wlan5.getClientList",
    ])
    unreachable_phases: list[dict] = field(default_factory=lambda: [
        {"interval": 10, "duration": 120},
        {"interval": 60, "duration": 600},
        {"interval": 300, "duration": None},
    ])

    @property
    def base_url(self) -> str:
        return f"http://{self.hostname}/api/1.0/"


@dataclass
class MonitorState:
    mode: Mode
    poll_count: int
    token: str
    last_auth_time: float
    previous_uptime: int | None
    pre_crash_client_count: int
    baseline_polls_done: int = 0
    crash_start: float = 0.0
    pre_crash_uptime: int = 0
    unreachable_phase: int = 0
    unreachable_phase_start: float = 0.0
    unreachable_error: str = ""


# ---------------------------------------------------------------------------
# Helpers (unchanged logic)
# ---------------------------------------------------------------------------

def get_password() -> str:
    password = os.environ.get("SFR_PASSWORD")
    if password:
        return password
    config_path = Path("config.local.json")
    if config_path.exists():
        with open(config_path) as f:
            config = json.load(f)
        password = config.get("password")
        if password:
            return password
    print("ERROR: No password found. Set SFR_PASSWORD env var or create config.local.json", file=sys.stderr)
    sys.exit(1)


def write_jsonl(entry: dict) -> None:
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_file = log_dir / f"monitor_{today}.jsonl"
    with open(log_file, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def notify_macos(title: str, message: str) -> None:
    if sys.platform != "darwin":
        return
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{message}" with title "{title}"'],
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass


def extract_uptime(system_info: dict) -> int | None:
    try:
        return int(system_info["rsp"]["status"]["@uptime"])
    except (KeyError, ValueError, TypeError):
        return None


def extract_client_count(client_list: dict) -> int:
    try:
        hosts = client_list["rsp"]["clients"]["client"]
        if isinstance(hosts, list):
            return len(hosts)
        return 1
    except (KeyError, TypeError):
        return 0


def tag_repeater_in_results(results: dict[str, dict], repeater_mac: str) -> None:
    for endpoint in CLIENT_LIST_ENDPOINTS:
        if endpoint not in results or "error" in results[endpoint]:
            continue
        try:
            clients_data = results[endpoint]["rsp"]
            if "clients" in clients_data:
                client = clients_data["clients"]["client"]
            elif "hosts" in clients_data:
                client = clients_data["hosts"]["host"]
            else:
                continue
            clients = client if isinstance(client, list) else [client]
            for c in clients:
                mac = c.get("@mac", c.get("@MAC", "")).upper()
                if mac == repeater_mac:
                    c["repeater"] = True
        except (KeyError, TypeError):
            continue


def find_repeater_status(results: dict[str, dict], repeater_mac: str) -> tuple[bool, str | None]:
    last_seen: str | None = None
    for endpoint in CLIENT_LIST_ENDPOINTS:
        if endpoint not in results or "error" in results[endpoint]:
            continue
        try:
            clients_data = results[endpoint]["rsp"]
            if "clients" in clients_data:
                client = clients_data["clients"]["client"]
            elif "hosts" in clients_data:
                client = clients_data["hosts"]["host"]
            else:
                continue
            clients = client if isinstance(client, list) else [client]
            for c in clients:
                mac = c.get("@mac", c.get("@MAC", "")).upper()
                if mac == repeater_mac:
                    ts = c.get("@last_seen", c.get("@assoc_time"))
                    if ts is not None:
                        last_seen = ts
                    return True, last_seen
        except (KeyError, TypeError):
            continue
    return False, None


# ---------------------------------------------------------------------------
# poll_all_endpoints — standalone helper for the 9-endpoint iteration
# ---------------------------------------------------------------------------

def poll_all_endpoints(
    base_url: str,
    token: str,
    config: MonitorConfig,
) -> tuple[dict[str, dict], int, str, str, bool]:
    """Poll all endpoints with inline auth-retry.

    Returns (results, failure_count, last_error, token, auth_refreshed).
    """
    results: dict[str, dict] = {}
    failures = 0
    last_error = ""
    auth_refreshed = False

    for endpoint in ALL_ENDPOINTS:
        needs_token = endpoint in config.private_endpoints
        try:
            results[endpoint] = sfr_box.poll_endpoint(
                base_url, endpoint, token if needs_token else None,
            )
            resp_stat = results[endpoint].get("rsp", {}).get("@stat")
            if resp_stat and resp_stat != "ok" and needs_token:
                new_token, ok = sfr_box.authenticate(base_url, config.username, config.password)
                if ok:
                    token = new_token
                    auth_refreshed = True
                    results[endpoint] = sfr_box.poll_endpoint(base_url, endpoint, token)
        except requests.exceptions.ConnectionError as e:
            results[endpoint] = {"error": str(e)}
            failures += 1
            last_error = str(e)
        except requests.exceptions.Timeout as e:
            results[endpoint] = {"error": str(e)}
            failures += 1
            last_error = str(e)
        except Exception as e:
            results[endpoint] = {"error": str(e)}
            failures += 1
            last_error = str(e)

    return results, failures, last_error, token, auth_refreshed


# ---------------------------------------------------------------------------
# Tick functions — one per mode, pure data in/out
# ---------------------------------------------------------------------------

def tick_baseline(
    state: MonitorState, config: MonitorConfig,
    now_utc: datetime, now_mono: float,
) -> tuple[MonitorState, dict]:
    new_count = state.poll_count + 1

    try:
        system_info = sfr_box.poll_endpoint(config.base_url, "system.getInfo")
    except Exception as e:
        print(f"[BASELINE] Poll FAILED: {e}", file=sys.stderr)
        print("ERROR: Baseline failed — API unreachable or auth error. Exiting.", file=sys.stderr)
        sys.exit(1)

    resp_stat = system_info.get("rsp", {}).get("@stat")
    if resp_stat != "ok":
        print(f"[BASELINE] Poll FAILED: stat={resp_stat}", file=sys.stderr)
        print("ERROR: Baseline failed — API returned error. Exiting.", file=sys.stderr)
        sys.exit(1)

    polls_done = state.baseline_polls_done + 1
    print(f"[BASELINE] Poll {polls_done}/{config.baseline_polls} OK")

    entry: dict = {
        "timestamp": now_utc.isoformat(),
        "poll_count": new_count,
        "baseline": True,
        "system.getInfo": system_info,
    }

    if polls_done >= config.baseline_polls:
        new_state = replace(state, poll_count=new_count, baseline_polls_done=polls_done, mode=Mode.NORMAL)
    else:
        new_state = replace(state, poll_count=new_count, baseline_polls_done=polls_done)

    return new_state, entry


def tick_normal(
    state: MonitorState, config: MonitorConfig,
    now_utc: datetime, now_mono: float,
) -> tuple[MonitorState, dict]:
    new_count = state.poll_count + 1
    auth_refreshed = False
    token = state.token

    # Proactive auth refresh
    if now_mono - state.last_auth_time >= config.auth_refresh_interval:
        new_token, ok = sfr_box.authenticate(config.base_url, config.username, config.password)
        if ok:
            token = new_token
            auth_refreshed = True

    # Poll all endpoints
    results, failures, last_error, token, poll_auth = poll_all_endpoints(
        config.base_url, token, config,
    )
    auth_refreshed = auth_refreshed or poll_auth
    last_auth = now_mono if auth_refreshed else state.last_auth_time

    # All failed → UNREACHABLE
    if failures == len(ALL_ENDPOINTS):
        new_state = MonitorState(
            mode=Mode.UNREACHABLE, poll_count=new_count, token=token,
            last_auth_time=last_auth, previous_uptime=state.previous_uptime,
            pre_crash_client_count=state.pre_crash_client_count,
            unreachable_phase=0, unreachable_phase_start=now_mono,
            unreachable_error=last_error,
        )
        entry = {
            "timestamp": now_utc.isoformat(), "poll_count": new_count,
            "status": "ALL FAILED", "error": last_error,
        }
        return new_state, entry

    # Partial or full success
    tag_repeater_in_results(results, config.repeater_mac)
    repeater_connected, repeater_last_seen = find_repeater_status(results, config.repeater_mac)

    current_uptime = extract_uptime(results.get("system.getInfo", {}))
    crash_detected = (
        state.previous_uptime is not None
        and current_uptime is not None
        and current_uptime < state.previous_uptime
    )

    pre_crash_client_count = state.pre_crash_client_count
    for ep in ("wlan.getClientList", "wlan5.getClientList"):
        if ep in results and "error" not in results[ep]:
            pre_crash_client_count = extract_client_count(results[ep])
            break

    status = f"PARTIAL ({failures} failures)" if failures > 0 else "OK"
    flag = " [AUTH REFRESHED]" if auth_refreshed else ""
    rep_flag = " [REPEATER UP]" if repeater_connected else " [REPEATER DOWN]"
    print(f"[{now_utc.strftime('%H:%M:%S')}] Poll #{new_count} — {status}{flag}{rep_flag}")

    entry = {
        "timestamp": now_utc.isoformat(), "poll_count": new_count,
        "status": status, "auth_refreshed": auth_refreshed,
        "repeater_connected": repeater_connected,
        "repeater_last_seen": repeater_last_seen,
        **results,
    }

    if crash_detected:
        new_state = MonitorState(
            mode=Mode.CRASH, poll_count=new_count, token=token,
            last_auth_time=last_auth, previous_uptime=None,
            pre_crash_client_count=pre_crash_client_count,
            crash_start=now_mono, pre_crash_uptime=state.previous_uptime or 0,
        )
    else:
        new_state = MonitorState(
            mode=Mode.NORMAL, poll_count=new_count, token=token,
            last_auth_time=last_auth,
            previous_uptime=current_uptime if current_uptime is not None else state.previous_uptime,
            pre_crash_client_count=pre_crash_client_count,
        )

    return new_state, entry


def tick_crash(
    state: MonitorState, config: MonitorConfig,
    now_utc: datetime, now_mono: float,
) -> tuple[MonitorState, dict]:
    new_count = state.poll_count + 1

    # Timeout → back to NORMAL
    if now_mono - state.crash_start >= config.crash_mode_max_duration:
        print(f"[CRASH MODE] {config.crash_mode_max_duration}s cap reached, returning to normal polling")
        new_state = MonitorState(
            mode=Mode.NORMAL, poll_count=new_count, token=state.token,
            last_auth_time=state.last_auth_time, previous_uptime=None,
            pre_crash_client_count=state.pre_crash_client_count,
        )
        entry = {
            "timestamp": now_utc.isoformat(), "poll_count": new_count,
            "crash_detected": True, "rapid_mode": False,
            "pre_crash_uptime": state.pre_crash_uptime,
        }
        return new_state, entry

    try:
        system_info = sfr_box.poll_endpoint(config.base_url, "system.getInfo")
        client_list = sfr_box.poll_endpoint(config.base_url, "wlan.getClientList", state.token)
    except Exception as e:
        entry = {
            "timestamp": now_utc.isoformat(), "poll_count": new_count,
            "crash_detected": True, "rapid_mode": True,
            "pre_crash_uptime": state.pre_crash_uptime, "error": str(e),
        }
        print(f"[CRASH MODE #{new_count}] Error: {e}")
        return replace(state, poll_count=new_count), entry

    current_uptime = extract_uptime(system_info)
    current_clients = extract_client_count(client_list)
    uptime_climbing = current_uptime is not None and current_uptime > 0
    clients_returning = current_clients >= (state.pre_crash_client_count * 0.5)
    recovered = uptime_climbing and clients_returning

    crash_results: dict[str, dict] = {
        "system.getInfo": system_info,
        "wlan.getClientList": client_list,
    }
    tag_repeater_in_results(crash_results, config.repeater_mac)
    repeater_connected, repeater_last_seen = find_repeater_status(crash_results, config.repeater_mac)

    status_tag = "RECOVERING" if not recovered else "RECOVERED"
    print(f"[CRASH MODE #{new_count}] uptime={current_uptime}s clients={current_clients} [{status_tag}]")

    entry = {
        "timestamp": now_utc.isoformat(), "poll_count": new_count,
        "crash_detected": True, "rapid_mode": not recovered,
        "pre_crash_uptime": state.pre_crash_uptime,
        "current_uptime": current_uptime, "current_clients": current_clients,
        "repeater_connected": repeater_connected,
        "repeater_last_seen": repeater_last_seen,
        **crash_results,
    }

    if recovered:
        print(f"[CRASH MODE] Recovery detected — uptime climbing, {current_clients} clients back")
        new_state = MonitorState(
            mode=Mode.NORMAL, poll_count=new_count, token=state.token,
            last_auth_time=state.last_auth_time, previous_uptime=current_uptime,
            pre_crash_client_count=current_clients,
        )
        return new_state, entry

    return replace(state, poll_count=new_count), entry


def tick_unreachable(
    state: MonitorState, config: MonitorConfig,
    now_utc: datetime, now_mono: float,
) -> tuple[MonitorState, dict]:
    new_count = state.poll_count + 1
    phase_idx = state.unreachable_phase
    phases = config.unreachable_phases

    # Phase advancement check
    phase = phases[phase_idx]
    phase_duration = phase["duration"]
    if phase_duration is not None and (now_mono - state.unreachable_phase_start) >= phase_duration:
        next_phase = phase_idx + 1
        if next_phase < len(phases):
            phase_idx = next_phase
            phase = phases[phase_idx]

    entry: dict = {
        "timestamp": now_utc.isoformat(), "poll_count": new_count,
        "box_unreachable": True, "phase": phase_idx + 1,
        "error": state.unreachable_error,
        "repeater_connected": False, "repeater_last_seen": None,
    }
    print(f"[UNREACHABLE phase={phase_idx + 1} #{new_count}] {state.unreachable_error}")

    # Probe the box via sfr_box (NOT raw requests)
    try:
        data = sfr_box.poll_endpoint(config.base_url, "system.getInfo")
        if data.get("rsp", {}).get("@stat") == "ok":
            recovered_uptime = int(data["rsp"]["status"]["@uptime"])
            entry["recovered"] = True

            # Uptime reset → CRASH, else → NORMAL
            if state.previous_uptime is not None and recovered_uptime < state.previous_uptime:
                new_state = MonitorState(
                    mode=Mode.CRASH, poll_count=new_count, token=state.token,
                    last_auth_time=state.last_auth_time,
                    previous_uptime=state.previous_uptime,
                    pre_crash_client_count=state.pre_crash_client_count,
                    crash_start=now_mono, pre_crash_uptime=state.previous_uptime,
                )
            else:
                new_token, ok = sfr_box.authenticate(config.base_url, config.username, config.password)
                new_state = MonitorState(
                    mode=Mode.NORMAL, poll_count=new_count,
                    token=new_token if ok else state.token,
                    last_auth_time=now_mono if ok else state.last_auth_time,
                    previous_uptime=recovered_uptime,
                    pre_crash_client_count=state.pre_crash_client_count,
                )
            return new_state, entry
    except Exception as e:
        return MonitorState(
            mode=Mode.UNREACHABLE, poll_count=new_count, token=state.token,
            last_auth_time=state.last_auth_time,
            previous_uptime=state.previous_uptime,
            pre_crash_client_count=state.pre_crash_client_count,
            unreachable_phase=phase_idx,
            unreachable_phase_start=now_mono if phase_idx != state.unreachable_phase else state.unreachable_phase_start,
            unreachable_error=str(e),
        ), entry

    # Still unreachable — advance phase in state if changed
    return MonitorState(
        mode=Mode.UNREACHABLE, poll_count=new_count, token=state.token,
        last_auth_time=state.last_auth_time,
        previous_uptime=state.previous_uptime,
        pre_crash_client_count=state.pre_crash_client_count,
        unreachable_phase=phase_idx,
        unreachable_phase_start=now_mono if phase_idx != state.unreachable_phase else state.unreachable_phase_start,
        unreachable_error=state.unreachable_error,
    ), entry


# ---------------------------------------------------------------------------
# Core dispatch
# ---------------------------------------------------------------------------

def tick(
    state: MonitorState, config: MonitorConfig,
    now_utc: datetime, now_mono: float,
) -> tuple[MonitorState, dict]:
    dispatch = {
        Mode.BASELINE: tick_baseline,
        Mode.NORMAL: tick_normal,
        Mode.CRASH: tick_crash,
        Mode.UNREACHABLE: tick_unreachable,
    }
    return dispatch[state.mode](state, config, now_utc, now_mono)


# ---------------------------------------------------------------------------
# Sleep interval and transition side-effects
# ---------------------------------------------------------------------------

def get_sleep_interval(state: MonitorState, config: MonitorConfig) -> float:
    if state.mode == Mode.BASELINE:
        return config.baseline_interval
    if state.mode == Mode.NORMAL:
        return config.poll_interval
    if state.mode == Mode.CRASH:
        return config.crash_poll_interval
    if state.mode == Mode.UNREACHABLE:
        phase = config.unreachable_phases[state.unreachable_phase]
        return phase["interval"]
    return config.poll_interval


def on_transition(prev_mode: Mode, new_mode: Mode, state: MonitorState) -> None:
    if prev_mode == new_mode:
        return

    if new_mode == Mode.CRASH:
        print(f"\n{'='*60}")
        print(f"  *** CRASH DETECTED ***  Uptime reset from {state.pre_crash_uptime}s")
        print(f"  Entering rapid polling mode")
        print(f"{'='*60}\n")
        notify_macos("SFR Box", "Crash detected — uptime reset")

    elif new_mode == Mode.UNREACHABLE:
        print(f"\n{'='*60}")
        print(f"  *** BOX UNREACHABLE ***  All API calls failing")
        print(f"  Entering unreachable mode with escalating backoff")
        print(f"{'='*60}\n")
        notify_macos("SFR Box", "Box unreachable — all API calls failing")

    elif new_mode == Mode.NORMAL:
        if prev_mode == Mode.BASELINE:
            print(f"[BASELINE] All baseline polls succeeded — entering main loop")
        elif prev_mode == Mode.CRASH:
            notify_macos("SFR Box", "Box recovered from crash")
        elif prev_mode == Mode.UNREACHABLE:
            print(f"[UNREACHABLE] Box is back online!")
            notify_macos("SFR Box", "Box back online")


# ---------------------------------------------------------------------------
# main — owns the while-loop, sleep, and logging
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="SFR Box continuous monitor")
    parser.add_argument("--hostname", default="192.168.1.1", help="Box hostname or IP (default: 192.168.1.1)")
    parser.add_argument("--username", default="admin", help="Username for auth (default: admin)")
    args = parser.parse_args()

    password = get_password()
    config = MonitorConfig(hostname=args.hostname, username=args.username, password=password)

    token, authenticated = sfr_box.authenticate(config.base_url, config.username, config.password)
    if not authenticated:
        print("ERROR: Authentication failed — cannot start baseline", file=sys.stderr)
        sys.exit(1)
    print("[AUTH] OK — initial token obtained")

    state = MonitorState(
        mode=Mode.BASELINE, poll_count=0, token=token,
        last_auth_time=time.monotonic(),
        previous_uptime=None, pre_crash_client_count=0,
    )
    print(f"[BASELINE] Starting {config.baseline_polls} baseline polls ({config.baseline_interval}s apart)...")

    shutdown_event = threading.Event()

    def handle_signal(signum: int, _frame: object) -> None:
        shutdown_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    while not shutdown_event.is_set():
        prev_mode = state.mode
        now_utc = datetime.now(timezone.utc)
        now_mono = time.monotonic()
        state, entry = tick(state, config, now_utc, now_mono)
        write_jsonl(entry)
        on_transition(prev_mode, state.mode, state)
        shutdown_event.wait(timeout=get_sleep_interval(state, config))

    write_jsonl({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "poll_count": state.poll_count,
        "status": "shutdown",
    })
    print(f"[SHUTDOWN] Clean exit after {state.poll_count} polls")


if __name__ == "__main__":
    main()
