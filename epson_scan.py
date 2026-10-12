"""Scan through Epson Scan 2's own window instead of NAPS2/WIA (Windows only, stdlib ctypes).

Epson's driver is much faster than WIA on the L3110 (about 11 s instead of 25 s for 150 dpi colour), but
Epson Scan 2 has no scanning command line. So this opens its window (`es2launcher.exe SCAN`), sets Image Type,
Resolution, Image Format and Folder in the dropdowns, clicks Scan, and picks up the JPEG it saves.
The window stays open between scans and is closed after IDLE_CLOSE seconds. It is never killed: killing
Epson's helper process left the scanner unreachable until it was power-cycled.
"""
import glob
import os
import shutil
import subprocess
import threading
import time

IDLE_CLOSE = 120  # seconds without a scan before the window is closed again
READY_TIMEOUT = 45  # how long to keep re-clicking Scan while the scanner is still busy after the previous page
LAUNCH_TIMEOUT = 40
EXPLORER_WATCH = 20  # seconds after a scan during which a File Explorer window that Epson opened is closed again
MODES = {"color": "Color", "gray": "Grayscale", "bw": "Black & White"}
EXE_CANDIDATES = [
    r"C:\Program Files (x86)\epson\Epson Scan 2\Core\es2launcher.exe",
    r"C:\Program Files\epson\Epson Scan 2\Core\es2launcher.exe",
]
TITLE = "Epson Scan 2"

BM_CLICK, WM_COMMAND, WM_SYSCOMMAND, SC_CLOSE = 0x00F5, 0x0111, 0x0112, 0xF060
CB_GETCOUNT, CB_GETCURSEL, CB_GETLBTEXT, CB_SETCURSEL, CBN_SELCHANGE = 0x146, 0x147, 0x148, 0x14E, 1


class EpsonError(Exception):
    """Epson Scan 2 could not be driven; the caller can fall back to another engine."""


def find_exe(configured=""):
    for path in ([configured] if configured else []) + EXE_CANDIDATES:
        if path and os.path.exists(path):
            return path
    raise EpsonError("Epson Scan 2 (es2launcher.exe) is not installed")


def documents_folder():
    """The user's Documents folder (the folder Epson Scan 2 saves to when 'Documents' is selected)."""
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD), ("d", ctypes.c_ubyte * 8)]

    guid = GUID(0xFDD39AD0, 0x238F, 0x46AF, (ctypes.c_ubyte * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7))
    out = ctypes.c_wchar_p()
    try:
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out)) == 0:
            return out.value
    finally:
        ctypes.windll.ole32.CoTaskMemFree(out)
    return os.path.join(os.path.expanduser("~"), "Documents")


class Win:
    """Small ctypes wrapper over the Win32 calls needed to find and drive the window."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ct, self.wt = ctypes, wintypes
        u = self.u = ctypes.windll.user32
        u.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.SendMessageW.restype = ctypes.c_ssize_t
        u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.enum_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def text(self, h):
        n = self.u.GetWindowTextLengthW(h)
        buf = self.ct.create_unicode_buffer(n + 1)
        self.u.GetWindowTextW(h, buf, n + 1)
        return buf.value

    def cls(self, h):
        buf = self.ct.create_unicode_buffer(256)
        self.u.GetClassNameW(h, buf, 256)
        return buf.value

    def _collect(self, enum_fn, *args):
        found = []
        enum_fn(*args, self.enum_type(lambda h, _: (found.append(h), True)[1]), 0)
        return found

    def tops(self):
        return self._collect(self.u.EnumWindows)

    def kids(self, h):
        return self._collect(self.u.EnumChildWindows, h)

    def items(self, combo):
        out = []
        for i in range(max(self.u.SendMessageW(combo, CB_GETCOUNT, 0, 0), 0)):
            buf = self.ct.create_unicode_buffer(256)
            self.u.SendMessageW(combo, CB_GETLBTEXT, i, self.ct.addressof(buf))
            out.append(buf.value)
        return out

    def selected(self, combo):
        sel = self.u.SendMessageW(combo, CB_GETCURSEL, 0, 0)
        items = self.items(combo)
        return items[sel] if 0 <= sel < len(items) else None

    def select(self, combo, value):
        items = self.items(combo)
        if value not in items:
            raise EpsonError(f"Epson Scan 2 has no option '{value}' (it offers {', '.join(i for i in items if i != '-')})")
        self.u.SendMessageW(combo, CB_SETCURSEL, items.index(value), 0)
        # MFC only reacts to the notification its parent dialog gets, not to CB_SETCURSEL alone.
        self.u.SendMessageW(self.u.GetParent(combo), WM_COMMAND,
                            (CBN_SELCHANGE << 16) | self.u.GetDlgCtrlID(combo), combo)

    def click(self, button):
        self.u.PostMessageW(button, BM_CLICK, 0, 0)

    def close(self, window):
        self.u.PostMessageW(window, WM_SYSCOMMAND, SC_CLOSE, 0)


class Session:
    """One reusable Epson Scan 2 window. All scanning happens under `lock`, one scan at a time."""

    def __init__(self, exe="", folder=None):
        self.exe_setting = exe
        self.folder = folder
        self.lock = threading.RLock()
        self.proc = None
        self.last_used = 0.0
        self.win = None
        self._closer = None

    # --- window lookup -------------------------------------------------------------------------
    def _w(self):
        if os.name != "nt":
            raise EpsonError("the Epson Scan 2 engine only works on Windows")
        if self.win is None:
            self.win = Win()
        return self.win

    def _main(self):
        """The real scanning window: visible, titled 'Epson Scan 2', with a Scan button (the start-up
        splash with the same title has none)."""
        w = self._w()
        for h in w.tops():
            if w.u.IsWindowVisible(h) and w.text(h) == TITLE:
                for c in w.kids(h):
                    if w.cls(c) == "Button" and w.text(c) == "Scan":
                        return h, c
        return None, None

    def _error_dialog(self):
        """Epson's modal error box ('Unable to communicate with the scanner…'): dismiss it, return its text."""
        w = self._w()
        for h in w.tops():
            if w.u.IsWindowVisible(h) and w.text(h) == TITLE:
                kids = [(w.cls(c), w.text(c), c) for c in w.kids(h)]
                no = [c for k, t, c in kids if k == "Button" and t == "No"]
                if no and any(k == "Button" and t == "Yes" for k, t, c in kids):
                    msg = " ".join(t for k, t, c in kids if k == "Static" and t)
                    w.click(no[0])
                    return msg or "Epson Scan 2 reported an error"
        return None

    def active(self):
        """True while an Epson Scan 2 window is open (the scanner is then in use)."""
        try:
            return self._main()[0] is not None
        except Exception:
            return False

    # --- lifecycle -----------------------------------------------------------------------------
    def _ensure_open(self, progress=lambda phase: None):
        main, button = self._main()
        if main:
            return main
        exe = find_exe(self.exe_setting)
        progress("opening")
        self.proc = subprocess.Popen([exe, "SCAN"])
        deadline = time.monotonic() + LAUNCH_TIMEOUT
        while time.monotonic() < deadline:
            main, button = self._main()
            if main:
                return main
            err = self._error_dialog()
            if err:
                raise EpsonError(err)
            time.sleep(0.1)
        raise EpsonError("Epson Scan 2 did not open its scan window")

    def close(self):
        """Ask the window to close (never kill it), and wait briefly for it to go away."""
        with self.lock:
            w = self._w()
            for h in w.tops():
                if w.u.IsWindowVisible(h) and w.text(h) == TITLE:
                    w.close(h)
            for _ in range(100):
                if self._main()[0] is None:
                    break
                time.sleep(0.1)

    def _start_closer(self):
        if self._closer and self._closer.is_alive():
            return

        def watch():
            while True:
                time.sleep(5)
                if not self.lock.acquire(blocking=False):
                    continue  # a scan is running
                try:
                    if self._main()[0] is None:
                        return
                    if time.monotonic() - self.last_used > IDLE_CLOSE:
                        self.close()
                        return
                finally:
                    self.lock.release()

        self._closer = threading.Thread(target=watch, daemon=True)
        self._closer.start()

    # --- scanning ------------------------------------------------------------------------------
    def scan(self, dpi, mode, out_path, timeout=120, progress=None):
        """Scan one page from the glass and move the resulting JPEG to out_path.
        progress(phase) is called with 'opening' (Epson's window is starting), 'waiting' (the scanner is still busy
        after the last page) and 'scanning' (Epson accepted the click: only this phase is the real scan time)."""
        if mode not in MODES:
            raise EpsonError(f"unsupported mode {mode}")
        with self.lock:
            try:
                return self._scan(str(dpi), MODES[mode], str(out_path), timeout, progress or (lambda phase: None))
            finally:
                self.last_used = time.monotonic()
                self._start_closer()

    def _combos(self, main):
        w, out, label = self._w(), {}, ""
        for c in w.kids(main):
            if w.cls(c) == "Static" and w.text(c) and w.u.IsWindowVisible(c):
                label = w.text(c).strip(" :")
            if w.cls(c) == "ComboBox" and w.u.IsWindowVisible(c):
                out[label] = c
        return out

    def _scan(self, dpi, image_type, out_path, timeout, progress):
        w = self._w()
        folder = self.folder or documents_folder()
        main = self._ensure_open(progress)
        # Wait for the Scan button to exist and be enabled (right after launch it may not be yet).
        deadline = time.monotonic() + 30
        while True:
            main, button = self._main()
            if button and w.u.IsWindowEnabled(button):
                break
            if time.monotonic() > deadline or not main:
                raise EpsonError("Epson Scan 2 is busy or closed")
            time.sleep(0.2)
        combos = self._combos(main)
        wanted = {"Image Type": image_type, "Resolution": dpi, "Image Format": "JPEG", "Folder": "Documents"}
        for label, value in wanted.items():
            if label not in combos:
                raise EpsonError(f"Epson Scan 2 window has no '{label}' setting (different version?)")
            if w.selected(combos[label]) != value:
                w.select(combos[label], value)
                time.sleep(0.3)
        seen = set(glob.glob(os.path.join(folder, "*")))
        explorers = self._explorer_windows()
        started = self._click_until_accepted(button, progress)
        wait = min(timeout, 50 if int(dpi) <= 300 else timeout)  # 300 dpi colour takes ~35 s
        if self._wait_for_file(folder, seen, started + wait, out_path):
            threading.Thread(target=self._close_new_explorers, args=(explorers, os.path.basename(documents_folder())),
                             daemon=True).start()
            return round(time.monotonic() - started, 1)
        raise EpsonError("Epson Scan 2 did not produce a scan (the scanner may be busy)")

    def _explorer_windows(self):
        w = self._w()
        return {h for h in w.tops() if w.u.IsWindowVisible(h) and w.cls(h) == "CabinetWClass"}

    def _close_new_explorers(self, before, title):
        """Epson Scan 2 has no setting to stop it opening the destination folder in File Explorer after each scan.
        Close the window it opened (a new Explorer window titled like the folder). A window that was already open
        is reused by Explorer, so one the user opened themselves is never closed."""
        try:
            w, before = self._w(), set(before)
            deadline = time.monotonic() + EXPLORER_WATCH
            while time.monotonic() < deadline:
                for h in self._explorer_windows() - before:
                    if w.text(h) == title:  # a new window is first titled "File Explorer": keep checking until it names the folder
                        w.close(h)
                        before.add(h)
                time.sleep(0.3)
        except Exception:
            pass  # cosmetic: never let this affect a scan

    def _scan_started(self, main):
        """Epson greys out its window and shows a 'Progress' box as soon as it accepts a click on Scan."""
        w = self._w()
        if not w.u.IsWindowEnabled(main):
            return True
        return any(w.u.IsWindowVisible(h) and w.text(h) == "Progress" for h in w.tops())

    def _click_until_accepted(self, button, progress):
        """Right after a page the scanner is still busy (head returning) and Epson silently drops clicks on Scan.
        A dropped click is never queued, so clicking again until the window reacts cannot start two scans."""
        w = self._w()
        deadline = time.monotonic() + READY_TIMEOUT
        told_waiting = False
        while True:
            main, fresh_button = self._main()
            if not main:
                raise EpsonError("Epson Scan 2 window closed before the scan")
            started = time.monotonic()
            w.click(fresh_button or button)
            while time.monotonic() - started < 2:
                if self._scan_started(main):
                    progress("scanning")
                    return started
                time.sleep(0.05)
            err = self._error_dialog()
            if err:
                raise EpsonError(err)
            if time.monotonic() > deadline:
                raise EpsonError("Epson Scan 2 ignored the Scan button (the scanner may be busy)")
            if not told_waiting:
                told_waiting = True
                progress("waiting")
            time.sleep(1)

    def _wait_for_file(self, folder, seen, deadline, out_path):
        """Wait for Epson to save the JPEG, move it to out_path. None if nothing appears before the deadline."""
        while time.monotonic() < deadline:
            fresh = [f for f in glob.glob(os.path.join(folder, "*")) if f not in seen
                     and f.lower().endswith((".jpg", ".jpeg"))]
            if fresh:
                f = fresh[0]
                size = -1
                while size != os.path.getsize(f):  # wait until Epson has finished writing it
                    size = os.path.getsize(f)
                    time.sleep(0.3)
                if size > 0:
                    shutil.move(f, out_path)
                    return True
            err = self._error_dialog()
            if err:
                raise EpsonError(err)
            if self._main()[0] is None:
                raise EpsonError("Epson Scan 2 window closed during the scan")
            time.sleep(0.2)
        return None
