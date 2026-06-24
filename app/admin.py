"""Small Flask admin UI to manage per-server Discord webhooks.

Runs alongside supercronic in the same container. Reads the Pelican server list
from the monitor's cache (no Pelican keys needed here) and the webhook mapping
from /data/webhooks.json, which it lets you edit and test.
"""
import os
import hmac
import functools

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    Response,
)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.middleware.dispatcher import DispatcherMiddleware

from branding import branding, COLOR_UP
from notify import send_discord
from store import (
    load_webhooks,
    save_webhooks,
    list_pelican_servers,
    load_branding,
    save_branding,
    webhook_for,
    BRANDING_FIELDS,
)

app = Flask(__name__)
# Trust one layer of reverse proxy (NPM) for scheme/host/prefix, so redirects and
# url_for honor the external https host instead of the internal http one.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Serve the whole app under a sub-path (e.g. /admin) so it can live on the same
# host as the status page (status.example.net/admin) instead of its own subdomain.
# The proxy forwards the full /admin/... path unchanged; this mounts the app there
# and sets SCRIPT_NAME, so url_for/redirects/static all include the prefix.
ADMIN_URL_PREFIX = os.environ.get("ADMIN_URL_PREFIX", "").strip().rstrip("/")
if ADMIN_URL_PREFIX:
    def _prefix_not_found(environ, start_response):
        start_response("404 Not Found", [("Content-Type", "text/plain")])
        return [b"Not Found (admin UI is served under " + ADMIN_URL_PREFIX.encode() + b")"]
    app.wsgi_app = DispatcherMiddleware(_prefix_not_found, {ADMIN_URL_PREFIX: app.wsgi_app})

# Predictable default would let anyone forge flash/session cookies — require an
# explicit secret in any real deployment, but keep a usable dev default.
app.secret_key = os.environ.get("ADMIN_SECRET_KEY", "gamemonitor-admin-dev")

ADMIN_USER = os.environ.get("ADMIN_USER", "").strip() or "admin"
ADMIN_PASS = os.environ.get("ADMIN_PASS", "").strip()

# Used only to build outbound links to Kuma (no API access — keeps decoupling).
KUMA_URL = os.environ.get("KUMA_URL", "").strip().rstrip("/")
STATUS_PAGE_SLUG = os.environ.get("STATUS_PAGE_SLUG", "gamersaloon").strip()
STATUS_PAGE_ENABLED = os.environ.get("STATUS_PAGE_ENABLED", "1") == "1"


def _kuma_links() -> dict:
    if not KUMA_URL:
        return {}
    links = {"dashboard": KUMA_URL}
    if STATUS_PAGE_ENABLED and STATUS_PAGE_SLUG:
        links["status"] = f"{KUMA_URL}/status/{STATUS_PAGE_SLUG}"
    return links
# Auth turns on as soon as a password is set (username defaults to "admin").
# Setting only one of the two no longer silently leaves the panel wide open.
AUTH_ENABLED = bool(ADMIN_PASS)


def _check_auth(auth) -> bool:
    if not auth:
        return False
    # Constant-time compares to avoid leaking credentials via response timing.
    user_ok = hmac.compare_digest(auth.username or "", ADMIN_USER)
    pass_ok = hmac.compare_digest(auth.password or "", ADMIN_PASS)
    return user_ok and pass_ok


def require_auth(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if AUTH_ENABLED and not _check_auth(request.authorization):
            return Response(
                "Authentication required.",
                401,
                {"WWW-Authenticate": 'Basic realm="GameMonitor Admin"'},
            )
        return view(*args, **kwargs)

    return wrapped


def _rows():
    """Merge the cached server list with stored webhook assignments.

    Includes servers that no longer appear in the cache but still have a saved
    webhook, so assignments are never silently lost.
    """
    cfg = load_webhooks()
    saved = cfg.get("servers", {})
    rows = []
    seen = set()
    for s in list_pelican_servers():
        ident = s["identifier"]
        seen.add(ident)
        entry = saved.get(ident, {})
        rows.append({
            "identifier": ident,
            "name": s["name"],
            "webhook_url": entry.get("webhook_url", ""),
            "known": True,
        })
    for ident, entry in saved.items():
        if ident in seen:
            continue
        rows.append({
            "identifier": ident,
            "name": entry.get("name", ident),
            "webhook_url": entry.get("webhook_url", ""),
            "known": False,
        })
    rows.sort(key=lambda r: r["name"].lower())
    return cfg.get("default", ""), rows


@app.route("/healthz")
def healthz():
    return {"status": "ok"}


@app.route("/")
@require_auth
def index():
    default_url, rows = _rows()
    return render_template(
        "index.html",
        brand=branding(),            # effective values (for header + placeholders)
        overrides=load_branding(),   # raw admin-saved values (for input values)
        default_url=default_url,
        rows=rows,
        kuma=_kuma_links(),
    )


@app.route("/branding", methods=["POST"])
@require_auth
def save_brand():
    values = {f: request.form.get(f"brand__{f}", "").strip() for f in BRANDING_FIELDS}
    save_branding(values)
    flash("Branding saved.", "success")
    return redirect(url_for("index"))


@app.route("/save", methods=["POST"])
@require_auth
def save():
    # Merge onto the stored config rather than replacing it, so a server that
    # was added (or edited by another admin) after this form rendered isn't
    # wiped just because it wasn't in this POST.
    cfg = load_webhooks()
    servers = dict(cfg.get("servers", {}))
    # Hidden inputs name__<ident> carry the display name alongside url__<ident>.
    for key, value in request.form.items():
        if not key.startswith("url__"):
            continue
        ident = key[len("url__"):]
        servers[ident] = {
            "name": request.form.get(f"name__{ident}", "").strip(),
            "webhook_url": value.strip(),
        }
    save_webhooks(request.form.get("default_url", "").strip(), servers)
    flash("Webhook configuration saved.", "success")
    return redirect(url_for("index"))


@app.route("/test", methods=["POST"])
@require_auth
def test():
    """Send a test embed to a single server's resolved webhook — using the same
    resolution (webhook_for) and sender (send_discord) as real notifications, so
    a passing test reflects what a real alert would do."""
    ident = request.form.get("identifier", "").strip()
    name = request.form.get(f"name__{ident}", "").strip() or ident
    url = webhook_for(ident)
    if not url:
        flash("No webhook configured for that server (and no default set).", "error")
        return redirect(url_for("index"))

    b = branding()
    try:
        send_discord(
            url,
            f"{b['name']} test notification",
            f"Test alert for **{name}**.",
            COLOR_UP,
        )
        flash("Test notification sent.", "success")
    except Exception as e:
        flash(f"Test failed: {type(e).__name__}: {e}", "error")
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("ADMIN_PORT", "8080")))
