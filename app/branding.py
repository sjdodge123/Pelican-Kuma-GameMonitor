"""Branding configuration.

Effective branding is, per field: the admin-saved override (store.load_branding,
from /data/branding.json) if set, else the BRAND_* env var, else a neutral code
default. The admin UI can edit/persist overrides live; env vars provide the
initial defaults — e.g. the GamerSaloon deployment sets BRAND_NAME=GamerSaloon.
"""
import os

from store import load_branding


def _hex_to_int(color: str) -> int:
    """Convert a #rrggbb / rrggbb hex string to a Discord embed color int."""
    try:
        return int(color.strip().lstrip("#"), 16)
    except Exception:
        return 0x0EA5E9


def _resolve(overrides: dict, key: str, env_key: str, default: str = "") -> str:
    """Override (admin-saved) wins, then env var, then default."""
    val = (overrides.get(key) or "").strip()
    if val:
        return val
    return os.environ.get(env_key, "").strip() or default


def _absolutize(url: str) -> str:
    """Turn a root-relative asset path (e.g. /static/brand/logo.png) into an
    absolute URL using BRAND_ASSET_BASE_URL, so the container can self-host the
    brand images and still hand Discord / the Kuma status page a fetchable URL.
    Already-absolute (http...) URLs are returned unchanged.
    """
    base = os.environ.get("BRAND_ASSET_BASE_URL", "").strip().rstrip("/")
    if url.startswith("/") and base:
        return base + url
    return url


def branding() -> dict:
    o = load_branding()
    name = _resolve(o, "name", "BRAND_NAME", "GameMonitor")
    logo_url = _absolutize(_resolve(o, "logo_url", "BRAND_LOGO_URL"))
    color = _resolve(o, "color", "BRAND_COLOR", "#0ea5e9")
    return {
        "name": name,
        "logo_url": logo_url,
        # Discord webhook avatar defaults to the logo if not separately set.
        "avatar_url": _absolutize(_resolve(o, "avatar_url", "BRAND_AVATAR_URL")) or logo_url,
        "webhook_username": _resolve(o, "webhook_username", "BRAND_WEBHOOK_USERNAME") or name,
        "color": color,
        "color_int": _hex_to_int(color),
        "url": _resolve(o, "url", "BRAND_URL"),
    }


# Status-change embed colors (override the brand color for clarity).
COLOR_UP = 0x2ECC71
COLOR_DOWN = 0xE74C3C
