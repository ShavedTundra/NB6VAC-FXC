"""Shared SFR Box communication — XML parsing, auth, endpoint polling, and credential resolution."""

import json
import sys
from collections import defaultdict
from hashlib import sha256
from hmac import new as hmac_new
from pathlib import Path
from xml.etree import ElementTree

import requests


def etree_to_dict(t: ElementTree.Element) -> dict:
    """Convert an XML element tree to a nested dict."""
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


def parse_xml(content: bytes) -> dict:
    """Parse XML bytes into a nested dict."""
    return etree_to_dict(ElementTree.fromstring(content))


def get_password() -> str:
    """Resolve password from SFR_PASSWORD env var or config.local.json fallback.

    Exits with error message if no password is found.
    """
    password = _password_from_env()
    if password:
        return password
    password = _password_from_config()
    if password:
        return password
    print("ERROR: No password found. Set SFR_PASSWORD env var or create config.local.json", file=sys.stderr)
    sys.exit(1)


def _password_from_env() -> str | None:
    import os
    return os.environ.get("SFR_PASSWORD")


def _password_from_config() -> str | None:
    config_path = Path("config.local.json")
    if config_path.exists():
        with open(config_path) as f:
            config = json.load(f)
        return config.get("password")
    return None


def compute_auth_hash(token: str, username: str, password: str) -> str:
    """Compute HMAC-SHA256 auth hash from token, username, and password."""
    fh1 = sha256(username.encode()).hexdigest()
    key_hash1 = hmac_new(token.encode(), msg=fh1.encode(), digestmod=sha256).hexdigest()
    fh2 = sha256(password.encode()).hexdigest()
    key_hash2 = hmac_new(token.encode(), msg=fh2.encode(), digestmod=sha256).hexdigest()
    return key_hash1 + key_hash2


def authenticate(base_url: str, username: str, password: str) -> tuple[str, bool]:
    """Two-step auth flow: getToken → HMAC → checkToken. Returns (token, ok)."""
    r = requests.get(f"{base_url}?method=auth.getToken", timeout=10)
    data = parse_xml(r.content)
    token = data["rsp"]["auth"]["@token"]

    key_hash = compute_auth_hash(token, username, password)
    r = requests.get(
        f"{base_url}?method=auth.checkToken&token={token}&hash={key_hash}",
        timeout=10,
    )
    data = parse_xml(r.content)

    if data["rsp"]["@stat"] != "ok":
        return token, False
    return token, True


def extract_uptime(system_info: dict) -> int:
    """Extract uptime in seconds from a system.getInfo response.

    Raises KeyError when the expected path (rsp.system.@uptime) is absent.
    """
    return int(system_info["rsp"]["system"]["@uptime"])


def poll_endpoint(base_url: str, method: str, token: str | None = None) -> dict:
    """Poll a single API endpoint and return parsed XML as dict."""
    url = f"{base_url}?method={method}"
    if token:
        url += f"&token={token}"
    r = requests.get(url, timeout=10)
    return parse_xml(r.content)
