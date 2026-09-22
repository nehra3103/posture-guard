#!/usr/bin/env python3
"""Posture Guard menu bar app: posture detection runs in the background behind a menu bar icon.

    python menubar.py              run the menu bar app
    python menubar.py --install    build ~/Applications/Posture Guard.app (double-click to launch)
    python menubar.py --uninstall  remove the app and the start-at-login entry
"""

import argparse
import fcntl
import math
import plistlib
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import cv2
import AppKit
import rumps
from AppKit import (NSApplication, NSApplicationActivationPolicyAccessory, NSBackingStoreBuffered, NSColor,
                    NSScreen, NSScreenSaverWindowLevel, NSWindow, NSWindowStyleMaskBorderless)
from Foundation import NSDistributedNotificationCenter, NSObject
from PyObjCTools import AppHelper
import objc

import posture_guard as pg
import report
from history import History, fmt_duration

PROJECT_DIR = Path(__file__).resolve().parent
APP_PATH = Path.home() / "Applications" / "Posture Guard.app"
AGENT_LABEL = "com.postureguard.menubar"
AGENT_PATH = Path.home() / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist"
LOG_PATH = Path.home() / "Library" / "Logs" / "PostureGuard.log"
PREVIEW_WINDOW = "Posture Guard"

ICONS = {"starting": "⚪", "calibrating": "🟡", "good": "🟢", "bad": "🔴",
         "away": "⚪", "paused": "⏸", "no_camera": "⚠️", "timer": "⏰", "moved": "🟡", "break": "🧘", "sleeping": "💤"}

SENSITIVITY = [("Strict", 0.10), ("Normal", 0.15), ("Relaxed", 0.22)]
FIRST_ALERT = [("5 seconds", 5.0), ("10 seconds", 10.0), ("20 seconds", 20.0), ("30 seconds", 30.0)]
REPEAT_ALERT = [("10 seconds", 10.0), ("15 seconds", 15.0), ("30 seconds", 30.0), ("1 minute", 60.0)]
REMINDERS = [(f"Every {m} min", float(m)) for m in (5, 10, 15, 20, 30, 45, 60)]
DISTANCES = [("Off", 0.0), ("Closer than 40 cm", 40.0), ("Closer than 45 cm", 45.0), ("Closer than 50 cm", 50.0)]
BREAKS = [("Off", 0.0), ("Every 30 min", 30.0), ("Every 45 min", 45.0), ("Every 60 min", 60.0)]

# Guided stretch: (name, instruction, seconds). About 75 seconds in total.
ROUTINE = [
    ("Stand up", "Stand up and take a step back from your desk.", 5),
    ("Chin tucks", "Pull your chin straight back (make a double chin), hold 2 seconds, release. Repeat.", 15),
    ("Shoulder rolls", "Roll your shoulders up, back and down, slowly.", 15),
    ("Chest opener", "Clasp your hands behind your back, squeeze your shoulder blades and lift your chest.", 20),
    ("Look far away", "Look at something at least 20 feet away and blink slowly.", 20),
]


def osascript_dialog(text, buttons, default, timeout):
    """Show a dialog and return the button pressed ('' if it timed out)."""
    quote = lambda t: '"' + t.replace("\\", "\\\\").replace('"', '\\"') + '"'
    script = (f"display dialog {quote(text)} with title \"Posture Guard\" with icon note "
              f"buttons {{{', '.join(quote(b) for b in buttons)}}} default button {quote(default)} "
              f"giving up after {timeout}")
    out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True).stdout
    for b in buttons:
        if f"button returned:{b}," in out + ",":
            return b
    return ""

# macOS events that mean nobody is at the Mac -> (reason, starts?). Posture Guard sleeps while any reason is active.
POWER_EVENTS = {
    "NSWorkspaceWillSleepNotification": ("system", True),
    "NSWorkspaceDidWakeNotification": ("system", False),
    "NSWorkspaceScreensDidSleepNotification": ("display", True),
    "NSWorkspaceScreensDidWakeNotification": ("display", False),
    "NSWorkspaceSessionDidResignActiveNotification": ("session", True),  # switched to another user
    "NSWorkspaceSessionDidBecomeActiveNotification": ("session", False),
    "com.apple.screenIsLocked": ("lock", True),
    "com.apple.screenIsUnlocked": ("lock", False),
}


class PowerObserver(NSObject):
    """Forwards sleep/wake/lock notifications to a Python callback (on the main thread)."""

    def initWithCallback_(self, callback):
        self = objc.super(PowerObserver, self).init()
        if self is not None:
            self.callback = callback
        return self

    def handle_(self, note):
        self.callback(str(note.name()))

    def register(self):
        workspace = AppKit.NSWorkspace.sharedWorkspace().notificationCenter()
        distributed = NSDistributedNotificationCenter.defaultCenter()
        for name in POWER_EVENTS:
            center = distributed if name.startswith("com.apple.") else workspace
            center.addObserver_selector_name_object_(self, "handle:", name, None)


class PostureGuardApp(rumps.App):
    def __init__(self):
        super().__init__("Posture Guard", title=ICONS["starting"], quit_button=None)

        cfg = pg.load_config()
        self.settings = pg.build_settings(cfg)
        if self.settings.camera is None:
            self.settings.camera = pg.pick_default_camera()
        self.camera = pg.camera_name(self.settings.camera)
        self.history = History(pg.HISTORY_FILE)
        self.guard = pg.PostureGuard(self.settings, pg.load_baseline(cfg, self.camera),
                                     on_calibrated=lambda b: pg.save_baseline(b, self.camera),
                                     history=self.history, on_moved=self.ask_recalibrate,
                                     on_alert=self.send_alert, on_break=self.offer_break)
        self.today_checked = 0.0

        self.state = "starting"
        self.status = "Starting camera…"
        self.frame = None
        self.show_preview = False
        self.stop = threading.Event()    # app is quitting
        self.switch = threading.Event()  # camera/timer mode changed; restart the worker loop
        self.routine_status = None       # set while a guided stretch is running
        self.dialog_open = False
        self.flash_windows = []
        self.sleep_reasons = set()
        self.camera_warned = False
        self.power_observer = PowerObserver.alloc().initWithCallback_(self.on_power_event)
        self.power_observer.register()

        self.status_item = rumps.MenuItem(self.status)
        self.today_item = rumps.MenuItem("Today: no data yet")
        self.streak_item = rumps.MenuItem("")
        self.pause_item = rumps.MenuItem("Pause", callback=self.toggle_pause)
        self.preview_item = rumps.MenuItem("Show Camera Preview", callback=self.toggle_preview)
        self.login_item = rumps.MenuItem("Start at Login", callback=self.toggle_login)
        self.login_item.state = AGENT_PATH.exists()
        self.timer_item = rumps.MenuItem("Timed Reminders Only (camera off)", callback=self.toggle_timer_only)
        self.timer_item.state = self.settings.timer_only
        self.eye_item = rumps.MenuItem("")

        settings = rumps.MenuItem("Settings")
        for item in (
            self.choice_menu("Sensitivity", "sensitivity", SENSITIVITY),
            self.choice_menu("First Alert After", "grace", FIRST_ALERT),
            self.choice_menu("Repeat Alert Every", "cooldown", REPEAT_ALERT),
            self.choice_menu("Stretch Breaks", "break_every", BREAKS),
            self.choice_menu("Too-Close Warning", "min_distance", DISTANCES),
            None,
            self.toggle_item("Escalating Alerts (louder, then screen flash)", "escalate"),
            self.toggle_item("Mute Alerts During Calls", "mute_in_calls"),
            self.toggle_item("Eye Care (blinks & screen distance)", "eye_care"),
        ):
            settings.add(item if item is not None else rumps.separator)

        self.menu = [
            self.status_item,
            self.eye_item,
            None,
            self.today_item,
            self.streak_item,
            rumps.MenuItem("Open Progress Report…", callback=self.open_report),
            None,
            self.pause_item,
            rumps.MenuItem("Recalibrate (sit up straight)", callback=self.recalibrate),
            self.preview_item,
            rumps.MenuItem("Stretch Now (1 min)", callback=lambda _: self.start_routine()),
            None,
            self.timer_item,
            self.choice_menu("Timed Reminder Interval", "remind_every", REMINDERS),
            None,
            settings,
            None,
            self.login_item,
            rumps.MenuItem("Quit Posture Guard", callback=self.quit),
        ]

        threading.Thread(target=self.camera_worker, daemon=True).start()
        rumps.Timer(self.refresh_menu, 0.5).start()
        rumps.Timer(self.refresh_preview, 1 / 15).start()

    # ---- settings
    def save_setting(self, key, value):
        setattr(self.settings, key, value)
        cfg = pg.load_config()
        cfg.setdefault("settings", {})[key] = value
        pg.save_config(cfg)

    def toggle_item(self, title, key):
        """A checkbox menu item bound to an on/off setting."""
        def flip(item):
            item.state = not item.state
            self.save_setting(key, bool(item.state))
        item = rumps.MenuItem(title, callback=flip)
        item.state = bool(getattr(self.settings, key))
        return item

    def choice_menu(self, title, key, options):
        """A submenu of radio-style choices that updates a setting live and remembers it."""
        menu = rumps.MenuItem(title)
        values = dict(options)

        def pick(item):
            for child in menu.values():
                child.state = child.title == item.title
            self.save_setting(key, values[item.title])

        for label, value in options:
            item = rumps.MenuItem(label, callback=pick)
            item.state = getattr(self.settings, key) == value
            menu.add(item)
        return menu

    # ---- background worker thread
    def camera_worker(self):
        def on_frame(frame, status, color, landmarks):
            self.state, self.status = self.guard.state, status
            if frame is not None:
                self.camera_warned = False
            if self.show_preview and frame is not None:
                self.frame = pg.draw_overlay(frame, status, color, landmarks,
                                             hint="Use the menu bar icon to pause or recalibrate",
                                             info=self.guard.eye_info())
            return True

        while not self.stop.is_set():
            self.switch.clear()
            if self.settings.timer_only:
                self.timer_loop()
                continue
            self.state, self.status = "starting", "Starting camera…"
            ok = pg.run_camera(self.guard, self.settings, on_frame, stop=self.switch, release_when_paused=True)
            if ok or self.switch.is_set():
                continue  # stopped on purpose: quitting or switching mode
            # Camera missing or busy (e.g. still waking up): use timed reminders and retry every 30 s.
            self.state = "no_camera"
            self.status = "Camera unavailable: timed reminders only"
            if not self.camera_warned:
                self.camera_warned = True
                pg.notify("Posture Guard can't use the camera",
                          "Check System Settings > Privacy & Security > Camera. Retrying in the background.")
            retry_at = time.time() + 30
            while not self.switch.is_set() and time.time() < retry_at:
                self.guard._timers(time.time(), tracking=False)
                self.switch.wait(5)

    def timer_loop(self):
        """Camera off: a posture reminder every N minutes (plus stretch breaks)."""
        self.guard.last_posture_reminder = time.time()
        while not self.switch.is_set():
            now = time.time()
            if self.guard.suspended:
                self.state, self.status = "sleeping", "Sleeping (Mac asleep or locked)"
            elif self.guard.paused:
                self.state, self.status = "paused", "Paused"
                self.guard.last_posture_reminder = now
            else:
                self.guard._timers(now, tracking=False)
                left = self.settings.remind_every * 60 - (now - self.guard.last_posture_reminder)
                self.state = "timer"
                self.status = f"Timed reminders: next in {max(1, round(left / 60))} min"
            self.switch.wait(2)

    def send_alert(self, title, message, sound, flash):
        """Engine alert hook (camera thread): notification + sound, and a screen flash at the top level."""
        pg.notify(title, message, sound=sound)
        if flash:
            AppHelper.callAfter(self.flash_screen)

    def flash_screen(self):
        """Pulse a soft red tint over every screen twice (main thread). Clicks pass straight through."""
        self.flash_windows = []
        for screen in NSScreen.screens():
            w = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                screen.frame(), NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False)
            w.setBackgroundColor_(NSColor.colorWithCalibratedRed_green_blue_alpha_(0.85, 0.2, 0.15, 1.0))
            w.setOpaque_(False)
            w.setAlphaValue_(0.0)
            w.setIgnoresMouseEvents_(True)
            w.setLevel_(NSScreenSaverWindowLevel)
            w.setCollectionBehavior_((1 << 0) | (1 << 8))  # all Spaces, over full-screen apps
            w.setReleasedWhenClosed_(False)
            w.orderFrontRegardless()
            self.flash_windows.append(w)

        def set_alpha(a):
            for w in self.flash_windows:
                w.setAlphaValue_(a)

        for i, alpha in enumerate((0.22, 0.0, 0.22, 0.0)):  # two slow pulses (well under 3 flashes/s)
            AppHelper.callLater(0.4 * i, set_alpha, alpha)
        AppHelper.callLater(1.8, lambda: [w.orderOut_(None) for w in self.flash_windows])

    def offer_break(self):
        """Engine hook when a stretch break is due: ask, then run the guided routine."""
        if self.dialog_open or self.routine_status:
            return

        def ask():
            self.dialog_open = True
            try:
                subprocess.Popen(["afplay", "/System/Library/Sounds/Hero.aiff"])
                choice = osascript_dialog(
                    f"You've been sitting for {self.settings.break_every:g} minutes. "
                    "Time for a 1-minute stretch: it resets your posture and rests your eyes.",
                    ["Skip", "Snooze 10 min", "Start Stretch"], "Start Stretch", 300)
            finally:
                self.dialog_open = False
            if choice == "Start Stretch":
                self.start_routine()
            elif choice == "Snooze 10 min":
                self.guard.sitting_since = time.time() - (self.settings.break_every - 10) * 60
        threading.Thread(target=ask, daemon=True).start()

    def start_routine(self):
        if self.routine_status:
            return

        def run():
            total = sum(s for *_, s in ROUTINE)
            self.guard.start_break(total + 10)  # a little extra to sit back down
            try:
                for i, (name, text, seconds) in enumerate(ROUTINE, 1):
                    pg.notify(f"Stretch {i}/{len(ROUTINE)}: {name}", text, sound="Glass")
                    end = time.time() + seconds
                    while time.time() < end and not self.stop.is_set():
                        self.routine_status = f"Stretch {i}/{len(ROUTINE)}: {name} ({max(1, math.ceil(end - time.time()))}s)"
                        time.sleep(0.5)
                    if self.stop.is_set():
                        return
                self.routine_status = "Stretch done: sit back down"
                time.sleep(8)
            finally:
                self.routine_status = None
                self.guard.end_break()
            if self.guard.stood_up is False:
                message = "Nice. Next time try standing up for it, it does more for your back."
            else:
                message = "Nice work. Your back and eyes thank you."
            pg.notify("Stretch break done", message, sound="Hero")
        threading.Thread(target=run, daemon=True).start()

    def on_power_event(self, name):
        """Mac going to sleep / locking: stop the camera and all reminders. Back again: resume fresh."""
        reason, starting = POWER_EVENTS.get(name, (None, None))
        if reason is None:
            return
        if starting:
            self.sleep_reasons.add(reason)
        else:
            self.sleep_reasons.discard(reason)
            if reason == "system":
                self.sleep_reasons.discard("display")  # the display wakes with the system
        was_suspended = self.guard.suspended
        if self.sleep_reasons and not was_suspended:
            self.guard.suspended = True
            print(f"[{time.strftime('%H:%M:%S')}] Sleeping ({', '.join(sorted(self.sleep_reasons))})", flush=True)
        elif not self.sleep_reasons and was_suspended:
            self.guard.wake()
            print(f"[{time.strftime('%H:%M:%S')}] Awake again, resuming", flush=True)

    def ask_recalibrate(self):
        """Engine hook (camera thread) when your sitting position looks different from calibration."""
        def ask():
            subprocess.Popen(["afplay", "/System/Library/Sounds/Glass.aiff"])
            choice = osascript_dialog("It looks like you moved your chair or laptop.\n\n"
                                      "Sit up straight, then click Recalibrate.",
                                      ["Keep Current", "Recalibrate"], "Recalibrate", 120)
            if choice == "Recalibrate":
                self.guard.start_calibration()
            else:
                self.guard.dismiss_moved()
        threading.Thread(target=ask, daemon=True).start()

    # ---- main-thread UI updates
    def refresh_menu(self, _):
        if self.routine_status:
            self.title, status = ICONS["break"], self.routine_status
        else:
            self.title, status = ICONS.get(self.state, "⚪"), self.status
        if self.guard.in_call and self.settings.mute_in_calls:
            status += "  ·  🔇 alerts muted (on a call)"
        self.status_item.title = status
        info = self.guard.eye_info() if not self.settings.timer_only else ""
        self.eye_item.title = f"👁 {info}" if info else ("👁 Eye care: looking for your face…"
                                                         if self.settings.eye_care and not self.settings.timer_only
                                                         else "")
        self.pause_item.title = "Resume" if self.guard.paused else "Pause"
        if time.time() - self.today_checked >= 10:
            self.today_checked = time.time()
            t = self.history.today()
            if t["percent"] is None:
                self.today_item.title, self.streak_item.title = "Today: no data yet", ""
            else:
                self.today_item.title = (f"Today: {t['percent']:.0f}% good posture · "
                                         f"{fmt_duration(t['good'] + t['bad'])} tracked")
                self.streak_item.title = f"Best streak {t['streak']} min · {t['alerts']} alert(s)"

    def refresh_preview(self, _):
        if not self.show_preview:
            return
        if self.frame is not None:
            cv2.imshow(PREVIEW_WINDOW, self.frame)
        cv2.waitKey(1)
        try:  # the user closed the window with its red button
            if self.frame is not None and cv2.getWindowProperty(PREVIEW_WINDOW, cv2.WND_PROP_VISIBLE) < 1:
                self.toggle_preview(self.preview_item)
        except cv2.error:
            pass

    # ---- menu actions
    def open_report(self, _):
        report.open_report(self.history, pg.CONFIG_DIR)

    def toggle_pause(self, _):
        self.guard.paused = not self.guard.paused

    def recalibrate(self, _):
        if self.settings.timer_only:
            pg.notify("Posture Guard", "Turn off Timed Reminders Only to use the camera.")
            return
        self.guard.paused = False
        self.guard.start_calibration()

    def toggle_timer_only(self, item):
        item.state = self.settings.timer_only = not item.state
        cfg = pg.load_config()
        cfg.setdefault("settings", {})["timer_only"] = self.settings.timer_only
        pg.save_config(cfg)
        if self.settings.timer_only:
            if self.show_preview:
                self.toggle_preview(self.preview_item)
            pg.notify("Timed reminders only",
                      f"Camera off. You'll get a posture reminder every {self.settings.remind_every:g} min.", sound="Tink")
        else:
            pg.notify("Posture Guard", "Camera on. Watching your posture again.", sound="Tink")
        self.switch.set()

    def toggle_preview(self, item):
        self.show_preview = item.state = not item.state
        if not self.show_preview:
            self.frame = None
            cv2.destroyWindow(PREVIEW_WINDOW)
            cv2.waitKey(1)

    def toggle_login(self, item):
        if item.state:
            AGENT_PATH.unlink(missing_ok=True)
        else:
            if not APP_PATH.exists():
                build_app()
            install_login_agent()
        item.state = AGENT_PATH.exists()

    def quit(self, _):
        self.stop.set()
        self.switch.set()
        cv2.destroyAllWindows()
        time.sleep(0.3)  # let the camera thread finish its current frame
        self.history.close()
        rumps.quit_application()


# ---------------------------------------------------------------- app bundle + login

def build_app():
    """Create a tiny 'Posture Guard.app' launcher.

    Launching through a real .app (instead of Terminal) means macOS asks for camera permission
    for "Posture Guard" itself, and the app can be started at login or from Spotlight.
    """
    command = (f"cd {shlex.quote(str(PROJECT_DIR))} && "
               f"exec env PYTHONUNBUFFERED=1 {shlex.quote(str(sys.executable))} menubar.py "
               f">> {shlex.quote(str(LOG_PATH))} 2>&1")
    script = f'do shell script "{command.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"'

    if APP_PATH.exists():
        shutil.rmtree(APP_PATH)
    APP_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["osacompile", "-o", str(APP_PATH), "-e", script], check=True)

    info_path = APP_PATH / "Contents" / "Info.plist"
    with open(info_path, "rb") as f:
        info = plistlib.load(f)
    info.update({
        "CFBundleName": "Posture Guard",
        "CFBundleIdentifier": "com.postureguard.app",
        "LSUIElement": True,  # no Dock icon; the menu bar icon is the UI
        "NSCameraUsageDescription": "Posture Guard watches your posture locally to remind you to sit up straight.",
    })
    with open(info_path, "wb") as f:
        plistlib.dump(info, f)
    # Editing Info.plist invalidates the signature; re-sign ad hoc so macOS will run it.
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(APP_PATH)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"Built {APP_PATH}")


def install_login_agent():
    AGENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(AGENT_PATH, "wb") as f:
        plistlib.dump({
            "Label": AGENT_LABEL,
            "ProgramArguments": ["/usr/bin/open", "-a", str(APP_PATH)],
            "RunAtLoad": True,
        }, f)


def uninstall():
    AGENT_PATH.unlink(missing_ok=True)
    if APP_PATH.exists():
        shutil.rmtree(APP_PATH)
    print("Removed Posture Guard.app and the start-at-login entry. Your saved settings are in "
          f"{pg.CONFIG_DIR} if you want to delete them too.")


# ---------------------------------------------------------------- entry point

def main():
    p = argparse.ArgumentParser(description="Posture Guard menu bar app.")
    p.add_argument("--install", action="store_true", help="build ~/Applications/Posture Guard.app")
    p.add_argument("--uninstall", action="store_true", help="remove the app and start-at-login entry")
    args = p.parse_args()

    if args.install:
        build_app()
        print("Open it from Spotlight (Cmd+Space, 'Posture Guard') or Finder > Applications.")
        return
    if args.uninstall:
        uninstall()
        return

    # Only one copy at a time.
    pg.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(pg.CONFIG_DIR / "menubar.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        pg.notify("Posture Guard", "Already running. Look for the icon in your menu bar.")
        return

    NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    PostureGuardApp().run()


if __name__ == "__main__":
    main()
