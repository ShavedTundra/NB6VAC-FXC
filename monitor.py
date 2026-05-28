#!/usr/bin/env python3
"""SFR Box monitor — continuous polling with crash detection, unreachable handling, and repeater tracking."""

import argparse
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

import requests

BASE_URL_TEMPLATE = "http://{hostname}/api/1.0/"

PUBLIC_ENDPOINTS = [
    "system.getInfo",
    "wan.getInfo",
    "ppp.getInfo",
    "dsl.getInfo",
    "ftth.getInfo",
    "ont.getInfo",
    "lan.getHostsList",
]

PRIVATE_ENDPOINTS = [
    "wlan.getClientList",
    "wlan5.getClientList",
]

ALL_ENDPOINTS = PUBLIC_ENDPOINTS + PRIVATE_ENDPOINTS

CLIENT_LIST_ENDPOINTS = {"wlan.getClientList", "wlan5.getClientList", "lan.getHostsList"}

AUTH_REFRESH_INTERVAL = 3600
POLL_INTERVAL = 60

CRASH_POLL_INTERVAL = 10
CRASH_MODE_MAX_DURATION = 300

UNREACHABLE_PHASES = [
    {"interval": 10, "duration": 120},
    {"interval": 60, "duration": 600},
    {"interval": 300, "duration": None},
]

BASELINE_POLLS = 3
BASELINE_INTERVAL = 10

REPEATER_MAC = "6C:4C:BC:91:DF:A9"


def etree_to_dict(t: ElementTree.Element) -> dict:
    d: dict = {t.tag: {} if t.attrib else None}
    children = list(t)
    if children:
        dd: dict = defaultdict(list)
        for dc in map(etree_to_dict, children):
            for k, v in dc.items():
                dd[k].append(v)
        d = {t.tag: {k: v[0] if len(v) == 1 else v for k, v in dd.items()}}
    if t.attrib:
        d[t.tag].update(("@" + k, v) for k, v in t.attrib.items())
    if t.text:
        text = t.text.strip()
        if children or t.attrib:
            if text:
                d[t.tag]["#text"] = text
        else:
            d[t.tag] = text
    return d


def parse_xml_response(content: bytes) -> dict:
    return etree_to_dict(ElementTree.fromstring(content))


def compute_auth_hash(token: str, username: str, password: str) -> str:
    fh1 = hashlib.sha256(username.encode()).hexdigest()
    key_hash1 = hmac.new(token.encode(), msg=fh1.encode(), digestmod=hashlib.sha256).hexdigest()
    fh2 = hashlib.sha256(password.encode()).hexdigest()
    key_hash2 = hmac.new(token.encode(), msg=fh2.encode(), digestmod=hashlib.sha256).hexdigest()
    return key_hash1 + key_hash2


def authenticate(base_url: str, username: str, password: str) -> tuple[str, bool]:
    r = requests.get(f"{base_url}?method=auth.getToken", timeout=10)
    data = parse_xml_response(r.content)
    token = data["rsp"]["auth"]["@token"]

    key_hash = compute_auth_hash(token, username, password)
    r = requests.get(f"{base_url}?method=auth.checkToken&token={token}&hash={key_hash}", timeout=10)
    data = parse_xml_response(r.content)

    if data["rsp"]["@stat"] != "ok":
        return token, False
    return token, True


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


def poll_endpoint(base_url: str, method: str, token: str | None = None) -> dict:
    url = f"{base_url}?method={method}"
    if token:
        url += f"&token={token}"
    r = requests.get(url, timeout=10)
    return parse_xml_response(r.content)


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


def tag_repeater_in_results(results: dict[str, dict]) -> None:
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
                if mac == REPEATER_MAC:
                    c["repeater"] = True
        except (KeyError, TypeError):
            continue


def find_repeater_status(results: dict[str, dict]) -> tuple[bool, str | None]:
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
                if mac == REPEATER_MAC:
                    ts = c.get("@last_seen", c.get("@assoc_time"))
                    if ts is not None:
                        last_seen = ts
                    return True, last_seen
        except (KeyError, TypeError):
            continue
    return False, None


def run_baseline(
    base_url: str,
    token: str,
    poll_count: int,
) -> int:
    print(f"[BASELINE] Starting {BASELINE_POLLS} baseline polls ({BASELINE_INTERVAL}s apart)...")
    for i in range(BASELINE_POLLS):
        poll_count += 1
        now = datetime.now(timezone.utc)

        try:
            system_info = poll_endpoint(base_url, "system.getInfo")
        except Exception as e:
            print(f"[BASELINE] Poll {i+1}/{BASELINE_POLLS} FAILED: {e}", file=sys.stderr)
            print(f"ERROR: Baseline failed — API unreachable or auth error. Exiting.", file=sys.stderr)
            sys.exit(1)

        resp_stat = system_info.get("rsp", {}).get("@stat")
        if resp_stat != "ok":
            print(f"[BASELINE] Poll {i+1}/{BASELINE_POLLS} FAILED: stat={resp_stat}", file=sys.stderr)
            print(f"ERROR: Baseline failed — API returned error. Exiting.", file=sys.stderr)
            sys.exit(1)

        entry = {
            "timestamp": now.isoformat(),
            "poll_count": poll_count,
            "baseline": True,
            "system.getInfo": system_info,
        }
        write_jsonl(entry)
        print(f"[BASELINE] Poll {i+1}/{BASELINE_POLLS} OK")

        if i < BASELINE_POLLS - 1:
            time.sleep(BASELINE_INTERVAL)

    print(f"[BASELINE] All {BASELINE_POLLS} baseline polls succeeded — entering main loop")
    return poll_count


def run_crash_mode(
    base_url: str,
    token: str,
    username: str,
    password: str,
    pre_crash_uptime: int,
    pre_crash_client_count: int,
    poll_count: int,
) -> int:
    print(f"\n{'='*60}")
    print(f"  *** CRASH DETECTED ***  Uptime reset from {pre_crash_uptime}s")
    print(f"  Entering rapid polling mode (10s interval, max 5 min)")
    print(f"{'='*60}\n")
    notify_macos("SFR Box", "Crash detected — uptime reset")

    crash_start = time.monotonic()
    while True:
        elapsed = time.monotonic() - crash_start
        if elapsed >= CRASH_MODE_MAX_DURATION:
            print(f"[CRASH MODE] 5-minute cap reached, returning to normal polling")
            break

        poll_count += 1
        now = datetime.now(timezone.utc)

        try:
            system_info = poll_endpoint(base_url, "system.getInfo")
            client_list = poll_endpoint(base_url, "wlan.getClientList", token)
        except Exception as e:
            entry = {
                "timestamp": now.isoformat(),
                "poll_count": poll_count,
                "crash_detected": True,
                "rapid_mode": True,
                "pre_crash_uptime": pre_crash_uptime,
                "error": str(e),
            }
            write_jsonl(entry)
            print(f"[CRASH MODE #{poll_count}] Error: {e}")
            time.sleep(CRASH_POLL_INTERVAL)
            continue

        current_uptime = extract_uptime(system_info)
        current_clients = extract_client_count(client_list)

        uptime_climbing = current_uptime is not None and current_uptime > 0
        clients_returning = current_clients >= (pre_crash_client_count * 0.5)

        recovered = uptime_climbing and clients_returning

        crash_results: dict[str, dict] = {
            "system.getInfo": system_info,
            "wlan.getClientList": client_list,
        }
        tag_repeater_in_results(crash_results)
        repeater_connected, repeater_last_seen = find_repeater_status(crash_results)

        entry = {
            "timestamp": now.isoformat(),
            "poll_count": poll_count,
            "crash_detected": True,
            "rapid_mode": not recovered,
            "pre_crash_uptime": pre_crash_uptime,
            "current_uptime": current_uptime,
            "current_clients": current_clients,
            "repeater_connected": repeater_connected,
            "repeater_last_seen": repeater_last_seen,
            **crash_results,
        }
        write_jsonl(entry)

        status_tag = "RECOVERING" if not recovered else "RECOVERED"
        print(f"[CRASH MODE #{poll_count}] uptime={current_uptime}s clients={current_clients} [{status_tag}]")

        if recovered:
            print(f"\n[CRASH MODE] Recovery detected — uptime climbing, {current_clients} clients back")
            notify_macos("SFR Box", f"Recovered — uptime climbing, {current_clients} clients back")
            break

        time.sleep(CRASH_POLL_INTERVAL)

    return poll_count


def run_unreachable_mode(
    base_url: str,
    error_message: str,
    poll_count: int,
) -> tuple[int, bool, str | None]:
    print(f"\n{'='*60}")
    print(f"  *** BOX UNREACHABLE ***  All API calls failing")
    print(f"  Entering unreachable mode with escalating backoff")
    print(f"{'='*60}\n")
    notify_macos("SFR Box", "Box unreachable — all API calls failing")

    uptime_on_recovery: str | None = None

    for phase_idx, phase in enumerate(UNREACHABLE_PHASES):
        phase_num = phase_idx + 1
        phase_start = time.monotonic()
        phase_interval = phase["interval"]
        phase_duration = phase["duration"]

        while True:
            poll_count += 1
            now = datetime.now(timezone.utc)

            entry = {
                "timestamp": now.isoformat(),
                "poll_count": poll_count,
                "box_unreachable": True,
                "phase": phase_num,
                "error": error_message,
                "repeater_connected": False,
                "repeater_last_seen": None,
            }
            write_jsonl(entry)

            print(f"[UNREACHABLE phase={phase_num} #{poll_count}] {error_message}")

            try:
                r = requests.get(f"{base_url}?method=system.getInfo", timeout=10)
                data = parse_xml_response(r.content)
                if data.get("rsp", {}).get("@stat") == "ok":
                    uptime_on_recovery = data["rsp"]["status"]["@uptime"]
                    print(f"\n[UNREACHABLE] Box is back online!")
                    notify_macos("SFR Box", "Box back online")
                    return poll_count, True, uptime_on_recovery
            except Exception as e:
                error_message = str(e)

            time.sleep(phase_interval)

            if phase_duration is not None and time.monotonic() - phase_start >= phase_duration:
                break
            if phase_duration is None:
                continue

    return poll_count, False, None


def main() -> None:
    parser = argparse.ArgumentParser(description="SFR Box continuous monitor")
    parser.add_argument("--hostname", default="192.168.1.1", help="Box hostname or IP (default: 192.168.1.1)")
    parser.add_argument("--username", default="admin", help="Username for auth (default: admin)")
    args = parser.parse_args()

    password = get_password()
    base_url = BASE_URL_TEMPLATE.format(hostname=args.hostname)

    token, authenticated = authenticate(base_url, args.username, password)
    if not authenticated:
        print("ERROR: Authentication failed — cannot start baseline", file=sys.stderr)
        sys.exit(1)

    print("[AUTH] OK — initial token obtained")

    poll_count = 0
    poll_count = run_baseline(base_url, token, poll_count)

    last_auth_time = time.monotonic()
    previous_uptime: int | None = None
    pre_crash_client_count = 0

    while True:
        poll_count += 1
        now = datetime.now(timezone.utc)
        auth_refreshed = False

        if time.monotonic() - last_auth_time >= AUTH_REFRESH_INTERVAL:
            new_token, ok = authenticate(base_url, args.username, password)
            if ok:
                token = new_token
                last_auth_time = time.monotonic()
                auth_refreshed = True
                print(f"[AUTH] Token refreshed proactively")
            else:
                print(f"[AUTH] WARNING: proactive refresh failed, keeping old token")

        results: dict[str, dict] = {}
        failures = 0
        last_error = ""

        for endpoint in ALL_ENDPOINTS:
            needs_token = endpoint in PRIVATE_ENDPOINTS
            try:
                results[endpoint] = poll_endpoint(base_url, endpoint, token if needs_token else None)
                resp_stat = results[endpoint].get("rsp", {}).get("@stat")
                if resp_stat and resp_stat != "ok" and needs_token:
                    print(f"[AUTH] Auth failure on {endpoint}, re-authenticating...")
                    new_token, ok = authenticate(base_url, args.username, password)
                    if ok:
                        token = new_token
                        last_auth_time = time.monotonic()
                        auth_refreshed = True
                        results[endpoint] = poll_endpoint(base_url, endpoint, token)
                    else:
                        print(f"[AUTH] Re-authentication failed")
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

        if failures == len(ALL_ENDPOINTS):
            poll_count, recovered, uptime_str = run_unreachable_mode(
                base_url, last_error, poll_count,
            )
            if recovered and uptime_str is not None:
                try:
                    recovered_uptime = int(uptime_str)
                    if previous_uptime is not None and recovered_uptime < previous_uptime:
                        print(f"[RECOVERY] Uptime reset detected ({recovered_uptime}s < {previous_uptime}s), triggering crash mode")
                        poll_count = run_crash_mode(
                            base_url, token, args.username, password,
                            previous_uptime, pre_crash_client_count, poll_count,
                        )
                    previous_uptime = recovered_uptime
                except (ValueError, TypeError):
                    pass

                new_token, ok = authenticate(base_url, args.username, password)
                if ok:
                    token = new_token
                    last_auth_time = time.monotonic()

            continue

        tag_repeater_in_results(results)
        repeater_connected, repeater_last_seen = find_repeater_status(results)

        current_uptime = extract_uptime(results.get("system.getInfo", {}))

        crash_detected = False
        if previous_uptime is not None and current_uptime is not None:
            if current_uptime < previous_uptime:
                crash_detected = True

        if current_uptime is not None:
            previous_uptime = current_uptime

        for ep in ("wlan.getClientList", "wlan5.getClientList"):
            if ep in results and "error" not in results[ep]:
                pre_crash_client_count = extract_client_count(results[ep])
                break

        if failures > 0:
            status = f"PARTIAL ({failures} failures)"
        else:
            status = "OK"

        entry = {
            "timestamp": now.isoformat(),
            "poll_count": poll_count,
            "status": status,
            "auth_refreshed": auth_refreshed,
            "repeater_connected": repeater_connected,
            "repeater_last_seen": repeater_last_seen,
            **results,
        }
        write_jsonl(entry)

        flag = " [AUTH REFRESHED]" if auth_refreshed else ""
        rep_flag = " [REPEATER UP]" if repeater_connected else " [REPEATER DOWN]"
        print(f"[{now.strftime('%H:%M:%S')}] Poll #{poll_count} — {status}{flag}{rep_flag}")

        if crash_detected:
            poll_count = run_crash_mode(
                base_url, token, args.username, password,
                previous_uptime or 0, pre_crash_client_count, poll_count,
            )
            previous_uptime = None

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
