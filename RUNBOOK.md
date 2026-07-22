# wanwatch runbook

Operational guide to set up, verify, and tear down the full monitoring
system: the sampler, the public status dashboard, and the dead-man's-switch
heartbeat. The [README](README.md) covers the analysis CLI; this covers
running the thing continuously.

The reference deployment runs on **Windows (Git Bash + Task Scheduler)**,
monitoring a home fibre line from a laptop wired to the router. Linux/macOS
notes are given where they differ.

---

## What you end up with

- **`wan_monitor.sh`** sampling the router and the public internet every
  10 s, written to `~/wan_log.csv`, running as a self-healing scheduled
  task.
- A **public read-only status page** at `https://<user>.github.io/<repo>/`,
  rebuilt every 10 minutes, that anyone can view with no login and no path
  back to your machine.
- A **live status badge** (via healthchecks.io) embedded in that page,
  fetched fresh on every visit, so the page tells the truth about *right
  now* even when the rest of it is a few minutes stale — and flips to DOWN
  if your machine or connection goes dark.

Three moving parts: the **monitor** (writes the log), the **publisher**
(renders + pushes the page), the **heartbeat** (outbound ping to a hosted
check). All three are outbound-only. Nothing ever connects *to* your
machine.

---

## Prerequisites

1. **Git for Windows** (provides Git Bash). <https://git-scm.com/download/win>
2. **uv**. <https://docs.astral.sh/uv/> — `curl -LsSf https://astral.sh/uv/install.sh | sh`
3. A **GitHub account** and a fork/clone of this repo you can push to.
4. *(Optional, for the live badge)* a free **healthchecks.io** account.
5. The monitoring machine **wired to the router** (recommended — removes
   Wi-Fi as a variable) and set to **never sleep on AC**, including the
   lid-close action (Settings → Power; verify the lid action too, it is a
   common ambush).

---

## Part 1 — the monitor

```bash
git clone https://github.com/<user>/<repo> ~/git/wanwatch
cd ~/git/wanwatch
uv sync

# put the sampler where the scheduled task will call it
mkdir -p ~/bin
cp scripts/wan_monitor.sh ~/bin/wan_monitor.sh
```

Config is via environment variables, defaults in the script: `WANWATCH_ROUTER`
(default `192.168.1.254`) and `WANWATCH_LOG` (default `~/wan_log.csv`).

### Run it as a self-healing task (Windows)

From an **elevated** Git Bash:

```bash
./scripts/wanctl task-install
```

This registers a task (via `setup_wan_task.ps1`) that starts the monitor at
boot **and** retries every 5 minutes forever — so a reboot, crash, or
accidental kill self-heals within 5 minutes, and the gap shows in the log
as a missing heartbeat. It runs S4U: no console window, survives logoff, no
stored password.

Verify:

```bash
./scripts/wanctl status      # RUNNING + "last sample: Ns ago"
tail -6 ~/wan_log.csv        # a "# wanwatch-monitor ..." line, then heartbeats
```

You want heartbeats on a **single** ~61 s cadence. Two interleaved cadences
= two monitors running; stop and restart to fix (see Teardown → restart).

### Linux/macOS alternative

No Task Scheduler; use systemd or cron:

```
@reboot /home/you/git/wanwatch/scripts/wan_monitor.sh
*/5 * * * * pgrep -f wan_monitor.sh || /home/you/git/wanwatch/scripts/wan_monitor.sh &
```

---

## Part 2 — the public dashboard (GitHub Pages)

The publisher renders the status page and **force-pushes it to a `dashboard`
branch** as a single commit each run. GitHub Pages serves that branch. Your
machine only ever pushes; viewers talk to GitHub.

```bash
cp scripts/publish_dashboard scripts/publish_dashboard   # already present after clone
chmod +x scripts/publish_dashboard

# first publish — creates the dashboard branch
./scripts/publish_dashboard          # prints: published HH:MM:SS -> ...
```

Then **enable Pages** (one-time, in the browser):

1. Repo → **Settings** → **Pages**
2. Source: **Deploy from a branch**
3. Branch: **`dashboard`**, folder: **`/ (root)`** → **Save**

A minute later the page is live at `https://<user>.github.io/<repo>/`.
Pages rebuilds automatically on every push, so once the publisher task
(below) is running, the page maintains itself. (Check builds under the
repo's **Actions** tab; a hard refresh, Ctrl+F5, beats the browser cache.)

### Automate publishing every 10 minutes

From **ordinary** PowerShell (**not** elevated — the `git push` needs the
credentials in your interactive session):

```powershell
$a = New-ScheduledTaskAction -Execute 'C:\Program Files\Git\bin\bash.exe' -Argument '-l -c "/c/Users/<you>/git/wanwatch/scripts/publish_dashboard >> /c/Users/<you>/dashboard.log 2>&1"'
$t = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(-5) -RepetitionInterval (New-TimeSpan -Minutes 10)
$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName 'WAN Dashboard' -Action $a -Trigger $t -Settings $s
Start-ScheduledTask -TaskName 'WAN Dashboard'
```

Verify:

```powershell
Get-ScheduledTask -TaskName 'WAN Dashboard' | Get-ScheduledTaskInfo | Select-Object LastRunTime, LastTaskResult, NextRunTime
```

`LastTaskResult 0` and a populated `NextRunTime` ~10 min out. Then from Git
Bash, `tail ~/dashboard.log` should show a `published HH:MM:SS` line.

> **Note on GitHub Pages limits:** Pages allows roughly 10 builds/hour, so
> the 10-minute cadence is deliberately at the ceiling. Don't publish more
> often.

---

## Part 3 — the live badge / dead-man's switch (optional)

Push-based publishing goes silent exactly when the network fails — so the
absence of updates *is* the signal. A hosted check makes that visible: the
monitor pings it every minute while the WAN is up; if the pings stop, the
check's badge flips to DOWN on its own, and the badge (an `<img>` the
viewer's browser fetches live from healthchecks.io) shows that even though
your page is frozen.

1. On **healthchecks.io**: create a check. Set **Period 1 min**, **Grace
   5 min** (or Period 5 / Grace 15 to reduce heartbeat frequency). Mark the
   check **public** so its badge renders for viewers.
2. Copy the **ping URL** (check page) and the **badge SVG URL** (Badges tab).
3. Create `~/.wanwatch.env` (kept out of the repo by `.gitignore`):

```bash
cat > ~/.wanwatch.env << 'EOF'
WANWATCH_PING_URL="https://hc-ping.com/YOUR-UUID"
WANWATCH_BADGE_URL="https://healthchecks.io/badge/YOUR-BADGE-PATH.svg"
EOF
cat ~/.wanwatch.env       # confirm
```

**Ordering matters:** the monitor reads this file **once at startup**, so
create it *before* restarting the monitor. The publisher re-reads it each
run, so the badge appears on the next publish with no restart.

Restart the monitor to pick up the ping URL (see Teardown → restart), then:

- Within ~2 min, the healthchecks dashboard shows pings arriving.
- **Test the absence path** (the whole point): unplug the WAN for ~6 min.
  The check flips DOWN and the badge on your live page goes red *while the
  page itself stays frozen*. Plug back in; watch it recover. An untested
  dead-man's switch is decoration.

Without `~/.wanwatch.env`, the heartbeat disables itself silently and the
page renders with no badge — everything else works.

---

## Verification checklist

- [ ] `./scripts/wanctl status` → RUNNING, fresh sample
- [ ] `tail ~/wan_log.csv` → single ~61 s heartbeat cadence, provenance line present
- [ ] `https://<user>.github.io/<repo>/` loads and shows current data
- [ ] `WAN Dashboard` task: `LastTaskResult 0`, `NextRunTime` populated
- [ ] `tail ~/dashboard.log` → recent `published` line
- [ ] *(if using badge)* healthchecks shows pings; badge renders green; DOWN test passed

---

## Everyday operations

```bash
./scripts/wanctl status                    # running? log fresh?
./scripts/wanctl tail                      # watch the log live
uv run wanwatch analyze wan_condensed.csv ~/wan_log.csv   # stats
uv run wanwatch plot    wan_condensed.csv ~/wan_log.csv -o timeline.png
uv run wanwatch compact ~/wan_log.csv --condensed wan_condensed.csv  # shrink live log
```

**Restart the monitor** (after editing the script, or to clear duplicates).
From **elevated** PowerShell — the single-line sweep also kills orphaned
processes a normal `stop` can miss:

```powershell
Stop-ScheduledTask -TaskName 'WAN Monitor' -ErrorAction SilentlyContinue; Get-CimInstance Win32_Process -Filter "Name='bash.exe'" | Where-Object { $_.CommandLine -like '*wan_monitor*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Start-ScheduledTask -TaskName 'WAN Monitor'
```

Then confirm a single clean cadence in `~/wan_log.csv` before walking away.

---

## Teardown / stop monitoring

### Pause temporarily

```powershell
Disable-ScheduledTask -TaskName 'WAN Monitor'
Disable-ScheduledTask -TaskName 'WAN Dashboard'
```

Re-enable with `Enable-ScheduledTask`. Note the monitor's 5-minute watchdog
means a plain `wanctl stop` alone won't stick while the task is enabled —
disable the task to actually stop it.

### Remove completely

```bash
./scripts/wanctl task-uninstall        # removes the WAN Monitor task
```

```powershell
Unregister-ScheduledTask -TaskName 'WAN Dashboard' -Confirm:$false
```

Then, if you want it fully gone:

- Delete the `dashboard` branch: `git push origin --delete dashboard`
- Turn Pages off: Settings → Pages → Source → None
- Pause/delete the healthchecks.io check so it doesn't alert on the now-absent pings
- Optionally remove `~/.wanwatch.env`, `~/wan_log.csv`, `~/dashboard.log`

### Linux/macOS

Remove the cron lines or `systemctl disable --now` the unit; the dashboard
and healthchecks steps are identical.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Log shows heartbeats but `wanctl status` says NOT RUNNING | S4U task processes are invisible to a non-elevated shell; **trust log freshness**. Query from elevated PowerShell to see the process. |
| Two interleaved heartbeat cadences (~30 s apart) | Two monitors running (an orphan survived a restart). Run the sweep restart above. |
| `FileNotFoundError` on the raw log right after `compact` | Normal: `compact` rotates the file and the monitor recreates it on its next write. The tool skips missing files with a warning. |
| Page loads but is stale / badge red | Working as designed during an outage — the badge is the live truth; the page catches up when the connection returns. |
| Page never updates | Check the `WAN Dashboard` task result and `~/dashboard.log`; confirm `git push` works from that shell (credentials). |
| `LastTaskResult 0x800710E0` on the monitor task | The watchdog fired while an instance was already running and was refused — harmless (that's the "do not start a new instance" rule working). |
| Console window flashes every minute | The per-minute heartbeat `curl`. Cosmetic. Raise the ping interval (script) + healthchecks Period to reduce it. |
| Windows auto-reboot mid-run | The watchdog restarts the monitor within 5 min; consider pausing Windows Update or setting active hours during evidence-gathering. |

---

## How it fits together

```
 monitoring machine (outbound only)          the world
 ┌─────────────────────────────┐
 │ WAN Monitor task            │  ping/HTTPS   ┌───────────────┐
 │  wan_monitor.sh ────────────┼──────────────>│ 1.1.1.1 / ... │
 │   └─ writes ~/wan_log.csv   │               └───────────────┘
 │   └─ heartbeat every 60s ───┼──────────────>┌───────────────┐
 │                             │   (WAN up)    │ healthchecks  │<─┐ badge
 │ WAN Dashboard task          │               └───────────────┘  │ (live,
 │  publish_dashboard ─────────┼─ git push ───>┌───────────────┐  │  on each
 │   └─ wanwatch report        │               │ GitHub (Pages)│──┼─ visit)
 └─────────────────────────────┘               └───────┬───────┘  │
                                                        │ static   │
                                                viewer ─┴─ page ───┘
```

Every arrow points *outward* from your machine. There is no inbound path,
no open port, no tunnel — the dashboard is a static file on GitHub's CDN and
the badge is an image on healthchecks' CDN. Read-only by construction.
