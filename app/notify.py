"""Shared Discord notification helper (used by the monitor and the admin UI)."""
import requests

from branding import branding
from settings import settings


def send_discord(webhook_url: str, title: str, description: str, color: int) -> None:
    """Post a branded embed to a Discord webhook. The embed color comes from the
    brand; the message name/avatar default to the webhook's own Discord config
    unless the `discord_use_brand_identity` setting is on. Resolved per call so
    the long-lived admin UI honors live setting changes."""
    if not webhook_url:
        return
    payload = {
        "embeds": [{"title": title, "description": description, "color": color}],
    }
    if settings()["discord_use_brand_identity"]:
        b = branding()
        payload["username"] = b["webhook_username"]
        # Discord fetches avatar_url from its own servers, so only send one it
        # can reach. A root-relative path (no BRAND_ASSET_BASE_URL) would 404.
        av = b["avatar_url"]
        if av.startswith(("http://", "https://")):
            payload["avatar_url"] = av
    requests.post(webhook_url, json=payload, timeout=10).raise_for_status()
