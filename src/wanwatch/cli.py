#!/usr/bin/env python3
"""wanwatch - analyze, condense, compact and plot wan_monitor logs.


The raw log (written by wan_monitor.sh) has lines:
    timestamp,lan,wan,event        e.g.  2026-07-14 20:11:32,UP,UP,heartbeat

The condensed format produced here has lines:
    start,end,duration_s,lan,wan,kind      kind = state | gap

Commands (run with uv):
    wanwatch analyze  wan_log.csv [more files...] [--exclude "A..B"]
    wanwatch condense wan_log.csv -o wan_condensed.csv
    wanwatch compact  wan_log.csv --condensed wan_condensed.csv
    wanwatch plot     wan_condensed.csv wan_log.csv -o wan_timeline.png

'compact' safely condenses the live file in place while the monitor is
running: it renames the raw file aside (atomic; the monitor's next append
recreates it, since each append re-opens the file), condenses the rotated
chunk, merges it into the condensed archive, and deletes the chunk.
Adjacent same-state intervals across compactions are stitched together,
so nothing is lost. All commands accept raw and condensed files
interchangeably and auto-detect the format.
"""

import argparse
import os
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime

TS_FMT = "%Y-%m-%d %H:%M:%S"
DEFAULT_GAP_S = 180  # samples further apart than this = monitoring gap


@dataclass
class Interval:
    start: datetime
    end: datetime
    lan: str
    wan: str
    kind: str  # "state" or "gap"

    @property
    def dur(self) -> float:
        return (self.end - self.start).total_seconds()

    @property
    def state(self):
        return (self.lan, self.wan, self.kind)


def _pt(s: str) -> datetime:
    return datetime.strptime(s.strip(), TS_FMT)


def fmt_dur(seconds: float) -> str:
    seconds = int(round(seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    out = []
    if d:
        out.append(f"{d}d")
    if h:
        out.append(f"{h}h")
    if m:
        out.append(f"{m}m")
    if s or not out:
        out.append(f"{s}s")
    return " ".join(out)


# ---------------------------------------------------------------- parsing

def parse_raw_lines(lines) -> list:
    """Raw monitor samples -> [(ts, lan, wan, event)]. Tolerates no header."""
    samples = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("timestamp"):
            continue
        parts = line.split(",")
        if len(parts) < 4:
            continue
        try:
            ts = _pt(parts[0])
        except ValueError:
            continue
        samples.append((ts, parts[1].strip(), parts[2].strip(), parts[3].strip()))
    return samples


def samples_to_intervals(samples, gap_s=DEFAULT_GAP_S) -> list:
    """Collapse consecutive same-state samples into intervals; emit gap
    intervals where sampling paused. Samples with wan '-' (monitor_started
    markers) carry no state; their timestamp still bounds gaps."""
    ivs: list[Interval] = []
    cur: Interval | None = None
    prev_ts: datetime | None = None

    for ts, lan, wan, _event in samples:
        if prev_ts is not None and (ts - prev_ts).total_seconds() > gap_s:
            if cur is not None:
                cur.end = prev_ts
                ivs.append(cur)
                cur = None
            ivs.append(Interval(prev_ts, ts, "-", "-", "gap"))
        prev_ts = ts
        if wan == "-":            # startup marker: no state information
            continue
        if cur is not None and (lan, wan) != (cur.lan, cur.wan):
            cur.end = ts
            ivs.append(cur)
            cur = None
        if cur is None:
            cur = Interval(ts, ts, lan, wan, "state")
    if cur is not None and prev_ts is not None:
        cur.end = prev_ts
        ivs.append(cur)
    return [iv for iv in ivs if iv.kind == "gap" or iv.dur >= 0]


def parse_condensed_lines(lines) -> list:
    ivs = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("start,"):
            continue
        parts = line.split(",")
        if len(parts) < 6:
            continue
        try:
            ivs.append(Interval(_pt(parts[0]), _pt(parts[1]),
                                parts[3].strip(), parts[4].strip(),
                                parts[5].strip()))
        except ValueError:
            continue
    return ivs


def load_any(path: str, gap_s=DEFAULT_GAP_S) -> list:
    """Auto-detect raw vs condensed and return intervals."""
    if not os.path.exists(path):
        print(f"warning: {path} not found - skipping", file=sys.stderr)
        return []
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("start,") or line.count(",") >= 5:
            return parse_condensed_lines(lines)
        return samples_to_intervals(parse_raw_lines(lines), gap_s)
    return []


def merge_intervals(ivs: list, join_s=DEFAULT_GAP_S) -> list:
    """Sort and stitch adjacent same-state intervals (across compactions)."""
    ivs = sorted(ivs, key=lambda i: (i.start, i.end))
    seen = set()
    out: list[Interval] = []
    for iv in ivs:
        key = (iv.start, iv.end, iv.state)
        if key in seen:
            continue
        seen.add(key)
        if (out and iv.state == out[-1].state
                and (iv.start - out[-1].end).total_seconds() <= join_s):
            out[-1].end = max(out[-1].end, iv.end)
        else:
            out.append(Interval(iv.start, iv.end, iv.lan, iv.wan, iv.kind))
    return out


def write_condensed(path: str, ivs: list) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write("start,end,duration_s,lan,wan,kind\n")
        for iv in ivs:
            f.write(f"{iv.start.strftime(TS_FMT)},{iv.end.strftime(TS_FMT)},"
                    f"{int(iv.dur)},{iv.lan},{iv.wan},{iv.kind}\n")


# ---------------------------------------------------------------- exclude

def parse_excludes(specs) -> list:
    """--exclude 'YYYY-MM-DD HH:MM:SS..YYYY-MM-DD HH:MM:SS' (repeatable)."""
    out = []
    for spec in specs or []:
        try:
            a, b = spec.split("..")
            out.append((_pt(a), _pt(b)))
        except ValueError:
            sys.exit(f"bad --exclude window: {spec!r} "
                     f"(expected 'A..B' with '{TS_FMT}' timestamps)")
    return out


def overlaps_exclude(iv: Interval, excludes) -> bool:
    return any(iv.start < b and iv.end > a for a, b in excludes)


# ---------------------------------------------------------------- analyze

def cmd_analyze(args):
    ivs = merge_intervals(
        [iv for p in args.files for iv in load_any(p, args.gap)], args.gap)
    if not ivs:
        sys.exit("no intervals found")
    excludes = parse_excludes(args.exclude)

    first, last = ivs[0].start, ivs[-1].end
    span = (last - first).total_seconds()
    gaps = [iv for iv in ivs if iv.kind == "gap"]
    gap_total = sum(g.dur for g in gaps)

    outs_all = [iv for iv in ivs if iv.kind == "state"
                and iv.lan == "UP" and iv.wan == "DOWN"]
    excluded = [iv for iv in outs_all if overlaps_exclude(iv, excludes)]
    outages = [iv for iv in outs_all if iv not in excluded]
    lan_down = [iv for iv in ivs if iv.kind == "state" and iv.lan == "DOWN"]

    print(f"Coverage : {first:{'%Y-%m-%d %H:%M:%S'}}  ->  "
          f"{last:{'%Y-%m-%d %H:%M:%S'}}   (span {fmt_dur(span)})")
    print(f"Monitored: {fmt_dur(span - gap_total)}  "
          f"({(span - gap_total) / span * 100:.1f}% of span)")
    print(f"Gaps     : {len(gaps)} totalling {fmt_dur(gap_total)}")
    for g in gaps:
        print(f"    GAP  {g.start:%Y-%m-%d %H:%M:%S} -> "
              f"{g.end:%H:%M:%S}  ({fmt_dur(g.dur)})")

    print(f"\nWAN OUTAGES (lan UP, wan DOWN): {len(outages)}")
    if outages:
        durs = [o.dur for o in outages]
        print(f"  total downtime : {fmt_dur(sum(durs))}")
        print(f"  duration       : min {fmt_dur(min(durs))} / "
              f"median {fmt_dur(statistics.median(durs))} / "
              f"max {fmt_dur(max(durs))}")

        per_day: dict = {}
        for o in outages:
            d = per_day.setdefault(o.start.date(), [0, 0.0])
            d[0] += 1
            d[1] += o.dur
        print("\n  per-day:")
        print("    date        drops   downtime")
        for day in sorted(per_day):
            n, t = per_day[day]
            print(f"    {day}  {n:5d}   {fmt_dur(t)}")

        by_hour = [0] * 24
        for o in outages:
            by_hour[o.start.hour] += 1
        print("\n  by hour of day:")
        for h, n in enumerate(by_hour):
            if n:
                print(f"    {h:02d}:00-{h:02d}:59  {'#' * n} {n}")

        print("\n  full list:")
        for o in outages:
            print(f"    {o.start:%Y-%m-%d %H:%M:%S}  "
                  f"down {fmt_dur(o.dur):>8}")

    if excluded:
        print(f"\nEXCLUDED (deliberate downtime windows): {len(excluded)} "
              f"outage intervals, {fmt_dur(sum(o.dur for o in excluded))} "
              f"- not counted above")
    if lan_down:
        print(f"\nLAN-DOWN periods (monitor could not reach the router): "
              f"{len(lan_down)}, total {fmt_dur(sum(i.dur for i in lan_down))}")
        for i in lan_down:
            print(f"    {i.start:%Y-%m-%d %H:%M:%S}  lan={i.lan} wan={i.wan}  "
                  f"({fmt_dur(i.dur)})")


# --------------------------------------------------------------- condense

def cmd_condense(args):
    ivs = merge_intervals(
        [iv for p in args.files for iv in load_any(p, args.gap)], args.gap)
    write_condensed(args.output, ivs)
    print(f"{sum(1 for _ in ivs)} intervals -> {args.output}")


# ---------------------------------------------------------------- compact

def cmd_compact(args):
    raw, cond = args.file, args.condensed
    if not os.path.exists(raw):
        sys.exit(f"{raw}: not found (nothing to compact)")
    work = f"{raw}.compacting.{os.getpid()}.{int(time.time())}"

    for attempt in range(20):
        try:
            os.rename(raw, work)
            break
        except PermissionError:      # writer holds it for a few ms mid-append
            time.sleep(0.25)
    else:
        sys.exit("could not rotate the raw file (writer busy); try again")

    time.sleep(0.5)  # let any append that opened the old handle finish

    with open(work, newline="", encoding="utf-8", errors="replace") as f:
        new_ivs = samples_to_intervals(parse_raw_lines(f.readlines()), args.gap)

    old_ivs = []
    if os.path.exists(cond):
        with open(cond, newline="", encoding="utf-8", errors="replace") as f:
            old_ivs = parse_condensed_lines(f.readlines())

    merged = merge_intervals(old_ivs + new_ivs, args.gap)
    tmp = cond + ".tmp"
    write_condensed(tmp, merged)
    os.replace(tmp, cond)
    os.remove(work)

    print(f"compacted {len(new_ivs)} new intervals into {cond} "
          f"({len(merged)} total). Raw file will be recreated by the "
          f"monitor's next write.")


# ------------------------------------------------------------------- plot

def cmd_plot(args):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch
    from datetime import timedelta, time as dtime

    ivs = merge_intervals(
        [iv for p in args.files for iv in load_any(p, args.gap)], args.gap)
    if not ivs:
        sys.exit("no intervals found")

    C_UP, C_OUT, C_LAN, C_ALL, C_GAP = ("#c8e6c9", "#c62828",
                                        "#ef6c00", "#6a1b9a", "#e0e0e0")

    def color_of(iv):
        if iv.kind == "gap":
            return C_GAP
        if iv.lan == "UP" and iv.wan == "DOWN":
            return C_OUT
        if iv.lan == "DOWN" and iv.wan == "DOWN":
            return C_ALL
        if iv.lan == "DOWN":
            return C_LAN
        return C_UP

    n_out = sum(1 for iv in ivs if iv.kind == "state"
                and iv.lan == "UP" and iv.wan == "DOWN")

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(16, 8),
        gridspec_kw={"height_ratios": [1, 2.6], "hspace": 0.35})

    # ---- panel 1: full-span timeline (context) ----------------------------
    for iv in ivs:
        dur_s = iv.dur
        if iv.kind == "state" and (iv.lan, iv.wan) != ("UP", "UP"):
            dur_s = max(dur_s, args.min_width * 60)
        ax1.broken_barh([(mdates.date2num(iv.start), dur_s / 86400.0)],
                        (0, 1), facecolors=color_of(iv), edgecolors="none")
    ax1.set_ylim(0, 1)
    ax1.set_yticks([])
    ax1.xaxis.set_major_locator(mdates.DayLocator())
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%a %d"))
    ax1.grid(True, axis="x", alpha=0.3)
    ax1.set_title(f"Full span - {n_out} WAN outages")

    # ---- panel 2: one row per day, midnight to midnight -------------------
    def split_by_day(iv):
        cur = iv.start
        while cur.date() < iv.end.date():
            midnight = datetime.combine(cur.date() + timedelta(days=1),
                                        dtime.min)
            yield cur.date(), cur, midnight
            cur = midnight
        if cur < iv.end or iv.start.date() == iv.end.date():
            yield cur.date(), cur, iv.end

    days = sorted({d for iv in ivs for d, _, _ in split_by_day(iv)})
    row = {d: i for i, d in enumerate(days)}
    DAY_MIN_S = max(args.min_width, 6) * 60   # visibility floor on 24h axis

    ax2.axvspan(17, 22, color="#fff8e1", zorder=0)  # evening band
    for iv in ivs:
        col = color_of(iv)
        for d, a, b in split_by_day(iv):
            x = a.hour + a.minute / 60 + a.second / 3600
            w = (b - a).total_seconds() / 3600
            if iv.kind == "state" and (iv.lan, iv.wan) != ("UP", "UP"):
                w = max(w, DAY_MIN_S / 3600)
            y = row[d]
            ax2.broken_barh([(x, w)], (y + 0.08, 0.84),
                            facecolors=col, edgecolors="none", zorder=2)

    ax2.set_ylim(len(days), 0)                       # first day on top
    ax2.set_yticks([i + 0.5 for i in range(len(days))])
    ax2.set_yticklabels([d.strftime("%a %d %b") for d in days])
    ax2.set_xlim(0, 24)
    ax2.set_xticks(range(0, 25, 2))
    ax2.set_xticklabels([f"{h:02d}:00" for h in range(0, 25, 2)])
    ax2.grid(True, axis="x", alpha=0.3, zorder=1)
    ax2.set_title(f"By day and hour - outage blocks widened to "
                  f"{DAY_MIN_S // 60} min minimum for visibility "
                  f"(true durations in 'analyze'); shaded band = 17:00-22:00")

    fig.suptitle(f"WAN availability - {n_out} spontaneous WAN outages "
                 f"(router reachable throughout)", fontsize=13)
    fig.legend(handles=[
        Patch(color=C_UP, label="up"),
        Patch(color=C_OUT, label="WAN down (LAN up)"),
        Patch(color=C_LAN, label="router unreachable"),
        Patch(color=C_ALL, label="all down (incl. deliberate power-offs)"),
        Patch(color=C_GAP, label="not monitored"),
    ], loc="lower center", ncol=5, frameon=False)
    fig.savefig(args.output, dpi=150, bbox_inches="tight")
    print(f"wrote {args.output}")



# ----------------------------------------------------------------- report

_STALE_JS = """
<script>
(function(){
 var gen=__EPOCH__*1000, lim=20*60*1000;
 if(Date.now()-gen>lim){
   var b=document.querySelector(".badge");
   b.textContent="NO FRESH DATA";
   b.style.background="#616161";
   document.querySelector(".detail").textContent=
     "Nothing published since "+new Date(gen).toLocaleString()+
     " - the connection or the monitoring machine is offline."+
     " For this page, silence IS the outage signal"+
     " (the live badge above, if present, updates independently).";
 }
}());
</script>
"""

_REPORT_STYLE = """
<style>
 body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:2rem auto;
      max-width:760px;padding:0 1rem;color:#212121;background:#fafafa}
 .badge{display:inline-block;padding:.45rem 1.1rem;border-radius:8px;
        color:#fff;font-size:1.5rem;font-weight:700}
 .detail{color:#616161;margin:.4rem 0 1.4rem}
 .cards{display:flex;gap:1rem;flex-wrap:wrap;margin-bottom:1.4rem}
 .card{background:#fff;border:1px solid #e0e0e0;border-radius:8px;
       padding:.8rem 1rem;flex:1;min-width:150px}
 .card b{display:block;font-size:1.25rem}
 .card span{color:#757575;font-size:.85rem}
 h3{margin:1.4rem 0 .4rem;font-size:1rem;color:#424242}
 table{border-collapse:collapse;width:100%;background:#fff;
       border:1px solid #e0e0e0;border-radius:8px}
 td,th{padding:.4rem .8rem;border-bottom:1px solid #eee;text-align:left;
       font-size:.9rem}
 .note{color:#9e9e9e;font-size:.8rem;margin-top:1.6rem}
</style>
"""


def _svg_bars(vals, w=340, h=46, color="#c62828"):
    mx = max(vals) if vals and max(vals) > 0 else 1
    bw = w / max(len(vals), 1)
    parts = []
    for i, v in enumerate(vals):
        bh = 0 if v <= 0 else max(2.0, h * v / mx)
        parts.append(f'<rect x="{i * bw + 1:.1f}" y="{h - bh:.1f}" '
                     f'width="{max(bw - 2, 1):.1f}" height="{bh:.1f}" '
                     f'rx="1" fill="{color}"/>')
    return (f'<svg width="{w}" height="{h}" '
            f'style="background:#eeeeee;border-radius:4px">'
            f'{"".join(parts)}</svg>')


def cmd_report(args):
    """Render a self-contained, read-only HTML status page."""
    from datetime import timedelta

    ivs = merge_intervals(
        [iv for p in args.files for iv in load_any(p, args.gap)], args.gap)
    if not ivs:
        sys.exit("no intervals found")
    now = datetime.now()
    first, last_iv = ivs[0], ivs[-1]
    fresh_s = (now - last_iv.end).total_seconds()

    outages = [iv for iv in ivs if iv.kind == "state"
               and iv.lan == "UP" and iv.wan == "DOWN"]
    gaps = [iv for iv in ivs if iv.kind == "gap"]
    last_out = outages[-1] if outages else None
    span = (last_iv.end - first.start).total_seconds()
    coverage = ((span - sum(g.dur for g in gaps)) / span * 100) if span else 0

    ongoing = (last_iv.kind == "state" and last_iv.lan == "UP"
               and last_iv.wan == "DOWN" and fresh_s <= 600)
    if fresh_s > 600:
        colour, label = "#9e9e9e", "MONITOR STALE"
        detail = (f"no sample for {fmt_dur(fresh_s)} "
                  f"(monitoring machine asleep or monitor stopped)")
    elif ongoing:
        colour, label = "#c62828", "WAN DOWN"
        detail = (f"outage in progress since {last_iv.start:%H:%M:%S}, "
                  f"{fmt_dur((now - last_iv.start).total_seconds())} so far")
    elif last_out and (now - last_out.end).total_seconds() < 86400:
        colour, label = "#ef6c00", "UP (outage in last 24 h)"
        detail = f"last sample {int(fresh_s)} s ago"
    else:
        colour, label = "#2e7d32", "UP"
        detail = f"last sample {int(fresh_s)} s ago"

    days = [(now - timedelta(days=i)).date() for i in range(13, -1, -1)]
    day_down = {d: 0.0 for d in days}
    for o in outages:
        if o.start.date() in day_down:
            day_down[o.start.date()] += o.dur
    by_hour = [0] * 24
    for o in outages:
        by_hour[o.start.hour] += 1

    if last_out:
        last_out_txt = (f"{last_out.start:%a %d %b %H:%M:%S} · "
                        f"down {fmt_dur(last_out.dur)} · ended "
                        f"{fmt_dur((now - last_out.end).total_seconds())} ago")
    else:
        last_out_txt = "none recorded"

    rows = "".join(
        f"<tr><td>{o.start:%a %d %b %H:%M:%S}</td>"
        f"<td>{fmt_dur(o.dur)}</td></tr>"
        for o in reversed(outages[-12:])) or         "<tr><td colspan=2>none recorded</td></tr>"

    live_badge = ""
    if getattr(args, "badge", ""):
        live_badge = (
            "<div style='float:right;text-align:right'>"
            f"<img src='{args.badge}' alt='live status' height='22'>"
            "<div style='font-size:.7rem;color:#9e9e9e'>"
            "live - updates even when this page is stale</div></div>")

    html = ("<!doctype html><html><head><meta charset='utf-8'>"
            "<meta http-equiv='refresh' content='300'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>wanwatch status</title>" + _REPORT_STYLE + "</head><body>"
            + live_badge +
            "<h1 style='margin:.2rem 0;font-size:1.5rem'>"
            "Broadband connection monitor</h1>"
            "<p style='color:#616161;margin:.2rem 0 1rem;max-width:52ch'>"
            "Automated uptime monitoring of a home fibre connection "
            "(eir Fibre, <a href='https://www.eir.ie/support/' "
            "style='color:#1565c0'>eir support</a>). A script on the "
            "line pings the router and the public internet every 10 "
            "seconds and records every outage. The badge top-right is "
            "fetched live from healthchecks.io on each visit, so it "
            "shows the connection’s status right now even if the rest "
            "of this page is a few minutes old — refresh to update "
            "everything else.</p>" +
            f"<span class='badge' style='background:{colour}'>{label}</span>"
            f"<div class='detail'>{detail}</div>"
            "<div class='cards'>"
            f"<div class='card'><b>{len(outages)}</b>"
            "<span>WAN outages recorded</span></div>"
            f"<div class='card'><b>{fmt_dur(sum(o.dur for o in outages))}</b>"
            "<span>total downtime</span></div>"
            f"<div class='card'><b>{coverage:.1f}%</b>"
            "<span>monitoring coverage</span></div>"
            "</div>"
            f"<h3>Last outage</h3><div>{last_out_txt}</div>"
            f"<h3>Daily downtime, last 14 days "
            f"({days[0]:%d %b} – {days[-1]:%d %b})</h3>"
            f"{_svg_bars([day_down[d] for d in days])}"
            "<h3>Outages by hour of day (00–23)</h3>"
            f"{_svg_bars(by_hour, color='#ef6c00')}"
            "<h3>Recent outages</h3>"
            f"<table><tr><th>started</th><th>duration</th></tr>{rows}</table>"
            f"<div class='note'>Generated {now:%a %d %b %Y %H:%M:%S} "
            f"(local Irish time). Static read-only page, republished every "
            f"~10 minutes; data window {first.start:%d %b} – "
            f"{last_iv.end:%d %b %H:%M}. Page auto-reloads every 5 min. ""<a href='https://github.com/tommcd/wanwatch' style='color:#9e9e9e'>Source and methodology on GitHub</a>.</div>"
            + _STALE_JS.replace("__EPOCH__", str(int(now.timestamp())))
            + "</body></html>")

    with open(args.output, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"wrote {args.output}")


# ------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gap", type=int, default=DEFAULT_GAP_S,
                    help="seconds between samples that counts as a "
                         "monitoring gap (default 180)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("analyze", help="print outage statistics")
    p.add_argument("files", nargs="+")
    p.add_argument("--exclude", action="append", metavar="'A..B'",
                   help=f"exclude a deliberate-downtime window, "
                        f"timestamps as '{TS_FMT}..{TS_FMT}' (repeatable)")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("condense", help="write a condensed interval file")
    p.add_argument("files", nargs="+")
    p.add_argument("-o", "--output", default="wan_condensed.csv")
    p.set_defaults(func=cmd_condense)

    p = sub.add_parser("compact",
                       help="safely condense the LIVE raw log in place")
    p.add_argument("file", help="the live raw log")
    p.add_argument("--condensed", default="wan_condensed.csv",
                   help="condensed archive to merge into")
    p.set_defaults(func=cmd_compact)

    p = sub.add_parser("plot", help="render an availability timeline PNG")
    p.add_argument("files", nargs="+")
    p.add_argument("-o", "--output", default="wan_timeline.png")
    p.add_argument("--min-width", type=float, default=2.0,
                   help="minimum visual width for outage blocks, minutes "
                        "(default 2)")
    p.set_defaults(func=cmd_plot)

    p = sub.add_parser("report",
                       help="render a static, read-only HTML status page")
    p.add_argument("files", nargs="+")
    p.add_argument("-o", "--output", default="wan_status.html")
    p.add_argument("--badge", default="",
                   help="URL of a live status badge image to embed")
    p.set_defaults(func=cmd_report)


    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
