#!/usr/bin/env python3
"""Posture Guard menu bar app: posture detection runs in the background behind a menu bar icon.

    python menubar.py              run the menu bar app
    python menubar.py --install    build ~/Applications/Posture Guard.app (double-click to launch)
    python menubar.py --uninstall  remove the app and the start-at-login entry
"""

import argparse
import fcntl
import plistlib
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import cv2
import rumps
from AppKit import NSApplication, NSApplicationActivationPolicyAccessory

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
         "away": "⚪", "paused": "⏸", "no_camera": "⚠️", "timer": "⏰", "moved": "🟡"}

SENSITIVITY = [("Strict", 0.10), ("Normal", 0.15), ("Relaxed", 0.22)]
FIRST_ALERT = [("5 seconds", 5.0), ("10 seconds", 10.0), ("20 seconds", 20.0), ("30 seconds", 30.0)]
REPEAT_ALERT = [("10 seconds", 10.0), ("15 seconds", 15.0), ("30 seconds", 30.0), ("1 minute", 60.0)]
REMINDERS = [(f"Every {m} min", float(m)) for m in (5, 10, 15, 20, 30, 45, 60)]
BREAKS = [("Off", 0.0), ("Every 30 min", 30.0), ("Every 45 min", 45.0), ("Every 60 min", 60.0)]


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
                                     history=self.history, on_moved=self.ask_recalibrate)
        self.today_checked = 0.0

        self.state = "starting"
        self.status = "Starting camera…"
        self.frame = None
        self.show_preview = False
        self.stop = threading.Event()    # app is quitting
        self.switch = threading.Event()  # camera/timer mode changed; restart the worker loop

        self.status_item = rumps.MenuItem(self.status)
        self.today_item = rumps.MenuItem("Today: no data yet")
        self.streak_item = rumps.MenuItem("")
        self.pause_item = rumps.MenuItem("Pause", callback=self.toggle_pause)
        self.preview_item = rumps.MenuItem("Show Camera Preview", callback=self.toggle_preview)
        self.login_item = rumps.MenuItem("Start at Login", callback=self.toggle_login)
        self.login_item.state = AGENT_PATH.exists()
        self.timer_item = rumps.MenuItem("Timed Reminders Only (camera off)", callback=self.toggle_timer_only)
        self.timer_item.state = self.settings.timer_only

        self.menu = [
            self.status_item,
            None,
            self.today_item,
            self.streak_item,
            rumps.MenuItem("Open Progress Report…", callback=self.open_report),
            None,
            self.pause_item,
            rumps.MenuItem("Recalibrate (sit up straight)", callback=self.recalibrate),
            self.preview_item,
            None,
            self.timer_item,
            self.choice_menu("Timed Reminder Interval", "remind_every", REMINDERS),
            None,
            self.choice_menu("Sensitivity", "sensitivity", SENSITIVITY),
            self.choice_menu("First Alert After", "grace", FIRST_ALERT),
            self.choice_menu("Repeat Alert Every", "cooldown", REPEAT_ALERT),
            self.choice_menu("Stretch Breaks", "break_every", BREAKS),
            None,
            self.login_item,
            rumps.MenuItem("Quit Posture Guard", callback=self.quit),
        ]

        threading.Thread(target=self.camera_worker, daemon=True).start()
        rumps.Timer(self.refresh_menu, 0.5).start()
        rumps.Timer(self.refresh_preview, 1 / 15).start()

    # ---- settings
    def choice_menu(self, title, key, options):
        """A submenu of radio-style choices that updates a setting live and remembers it."""
        menu = rumps.MenuItem(title)
        values = dict(options)

        def pick(item):
            setattr(self.settings, key, values[item.title])
            for child in menu.values():
                child.state = child.title == item.title
            cfg = pg.load_config()
            cfg.setdefault("settings", {})[key] = values[item.title]
            pg.save_config(cfg)

        for label, value in options:
            item = rumps.MenuItem(label, callback=pick)
            item.state = getattr(self.settings, key) == value
            menu.add(item)
        return menu

    # ---- background worker thread
    def camera_worker(self):
        def on_frame(frame, status, color, landmarks):
            self.state, self.status = self.guard.state, status
            if self.show_preview and frame is not None:
                self.frame = pg.draw_overlay(frame, status, color, landmarks,
                                             hint="Use the menu bar icon to pause or recalibrate")
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
            self.state = "no_camera"
            self.status = "Camera unavailable: timed reminders only"
            pg.notify("Posture Guard can't use the camera",
                      "Allow it in System Settings > Privacy & Security > Camera, then restart Posture Guard.")
            while not self.switch.is_set():
                self.guard._timers(time.time(), tracking=False)
                self.switch.wait(5)

    def timer_loop(self):
        """Camera off: a posture reminder every N minutes (plus stretch breaks)."""
        self.guard.last_posture_reminder = time.time()
        while not self.switch.is_set():
            now = time.time()
            if self.guard.paused:
                self.state, self.status = "paused", "Paused"
                self.guard.last_posture_reminder = now
            else:
                self.guard._timers(now, tracking=False)
                left = self.settings.remind_every * 60 - (now - self.guard.last_posture_reminder)
                self.state = "timer"
                self.status = f"Timed reminders: next in {max(1, round(left / 60))} min"
            self.switch.wait(2)

    def ask_recalibrate(self):
        """Called from the camera thread when your sitting position looks different."""
        def ask():
            subprocess.Popen(["afplay", "/System/Library/Sounds/Glass.aiff"])
            script = ('display dialog "It looks like you moved your chair or laptop.\n\n'
                      'Sit up straight, then click Recalibrate." with title "Posture Guard" with icon note '
                      'buttons {"Keep Current", "Recalibrate"} default button "Recalibrate" giving up after 120')
            result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
            if "button returned:Recalibrate" in result.stdout:
                self.guard.start_calibration()
            else:
                self.guard.dismiss_moved()
        threading.Thread(target=ask, daemon=True).start()

    # ---- main-thread UI updates
    def refresh_menu(self, _):
        self.title = ICONS.get(self.state, "⚪")
        self.status_item.title = self.status
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
