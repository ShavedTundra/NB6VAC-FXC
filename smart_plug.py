"""Smart plug control — power-cycle via Shelly Gen3 RPC, Shelly Gen1 REST, or Tasmota."""

import requests

_BRAND_LADDER: list[tuple[str, str]] = [
    ("Shelly Gen3 RPC", "http://{plug_ip}/rpc/Switch.Set?id=0&on={on}"),
    ("Shelly Gen1 REST", "http://{plug_ip}/relay/0?turn={turn}"),
    ("Tasmota", "http://{plug_ip}/cm?cmnd=Power%20{power}"),
]


def set_plug(plug_ip: str, on: bool) -> str:
    """Turn a smart plug on or off.

    Tries each brand in order; returns the brand name that succeeded.
    Raises RuntimeError if no brand responds.
    """
    for brand, url_template in _BRAND_LADDER:
        url = url_template.format(
            plug_ip=plug_ip,
            on="true" if on else "false",
            turn="on" if on else "off",
            power="On" if on else "Off",
        )
        try:
            r = requests.get(url, timeout=5)
            if r.ok:
                state = "ON" if on else "OFF"
                print(f"  Plug {state} ({brand})")
                return brand
        except requests.RequestException:
            pass
    raise RuntimeError(f"Could not reach smart plug at {plug_ip}")
