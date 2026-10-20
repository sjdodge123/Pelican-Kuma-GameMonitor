"""Pure Discord-notification decisions (no I/O, no Kuma/Pelican imports).

Kept separate from update_kuma.py so it can be unit-tested without the Kuma
client or live env vars. Two pieces:

- ``kuma_block_reasons()`` turns one Kuma monitor dict into the reasons its
  Discord posts are muted this run (paused / maintenance window).
- ``decide_notification()`` is the per-server debounce: given the persisted
  entry and this run's observed status it returns the next entry and whether a
  Discord post should fire.
"""
from typing import Dict, Iterable, List, NamedTuple, Optional, Set

# Block reasons. "paused" and "maintenance" come from Kuma, "suppress" from the
# admin /api/suppress maintenance block.
PAUSED = "monitor paused"
MAINTENANCE = "kuma maintenance"
SCHEDULED_MAINTENANCE = "scheduled maintenance"  # only tool-synced windows
SUPPRESS = "maintenance block"

# Kuma's maintenance status string for a window that is in effect right now.
UNDER_MAINTENANCE = "under-maintenance"


def _falsy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("0", "false", "no", "")
    return not v


def monitor_is_paused(mon: Optional[dict]) -> bool:
    """True when a Kuma monitor is paused. uptime-kuma-api returns ``active`` as
    a bool (int on some versions); an absent key is treated as not paused."""
    if not isinstance(mon, dict) or "active" not in mon:
        return False
    return _falsy(mon.get("active"))


def kuma_block_reasons(mon: Optional[dict],
                       active_maint_ids: Optional[Iterable[int]] = None,
                       owned_maint_ids: Optional[Set[int]] = None) -> List[str]:
    """Reasons a monitor's Discord posts are muted this run.

    ``mon`` is the monitor dict from ``api.get_monitors()``. Kuma includes a
    ``maintenance`` bool there; ``active_maint_ids`` (ids of maintenances that
    are under-maintenance and attached to this monitor) is the fallback when the
    flag is missing, and also says *which* windows apply. A window made by this
    tool's schedule sync (``owned_maint_ids``) is reported as
    SCHEDULED_MAINTENANCE so the caller can treat it differently from a manual
    window."""
    reasons: List[str] = []
    if not isinstance(mon, dict):
        return reasons
    if monitor_is_paused(mon):
        reasons.append(PAUSED)

    ids = {int(i) for i in (active_maint_ids or [])}
    flag = mon.get("maintenance")
    in_maint = bool(ids) if flag is None else not _falsy(flag)
    if in_maint:
        owned = owned_maint_ids or set()
        if ids and ids <= owned:
            reasons.append(SCHEDULED_MAINTENANCE)
        else:
            reasons.append(MAINTENANCE)
    return reasons


# Reasons whose quiet carries past the end of the window until the server has
# settled. A scheduled (tool-synced) window is excluded: its end *is* the
# scheduled start, which should still be announced.
QUIET_TAIL_REASONS = {PAUSED, MAINTENANCE, SUPPRESS}


class Decision(NamedTuple):
    entry: Dict[str, object]  # status / pending / pending_count / quiet
    changed: bool             # a new status was confirmed this run
    notify: bool              # ...and it should be posted to Discord


def decide_notification(prev: Optional[dict], status: str, confirm_runs: int,
                        reasons: Iterable[str] = ()) -> Decision:
    """Advance one server's debounce state by one run.

    ``prev["status"]`` is the last *confirmed* status (the notification
    baseline); ``pending``/``pending_count`` track a change that has not yet held
    for ``confirm_runs`` consecutive runs. A confirmed change notifies unless:

    - any block reason applies this run (the baseline still advances, so nothing
      fires retroactively), or
    - the entry is ``quiet``: a paused / manual-maintenance / suppressed window
      was seen while this change was in flight. Quiet clears on the first
      unblocked run where the server is settled (no pending change), so a
      bounce (pause → restart → resume) stays silent even when the "back up"
      confirmation lands a run or two after the monitor is resumed.
    """
    prev = prev if isinstance(prev, dict) else {}
    reasons = list(reasons)
    blocked = bool(reasons)
    holds_quiet = any(r in QUIET_TAIL_REASONS for r in reasons)

    confirmed = prev.get("status")
    pending = prev.get("pending")
    try:
        pending_count = int(prev.get("pending_count") or 0)
    except (TypeError, ValueError):
        pending_count = 0
    was_quiet = bool(prev.get("quiet"))
    need = max(1, int(confirm_runs or 1))

    changed = False
    if confirmed is None:
        # First sighting: set the baseline, never notify.
        new_confirmed, new_pending, new_count = status, None, 0
    elif status == confirmed:
        # Back to (or still at) the baseline: cancel any flap.
        new_confirmed, new_pending, new_count = confirmed, None, 0
    else:
        new_count = pending_count + 1 if status == pending else 1
        if new_count >= need:
            new_confirmed, new_pending, new_count = status, None, 0
            changed = True
        else:
            new_confirmed, new_pending = confirmed, status

    settled = new_pending is None
    new_quiet = holds_quiet or (was_quiet and not settled)
    notify = changed and not blocked and not was_quiet

    entry = {
        "status": new_confirmed,
        "pending": new_pending,
        "pending_count": new_count,
        "quiet": new_quiet,
    }
    return Decision(entry, changed, notify)
