import os
import time
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from uptime_kuma_api import UptimeKumaApi, MonitorType

# --------------------
# Environment variables
# --------------------
KUMA_URL = os.environ.get("KUMA_URL", "").rstrip("/")
KUMA_USER = os.environ.get("KUMA_USER", "")
KUMA_PASS = os.environ.get("KUMA_PASS", "")
KUMA_SSL_VERIFY = os.environ.get("KUMA_SSL_VERIFY", "1") == "1"

PEL_URL = os.environ.get("PEL_URL", "").rstrip("/")
PEL_APP_KEY = os.environ.get("PEL_APP_KEY", "")         # Application API key (papp_*)
PEL_CLIENT_KEY = os.environ.get("PEL_CLIENT_KEY", "")   # Client API key (ptlc_* / user token)
PEL_SSL_VERIFY = os.environ.get("PEL_SSL_VERIFY", "1") == "1"

KUMA_NAME_PREFIX = os.environ.get("KUMA_NAME_PREFIX", "AUTO").strip()
KUMA_INTERVAL = int(os.environ.get("KUMA_INTERVAL", "60"))
KUMA_STALE_DAYS = int(os.environ.get("KUMA_STALE_DAYS", "7"))

# Cache so we don't hammer Pelican every minute
CACHE_PATH = Path(os.environ.get("CACHE_PATH", "/data/pelican_servers_cache.json"))
CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", "300"))

# --------------------
# Helpers
# --------------------
def require_env(name: str, val: str) -> None:
    if not val:
        raise SystemExit(f"Missing required environment variable: {name}")

def pel_app_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {PEL_APP_KEY}",
        "Accept": "Application/vnd.pterodactyl.v1+json",
    }

def pel_client_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {PEL_CLIENT_KEY}",
        "Accept": "Application/vnd.pterodactyl.v1+json",
    }

def load_cache() -> Optional[dict]:
    try:
        if CACHE_PATH.exists():
            data = json.loads(CACHE_PATH.read_text())
            # basic TTL
            ts = data.get("_cached_at", 0)
            if time.time() - ts <= CACHE_TTL_SECONDS:
                return data
    except Exception:
        return None
    return None

def save_cache(payload: dict) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload["_cached_at"] = time.time()
        CACHE_PATH.write_text(json.dumps(payload))
    except Exception:
        pass

def fetch_pelican_servers() -> List[dict]:
    """
    Uses Pelican/Pterodactyl Application API:
      GET /api/application/servers?page=..&per_page=100
    Returns a list of server attribute dicts including:
      uuid, identifier, name
    """
    servers: List[dict] = []
    page = 1

    while True:
        r = requests.get(
            f"{PEL_URL}/api/application/servers?page={page}&per_page=100",
            headers=pel_app_headers(),
            timeout=15,
            verify=PEL_SSL_VERIFY,
        )
        r.raise_for_status()
        data = r.json()

        for item in data.get("data", []):
            attr = item.get("attributes", {})
            if attr.get("uuid") and attr.get("identifier") and attr.get("name"):
                servers.append(attr)

        meta = data.get("meta", {})
        pagination = (meta.get("pagination", {}) or {})
        total_pages = pagination.get("total_pages", page)
        if page >= total_pages:
            break
        page += 1

    return servers

def fetch_server_resources(identifier: str) -> dict:
    """
    Uses Pelican/Pterodactyl Client API:
      GET /api/client/servers/{identifier}/resources

    Requires a client token that can access the server(s).
    """
    r = requests.get(
        f"{PEL_URL}/api/client/servers/{identifier}/resources",
        headers=pel_client_headers(),
        timeout=15,
        verify=PEL_SSL_VERIFY,
    )
    r.raise_for_status()
    return r.json()

def normalize_state(resources_json: dict) -> str:
    """
    Commonly: resources['attributes']['current_state'] is one of:
      running, starting, stopping, offline
    """
    return (
        resources_json.get("attributes", {})
        .get("current_state", "")
        .strip()
        .lower()
    )

def monitor_name(server_name: str) -> str:
    if KUMA_NAME_PREFIX:
        return f"{KUMA_NAME_PREFIX} {server_name}"
    return server_name

def get_last_hb_ms(mon: dict) -> Optional[int]:
    for k in ("lastHeartbeat", "last_heartbeat", "lastBeat", "last_beat"):
        v = mon.get(k)
        if isinstance(v, (int, float)):
            return int(v)
    hb = mon.get("heartbeat")
    if isinstance(hb, dict):
        v = hb.get("time") or hb.get("timestamp")
        if isinstance(v, (int, float)):
            return int(v)
    return None

def push(kuma_base: str, token: str, status: str, msg: str) -> None:
    url = f"{kuma_base}/api/push/{token}?status={requests.utils.quote(status)}&msg={requests.utils.quote(msg)}"
    requests.get(url, timeout=10, verify=KUMA_SSL_VERIFY).raise_for_status()

# --------------------
# Main
# --------------------
def main() -> None:
    require_env("KUMA_URL", KUMA_URL)
    require_env("KUMA_USER", KUMA_USER)
    require_env("KUMA_PASS", KUMA_PASS)

    require_env("PEL_URL", PEL_URL)
    require_env("PEL_APP_KEY", PEL_APP_KEY)
    require_env("PEL_CLIENT_KEY", PEL_CLIENT_KEY)

    # Pull Pelican server list (with TTL cache)
    cached = load_cache()
    if cached and isinstance(cached.get("servers"), list):
        servers = cached["servers"]
    else:
        servers = fetch_pelican_servers()
        save_cache({"servers": servers})

    # Build desired names & quick index by name
    desired: Dict[str, dict] = {}
    for s in servers:
        name = monitor_name(s["name"])
        desired[name] = s

    cutoff_ms = int((time.time() - KUMA_STALE_DAYS * 86400) * 1000)

    with UptimeKumaApi(KUMA_URL, ssl_verify=KUMA_SSL_VERIFY) as api:
        api.login(KUMA_USER, KUMA_PASS)

        # Get existing AUTO push monitors (identified by name prefix + pushToken)
        monitors = api.get_monitors()
        auto: Dict[str, dict] = {}
        for mon in monitors:
            n = str(mon.get("name", ""))
            if KUMA_NAME_PREFIX and not n.startswith(KUMA_NAME_PREFIX):
                continue
            if mon.get("pushToken"):
                auto[n] = mon

        # Ensure monitor exists for every Pelican server
        for name in desired.keys():
            if name not in auto:
                api.add_monitor(
                    type=MonitorType.PUSH,
                    name=name,
                    interval=KUMA_INTERVAL,
                )

        # Refresh list after adds
        monitors = api.get_monitors()
        auto = {}
        for mon in monitors:
            n = str(mon.get("name", ""))
            if KUMA_NAME_PREFIX and not n.startswith(KUMA_NAME_PREFIX):
                continue
            if mon.get("pushToken"):
                auto[n] = mon

        # Push status for each server
        for name, s in desired.items():
            mon = auto.get(name)
            if not mon:
                continue
            token = mon.get("pushToken")
            if not token:
                continue

            identifier = s["identifier"]
            try:
                resources = fetch_server_resources(identifier)
                state = normalize_state(resources)
            except Exception as e:
                # If panel is unreachable or auth fails, report down (but don't delete)
                push(KUMA_URL, token, "down", f"error fetching resources: {type(e).__name__}")
                continue

            # Define UP/DOWN mapping
            if state in ("running", "starting"):
                push(KUMA_URL, token, "up", f"state={state}")
            else:
                # stopping/offline/unknown => down
                push(KUMA_URL, token, "down", f"state={state or 'unknown'}")

        # Cleanup: delete stale AUTO monitors with no heartbeat for > KUMA_STALE_DAYS
        for name, mon in list(auto.items()):
            hb = get_last_hb_ms(mon)
            if hb is None:
                continue
            if hb < cutoff_ms:
                mid = mon.get("id")
                if mid:
                    try:
                        api.delete_monitor(mid)
                    except Exception:
                        pass

if __name__ == "__main__":
    main()