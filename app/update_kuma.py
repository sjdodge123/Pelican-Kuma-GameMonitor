import os
import re
import time
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
import docker
from docker.models.containers import Container
from uptime_kuma_api import UptimeKumaApi, MonitorType

# ----------------------------
# Env configuration
# ----------------------------
KUMA_URL = os.environ.get("KUMA_URL", "").rstrip("/")
KUMA_USER = os.environ.get("KUMA_USER", "")
KUMA_PASS = os.environ.get("KUMA_PASS", "")
KUMA_SSL_VERIFY = os.environ.get("KUMA_SSL_VERIFY", "1") == "1"

KUMA_NAME_PREFIX = os.environ.get("KUMA_NAME_PREFIX", "AUTO").strip()
KUMA_INTERVAL = int(os.environ.get("KUMA_INTERVAL", "60"))
KUMA_STALE_DAYS = int(os.environ.get("KUMA_STALE_DAYS", "7"))

PEL_URL = os.environ.get("PEL_URL", "").rstrip("/")
PEL_KEY = os.environ.get("PEL_KEY", "")
PEL_SSL_VERIFY = os.environ.get("PEL_SSL_VERIFY", "1") == "1"
PEL_API_MODE = os.environ.get("PEL_API_MODE", "application").lower().strip()  # application|client

DISCOVER_UUID_ONLY = os.environ.get("DISCOVER_UUID_ONLY", "1") == "1"
NAME_INCLUDE_PORTS = os.environ.get("NAME_INCLUDE_PORTS", "0") == "1"

CHECK_PAL_PROC = os.environ.get("CHECK_PAL_PROC", "1") == "1"

CACHE_PATH = Path(os.environ.get("CACHE_PATH", "/data/cache/pelican_names.json"))

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
PAL_PROC_RE = re.compile(r"PalServer-Linux-Shipping", re.I)

# ----------------------------
# Helpers
# ----------------------------
def now_ms() -> int:
    return int(time.time() * 1000)

def short_ports(container: Container) -> str:
    """
    Returns something like: "8211/udp,27015/udp"
    """
    ports = container.attrs.get("NetworkSettings", {}).get("Ports", {}) or {}
    out = []
    for container_port_proto, mappings in ports.items():
        # container_port_proto example: "8211/udp"
        if not mappings:
            continue
        # mappings is list[{"HostIp":"0.0.0.0","HostPort":"8211"}]
        out.append(container_port_proto.lower())
    out = sorted(set(out))
    return ",".join(out)

def make_name(friendly: str, ports: str) -> str:
    base = f"{KUMA_NAME_PREFIX} {friendly}".strip() if KUMA_NAME_PREFIX else friendly
    if NAME_INCLUDE_PORTS and ports:
        return f"{base} [{ports}]"
    return base

def load_cache() -> Dict[str, str]:
    try:
        if CACHE_PATH.exists():
            return json.loads(CACHE_PATH.read_text())
    except Exception:
        pass
    return {}

def save_cache(m: Dict[str, str]) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(m))
    except Exception:
        pass

def pelican_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {PEL_KEY}",
        "Accept": "Application/vnd.pterodactyl.v1+json",
    }

def fetch_pelican_name_map() -> Dict[str, str]:
    """
    Returns {uuid: name} for servers.
    Supports application or client API modes.
    """
    if not PEL_URL or not PEL_KEY:
        return {}

    m: Dict[str, str] = {}

    if PEL_API_MODE == "client":
        # /api/client/servers
        page = 1
        while True:
            r = requests.get(
                f"{PEL_URL}/api/client/servers?page={page}",
                headers=pelican_headers(),
                timeout=10,
                verify=PEL_SSL_VERIFY,
            )
            r.raise_for_status()
            data = r.json()
            for item in data.get("data", []):
                attr = item.get("attributes", {})
                uuid = attr.get("uuid")
                name = attr.get("name")
                if uuid and name:
                    m[uuid] = name
            meta = data.get("meta", {})
            pages = (meta.get("pagination", {}) or {}).get("total_pages", page)
            if page >= pages:
                break
            page += 1
        return m

    # application mode: /api/application/servers
    page = 1
    while True:
        r = requests.get(
            f"{PEL_URL}/api/application/servers?page={page}&per_page=100",
            headers=pelican_headers(),
            timeout=10,
            verify=PEL_SSL_VERIFY,
        )
        r.raise_for_status()
        data = r.json()
        for item in data.get("data", []):
            attr = item.get("attributes", {})
            uuid = attr.get("uuid")
            name = attr.get("name")
            if uuid and name:
                m[uuid] = name
        meta = data.get("meta", {})
        pages = (meta.get("pagination", {}) or {}).get("total_pages", page)
        if page >= pages:
            break
        page += 1
    return m

def get_last_hb_ms(mon: dict) -> Optional[int]:
    # common fields seen in Kuma monitor objects vary across versions
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

def push_url(push_token: str, status: str, msg: str) -> str:
    # Uptime Kuma push endpoint is /api/push/<token>?status=up|down...  [oai_citation:4‡GitHub](https://github.com/louislam/uptime-kuma/wiki/Internal-API?utm_source=chatgpt.com)
    return f"{KUMA_URL}/api/push/{push_token}?status={requests.utils.quote(status)}&msg={requests.utils.quote(msg)}"

def docker_client() -> docker.DockerClient:
    return docker.from_env()

def exec_in_container(cli: docker.DockerClient, container: Container, cmd: List[str], timeout_s: int = 3) -> Tuple[int, str]:
    exec_id = cli.api.exec_create(container.id, cmd)
    out = cli.api.exec_start(exec_id, tty=False, stream=False, demux=False)
    rc = cli.api.exec_inspect(exec_id)["ExitCode"]
    text = out.decode(errors="ignore") if isinstance(out, (bytes, bytearray)) else str(out)
    return rc, text

def container_is_interesting(container: Container) -> bool:
    name = (container.name or "").strip()
    if DISCOVER_UUID_ONLY and not UUID_RE.match(name):
        return False
    # must have at least one published port mapping
    ports = container.attrs.get("NetworkSettings", {}).get("Ports", {}) or {}
    return any(v for v in ports.values())

def main() -> None:
    if not KUMA_URL:
        raise SystemExit("KUMA_URL is required")
    if not KUMA_USER or not KUMA_PASS:
        raise SystemExit("KUMA_USER and KUMA_PASS are required (Socket.IO login).")

    # Pelican map (cached)
    cache = load_cache()
    try:
        fresh = fetch_pelican_name_map()
        if fresh:
            cache = fresh
            save_cache(cache)
    except Exception:
        # keep cache if API temporarily unavailable
        pass

    cli = docker_client()
    containers = [c for c in cli.containers.list(all=True) if container_is_interesting(c)]

    # Kuma connect
    with UptimeKumaApi(KUMA_URL, ssl_verify=KUMA_SSL_VERIFY) as api:
        api.login(KUMA_USER, KUMA_PASS)

        monitors = api.get_monitors()

        # Index auto-managed push monitors by name (must have pushToken)
        auto: Dict[str, dict] = {}
        for mon in monitors:
            n = str(mon.get("name", ""))
            if KUMA_NAME_PREFIX and not n.startswith(KUMA_NAME_PREFIX):
                continue
            if mon.get("pushToken"):
                auto[n] = mon

        # Ensure monitors exist for each discovered container
        desired_names = set()
        desired_by_uuid: Dict[str, str] = {}

        for c in containers:
            uuid = c.name
            friendly = cache.get(uuid, uuid)
            ports = short_ports(c)
            name = make_name(friendly, ports)
            desired_names.add(name)
            desired_by_uuid[uuid] = name

            if name not in auto:
                resp = api.add_monitor(
                    type=MonitorType.PUSH,
                    name=name,
                    interval=KUMA_INTERVAL,
                )
                # Some versions return monitorID
                _ = resp.get("monitorID") or resp.get("monitorId") or resp.get("monitorID".lower())

        # Refresh monitors after additions
        monitors = api.get_monitors()
        auto = {}
        for mon in monitors:
            n = str(mon.get("name", ""))
            if KUMA_NAME_PREFIX and not n.startswith(KUMA_NAME_PREFIX):
                continue
            if mon.get("pushToken"):
                auto[n] = mon

        # Push statuses
        for c in containers:
            uuid = c.name
            name = desired_by_uuid.get(uuid)
            if not name:
                continue
            mon = auto.get(name)
            if not mon:
                continue

            token = mon.get("pushToken")
            if not token:
                continue

            ports = short_ports(c)
            running = (c.status == "running")

            msg = f"UP ({ports})" if ports else "UP"
            status = "up"

            if running and CHECK_PAL_PROC and ("8211/udp" in ports):
                try:
                    rc, out = exec_in_container(cli, c, ["ps", "aux"])
                    if rc == 0 and PAL_PROC_RE.search(out):
                        msg = f"UP (PalServer) ({ports})"
                    else:
                        msg = f"UP (container only) ({ports})"
                except Exception:
                    msg = f"UP (container) ({ports})"

            if not running:
                status = "down"
                msg = f"DOWN (container stopped) ({ports})"

            try:
                requests.get(push_url(token, status, msg), timeout=5, verify=KUMA_SSL_VERIFY).raise_for_status()
            except Exception:
                # don’t crash the whole run
                pass

        # Cleanup stale monitors (> KUMA_STALE_DAYS)
        cutoff = now_ms() - (KUMA_STALE_DAYS * 86400 * 1000)
        for name, mon in list(auto.items()):
            hb = get_last_hb_ms(mon)
            if hb is None:
                continue
            if hb < cutoff:
                mid = mon.get("id")
                if mid:
                    try:
                        api.delete_monitor(mid)  # supported by wrapper  [oai_citation:5‡uptime-kuma-api.readthedocs.io](https://uptime-kuma-api.readthedocs.io/en/latest/api.html?utm_source=chatgpt.com)
                    except Exception:
                        pass

if __name__ == "__main__":
    main()