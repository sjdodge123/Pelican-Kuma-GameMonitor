"""Sync Pelican power schedules -> Uptime Kuma maintenance windows.

A server is expected ONLINE between a `power start` and the next `power stop`
in its weekly schedule; the complement (expected-offline) is published to Kuma
as maintenance windows so scheduled downtime shows as "maintenance" (blue, and
excluded from the degraded calc + uptime %) instead of "down".

compute_off_windows() is pure and unit-testable. reconcile_maintenance() is the
idempotent Kuma side: tool-owned maintenances (title-prefixed AUTO_TITLE_PREFIX)
are tracked in /data state and created/updated/deleted to match, with a
title-based orphan sweep so a lost state file can't duplicate them. Operator-made
maintenances are never touched.

Crons are interpreted in, and maintenances created in, a single timezone
(SCHEDULE_TZ, default UTC) — Pelican evaluates schedule crons in the panel TZ
(UTC by default), so the windows line up only if that matches SCHEDULE_TZ.
"""
import json
from typing import Dict, List, Optional, Set

from uptime_kuma_api import MaintenanceStrategy

WEEK_MINUTES = 7 * 24 * 60
AUTO_TITLE_PREFIX = "[auto] "


def _parse_cron_field(value, lo: int, hi: int) -> Optional[Set[int]]:
    """Parse a cron field into the set of matching ints in [lo, hi], supporting
    '*', 'a', 'a,b', 'a-b', '*/n', 'a-b/n', 'a/n'. Returns None if unparseable."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    out: Set[int] = set()
    for part in s.split(","):
        part = part.strip()
        if not part:
            return None
        step = 1
        if "/" in part:
            base, _, st = part.partition("/")
            if not st.isdigit() or int(st) == 0:
                return None
            step = int(st)
            part = base.strip()
        if part == "*":
            start, end = lo, hi
        elif "-" in part.lstrip("-"):  # range a-b (allow only non-negative)
            a, _, b = part.partition("-")
            if not (a.isdigit() and b.isdigit()):
                return None
            start, end = int(a), int(b)
        elif part.isdigit():
            start = end = int(part)
        else:
            return None
        if start < lo or end > hi or start > end:
            return None
        out.update(range(start, end + 1, step))
    return out or None


def _parse_dow(value) -> Optional[Set[int]]:
    """Cron day-of-week into a set of 0..6 (Sunday=0); cron allows 0/7=Sunday."""
    parsed = _parse_cron_field(value, 0, 7)
    if parsed is None:
        return None
    return {0 if d == 7 else d for d in parsed}


def _power_tasks(attr: dict) -> List[tuple]:
    """All power start/stop tasks of a schedule as (payload, time_offset_secs)."""
    tasks = (((attr.get("relationships") or {}).get("tasks") or {}).get("data")) or []
    out = []
    for t in tasks:
        a = t.get("attributes", t) if isinstance(t, dict) else {}
        if a.get("action") == "power" and a.get("payload") in ("start", "stop"):
            try:
                off = int(a.get("time_offset") or 0)
            except (TypeError, ValueError):
                off = 0
            out.append((a["payload"], off))
    return out


def _mow(dow: int, hour: int, minute: int) -> int:
    """Minutes-of-week, week starting Sunday 00:00 (dow 0=Sun..6=Sat)."""
    return (dow % 7) * 1440 + hour * 60 + minute


def _cron_from_mow(mow: int) -> str:
    mow %= WEEK_MINUTES
    dow, rem = divmod(mow, 1440)
    hour, minute = divmod(rem, 60)
    return f"{minute} {hour} * * {dow}"


def compute_off_windows(schedules: List[dict]) -> Optional[List[dict]]:
    """Weekly expected-OFFLINE windows from a server's schedules.

    Returns a list of {"cron","duration"} (minutes), [] if expected always-online,
    or None if the schedules can't be derived safely (date/month-restricted, only
    starts/stops, or genuinely malformed) — caller then leaves the server alone.
    """
    starts, stops = [], []
    for sched in schedules or []:
        attr = sched.get("attributes", sched) if isinstance(sched, dict) else {}
        if not attr.get("is_active", True):
            continue
        tasks = _power_tasks(attr)
        if not tasks:
            continue
        cron = attr.get("cron") or {}
        # Only plain weekly recurrences are derivable; a day-of-month/month
        # restriction isn't, so bail rather than mis-deriving a daily window.
        if str(cron.get("day_of_month", "*")).strip() not in ("*", "") or \
           str(cron.get("month", "*")).strip() not in ("*", ""):
            return None
        minutes = _parse_cron_field(cron.get("minute"), 0, 59)
        hours = _parse_cron_field(cron.get("hour"), 0, 23)
        dows = _parse_dow(cron.get("day_of_week"))
        if minutes is None or hours is None or dows is None:
            return None
        for payload, off in tasks:
            off_min = off // 60
            for d in dows:
                for h in hours:
                    for m in minutes:
                        mow = (_mow(d, h, m) + off_min) % WEEK_MINUTES
                        (starts if payload == "start" else stops).append(mow)

    if not starts and not stops:
        return []          # no power schedule -> nothing to manage
    if not starts or not stops:
        return None         # only starts or only stops -> ambiguous

    # ON interval = each start until the next stop strictly after it (circularly),
    # skipping a stop at the SAME minute (which would give a 0-length interval).
    events = sorted([(m, "start") for m in starts] + [(m, "stop") for m in stops])
    n = len(events)
    on_intervals = []
    for i, (mow, kind) in enumerate(events):
        if kind != "start":
            continue
        nxt = None
        for j in range(1, n + 1):
            cm, ck = events[(i + j) % n]
            if ck == "stop" and (cm - mow) % WEEK_MINUTES > 0:
                nxt = cm
                break
        if nxt is None:
            return None
        on_intervals.append((mow, (nxt - mow) % WEEK_MINUTES))

    # Paint the week ON, then the gaps are the OFF (maintenance) windows.
    covered = [False] * WEEK_MINUTES
    for start, length in on_intervals:
        for k in range(length):
            covered[(start + k) % WEEK_MINUTES] = True
    if all(covered):
        return []
    if not any(covered):
        return None

    windows, run_start = [], None
    for m in range(WEEK_MINUTES):
        if not covered[m] and run_start is None:
            run_start = m
        elif covered[m] and run_start is not None:
            windows.append((run_start, m - run_start))
            run_start = None
    if run_start is not None:
        windows.append((run_start, WEEK_MINUTES - run_start))
    # Join a window ending at week-end with one starting at week-start.
    if len(windows) > 1 and windows[0][0] == 0 and \
            windows[-1][0] + windows[-1][1] == WEEK_MINUTES:
        first = windows.pop(0)
        last = windows.pop()
        windows.append((last[0], last[1] + first[1]))

    return [{"cron": _cron_from_mow(s), "duration": dur} for s, dur in sorted(windows)]


def _sig(windows: List[dict]) -> str:
    return json.dumps(sorted((w["cron"], w["duration"]) for w in windows), sort_keys=True)


def reconcile_maintenance(api, windows_by_server: Dict[str, list],
                          monitor_by_server: Dict[str, int],
                          names_by_server: Dict[str, str],
                          known_idents: set, state: dict, *,
                          brand_name: str, timezone_option: str = "UTC",
                          log=lambda _m: None) -> dict:
    """Create/update/delete tool-owned Kuma maintenances to match the desired
    windows. Returns the new state dict to persist. Steady-state (no change) makes
    zero Kuma calls; on any change it also sweeps title-prefixed orphans so a lost
    state file can't leave duplicates behind."""
    new_state = dict(state)
    changed = False

    def _delete(ids):
        nonlocal changed
        for old in ids or []:
            try:
                api.delete_maintenance(int(old))
                changed = True
            except Exception:
                pass

    # Remove maintenances for servers no longer known to Pelican at all.
    for ident in list(state):
        if ident not in known_idents:
            _delete(state[ident].get("ids"))
            new_state.pop(ident, None)
            log(f"[maint] {ident}: server gone, removed maintenances")

    for ident, desired in windows_by_server.items():
        prev = state.get(ident, {})
        sig = _sig(desired)
        monitor_id = monitor_by_server.get(ident)

        if desired and monitor_id is None:
            log(f"[maint] {ident}: {len(desired)} window(s) but no monitor; skipping")
            continue
        if desired and sig == prev.get("sig") and prev.get("ids"):
            continue  # unchanged
        _delete(prev.get("ids"))  # rebuild from scratch (simple + correct)
        if not desired:
            new_state.pop(ident, None)
            continue

        name = names_by_server.get(ident, ident)
        ids = []
        for i, w in enumerate(desired):
            title = f"{AUTO_TITLE_PREFIX}{brand_name} offline — {name} ({i + 1}/{len(desired)})"
            try:
                r = api.add_maintenance(
                    title=title,
                    description="Auto-synced from the Pelican power schedule (expected offline).",
                    strategy=MaintenanceStrategy.CRON,
                    active=True,
                    cron=w["cron"],
                    durationMinutes=int(w["duration"]),
                    timezoneOption=timezone_option,
                )
                mid = r.get("maintenanceID") if isinstance(r, dict) else r
                ids.append(int(mid))      # track BEFORE attaching, so a failed
                changed = True            # attach can't orphan an untracked window
                api.add_monitor_maintenance(int(mid), [{"id": int(monitor_id)}])
            except Exception as e:
                log(f"[maint] {ident}: create failed {type(e).__name__}: {e}")
        new_state[ident] = {"sig": sig, "ids": ids}
        log(f"[maint] {ident} ({name}): {len(ids)} maintenance window(s) synced")

    # Title-based orphan sweep (only when something changed, to keep no-op cheap):
    # delete any [auto] maintenance not tracked in the new state — recovers from a
    # lost/reset state file instead of duplicating windows.
    if changed:
        keep = {int(x) for v in new_state.values() for x in v.get("ids", [])}
        try:
            for mm in api.get_maintenances():
                mid = mm.get("id")
                if mid is None or int(mid) in keep:
                    continue
                if str(mm.get("title", "")).startswith(AUTO_TITLE_PREFIX):
                    try:
                        api.delete_maintenance(int(mid))
                        log(f"[maint] swept orphan maintenance id={mid}")
                    except Exception:
                        pass
        except Exception:
            pass

    return new_state
