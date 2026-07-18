# wanwatch

Prove your ISP is dropping the line.

`wanwatch` is a tiny two-part toolkit born from a real fault-hunt: an
FTTH connection that silently dropped its WAN session dozens of times per
evening while every LED in the house glowed a healthy green. The ISP's
first-line script says "reboot the router". This produces the timestamped
evidence that ends that conversation.

* **`wan_monitor.sh`** - a dependency-free bash sampler. Every 10 seconds it
  pings your router (LAN) and two public anycast hosts (WAN) and appends a
  CSV row on every state change, every sample during an outage, and a
  one-minute heartbeat. The key signal is `UP,DOWN`: *your equipment is
  fine and the internet is gone* - the ISP's problem, timestamped to
  within 10 seconds.
* **`wanwatch`** - a Python CLI that turns the log into statistics, a
  losslessly condensed interval file you can attach to a complaint email,
  and an availability timeline PNG.

Runs on Linux, macOS, and Windows (via Git Bash). Windows gets full
lifecycle management including a self-healing Task Scheduler watchdog.

## Install

Requires [uv](https://docs.astral.sh/uv/) and, on Windows, Git for
Windows (for Git Bash).

```bash
git clone https://github.com/tommcd/wanwatch
cd wanwatch
uv sync                 # creates .venv and installs the CLI into it
uv run wanwatch --help
```

Or install the CLI on your PATH as a tool:

```bash
uv tool install .
wanwatch --help
```

## Start monitoring

```bash
./scripts/wanctl start      # background monitor, logs to ~/wan_log.csv
./scripts/wanctl status     # PIDs + log freshness ("last sample: 12s ago")
./scripts/wanctl tail       # watch the log live
./scripts/wanctl stop
./scripts/wanctl restart
```

Configuration is via environment variables (or edit the defaults at the
top of the scripts): `WANWATCH_ROUTER` (default `192.168.1.254`) and
`WANWATCH_LOG` (default `~/wan_log.csv`).

### Windows: survive reboots, sleep and crashes

`wanctl start` alone dies with your terminal session. For a long
evidence-gathering campaign, install the Task Scheduler watchdog
(from an **elevated** Git Bash):

```bash
./scripts/wanctl task-install
./scripts/wanctl task-status
./scripts/wanctl task-uninstall   # also needed before 'stop' sticks
```

This registers a task that starts the monitor at boot **and** re-tries
every 5 minutes forever ("do not start a new instance" makes the retry a
no-op while it's already running). Whatever kills the monitor - reboot,
crash, an accidental Ctrl+C - it is back within 5 minutes, and the gap is
visible in the log as a missing heartbeat. It runs S4U: no console
window, survives logoff, no stored password.

Two things the watchdog cannot fix - do them once:

* **Disable sleep on AC** (Settings -> Power), including the lid-close
  action. A sleeping laptop samples nothing, and evening outages happen
  exactly when laptops get closed.
* Consider pausing Windows Update while gathering evidence, or set
  active hours, so it doesn't reboot you mid-incident.

On Linux, run it under systemd or cron instead, e.g.
`@reboot /path/to/wan_monitor.sh` plus
`*/5 * * * * pgrep -f wan_monitor.sh || /path/to/wan_monitor.sh &`.

## Log format

```
timestamp,lan,wan,event
2026-07-10 21:28:49,UP,DOWN,state_change   <- outage begins
2026-07-10 21:29:03,UP,DOWN,outage_sample  <- still down (10s resolution)
2026-07-10 21:29:41,UP,UP,state_change     <- recovered: 52s outage
2026-07-10 21:30:41,UP,UP,heartbeat        <- proof-of-life, 1/min
2026-07-10 21:35:00,UP,-,monitor_started   <- monitor (re)launched
```

| lan | wan | meaning |
|-----|-----|---------|
| UP  | UP    | healthy |
| UP  | DOWN  | **WAN outage - your kit is fine, the line is not** |
| DOWN| UP    | router ignored a ping (usually an artefact: routers deprioritise ICMP to themselves under load) |
| DOWN| DOWN  | monitor host lost the LAN (machine asleep, Wi-Fi blip) |

A hole between heartbeats means the monitor wasn't sampling - honest
gaps, not silent ones.

## Analyze

```bash
uv run wanwatch analyze ~/wan_log.csv
```

Prints coverage span, an explicit list of monitoring gaps, outage count
with min/median/max durations, a per-day table, an hour-of-day histogram
(evening clustering shows up instantly), and the full timestamped outage
list. Deliberate downtime (e.g. you power-cycled things yourself) is
excluded honestly, listed but not counted:

```bash
uv run wanwatch analyze ~/wan_log.csv \
    --exclude "2026-07-17 21:00:00..2026-07-17 22:30:00"
```

## Condense (for attaching to an email)

```bash
uv run wanwatch condense ~/wan_log.csv -o wan_condensed.csv
```

Collapses runs of identical state into intervals
(`start,end,duration_s,lan,wan,kind`). A week of ~12,000 heartbeats
becomes a few dozen rows with zero information loss - every state change
and gap preserved to the second.

## Compact in place (while the monitor is running)

```bash
uv run wanwatch compact ~/wan_log.csv --condensed wan_condensed.csv
```

Safely shrinks the *live* log: the raw file is atomically renamed aside,
condensed into the archive, and deleted; the monitor's next append
recreates a fresh raw file. This is race-free **because the monitor
re-opens the log for every append** - an in-flight write lands either in
the rotated file (captured) or the new one (captured). Intervals that
span compactions are stitched back together. Afterwards, pass both files
to the other commands:

```bash
uv run wanwatch analyze wan_condensed.csv ~/wan_log.csv
```

(All commands accept raw and condensed files interchangeably and
auto-detect the format.)

## Plot

```bash
uv run wanwatch plot wan_condensed.csv ~/wan_log.csv -o wan_timeline.png
```

Renders an availability timeline: green up, red WAN outages, grey
unmonitored. Sub-minute outages are widened to a 2-minute minimum so they
remain visible at week scale (`--min-width` to change); the chart says so
in its title, and true durations always come from `analyze`.

## FAQ

**Why grep for `TTL=` instead of trusting ping's exit code?**
Windows `ping` exits 0 - "success" - when it receives *any* reply,
including "Destination host unreachable" generated by your own machine
when the router is off. Early versions of this tool logged `UP` for a
dead network because of exactly this. A genuine echo reply always
contains `TTL=`; unreachable messages never do.

**Why two WAN hosts?**
`1.1.1.1` and `8.8.8.8` are independent anycast networks. Requiring
either (not both) to reply means a single provider's hiccup can't
masquerade as your line being down.

**Why does my log show `DOWN,UP`?**
The router ignored one ping while the internet still answered - typically
ICMP deprioritisation on the router's CPU, not a real LAN failure. It's
logged faithfully and `analyze` reports it separately from WAN outages.

**How big does the log get?**
About 150 KB/week at one-minute heartbeats. `compact` exists more for
tidiness than necessity.

## License

MIT
