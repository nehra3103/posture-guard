"""Eye care: blink rate and distance from the screen, from MediaPipe Face Mesh landmarks."""

import math
import statistics
from collections import deque

IRIS_DIAMETER_MM = 11.7   # nearly the same in all adults, so the iris works as a ruler
CAMERA_HFOV_DEG = 75.0    # horizontal field of view; MacBook cameras are roughly 70–80°

# Six points per eye (corner, top, top, corner, bottom, bottom) for the eye aspect ratio.
LEFT_EYE = (33, 160, 158, 133, 153, 144)
RIGHT_EYE = (362, 385, 387, 263, 373, 380)
IRIS_EDGES = ((469, 471), (474, 476))  # left/right edge points of each iris

CLOSED_BELOW = 0.65  # eye counts as closed below this fraction of its usual open level
OPEN_ABOVE = 0.80
MAX_BLINK_S = 0.5    # longer closures are eyes shut / looking down, not blinks


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def eye_aspect_ratio(pt, idx):
    p1, p2, p3, p4, p5, p6 = (pt(i) for i in idx)
    return (_dist(p2, p6) + _dist(p3, p5)) / (2 * _dist(p1, p4) + 1e-6)


class EyeTracker:
    def __init__(self, hfov_deg=CAMERA_HFOV_DEG):
        self.hfov = math.radians(hfov_deg)
        self.ears = deque()      # (time, ear) over the last 10 s, for an adaptive "open" level
        self.blinks = deque()    # blink times
        self.seen = deque()      # times the face was visible
        self.closed_since = None
        self.distances = deque(maxlen=15)
        self.distance_cm = None

    def update(self, face, width, height, now):
        """face: one entry of FaceMesh multi_face_landmarks, or None."""
        for q, keep in ((self.ears, 10), (self.seen, 180), (self.blinks, 180)):
            while q and now - (q[0][0] if isinstance(q[0], tuple) else q[0]) > keep:
                q.popleft()
        if face is None:
            self.closed_since = None
            return

        lm = face.landmark

        def pt(i):
            return lm[i].x * width, lm[i].y * height

        ear = (eye_aspect_ratio(pt, LEFT_EYE) + eye_aspect_ratio(pt, RIGHT_EYE)) / 2
        self.seen.append(now)
        self.ears.append((now, ear))

        if len(self.ears) >= 10:
            values = sorted(e for _, e in self.ears)
            open_level = values[int(len(values) * 0.8)]
            if self.closed_since is None and ear < open_level * CLOSED_BELOW:
                self.closed_since = now
            elif self.closed_since is not None and ear > open_level * OPEN_ABOVE:
                if now - self.closed_since <= MAX_BLINK_S:
                    self.blinks.append(now)
                self.closed_since = None

        iris_px = statistics.mean(_dist(pt(a), pt(b)) for a, b in IRIS_EDGES)
        if iris_px > 1:
            focal_px = (width / 2) / math.tan(self.hfov / 2)
            self.distances.append(focal_px * IRIS_DIAMETER_MM / iris_px / 10)
            self.distance_cm = statistics.median(self.distances)

    def coverage(self, now, window):
        """Fraction of the last `window` seconds in which the face was visible."""
        return len({int(t) for t in self.seen if now - t <= window}) / window

    def blink_rate(self, now, window=120):
        """Blinks per minute over the window, or None if the face wasn't visible enough to say."""
        if self.coverage(now, window) < 0.8:
            return None
        return sum(1 for t in self.blinks if now - t <= window) * 60 / window
