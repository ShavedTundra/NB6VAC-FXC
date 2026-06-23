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
from xml.etree import ElementTree

import sfr_box

ALL_ENDPOINTS = [
    "system.getInfo", "wan.getInfo",
    "ftth.getInfo", "ont.getInfo", "lan.getHostsList",
    "wlan5.getClientList",
]

CLIENT_LIST_ENDPOINTS = {"wlan5.getClientList", "lan.getHostsList"}


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
    public_endpoints: list[str] = field(default_factory=lambda: [
        "system.getInfo", "wan.getInfo",
        "ftth.getInfo", "ont.getInfo", "lan.getHostsList",
    ])
    private_endpoints: list[str] = field(default_factory=lambda: [
        "wlan5.getClientList",
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
    baseline_polls_done: int = 0
    crash_start: float = 0.0
    pre_crash_uptime: int = 0
    crash_context: dict | None = None
    unreachable_phase: int = 0
    unreachable_phase_start: float = 0.0
    unreachable_error: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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




# ---------------------------------------------------------------------------
# poll_all_endpoints — standalone helper for the 6-endpoint iteration
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
        except ElementTree.ParseError as e:
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
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, ElementTree.ParseError) as e:
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
            unreachable_phase=0, unreachable_phase_start=now_mono,
            unreachable_error=last_error,
        )
        entry = {
            "timestamp": now_utc.isoformat(), "poll_count": new_count,
            "status": "ALL FAILED", "error": last_error,
        }
        return new_state, entry

    # Partial or full success. During a box flap system.getInfo can be a
    # partial failure ({"error": ...}) while other endpoints succeeded; calling
    # extract_uptime on that raises KeyError and kills the monitor (the cause of
    # the Jun 19 death). extract_uptime itself stays strict per ADR-0001 — the
    # caller tolerates the missing value, same pattern as tick_unreachable.
    sys_result = results.get("system.getInfo", {})
    if sys_result.get("rsp", {}).get("@stat") == "ok":
        current_uptime = sfr_box.extract_uptime(sys_result)
    else:
        current_uptime = None
    crash_detected = (
        state.previous_uptime is not None
        and current_uptime is not None
        and current_uptime < state.previous_uptime
    )

    status = f"PARTIAL ({failures} failures)" if failures > 0 else "OK"
    flag = " [AUTH REFRESHED]" if auth_refreshed else ""
    sys_rsp = results.get("system.getInfo", {}).get("rsp", {}).get("system", {})
    temp = f"{int(sys_rsp['@temperature'])/1000:.1f}°C" if sys_rsp.get("@temperature") else "n/a"
    volt = f"{float(sys_rsp['@alimvoltage'])/1000:.2f}V" if sys_rsp.get("@alimvoltage") else "n/a"
    print(f"[{now_utc.strftime('%H:%M:%S')}] Poll #{new_count} — {status} temp={temp} volt={volt}{flag}")

    entry = {
        "timestamp": now_utc.isoformat(), "poll_count": new_count,
        "status": status, "auth_refreshed": auth_refreshed,
        **results,
    }

    if crash_detected:
        # Build crash_context from the last successful normal poll
        sys_rsp = results.get("system.getInfo", {}).get("rsp", {}).get("system", {})
        wan_rsp = results.get("wan.getInfo", {}).get("rsp", {}).get("wan", {})
        lan_rsp = results.get("lan.getHostsList", {}).get("rsp", {})
        hosts = lan_rsp.get("host")
        if isinstance(hosts, list):
            host_count = len(hosts)
        elif isinstance(hosts, dict):
            host_count = 1
        else:
            host_count = 0
        crash_context = {
            "temperature": int(sys_rsp["@temperature"]) if "@temperature" in sys_rsp else None,
            "voltage": float(sys_rsp["@alimvoltage"]) if "@alimvoltage" in sys_rsp else None,
            "uptime": state.previous_uptime,
            "wan_status": wan_rsp.get("@status"),
            "host_count": host_count,
        }
        new_state = MonitorState(
            mode=Mode.CRASH, poll_count=new_count, token=token,
            last_auth_time=last_auth, previous_uptime=None,
            crash_start=now_mono, pre_crash_uptime=state.previous_uptime or 0,
            crash_context=crash_context,
        )
    else:
        new_state = MonitorState(
            mode=Mode.NORMAL, poll_count=new_count, token=token,
            last_auth_time=last_auth,
            previous_uptime=current_uptime if current_uptime is not None else state.previous_uptime,
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
        )
        entry = {
            "timestamp": now_utc.isoformat(), "poll_count": new_count,
            "crash_detected": True, "rapid_mode": False,
            "pre_crash_uptime": state.pre_crash_uptime,
        }
        return new_state, entry

    try:
        system_info = sfr_box.poll_endpoint(config.base_url, "system.getInfo")
        wan_info = sfr_box.poll_endpoint(config.base_url, "wan.getInfo")
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, ElementTree.ParseError) as e:
        entry = {
            "timestamp": now_utc.isoformat(), "poll_count": new_count,
            "crash_detected": True, "rapid_mode": True,
            "pre_crash_uptime": state.pre_crash_uptime, "error": str(e),
        }
        print(f"[CRASH MODE #{new_count}] Error: {e}")
        return replace(state, poll_count=new_count, crash_context=None), entry

    current_uptime = sfr_box.extract_uptime(system_info)
    wan_status = wan_info["rsp"]["wan"]["@status"]
    recovered = current_uptime < 600 and wan_status == "up"

    crash_results: dict[str, dict] = {
        "system.getInfo": system_info,
        "wan.getInfo": wan_info,
    }

    status_tag = "RECOVERING" if not recovered else "RECOVERED"
    print(f"[CRASH MODE #{new_count}] uptime={current_uptime}s wan={wan_status} [{status_tag}]")

    entry: dict = {
        "timestamp": now_utc.isoformat(), "poll_count": new_count,
        "crash_detected": True, "rapid_mode": not recovered,
        "pre_crash_uptime": state.pre_crash_uptime,
        "current_uptime": current_uptime, "wan_status": wan_status,
        **crash_results,
    }
    if state.crash_context is not None:
        entry["crash_context"] = state.crash_context

    if recovered:
        print(f"[CRASH MODE] Recovery detected — fresh uptime {current_uptime}s, WAN is up")
        new_state = MonitorState(
            mode=Mode.NORMAL, poll_count=new_count, token=state.token,
            last_auth_time=state.last_auth_time, previous_uptime=current_uptime,
        )
        return new_state, entry

    return replace(state, poll_count=new_count, crash_context=None), entry


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
    }
    print(f"[UNREACHABLE phase={phase_idx + 1} #{new_count}] {state.unreachable_error}")

    # Probe the box via sfr_box (NOT raw requests)
    try:
        data = sfr_box.poll_endpoint(config.base_url, "system.getInfo")
        if data.get("rsp", {}).get("@stat") == "ok":
            recovered_uptime = sfr_box.extract_uptime(data)
            entry["recovered"] = True

            # Uptime reset → CRASH, else → NORMAL
            if state.previous_uptime is not None and recovered_uptime < state.previous_uptime:
                new_state = MonitorState(
                    mode=Mode.CRASH, poll_count=new_count, token=state.token,
                    last_auth_time=state.last_auth_time,
                    previous_uptime=state.previous_uptime,
                    crash_start=now_mono, pre_crash_uptime=state.previous_uptime,
                )
            else:
                new_token, ok = sfr_box.authenticate(config.base_url, config.username, config.password)
                new_state = MonitorState(
                    mode=Mode.NORMAL, poll_count=new_count,
                    token=new_token if ok else state.token,
                    last_auth_time=now_mono if ok else state.last_auth_time,
                    previous_uptime=recovered_uptime,
                )
            return new_state, entry
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, ElementTree.ParseError) as e:
        return MonitorState(
            mode=Mode.UNREACHABLE, poll_count=new_count, token=state.token,
            last_auth_time=state.last_auth_time,
            previous_uptime=state.previous_uptime,
            unreachable_phase=phase_idx,
            unreachable_phase_start=now_mono if phase_idx != state.unreachable_phase else state.unreachable_phase_start,
            unreachable_error=str(e),
        ), entry

    # Still unreachable — advance phase in state if changed
    return MonitorState(
        mode=Mode.UNREACHABLE, poll_count=new_count, token=state.token,
        last_auth_time=state.last_auth_time,
        previous_uptime=state.previous_uptime,
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

    password = sfr_box.get_password()
    config = MonitorConfig(hostname=args.hostname, username=args.username, password=password)

    token, authenticated = sfr_box.authenticate(config.base_url, config.username, config.password)
    if not authenticated:
        print("ERROR: Authentication failed — cannot start baseline", file=sys.stderr)
        sys.exit(1)
    print("[AUTH] OK — initial token obtained")

    state = MonitorState(
        mode=Mode.BASELINE, poll_count=0, token=token,
        last_auth_time=time.monotonic(),
        previous_uptime=None,
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
