# Unattended pipeline runs

Installs a user timer that runs `pipeline.py` on an **intermittently
available** machine — one that may be off for days, or on all day.

## Why not a plain twice-daily schedule

A pure `OnCalendar` schedule fits neither half of that lifecycle: it misses
every window while the machine is off, and it does nothing extra on a day the
machine is up for twelve hours. Instead the timer fires from three angles —
at boot, on an uptime interval, and on a wall-clock calendar — and
`pipeline.py --max-age=5h` collapses the redundancy: whichever trigger fires
first does the work, the rest find a recent success marker and exit 0.

The practical effect: **the data refreshes shortly after you open the
machine**, which is when it is staleest and when you are most likely to look
at the viewer, and it stays fresh on a long day without running five times.

| Situation | What happens |
|---|---|
| Off two days, then booted | `OnBootSec` fires ~5 min in; marker is stale; full run |
| Suspended overnight, lid opened | `OnCalendar` + `Persistent=true` fires on resume; full run |
| Left on all day | `OnUnitActiveSec` every 6h; runs about twice |
| Booted twice in an hour | Second boot's trigger finds a fresh marker; exits 0 |
| A photo batch is still running | `flock` sees the lock; exits 0, not a failure |

## Install

```bash
# user timers must survive logout / run without an active session
loginctl enable-linger "$USER"

mkdir -p ~/.config/systemd/user
ln -sf /home/bengi/code/home-search/ops/systemd/home-search-pipeline.service ~/.config/systemd/user/
ln -sf /home/bengi/code/home-search/ops/systemd/home-search-pipeline.timer   ~/.config/systemd/user/

systemctl --user daemon-reload
systemctl --user enable --now home-search-pipeline.timer
```

## Operate

```bash
systemctl --user list-timers home-search-pipeline    # when does it next fire
systemctl --user start home-search-pipeline          # run now
journalctl --user -u home-search-pipeline -f         # follow
ls -t data/logs/ | head                              # per-run stage logs
cat data/.pipeline-last-success.json                 # last full refresh
```

Run it by hand any time — `python pipeline.py` has no `--max-age` by default,
so a manual run always does the work.

## Turn it off

```bash
systemctl --user disable --now home-search-pipeline.timer
```

For the full picture — retiring the local timer once a cloud pipeline takes
over, returning to local if it does not, or removing the automation
altogether — see [DECOMMISSION.md](../DECOMMISSION.md).
