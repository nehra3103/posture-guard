#!/usr/bin/env python3
"""Posture Guard: webcam-based slouch detection plus timed posture and stretch reminders.

Everything runs locally; no frames leave your machine.
This file is the core engine plus a terminal/preview-window mode. See menubar.py for the menu bar app.
"""

import argparse
import json
import math
import statistics
import subprocess
import sys
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace

import cv2
import mediapipe as mp

from history import History

PL = mp.solutions.pose.PoseLandmark
REQUIRED = (PL.LEFT_EAR, PL.RIGHT_EAR, PL.LEFT_SHOULDER, PL.RIGHT_SHOULDER)

CONFIG_DIR = Path.home() / "Library" / "Application Support" / "PostureGuard"
CONFIG_FILE = CONFIG_DIR / "config.json"
HISTORY_FILE = CONFIG_DIR / "history.db"

DEFAULTS = {
    "camera": None,        # None = pick the Mac's built-in camera
    "sensitivity": 0.15,
    "tilt": 8.0,
    "grace": 10.0,
    "cooldown": 15.0,
    "calibrate": 4.0,
    "remind_every": 30.0,
    "break_every": 45.0,
    "away_reset": 120.0,
    "fps": 8.0,
}


# ---------------------------------------------------------------- notifications

def notify(title, message, sound="Funk"):
    """Show a macOS notification and play a sound. Non-blocking.

    The sound is played directly so you still hear alerts when macOS hides banners
    (Focus / Do Not Disturb, or notifications turned off for Script Editor).
    """
    title = title.replace('"', "'")
    message = message.replace('"', "'")
    script = f'display notification "{message}" with title "{title}"'
    subprocess.Popen(["osascript", "-e", script],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.Popen(["afplay", f"/System/Library/Sounds/{sound}.aiff"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"[{time.strftime('%H:%M:%S')}] {title}: {message}", flush=True)


# ---------------------------------------------------------------- posture metrics

@dataclass
class Metrics:
    neck: float        # ear-to-shoulder vertical gap / shoulder width. Drops when the head juts forward or droops.
    lean: float        # shoulder width / frame width. Grows when leaning in toward the screen.
    tilt: float        # shoulder line angle in degrees. Drifts when slumping to one side.
    head_ratio: float  # ear span / shoulder width. Grows when the shoulders round inward.


def extract_metrics(landmarks, width, height, min_visibility=0.5):
    lm = landmarks.landmark
    if any(lm[i].visibility < min_visibility for i in REQUIRED):
        return None

    def px(i):
        return lm[i].x * width, lm[i].y * height

    lsx, lsy = px(PL.LEFT_SHOULDER)
    rsx, rsy = px(PL.RIGHT_SHOULDER)
    lex, ley = px(PL.LEFT_EAR)
    rex, rey = px(PL.RIGHT_EAR)

    shoulder_w = math.hypot(lsx - rsx, lsy - rsy)
    if shoulder_w < 1:
        return None

    return Metrics(
        neck=((lsy + rsy) / 2 - (ley + rey) / 2) / shoulder_w,
        lean=shoulder_w / width,
        tilt=math.degrees(math.atan2(lsy - rsy, lsx - rsx)),
        head_ratio=math.hypot(lex - rex, ley - rey) / shoulder_w,
    )


def average(samples):
    return Metrics(*(statistics.median(getattr(s, f) for s in samples)
                     for f in ("neck", "lean", "tilt", "head_ratio")))


def find_problems(current, baseline, sensitivity, tilt_limit):
    """Compare smoothed metrics against the calibrated baseline and return a list of issues."""
    problems = []
    if current.neck < baseline.neck * (1 - sensitivity):
        problems.append("Head forward / neck bent")
    if current.lean > baseline.lean * (1 + sensitivity):
        problems.append("Leaning toward screen")
    if current.head_ratio > baseline.head_ratio * (1 + sensitivity):
        problems.append("Shoulders rounding")
    if abs(current.tilt - baseline.tilt) > tilt_limit:
        problems.append("Leaning to one side")
    return problems


# ---------------------------------------------------------------- saved settings

def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    tmp.replace(CONFIG_FILE)


def build_settings(cfg, cli_args=None):
    """Defaults, overridden by saved settings, overridden by flags given on the command line."""
    settings = dict(DEFAULTS)
    settings.update({k: v for k, v in cfg.get("settings", {}).items() if k in DEFAULTS})
    if cli_args is not None:
        settings.update({k: v for k, v in vars(cli_args).items() if k in DEFAULTS and v is not None})
    return SimpleNamespace(**settings)


def load_baseline(cfg, camera):
    """Return the saved calibration, but only if it was made with this same camera."""
    saved = cfg.get("baseline")
    if not saved or saved.get("camera") != camera:
        return None
    try:
        return Metrics(**saved["metrics"])
    except (KeyError, TypeError):
        return None


def save_baseline(baseline, camera):
    cfg = load_config()
    cfg["baseline"] = {"camera": camera, "metrics": asdict(baseline), "saved_at": time.strftime("%Y-%m-%d %H:%M")}
    save_config(cfg)


# ---------------------------------------------------------------- engine

class PostureGuard:
    def __init__(self, settings, baseline=None, on_calibrated=None, history=None):
        self.args = settings
        self.history = history
        self.pose = mp.solutions.pose.Pose(model_complexity=0,
                                           min_detection_confidence=0.5,
                                           min_tracking_confidence=0.5)
        self.baseline = baseline
        self.on_calibrated = on_calibrated
        self.calib_samples = []
        self.calib_until = None
        self.recent = deque(maxlen=max(1, int(settings.fps * 1.5)))  # ~1.5s smoothing window
        self.state = "starting"  # starting | calibrating | good | bad | away | paused

        now = time.time()
        self.bad_since = None
        self.last_alert = 0.0
        self.last_seen = now
        self.sitting_since = now
        self.last_posture_reminder = now
        self.paused = False

        self.good_seconds = 0.0
        self.bad_seconds = 0.0
        self.alerts = 0

    # ---- calibration
    def start_calibration(self):
        self.baseline = None
        self.calib_samples = []
        self.calib_until = time.time() + self.args.calibrate
        self.recent.clear()
        self.bad_since = None
        notify("Posture Guard: calibrating",
               f"Sit up straight and look at the screen for {self.args.calibrate:.0f} seconds.", sound="Tink")

    def _update_calibration(self, metrics, now):
        if metrics:
            self.calib_samples.append(metrics)
        if now < self.calib_until:
            return
        if len(self.calib_samples) < 5:
            print("Couldn't see your ears and shoulders clearly. Retrying calibration.", flush=True)
            self.start_calibration()
            return
        self.baseline = average(self.calib_samples)
        self.calib_until = None
        print("Calibrated. "
              f"(neck={self.baseline.neck:.2f}, lean={self.baseline.lean:.2f}, "
              f"tilt={self.baseline.tilt:.1f}, head={self.baseline.head_ratio:.2f})", flush=True)
        notify("Posture Guard", "Calibrated. I'll let you know if you start slouching.", sound="Glass")
        if self.on_calibrated:
            self.on_calibrated(self.baseline)

    # ---- per-frame logic
    def step(self, frame, dt):
        """Process one frame (None while paused). Returns (status_text, color, landmarks)."""
        now = time.time()
        dt = min(dt, 2.0)  # don't count a camera hiccup or wake-from-sleep as minutes of posture
        if self.paused:
            self.state = "paused"
            self._timers(now, tracking=False)
            return "Paused", (200, 200, 200), None

        h, w = frame.shape[:2]
        result = self.pose.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        metrics = extract_metrics(result.pose_landmarks, w, h) if result.pose_landmarks else None

        if metrics:
            if now - self.last_seen > self.args.away_reset:
                # They were away long enough to count as a break.
                self.sitting_since = now
            self.last_seen = now

        if self.calib_until:
            self.state = "calibrating"
            left = max(0, self.calib_until - now)
            self._update_calibration(metrics, now)
            return f"Sit up straight... calibrating {left:.0f}s", (255, 200, 0), result.pose_landmarks

        self._timers(now, tracking=True)

        if not metrics:
            self.state = "away"
            self.recent.clear()
            self.bad_since = None
            return "No one detected", (160, 160, 160), result.pose_landmarks

        self.recent.append(metrics)
        problems = find_problems(average(self.recent), self.baseline,
                                 self.args.sensitivity, self.args.tilt)

        if not problems:
            self.state = "good"
            self.bad_since = None
            self.good_seconds += dt
            if self.history:
                self.history.record(good=dt)
            return "Good posture", (0, 200, 0), result.pose_landmarks

        self.state = "bad"
        self.bad_seconds += dt
        if self.history:
            self.history.record(bad=dt)
        self.bad_since = self.bad_since or now
        slouched_for = now - self.bad_since
        if slouched_for >= self.args.grace and now - self.last_alert >= self.args.cooldown:
            notify("Sit up straight!", ", ".join(problems))
            self.last_alert = now
            self.alerts += 1
            if self.history:
                self.history.record(alerts=1)
        return f"{', '.join(problems)} ({slouched_for:.0f}s)", (0, 0, 255), result.pose_landmarks

    def _timers(self, now, tracking):
        # Fallback reminder: only when the camera can't judge posture for us.
        if not tracking and self.args.remind_every > 0:
            if now - self.last_posture_reminder >= self.args.remind_every * 60:
                notify("Posture check", "Shoulders back, chin tucked, screen at eye level.")
                self.last_posture_reminder = now
        elif tracking:
            self.last_posture_reminder = now

        # Stretch breaks: based on continuous sitting time.
        if self.args.break_every > 0 and now - self.sitting_since >= self.args.break_every * 60:
            notify("Time for a stretch break",
                   f"You've been sitting {self.args.break_every:.0f} min. "
                   "Stand up, roll your shoulders, do some chin tucks.", sound="Hero")
            self.sitting_since = now

    def good_percent(self):
        total = self.good_seconds + self.bad_seconds
        return None if total < 1 else 100 * self.good_seconds / total

    def summary(self):
        pct = self.good_percent()
        if pct is None:
            return "No posture data recorded yet."
        total = self.good_seconds + self.bad_seconds
        return f"Tracked {total / 60:.1f} min: {pct:.0f}% good posture, {self.alerts} slouch alert(s)."


# ---------------------------------------------------------------- camera

def draw_overlay(frame, status, color, landmarks, hint="c: recalibrate  p: pause  q: quit"):
    if landmarks is not None:
        mp.solutions.drawing_utils.draw_landmarks(
            frame, landmarks, mp.solutions.pose.POSE_CONNECTIONS)
    frame = cv2.flip(frame, 1)  # mirror so it feels like a mirror
    if color == (0, 0, 255):  # slouching: red border so it's obvious even without notifications
        cv2.rectangle(frame, (0, 0), (frame.shape[1] - 1, frame.shape[0] - 1), color, 12)
    cv2.rectangle(frame, (0, 0), (frame.shape[1], 40), (0, 0, 0), -1)
    cv2.putText(frame, status, (10, 27), cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
    cv2.putText(frame, hint, (10, frame.shape[0] - 12),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
    return frame


def list_cameras():
    """Return [(opencv_index, name, is_builtin, is_phone)] in the same order OpenCV numbers them.

    OpenCV's macOS backend takes AVFoundation's video + muxed devices and sorts them by uniqueID,
    so we mirror that here to map names to indices.
    """
    try:
        import AVFoundation as AV
    except ImportError:
        return []
    devices = (list(AV.AVCaptureDevice.devicesWithMediaType_(AV.AVMediaTypeVideo))
               + list(AV.AVCaptureDevice.devicesWithMediaType_(AV.AVMediaTypeMuxed)))
    devices.sort(key=lambda d: str(d.uniqueID()))
    cameras = []
    for i, d in enumerate(devices):
        name = str(d.localizedName())
        kind = str(d.deviceType())
        is_phone = "Continuity" in kind or "iPhone" in name or "iPad" in name
        cameras.append((i, name, "BuiltIn" in kind, is_phone))
    return cameras


def pick_default_camera():
    """Prefer the Mac's built-in camera, then any non-iPhone camera, over Continuity Camera."""
    cameras = list_cameras()
    for pref in (lambda c: c[2], lambda c: not c[3]):
        for index, name, *_ in filter(pref, cameras):
            print(f"Using camera {index}: {name}", flush=True)
            return index
    return 0


def camera_name(index):
    for i, name, *_ in list_cameras():
        if i == index:
            return name
    return f"camera {index}"


def open_camera(index, wait=20):
    """Open the camera, retrying while the user answers macOS's permission prompt."""
    cap = cv2.VideoCapture(index)
    deadline = time.time() + wait
    while not cap.isOpened() and time.time() < deadline:
        if wait:
            print("Waiting for camera access. Click 'Allow' if macOS asks...", flush=True)
            wait = 0
        time.sleep(2)
        cap = cv2.VideoCapture(index)
    return cap


def run_camera(guard, settings, on_frame, stop=None, release_when_paused=False):
    """Capture loop shared by the terminal and menu bar apps.

    on_frame(frame, status, color, landmarks) is called for every processed frame (frame is None
    while paused) and returns False to stop. Returns False if the camera couldn't be used.
    """
    stopped = stop.is_set if stop else (lambda: False)
    cap = open_camera(settings.camera)
    if not cap.isOpened():
        return False

    if guard.baseline is None:
        guard.start_calibration()
    else:
        print("Using saved calibration. Recalibrate if you've moved your camera or chair.", flush=True)

    frame_time = 1.0 / settings.fps
    last = time.time()
    failures = 0
    try:
        while not stopped():
            start = time.time()

            if guard.paused and release_when_paused:
                cap.release()  # turn the camera (and its green light) off while paused
                while guard.paused and not stopped():
                    on_frame(None, *guard.step(None, 0))
                    time.sleep(0.5)
                cap = open_camera(settings.camera, wait=5)
                if not cap.isOpened():
                    return False
                last = time.time()
                continue

            ok, frame = cap.read()
            if not ok:
                failures += 1
                if failures > 30:
                    print("Camera stopped delivering frames.", flush=True)
                    return False
                time.sleep(0.1)
                continue
            failures = 0

            now = time.time()
            status, color, landmarks = guard.step(frame, now - last)
            last = now
            if on_frame(frame, status, color, landmarks) is False:
                break
            time.sleep(max(0, frame_time - (time.time() - start)))
    finally:
        cap.release()
    return True


# ---------------------------------------------------------------- terminal mode

def run_timer_only(guard):
    print("Timer-only mode: posture reminders every "
          f"{guard.args.remind_every:.0f} min, stretch breaks every {guard.args.break_every:.0f} min.")
    while True:
        guard._timers(time.time(), tracking=False)
        time.sleep(5)


def parse_args():
    d = DEFAULTS
    p = argparse.ArgumentParser(
        description="Webcam posture monitor with slouch alerts and stretch reminders. "
                    "Settings changed in the menu bar app are remembered; flags here override them for this run.")
    p.add_argument("--camera", type=int, help="camera index (default: the Mac's built-in camera; see --list-cameras)")
    p.add_argument("--list-cameras", action="store_true", help="show available cameras and exit")
    p.add_argument("--no-camera", action="store_true", help="timer reminders only, don't use the webcam")
    p.add_argument("--no-preview", dest="preview", action="store_false", help="run without the preview window")
    p.add_argument("--recalibrate", action="store_true", help="ignore the saved calibration and calibrate again")
    p.add_argument("--sensitivity", type=float,
                   help=f"fractional change from your baseline that counts as slouching (default {d['sensitivity']})")
    p.add_argument("--tilt", type=float, help=f"shoulder tilt in degrees before alerting (default {d['tilt']:g})")
    p.add_argument("--grace", type=float, help=f"seconds of slouching before an alert (default {d['grace']:g})")
    p.add_argument("--cooldown", type=float, help=f"minimum seconds between slouch alerts (default {d['cooldown']:g})")
    p.add_argument("--calibrate", type=float, help=f"calibration duration in seconds (default {d['calibrate']:g})")
    p.add_argument("--remind-every", type=float,
                   help="minutes between posture reminders when the camera isn't tracking "
                        f"(0 = off, default {d['remind_every']:g})")
    p.add_argument("--break-every", type=float,
                   help=f"minutes of continuous sitting before a stretch break (0 = off, default {d['break_every']:g})")
    p.add_argument("--away-reset", type=float,
                   help=f"seconds away from the desk that count as a break (default {d['away_reset']:g})")
    p.add_argument("--fps", type=float, help=f"frames analysed per second (default {d['fps']:g})")
    return p.parse_args()


def main():
    args = parse_args()
    if args.list_cameras:
        for index, name, builtin, phone in list_cameras():
            tag = " (built-in)" if builtin else " (iPhone/Continuity)" if phone else ""
            print(f"  --camera {index}   {name}{tag}")
        return

    cfg = load_config()
    settings = build_settings(cfg, args)
    if settings.camera is None and not args.no_camera:
        settings.camera = pick_default_camera()
    cam = camera_name(settings.camera) if not args.no_camera else None
    baseline = None if args.recalibrate else load_baseline(cfg, cam)
    history = History(HISTORY_FILE)
    guard = PostureGuard(settings, baseline, on_calibrated=lambda b: save_baseline(b, cam), history=history)

    def on_frame(frame, status, color, landmarks):
        if not args.preview:
            return True
        cv2.imshow("Posture Guard", draw_overlay(frame, status, color, landmarks))
        key = cv2.waitKey(1) & 0xFF
        if key in (ord("q"), 27):
            return False
        if key == ord("c"):
            guard.start_calibration()
        if key == ord("p"):
            guard.paused = not guard.paused
            print("Paused." if guard.paused else "Resumed.")
        return True

    try:
        if args.no_camera or not run_camera(guard, settings, on_frame):
            if not args.no_camera:
                print("Couldn't use the camera. Allow your terminal app (Terminal or Visual Studio Code) in\n"
                      "System Settings > Privacy & Security > Camera, then fully quit and reopen that app.\n"
                      "Falling back to timer reminders for now.")
            run_timer_only(guard)
    except KeyboardInterrupt:
        pass
    finally:
        cv2.destroyAllWindows()
        history.close()
    print("\n" + guard.summary())


if __name__ == "__main__":
    sys.exit(main())
