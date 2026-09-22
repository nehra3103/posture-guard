# Posture Guard

Sitting at a computer all day leads to forward head posture ("tech neck") and rounded shoulders. Posture Guard watches through your webcam and sends a notification when you start slouching. It also reminds you to take stretch breaks.

Everything runs locally on your machine. No video is saved or uploaded.

## How it works

1. **Calibration.** The first time, sit up straight for 4 seconds. That becomes your personal baseline, and it's saved, so you only redo it if you move your camera or chair.
2. **Detection.** MediaPipe Pose tracks your ears and shoulders. Posture Guard compares four signals against your baseline:
   | Signal | What it catches |
   |---|---|
   | Ear-to-shoulder height (relative to shoulder width) | Head jutting forward or dropping (tech neck) |
   | Shoulder width in frame | Leaning in toward the screen |
   | Ear span / shoulder width | Shoulders rounding inward |
   | Shoulder line angle | Slumping to one side |
3. **Alerts.** If bad posture lasts 10 seconds, you get a macOS notification, then again every 15 seconds while you stay slouched.
4. **Timers.**
   - A **stretch break** reminder after 45 minutes of continuous sitting. Being away from the desk for 2+ minutes resets it.
   - A **posture check** reminder every 15 minutes (adjustable), but only when the camera isn't tracking (Timed Reminders Only mode, paused, `--no-camera`, or camera unavailable).

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Menu bar app (recommended)

```bash
.venv/bin/python menubar.py --install    # builds ~/Applications/Posture Guard.app
```

Open **Posture Guard** from Spotlight (Cmd+Space). There's no window or Dock icon, only an icon in the menu bar:

| Icon | Meaning |
|---|---|
| 🟢 | Good posture |
| 🔴 | Slouching |
| 🟡 | Calibrating, or asking to recalibrate |
| ⚪ | Nobody at the desk |
| ⏸ | Paused (camera off) |
| ⚠️ | Camera unavailable (timed reminders only) |
| ⏰ | Timed reminders only (camera off) |
| 🧘 | Guided stretch in progress |
| 💤 | Mac asleep, display off or screen locked (camera off, no reminders) |

### Smarter alerts

- **Escalation.** The first slouch alert is gentle (soft sound). If you ignore it, the next one is louder, and after that the screen pulses a soft red twice. Sitting up properly for 5 seconds resets it. You can turn this off under Settings → Escalating Alerts.
- **Muted during calls.** While any app is using a microphone (Zoom, Meet, FaceTime, Teams, Discord…), alerts stay silent and stretch breaks wait until the call ends. Your posture is still tracked, and the menu shows 🔇.
- **Guided stretch breaks.** When a break is due, a dialog offers **Start Stretch**, **Snooze 10 min** or **Skip**. The routine takes about 75 seconds: chin tucks, shoulder rolls, a chest opener, and looking far away. The camera checks whether you actually stood up. You can also start one any time with **Stretch Now**.

### Eye care

With **Eye Care** on (Settings), the app also tracks your face:
- **Distance from the screen**, measured from the size of your iris (about 11.7 mm across in almost everyone). If you're closer than 45 cm for 20 seconds, you get a warning. You can change this under Settings → Too-Close Warning.
- **Blink rate.** Normal is 15–20 blinks a minute, but staring at a screen often drops it to about 5. If it stays below 8 a minute over 2 minutes, you get a reminder to blink and look 20 feet away for 20 seconds (at most every 20 min).

Both show in the menu and the preview window. The distance assumes a camera with a ~75° horizontal field of view (typical for a MacBook). If it reads consistently high or low, change `CAMERA_HFOV_DEG` in `eyes.py`.

### Sleep and lock

When your Mac sleeps, the display turns off, the screen locks, or you switch to another user, Posture Guard turns the camera off and stops all alerts and reminders. When you're back, it resumes and restarts the sitting and reminder timers, so an overdue stretch break doesn't greet you the moment you unlock. A pause you set yourself stays paused. If the camera isn't ready right after wake, it retries every 30 seconds.

### Timed reminders only

**Timed Reminders Only** turns the camera off completely and just sends a posture reminder at the interval you pick under **Timed Reminder Interval** (5–60 min, default 15). The same interval is used while you're paused or the camera is unavailable.

**Moved your chair?** Calibration remembers where you sit in the frame. If you come back from a break and you're sitting somewhere clearly different (shifted sideways/up/down, or much closer or further away), Posture Guard holds slouch alerts and asks whether to recalibrate. It also asks if your position stays different for 30 seconds while you're seated, for example after nudging the laptop.

Click the icon to see today's stats, open the progress report, pause (for calls), recalibrate, show the camera preview, change sensitivity and alert timing, turn stretch breaks on or off, and toggle **Start at Login**. Settings you change there are remembered.

The first launch asks for camera permission for "Posture Guard". Logs are written to `~/Library/Logs/PostureGuard.log`. To remove the app and the start-at-login entry, run `.venv/bin/python menubar.py --uninstall`.

If you move the project folder, run `--install` again, because the app points to this folder.

## Progress tracking

Posture Guard records how many seconds you spend upright versus slouched each minute, plus the alerts, in a local database. Nothing leaves your Mac.

- **In the menu:** today's good-posture percentage, time tracked, best streak and alert count.
- **Progress report** (menu → *Open Progress Report…*, or `.venv/bin/python report.py`): a page in your browser with
  - today's score and a comparison of the last 7 days with the week before
  - good posture per day for the last 14 days
  - today by hour (upright vs slouched)
  - the hours when you slouch most, from the last 30 days
  - your average eye-to-screen distance, with ✓ **Good distance** (50 cm or more) or ⚠ **Needs attention**, shown on a 30–90 cm scale with the recommended 50–70 cm range highlighted (needs Eye Care on)
  - a daily table

A **streak** is a run of minutes with no alerts where you were upright at least half the time. Stepping away for up to 5 minutes doesn't break it.

## Terminal mode

This mode asks for camera permission for your terminal or VS Code. If it's denied, enable it in **System Settings → Privacy & Security → Camera**.

```bash
.venv/bin/python posture_guard.py                 # webcam + preview window
.venv/bin/python posture_guard.py --no-preview    # run in the background (Ctrl+C to stop)
.venv/bin/python posture_guard.py --no-camera     # timer reminders only
.venv/bin/python posture_guard.py --recalibrate   # ignore the saved calibration
```

Preview window keys: `c` recalibrate · `p` pause · `q` quit. When you quit, it prints a summary (percentage of time in good posture and the number of alerts).

### Tuning

Flags override the settings saved by the menu bar app, but only for that run.

| Flag | Default | Meaning |
|---|---|---|
| `--sensitivity` | 0.15 | How far from baseline counts as slouching (lower means stricter) |
| `--grace` | 10 | Seconds of slouching before an alert |
| `--cooldown` | 15 | Minimum seconds between alerts |
| `--tilt` | 8 | Degrees of shoulder tilt allowed |
| `--break-every` | 45 | Minutes of sitting before a stretch break (0 = off) |
| `--remind-every` | 15 | Minutes between fallback posture reminders (0 = off) |
| `--fps` | 15 | Frames analysed per second (lower uses less CPU, but under ~12 misses blinks) |

## Tips for accurate detection

- Put the camera roughly at eye level, facing you, with your shoulders in frame.
- Recalibrate whenever you move your chair or laptop (menu bar → Recalibrate, or `c` in the preview window).
- Good lighting helps the pose model.

## Where things are saved

- Settings and calibration: `~/Library/Application Support/PostureGuard/config.json`
- Posture history: `~/Library/Application Support/PostureGuard/history.db` (delete it to reset your stats)
- The app launcher: `~/Applications/Posture Guard.app`
- Start at login: `~/Library/LaunchAgents/com.postureguard.menubar.plist`
