#!/usr/bin/env python3
"""Build an HTML progress report from the posture history and open it in the browser.

    python report.py
"""

import html
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from history import History, fmt_duration, local_midnight, summarize

REPORT_FILE_NAME = "report.html"


# ---------------------------------------------------------------- data

def gather(history, now=None):
    now = now or datetime.now()
    midnight = local_midnight(now)
    rows = history.rows(midnight - timedelta(days=29))

    def day_of(minute):
        return local_midnight(datetime.fromtimestamp(minute * 60))

    by_day = {}
    for r in rows:
        by_day.setdefault(day_of(r[0]), []).append(r)

    days = []
    for i in range(13, -1, -1):
        d = midnight - timedelta(days=i)
        days.append((d, summarize(by_day.get(d, []))))

    def span(start_days_ago, end_days_ago):
        lo, hi = midnight - timedelta(days=start_days_ago), midnight - timedelta(days=end_days_ago)
        return summarize([r for d, rs in by_day.items() if lo <= d < hi for r in rs])

    today_rows = by_day.get(midnight, [])
    today_hours = {}
    for minute, good, bad, _ in today_rows:
        h = datetime.fromtimestamp(minute * 60).hour
        g, b = today_hours.get(h, (0.0, 0.0))
        today_hours[h] = (g + good, b + bad)

    hour_of_day = {}
    for minute, good, bad, _ in rows:
        h = datetime.fromtimestamp(minute * 60).hour
        g, b = hour_of_day.get(h, (0.0, 0.0))
        hour_of_day[h] = (g + good, b + bad)

    return {
        "now": now,
        "today": summarize(today_rows),
        "days": days,
        "week": span(6, -1),
        "prev_week": span(13, 6),
        "today_hours": today_hours,
        "hour_of_day": hour_of_day,
        "has_data": bool(rows),
    }


# ---------------------------------------------------------------- svg helpers

W, H = 640, 220
PAD_L, PAD_R, PAD_T, PAD_B = 40, 8, 12, 28


def hour_label(h):
    return f"{h % 12 or 12}{'am' if h < 12 else 'pm'}"


def bar_path(x, y, w, h, r=4):
    """Column with a rounded data end (top) and a square baseline."""
    if h <= 0:
        return ""
    r = min(r, h, w / 2)
    return (f"M{x:.1f},{y + h:.1f} L{x:.1f},{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
            f"L{x + w - r:.1f},{y:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} L{x + w:.1f},{y + h:.1f} Z")


def column_chart(labels, series, y_max, y_ticks, y_fmt, tips, label_every=1, cap_labels=None):
    """Stacked column chart. series: [(css_var, [values])] bottom-up. tips: tooltip text per column."""
    plot_w, plot_h = W - PAD_L - PAD_R, H - PAD_T - PAD_B
    n = len(labels)
    band = plot_w / n
    bar_w = min(24, band * 0.6)
    y = lambda v: PAD_T + plot_h - (v / y_max) * plot_h if y_max else PAD_T + plot_h

    out = [f'<svg viewBox="0 0 {W} {H}" role="img" preserveAspectRatio="xMidYMid meet">']
    for t in y_ticks:
        ty = y(t)
        out.append(f'<line class="grid" x1="{PAD_L}" x2="{W - PAD_R}" y1="{ty:.1f}" y2="{ty:.1f}"/>')
        out.append(f'<text class="tick" x="{PAD_L - 6}" y="{ty + 4:.1f}" text-anchor="end">{y_fmt(t)}</text>')
    base = PAD_T + plot_h
    out.append(f'<line class="axis" x1="{PAD_L}" x2="{W - PAD_R}" y1="{base}" y2="{base}"/>')

    for i, label in enumerate(labels):
        x = PAD_L + band * i + (band - bar_w) / 2
        out.append(f'<g class="col" tabindex="0" data-tip="{html.escape(tips[i])}">')
        out.append(f'<rect class="hit" x="{PAD_L + band * i:.1f}" y="{PAD_T}" width="{band:.1f}" height="{plot_h}"/>')
        values = [vals[i] for _, vals in series]
        top_index = max((k for k, v in enumerate(values) if v > 0), default=-1)
        acc = 0.0
        for k, (var, vals) in enumerate(series):
            v = vals[i]
            if v <= 0:
                continue
            y0, y1 = y(acc), y(acc + v)
            acc += v
            gap = 2 if k < top_index else 0  # 2px surface gap between stacked segments
            h_seg = max(0.0, y0 - y1 - gap)
            if k == top_index:
                out.append(f'<path d="{bar_path(x, y1, bar_w, h_seg)}" style="fill:var({var})"/>')
            else:
                out.append(f'<rect x="{x:.1f}" y="{y1 + gap:.1f}" width="{bar_w:.1f}" height="{h_seg:.1f}" '
                           f'style="fill:var({var})"/>')
        if cap_labels and cap_labels[i]:
            out.append(f'<text class="cap" x="{x + bar_w / 2:.1f}" y="{y(acc) - 6:.1f}" '
                       f'text-anchor="middle">{cap_labels[i]}</text>')
        if i % label_every == 0 or i == n - 1:
            out.append(f'<text class="tick" x="{x + bar_w / 2:.1f}" y="{H - 8}" text-anchor="middle">'
                       f'{html.escape(label)}</text>')
        out.append("</g>")
    out.append("</svg>")
    return "\n".join(out)


def nice_ticks(max_v, count=4):
    if max_v <= 0:
        return 1, [0]
    raw = max_v / count
    mag = 10 ** (len(str(int(raw))) - 1) if raw >= 1 else 1
    step = next(s * mag for s in (1, 2, 5, 10) if s * mag >= raw)
    top = step * -(-max_v // step)
    return top, [step * i for i in range(int(top / step) + 1)]


# ---------------------------------------------------------------- page

def pct(v):
    return "–" if v is None else f"{v:.0f}%"


def build_html(data):
    today, week, prev = data["today"], data["week"], data["prev_week"]
    now = data["now"]

    # Stat tiles
    delta_html = ""
    if week["percent"] is not None and prev["percent"] is not None:
        diff = week["percent"] - prev["percent"]
        cls = "up" if diff >= 0.5 else "down" if diff <= -0.5 else "flat"
        arrow = "▲" if cls == "up" else "▼" if cls == "down" else "■"
        delta_html = f'<div class="delta {cls}">{arrow} {abs(diff):.0f} pts vs prior week</div>'

    tiles = f"""
    <div class="tiles">
      <div class="tile"><div class="label">Tracked today</div><div class="value">{fmt_duration(today["good"] + today["bad"])}</div></div>
      <div class="tile"><div class="label">Slouch alerts today</div><div class="value">{today["alerts"]}</div></div>
      <div class="tile"><div class="label">Best streak today</div><div class="value">{today["streak"]} min</div></div>
      <div class="tile"><div class="label">Last 7 days</div><div class="value">{pct(week["percent"])}</div>{delta_html}</div>
    </div>"""

    # Chart 1: % good posture per day, last 14 days
    days = data["days"]
    day_labels = [d.strftime("%a %-d") if i in (0, 6, 13) else d.strftime("%a")[0] for i, (d, _) in enumerate(days)]
    day_vals = [s["percent"] or 0 for _, s in days]
    day_tips = [f"{d.strftime('%a %-d %b')}: " + (f"{s['percent']:.0f}% good posture, {fmt_duration(s['good'] + s['bad'])} tracked, "
                                                  f"{s['alerts']} alerts" if s["percent"] is not None else "no data")
                for d, s in days]
    caps = [""] * 13 + [pct(days[-1][1]["percent"]) if days[-1][1]["percent"] is not None else ""]
    chart_days = column_chart(day_labels, [("--series-1", day_vals)], 100, [0, 25, 50, 75, 100],
                              lambda t: f"{t}%", day_tips, cap_labels=caps)

    # Chart 2: today by hour, good vs slouched minutes (stacked)
    th = data["today_hours"]
    first = min([9] + list(th)); last = max([18] + list(th))
    hours = list(range(first, last + 1))
    good_m = [th.get(h, (0, 0))[0] / 60 for h in hours]
    bad_m = [th.get(h, (0, 0))[1] / 60 for h in hours]
    top, ticks = nice_ticks(max([g + b for g, b in zip(good_m, bad_m)] + [10]))
    hour_tips = [f"{hour_label(h)}–{hour_label((h + 1) % 24)}: {g:.0f} min upright, {b:.0f} min slouched"
                 for h, g, b in zip(hours, good_m, bad_m)]
    chart_today = column_chart([hour_label(h) for h in hours], [("--series-1", good_m), ("--series-2", bad_m)],
                               top, ticks, lambda t: f"{t:g}m", hour_tips, label_every=2)

    # Chart 3: slouch rate by hour of day, last 30 days
    hod = data["hour_of_day"]
    tracked_hours = sorted(h for h, (g, b) in hod.items() if g + b >= 300)
    worst_html = ""
    if tracked_hours:
        hrs = list(range(tracked_hours[0], tracked_hours[-1] + 1))
        rate = {h: 100 * hod[h][1] / sum(hod[h]) for h in tracked_hours}
        vals = [rate.get(h, 0) for h in hrs]
        worst = max(rate, key=rate.get)
        rtop, rticks = nice_ticks(max(vals + [10]))
        rtop, rticks = min(rtop, 100), [t for t in rticks if t <= 100]
        rtips = [f"{hour_label(h)}–{hour_label((h + 1) % 24)}: " +
                 (f"slouching {rate[h]:.0f}% of the time ({fmt_duration(sum(hod[h]))} tracked)" if h in rate
                  else "not enough data") for h in hrs]
        chart_hod = column_chart([hour_label(h) for h in hrs], [("--series-2", vals)], rtop, rticks,
                                 lambda t: f"{t:g}%", rtips, label_every=2)
        worst_html = (f'<p class="note">You slouch most around <strong>{hour_label(worst)}–{hour_label((worst + 1) % 24)}</strong> '
                      f'({rate[worst]:.0f}% of the time). That\'s a good time for a stretch break.</p>')
    else:
        chart_hod = '<p class="empty">Not enough data yet. Each hour needs at least 5 minutes of tracking.</p>'

    # Table view
    table_rows = "\n".join(
        f"<tr><td>{d.strftime('%a %-d %b')}</td><td>{pct(s['percent'])}</td>"
        f"<td>{fmt_duration(s['good'] + s['bad'])}</td><td>{s['alerts']}</td><td>{s['streak']} min</td></tr>"
        for d, s in reversed(days))

    body = f"""
    <header>
      <h1>Posture report</h1>
      <p class="sub">{now.strftime("%A %-d %B %Y, %-I:%M %p")}</p>
    </header>
    <section class="hero">
      <div class="label">Good posture today</div>
      <div class="big{' none' if today['percent'] is None else ''}">{pct(today["percent"]) if today["percent"] is not None else "No data yet"}</div>
      <div class="sub">{fmt_duration(today["good"])} upright · {fmt_duration(today["bad"])} slouched</div>
    </section>
    {tiles}
    <section class="card" id="days">
      <h2>Good posture, last 14 days</h2>
      <p class="sub">Share of tracked time spent sitting upright</p>
      {chart_days}
    </section>
    <section class="card" id="today">
      <h2>Today by hour</h2>
      <div class="legend"><span><i style="background:var(--series-1)"></i>Upright</span><span><i style="background:var(--series-2)"></i>Slouched</span></div>
      {chart_today}
    </section>
    <section class="card" id="hours">
      <h2>When you slouch most</h2>
      <p class="sub">Share of time slouching by hour of day, last 30 days</p>
      {chart_hod}
      {worst_html}
    </section>
    <details class="card">
      <summary>Daily table</summary>
      <table><thead><tr><th>Day</th><th>Good posture</th><th>Tracked</th><th>Alerts</th><th>Best streak</th></tr></thead>
      <tbody>{table_rows}</tbody></table>
    </details>
    """
    if not data["has_data"]:
        body = """<header><h1>Posture report</h1></header>
        <section class="card"><p class="empty">No posture data yet. Keep Posture Guard running and check back later.</p></section>"""

    return PAGE.replace("{{BODY}}", body)


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Posture Report</title>
<style>
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface-1: #fcfcfb; --border: rgba(11,11,11,0.10);
  --text-primary: #0b0b0b; --text-secondary: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --good-text: #006300; --bad-text: #b3261e;
  --series-1: #2a78d6; --series-2: #eb6834;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0d0d0d; --surface-1: #1a1a19; --border: rgba(255,255,255,0.10);
    --text-primary: #ffffff; --text-secondary: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --good-text: #0ca30c; --bad-text: #e66767;
    --series-1: #3987e5; --series-2: #d95926;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface-1: #1a1a19; --border: rgba(255,255,255,0.10);
  --text-primary: #ffffff; --text-secondary: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --good-text: #0ca30c; --bad-text: #e66767;
  --series-1: #3987e5; --series-2: #d95926;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--text-primary);
       font: 15px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 760px; margin: 0 auto; padding: 32px 16px 64px; }
h1 { font-size: 26px; margin: 0; font-weight: 650; }
h2 { font-size: 16px; margin: 0 0 2px; font-weight: 600; }
.sub { color: var(--text-secondary); margin: 2px 0 0; font-size: 13px; }
header { margin-bottom: 24px; }
.hero { margin-bottom: 20px; }
.hero .label, .tile .label { color: var(--text-secondary); font-size: 13px; }
.hero .big { font-size: 56px; font-weight: 650; line-height: 1.1; }
.hero .big.none { font-size: 28px; color: var(--text-secondary); padding: 8px 0; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 20px; }
.tile, .card { background: var(--surface-1); border: 1px solid var(--border); border-radius: 12px; }
.tile { padding: 14px 16px; }
.tile .value { font-size: 24px; font-weight: 600; margin-top: 2px; }
.delta { font-size: 12px; margin-top: 4px; color: var(--text-secondary); }
.delta.up { color: var(--good-text); } .delta.down { color: var(--bad-text); }
.card { padding: 18px 18px 12px; margin-bottom: 16px; }
.card svg { width: 100%; height: auto; display: block; margin-top: 10px; overflow: visible; }
.grid { stroke: var(--grid); stroke-width: 1; }
.axis { stroke: var(--axis); stroke-width: 1; }
.tick { fill: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.cap { fill: var(--text-primary); font-size: 12px; font-weight: 600; }
.hit { fill: transparent; }
.col:hover .hit, .col:focus .hit { fill: var(--text-primary); opacity: 0.05; }
.col:focus { outline: none; }
.legend { display: flex; gap: 16px; font-size: 13px; color: var(--text-secondary); margin-top: 6px; }
.legend i { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 6px; vertical-align: -1px; }
.note { font-size: 14px; color: var(--text-secondary); margin: 8px 0 4px; }
.note strong { color: var(--text-primary); }
.empty { color: var(--text-secondary); padding: 24px 0; text-align: center; }
summary { cursor: pointer; font-weight: 600; padding-bottom: 6px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; margin: 8px 0; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--grid); }
td:not(:first-child), th:not(:first-child) { text-align: right; font-variant-numeric: tabular-nums; }
th { color: var(--text-secondary); font-weight: 500; }
#tip { position: fixed; pointer-events: none; background: var(--surface-1); color: var(--text-primary);
       border: 1px solid var(--border); border-radius: 8px; padding: 6px 10px; font-size: 12px;
       box-shadow: 0 4px 16px rgba(0,0,0,0.12); opacity: 0; transition: opacity .1s; max-width: 260px; }
@media (max-width: 480px) { .hero .big { font-size: 48px; } }
</style></head>
<body><main>
{{BODY}}
</main>
<div id="tip" role="status"></div>
<script>
const tip = document.getElementById('tip');
function show(el, x, y) {
  tip.textContent = el.dataset.tip; tip.style.opacity = 1;
  const r = tip.getBoundingClientRect();
  tip.style.left = Math.min(x + 12, innerWidth - r.width - 8) + 'px';
  tip.style.top = Math.max(8, y - r.height - 12) + 'px';
}
document.querySelectorAll('[data-tip]').forEach(el => {
  el.addEventListener('mousemove', e => show(el, e.clientX, e.clientY));
  el.addEventListener('mouseleave', () => tip.style.opacity = 0);
  el.addEventListener('focus', () => { const b = el.getBoundingClientRect(); show(el, b.left, b.top); });
  el.addEventListener('blur', () => tip.style.opacity = 0);
});
</script>
</body></html>
"""


def write_report(history, out_dir):
    path = Path(out_dir) / REPORT_FILE_NAME
    path.write_text(build_html(gather(history)))
    return path


def open_report(history, out_dir):
    path = write_report(history, out_dir)
    subprocess.Popen(["open", str(path)])
    return path


def main():
    import posture_guard as pg
    history = History(pg.HISTORY_FILE)
    try:
        print(f"Opened {open_report(history, pg.CONFIG_DIR)}")
    finally:
        history.close()


if __name__ == "__main__":
    sys.exit(main())
