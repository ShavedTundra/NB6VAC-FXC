#!/usr/bin/env python3
"""SFR Box monitor — continuous polling of 9 endpoints with JSONL logging."""

import argparse
import hashlib
import hmac
import json
import os
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

AUTH_REFRESH_INTERVAL = 3600  # 60 minutes
POLL_INTERVAL = 60  # seconds


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


def poll_cycle(base_url: str, token: str) -> dict:
    results: dict[str, dict] = {}
    failures = 0
    auth_refreshed = False

    for endpoint in ALL_ENDPOINTS:
        try:
            results[endpoint] = poll_endpoint(base_url, endpoint, token if endpoint in PRIVATE_ENDPOINTS else None)
        except Exception as e:
            results[endpoint] = {"error": str(e)}
            failures += 1

    status = "OK"
    if failures == len(ALL_ENDPOINTS):
        status = "ALL FAILED"
    elif failures > 0:
        status = f"PARTIAL ({failures} failures)"

    return {"results": results, "status": status, "auth_refreshed": auth_refreshed}


def main() -> None:
    parser = argparse.ArgumentParser(description="SFR Box continuous monitor")
    parser.add_argument("--hostname", default="192.168.1.1", help="Box hostname or IP (default: 192.168.1.1)")
    parser.add_argument("--username", default="admin", help="Username for auth (default: admin)")
    args = parser.parse_args()

    password = get_password()
    base_url = BASE_URL_TEMPLATE.format(hostname=args.hostname)

    token, authenticated = authenticate(base_url, args.username, password)
    if not authenticated:
        print("ERROR: Authentication failed", file=sys.stderr)
        sys.exit(1)

    print("[AUTH] OK — initial token obtained")
    poll_count = 0
    last_auth_time = time.monotonic()

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
            except Exception as e:
                results[endpoint] = {"error": str(e)}
                failures += 1

        if failures == len(ALL_ENDPOINTS):
            status = "ALL FAILED"
        elif failures > 0:
            status = f"PARTIAL ({failures} failures)"
        else:
            status = "OK"

        entry = {
            "timestamp": now.isoformat(),
            "poll_count": poll_count,
            "status": status,
            "auth_refreshed": auth_refreshed,
            **results,
        }
        write_jsonl(entry)

        flag = " [AUTH REFRESHED]" if auth_refreshed else ""
        print(f"[{now.strftime('%H:%M:%S')}] Poll #{poll_count} — {status}{flag}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
