import os
import time
import json
import base64
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from uptime_kuma_api import UptimeKumaApi, MonitorType

from branding import branding, COLOR_UP, COLOR_DOWN
from store import load_webhooks, webhook_for, read_json, write_json
from notify import send_discord
import maintenance as maint

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

# Discord (optional). Per-server webhooks are managed via the admin UI and
# stored in /data/webhooks.json; DISCORD_WEBHOOK_URL remains the global fallback.
DISCORD_STATE_PATH = Path(os.environ.get("DISCORD_STATE_PATH", "/data/discord_state.json"))

# Status page (dynamic, single page grouped by wing/node)
STATUS_PAGE_ENABLED = os.environ.get("STATUS_PAGE_ENABLED", "1") == "1"
STATUS_PAGE_SLUG = os.environ.get("STATUS_PAGE_SLUG", "gamersaloon").strip()
STATUS_PAGE_TITLE = os.environ.get("STATUS_PAGE_TITLE", "").strip()
STATUS_PAGE_THEME = os.environ.get("STATUS_PAGE_THEME", "dark").strip()
STATUS_PAGE_UNGROUPED = os.environ.get("STATUS_PAGE_UNGROUPED_LABEL", "Other").strip()
STATUS_PAGE_STATE_PATH = Path(os.environ.get("STATUS_PAGE_STATE_PATH", "/data/statuspage_state.json"))
# Icon shown on the Kuma status page. A local /static/... path is embedded as a
# base64 data URI (Kuma's native icon format) so branding renders with no public
# hosting; an absolute http(s) URL is passed through as-is.
STATUS_PAGE_ICON = os.environ.get("STATUS_PAGE_ICON", "/static/brand/icon.png").strip()
# Custom CSS pushed to the Kuma status page so it matches the admin UI's theme.
# Leave STATUS_PAGE_CUSTOM_CSS empty to use the generated brand theme; set it to
# your own CSS to override, or set STATUS_PAGE_BRAND_CSS=0 to manage CSS yourself.
STATUS_PAGE_CUSTOM_CSS = os.environ.get("STATUS_PAGE_CUSTOM_CSS", "")
STATUS_PAGE_BRAND_CSS = os.environ.get("STATUS_PAGE_BRAND_CSS", "1") == "1"

# Maintenance sync (opt-in): mirror Pelican power schedules into Kuma maintenance
# windows so scheduled-offline shows as "maintenance" instead of degraded.
MAINTENANCE_SYNC_ENABLED = os.environ.get("MAINTENANCE_SYNC_ENABLED", "0") == "1"
# Timezone Pelican evaluates schedule crons in (panel TZ; UTC by default). Used
# both to interpret the crons and as the Kuma maintenance timezone.
SCHEDULE_TZ = os.environ.get("SCHEDULE_TZ", "UTC").strip() or "UTC"
MAINTENANCE_STATE_PATH = Path(os.environ.get("MAINTENANCE_STATE_PATH", "/data/maintenance_state.json"))
SCHEDULE_CACHE_PATH = Path(os.environ.get("SCHEDULE_CACHE_PATH", "/data/pelican_schedules_cache.json"))
SCHEDULE_CACHE_TTL_SECONDS = int(os.environ.get("SCHEDULE_CACHE_TTL_SECONDS", "3600"))

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
    data = read_json(path, None)
    if isinstance(data, dict):
        ts = data.get("_cached_at", 0)
        if time.time() - ts <= ttl_seconds:
            return data
    return None


def save_cache(path: Path, payload: dict) -> None:
    payload["_cached_at"] = time.time()
    try:
        write_json(path, payload)  # atomic temp+replace
    except Exception:
        pass


def load_state(path: Path) -> Dict[str, dict]:
    data = read_json(path, {})
    return data if isinstance(data, dict) else {}


def save_state(path: Path, payload: dict) -> None:
    try:
        write_json(path, payload)  # atomic temp+replace
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


def fetch_server_schedules(identifier: str) -> List[dict]:
    """Client API: GET /api/client/servers/{identifier}/schedules?include=tasks
    Returns the list of schedule objects (each with `.attributes`)."""
    r = requests.get(
        f"{PEL_URL}/api/client/servers/{identifier}/schedules?include=tasks",
        headers=pel_client_headers(),
        timeout=15,
        verify=PEL_SSL_VERIFY,
    )
    r.raise_for_status()
    data = r.json().get("data", [])
    return data if isinstance(data, list) else []


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


def get_monitor_tags(api: UptimeKumaApi, mon: dict) -> List[Tuple[int, str]]:
    """Return [(tag_id, tag_name)] for a monitor across Kuma version shapes.

    Prefer the tags already present in the bulk get_monitors() payload; only fall
    back to a per-monitor api.get_monitor() call when they're absent (avoids an
    N+1 of one socket round-trip per monitor every run)."""
    tags = mon.get("tags")
    if tags is None:
        try:
            tags = (api.get_monitor(mon.get("id")) or {}).get("tags") or []
        except Exception:
            return []
    out: List[Tuple[int, str]] = []
    for t in tags or []:
        if not isinstance(t, dict):
            continue
        tag_obj = t.get("tag") if isinstance(t.get("tag"), dict) else t
        tid = tag_obj.get("tag_id") if "tag_id" in tag_obj else tag_obj.get("id")
        tname = tag_obj.get("name")
        if isinstance(tid, (int, str)) and isinstance(tname, str):
            try:
                out.append((int(tid), tname))
            except (TypeError, ValueError):
                continue
    return out


# Status-page palette — single source of truth, mirrored from app/static/style.css.
# Keep these in sync with the :root vars there (admin UI) if the theme changes.
STATUS_PALETTE = {
    "bg": "#0e1116",
    "card": "#161b22",
    "line": "#262d36",
    "text": "#e6e8eb",
    "muted": "#8b949e",
}
# Cap on an embedded (data-URI) icon's raw size; Kuma stores the icon inline and
# serves it to every status-page viewer, so a huge image would bloat the page.
MAX_ICON_BYTES = 512 * 1024


def build_status_css(brand: dict) -> str:
    """Custom CSS for the Kuma status page, using the SAME palette as the admin
    UI (app/static/style.css) so both surfaces feel like one server. Accent and
    the brand name in the banner comment come from branding (never hardcoded)."""
    color = brand.get("color", "#0ea5e9")
    accent = color if color.startswith("#") else f"#{color}"
    p = STATUS_PALETTE
    css = """
/* __NAME__ theme — matches the admin UI palette */
body, #app, .container, .status-page-body { background-color: __BG__ !important; color: __TEXT__ !important; }
h1, h2, h3, h4, .title, .item-name { color: __TEXT__ !important; }
.description, .text-muted, .timeline, small { color: __MUTED__ !important; }
.shadow-box, .card, .monitor-list .item { background-color: __CARD__ !important; border: 1px solid __LINE__ !important; }
hr, .list-group-item { border-color: __LINE__ !important; }
a, a:hover { color: __ACCENT__ !important; }
.btn-primary, .btn.btn-primary { background-color: __ACCENT__ !important; border-color: __ACCENT__ !important; color: #1a1205 !important; }
.incident, .maintenance-bg-info { border-left: 3px solid __ACCENT__ !important; }
.status-page-wrapper .title-flex, header, .top { border-bottom: 2px solid __ACCENT__ !important; }
img.logo, .logo { border-radius: 8px; }
"""
    repl = {"__ACCENT__": accent, "__BG__": p["bg"], "__CARD__": p["card"],
            "__LINE__": p["line"], "__TEXT__": p["text"], "__MUTED__": p["muted"],
            "__NAME__": brand.get("name", "GameMonitor")}
    for k, v in repl.items():
        css = css.replace(k, v)
    return css.strip()


_STATIC_ROOT = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"))


def _icon_source(brand: dict) -> Optional[str]:
    """Resolve the icon to a source token WITHOUT reading the file: an absolute
    URL ('url:<u>') passed through, or a confined local file ('file:<abspath>').
    Tries STATUS_PAGE_ICON, then the brand avatar, then the logo. Local paths are
    realpath-confined to the static dir (no '..' traversal) and size-capped."""
    for candidate in (STATUS_PAGE_ICON, brand.get("avatar_url", ""), brand.get("logo_url", "")):
        if not candidate:
            continue
        if candidate.startswith(("http://", "https://", "data:")):
            return f"url:{candidate}"
        if candidate.startswith("/"):
            path = os.path.realpath(os.path.join(_STATIC_ROOT, os.path.relpath(candidate, "/static")))
            # Confine to the static dir; reject traversal outside it.
            if path != _STATIC_ROOT and not path.startswith(_STATIC_ROOT + os.sep):
                log(f"[status] icon path rejected (outside static dir): {candidate}")
                continue
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if size > MAX_ICON_BYTES:
                log(f"[status] icon too large ({size}B > {MAX_ICON_BYTES}B), skipping: {candidate}")
                continue
            return f"file:{path}"
    return None


def _icon_fingerprint(source: Optional[str]) -> str:
    """Cheap change token for an icon source — no file read/encode."""
    if not source:
        return ""
    if source.startswith("file:"):
        path = source[len("file:"):]
        try:
            st = os.stat(path)
            return f"file:{path}:{int(st.st_mtime)}:{st.st_size}"
        except OSError:
            return ""
    return source  # url:... is already stable


def _materialize_icon(source: Optional[str]) -> Optional[str]:
    """Turn a source token into a value Kuma accepts (only called when saving):
    a URL as-is, or a base64 data URI for a local file."""
    if not source:
        return None
    if source.startswith("url:"):
        return source[len("url:"):]
    path = source[len("file:"):]
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    import mimetypes
    mime = mimetypes.guess_type(path)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _norm_groups(public_group_list) -> list:
    """Normalize a publicGroupList to a comparable [name, [sorted ids]] form."""
    out = []
    for g in public_group_list or []:
        if not isinstance(g, dict):
            continue
        ids = sorted(int(m["id"]) for m in (g.get("monitorList") or []) if isinstance(m, dict) and "id" in m)
        out.append([str(g.get("name", "")), ids])
    out.sort()
    return out


# Status-page fields this tool does NOT manage — preserved across saves so an
# operator's manual edits (footer, analytics, custom domains, cert-expiry) survive.
_STATUS_PRESERVE = ("footerText", "googleAnalyticsId", "showPoweredBy",
                    "domainNameList", "showCertificateExpiry")


def reconcile_status_page(api: UptimeKumaApi, groups: Dict[str, List[int]], brand: dict) -> None:
    """Create/update a single status page listing all managed monitors, grouped
    by wing/node. Compares against the LIVE page (so external drift is corrected),
    saves only on an actual difference, and preserves operator-set fields it does
    not own. The expensive icon encode happens only when a save is needed.
    Failures here never abort the monitoring run."""
    if not STATUS_PAGE_ENABLED or not STATUS_PAGE_SLUG:
        return

    title = STATUS_PAGE_TITLE or f"{brand['name']} Game Servers"
    theme = STATUS_PAGE_THEME if STATUS_PAGE_THEME in ("light", "dark") else "dark"
    # Branded CSS so the status page matches the admin UI. Operator override wins;
    # STATUS_PAGE_BRAND_CSS=0 hands CSS back to the operator entirely.
    if STATUS_PAGE_CUSTOM_CSS:
        custom_css = STATUS_PAGE_CUSTOM_CSS
    elif STATUS_PAGE_BRAND_CSS:
        custom_css = build_status_css(brand)
    else:
        custom_css = None

    public_groups = [
        {"name": g, "monitorList": [{"id": mid} for mid in groups[g]]}
        for g in sorted(groups.keys(), key=lambda x: x.lower())
        if groups[g]
    ]

    # Fetch the live page once (serves existence check, drift detection, AND
    # field preservation). Failure => assume it needs creating.
    try:
        current = api.get_status_page(STATUS_PAGE_SLUG)
        newly = False
    except Exception:
        current, newly = None, True

    if newly:
        try:
            api.add_status_page(STATUS_PAGE_SLUG, title)
            log(f"[status] created status page slug={STATUS_PAGE_SLUG}")
        except Exception as e:
            log(f"[status] ERROR creating page {type(e).__name__}: {e}")
            return
        cfg, live_groups = {}, []
    else:
        cfg = current.get("config", current) if isinstance(current, dict) else {}
        live_groups = current.get("publicGroupList") if isinstance(current, dict) else []

    # Compare desired managed fields against what's live; only save on a real diff
    # or when the icon source changed (icon can't be read back to compare).
    icon_source = _icon_source(brand)
    icon_fp = _icon_fingerprint(icon_source)
    desired = {"title": title, "theme": theme, "groups": _norm_groups(public_groups)}
    live = {"title": cfg.get("title"), "theme": cfg.get("theme"), "groups": _norm_groups(live_groups)}
    if custom_css is not None:
        desired["css"] = custom_css
        live["css"] = cfg.get("customCSS") or ""
    saved_state = load_state(STATUS_PAGE_STATE_PATH)
    if not newly and desired == live and icon_fp == saved_state.get("icon_fp", ""):
        return

    # Preserve operator-set fields we don't manage, plus published/showTags
    # (default True only on first creation, never overriding a later operator change).
    kwargs: Dict[str, object] = {}
    for k in _STATUS_PRESERVE:
        if isinstance(cfg, dict) and k in cfg and cfg.get(k) not in (None,):
            kwargs[k] = cfg[k]
    kwargs["published"] = True if newly else cfg.get("published", True)
    kwargs["showTags"] = True if newly else cfg.get("showTags", True)
    if custom_css is None and isinstance(cfg, dict) and cfg.get("customCSS"):
        kwargs["customCSS"] = cfg["customCSS"]  # operator-managed CSS, keep it

    kwargs.update(
        title=title,
        description=f"Live status of {brand['name']} game servers.",
        theme=theme,
        publicGroupList=public_groups,
    )
    icon = _materialize_icon(icon_source)  # expensive read/encode — only now
    if icon:
        kwargs["icon"] = icon
    if custom_css is not None:
        kwargs["customCSS"] = custom_css
    # Tie the footer to the brand only on first creation (don't resurrect a footer
    # the operator deliberately cleared).
    if newly:
        kwargs.setdefault("footerText", brand["name"])

    try:
        api.save_status_page(STATUS_PAGE_SLUG, **kwargs)
        save_state(STATUS_PAGE_STATE_PATH, {"icon_fp": icon_fp})
        log(f"[status] saved status page slug={STATUS_PAGE_SLUG} groups={len(public_groups)}")
    except Exception as e:
        log(f"[status] ERROR saving page {type(e).__name__}: {e}")


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

    brand = branding()

    # Per-server Discord webhooks (managed via admin UI / webhooks.json).
    webhooks_cfg = load_webhooks()
    discord_enabled = bool(webhooks_cfg.get("default")) or any(
        isinstance(e, dict) and e.get("webhook_url")
        for e in webhooks_cfg.get("servers", {}).values()
    )
    # Always track state (even when notifications are off) so re-enabling later
    # compares against a fresh baseline instead of firing a stale-state burst.
    state_cache: Dict[str, dict] = load_state(DISCORD_STATE_PATH)
    if not discord_enabled:
        log("[discord] disabled (no webhooks configured)")

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

        # Monitors are keyed by name in Kuma, but Pelican server names are not
        # unique (e.g. two "Minecraft" servers on different nodes). Count names so
        # collisions can be disambiguated with the unique identifier; unique names
        # stay unchanged (no monitor churn for existing single-name deployments).
        name_counts: Dict[str, int] = {}
        for s in servers:
            n = monitor_name(str(s.get("name", "")).strip())
            if n:
                name_counts[n] = name_counts.get(n, 0) + 1

        def disamb(server_name: str, ident: str) -> str:
            """Monitor display name, disambiguated with the identifier on collision."""
            nm = monitor_name(server_name)
            return f"{nm} ({ident})" if name_counts.get(nm, 0) > 1 else nm

        running_now: List[Tuple[str, str, str, Optional[str]]] = []
        created_names: set[str] = set()
        # (identifier, server_name, new_status)
        pending_notifications: List[Tuple[str, str, str]] = []

        # Only create/push for running/starting
        for s in servers:
            sname = str(s.get("name", "")).strip()
            identifier = str(s.get("identifier", "")).strip()
            if not sname or not identifier:
                continue

            name = disamb(sname, identifier)

            try:
                resources = fetch_server_resources(identifier)
                state = extract_state(resources)
                log(f"[pelican] {sname} ({identifier}) state={state}")
            except Exception as e:
                log(f"[pelican] {sname} ({identifier}) resources ERROR {type(e).__name__}: {e}")
                continue

            status = "up" if state in RUNNING_STATES else "down"
            prev_entry = state_cache.get(identifier)
            prev_status = prev_entry.get("status") if isinstance(prev_entry, dict) else None

            # Only notify on a change after we've seen the server before, and only
            # when notifications are enabled — but always update the baseline below.
            if discord_enabled and prev_status and prev_status != status:
                pending_notifications.append((identifier, sname, status))

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

        # Discord notifications (per-server webhook, branded embed)
        for identifier, sname, status in pending_notifications:
            url = webhook_for(identifier, webhooks_cfg)
            if not url:
                continue
            color = COLOR_UP if status == "up" else COLOR_DOWN
            title = f"{sname} is {status.upper()}"
            desc = f"Server **{sname}** changed state to **{status.upper()}**."
            try:
                send_discord(url, title, desc, color)
                log(f"[discord] sent ({identifier}): {title}")
            except Exception as e:
                log(f"[discord] ERROR {type(e).__name__}: {e}")

        # Persist the baseline every run, enabled or not (see load_state above).
        save_state(DISCORD_STATE_PATH, state_cache)

        # Single pass over managed (push) monitors:
        #   - delete stale managed monitors (no heartbeat past cutoff)
        #   - collect surviving managed monitors into wing groups for the status page
        #   - retroactively apply a missing wing tag for running monitors (covers
        #     monitors created before node perms were granted, or node renames)
        node_by_name = {nm: nn for (nm, _i, _s, nn) in running_now if nn}
        monitors = api.get_monitors()
        wing_prefix = f"{KUMA_WING_TAG_PREFIX}:"
        status_groups: Dict[str, List[int]] = {}
        for mon in monitors:
            mid = mon.get("id")
            # Managed monitors are always Push monitors; skip everything else so
            # we never read tags for monitors this tool doesn't own.
            if not mid or not mon.get("pushToken"):
                continue

            mtags = get_monitor_tags(api, mon)
            is_managed = any(tid == managed_tag_id for tid, _ in mtags)
            if not is_managed:
                continue

            hb = get_last_hb_ms(mon)
            if hb is not None and hb < cutoff_ms:
                try:
                    api.delete_monitor(int(mid))
                    log(f"[kuma] deleted stale managed monitor id={mid}")
                except Exception:
                    pass
                continue

            # Group by wing tag (strip the "wing:" prefix); else "Other".
            group = STATUS_PAGE_UNGROUPED
            for _, tname in mtags:
                if tname.startswith(wing_prefix):
                    group = tname[len(wing_prefix):] or STATUS_PAGE_UNGROUPED
                    break

            # Untagged-but-running monitor whose node we now know: add the wing
            # tag (safe — it has none, so no duplicate) so grouping self-heals.
            if group == STATUS_PAGE_UNGROUPED:
                node_name = node_by_name.get(str(mon.get("name", "")))
                if node_name:
                    wing_tag_id = ensure_tag_id(api, tags_by_name, wing_tag_name(node_name))
                    add_tag_to_monitor(api, int(mid), wing_tag_id)
                    group = node_name

            status_groups.setdefault(group, []).append(int(mid))

        # Dynamic status page reflecting current managed monitors
        reconcile_status_page(api, status_groups, brand)

        # Maintenance windows synced from Pelican power schedules (opt-in): mark
        # expected-offline hours as "maintenance" so they don't read as degraded.
        if MAINTENANCE_SYNC_ENABLED:
            # Validate the timezone once (Kuma takes UTC / SAME_AS_SERVER / IANA).
            tz = SCHEDULE_TZ
            if tz not in ("UTC", "SAME_AS_SERVER"):
                try:
                    from zoneinfo import ZoneInfo
                    ZoneInfo(tz)
                except Exception:
                    log(f"[maint] invalid SCHEDULE_TZ {tz!r}; using UTC")
                    tz = "UTC"

            # Fresh monitor list AFTER stale-deletion, so we never resolve to a
            # monitor id that was just deleted this run.
            id_by_name = {str(m.get("name", "")): m.get("id")
                          for m in api.get_monitors() if m.get("pushToken")}

            # Per-server schedule/window cache with INDEPENDENT TTLs (so refetching
            # one server doesn't slide the freshness window for the rest). The
            # parsed windows are cached too, so the parse only runs on a fetch.
            cache = read_json(SCHEDULE_CACHE_PATH, {})
            cache_servers = cache.get("servers") if isinstance(cache, dict) else {}
            if not isinstance(cache_servers, dict):
                cache_servers = {}
            now = time.time()

            def _fresh(ident: str) -> bool:
                ent = cache_servers.get(ident)
                return isinstance(ent, dict) and (now - ent.get("at", 0)) <= SCHEDULE_CACHE_TTL_SECONDS

            idents = [str(s.get("identifier", "")).strip() for s in servers]
            to_fetch = [i for i in idents if i and not _fresh(i)]
            if to_fetch:
                # Concurrent fetch so a cold/expired cache doesn't serialize N×timeout.
                from concurrent.futures import ThreadPoolExecutor

                def _fetch(idn):
                    try:
                        return idn, maint.compute_off_windows(fetch_server_schedules(idn)), None
                    except Exception as e:
                        return idn, None, e

                with ThreadPoolExecutor(max_workers=min(8, len(to_fetch))) as ex:
                    for idn, win, err in ex.map(_fetch, to_fetch):
                        if err is not None:
                            log(f"[maint] {idn} schedules ERROR {type(err).__name__}: {err}")
                            continue  # leave stale/absent; retried next run
                        cache_servers[idn] = ({"at": now, "w": win} if win is not None
                                              else {"at": now, "u": True})
                write_json(SCHEDULE_CACHE_PATH, {"servers": cache_servers})

            monitor_by_server: Dict[str, int] = {}
            names_by_server: Dict[str, str] = {}
            windows_by_server: Dict[str, list] = {}
            known_idents: set = set()

            for s in servers:
                sname = str(s.get("name", "")).strip()
                identifier = str(s.get("identifier", "")).strip()
                if not sname or not identifier:
                    continue
                known_idents.add(identifier)
                names_by_server[identifier] = sname
                ent = cache_servers.get(identifier)
                if not isinstance(ent, dict) or ent.get("u"):
                    continue  # fetch failed this run, or ambiguous -> leave unmanaged
                windows = ent.get("w") or []
                windows_by_server[identifier] = windows

                name = disamb(sname, identifier)
                mid = id_by_name.get(name)
                # An always-scheduled-off server (the core case) has never run, so
                # has no monitor; create one so maintenance can attach and it shows
                # on the status page (as maintenance during its off hours).
                if mid is None and windows:
                    nid = extract_node_id(s)
                    node_name = nodes.get(nid) if nid is not None else None
                    try:
                        resp = api.add_monitor(type=MonitorType.PUSH, name=name, interval=KUMA_INTERVAL)
                        mid = resp.get("monitorID") if isinstance(resp, dict) else resp
                        add_tag_to_monitor(api, int(mid), managed_tag_id)
                        if node_name:
                            add_tag_to_monitor(api, int(mid),
                                               ensure_tag_id(api, tags_by_name, wing_tag_name(node_name)))
                        log(f"[maint] created monitor for scheduled-off server: {name}")
                    except Exception as e:
                        log(f"[maint] {identifier}: monitor create failed {type(e).__name__}: {e}")
                        mid = None
                if mid:
                    monitor_by_server[identifier] = int(mid)

            maint_state = load_state(MAINTENANCE_STATE_PATH)
            new_state = maint.reconcile_maintenance(
                api, windows_by_server, monitor_by_server, names_by_server,
                known_idents, maint_state,
                brand_name=brand["name"], timezone_option=tz, log=log,
            )
            save_state(MAINTENANCE_STATE_PATH, new_state)


if __name__ == "__main__":
    main()
