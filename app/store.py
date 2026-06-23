"""Shared JSON-backed config/state store.

Used by both the monitor (update_kuma.py) and the admin UI (admin.py) so the
two processes stay decoupled: the monitor owns the Pelican server cache, the
admin UI owns the per-server Discord webhook mapping, and both live in /data.
"""
import os
import json
from pathlib import Path
from typing import Dict, List, Optional

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))

WEBHOOKS_PATH = Path(
    os.environ.get("WEBHOOKS_CONFIG_PATH", str(DATA_DIR / "webhooks.json"))
)
BRANDING_PATH = Path(
    os.environ.get("BRANDING_CONFIG_PATH", str(DATA_DIR / "branding.json"))
)
# The monitor writes this cache; the admin UI reads it to list servers without
# needing any Pelican API keys of its own.
PEL_SERVERS_CACHE_PATH = Path(
    os.environ.get("CACHE_PATH", str(DATA_DIR / "pelican_servers_cache.json"))
)


def _read_json(path: Path, default):
    try:
        if path.exists():
            data = json.loads(path.read_text())
            return data
    except Exception:
        pass
    return default


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)  # atomic on POSIX


# Public aliases so other modules (e.g. the monitor) share one atomic-write /
# tolerant-read implementation instead of hand-rolling their own.
def read_json(path: Path, default=None):
    return _read_json(path, default)


def write_json(path: Path, payload) -> None:
    _write_json(path, payload)


# --------------------
# Per-server Discord webhooks
# --------------------
def load_webhooks() -> dict:
    """Return {"default": <url>, "servers": {<identifier>: {name, webhook_url}}}.

    `default` falls back to the legacy DISCORD_WEBHOOK_URL env var so existing
    single-webhook deployments keep working with no config file.
    """
    data = _read_json(WEBHOOKS_PATH, {})
    if not isinstance(data, dict):
        data = {}
    if not data.get("default"):
        data["default"] = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    servers = data.get("servers")
    if not isinstance(servers, dict):
        servers = {}
    data["servers"] = servers
    return data


def save_webhooks(default_url: str, servers: Dict[str, dict]) -> None:
    clean: Dict[str, dict] = {}
    for ident, entry in (servers or {}).items():
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("webhook_url", "")).strip()
        clean[ident] = {
            "name": str(entry.get("name", "")).strip(),
            "webhook_url": url,
        }
    _write_json(WEBHOOKS_PATH, {"default": (default_url or "").strip(), "servers": clean})


def webhook_for(identifier: str, cfg: Optional[dict] = None) -> str:
    """Resolve the webhook URL for a server identifier (per-server, else default)."""
    data = cfg if cfg is not None else load_webhooks()
    entry = data.get("servers", {}).get(identifier)
    if isinstance(entry, dict) and entry.get("webhook_url"):
        return str(entry["webhook_url"]).strip()
    return str(data.get("default") or "").strip()


# --------------------
# Branding overrides (admin-editable; env vars are the fallback defaults)
# --------------------
BRANDING_FIELDS = ("name", "logo_url", "avatar_url", "webhook_username", "color", "url")


def load_branding() -> Dict[str, str]:
    """Return the raw admin-set branding overrides (may be partial/empty).

    Effective branding (overrides merged over env defaults) is computed in
    branding.py — this only returns what the admin UI has saved.
    """
    data = _read_json(BRANDING_PATH, {})
    if not isinstance(data, dict):
        return {}
    return {k: str(data.get(k, "")).strip() for k in BRANDING_FIELDS if data.get(k)}


def save_branding(values: Dict[str, str]) -> None:
    clean = {k: str(values.get(k, "")).strip() for k in BRANDING_FIELDS}
    _write_json(BRANDING_PATH, clean)


# --------------------
# Pelican server list (read from the monitor's cache)
# --------------------
def list_pelican_servers() -> List[dict]:
    data = _read_json(PEL_SERVERS_CACHE_PATH, {})
    servers = data.get("servers") if isinstance(data, dict) else None
    out: List[dict] = []
    if isinstance(servers, list):
        for s in servers:
            if not isinstance(s, dict):
                continue
            name = str(s.get("name", "")).strip()
            ident = str(s.get("identifier", "")).strip()
            if name and ident:
                out.append({"name": name, "identifier": ident})
    out.sort(key=lambda x: x["name"].lower())
    return out
