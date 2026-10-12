"""Close CPU-hungry apps (e.g. the Synology Surveillance Station client) while scanning, reopen them when idle.

On a slow PC such a viewer can make a scan take twice as long. `pause()` closes the configured apps when a scan
starts, `resume()` starts them again once the scanner has been idle. An app is only reopened if it was running
(and was closed by us): an app you had closed yourself is never launched.

Config entry (config.json "PAUSE_APPS"), one object per app:
  {"exe": "C:\\...\\Synology Surveillance Station Client.exe",
   "processes": ["Synology Surveillance Station Client.exe", "NativeUI.exe"]}
`processes` are the image names to close (defaults to the exe's file name); `exe` is what gets started again.
Windows only (taskkill / tasklist).
"""
import ntpath
import subprocess
import threading
import time

GRACE = 8  # seconds an app gets to close by itself before it is force-closed


class Pauser:
    def __init__(self, apps):
        self.apps = [self._normalize(a) for a in apps]
        self.closed = []  # apps we closed and still owe a relaunch
        self.lock = threading.Lock()

    @staticmethod
    def _normalize(app):
        if not isinstance(app, dict) or not app.get("exe"):
            raise ValueError(f"PAUSE_APPS entries need an 'exe', got {app!r}")
        exe = app["exe"]
        return {"exe": exe, "processes": list(app.get("processes") or [ntpath.basename(exe)])}

    # --- Windows primitives (overridden in tests) ----------------------------------------------
    def _snapshot(self):
        """Lower-cased image names of every running process (one tasklist call, which is slow on a busy PC)."""
        out = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True).stdout
        return {line.split('","')[0].strip('"').lower() for line in out.splitlines() if line.strip()}

    def _ask_to_close(self, image):
        """Send the close request a click on X would (no /f). False if Windows says the process has no window to ask
        ("can only be terminated forcefully"), in which case waiting for it to close itself is pointless."""
        return subprocess.run(["taskkill", "/im", image], capture_output=True).returncode == 0

    def _force_close(self, image):
        subprocess.run(["taskkill", "/f", "/im", image], capture_output=True)

    def _launch(self, exe):
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen([exe], cwd=ntpath.dirname(exe) or None, creationflags=flags,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # --- public API ----------------------------------------------------------------------------
    def pause(self, on_close=None):
        """Close every configured app that is running. on_close() is called once, before the first close, so the
        caller can tell the user why the scan is delayed. Returns the names of the apps closed."""
        names = []
        with self.lock:
            for app in self.apps:
                alive = self._snapshot()
                running = [p for p in app["processes"] if p.lower() in alive]
                if not running:
                    continue
                if on_close and not names:
                    on_close()
                asked = [image for image in running if self._ask_to_close(image)]
                for image in running:
                    if image not in asked:
                        self._force_close(image)  # nothing to ask politely
                deadline = time.monotonic() + GRACE
                while asked and time.monotonic() < deadline:
                    alive = self._snapshot()
                    asked = [image for image in asked if image.lower() in alive]
                    time.sleep(0.3)
                for image in asked:  # ignored the request for GRACE seconds
                    self._force_close(image)
                if app not in self.closed:
                    self.closed.append(app)
                names.append(ntpath.basename(app["exe"]))
        return names

    def resume(self):
        """Start again the apps pause() closed. Returns their names."""
        names = []
        with self.lock:
            for app in list(self.closed):
                self._launch(app["exe"])
                self.closed.remove(app)
                names.append(ntpath.basename(app["exe"]))
        return names

    def waiting_to_resume(self):
        return bool(self.closed)
