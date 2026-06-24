"""Shared Discord notification helper (used by the monitor and the admin UI)."""
import os

import requests

from branding import branding

# By default each Discord webhook's own configured name + avatar identify the
# message (set in Discord's Integrations > Webhooks UI), so per-channel webhooks
# can carry their own game's name/icon. Set DISCORD_USE_BRAND_IDENTITY=1 to
# instead override every message with the brand username/avatar.
USE_BRAND_IDENTITY = os.environ.get("DISCORD_USE_BRAND_IDENTITY", "0") == "1"


def send_discord(webhook_url: str, title: str, description: str, color: int) -> None:
    """Post a branded embed to a Discord webhook. The embed color comes from the
    brand; the message name/avatar default to the webhook's own Discord config
    unless DISCORD_USE_BRAND_IDENTITY=1."""
    if not webhook_url:
        return
    payload = {
        "embeds": [{"title": title, "description": description, "color": color}],
    }
    if USE_BRAND_IDENTITY:
        b = branding()
        payload["username"] = b["webhook_username"]
        # Discord fetches avatar_url from its own servers, so only send one it
        # can reach. A root-relative path (no BRAND_ASSET_BASE_URL) would 404.
        av = b["avatar_url"]
        if av.startswith(("http://", "https://")):
            payload["avatar_url"] = av
    requests.post(webhook_url, json=payload, timeout=10).raise_for_status()
