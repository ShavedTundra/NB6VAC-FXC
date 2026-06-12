#!/usr/bin/env python3
"""Reboot the SFR Box via API (soft) or smart plug (hard).

Usage:
    python reboot.py --hostname 192.168.1.1              # soft reboot via API
    python reboot.py --hostname 192.168.1.1 --hard       # hard reboot via smart plug
    python reboot.py --hostname 192.168.1.1 --scheduled  # only if uptime > UPTIME_THRESHOLD_H
"""

import argparse
import os
import subprocess
import sys
import time

import requests

import sfr_box
import smart_plug

UPTIME_THRESHOLD_H = 18  # max hours before scheduled reboot triggers


def get_uptime_hours(base_url: str, token: str) -> float | None:
    """Get current uptime in hours from system.getInfo.

    Returns None if uptime cannot be extracted (malformed response).
    """
    try:
        data = sfr_box.poll_endpoint(base_url, "system.getInfo", token)
        return sfr_box.extract_uptime(data) / 3600
    except (KeyError, ValueError):
        return None


def soft_reboot(base_url: str, username: str, password: str) -> bool:
    """Authenticate and send system.reboot via the box API."""
    token, ok = sfr_box.authenticate(base_url, username, password)
    if not ok:
        print("ERROR: Authentication failed", file=sys.stderr)
        return False

    try:
        r = requests.post(f"{base_url}?method=system.reboot&token={token}", timeout=10)
        data = sfr_box.parse_xml(r.content)
        stat = data.get("rsp", {}).get("@stat", "")
        if stat == "ok":
            print("Soft reboot command sent successfully.")
            return True
        else:
            print(f"ERROR: Reboot rejected — {data}", file=sys.stderr)
            return False
    except requests.RequestException as e:
        print(f"ERROR: Reboot request failed — {e}", file=sys.stderr)
        return False


def hard_reboot_via_ping(hostname: str) -> bool:
    """Wait for box to come back up after a hard power cycle (called by caller after plug off/on)."""
    print(f"Waiting for {hostname} to come back up...")
    max_wait = 180
    waited = 0
    while waited < max_wait:
        time.sleep(5)
        waited += 5
        try:
            result = subprocess.run(
                ["ping", "-c", "1", "-W", "2", hostname],
                capture_output=True, timeout=5,
            )
            if result.returncode == 0:
                print(f"Box is back up after {waited}s.")
                return True
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        print(f"  {waited}s...")

    print(f"ERROR: Box not responding after {max_wait}s", file=sys.stderr)
    return False




def main():
    parser = argparse.ArgumentParser(description="Reboot SFR Box")
    parser.add_argument("--hostname", default="192.168.1.1")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default=None, help="Box password (default: from SFR_PASSWORD or config.local.json)")
    parser.add_argument("--hard", action="store_true", help="Hard power cycle via smart plug (requires SMART_PLUG_IP)")
    parser.add_argument("--scheduled", action="store_true", help=f"Only reboot if uptime > {UPTIME_THRESHOLD_H}h")
    parser.add_argument("--smart-plug-ip", default=None, help="Tasmota/Shelly smart plug IP (or set SMART_PLUG_IP env)")
    args = parser.parse_args()

    password = args.password or sfr_box.get_password()
    base_url = f"http://{args.hostname}/api/1.0/"

    # Auth first — we need a token to check uptime
    token, ok = sfr_box.authenticate(base_url, args.username, password)
    if not ok:
        print("ERROR: Authentication failed", file=sys.stderr)
        sys.exit(1)

    # Check uptime for scheduled mode
    uptime_h = get_uptime_hours(base_url, token)
    if uptime_h is not None:
        print(f"Current uptime: {uptime_h:.1f}h")
    else:
        print("Could not read uptime — proceeding anyway.")

    if args.scheduled:
        if uptime_h is not None and uptime_h < UPTIME_THRESHOLD_H:
            print(f"Uptime {uptime_h:.1f}h < {UPTIME_THRESHOLD_H}h threshold — skipping.")
            sys.exit(0)
        if uptime_h is None:
            print("WARNING: Uptime unknown — skipping scheduled reboot to be safe.")
            sys.exit(0)
        print(f"Uptime {uptime_h:.1f}h >= {UPTIME_THRESHOLD_H}h — proceeding with reboot.")

    if args.hard:
        plug_ip = args.smart_plug_ip or os.environ.get("SMART_PLUG_IP")
        if not plug_ip:
            print("ERROR: --hard requires --smart-plug-ip or SMART_PLUG_IP env var", file=sys.stderr)
            sys.exit(1)

        print(f"Hard reboot: cutting power to plug {plug_ip}...")
        try:
            smart_plug.set_plug(plug_ip, on=False)
            time.sleep(30)
            smart_plug.set_plug(plug_ip, on=True)
        except Exception as e:
            print(f"ERROR: Smart plug control failed — {e}", file=sys.stderr)
            sys.exit(1)

        hard_reboot_via_ping(args.hostname)
    else:
        if not soft_reboot(base_url, args.username, password):
            sys.exit(1)


if __name__ == "__main__":
    main()
