from debounce import (
    MAINTENANCE,
    PAUSED,
    SCHEDULED_MAINTENANCE,
    SUPPRESS,
    decide_notification,
    kuma_block_reasons,
    monitor_is_paused,
)


def run(entry, statuses, confirm_runs=2):
    """Feed (status, reasons) pairs through the decision; return (entry, notifications)."""
    sent = []
    for status, reasons in statuses:
        d = decide_notification(entry, status, confirm_runs, reasons)
        entry = d.entry
        if d.notify:
            sent.append(status)
    return entry, sent


UP = {"status": "up", "pending": None, "pending_count": 0}


# ---- plain debounce (unchanged behaviour) ---------------------------------

def test_first_sighting_sets_baseline_without_notifying():
    d = decide_notification(None, "up", 2)
    assert d.entry["status"] == "up" and not d.changed and not d.notify


def test_garbage_prev_entry_is_treated_as_first_sighting():
    d = decide_notification("not a dict", "down", 2)
    assert d.entry["status"] == "down" and not d.notify


def test_change_notifies_after_confirm_runs():
    entry, sent = run(UP, [("down", []), ("down", [])])
    assert sent == ["down"] and entry["status"] == "down"


def test_single_run_flap_is_discarded():
    entry, sent = run(UP, [("down", []), ("up", []), ("up", [])])
    assert sent == [] and entry == {**UP, "quiet": False}


def test_confirm_runs_one_notifies_immediately():
    _, sent = run(UP, [("down", [])], confirm_runs=1)
    assert sent == ["down"]


# ---- blocks: paused / maintenance / suppress -------------------------------

def test_paused_change_advances_baseline_silently():
    d1 = decide_notification(UP, "down", 2, [PAUSED])
    d2 = decide_notification(d1.entry, "down", 2, [PAUSED])
    assert d2.changed and not d2.notify
    assert d2.entry["status"] == "down" and d2.entry["quiet"] is True


def test_white_glove_bounce_stays_silent_after_resume():
    # The 2026-10-19 incident: pause, restart (down confirmed while paused),
    # server reports running, monitor resumed, "up" confirmed after resume.
    entry, sent = run(UP, [
        ("down", [PAUSED]),
        ("down", [PAUSED]),   # down confirmed silently
        ("up", [PAUSED]),     # starting, still paused
        ("up", []),           # resumed: up confirmed -> must stay quiet
    ])
    assert sent == []
    assert entry["status"] == "up" and entry["quiet"] is False


def test_bounce_with_resume_before_server_seen_up_stays_silent():
    entry, sent = run(UP, [
        ("down", [PAUSED]),
        ("down", [PAUSED]),
        ("up", []),           # first up seen only after resume
        ("up", []),
    ])
    assert sent == [] and entry["quiet"] is False


def test_real_outage_after_quiet_window_settles_still_notifies():
    entry, _ = run(UP, [("down", [PAUSED]), ("down", [PAUSED]), ("up", []), ("up", [])])
    entry, sent = run(entry, [("down", []), ("down", [])])
    assert sent == ["down"]


def test_pause_with_no_change_clears_quiet_on_first_unpaused_run():
    entry, sent = run(UP, [("up", [PAUSED]), ("up", [PAUSED]), ("up", [])])
    assert sent == [] and entry["quiet"] is False
    entry, sent = run(entry, [("down", []), ("down", [])])
    assert sent == ["down"]


def test_server_left_down_after_pause_ends_is_silent_then_up_notifies():
    entry, sent = run(UP, [("down", [PAUSED]), ("down", [PAUSED]), ("down", [])])
    assert sent == [] and entry["status"] == "down" and entry["quiet"] is False
    entry, sent = run(entry, [("up", []), ("up", [])])
    assert sent == ["up"]


def test_manual_maintenance_window_behaves_like_pause():
    _, sent = run(UP, [("down", [MAINTENANCE]), ("down", [MAINTENANCE]), ("up", []), ("up", [])])
    assert sent == []


def test_suppress_block_keeps_working_and_covers_the_tail():
    entry, sent = run(UP, [("down", [SUPPRESS]), ("down", [SUPPRESS])])
    assert sent == [] and entry["status"] == "down"
    entry, sent = run(entry, [("up", []), ("up", [])])
    assert sent == []


def test_scheduled_window_mutes_stop_but_announces_scheduled_start():
    # Tool-synced (Pelican schedule) window: the stop inside the window is
    # muted, the start at the window's end is still announced.
    entry, sent = run(UP, [("down", [SCHEDULED_MAINTENANCE]), ("down", [SCHEDULED_MAINTENANCE])])
    assert sent == [] and entry["status"] == "down" and entry["quiet"] is False
    entry, sent = run(entry, [("up", []), ("up", [])])
    assert sent == ["up"]


# ---- kuma_block_reasons ------------------------------------------------------

def test_active_monitor_has_no_reasons():
    assert kuma_block_reasons({"id": 3, "active": True, "maintenance": False}) == []


def test_paused_monitor():
    assert kuma_block_reasons({"id": 3, "active": False, "maintenance": False}) == [PAUSED]
    assert monitor_is_paused({"active": 0})
    assert monitor_is_paused({"active": "false"})


def test_missing_active_key_or_monitor_is_not_paused():
    assert not monitor_is_paused({"id": 3})
    assert kuma_block_reasons(None) == []


def test_maintenance_flag_without_ids_is_manual_maintenance():
    assert kuma_block_reasons({"active": True, "maintenance": True}) == [MAINTENANCE]


def test_maintenance_only_from_owned_windows_is_scheduled():
    r = kuma_block_reasons({"active": True, "maintenance": True}, {7}, {7, 8})
    assert r == [SCHEDULED_MAINTENANCE]


def test_manual_window_alongside_owned_one_counts_as_manual():
    r = kuma_block_reasons({"active": True, "maintenance": True}, {7, 9}, {7})
    assert r == [MAINTENANCE]


def test_missing_maintenance_flag_falls_back_to_active_window_ids():
    assert kuma_block_reasons({"active": True}, {9}, set()) == [MAINTENANCE]
    assert kuma_block_reasons({"active": True}, set(), set()) == []


def test_paused_and_in_maintenance_reports_both():
    r = kuma_block_reasons({"active": False, "maintenance": True})
    assert r == [PAUSED, MAINTENANCE]
