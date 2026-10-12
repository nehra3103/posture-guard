"""Render the progress report from two weeks of made-up data and save docs/report.png for the README.

Uses a throwaway database, so your real history is never touched. Needs Google Chrome.
"""

import random
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import report  # noqa: E402
from history import History, local_midnight  # noqa: E402

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def fill(history, now):
    rng = random.Random(7)
    today = local_midnight(now)
    rows = []
    for days_ago in range(20, -1, -1):
        day = today - timedelta(days=days_ago)
        if day.weekday() >= 5 and rng.random() < 0.7:
            continue  # most weekends off
        skill = 0.55 + 0.25 * (20 - days_ago) / 20  # posture improves over the weeks
        start = day + timedelta(hours=9, minutes=rng.randint(0, 40))
        end = min(day + timedelta(hours=17, minutes=rng.randint(0, 60)), now)
        t = start
        while t < end:
            if 12.5 <= t.hour + t.minute / 60 < 13.25 or rng.random() < 0.06:
                t += timedelta(minutes=1)  # lunch and short trips away
                continue
            slump = 0.18 if t.hour >= 15 else 0.0  # the afternoon slump
            good_share = max(0.05, min(1.0, rng.gauss(skill - slump, 0.18)))
            good = 60 * good_share
            alerts = 1 if good_share < 0.35 and rng.random() < 0.3 else 0
            dist = rng.gauss(54 + 6 * (20 - days_ago) / 20, 4)
            rows.append((int(t.timestamp() // 60), good, 60 - good, alerts, dist * 400, 400))
            t += timedelta(minutes=1)
    history.db.executemany("INSERT INTO minutes VALUES (?, ?, ?, ?, ?, ?)", rows)
    history.db.commit()


def main():
    now = datetime.now().replace(hour=17, minute=30, second=0, microsecond=0)
    out = ROOT / "docs" / "report.png"
    out.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        history = History(Path(tmp) / "demo.db")
        fill(history, now)
        page = Path(tmp) / "report.html"
        page.write_text(report.build_html(report.gather(history, now)))
        history.close()
        subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                        "--force-device-scale-factor=2", "--window-size=1000,800",
                        f"--screenshot={out}", page.as_uri()],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
