"""Shared Discord notification helper (used by the monitor and the admin UI)."""
import requests

from branding import branding


def send_discord(webhook_url: str, title: str, description: str, color: int) -> None:
    """Post a branded embed to a Discord webhook (brand username/avatar)."""
    if not webhook_url:
        return
    b = branding()
    payload = {
        "username": b["webhook_username"],
        "embeds": [{"title": title, "description": description, "color": color}],
    }
    # Discord fetches avatar_url from its own servers, so only send one it can
    # reach. A root-relative path (no BRAND_ASSET_BASE_URL) would 404 — skip it.
    av = b["avatar_url"]
    if av.startswith(("http://", "https://")):
        payload["avatar_url"] = av
    requests.post(webhook_url, json=payload, timeout=10).raise_for_status()
