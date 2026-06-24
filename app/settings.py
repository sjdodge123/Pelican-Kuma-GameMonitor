"""Optional runtime settings, editable in the admin UI.

Effective value per field: the admin-saved override (store.load_settings, from
/data/settings.json) if set, else the matching env var, else a code default —
the same override → env → default model as branding.py, but for typed
operational toggles (bool/int/choice/str).

Secrets, connection URLs, file paths and the status-page slug are deliberately
NOT here: they stay env-only so the admin UI keeps no Pelican/Kuma credentials
and the two-process decoupling holds.

The monitor (update_kuma.py) runs as a fresh process each minute, so it reads
effective settings at import; the long-lived admin UI resolves per request.
"""
import os

from store import load_settings

# Each entry: `key` is the storage/return key, `env` the fallback env var, and
# `type` drives both parsing and the admin form widget. `group` buckets fields
# into form sections; `label`/`help` are presentation only.
SETTINGS_SCHEMA = [
    {
        "key": "discord_confirm_runs", "env": "DISCORD_CONFIRM_RUNS",
        "type": "int", "default": 2, "min": 1,
        "group": "Discord / notifications", "label": "Flap debounce (runs)",
        "help": "A status change must persist this many consecutive runs "
                "(~minutes) before notifying. 1 = notify on every change.",
    },
    {
        "key": "discord_use_brand_identity", "env": "DISCORD_USE_BRAND_IDENTITY",
        "type": "bool", "default": False,
        "group": "Discord / notifications",
        "label": "Override webhook name/avatar with brand",
        "help": "Off: each Discord webhook's own name + avatar identify the "
                "message. On: override every message with the brand identity.",
    },
    {
        "key": "status_page_public_url", "env": "STATUS_PAGE_PUBLIC_URL",
        "type": "str", "default": "",
        "group": "Discord / notifications", "label": "Status page public URL",
        "help": "e.g. status.gamernight.net — appends a “View live status” "
                "link to notifications. A bare host is upgraded to https://.",
    },

    {
        "key": "status_page_enabled", "env": "STATUS_PAGE_ENABLED",
        "type": "bool", "default": True,
        "group": "Status page", "label": "Enable status page",
        "help": "Maintain the dynamic Kuma status page each run.",
    },
    {
        "key": "status_page_title", "env": "STATUS_PAGE_TITLE",
        "type": "str", "default": "",
        "group": "Status page", "label": "Title",
        "help": "Defaults to “<brand> Game Servers”.",
    },
    {
        "key": "status_page_theme", "env": "STATUS_PAGE_THEME",
        "type": "choice", "choices": ["dark", "light"], "default": "dark",
        "group": "Status page", "label": "Theme",
    },
    {
        "key": "status_page_ungrouped_label", "env": "STATUS_PAGE_UNGROUPED_LABEL",
        "type": "str", "default": "Other",
        "group": "Status page", "label": "Ungrouped group label",
        "help": "Group name for monitors with no wing tag.",
    },

    {
        "key": "maintenance_sync_enabled", "env": "MAINTENANCE_SYNC_ENABLED",
        "type": "bool", "default": False,
        "group": "Maintenance sync", "label": "Enable maintenance sync",
        "help": "Mirror Pelican power schedules into Kuma maintenance windows.",
    },
    {
        "key": "schedule_tz", "env": "SCHEDULE_TZ",
        "type": "str", "default": "UTC",
        "group": "Maintenance sync", "label": "Schedule timezone",
        "help": "Timezone Pelican evaluates crons in; maintenance windows "
                "created in this tz.",
    },
]

SETTINGS_KEYS = tuple(f["key"] for f in SETTINGS_SCHEMA)

_TRUE = {"1", "true", "yes", "on"}


def _env_bool(env_key: str, default: bool) -> bool:
    raw = os.environ.get(env_key, "").strip()
    return raw.lower() in _TRUE if raw else default


def _resolve_one(o: dict, f: dict):
    """Override (admin-saved, non-empty) wins, then env var, then default."""
    raw = o.get(f["key"])
    has = raw is not None and str(raw).strip() != ""
    typ = f["type"]

    if typ == "bool":
        if has:
            return str(raw).strip().lower() in _TRUE
        return _env_bool(f["env"], f["default"])

    if typ == "int":
        src = str(raw).strip() if has else os.environ.get(f["env"], "").strip()
        try:
            v = int(src) if src else f["default"]
        except ValueError:
            v = f["default"]
        if "min" in f:
            v = max(f["min"], v)
        return v

    if typ == "choice":
        val = str(raw).strip() if has else os.environ.get(f["env"], "").strip()
        return val if val in f["choices"] else f["default"]

    # str
    if has:
        return str(raw).strip()
    return os.environ.get(f["env"], "").strip() or f["default"]


def settings() -> dict:
    """Effective settings keyed by `key` (override → env → default)."""
    o = load_settings()
    if not isinstance(o, dict):
        o = {}
    return {f["key"]: _resolve_one(o, f) for f in SETTINGS_SCHEMA}
