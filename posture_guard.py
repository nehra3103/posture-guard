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
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from types import SimpleNamespace

import cv2
import mediapipe as mp

import calls
from eyes import EyeTracker
from history import History

PL = mp.solutions.pose.PoseLandmark
REQUIRED = (PL.LEFT_EAR, PL.RIGHT_EAR, PL.LEFT_SHOULDER, PL.RIGHT_SHOULDER)

CONFIG_DIR = Path.home() / "Library" / "Application Support" / "PostureGuard"
CONFIG_FILE = CONFIG_DIR / "config.json"
HISTORY_FILE = CONFIG_DIR / "history.db"

# Moving your chair/laptop shifts where you sit in the frame; slouching barely does.
MOVED_SHIFT = 0.10        # shoulder centre moved by this fraction of the frame width/height
MOVED_SCALE = 0.25        # or you're this much closer/further (shoulder width change)
MOVED_AFTER_RETURN = 5.0  # seconds of a changed position after coming back before asking
MOVED_WHILE_SEATED = 30.0 # seconds of a changed position otherwise (e.g. laptop nudged)
AWAY_FOR_RETURN = 15.0    # seconds out of frame that count as "stood up and came back"
STOOD_UP_RISE = 0.12      # shoulders this far above sitting height (fraction of frame) = standing

PROCESS_WIDTH = 960       # frames are downscaled to this width before analysis

TOO_CLOSE_FOR = 20.0          # seconds closer than min_distance before warning
DISTANCE_ALERT_EVERY = 300.0  # at most one too-close warning per 5 min
LOW_BLINK_RATE = 8.0          # blinks/min over 2 min; normal is 15-20, screens often drop it to ~5
BLINK_ALERT_EVERY = 1200.0    # at most one eye-break reminder per 20 min

# Slouch alerts get firmer if you ignore them: (title, sound) per level; level 3 also flashes the screen.
ESCALATION = {
    1: ("Sit up straight!", "Funk"),
    2: ("Still slouching", "Ping"),
    3: ("You've been slouching a while", "Sosumi"),
}

DEFAULTS = {
    "camera": None,        # None = pick the Mac's built-in camera
    "sensitivity": 0.15,
    "tilt": 8.0,
    "grace": 10.0,
    "cooldown": 15.0,
    "calibrate": 4.0,
    "remind_every": 15.0,
    "break_every": 45.0,
    "away_reset": 120.0,
    "fps": 15.0,           # fast enough to catch blinks
    "timer_only": False,   # menu bar app: camera off, timed reminders only
    "escalate": True,      # alerts get firmer if ignored
    "mute_in_calls": True, # no alerts while another app is using the microphone
    "eye_care": True,      # blink rate and screen distance
    "min_distance": 45.0,  # cm; warn when closer than this (0 = off)
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
    center_x: float    # shoulder midpoint as a fraction of frame width. Used to notice a moved chair/camera.
    center_y: float    # shoulder midpoint as a fraction of frame height.


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
        center_x=(lsx + rsx) / 2 / width,
        center_y=(lsy + rsy) / 2 / height,
    )


def average(samples):
    return Metrics(*(statistics.median(getattr(s, f.name) for s in samples) for f in fields(Metrics)))


def position_changed(current, baseline):
    """True if you're sitting somewhere different in the frame than when you calibrated."""
    return (abs(current.center_x - baseline.center_x) > MOVED_SHIFT
            or abs(current.center_y - baseline.center_y) > MOVED_SHIFT
            or abs(current.lean / baseline.lean - 1) > MOVED_SCALE)


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
    def __init__(self, settings, baseline=None, on_calibrated=None, history=None, on_moved=None,
                 on_alert=None, on_break=None):
        self.args = settings
        self.on_moved = on_moved  # called (from the camera thread) when your position seems to have changed
        self.on_alert = on_alert  # on_alert(title, message, sound, flash); default: a notification
        self.on_break = on_break  # called when a stretch break is due; default: a notification
        self.history = history
        self.pose = mp.solutions.pose.Pose(model_complexity=0,
                                           min_detection_confidence=0.5,
                                           min_tracking_confidence=0.5)
        self.face_mesh = None  # created on first use when eye care is on
        self.eyes = EyeTracker()
        self.baseline = baseline
        self.on_calibrated = on_calibrated
        self.calib_samples = []
        self.calib_until = None
        self.recent = deque(maxlen=max(1, int(settings.fps * 1.5)))  # ~1.5s smoothing window
        self.state = "starting"  # starting | calibrating | good | bad | away | paused | moved | break | sleeping

        now = time.time()
        self.bad_since = None
        self.good_since = None
        self.last_alert = 0.0
        self.escalation = 0         # slouch alerts sent in a row without sitting up in between
        self.last_seen = now
        self.sitting_since = now
        self.last_posture_reminder = now
        self.paused = False         # paused by you
        self.suspended = False      # paused by the system: Mac asleep, display off or screen locked
        self.returned_at = None     # when you came back after being away
        self.moved_since = None     # when your position first looked different from calibration
        self.moved_prompted = False # already asked about this position change
        self.moved = False          # waiting on an answer; slouch alerts are held meanwhile

        self.eyes_since = now
        self.too_close_since = None
        self.last_distance_alert = 0.0
        self.last_blink_alert = now  # give the blink counter a few minutes before the first reminder

        self.break_until = 0.0      # during a guided stretch: no posture checks
        self.stood_up = None        # did we see you stand up during the stretch? (None = couldn't tell)

        self.in_call = False
        self._call_checked = 0.0

        self.good_seconds = 0.0
        self.bad_seconds = 0.0
        self.alerts = 0

    @property
    def idle(self):
        return self.paused or self.suspended

    def wake(self):
        """Back from sleep/lock: you were away, so restart the timers instead of catching up."""
        now = time.time()
        self.suspended = False
        self.sitting_since = now
        self.last_posture_reminder = now
        self.last_blink_alert = now
        self.recent.clear()
        self.bad_since = self.good_since = None
        self.escalation = 0

    # ---- alerts
    def muted(self):
        """True while another app is using the microphone (you're probably on a call)."""
        if not self.args.mute_in_calls:
            self.in_call = False
            return False
        now = time.time()
        if now - self._call_checked >= 3:
            self._call_checked = now
            self.in_call = calls.mic_in_use()
        return self.in_call

    def alert(self, title, message, sound="Funk", flash=False):
        """Send an alert unless you're on a call. Returns True if it went out."""
        if self.muted():
            return False
        if self.on_alert:
            self.on_alert(title, message, sound, flash)
        else:
            notify(title, message, sound=sound)
        return True

    # ---- calibration
    def start_calibration(self):
        self.baseline = None
        self.calib_samples = []
        self.calib_until = time.time() + self.args.calibrate
        self.recent.clear()
        self.bad_since = None
        self.dismiss_moved()
        self.moved_prompted = False
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

    def dismiss_moved(self):
        """Keep the current calibration; don't ask again until you next step away."""
        self.moved = False
        self.moved_since = None

    def _check_moved(self, metrics, now):
        if self.moved or self.moved_prompted:
            return
        if not position_changed(metrics, self.baseline):
            self.moved_since = None
            return
        self.moved_since = self.moved_since or now
        just_returned = self.returned_at is not None and now - self.returned_at < 30
        if now - self.moved_since >= (MOVED_AFTER_RETURN if just_returned else MOVED_WHILE_SEATED):
            self.moved = self.moved_prompted = True
            self.bad_since = None
            print("Position changed since calibration.", flush=True)
            if self.on_moved:
                self.on_moved()
            else:
                notify("Did you move your chair or laptop?",
                       "Sit up straight and press c in the preview window to recalibrate.", sound="Glass")
                self.moved = False

    # ---- guided stretch breaks
    def start_break(self, seconds):
        self.break_until = time.time() + seconds
        self.stood_up = None

    def end_break(self):
        self.break_until = 0.0
        self.sitting_since = time.time()
        self.recent.clear()
        self.bad_since = None

    # ---- eye care
    def eye_info(self):
        if not self.args.eye_care or self.eyes.distance_cm is None:
            return ""
        parts = [f"~{self.eyes.distance_cm:.0f} cm from screen"]
        rate = self.eyes.blink_rate(time.time(), 60)
        if rate is not None:
            parts.append(f"{rate:.0f} blinks/min")
        return " · ".join(parts)

    def _check_eyes(self, face_visible, now):
        d = self.eyes.distance_cm
        if face_visible and self.args.min_distance > 0 and d is not None and d < self.args.min_distance:
            self.too_close_since = self.too_close_since or now
            if now - self.too_close_since >= TOO_CLOSE_FOR and now - self.last_distance_alert >= DISTANCE_ALERT_EVERY:
                if self.alert("You're too close to the screen",
                              f"About {d:.0f} cm away. Sit back to arm's length (50–70 cm) to ease your eyes and neck.",
                              sound="Glass"):
                    self.last_distance_alert = now
        else:
            self.too_close_since = None

        rate = self.eyes.blink_rate(now, 120)
        if rate is not None and rate < LOW_BLINK_RATE and now - self.last_blink_alert >= BLINK_ALERT_EVERY:
            if self.alert("Give your eyes a break",
                          f"You're blinking about {rate:.0f} times a minute (normal is 15–20). Blink slowly a few "
                          "times, then look at something 20 feet away for 20 seconds.", sound="Glass"):
                self.last_blink_alert = now

    # ---- per-frame logic
    def step(self, frame, dt):
        """Process one frame (None while paused). Returns (status_text, color, landmarks)."""
        now = time.time()
        dt = min(dt, 2.0)  # don't count a camera hiccup or wake-from-sleep as minutes of posture
        if self.suspended:
            self.state = "sleeping"
            return "Sleeping (Mac asleep or locked)", (200, 200, 200), None
        if self.paused:
            self.state = "paused"
            self._timers(now, tracking=False)
            return "Paused", (200, 200, 200), None

        h, w = frame.shape[:2]
        if w > PROCESS_WIDTH:  # full-HD frames are slower and no more accurate for this
            h, w = int(h * PROCESS_WIDTH / w), PROCESS_WIDTH
            frame = cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        result = self.pose.process(rgb)
        metrics = extract_metrics(result.pose_landmarks, w, h) if result.pose_landmarks else None

        face = None
        if self.args.eye_care:
            if self.face_mesh is None:
                self.face_mesh = mp.solutions.face_mesh.FaceMesh(max_num_faces=1, refine_landmarks=True,
                                                                 min_detection_confidence=0.5,
                                                                 min_tracking_confidence=0.5)
            faces = self.face_mesh.process(rgb).multi_face_landmarks
            face = faces[0] if faces else None
            self.eyes.update(face, w, h, now)
            if face is not None and self.history and self.eyes.distance_cm and now >= self.break_until:
                self.history.record(distance=self.eyes.distance_cm)

        if metrics:
            away_for = now - self.last_seen
            if away_for > self.args.away_reset:
                # They were away long enough to count as a break.
                self.sitting_since = now
            if away_for > AWAY_FOR_RETURN:
                self.returned_at = now
                self.moved_prompted = False  # a new sitting position is worth asking about again
                self.moved_since = None
            self.last_seen = now

        if now < self.break_until:
            self.state = "break"
            if self.stood_up is None:
                self.stood_up = False
            if metrics is None or (self.baseline and self.baseline.center_y - metrics.center_y > STOOD_UP_RISE):
                self.stood_up = True  # left the frame or shoulders rose well above sitting height
            return "Stretch break", (255, 200, 0), result.pose_landmarks

        if self.calib_until:
            self.state = "calibrating"
            left = max(0, self.calib_until - now)
            self._update_calibration(metrics, now)
            return f"Sit up straight... calibrating {left:.0f}s", (255, 200, 0), result.pose_landmarks

        self._timers(now, tracking=True)

        if not metrics:
            self.state = "away"
            self.recent.clear()
            self.bad_since = self.good_since = None
            self.escalation = 0
            return "No one detected", (160, 160, 160), result.pose_landmarks

        if self.args.eye_care:
            self._check_eyes(face is not None, now)

        self.recent.append(metrics)
        smoothed = average(self.recent)
        self._check_moved(smoothed, now)
        if self.moved:
            self.state = "moved"
            return "Position changed: recalibrate?", (255, 200, 0), result.pose_landmarks

        problems = find_problems(smoothed, self.baseline, self.args.sensitivity, self.args.tilt)

        if not problems:
            self.state = "good"
            self.bad_since = None
            self.good_since = self.good_since or now
            if now - self.good_since >= 5:
                self.escalation = 0  # sat up properly; next slouch starts gentle again
            self.good_seconds += dt
            if self.history:
                self.history.record(good=dt)
            return "Good posture", (0, 200, 0), result.pose_landmarks

        self.state = "bad"
        self.good_since = None
        self.bad_seconds += dt
        if self.history:
            self.history.record(bad=dt)
        self.bad_since = self.bad_since or now
        slouched_for = now - self.bad_since
        if slouched_for >= self.args.grace and now - self.last_alert >= self.args.cooldown:
            self.last_alert = now
            level = min(self.escalation + 1, 3) if self.args.escalate else 1
            title, sound = ESCALATION[level] if self.args.escalate else ("Sit up straight!", "Funk")
            if self.alert(title, ", ".join(problems), sound=sound, flash=level >= 3):
                self.escalation += 1
                self.alerts += 1
                if self.history:
                    self.history.record(alerts=1)
        return f"{', '.join(problems)} ({slouched_for:.0f}s)", (0, 0, 255), result.pose_landmarks

    def _timers(self, now, tracking):
        if self.suspended:
            return
        # Fallback reminder: only when the camera can't judge posture for us.
        if not tracking and self.args.remind_every > 0:
            if now - self.last_posture_reminder >= self.args.remind_every * 60:
                if self.alert("Posture check", "Shoulders back, chin tucked, screen at eye level."):
                    self.last_posture_reminder = now
        elif tracking:
            self.last_posture_reminder = now

        # Stretch breaks: based on continuous sitting time. Held until a call ends.
        if (self.args.break_every > 0 and now - self.sitting_since >= self.args.break_every * 60
                and now >= self.break_until and not self.muted()):
            self.sitting_since = now
            if self.on_break:
                self.on_break()
            else:
                notify("Time for a stretch break",
                       f"You've been sitting {self.args.break_every:.0f} min. "
                       "Stand up, roll your shoulders, do some chin tucks.", sound="Hero")

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

def draw_overlay(frame, status, color, landmarks, hint="c: recalibrate  p: pause  q: quit", info=""):
    if landmarks is not None:
        mp.solutions.drawing_utils.draw_landmarks(
            frame, landmarks, mp.solutions.pose.POSE_CONNECTIONS)
    frame = cv2.flip(frame, 1)  # mirror so it feels like a mirror
    if color == (0, 0, 255):  # slouching: red border so it's obvious even without notifications
        cv2.rectangle(frame, (0, 0), (frame.shape[1] - 1, frame.shape[0] - 1), color, 12)
    cv2.rectangle(frame, (0, 0), (frame.shape[1], 40), (0, 0, 0), -1)
    cv2.putText(frame, status, (10, 27), cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
    if info:
        cv2.putText(frame, info, (10, frame.shape[0] - 40), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
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

            if guard.idle and release_when_paused:
                cap.release()  # turn the camera (and its green light) off while paused or asleep
                while guard.idle and not stopped():
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
        cv2.imshow("Posture Guard", draw_overlay(frame, status, color, landmarks, info=guard.eye_info()))
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
