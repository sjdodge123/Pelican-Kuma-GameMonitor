# Brand assets (GamerSaloon)

These images are self-hosted by the admin container and served at `/static/brand/`:

- `logo.png`   — GAMER SALOON banner (status page + admin header). Watermark removed.
- `avatar.png` — square saloon-doors crop for the Discord webhook avatar. Watermark removed.

They're wired up in `.env.example` as root-relative paths:

    BRAND_LOGO_URL=/static/brand/logo.png
    BRAND_AVATAR_URL=/static/brand/avatar.png

A root-relative path only resolves inside the admin UI itself. For Discord (which
fetches the avatar from the internet) and the Kuma status-page icon (loaded by
status-page viewers), set the container's externally-reachable base URL so the
path is turned into an absolute URL:

    BRAND_ASSET_BASE_URL=https://monitor.gamersaloon.example

You can also bypass all of this by pasting full `https://...` URLs straight into
`BRAND_LOGO_URL` / `BRAND_AVATAR_URL` (absolute URLs pass through unchanged).

To swap artwork, replace these files (and rebuild/redeploy the container image).
