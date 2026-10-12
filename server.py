"""Tiny web wrapper around the NAPS2 console CLI. Standard library only."""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import select
import shlex
import shutil
import struct
import subprocess
import tempfile
import threading
import traceback
import time
import uuid
from html import escape
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import epson_scan
import pause_apps

HERE = Path(__file__).resolve().parent

DEFAULTS = {
    "NAPS2_PATH": r"C:\Program Files\NAPS2\NAPS2.Console.exe",
    "SCANNER_DRIVER": "wia",  # wia | twain | sane | escl | apple
    "SCANNER_DEVICE": "",  # exact name from /devices; empty = NAPS2 picks
    "SCAN_ENGINE": "wia",  # wia = NAPS2 | epson = drive Epson Scan 2's window (faster on Epson scanners, Windows only)
    "AUTH_USER": "",  # set both AUTH_USER and AUTH_PASSWORD to require a login; empty = no login
    "AUTH_PASSWORD": "",
    "AUTH_SESSION_HOURS": 12,  # how long a login lasts
    "PAUSE_APPS": [],  # apps closed while scanning and reopened when idle, see pause_apps.py
    "PAUSE_RESUME_AFTER": 120,  # seconds without a scan before PAUSE_APPS are reopened
    "EPSON_SCAN2_PATH": "",  # es2launcher.exe; empty = look in the usual Program Files folders
    "PORT": 8080,
    "HOST": "0.0.0.0",
    "TIMEOUT": 120,
    "SAVE_DIR": str(HERE / "scans"),  # where finished PDFs are written
    # Test overrides. {out} is the output file, {inputs} the space-separated page files.
    "SCAN_CMD": "",  # e.g. "python3 stub_scan.py scan {out}"
    "MERGE_CMD": "",  # e.g. "python3 stub_scan.py merge {out} {inputs}"
    "OPEN_CMD": "",  # e.g. "python3 stub_scan.py split {in} {outdir}"
}
ALLOWED_DPI = {"100", "150", "200", "300", "600"}
ALLOWED_MODES = {"color", "gray", "bw"}
ID_RE = re.compile(r"^[0-9a-f]{32}$")


def load_config():
    cfg = dict(DEFAULTS)
    path = HERE / "config.json"
    if path.exists():
        cfg.update(json.loads(path.read_text(encoding="utf-8")))
    for key in DEFAULTS:
        if key in os.environ:
            cfg[key] = json.loads(os.environ[key]) if key == "PAUSE_APPS" else os.environ[key]
    cfg["PAUSE_RESUME_AFTER"] = int(cfg["PAUSE_RESUME_AFTER"])
    cfg["AUTH_SESSION_HOURS"] = float(cfg["AUTH_SESSION_HOURS"])
    if bool(cfg["AUTH_USER"]) != bool(cfg["AUTH_PASSWORD"]):
        raise ValueError("AUTH_USER and AUTH_PASSWORD must both be set (or both empty) in config.json")
    cfg["PORT"] = int(cfg["PORT"])
    cfg["TIMEOUT"] = int(cfg["TIMEOUT"])
    return cfg


CFG = load_config()
SAVE_DIR = Path(CFG["SAVE_DIR"])
PAGES_DIR = Path(tempfile.mkdtemp(prefix="scan-pages-"))
STATIC_DIR = (HERE / "static").resolve()
STATIC_TYPES = {".js": "text/javascript", ".css": "text/css", ".woff2": "font/woff2"}
scan_lock = threading.Lock()
COLD_AFTER = 180  # seconds idle after which the next scan is slower (scanner warm-up)
last_scan_end = None
scan_jobs = {}  # job id -> {'state': running|done|error, ...}; polled by the page
_epson = None
pauser = pause_apps.Pauser(CFG["PAUSE_APPS"]) if CFG["PAUSE_APPS"] else None


def epson_session():
    global _epson
    if _epson is None:
        _epson = epson_scan.Session(CFG["EPSON_SCAN2_PATH"])
    return _epson


def use_epson():
    return CFG["SCAN_ENGINE"].lower() == "epson" and not CFG["SCAN_CMD"]


def naps2_base():
    return [CFG["NAPS2_PATH"], "--noprofile"]


known_devices = []  # last list NAPS2 reported; scan requests may only pick from it


def list_devices():
    global known_devices
    if CFG["SCAN_CMD"]:
        time.sleep(float(os.environ.get("STUB_DEVICE_DELAY", 0)))  # test hook: simulate NAPS2's slow search
        known_devices = ["(stub scanner)"]
        return known_devices
    result = subprocess.run(
        naps2_base() + ["--driver", CFG["SCANNER_DRIVER"], "--listdevices"],
        capture_output=True, text=True, timeout=90,
    )
    known_devices = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return known_devices


dev_lock = threading.Lock()  # only one NAPS2 --listdevices at a time
dev_state = {"devices": [], "updated": None, "refreshing": False, "error": None}
DEV_STALE = 30  # seconds before a page load triggers a quiet background refresh


def devices_message():
    return {"type": "devices", **dev_state, "engine": "epson" if use_epson() else "wia"}


def refresh_devices(force=False):
    """Start a background device search unless one is running or the cache is fresh. Progress is pushed to the pages.
    Returns why nothing was started when a refresh was skipped because it would disturb a scan, else None."""
    fresh = dev_state["updated"] and time.time() - dev_state["updated"] < DEV_STALE
    if scan_lock.locked() or (use_epson() and _epson is not None and _epson.active()):
        # NAPS2 listing the scanner while it scans (or while Epson's window holds it) breaks the scan
        return "a scan is in progress or Epson Scan 2 is open; the list will refresh after it closes"
    if (fresh and not force) or not dev_lock.acquire(blocking=False):
        return None
    dev_state["refreshing"] = True
    broadcast(devices_message())

    def work():
        try:
            devs = list_devices()
            dev_state.update(devices=devs, updated=time.time(), error=None)
        except Exception as e:  # shown on the page as "Could not list scanners"
            dev_state["error"] = str(e) or type(e).__name__
            if not isinstance(e, (OSError, subprocess.SubprocessError)):
                traceback.print_exc()
        finally:
            dev_state["refreshing"] = False
            dev_lock.release()
            broadcast(devices_message())

    threading.Thread(target=work, daemon=True).start()
    return None


def run_scan(out_img, dpi, mode, device):
    if CFG["SCAN_CMD"]:
        cmd = [p.replace("{out}", str(out_img)) for p in shlex.split(CFG["SCAN_CMD"])]
    else:
        cmd = naps2_base() + [
            "--driver", CFG["SCANNER_DRIVER"],
            "-o", str(out_img), "-f", "-n", "1",
            "--source", "glass", "--dpi", dpi, "--bitdepth", mode,
        ]
        if device:
            cmd += ["--device", device]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=CFG["TIMEOUT"])


def run_merge(out_pdf, pages):
    if CFG["MERGE_CMD"]:
        cmd = []
        for part in shlex.split(CFG["MERGE_CMD"]):
            if part == "{inputs}":
                cmd += [str(p) for p in pages]
            else:
                cmd.append(part.replace("{out}", str(out_pdf)))
    else:
        cmd = naps2_base() + ["-n", "0", "-i", ";".join(str(p) for p in pages), "-o", str(out_pdf), "-f"]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=CFG["TIMEOUT"])


BAD_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
MAX_DEPTH = 3
MAX_OPEN_PAGES = 200


def clean_name(name, fallback="Scan"):
    name = BAD_NAME.sub("_", str(name)).strip(" .")[:100]
    return name if name not in ("", ".", "..") else fallback


def run_split(pdf, outdir):
    """Turn a PDF into one image per page inside outdir (NAPS2 numbers them via $(nnn))."""
    if CFG["OPEN_CMD"]:
        cmd = [p.replace("{in}", str(pdf)).replace("{outdir}", str(outdir))
               for p in shlex.split(CFG["OPEN_CMD"])]
    else:
        cmd = naps2_base() + ["-n", "0", "-i", str(pdf), "-o", str(outdir / "page$(nnnn).jpg"), "-f"]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=CFG["TIMEOUT"])


def safe_pdf_name(name):
    name = clean_name(name)
    return name if name.lower().endswith(".pdf") else name + ".pdf"


def resolve_rel(rel, must_exist=True):
    """Map a '/'-separated path from the browser to a path inside SAVE_DIR, or None."""
    root = SAVE_DIR.resolve()
    target = (root / str(rel or "")).resolve()
    if target != root and root not in target.parents:
        return None
    if must_exist and not target.exists():
        return None
    return target


def rel_of(path):
    return path.resolve().relative_to(SAVE_DIR.resolve()).as_posix()


def unique_path(folder, name):
    """folder/name, or folder/'name (2)' etc. so nothing is ever overwritten."""
    target, stem, ext, n = folder / name, Path(name).stem, Path(name).suffix, 2
    while target.exists():
        target = folder / f"{stem} ({n}){ext}"
        n += 1
    return target


def page_path(page_id):
    return PAGES_DIR / f"{page_id}.img"


def build_pdf(ids, target):
    """Merge scanned pages into a PDF at target. Returns (ok, status, error)."""
    if not isinstance(ids, list) or not ids or not all(
            isinstance(i, str) and ID_RE.match(i) for i in ids):
        return False, 400, "no valid pages"
    pages = []
    try:
        for i in ids:
            f = page_path(i)
            if not f.is_file():
                return False, 404, "page no longer exists"
            # NAPS2 detects the format by extension, so give each page a real one.
            real = f.with_suffix(".png" if f.read_bytes()[:4] == b"\x89PNG" else ".jpg")
            if not real.exists():
                shutil.copyfile(f, real)
            pages.append(real)
        try:
            proc = run_merge(target, pages)
        except subprocess.TimeoutExpired:
            return False, 504, "saving timed out"
        except OSError as e:
            return False, 500, f"cannot run NAPS2: {e}"
        if proc.returncode != 0 or not target.exists():
            return False, 500, (proc.stderr or proc.stdout or "save failed").strip()
        return True, 200, None
    finally:
        for p in pages:
            p.unlink(missing_ok=True)


def list_library():
    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    folders, files = [], []
    for p in sorted(SAVE_DIR.rglob("*"), key=lambda x: x.as_posix().lower()):
        if p.is_symlink():
            continue
        if p.is_dir():
            folders.append(rel_of(p))
        elif p.suffix.lower() == ".pdf":
            st = p.stat()
            files.append({"path": rel_of(p), "size": st.st_size, "mtime": int(st.st_mtime)})
    return {"dir": str(SAVE_DIR), "folders": folders, "files": files}


WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
ws_clients = set()  # connected pages; they are pushed scan progress instead of having to poll


class WSClient:
    def __init__(self, sock):
        self.sock, self.lock = sock, threading.Lock()

    def send(self, opcode, payload=b""):
        n = len(payload)
        head = bytes([0x80 | opcode])
        head += bytes([n]) if n < 126 else bytes([126]) + struct.pack(">H", n) if n < 65536 else bytes([127]) + struct.pack(">Q", n)
        with self.lock:
            self.sock.sendall(head + payload)


def ws_parse(buf):
    """Return (opcode, payload, rest) for the first complete client frame in buf, or None if it is incomplete."""
    if len(buf) < 2:
        return None
    opcode, masked, n, i = buf[0] & 0x0F, buf[1] & 0x80, buf[1] & 0x7F, 2
    if n == 126:
        if len(buf) < 4:
            return None
        n, i = struct.unpack(">H", buf[2:4])[0], 4
    elif n == 127:
        if len(buf) < 10:
            return None
        n, i = struct.unpack(">Q", buf[2:10])[0], 10
    mask = buf[i:i + 4] if masked else b""
    i += 4 if masked else 0
    if len(buf) < i + n:
        return None
    payload = buf[i:i + n]
    if masked:
        payload = bytes(b ^ mask[k % 4] for k, b in enumerate(payload))
    return opcode, payload, buf[i + n:]


def broadcast(message):
    data = json.dumps(message).encode()
    for client in list(ws_clients):
        try:
            client.send(1, data)
        except OSError:
            ws_clients.discard(client)


def publish(job, info):
    """Record a scan job's state for polling and push it to every connected page."""
    scan_jobs[job] = info
    broadcast({"type": "scan", "job": job, **info})


def scan_with_epson(job, tmp, dpi, mode):
    """Scan through Epson Scan 2. Returns (scan seconds, None), or (None, reason) when WIA should be used instead.
    The seconds run from Epson accepting the click to the saved file, so opening its window and waiting for the
    scanner head to reset after the previous page are not counted."""
    if not dev_lock.acquire(blocking=False):  # a running NAPS2 device search would collide with Epson's connection
        publish(job, {"state": "running", "phase": "searching"})
        if not dev_lock.acquire(timeout=90):
            return None, "the scanner search was still running after 90 s"
    dev_lock.release()
    progress = lambda phase: publish(job, {"state": "running", "phase": phase})
    try:
        return epson_session().scan(dpi, mode, tmp, CFG["TIMEOUT"], progress), None
    except Exception as e:
        if not isinstance(e, epson_scan.EpsonError):
            traceback.print_exc()
        try:
            epson_session().close()  # an open Epson window makes WIA report "scanner busy"
        except Exception:
            traceback.print_exc()
        return None, str(e) or type(e).__name__


def scan_with_naps2(tmp, dpi, mode, device):
    try:
        proc = run_scan(tmp, dpi, mode, device)
    except subprocess.TimeoutExpired:
        raise RuntimeError("scan timed out")
    except OSError as e:
        raise RuntimeError(f"cannot run scanner command: {e}")
    if proc.returncode != 0 or not tmp.exists():
        raise RuntimeError((proc.stderr or proc.stdout or "scan failed").strip())


def free_cpu(job):
    """Close the PAUSE_APPS before scanning. A failure here must not stop the scan, but it is logged."""
    if pauser is None:
        return
    try:
        pauser.pause(on_close=lambda: publish(job, {"state": "running", "phase": "freeing"}))
    except Exception:
        traceback.print_exc()


def resume_loop():
    """Reopen the paused apps once the scanner has been idle for PAUSE_RESUME_AFTER seconds."""
    while True:
        time.sleep(5)
        try:
            idle = time.monotonic() - (last_scan_end or 0) > CFG["PAUSE_RESUME_AFTER"]
            if pauser.waiting_to_resume() and idle and not scan_lock.locked():
                pauser.resume()
        except Exception:
            traceback.print_exc()


def scan_job(job, dpi, mode, device):
    """Run one scan in the background (caller holds scan_lock). Progress and the outcome are pushed over the WebSocket
    and also kept in scan_jobs for the page to poll."""
    global last_scan_end
    try:
        pid = uuid.uuid4().hex
        tmp = PAGES_DIR / f"{pid}.jpg"  # extension tells NAPS2 the format
        free_cpu(job)
        if CFG["SCAN_CMD"] and os.environ.get("STUB_PHASES"):  # test hook: pretend to open Epson and wait for the scanner
            for phase in ("opening", "waiting"):
                publish(job, {"state": "running", "phase": phase})
                time.sleep(1.5)
        started = time.monotonic()
        cold = last_scan_end is None or started - last_scan_end > COLD_AFTER
        engine, note, seconds = "wia", None, None
        if use_epson():
            seconds, note = scan_with_epson(job, tmp, dpi, mode)
            if seconds is not None:
                engine, cold = "epson", False
        if engine == "wia":
            publish(job, {"state": "running", "phase": "scanning"})
            scan_with_naps2(tmp, dpi, mode, device)
        last_scan_end = time.monotonic()
        tmp.rename(page_path(pid))
        done = {"state": "done", "id": pid, "engine": engine, "cold": cold,
                "seconds": seconds if seconds is not None else round(last_scan_end - started, 1)}
        if note:
            done["note"] = f"Epson Scan 2 failed ({note}); scanned with WIA instead."
        publish(job, done)
    except Exception as e:
        last_scan_end = time.monotonic()
        if not isinstance(e, (RuntimeError, epson_scan.EpsonError)):
            traceback.print_exc()  # a bug, not a scanner problem: make it visible in server.log too
        publish(job, {"state": "error", "error": str(e) or type(e).__name__})
    finally:
        scan_lock.release()
        if len(scan_jobs) > 50:  # keep the table small
            for k in list(scan_jobs)[:-20]:
                scan_jobs.pop(k, None)


LOGIN_ATTEMPTS = 5  # failed logins from one address before it is locked out
LOGIN_LOCKOUT = 60  # seconds
sessions = {}  # session token -> expiry (monotonic); lost on restart, so everyone logs in again
failed_logins = {}  # client address -> [failures, locked until (monotonic)]
auth_lock = threading.Lock()

LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in - Scan Creator</title><style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#2b2b2b;font:14px "Open Sans",Segoe UI,Arial,sans-serif;color:#222}
form{width:min(92vw,340px);background:#fff;padding:24px;box-shadow:0 8px 30px #0008}
h1{margin:0 0 4px;font-size:18px}p{margin:0 0 16px;color:#666}
label{display:block;margin:12px 0 4px;font-weight:600}
input{box-sizing:border-box;width:100%;padding:8px;border:1px solid #bbb;font:inherit}
input:focus{outline:2px solid #e04040;border-color:#e04040}
button{margin-top:18px;width:100%;padding:9px;border:0;background:#e04040;color:#fff;font:inherit;font-weight:600;cursor:pointer}
button:hover{background:#c83030}.err{margin-top:14px;padding:8px;background:#fdeaea;border-left:3px solid #e04040;color:#8a1f1f}
</style></head><body><form method="post" action="/login">
<h1>Scan Creator</h1><p>Sign in to use the scanner.</p>
<label for="u">Username</label><input id="u" name="username" autocomplete="username" required autofocus>
<label for="p">Password</label><input id="p" name="password" type="password" autocomplete="current-password" required>
<button type="submit">Sign in</button>{{NOTE}}</form></body></html>"""


def auth_enabled():
    return bool(CFG["AUTH_USER"] and CFG["AUTH_PASSWORD"])


def new_session():
    token = secrets.token_urlsafe(32)
    with auth_lock:
        now = time.monotonic()
        for t in [t for t, exp in sessions.items() if exp < now]:
            del sessions[t]
        sessions[token] = now + CFG["AUTH_SESSION_HOURS"] * 3600
    return token


def credentials_ok(user, password):
    # compare both before deciding, in constant time, so neither field's correctness leaks through timing
    user_ok = hmac.compare_digest(user.encode(), CFG["AUTH_USER"].encode())
    pass_ok = hmac.compare_digest(password.encode(), CFG["AUTH_PASSWORD"].encode())
    return user_ok and pass_ok


class Handler(BaseHTTPRequestHandler):
    def send_bytes(self, status, body, ctype, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        try:
            self.end_headers()
            self.wfile.write(body)
        except ConnectionError:
            pass  # client closed the connection (page refreshed or navigated away)

    def send_json(self, status, obj):
        self.send_bytes(status, json.dumps(obj).encode(), "application/json")

    def send_pdf(self, f, disposition):
        name = f.name.encode("ascii", "replace").decode().replace('"', "")
        star = quote(f.name)
        self.send_bytes(200, f.read_bytes(), "application/pdf", {
            "Content-Disposition": f"{disposition}; filename=\"{name}\"; filename*=UTF-8''{star}"})

    # --- login ---------------------------------------------------------------------------------
    def session_token(self):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        return cookie["session"].value if "session" in cookie else None

    def logged_in(self):
        token = self.session_token()
        with auth_lock:
            return token in sessions and sessions[token] > time.monotonic()

    def authorize(self, path):
        """True if the request may proceed. Otherwise the reply has been sent: pages go to the login form, API calls get 401."""
        if not auth_enabled() or path == "/login" or self.logged_in():
            return True
        if path == "/" and self.command == "GET":
            self.send_response(302)
            self.send_header("Location", "/login")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.send_json(401, {"error": "login required"})
        return False

    def send_login(self, status=200, error=""):
        note = f'<div class="err">{escape(error)}</div>' if error else ""
        self.send_bytes(status, LOGIN_PAGE.replace("{{NOTE}}", note).encode(), "text/html; charset=utf-8", {"Cache-Control": "no-store"})

    def handle_login(self):
        if not auth_enabled():
            return self.redirect("/")
        if self.command == "GET":
            return self.redirect("/") if self.logged_in() else self.send_login()
        ip = self.client_address[0]
        with auth_lock:
            count, locked_until = failed_logins.get(ip, [0, 0.0])
        wait = locked_until - time.monotonic()
        if locked_until and wait <= 0:  # the lockout has ended: start counting again
            count, locked_until = 0, 0.0
        if wait > 0:
            return self.send_login(429, f"Too many failed attempts. Try again in {int(wait) + 1} seconds.")
        length = min(int(self.headers.get("Content-Length") or 0), 4096)
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"), keep_blank_values=True)
        user, password = form.get("username", [""])[0], form.get("password", [""])[0]
        if not credentials_ok(user, password):
            with auth_lock:
                count += 1
                failed_logins[ip] = [count, time.monotonic() + LOGIN_LOCKOUT if count >= LOGIN_ATTEMPTS else 0.0]
            time.sleep(0.5)  # slows guessing a little
            print(f"failed login from {ip} ({count} in a row)", flush=True)
            return self.send_login(401, "Wrong username or password.")
        with auth_lock:
            failed_logins.pop(ip, None)
        max_age = int(CFG["AUTH_SESSION_HOURS"] * 3600)
        self.redirect("/", {"Set-Cookie": f"session={new_session()}; HttpOnly; SameSite=Strict; Path=/; Max-Age={max_age}"})

    def redirect(self, where, headers=None):
        self.send_response(303)
        self.send_header("Location", where)
        self.send_header("Content-Length", "0")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def handle_logout(self):
        token = self.session_token()
        with auth_lock:
            sessions.pop(token, None)
        self.send_json_with(200, {"ok": True}, {"Set-Cookie": "session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"})

    def send_json_with(self, status, obj, headers):
        self.send_bytes(status, json.dumps(obj).encode(), "application/json", headers)

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return None

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path
        if not self.authorize(path):
            return
        if path == "/login":
            self.handle_login()
        elif path == "/me":
            self.send_json(200, {"auth": auth_enabled(), "user": CFG["AUTH_USER"] if auth_enabled() else None})
        elif path == "/":
            self.send_bytes(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path.startswith("/scan/"):
            self.handle_scan_status(path[len("/scan/"):])
        elif path == "/ws":
            self.handle_ws()
        elif path == "/devices":
            forced = "refresh" in parse_qs(url.query, keep_blank_values=True)
            skipped = refresh_devices(force=forced)
            reply = {**dev_state, "engine": "epson" if use_epson() else "wia"}  # instant: the cached list plus whether a search is running
            if skipped and forced:
                reply["skipped"] = skipped
            self.send_json(200, reply)
        elif path == "/files":
            self.send_json(200, list_library())
        elif path.startswith("/files/"):
            f = resolve_rel(unquote(path[len("/files/"):]))
            if not f or not f.is_file() or f.suffix.lower() != ".pdf":
                return self.send_json(404, {"error": "not found"})
            dl = "download" in parse_qs(url.query, keep_blank_values=True)
            self.send_pdf(f, "attachment" if dl else "inline")
        elif path.startswith("/static/"):
            f = (STATIC_DIR / unquote(path[len("/static/"):])).resolve()
            if STATIC_DIR not in f.parents or not f.is_file():
                return self.send_json(404, {"error": "not found"})
            ctype = STATIC_TYPES.get(f.suffix.lower(), "application/octet-stream")
            self.send_bytes(200, f.read_bytes(), ctype, {"Cache-Control": "max-age=86400"})
        elif path.startswith("/page/"):
            pid = path[len("/page/"):]
            f = page_path(pid) if ID_RE.match(pid) else None
            if not f or not f.is_file():
                return self.send_json(404, {"error": "not found"})
            body = f.read_bytes()
            ctype = "image/png" if body[:4] == b"\x89PNG" else "image/jpeg"
            self.send_bytes(200, body, ctype)
        else:
            self.send_json(404, {"error": "not found"})

    def do_DELETE(self):
        path = urlparse(self.path).path
        if not self.authorize(path):
            return
        if path.startswith("/page/"):
            pid = path[len("/page/"):]
            if ID_RE.match(pid):
                page_path(pid).unlink(missing_ok=True)
                return self.send_json(200, {"ok": True})
        elif path.startswith("/files/"):
            f = resolve_rel(unquote(path[len("/files/"):]))
            if not f or f == SAVE_DIR.resolve():
                return self.send_json(404, {"error": "not found"})
            if f.is_file() and f.suffix.lower() == ".pdf":
                f.unlink()
            elif f.is_dir():
                try:
                    f.rmdir()
                except OSError:
                    return self.send_json(409, {"error": "folder is not empty"})
            else:
                return self.send_json(404, {"error": "not found"})
            return self.send_json(200, {"ok": True})
        self.send_json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if not self.authorize(path):
            return
        if path == "/login":
            return self.handle_login()
        if path == "/logout":
            return self.handle_logout()
        opts = self.read_json()
        if not isinstance(opts, dict):
            return self.send_json(400, {"error": "invalid JSON"})
        handlers = {"/scan": self.handle_scan, "/save": self.handle_save, "/pdf": self.handle_pdf,
                    "/folder": self.handle_folder, "/open": self.handle_open, "/rename": self.handle_rename,
                    "/move": self.handle_move}
        if path in handlers:
            return handlers[path](opts)
        self.send_json(404, {"error": "not found"})

    def ws_reply(self, payload):
        try:
            msg = json.loads(payload)
            job = msg["job"] if msg.get("type") == "state" else None
        except (ValueError, KeyError, AttributeError):
            return {"type": "error", "error": "bad WebSocket message"}
        if job not in scan_jobs:
            return {"type": "scan", "job": job, "state": "error", "error": "unknown scan job"}
        return {"type": "scan", "job": job, **scan_jobs[job]}

    def handle_ws(self):
        """WebSocket (RFC 6455) that pushes scan progress. Only pages served by this server may connect."""
        key = self.headers.get("Sec-WebSocket-Key")
        origin = self.headers.get("Origin")
        if self.headers.get("Upgrade", "").lower() != "websocket" or not key:
            return self.send_json(400, {"error": "WebSocket upgrade required"})
        if origin and urlparse(origin).netloc != self.headers.get("Host"):
            return self.send_json(403, {"error": "cross-site WebSocket refused"})
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        # Written by hand: send_response() answers "HTTP/1.0 101", which Chrome accepts but Firefox rejects
        # (it then reconnects in a loop). RFC 6455 requires HTTP/1.1 here.
        self.log_request(101)
        self.wfile.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                          f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        self.close_connection = True
        sock = self.connection
        client = WSClient(sock)
        ws_clients.add(client)
        buf = b""
        try:
            while True:
                if not select.select([sock], [], [], 25)[0]:
                    client.send(9)  # ping, so dead connections are noticed
                    continue
                chunk = sock.recv(4096)
                if not chunk:
                    return
                buf += chunk
                while (frame := ws_parse(buf)) is not None:
                    opcode, payload, buf = frame
                    if opcode == 8:  # close
                        client.send(8)
                        return
                    if opcode == 9:  # ping
                        client.send(10, payload)
                    elif opcode == 1:  # text: the page asks where a job stands (it may have missed earlier pushes)
                        client.send(1, json.dumps(self.ws_reply(payload)).encode())
                if len(buf) > 65536:
                    return
        except OSError:
            pass
        finally:
            ws_clients.discard(client)

    def handle_scan(self, opts):
        dpi, mode = str(opts.get("dpi", "300")), opts.get("mode", "color")
        if dpi not in ALLOWED_DPI or mode not in ALLOWED_MODES:
            return self.send_json(400, {"error": "invalid dpi or mode"})
        device = opts.get("device") or CFG["SCANNER_DEVICE"] or (known_devices[0] if known_devices else "")
        if not device and not CFG["SCAN_CMD"] and not use_epson():
            err = dev_state["error"]
            return self.send_json(503, {"error": f"Cannot list scanners: {err}" if err
                                        else "Still looking for scanners. Try again in a few seconds."})
        if device and device != CFG["SCANNER_DEVICE"] and device not in known_devices:
            return self.send_json(400, {"error": "unknown scanner; refresh the list"})
        if not scan_lock.acquire(blocking=False):
            return self.send_json(409, {"error": "scanner busy"})
        job = uuid.uuid4().hex
        scan_jobs[job] = {"state": "running", "phase": "preparing"}
        threading.Thread(target=scan_job, args=(job, dpi, mode, device), daemon=True).start()
        self.send_json(202, {"job": job})

    def handle_scan_status(self, job):
        info = scan_jobs.get(job)
        if info is None:
            return self.send_json(404, {"error": "unknown scan job"})
        self.send_json(200, info)

    def handle_save(self, opts):
        """Save the pages as a PDF into a folder under SAVE_DIR."""
        SAVE_DIR.mkdir(parents=True, exist_ok=True)
        folder = resolve_rel(opts.get("folder", ""))
        if not folder or not folder.is_dir():
            return self.send_json(400, {"error": "unknown folder"})
        target = unique_path(folder, safe_pdf_name(opts.get("name", "Scan")))
        ok, status, err = build_pdf(opts.get("pages"), target)
        if not ok:
            return self.send_json(status, {"error": err})
        self.send_json(200, {"path": rel_of(target), "name": target.name})

    def handle_pdf(self, opts):
        """Build a PDF and return it directly (browser download), saving nothing."""
        tmp = Path(tempfile.mkdtemp(prefix="dl-"))
        try:
            target = tmp / safe_pdf_name(opts.get("name", "Scan"))
            ok, status, err = build_pdf(opts.get("pages"), target)
            if not ok:
                return self.send_json(status, {"error": err})
            self.send_pdf(target, "attachment")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def handle_open(self, opts):
        """Split a saved PDF into page images so it can be edited in the workspace."""
        src = resolve_rel(opts.get("path"))
        if not src or not src.is_file() or src.suffix.lower() != ".pdf":
            return self.send_json(404, {"error": "not found"})
        tmp = Path(tempfile.mkdtemp(prefix="open-"))
        try:
            try:
                proc = run_split(src, tmp)
            except subprocess.TimeoutExpired:
                return self.send_json(504, {"error": "opening timed out"})
            except OSError as e:
                return self.send_json(500, {"error": f"cannot run NAPS2: {e}"})
            imgs = sorted((p for p in tmp.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png")),
                          key=lambda p: [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", p.name)])
            if proc.returncode != 0 or not imgs:
                return self.send_json(500, {"error": (proc.stderr or proc.stdout or "no pages found in PDF").strip()})
            if len(imgs) > MAX_OPEN_PAGES:
                return self.send_json(400, {"error": f"PDF has {len(imgs)} pages; the limit is {MAX_OPEN_PAGES}"})
            ids = []
            for img in imgs:
                pid = uuid.uuid4().hex
                shutil.move(str(img), page_path(pid))
                ids.append(pid)
            self.send_json(200, {"pages": ids, "name": src.stem})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def handle_folder(self, opts):
        parent = resolve_rel(opts.get("parent", ""))
        if not parent or not parent.is_dir():
            return self.send_json(400, {"error": "unknown parent folder"})
        depth = len(parent.resolve().relative_to(SAVE_DIR.resolve()).parts)
        if depth >= MAX_DEPTH:
            return self.send_json(400, {"error": f"folders can only nest {MAX_DEPTH} levels deep"})
        name = clean_name(opts.get("name", ""), "")
        if not name:
            return self.send_json(400, {"error": "folder name required"})
        target = parent / name
        if target.exists():
            return self.send_json(409, {"error": "already exists"})
        target.mkdir()
        self.send_json(200, {"path": rel_of(target)})

    def handle_rename(self, opts):
        src = resolve_rel(opts.get("path"))
        if not src or src == SAVE_DIR.resolve():
            return self.send_json(404, {"error": "not found"})
        if src.is_file() and src.suffix.lower() != ".pdf":
            return self.send_json(404, {"error": "not found"})
        name = safe_pdf_name(opts.get("name", "")) if src.is_file() else clean_name(opts.get("name", ""), "")
        if not name:
            return self.send_json(400, {"error": "name required"})
        dest = src.with_name(name)
        if dest.exists() and dest != src:
            return self.send_json(409, {"error": "a file with that name already exists"})
        src.rename(dest)
        self.send_json(200, {"path": rel_of(dest)})

    def handle_move(self, opts):
        src = resolve_rel(opts.get("path"))
        folder = resolve_rel(opts.get("folder", ""))
        if not src or src == SAVE_DIR.resolve() or not folder or not folder.is_dir():
            return self.send_json(404, {"error": "not found"})
        if src.is_file() and src.suffix.lower() != ".pdf":
            return self.send_json(404, {"error": "not found"})
        if src == folder or src in folder.parents:
            return self.send_json(400, {"error": "cannot move a folder into itself"})
        dest = unique_path(folder, src.name)
        src.rename(dest)
        self.send_json(200, {"path": rel_of(dest)})


if __name__ == "__main__":
    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    refresh_devices(force=True)  # find scanners now so the first page load is instant
    if pauser:
        threading.Thread(target=resume_loop, daemon=True).start()
    server = ThreadingHTTPServer((CFG["HOST"], CFG["PORT"]), Handler)
    print(f"Scan page on http://localhost:{CFG['PORT']}  (saving PDFs to {SAVE_DIR})")
    try:
        server.serve_forever()
    finally:
        shutil.rmtree(PAGES_DIR, ignore_errors=True)
