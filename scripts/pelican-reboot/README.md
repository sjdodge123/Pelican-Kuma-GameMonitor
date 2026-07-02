# RAM-triggered clean host reboot (dynamic)

Watches the Pelican host's available RAM and, when it stays low, performs a
clean reboot cycle: warns connected players in-game, sets a PuK maintenance
block (no Discord spam), stops **all** game servers cleanly, reboots the host,
then starts back up exactly the servers that were running beforehand. The
server list is fetched from the panel at run time, so newly added servers are
covered automatically — nothing per-server to configure.

How it works:

1. `pelican-ramwatch.timer` fires every 5 minutes and runs
   `pelican_reboot.py ramwatch`: reads `MemAvailable` from `/proc/meminfo`.
   After `RAM_BREACH_COUNT` consecutive readings below `RAM_MIN_AVAILABLE_MB`
   (default: <1 GiB for ~15 min), and if the last triggered reboot was more
   than `REBOOT_COOLDOWN_HOURS` ago, it starts the shutdown flow.
2. Shutdown: POSTs to PuK's `/api/suppress` (maintenance block, so the planned
   outage sends no Discord pings), records which servers are
   `running`/`starting`, sends a countdown to each running server's console
   (`NOTICE_SCHEDULE`, default 90/30/10s), then a clean **stop** to each, polls
   until all are `offline` (kills stragglers after `STOP_TIMEOUT`), reboots.
3. After boot, `pelican-restore.service` runs `pelican_reboot.py restore`
   (only if the state file exists): waits for the panel, then starts the
   recorded servers `START_STAGGER` seconds apart. Servers that were
   deliberately stopped before the reboot stay stopped.

You can also trigger the same flow manually or on a fixed schedule:
`pelican_reboot.py shutdown` (see `pelican-reboot.service` / add your own
timer with an `OnCalendar` of your choice).

## Install (on the Pelican/Wings host)

```bash
cp pelican_reboot.py /usr/local/bin/pelican_reboot.py
chmod +x /usr/local/bin/pelican_reboot.py

cp pelican-reboot.env.example /etc/pelican-reboot.env
chmod 600 /etc/pelican-reboot.env
# edit /etc/pelican-reboot.env: PEL_URL, the two API keys (client key must be
# an admin's), and the PUK_* block if you want Discord suppression.

cp pelican-ramwatch.service pelican-ramwatch.timer \
   pelican-reboot.service pelican-restore.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now pelican-ramwatch.timer
systemctl enable pelican-restore.service
```

## Test without rebooting

```bash
set -a; . /etc/pelican-reboot.env; set +a
pelican_reboot.py ramwatch                    # one RAM check, no side effects unless low
pelican_reboot.py shutdown --dry-run          # shows states + what would be stopped
REBOOT_CMD= pelican_reboot.py shutdown --now  # real stop cycle, no countdown, no reboot
pelican_reboot.py restore                     # starts them back up
```

Logs land in the journal: `journalctl -u pelican-ramwatch -u pelican-restore`.

## Notes

- Requires only Python 3 stdlib on the host — no pip packages.
- The PuK maintenance block needs PuK ≥ the version with `/api/suppress`
  (app/admin.py) and the admin credentials in the env file. Without it the
  reboot still works; Discord just gets the usual DOWN/UP pings.
- `NOTICE_COMMAND` defaults to `say {msg}`, which covers Minecraft and
  Source-engine games. Games with a different console syntax silently ignore
  the notice — it never blocks the reboot.
- If ramwatch keeps hitting the cooldown ("in cooldown — NOT rebooting" in the
  journal), the workload has outgrown the host: add RAM or shrink allocations.
  The reboot only resets the pressure clock; it is not the fix.
