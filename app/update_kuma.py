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
PEL_APP_KEY = os.environ.get("PEL_APP_KEY", "")
PEL_CLIENT_KEY = os.environ.get("PEL_CLIENT_KEY", "")
PEL_SSL_VERIFY = os.environ.get("PEL_SSL_VERIFY", "1") == "1"

# Optional naming prefix (default empty)
KUMA_NAME_PREFIX = os.environ.get("KUMA_NAME_PREFIX", "").strip()

KUMA_INTERVAL = int(os.environ.get("KUMA_INTERVAL", "60"))
KUMA_STALE_DAYS = int(os.environ.get("KUMA_STALE_DAYS", "7"))

# Tagging (management + grouping)
KUMA_MANAGED_TAG = os.environ.get("KUMA_MANAGED_TAG", "managed:pelican").strip()
KUMA_WING_TAG_PREFIX = os.environ.get("KUMA_WING_TAG_PREFIX", "wing").strip().rstrip(":")
KUMA_TAG_COLOR = os.environ.get("KUMA_TAG_COLOR", "#0ea5e9").strip()

# Discord (optional)
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
DISCORD_STATE_PATH = Path(os.environ.get("DISCORD_STATE_PATH", "/data/discord_state.json"))

# Cache
CACHE_PATH = Path(os.environ.get("CACHE_PATH", "/data/pelican_servers_cache.json"))
CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", "300"))

NODE_CACHE_PATH = Path(os.environ.get("PEL_NODE_CACHE_PATH", "/data/pelican_nodes_cache.json"))
NODE_CACHE_TTL_SECONDS = int(os.environ.get("PEL_NODE_CACHE_TTL_SECONDS", "3600"))

DEBUG = os.environ.get("DEBUG", "0") == "1"

RUNNING_STATES = {"running", "starting"}


# --------------------
# Helpers
# --------------------
def require_env(name: str, val: str) -> None:
    if not val:
        raise SystemExit(f"Missing required environment variable: {name}")


def log(msg: str) -> None:
    if DEBUG:
        print(msg, flush=True)


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


def load_cache(path: Path, ttl_seconds: int) -> Optional[dict]:
    try:
        if path.exists():
            data = json.loads(path.read_text())
            ts = data.get("_cached_at", 0)
            if time.time() - ts <= ttl_seconds:
                return data
    except Exception:
        return None
    return None


def save_cache(path: Path, payload: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload["_cached_at"] = time.time()
        path.write_text(json.dumps(payload))
    except Exception:
        pass


def load_state(path: Path) -> Dict[str, dict]:
    try:
        if path.exists():
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                return data
    except Exception:
        return {}
    return {}


def save_state(path: Path, payload: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))
    except Exception:
        pass


def fetch_pelican_servers() -> List[dict]:
    """Application API: GET /api/application/servers"""
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
            attr = item.get("attributes", {}) or {}
            if attr.get("identifier") and attr.get("name"):
                servers.append(attr)

        meta = data.get("meta", {}) or {}
        pagination = (meta.get("pagination", {}) or {})
        total_pages = pagination.get("total_pages", page)
        if page >= total_pages:
            break
        page += 1

    return servers


def fetch_pelican_nodes() -> Dict[int, str]:
    """
    Application API: GET /api/application/nodes
    Returns mapping: node_id -> node_name

    If the Application API key lacks node permissions, Pelican can return 401/403.
    In that case, return an empty mapping and proceed without wing tagging.
    """
    nodes: Dict[int, str] = {}
    page = 1

    while True:
        r = requests.get(
            f"{PEL_URL}/api/application/nodes?page={page}&per_page=100",
            headers=pel_app_headers(),
            timeout=15,
            verify=PEL_SSL_VERIFY,
        )

        if r.status_code in (401, 403):
            log(
                f"[pelican] WARNING: cannot read nodes (HTTP {r.status_code}). "
                "Wing tagging will be disabled. Grant the Application API key node read permissions to enable it."
            )
            return {}

        r.raise_for_status()
        data = r.json()

        for item in data.get("data", []):
            attr = item.get("attributes", {}) or {}
            nid = attr.get("id")
            nname = attr.get("name")
            if isinstance(nid, int) and isinstance(nname, str) and nname.strip():
                nodes[nid] = nname.strip()

        meta = data.get("meta", {}) or {}
        pagination = (meta.get("pagination", {}) or {})
        total_pages = pagination.get("total_pages", page)
        if page >= total_pages:
            break
        page += 1

    return nodes


def fetch_server_resources(identifier: str) -> dict:
    """Client API: GET /api/client/servers/{identifier}/resources"""
    r = requests.get(
        f"{PEL_URL}/api/client/servers/{identifier}/resources",
        headers=pel_client_headers(),
        timeout=15,
        verify=PEL_SSL_VERIFY,
    )
    r.raise_for_status()
    return r.json()


def extract_state(resources_json: dict) -> str:
    """Extract current_state from multiple possible response shapes."""
    candidates = []
    candidates.append(resources_json.get("attributes", {}).get("current_state"))
    candidates.append(resources_json.get("attributes", {}).get("state"))

    data = resources_json.get("data", {})
    if isinstance(data, dict):
        candidates.append(data.get("attributes", {}).get("current_state"))
        candidates.append(data.get("attributes", {}).get("state"))
        candidates.append(data.get("current_state"))
        candidates.append(data.get("state"))

    candidates.append(resources_json.get("current_state"))
    candidates.append(resources_json.get("state"))

    for c in candidates:
        if isinstance(c, str) and c.strip():
            return c.strip().lower()
    return "unknown"


def extract_node_id(server_attr: dict) -> Optional[int]:
    for k in ("node", "node_id"):
        v = server_attr.get(k)
        if isinstance(v, int):
            return v
        if isinstance(v, str) and v.isdigit():
            return int(v)
    return None


def monitor_name(server_name: str) -> str:
    # default is just server_name; optional prefix supported
    if KUMA_NAME_PREFIX:
        return f"{KUMA_NAME_PREFIX} {server_name}".strip()
    return server_name


def wing_tag_name(node_name: str) -> str:
    return f"{KUMA_WING_TAG_PREFIX}:{node_name}"


def push(kuma_base: str, token: str, status: str, msg: str) -> None:
    url = (
        f"{kuma_base}/api/push/{token}?status={requests.utils.quote(status)}"
        f"&msg={requests.utils.quote(msg)}"
    )
    requests.get(url, timeout=10, verify=KUMA_SSL_VERIFY).raise_for_status()


def send_discord(webhook_url: str, content: str) -> None:
    if not webhook_url:
        return
    payload = {"content": content}
    requests.post(webhook_url, json=payload, timeout=10).raise_for_status()


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


def ensure_tag_id(api: UptimeKumaApi, tags_by_name: Dict[str, dict], name: str) -> int:
    if name in tags_by_name:
        return int(tags_by_name[name]["id"])
    created = api.add_tag(name=name, color=KUMA_TAG_COLOR)
    tags_by_name[name] = created
    return int(created["id"])


def add_tag_to_monitor(api: UptimeKumaApi, monitor_id: int, tag_id: int) -> None:
    try:
        api.add_monitor_tag(tag_id=tag_id, monitor_id=monitor_id, value="")
    except Exception:
        pass


def monitor_has_managed_tag(api: UptimeKumaApi, monitor_id: int, managed_tag_id: int) -> bool:
    """Use get_monitor() because get_monitors() may omit tags depending on Kuma version."""
    try:
        mon = api.get_monitor(monitor_id)
    except Exception:
        return False

    tags = mon.get("tags") or []
    for t in tags:
        if isinstance(t, dict):
            if int(t.get("tag_id") or t.get("id") or -1) == managed_tag_id:
                return True
            tag_obj = t.get("tag")
            if isinstance(tag_obj, dict) and int(tag_obj.get("id") or -1) == managed_tag_id:
                return True
    return False


def main() -> None:
    require_env("KUMA_URL", KUMA_URL)
    require_env("KUMA_USER", KUMA_USER)
    require_env("KUMA_PASS", KUMA_PASS)

    require_env("PEL_URL", PEL_URL)
    require_env("PEL_APP_KEY", PEL_APP_KEY)
    require_env("PEL_CLIENT_KEY", PEL_CLIENT_KEY)

    # Cache servers + nodes together
    cached = load_cache(CACHE_PATH, CACHE_TTL_SECONDS)
    if cached and isinstance(cached.get("servers"), list) and isinstance(cached.get("nodes"), dict):
        servers = cached["servers"]
        nodes = {int(k): v for k, v in cached["nodes"].items()}
    else:
        servers = fetch_pelican_servers()
        nodes = fetch_pelican_nodes()
        save_cache(CACHE_PATH, {"servers": servers, "nodes": nodes})

    state_cache: Dict[str, dict] = {}
    if DISCORD_WEBHOOK_URL:
        state_cache = load_state(DISCORD_STATE_PATH)
    else:
        log("[discord] disabled (DISCORD_WEBHOOK_URL not set)")

    cutoff_ms = int((time.time() - KUMA_STALE_DAYS * 86400) * 1000)

    with UptimeKumaApi(KUMA_URL, ssl_verify=KUMA_SSL_VERIFY) as api:
        api.login(KUMA_USER, KUMA_PASS)

        # Tags
        tags_list = api.get_tags()
        tags_by_name: Dict[str, dict] = {
            t["name"]: t for t in tags_list if isinstance(t, dict) and "name" in t
        }
        managed_tag_id = ensure_tag_id(api, tags_by_name, KUMA_MANAGED_TAG)

        # Existing push monitors by name
        monitors = api.get_monitors()
        by_name: Dict[str, dict] = {
            str(m.get("name", "")): m for m in monitors if m.get("pushToken")
        }

        running_now: List[Tuple[str, str, str, Optional[str]]] = []
        created_names: set[str] = set()
        pending_notifications: List[str] = []

        # Only create/push for running/starting
        for s in servers:
            sname = str(s.get("name", "")).strip()
            identifier = str(s.get("identifier", "")).strip()
            if not sname or not identifier:
                continue

            name = monitor_name(sname)

            try:
                resources = fetch_server_resources(identifier)
                state = extract_state(resources)
                log(f"[pelican] {sname} ({identifier}) state={state}")
            except Exception as e:
                log(f"[pelican] {sname} ({identifier}) resources ERROR {type(e).__name__}: {e}")
                continue

            status = "up" if state in RUNNING_STATES else "down"
            if DISCORD_WEBHOOK_URL:
                prev_status = None
                cached_entry = state_cache.get(identifier)
                if isinstance(cached_entry, dict):
                    prev_status = cached_entry.get("status")

                # Only notify on state change after we've seen the server before
                if prev_status and prev_status != status:
                    pending_notifications.append(
                        f"[{status.upper()}] {sname} is now {status.upper()}"
                    )

                state_cache[identifier] = {
                    "status": status,
                    "name": sname,
                    "updated_at": int(time.time()),
                }

            if status != "up":
                continue

            node_name = None
            nid = extract_node_id(s)
            if nid is not None:
                node_name = nodes.get(nid)

            running_now.append((name, identifier, state, node_name))

            if name not in by_name:
                resp = api.add_monitor(type=MonitorType.PUSH, name=name, interval=KUMA_INTERVAL)
                log(f"[kuma] created monitor: {name} resp={resp}")
                created_names.add(name)

        # Refresh monitors
        monitors = api.get_monitors()
        by_name = {str(m.get("name", "")): m for m in monitors if m.get("pushToken")}

        # Tag and push for running servers
        for name, identifier, state, node_name in running_now:
            mon = by_name.get(name)
            if not mon:
                continue

            monitor_id = mon.get("id")
            token = mon.get("pushToken")
            if not monitor_id or not token:
                continue

            # Only tag when the monitor is newly created
            if name in created_names:
                # Managed tag
                add_tag_to_monitor(api, int(monitor_id), managed_tag_id)

                # Wing tag if we have node mapping
                if node_name:
                    wing_name = wing_tag_name(node_name)
                    wing_tag_id = ensure_tag_id(api, tags_by_name, wing_name)
                    add_tag_to_monitor(api, int(monitor_id), wing_tag_id)

            # Push UP with state msg
            push(KUMA_URL, token, "up", f"state={state}")
            log(f"[kuma] push up: {name} msg=state={state}")

        # Discord notifications (if configured)
        if DISCORD_WEBHOOK_URL:
            for msg in pending_notifications:
                try:
                    send_discord(DISCORD_WEBHOOK_URL, msg)
                    log(f"[discord] sent: {msg}")
                except Exception as e:
                    log(f"[discord] ERROR {type(e).__name__}: {e}")

            save_state(DISCORD_STATE_PATH, state_cache)

        # Cleanup: delete stale monitors only if managed tag is present
        monitors = api.get_monitors()
        for mon in monitors:
            mid = mon.get("id")
            if not mid:
                continue
            hb = get_last_hb_ms(mon)
            if hb is None or hb >= cutoff_ms:
                continue

            if monitor_has_managed_tag(api, int(mid), managed_tag_id):
                try:
                    api.delete_monitor(int(mid))
                    log(f"[kuma] deleted stale managed monitor id={mid}")
                except Exception:
                    pass


if __name__ == "__main__":
    main()
