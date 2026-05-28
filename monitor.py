#!/usr/bin/env python3
"""SFR Box monitor — authenticates, polls system.getInfo, writes JSONL log."""

import argparse
import hashlib
import hmac
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

import requests
from collections import defaultdict


BASE_URL_TEMPLATE = "http://{hostname}/api/1.0/"


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


def main() -> None:
    parser = argparse.ArgumentParser(description="SFR Box monitor")
    parser.add_argument("--hostname", default="192.168.1.1", help="Box hostname or IP (default: 192.168.1.1)")
    parser.add_argument("--username", default="admin", help="Username for auth (default: admin)")
    args = parser.parse_args()

    password = get_password()
    base_url = BASE_URL_TEMPLATE.format(hostname=args.hostname)

    token, authenticated = authenticate(base_url, args.username, password)
    if not authenticated:
        print("ERROR: Authentication failed", file=sys.stderr)
        sys.exit(1)

    print(f"[AUTH] OK — token obtained")

    result = poll_endpoint(base_url, "system.getInfo")

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "system.getInfo": result,
    }
    write_jsonl(entry)

    print(f"[POLL] system.getInfo — {json.dumps(result, indent=2)}")
    print(f"[LOG] Written to logs/")


if __name__ == "__main__":
    main()
