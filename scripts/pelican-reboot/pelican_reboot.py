#!/usr/bin/env python3
"""Cleanly stop ALL Pelican game servers, reboot the host, and start the
previously-running ones back up after boot. Fully dynamic: the server list is
fetched from the panel at run time, so newly added servers are covered without
any per-server configuration.

Usage:
  pelican_reboot.py ramwatch
      Check the host's available RAM. After RAM_BREACH_COUNT consecutive
      low-memory checks (and outside REBOOT_COOLDOWN_HOURS of the last
      triggered reboot), run the full shutdown+reboot flow. Intended to be
      fired every few minutes by pelican-ramwatch.timer.
  pelican_reboot.py shutdown [--dry-run] [--now]
      Set a PuK maintenance block, warn connected players (90s countdown by
      default; --now skips the countdown), record which servers are running,
      send each a clean stop, wait until they are offline (kill stragglers
      after STOP_TIMEOUT), then reboot.
  pelican_reboot.py restore [--dry-run]
      After boot: wait for the panel, then start the servers recorded by the
      last shutdown (staggered, to avoid a boot-time resource spike).

Config comes from the environment (see pelican-reboot.env.example):
  PEL_URL              panel base URL, e.g. https://panel.example.com
  PEL_APP_KEY          Application API key (lists all servers)
  PEL_CLIENT_KEY       Client API key of an ADMIN user (power/state/console)
  RAM_MIN_AVAILABLE_MB reboot trigger threshold (default 1024)
  RAM_BREACH_COUNT     consecutive low checks required (default 3)
  REBOOT_COOLDOWN_HOURS min hours between triggered reboots (default 12)
  NOTICE_SCHEDULE      countdown marks in seconds (default "90,30,10"; empty = no notices)
  NOTICE_COMMAND       console command template (default "say {msg}")
  NOTICE_MESSAGE       message template (default below; {seconds} substituted)
  PUK_SUPPRESS_URL     PuK admin endpoint, e.g. https://host/admin/api/suppress
  PUK_ADMIN_USER/PASS  PuK admin basic-auth credentials
  SUPPRESS_MINUTES     length of the PuK maintenance block (default 30)
  STOP_TIMEOUT         seconds to wait for clean stops    (default 300)
  KILL_GRACE           seconds to wait after kill signal  (default 30)
  START_STAGGER        seconds between server starts      (default 20)
  PANEL_WAIT_TIMEOUT   seconds to wait for panel on boot  (default 600)
  STATE_DIR            state directory (default /var/lib/pelican-reboot)
  REBOOT_CMD           command run after shutdown (default /sbin/reboot; empty = don't)

Only Python 3 stdlib is used — no pip installs needed on the host.
"""

import base64
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

PEL_URL = os.environ.get("PEL_URL", "").rstrip("/")
PEL_APP_KEY = os.environ.get("PEL_APP_KEY", "")
PEL_CLIENT_KEY = os.environ.get("PEL_CLIENT_KEY", "")
PEL_SSL_VERIFY = os.environ.get("PEL_SSL_VERIFY", "1") == "1"

RAM_MIN_AVAILABLE_MB = int(os.environ.get("RAM_MIN_AVAILABLE_MB", "1024"))
RAM_BREACH_COUNT = int(os.environ.get("RAM_BREACH_COUNT", "3"))
REBOOT_COOLDOWN_HOURS = float(os.environ.get("REBOOT_COOLDOWN_HOURS", "12"))

NOTICE_SCHEDULE = os.environ.get("NOTICE_SCHEDULE", "90,30,10")
NOTICE_COMMAND = os.environ.get("NOTICE_COMMAND", "say {msg}")
NOTICE_MESSAGE = os.environ.get(
    "NOTICE_MESSAGE", "SERVER REBOOTING for maintenance in {seconds} seconds! It will be back in a few minutes.")

PUK_SUPPRESS_URL = os.environ.get("PUK_SUPPRESS_URL", "").strip()
PUK_ADMIN_USER = os.environ.get("PUK_ADMIN_USER", "admin")
PUK_ADMIN_PASS = os.environ.get("PUK_ADMIN_PASS", "")
SUPPRESS_MINUTES = int(os.environ.get("SUPPRESS_MINUTES", "30"))

STOP_TIMEOUT = int(os.environ.get("STOP_TIMEOUT", "300"))
KILL_GRACE = int(os.environ.get("KILL_GRACE", "30"))
START_STAGGER = int(os.environ.get("START_STAGGER", "20"))
PANEL_WAIT_TIMEOUT = int(os.environ.get("PANEL_WAIT_TIMEOUT", "600"))
POLL_INTERVAL = 10

STATE_DIR = os.environ.get("STATE_DIR", "/var/lib/pelican-reboot")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
RAMWATCH_FILE = os.path.join(STATE_DIR, "ramwatch.json")
REBOOT_CMD = os.environ.get("REBOOT_CMD", "/sbin/reboot")

RUNNING_STATES = {"running", "starting"}


def log(msg: str) -> None:
    print(f"[pelican-reboot] {msg}", flush=True)


def _request(method: str, path: str, key: str, body=None):
    url = f"{PEL_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {key}",
        "Accept": "Application/vnd.pterodactyl.v1+json",
        "Content-Type": "application/json",
    })
    ctx = None if PEL_SSL_VERIFY else ssl._create_unverified_context()
    with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else None


def _read_state(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return default


def _write_state(path, payload) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh)
    os.replace(tmp, path)


# --------------------
# Pelican API
# --------------------
def list_servers() -> list:
    """Application API: all servers on the panel (paginated)."""
    servers, page = [], 1
    while True:
        data = _request("GET", f"/api/application/servers?page={page}&per_page=100", PEL_APP_KEY)
        for item in data.get("data", []):
            attr = item.get("attributes", {}) or {}
            if attr.get("identifier") and attr.get("name"):
                servers.append({"identifier": attr["identifier"], "name": attr["name"]})
        pagination = (data.get("meta", {}) or {}).get("pagination", {}) or {}
        if page >= pagination.get("total_pages", page):
            break
        page += 1
    return servers


def server_state(identifier: str) -> str:
    try:
        data = _request("GET", f"/api/client/servers/{identifier}/resources", PEL_CLIENT_KEY)
    except Exception as e:
        log(f"  {identifier}: state fetch failed ({type(e).__name__}) — treating as unknown")
        return "unknown"
    state = (data or {}).get("attributes", {}).get("current_state", "")
    return state.strip().lower() if isinstance(state, str) and state.strip() else "unknown"


def power(identifier: str, signal: str) -> bool:
    try:
        _request("POST", f"/api/client/servers/{identifier}/power", PEL_CLIENT_KEY,
                 {"signal": signal})
        return True
    except Exception as e:
        log(f"  {identifier}: power '{signal}' failed ({type(e).__name__}: {e})")
        return False


def send_console(identifier: str, command: str) -> None:
    """Best-effort console command (player notice). Games without a matching
    console command just error; that must never block the reboot."""
    try:
        _request("POST", f"/api/client/servers/{identifier}/command", PEL_CLIENT_KEY,
                 {"command": command})
    except Exception:
        pass


# --------------------
# PuK maintenance block
# --------------------
def suppress_puk_notifications() -> None:
    """Tell PuK to hold Discord notifications for SUPPRESS_MINUTES so the
    planned outage doesn't ping anyone. Best-effort: a PuK that's down or
    unconfigured must never block the reboot."""
    if not PUK_SUPPRESS_URL:
        log("PUK_SUPPRESS_URL not set — skipping maintenance block")
        return
    data = f"minutes={SUPPRESS_MINUTES}".encode()
    req = urllib.request.Request(PUK_SUPPRESS_URL, data=data, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded",
    })
    if PUK_ADMIN_PASS:
        cred = base64.b64encode(f"{PUK_ADMIN_USER}:{PUK_ADMIN_PASS}".encode()).decode()
        req.add_header("Authorization", f"Basic {cred}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        log(f"PuK maintenance block set for {SUPPRESS_MINUTES} min")
    except Exception as e:
        log(f"WARNING: could not set PuK maintenance block ({type(e).__name__}: {e})")


# --------------------
# Player notices
# --------------------
def warn_players(running: list) -> None:
    """Countdown notices to every running server's console, e.g. at 90/30/10s.
    NOTICE_SCHEDULE="" disables. The final mark's delay is waited out too, so
    the stop lands right when the countdown says it will."""
    try:
        marks = sorted({int(x) for x in NOTICE_SCHEDULE.split(",") if x.strip()}, reverse=True)
    except ValueError:
        log(f"bad NOTICE_SCHEDULE {NOTICE_SCHEDULE!r} — skipping notices")
        return
    if not marks or not running:
        return
    log(f"warning players at {marks}s before stop")
    for i, mark in enumerate(marks):
        msg = NOTICE_MESSAGE.format(seconds=mark)
        cmd = NOTICE_COMMAND.format(msg=msg)
        for s in running:
            send_console(s["identifier"], cmd)
        next_mark = marks[i + 1] if i + 1 < len(marks) else 0
        time.sleep(mark - next_mark)


# --------------------
# Modes
# --------------------
def do_shutdown(dry_run: bool, now: bool = False) -> None:
    servers = list_servers()
    log(f"panel reports {len(servers)} server(s)")

    running = []
    for s in servers:
        state = server_state(s["identifier"])
        log(f"  {s['name']} ({s['identifier']}): {state}")
        if state in RUNNING_STATES:
            running.append(s)

    if dry_run:
        log(f"DRY RUN: would warn + stop {len(running)} server(s), then reboot")
        return

    suppress_puk_notifications()

    # Record the running set BEFORE stopping, so restore starts exactly these
    # (deliberately-stopped servers stay stopped).
    _write_state(STATE_FILE, {"stopped_at": int(time.time()), "servers": running})

    if not now:
        warn_players(running)

    for s in running:
        log(f"stopping {s['name']}")
        power(s["identifier"], "stop")

    # Wait for clean stops; anything still up after STOP_TIMEOUT gets killed.
    pending = {s["identifier"]: s["name"] for s in running}
    deadline = time.time() + STOP_TIMEOUT
    while pending and time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        for ident in list(pending):
            if server_state(ident) == "offline":
                log(f"  {pending.pop(ident)} stopped cleanly")

    if pending:
        for ident, name in pending.items():
            log(f"  {name} did not stop within {STOP_TIMEOUT}s — sending kill")
            power(ident, "kill")
        time.sleep(KILL_GRACE)

    if REBOOT_CMD:
        log(f"rebooting host: {REBOOT_CMD}")
        subprocess.run(REBOOT_CMD.split(), check=False)
    else:
        log("REBOOT_CMD empty — skipping reboot")


def do_restore(dry_run: bool) -> None:
    if not os.path.exists(STATE_FILE):
        log("no state file — nothing to restore")
        return
    state = _read_state(STATE_FILE, {})
    servers = state.get("servers", [])
    if not servers:
        os.remove(STATE_FILE)
        return

    # Wait until the panel answers before issuing starts.
    deadline = time.time() + PANEL_WAIT_TIMEOUT
    while True:
        try:
            _request("GET", "/api/application/servers?per_page=1", PEL_APP_KEY)
            break
        except Exception:
            if time.time() >= deadline:
                log(f"panel not reachable after {PANEL_WAIT_TIMEOUT}s — keeping state file for retry")
                sys.exit(1)
            time.sleep(POLL_INTERVAL)

    log(f"starting {len(servers)} server(s), {START_STAGGER}s apart")
    for i, s in enumerate(servers):
        if dry_run:
            log(f"DRY RUN: would start {s['name']}")
            continue
        if i:
            time.sleep(START_STAGGER)
        log(f"starting {s['name']}")
        power(s["identifier"], "start")

    if not dry_run:
        os.remove(STATE_FILE)


def mem_available_mb() -> int:
    with open("/proc/meminfo") as fh:
        for line in fh:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    raise RuntimeError("MemAvailable not found in /proc/meminfo")


def do_ramwatch() -> None:
    avail = mem_available_mb()
    st = _read_state(RAMWATCH_FILE, {})
    breaches = int(st.get("breaches", 0))
    last_reboot = float(st.get("last_reboot", 0))

    if avail >= RAM_MIN_AVAILABLE_MB:
        if breaches:
            _write_state(RAMWATCH_FILE, {"breaches": 0, "last_reboot": last_reboot})
        log(f"ok: {avail}MB available (threshold {RAM_MIN_AVAILABLE_MB}MB)")
        return

    breaches += 1
    log(f"LOW MEMORY: {avail}MB available (threshold {RAM_MIN_AVAILABLE_MB}MB), "
        f"breach {breaches}/{RAM_BREACH_COUNT}")

    if breaches < RAM_BREACH_COUNT:
        _write_state(RAMWATCH_FILE, {"breaches": breaches, "last_reboot": last_reboot})
        return

    cooldown = REBOOT_COOLDOWN_HOURS * 3600
    if time.time() - last_reboot < cooldown:
        hrs = (cooldown - (time.time() - last_reboot)) / 3600
        log(f"in cooldown ({hrs:.1f}h left) — NOT rebooting. If this repeats, "
            "the host genuinely needs more RAM.")
        # Hold the counter at the threshold so recovery still resets it.
        _write_state(RAMWATCH_FILE, {"breaches": RAM_BREACH_COUNT, "last_reboot": last_reboot})
        return

    log("sustained low memory — triggering clean reboot")
    _write_state(RAMWATCH_FILE, {"breaches": 0, "last_reboot": time.time()})
    do_shutdown(dry_run=False)


def main() -> None:
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    now = "--now" in args
    cmd = next((a for a in args if not a.startswith("--")), "")

    for name, val in (("PEL_URL", PEL_URL), ("PEL_APP_KEY", PEL_APP_KEY),
                      ("PEL_CLIENT_KEY", PEL_CLIENT_KEY)):
        if not val:
            sys.exit(f"Missing required environment variable: {name}")

    if cmd == "shutdown":
        do_shutdown(dry_run, now)
    elif cmd == "restore":
        do_restore(dry_run)
    elif cmd == "ramwatch":
        do_ramwatch()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
