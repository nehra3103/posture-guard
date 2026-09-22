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
   - A **posture check** reminder every 30 minutes, but only when the camera isn't tracking (paused, `--no-camera`, or camera unavailable).

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
| 🟡 | Calibrating: sit up straight |
| ⚪ | Nobody at the desk |
| ⏸ | Paused (camera off) |
| ⚠️ | Camera unavailable (timer reminders only) |

Click the icon to see this session's stats, pause (for calls), recalibrate, show the camera preview, change sensitivity and alert timing, turn stretch breaks on or off, and toggle **Start at Login**. Settings you change there are remembered.

The first launch asks for camera permission for "Posture Guard". Logs are written to `~/Library/Logs/PostureGuard.log`. To remove the app and the start-at-login entry, run `.venv/bin/python menubar.py --uninstall`.

If you move the project folder, run `--install` again, because the app points to this folder.

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
| `--remind-every` | 30 | Minutes between fallback posture reminders (0 = off) |
| `--fps` | 8 | Frames analysed per second (lower uses less CPU) |

## Tips for accurate detection

- Put the camera roughly at eye level, facing you, with your shoulders in frame.
- Recalibrate whenever you move your chair or laptop (menu bar → Recalibrate, or `c` in the preview window).
- Good lighting helps the pose model.

## Where things are saved

- Settings and calibration: `~/Library/Application Support/PostureGuard/config.json`
- The app launcher: `~/Applications/Posture Guard.app`
- Start at login: `~/Library/LaunchAgents/com.postureguard.menubar.plist`
