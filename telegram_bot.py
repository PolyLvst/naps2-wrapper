"""Telegram bot: print files sent to it and scan to PDF from the chat. Standard library only.

It long-polls the Bot API (outbound HTTPS only, no open port). Coworkers register once with TELEGRAM_PASSWORD;
registrations are kept in telegram_users.json. The server module is passed in by server.py (see start()).
"""
import contextlib
import hashlib
import hmac
import http.client
import json
import mimetypes
import os
import re
import secrets
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import office_pdf
import printing

POLL_TIMEOUT = 50  # seconds a getUpdates call waits for news
MAX_DOWNLOAD = 20 * 1024 * 1024  # Bot API limit for files a bot can download
MAX_UPLOAD = 50 * 1024 * 1024  # Bot API limit for files a bot can send
SCAN_IDLE = 30 * 60  # an unfinished scan is dropped after this long without a tap
PRINT_IDLE = 15 * 60  # a print dialog expires after this long
READY_IDLE = 60 * 60  # a finished scan can be printed with its "Print it" button for this long
STALE_UPDATE = 10 * 60  # messages older than this (sent while the server was down) are ignored
REGISTER_ATTEMPTS, REGISTER_LOCKOUT = 5, 10 * 60
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".gif"}
DPIS = ("100", "150", "200", "300", "600")
MODES = {"color": "color", "colour": "color", "gray": "gray", "grey": "gray", "bw": "bw"}
MODE_TEXT = {"color": "colour", "gray": "grey", "bw": "black & white"}
PHASES = {
    "preparing": "Starting the scan…",
    "freeing": "Closing apps that slow the scan down…",
    "searching": "Waiting for the scanner search to finish…",
    "opening": "Opening Epson Scan 2…",
    "waiting": "Waiting for the scanner to be ready…",
    "scanning": "Scanning…",
}
COMMANDS = [("menu", "Show the buttons"), ("scan", "Scan from the glass to a PDF"), ("status", "Is the scanner or printer busy?"),
            ("cancel", "Stop the current scan or print"), ("help", "What this bot can do"),
            ("logout", "Stop using the bot on this account")]
MENU_TEXT = """🖨 Scanner & printer

To print: send me a PDF, a picture, or a Word, Excel or PowerPoint file.
To scan: put the page on the glass and tap Scan."""
HELP = """❓ Help

• Print: tap Print to see how, or just send a PDF, a picture, or a Word, Excel or PowerPoint file. Before it prints you choose copies, colour, pages, paper and more with buttons. (Send pictures "as File" for full quality.)
• Scan: tap Scan to scan a page from the glass. Add more pages, then tap Finish to get them as one PDF. Scan settings changes the resolution and colour.
• Status: is the scanner free and the printer ready?

Commands work too: /menu, /scan (e.g. /scan 300 gray), /status, /cancel, /logout."""
ADMIN_HELP = "\n\nAdmin: Users in the menu (or /users, /remove <id or @username>)."


def log(msg):
    print(f"telegram: {msg}", flush=True)


class TelegramError(Exception):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


class Api:
    def __init__(self, base, token):
        self.base, self.token = base.rstrip("/"), token
        self.ctx, self.ctx_lock, self.refreshed = ssl.create_default_context(), threading.Lock(), 0.0
        self.pool, self.pool_lock = [], threading.Lock()  # idle kept-open connections: (connection, idle since)

    def refresh_trust(self):
        """After a certificate error: let Windows check the server once, which makes it download a root certificate
        it does not have yet (Windows fetches them on demand, Python only reads what is already there), then reload
        the certificates. At most every 10 minutes. Returns True if it is worth trying again."""
        with self.ctx_lock:
            if time.monotonic() - self.refreshed < 600:
                return False
            self.refreshed = time.monotonic()
            host = urlparse(self.base).hostname or ""
            if os.name == "nt" and re.fullmatch(r"[A-Za-z0-9.-]+", host):
                ps = (f"$t = New-Object Net.Sockets.TcpClient('{host}', 443); "
                      f"$s = New-Object Net.Security.SslStream($t.GetStream()); $s.AuthenticateAsClient('{host}'); $t.Close()")
                try:
                    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
                                   capture_output=True, timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                except (OSError, subprocess.TimeoutExpired):
                    pass
            self.ctx = ssl.create_default_context()
            with self.pool_lock:
                for conn, _ in self.pool:
                    conn.close()
                self.pool.clear()
            log("certificate check failed; asked Windows to update its root certificates and reloaded them")
            return True

    # Telegram is ~0.2 s away from the scanner PC, but a new HTTPS connection costs ~0.4 s more, so connections are
    # kept open and reused (a tap on a button makes two or three calls).
    def _connect(self, timeout):
        u = urlparse(self.base)
        if u.scheme == "https":
            return http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout, context=self.ctx)
        return http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)

    def _take(self, timeout):
        with self.pool_lock:
            while self.pool:
                conn, idle_since = self.pool.pop()
                if time.monotonic() - idle_since < 30:  # older ones may have been closed by the server
                    conn.timeout = timeout
                    if conn.sock:
                        conn.sock.settimeout(timeout)
                    return conn, True
                conn.close()
        return self._connect(timeout), False

    def _give(self, conn):
        with self.pool_lock:
            self.pool.append((conn, time.monotonic()))

    @contextlib.contextmanager
    def response(self, path, body=None, headers=None, timeout=30):
        """Send a request and yield the response; the connection goes back to the pool if it is still usable."""
        conn, reused = self._take(timeout)
        resp, retried_trust = None, False
        while resp is None:
            try:
                conn.request("GET" if body is None else "POST", path, body, headers or {})
                resp = conn.getresponse()
            except ssl.SSLCertVerificationError:
                conn.close()
                if retried_trust or not self.refresh_trust():
                    raise
                retried_trust, conn = True, self._connect(timeout)
            except (OSError, http.client.HTTPException):
                conn.close()
                if not reused:
                    raise
                conn, reused = self._connect(timeout), False  # the kept connection had gone stale: once more
        try:
            yield resp
        except BaseException:
            conn.close()
            raise
        if resp.isclosed() and not resp.will_close:
            self._give(conn)
        else:
            conn.close()

    def call(self, method, params=None, files=None, timeout=30):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if files:
            body, ctype = multipart(params, files)
        else:
            body, ctype = json.dumps(params).encode(), "application/json"
        prefix = urlparse(self.base).path.rstrip("/")
        try:  # never put the path in an error message: it holds the token
            with self.response(f"{prefix}/bot{self.token}/{method}", body, {"Content-Type": ctype}, timeout) as r:
                raw, status = r.read(), r.status
        except (OSError, http.client.HTTPException) as e:
            raise TelegramError(f"cannot reach Telegram ({e or type(e).__name__})") from None
        try:
            data = json.loads(raw)
        except ValueError:
            raise TelegramError(f"Telegram answered HTTP {status}", status) from None
        if not data.get("ok"):
            raise TelegramError(data.get("description") or "Telegram error", data.get("error_code"))
        return data["result"]

    def download(self, file_path, dest, timeout=180):
        prefix = urlparse(self.base).path.rstrip("/")
        try:
            with self.response(f"{prefix}/file/bot{self.token}/{file_path}", timeout=timeout) as r, open(dest, "wb") as f:
                if r.status != 200:
                    r.read()
                    raise TelegramError(f"downloading the file failed (HTTP {r.status})")
                got = 0
                while chunk := r.read(65536):
                    got += len(chunk)
                    if got > MAX_DOWNLOAD:
                        raise TelegramError("the file is larger than 20 MB")
                    f.write(chunk)
        except (OSError, http.client.HTTPException) as e:
            raise TelegramError(f"downloading the file failed ({e or type(e).__name__})") from None


def multipart(params, files):
    boundary = "----tg" + secrets.token_hex(12)
    out = bytearray()
    for k, v in params.items():
        if v is None:
            continue
        v = json.dumps(v) if isinstance(v, (dict, list)) else str(v)
        out += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n'.encode() + v.encode() + b"\r\n"
    for field, filename, data, ctype in files:
        filename = filename.replace('"', "'").replace("\r", " ").replace("\n", " ")
        out += (f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
                f"Content-Type: {ctype}\r\n\r\n").encode() + data + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def tap_note(data):
    """The short note Telegram shows at the top of the chat right after a button is tapped (None = nothing)."""
    parts = (data or "").split(":")
    kind, action = parts[0], parts[-1]
    if kind == "m" and len(parts) == 3 and parts[1] == "dpi":
        return f"✓ {parts[2]} dpi"
    if kind == "m" and len(parts) == 3 and parts[1] == "mode":
        return f"✓ {MODE_TEXT.get(parts[2], parts[2]).capitalize()}"
    notes = {("m", "status"): "⏳ Checking the scanner and printer…", ("m", "scan"): "📄 Starting the scan…",
             ("s", "next"): "📄 Starting the scan…", ("s", "finish"): "⏳ Making the PDF…",
             ("p", "go"): "🖨 Sending to the printer…", ("m", "cancel"): "Cancelling…", ("s", "cancel"): "Cancelling…"}
    if kind == "r":
        return "⏳ Opening the print settings…"
    return notes.get((kind, action))


def button(text, data):
    return {"text": text, "callback_data": data}


def keyboard(rows):
    return {"inline_keyboard": rows}


NO_KEYBOARD = {"inline_keyboard": []}


def tick(on):
    return "✓ " if on else ""


def plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


# --- registered users ------------------------------------------------------------------------------------
def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()
    return f"pbkdf2_sha256${salt}${digest}"


def password_matches(stored, password):
    try:
        _, salt, _ = stored.split("$")
    except (AttributeError, ValueError):
        return False
    return hmac.compare_digest(hash_password(password, salt), stored)


class Users:
    """telegram_users.json: {"password_hash": ..., "users": {"<id>": {"name", "username", "since"}}}.
    When TELEGRAM_PASSWORD changes, the stored hash no longer matches and everyone has to register again."""

    def __init__(self, path, password, admins):
        self.path, self.password, self.admins = path, password, set(admins)
        self.lock = threading.Lock()
        data = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                log(f"{path.name} is not valid JSON; starting with no registered users")
        self.users = data.get("users", {}) if isinstance(data.get("users"), dict) else {}
        self.hash = data.get("password_hash") or ""
        if not password:
            self.users = {}  # registration is off: only admins may use the bot
        elif not password_matches(self.hash, password):
            if self.users:
                log(f"TELEGRAM_PASSWORD changed: {len(self.users)} registered user(s) must register again")
            self.users, self.hash = {}, hash_password(password)
            self._save()

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"password_hash": self.hash, "users": self.users}, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def allowed(self, uid):
        return uid in self.admins or str(uid) in self.users

    def add(self, user):
        with self.lock:
            self.users[str(user["id"])] = {"name": " ".join(filter(None, [user.get("first_name"), user.get("last_name")])),
                                           "username": user.get("username") or "",
                                           "since": datetime.now().strftime("%Y-%m-%d %H:%M")}
            self._save()

    def remove(self, key):
        """Remove by id or @username; returns the removed (id, entry) or None."""
        key = key.strip().lstrip("@").lower()
        with self.lock:
            for uid, u in list(self.users.items()):
                if uid == key or (u.get("username") or "").lower() == key:
                    del self.users[uid]
                    self._save()
                    return uid, u
        return None


def describe(uid, u):
    name = u.get("name") or "?"
    return f"{name}{' (@' + u['username'] + ')' if u.get('username') else ''}, id {uid}"


# --- per-chat state --------------------------------------------------------------------------------------
class Chat:
    def __init__(self):
        self.lock = threading.Lock()
        self.awaiting = None  # "password" | ("range", print token)
        self.scan = None  # {"sid", "pages", "dpi", "mode", "last", "buttons"}
        self.prints = {}  # token -> print job
        self.settings = None  # this user's last print settings
        self.scan_prefs = {"dpi": "200", "mode": "color"}  # resolution and colour for the next scan
        self.ready = {}  # token -> finished scan kept for its "Print it" button: {"pdf", "name", "pages", "msg", ...}


class Bot:
    def __init__(self, srv):
        self.srv, cfg = srv, srv.CFG
        self.api = Api(cfg["TELEGRAM_API"], cfg["TELEGRAM_BOT_TOKEN"])
        self.users = Users(srv.HERE / "telegram_users.json", cfg["TELEGRAM_PASSWORD"], cfg["TELEGRAM_ADMINS"])
        self.chats, self.chats_lock = {}, threading.Lock()
        self.failed, self.failed_lock = {}, threading.Lock()  # user id -> [failures, locked until]
        # files of scans/prints that were open when the server last stopped (only this bot uses these prefixes)
        for prefix in ("tg-ready-", "tg-print-", "tg-scan-"):
            for old in Path(tempfile.gettempdir()).glob(prefix + "*"):
                shutil.rmtree(old, ignore_errors=True)

    # --- plumbing ------------------------------------------------------------------------------------------
    def chat(self, chat_id):
        with self.chats_lock:
            return self.chats.setdefault(chat_id, Chat())

    def send(self, chat_id, text, kb=None, **extra):
        params = {"chat_id": chat_id, "text": text, **extra}
        if kb is not None:
            params["reply_markup"] = kb
        return self.api.call("sendMessage", params)["message_id"]

    def edit(self, chat_id, msg_id, text, kb=None):
        """Edit a text message; if that is impossible (deleted, too old), send a new one. Returns the message id."""
        params = {"chat_id": chat_id, "message_id": msg_id, "text": text, "reply_markup": kb or NO_KEYBOARD}
        try:
            self.api.call("editMessageText", params)
            return msg_id
        except TelegramError as e:
            if "not modified" in str(e):
                return msg_id
            return self.send(chat_id, text, kb)

    def quiet(self, method, **params):
        """A call whose failure does not matter (deleting a message, removing buttons)."""
        try:
            self.api.call(method, params)
        except TelegramError:
            pass

    def send_file(self, chat_id, path, filename, caption="", kb=None, photo=False):
        data = path.read_bytes()
        ctype = "image/png" if data[:4] == b"\x89PNG" else "image/jpeg" if photo else "application/pdf"
        params = {"chat_id": chat_id, "caption": caption, "reply_markup": kb}
        method, field = ("sendPhoto", "photo") if photo else ("sendDocument", "document")
        return self.api.call(method, params, files=[(field, filename, data, ctype)], timeout=300)["message_id"]

    # --- main loop -----------------------------------------------------------------------------------------
    def run(self):
        offset, me = None, None
        while me is None:
            try:
                me = self.api.call("getMe")
                self.api.call("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in COMMANDS]})
            except TelegramError as e:
                log(f"cannot start yet: {e}" + (" (check TELEGRAM_BOT_TOKEN)" if e.code == 401 else ""))
                time.sleep(60 if e.code == 401 else 10)
        threading.Thread(target=self.warm_printer, daemon=True).start()
        log(f"bot @{me.get('username')} is running ({len(self.users.users)} registered user(s), "
            f"{len(self.users.admins)} admin(s))")
        while True:
            try:
                updates = self.api.call("getUpdates", {"timeout": POLL_TIMEOUT, "offset": offset,
                                                        "allowed_updates": ["message", "callback_query"]},
                                        timeout=POLL_TIMEOUT + 15)
            except TelegramError as e:
                if e.code == 409:
                    log("another program is reading this bot's messages (same token used twice?)")
                else:
                    log(f"polling failed: {e}")
                time.sleep(5)
                continue
            except Exception:
                traceback.print_exc()
                time.sleep(5)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                threading.Thread(target=self.dispatch, args=(u,), daemon=True).start()
            self.expire()

    def warm_printer(self):
        """Look the printer up once at start, so the first print settings message comes up quickly."""
        try:
            if self.srv.printer.configured():
                self.srv.printer.options()
        except printing.PrintError as e:
            log(f"printer not ready: {e}")

    def dispatch(self, update):
        msg, cb = update.get("message"), update.get("callback_query")
        where = msg or (cb or {}).get("message")
        if not where or where["chat"].get("type") != "private":
            return  # groups and inline buttons on other bots' messages are ignored
        if msg and time.time() - msg.get("date", 0) > STALE_UPDATE:
            return
        chat_id = where["chat"]["id"]
        c = self.chat(chat_id)
        # a quick tap right after another usually only waits a moment for it; a long scan or print means "busy"
        free = c.lock.acquire(timeout=0.7)
        if cb:  # answer at once: stops the button's spinner and says what is happening
            note = tap_note(cb.get("data")) if free else "⏳ Still busy with your previous step, one moment…"
            params = {"callback_query_id": cb["id"]}
            if note:
                params["text"] = note
            self.quiet("answerCallbackQuery", **params)
        elif msg.get("document") or msg.get("photo") or (msg.get("text") or "").split(" ")[0] in ("/scan", "/status"):
            self.quiet("sendChatAction", chat_id=chat_id, action="typing")  # "typing…" while the file is fetched
        if not free:
            c.lock.acquire()
        try:
            if cb:
                self.on_callback(c, chat_id, cb)
            else:
                self.on_message(c, chat_id, msg)
        except Exception as e:
            traceback.print_exc()
            self.quiet("sendMessage", chat_id=chat_id, text=f"Something went wrong: {e}")
        finally:
            c.lock.release()

    def expire(self):
        now = time.monotonic()
        for chat_id, c in list(self.chats.items()):
            if not c.lock.acquire(blocking=False):
                continue
            try:
                if c.scan and now - c.scan["last"] > SCAN_IDLE:
                    n = len(c.scan["pages"])
                    self.drop_scan(c, chat_id)
                    if n:
                        self.quiet("sendMessage", chat_id=chat_id,
                                   text=f"Your unfinished scan ({plural(n, 'page')}) was dropped after 30 minutes.")
                for tok, r in list(c.ready.items()):
                    if now - r["created"] > READY_IDLE:
                        self.drop_ready(c, tok)
                        self.quiet("editMessageReplyMarkup", chat_id=chat_id, message_id=r["msg"],
                                   reply_markup=self.after_scan_keyboard(None))
                for tok, job in list(c.prints.items()):
                    if now - job["created"] > PRINT_IDLE:
                        self.drop_print(c, tok)
                        self.quiet("editMessageText", chat_id=chat_id, message_id=job["msg"],
                                   text=f"⌛ {job['name']}: expired, send the file again to print it.")
            finally:
                c.lock.release()

    # --- messages ------------------------------------------------------------------------------------------
    def on_message(self, c, chat_id, msg):
        user = msg.get("from") or {}
        uid = user.get("id")
        text = (msg.get("text") or "").strip()
        cmd, arg = "", ""
        if text.startswith("/"):
            head, _, arg = text.partition(" ")
            cmd, arg = head.split("@")[0].lower(), arg.strip()
        if not self.users.allowed(uid):
            return self.stranger(c, chat_id, msg, cmd, arg, text)
        if cmd in ("/start", "/menu"):
            self.send_menu(c, chat_id, uid)
        elif cmd == "/help":
            self.send(chat_id, self.help_text(uid), keyboard([[button("☰ Menu", "m:menu")]]))
        elif cmd == "/register":
            self.quiet("deleteMessage", chat_id=chat_id, message_id=msg["message_id"])
            self.send_menu(c, chat_id, uid, "You are already registered.\n\n" + MENU_TEXT)
        elif cmd == "/scan":
            self.cmd_scan(c, chat_id, arg)
        elif cmd == "/status":
            self.cmd_status(chat_id)
        elif cmd == "/cancel":
            self.cmd_cancel(c, chat_id)
        elif cmd == "/logout":
            if uid in self.users.admins:
                self.send(chat_id, "You are an admin (TELEGRAM_ADMINS in config.json), so you can't log out here.")
            else:
                self.users.remove(str(uid))
                self.cmd_cancel(c, chat_id, quiet=True)
                self.send(chat_id, "Logged out. Send /register <password> to use the bot again.")
        elif cmd in ("/users", "/remove") and uid in self.users.admins:
            self.cmd_users(chat_id, cmd, arg)
        elif msg.get("document") or msg.get("photo"):
            self.receive_file(c, chat_id, msg)
        elif isinstance(c.awaiting, tuple) and text and not cmd:
            self.set_range(c, chat_id, text)
        elif cmd:
            self.send_menu(c, chat_id, uid, "I don't know that command. Use the buttons:")
        else:
            self.send_menu(c, chat_id, uid)

    def stranger(self, c, chat_id, msg, cmd, arg, text):
        if not self.users.password:
            return self.send(chat_id, f"This bot is private. Ask the admin to add your Telegram id: {msg['from']['id']}")
        if cmd == "/register":
            return self.register(c, chat_id, msg, arg)
        if c.awaiting == "password" and text and not cmd:
            return self.register(c, chat_id, msg, text)
        if cmd == "/start":
            c.awaiting = "password"
            return self.send(chat_id, "Hi! Send the password to start using this bot. You only need to do this once.")
        self.send(chat_id, "Send /register <password> to use this bot.")

    def register(self, c, chat_id, msg, password):
        user = msg["from"]
        uid = user["id"]
        self.quiet("deleteMessage", chat_id=chat_id, message_id=msg["message_id"])  # don't leave the password in the chat
        if not password:
            c.awaiting = "password"
            return self.send(chat_id, "Send the password now.")
        with self.failed_lock:
            count, until = self.failed.get(uid, [0, 0.0])
        wait = until - time.monotonic()
        if wait > 0:
            return self.send(chat_id, f"Too many wrong passwords. Try again in {int(wait // 60) + 1} minutes.")
        if hmac.compare_digest(password.encode(), self.users.password.encode()):
            with self.failed_lock:
                self.failed.pop(uid, None)
            self.users.add(user)
            c.awaiting = None
            log(f"registered {describe(uid, self.users.users[str(uid)])}")
            return self.send_menu(c, chat_id, uid, "✅ You're registered.\n\n" + MENU_TEXT)
        count = 0 if until else count  # a lockout that has ended starts the count again
        count += 1
        locked = count >= REGISTER_ATTEMPTS
        with self.failed_lock:
            self.failed[uid] = [count, time.monotonic() + REGISTER_LOCKOUT if locked else 0.0]
        log(f"wrong password from Telegram user {uid} ({count} in a row)")
        time.sleep(1)  # slows guessing a little
        c.awaiting = "password"
        self.send(chat_id, "Wrong password." + (" Too many tries: wait 10 minutes." if locked else " Try again."))

    def cmd_users(self, chat_id, cmd, arg):
        if cmd == "/remove":
            if not arg:
                return self.send(chat_id, "Usage: /remove <id or @username>")
            gone = self.users.remove(arg)
            if gone:
                log(f"admin removed {describe(*gone)}")
            return self.send(chat_id, f"Removed {describe(*gone)}." if gone else f"No registered user {arg}.")
        self.send(chat_id, self.users_text())

    def users_text(self):
        lines = [f"• {describe(uid, u)}, since {u.get('since', '?')}" for uid, u in self.users.users.items()]
        admins = ", ".join(str(a) for a in sorted(self.users.admins)) or "none"
        return ((f"👥 Registered ({len(lines)}):\n" + "\n".join(lines) if lines else "👥 Nobody has registered yet.")
                + f"\n\nAdmins (config.json): {admins}")

    def cmd_status(self, chat_id):
        msg = self.send(chat_id, "ℹ️ Status\n\n⏳ Checking the scanner and printer…")
        self.edit(chat_id, msg, self.status_text(), keyboard([[button("☰ Menu", "m:menu")]]))

    def status_text(self):
        srv = self.srv
        scanner = "busy (a scan is running)" if srv.scan_lock.locked() else "free"
        try:
            opts = srv.printer.options(fresh=True)
            printer = f"{opts['name']}, reachable"
        except printing.PrintError as e:
            printer = f"not available: {e}"
        return f"ℹ️ Status ({datetime.now():%H:%M:%S})\n\nScanner: {scanner}\nPrinter: {printer}"

    def cmd_cancel(self, c, chat_id, quiet=False):
        had = bool(c.scan or c.prints)
        if c.scan:
            self.drop_scan(c, chat_id)
        for tok, job in list(c.prints.items()):
            self.drop_print(c, tok)
            self.quiet("editMessageText", chat_id=chat_id, message_id=job["msg"], text=f"✖ {job['name']}: cancelled.")
        c.awaiting = None
        if not quiet:
            self.send(chat_id, "Cancelled." if had else "Nothing to cancel.")

    def on_callback(self, c, chat_id, cb):
        if not self.users.allowed(cb["from"]["id"]):
            return self.send(chat_id, "Send /register <password> to use this bot.")
        msg_id = cb["message"]["message_id"]
        parts = (cb.get("data") or "").split(":")
        if parts[0] == "m":
            self.menu_button(c, chat_id, msg_id, cb["from"]["id"], parts[1:])
        elif parts[0] == "s" and len(parts) == 3:
            self.scan_button(c, chat_id, msg_id, parts[1], parts[2])
        elif parts[0] == "r" and len(parts) == 2:
            self.print_ready(c, chat_id, parts[1])
        elif parts[0] == "p" and len(parts) == 3:
            self.print_button(c, chat_id, msg_id, parts[1], parts[2], cb["from"])

    # --- the button menu -----------------------------------------------------------------------------------
    def help_text(self, uid):
        return HELP + (ADMIN_HELP if uid in self.users.admins else "")

    def menu_keyboard(self, c, uid):
        p = c.scan_prefs
        rows = [[button("🖨 Print", "m:print"), button(f"📄 Scan ({p['dpi']} dpi, {MODE_TEXT[p['mode']]})", "m:scan")],
                [button("⚙️ Scan settings", "m:set"), button("ℹ️ Status", "m:status")],
                [button("❓ Help", "m:help")] + ([button("👥 Users", "m:users")] if uid in self.users.admins else [])]
        if c.scan or c.prints:
            rows.append([button("✖ Cancel what's running", "m:cancel")])
        return keyboard(rows)

    def send_menu(self, c, chat_id, uid, text=MENU_TEXT):
        return self.send(chat_id, text, self.menu_keyboard(c, uid))

    def settings_keyboard(self, c):
        p = c.scan_prefs
        return keyboard([
            [button(tick(d == p["dpi"]) + d, f"m:dpi:{d}") for d in DPIS],
            [button(tick(m == p["mode"]) + MODE_TEXT[m].capitalize(), f"m:mode:{m}") for m in ("color", "gray", "bw")],
            [button("📄 Scan now", "m:scan"), button("« Back", "m:back")]])

    PRINT_TEXT = """🖨 Print

Send me the file in this chat:
1. Tap 📎 (the paperclip next to the message box).
2. Choose File for a PDF, Word, Excel or PowerPoint file, or Gallery for a photo. (For a photo, "File" gives better quality.)
3. Send it. I'll show the print settings (copies, colour, pages, paper) and you tap Print.

You can also forward a file from another chat to me."""

    SETTINGS_TEXT = ("⚙️ Scan settings\n\nResolution in dpi: 200 is fine for documents, 300 for small print, "
                     "600 for photos (slow, big files).")

    def menu_button(self, c, chat_id, msg_id, uid, parts):
        action, arg = parts[0] if parts else "", parts[1] if len(parts) > 1 else ""
        back = keyboard([[button("« Back", "m:back")]])
        if action == "menu":  # from another message: a fresh menu at the bottom of the chat
            self.send_menu(c, chat_id, uid)
        elif action == "back":
            self.edit(chat_id, msg_id, MENU_TEXT, self.menu_keyboard(c, uid))
        elif action == "scan":
            if c.scan:  # a scan is open: add a page to it rather than dropping its pages
                c.scan["last"] = time.monotonic()
                self.scan_page(c, chat_id)
            else:
                self.begin_scan(c, chat_id)
        elif action == "print":
            self.edit(chat_id, msg_id, self.PRINT_TEXT, back)
        elif action == "set":
            self.edit(chat_id, msg_id, self.SETTINGS_TEXT, self.settings_keyboard(c))
        elif action in ("dpi", "mode"):
            if (action == "dpi" and arg in DPIS) or (action == "mode" and arg in MODE_TEXT):
                c.scan_prefs[action] = arg
            self.edit(chat_id, msg_id, self.SETTINGS_TEXT, self.settings_keyboard(c))
        elif action == "status":
            self.edit(chat_id, msg_id, "ℹ️ Status\n\n⏳ Checking the scanner and printer…")
            self.edit(chat_id, msg_id, self.status_text(),
                      keyboard([[button("🔄 Refresh", "m:status"), button("« Back", "m:back")]]))
        elif action == "help":
            self.edit(chat_id, msg_id, self.help_text(uid), back)
        elif action == "cancel":
            self.cmd_cancel(c, chat_id, quiet=True)
            self.edit(chat_id, msg_id, "Cancelled.\n\n" + MENU_TEXT, self.menu_keyboard(c, uid))
        elif uid in self.users.admins and action in ("users", "rm", "rmok"):
            self.users_button(chat_id, msg_id, action, arg)

    def users_button(self, chat_id, msg_id, action, arg):
        users = self.users.users
        if action == "rm" and arg in users:
            return self.edit(chat_id, msg_id, f"Remove {describe(arg, users[arg])}? They can register again with the password.",
                             keyboard([[button("Yes, remove", f"m:rmok:{arg}"), button("No", "m:users")]]))
        if action == "rmok" and arg in users:
            gone = self.users.remove(arg)
            if gone:
                log(f"admin removed {describe(*gone)}")
        rows = [[button(f"✖ {u.get('name') or uid}", f"m:rm:{uid}")] for uid, u in list(users.items())[:40]]
        rows.append([button("« Back", "m:back")])
        self.edit(chat_id, msg_id, self.users_text() + ("\n\nTap someone to remove them." if users else ""), keyboard(rows))

    # --- scanning ------------------------------------------------------------------------------------------
    def cmd_scan(self, c, chat_id, arg):
        for word in arg.lower().replace("dpi", " ").split():
            if word in DPIS:
                c.scan_prefs["dpi"] = word
            elif word in MODES:
                c.scan_prefs["mode"] = MODES[word]
            else:
                return self.send(chat_id, f"Don't know '{word}'. Example: /scan 300 gray (dpi {', '.join(DPIS)}; "
                                          "color, gray or bw)", keyboard([[button("⚙️ Scan settings", "m:set")]]))
        self.begin_scan(c, chat_id)

    def begin_scan(self, c, chat_id):
        dpi, mode = c.scan_prefs["dpi"], c.scan_prefs["mode"]
        if c.scan and c.scan["pages"]:
            self.send(chat_id, f"Starting a new scan; the unfinished one ({plural(len(c.scan['pages']), 'page')}) "
                               "was dropped.")
        if c.scan:
            self.drop_scan(c, chat_id)
        c.scan = {"sid": secrets.token_hex(3), "pages": [], "dpi": dpi, "mode": mode,
                  "last": time.monotonic(), "buttons": None}
        self.scan_page(c, chat_id)

    def scan_keyboard(self, s, failed=False):
        sid, n = s["sid"], len(s["pages"])
        rows = [[button("🔁 Try again" if failed else "➕ Scan next page", f"s:{sid}:next")]]
        if n:
            rows.append([button(f"✅ Finish as PDF ({plural(n, 'page')})", f"s:{sid}:finish")])
        rows.append([button("✖ Cancel", f"s:{sid}:cancel")])
        return keyboard(rows)

    def move_buttons(self, chat_id, s, msg_id):
        """Only the newest scan message carries the buttons."""
        if s["buttons"] and s["buttons"] != msg_id:
            self.quiet("editMessageReplyMarkup", chat_id=chat_id, message_id=s["buttons"], reply_markup=NO_KEYBOARD)
        s["buttons"] = msg_id

    def scan_page(self, c, chat_id):
        srv, s = self.srv, c.scan
        n = len(s["pages"]) + 1
        device, err = srv.pick_device()
        job = srv.start_scan(s["dpi"], s["mode"], device) if not err else None
        if job is None:
            text = f"Page {n}: {err}" if err else "The scanner is busy (someone else is scanning). Try again in a moment."
            msg = self.send(chat_id, text, self.scan_keyboard(s, failed=True))
            return self.move_buttons(chat_id, s, msg)
        self.move_buttons(chat_id, s, None)
        status = self.send(chat_id, f"⏳ Page {n}: {PHASES['preparing']}")
        phase, deadline = "preparing", time.monotonic() + srv.CFG["TIMEOUT"] * 3 + 120
        while True:
            info = srv.scan_jobs.get(job) or {"state": "error", "error": "the scan was lost"}
            if info["state"] != "running":
                break
            if time.monotonic() > deadline:
                info = {"state": "error", "error": "no answer from the scanner"}
                break
            if info.get("phase") != phase:
                phase = info.get("phase")
                status = self.edit(chat_id, status, f"⏳ Page {n}: {PHASES.get(phase, 'Scanning…')}")
            time.sleep(1)
        s["last"] = time.monotonic()
        if info["state"] != "done":
            status = self.edit(chat_id, status, f"❌ Page {n} failed: {info.get('error')}", self.scan_keyboard(s, True))
            return self.move_buttons(chat_id, s, status)
        s["pages"].append(info["id"])
        self.quiet("deleteMessage", chat_id=chat_id, message_id=status)
        caption = (f"Page {n} ({s['dpi']} dpi, {MODE_TEXT[s['mode']]}), scanned in {info.get('seconds', '?')} s."
                   + (f"\n{info['note']}" if info.get("note") else ""))
        page = srv.page_path(info["id"])
        try:
            msg = self.send_file(chat_id, page, f"page-{n}.jpg", caption, self.scan_keyboard(s), photo=True)
        except TelegramError:  # e.g. a 600 dpi page is too large for a photo; send it as a file instead
            msg = self.api.call("sendDocument", {"chat_id": chat_id, "caption": caption,
                                                 "reply_markup": self.scan_keyboard(s)},
                                files=[("document", f"page-{n}.jpg", page.read_bytes(), "image/jpeg")],
                                timeout=300)["message_id"]
        self.move_buttons(chat_id, s, msg)

    def scan_button(self, c, chat_id, msg_id, sid, action):
        s = c.scan
        if not s or s["sid"] != sid:
            self.quiet("editMessageReplyMarkup", chat_id=chat_id, message_id=msg_id, reply_markup=NO_KEYBOARD)
            return self.send(chat_id, "That scan has ended. Send /scan to start a new one.")
        s["last"] = time.monotonic()
        if action == "next":
            self.scan_page(c, chat_id)
        elif action == "finish":
            self.finish_scan(c, chat_id)
        elif action == "cancel":
            n = len(s["pages"])
            self.drop_scan(c, chat_id)
            pages = f" ({plural(n, 'page')} deleted)" if n else ""
            self.send(chat_id, f"Scan cancelled{pages}.")

    def finish_scan(self, c, chat_id):
        srv, s = self.srv, c.scan
        if not s["pages"]:
            return
        self.move_buttons(chat_id, s, None)
        n = len(s["pages"])
        status = self.send(chat_id, f"⏳ Making the PDF ({plural(n, 'page')})…")
        name = f"Scan {datetime.now():%Y-%m-%d %H-%M}.pdf"
        tmp = Path(tempfile.mkdtemp(prefix="tg-scan-"))
        try:
            pdf = tmp / name
            ok, _, err = srv.build_pdf(list(s["pages"]), pdf)
            if not ok:
                status = self.edit(chat_id, status, f"❌ Making the PDF failed: {err}", self.scan_keyboard(s, True))
                return self.move_buttons(chat_id, s, status)
            saved = ""
            folder_name = srv.CFG["TELEGRAM_SAVE_FOLDER"]
            if folder_name:
                folder = srv.SAVE_DIR / srv.clean_name(folder_name, "Telegram")
                folder.mkdir(parents=True, exist_ok=True)
                target = srv.unique_path(folder, name)
                shutil.copyfile(pdf, target)
                saved = f"\nAlso saved on the scanner PC: {srv.rel_of(target)}"
            tok = self.keep_ready(c, pdf, name, n) if srv.printer.configured() else None
            kb = self.after_scan_keyboard(tok)
            if pdf.stat().st_size > MAX_UPLOAD:
                msg = self.edit(chat_id, status, f"The PDF is {pdf.stat().st_size // 2**20} MB, too big for Telegram "
                                "(50 MB)." + (saved or " Scan at a lower dpi."), kb)
            else:
                msg = self.send_file(chat_id, pdf, name, f"{plural(n, 'page')}.{saved}", kb)
                self.quiet("deleteMessage", chat_id=chat_id, message_id=status)
            if tok:
                c.ready[tok]["msg"] = msg
            self.drop_scan(c, chat_id)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def after_scan_keyboard(self, ready_tok):
        rows = [[button("🖨 Print it", f"r:{ready_tok}")]] if ready_tok else []
        return keyboard(rows + [[button("📄 New scan", "m:scan"), button("☰ Menu", "m:menu")]])

    def keep_ready(self, c, pdf, name, pages):
        """Keep a copy of a finished scan so its "Print it" button works for READY_IDLE."""
        keep = Path(tempfile.mkdtemp(prefix="tg-ready-"))
        shutil.copyfile(pdf, keep / "scan.pdf")
        tok = secrets.token_hex(4)
        c.ready[tok] = {"dir": keep, "pdf": keep / "scan.pdf", "name": name, "pages": pages,
                        "created": time.monotonic(), "msg": None}
        return tok

    def drop_ready(self, c, tok):
        r = c.ready.pop(tok, None)
        if r:
            shutil.rmtree(r["dir"], ignore_errors=True)

    def print_ready(self, c, chat_id, tok):
        r = c.ready.get(tok)
        if not r:
            return self.send(chat_id, "That scan is no longer kept for printing (after an hour, or the server restarted). "
                                      "Send me the PDF to print it.")
        r["created"] = time.monotonic()  # printing it again later keeps it a while longer
        status = self.send(chat_id, f"⏳ Opening the print settings for {r['name']}…")
        tmp = Path(tempfile.mkdtemp(prefix="tg-print-"))
        pdf = tmp / "print.pdf"
        shutil.copyfile(r["pdf"], pdf)  # the dialog's job owns (and later deletes) its own copy
        self.open_dialog(c, chat_id, r["name"], tmp, pdf, r["pages"], status)

    def drop_scan(self, c, chat_id):
        s, c.scan = c.scan, None
        if not s:
            return
        if s["buttons"]:
            self.quiet("editMessageReplyMarkup", chat_id=chat_id, message_id=s["buttons"], reply_markup=NO_KEYBOARD)
        for pid in s["pages"]:
            self.srv.page_path(pid).unlink(missing_ok=True)

    # --- printing ------------------------------------------------------------------------------------------
    def receive_file(self, c, chat_id, msg):
        srv = self.srv
        if not srv.printer.configured():
            return self.send(chat_id, f"Printing is not set up on the scanner PC: {srv.printer.problem}.")
        if msg.get("document"):
            doc = msg["document"]
            name = doc.get("file_name") or "file" + (mimetypes.guess_extension(doc.get("mime_type") or "") or "")
        else:
            doc = msg["photo"][-1]  # the largest size
            name = f"Photo {datetime.now():%Y-%m-%d %H-%M}.jpg"
        ext = Path(name).suffix.lower()
        kind = "pdf" if ext == ".pdf" else "image" if ext in IMAGE_EXTS else "office" if office_pdf.kind_of(name) else None
        if not kind:
            return self.send(chat_id, f"I can't print {ext or 'these'} files. Send a PDF, a picture, "
                                      "or a Word, Excel or PowerPoint file.")
        if (doc.get("file_size") or 0) > MAX_DOWNLOAD:
            return self.send(chat_id, f"{name} is {doc['file_size'] // 2**20} MB. Telegram only lets bots fetch files "
                                      "up to 20 MB; make it smaller (or split it) and send it again.")
        status = self.send(chat_id, f"⏳ Getting {name}…")
        tmp = Path(tempfile.mkdtemp(prefix="tg-print-"))
        try:
            src = tmp / f"source{ext}"  # the sender's file name is only shown, never used as a path
            info = self.api.call("getFile", {"file_id": doc["file_id"]})
            self.api.download(info["file_path"], src)
            options = srv.printer.options()
            pdf = src if kind == "pdf" or (kind == "image" and options["images"]) else tmp / "print.pdf"
            if kind == "image" and pdf != src:  # this printer only takes PDF
                status = self.edit(chat_id, status, f"⏳ Preparing {name}…")
                proc = srv.run_merge(pdf, [src])
                if proc.returncode != 0 or not pdf.exists():
                    raise RuntimeError(f"could not read the picture ({(proc.stderr or proc.stdout or '').strip()[:200]})")
            elif kind == "office":
                app = office_pdf.APP_NAMES[office_pdf.kind_of(name)]
                status = self.edit(chat_id, status, f"⏳ Opening {name} with {app}… (can take a minute)")
                office_pdf.to_pdf(src, pdf, srv.CFG["TIMEOUT"], srv.CFG["OFFICE_CMD"])
            pages = 1 if pdf.suffix.lower() != ".pdf" else printing.pdf_page_count(pdf)
        except (TelegramError, office_pdf.OfficeError, printing.PrintError, RuntimeError, OSError,
                subprocess.SubprocessError) as e:
            shutil.rmtree(tmp, ignore_errors=True)
            return self.edit(chat_id, status, f"❌ {name}: {e}")
        self.open_dialog(c, chat_id, name, tmp, pdf, pages, status)

    def open_dialog(self, c, chat_id, name, tmp, pdf, pages, status=None):
        """Show the print settings for a file ready to print (tmp is the job's folder, deleted when it ends)."""
        try:
            options = self.srv.printer.options()
        except printing.PrintError as e:
            shutil.rmtree(tmp, ignore_errors=True)
            text = f"❌ {name}: {e}"
            return self.edit(chat_id, status, text) if status else self.send(chat_id, text)
        settings = {**printing.DEFAULT_SETTINGS, **(c.settings or {}), "pages": None}
        if settings["media"] not in options["media"]:
            settings["media"] = options["media_default"]  # the printer's name for A4, or its default
        settings["copies"] = min(settings["copies"], options["copies_max"])
        tok = secrets.token_hex(4)
        job = {"name": name, "tmp": tmp, "pdf": pdf, "pages": pages, "settings": settings, "options": options,
               "msg": status, "created": time.monotonic()}
        c.prints[tok] = job
        text, kb = self.dialog_text(job), self.dialog_keyboard(tok, job)
        job["msg"] = self.edit(chat_id, status, text, kb) if status else self.send(chat_id, text, kb)

    def dialog_text(self, job, footer="Choose the settings, then tap Print."):
        pages = plural(job["pages"], "page") if job["pages"] else "? pages"
        return f"🖨 {job['name']}\n{pages} · {job['options']['name']}\n\n{footer}"

    def dialog_keyboard(self, tok, job):
        s, o = job["settings"], job["options"]
        b = lambda text, action: button(text, f"p:{tok}:{action}")
        rows = [[b("➖", "c-"), b(f"Copies: {s['copies']}", "noop"), b("➕", "c+")]]
        if o["color"]:
            rows.append([b(tick(s["color"]) + "Colour", "col1"), b(tick(not s["color"]) + "Grey", "col0")])
        if job["pages"] != 1:
            chosen = s["pages"]
            rows.append([b(tick(not chosen) + "All pages", "rall"),
                         b(f"✓ Pages {printing.ranges_text(chosen)}" if chosen else "Choose pages…", "rng")])
        if len(o["media"]) > 1:
            rows.append([b(tick(m == s["media"]) + printing.MEDIA_LABELS[m], f"m{i}") for i, m in enumerate(o["media"])])
        if o["orientation"]:
            rows.append([b(tick(s["orientation"] == k) + k.capitalize(), f"o{k}") for k in ("auto", "portrait", "landscape")])
        if o["quality"]:
            rows.append([b(tick(s["quality"] == q) + q.capitalize(), f"q{q}") for q in o["quality"]])
        if o["scaling"]:
            labels = {"fit": "Fit to page", "none": "Actual size"}
            rows.append([b(tick(s["scaling"] == k) + labels[k], f"s{k}") for k in o["scaling"]])
        rows.append([b("🖨 Print", "go"), b("✖ Cancel", "x")])
        return keyboard(rows)

    def print_button(self, c, chat_id, msg_id, tok, action, user):
        job = c.prints.get(tok)
        if not job:
            self.quiet("editMessageReplyMarkup", chat_id=chat_id, message_id=msg_id, reply_markup=NO_KEYBOARD)
            return self.send(chat_id, "That print has ended or expired. Send the file again to print it.")
        s, o = job["settings"], job["options"]
        if action in ("c-", "c+"):
            s["copies"] = max(1, min(o["copies_max"], s["copies"] + (1 if action == "c+" else -1)))
        elif action in ("col0", "col1"):
            s["color"] = action == "col1"
        elif action == "rall":
            s["pages"] = None
            if c.awaiting == ("range", tok):
                c.awaiting = None
        elif action == "rng":
            c.awaiting = ("range", tok)
            count = f" (1-{job['pages']})" if job["pages"] else ""
            return self.send(chat_id, f"Which pages of {job['name']}{count}? Reply with e.g. 1-3, 5",
                             {"force_reply": True, "input_field_placeholder": "1-3, 5"})
        elif action.startswith("m") and action[1:].isdigit() and int(action[1:]) < len(o["media"]):
            s["media"] = o["media"][int(action[1:])]
        elif action[:1] == "o" and action[1:] in ("auto", "portrait", "landscape"):
            s["orientation"] = action[1:]
        elif action[:1] == "q" and action[1:] in o["quality"]:
            s["quality"] = action[1:]
        elif action[:1] == "s" and action[1:] in o["scaling"]:
            s["scaling"] = action[1:]
        elif action == "x":
            self.drop_print(c, tok)
            return self.edit(chat_id, msg_id, f"✖ {job['name']}: cancelled.")
        elif action == "go":
            return self.do_print(c, chat_id, tok, user)
        else:
            return
        job["msg"] = self.edit(chat_id, msg_id, self.dialog_text(job), self.dialog_keyboard(tok, job))

    def set_range(self, c, chat_id, text):
        tok = c.awaiting[1]
        job = c.prints.get(tok)
        if not job:
            c.awaiting = None
            return self.send(chat_id, "That print has ended or expired. Send the file again to print it.")
        try:
            ranges = printing.parse_ranges(text, job["pages"])
        except ValueError as e:
            return self.send(chat_id, f"{e}. Try again, e.g. 1-3, 5 (or tap All pages).",
                             {"force_reply": True, "input_field_placeholder": "1-3, 5"})
        c.awaiting = None
        job["settings"]["pages"] = None if job["pages"] and ranges == [(1, job["pages"])] else ranges
        # show the dialog again below the reply, so it doesn't have to be scrolled to
        self.quiet("deleteMessage", chat_id=chat_id, message_id=job["msg"])
        job["msg"] = self.send(chat_id, self.dialog_text(job), self.dialog_keyboard(tok, job))

    def do_print(self, c, chat_id, tok, user):
        srv = self.srv
        job = c.prints.pop(tok)
        if c.awaiting == ("range", tok):
            c.awaiting = None
        s, o = dict(job["settings"]), job["options"]
        msg = self.edit(chat_id, job["msg"], self.dialog_text(job, "⏳ Sending to the printer…"))
        try:
            pdf = job["pdf"]
            if s["pages"] and not o["page_ranges"]:  # the printer can't pick pages: cut them out here
                pdf = self.cut_pages(job, s["pages"])
                s["pages"] = None
            who = user.get("username") or " ".join(filter(None, [user.get("first_name"), user.get("last_name")]))
            job_id = srv.printer.print_pdf(pdf, s, job["name"], who or str(user["id"]))
        except (printing.PrintError, RuntimeError, OSError, subprocess.SubprocessError) as e:
            c.prints[tok] = job  # keep the dialog so it can be tried again
            job["msg"] = self.edit(chat_id, msg, self.dialog_text(job, f"❌ Printing failed: {e}"),
                                   self.dialog_keyboard(tok, job))
            return
        c.settings = {k: v for k, v in job["settings"].items() if k != "pages"}
        log(f"{who or user['id']} printed {job['name']}" + (f" (job {job_id})" if job_id else ""))
        self.drop_print(c, None, job)
        number = f" (job {job_id})" if job_id else ""
        self.send(chat_id, f"✅ Sent {job['name']} to the printer{number}.\n{self.summary(job)}",
                  keyboard([[button("☰ Menu", "m:menu")]]))
        self.quiet("deleteMessage", chat_id=chat_id, message_id=msg)

    def cut_pages(self, job, ranges):
        srv = self.srv
        outdir = job["tmp"] / "pages"
        shutil.rmtree(outdir, ignore_errors=True)
        outdir.mkdir()
        imgs = srv.split_to_images(job["pdf"], outdir)
        wanted = [imgs[p - 1] for p in printing.selected_pages(ranges) if p <= len(imgs)]
        if not wanted:
            raise RuntimeError("none of those pages are in the file")
        out = job["tmp"] / "selected.pdf"
        proc = srv.run_merge(out, wanted)
        if proc.returncode != 0 or not out.exists():
            raise RuntimeError((proc.stderr or proc.stdout or "could not pick the pages").strip())
        return out

    def summary(self, job):
        s, o = job["settings"], job["options"]
        parts = [plural(s["copies"], "copy").replace("copys", "copies")]
        if o["color"]:
            parts.append("colour" if s["color"] else "grey")
        parts.append(f"pages {printing.ranges_text(s['pages'])}" if s["pages"] else "all pages")
        if s["media"] in o["media"]:
            parts.append(printing.MEDIA_LABELS[s["media"]])
        if o["orientation"] and s["orientation"] != "auto":
            parts.append(s["orientation"])
        if o["quality"]:
            parts.append(f"{s['quality']} quality")
        if o["scaling"]:
            parts.append("fit to page" if s["scaling"] == "fit" else "actual size")
        return " · ".join(parts)

    def drop_print(self, c, tok, job=None):
        job = job or c.prints.pop(tok, None)
        if job:
            shutil.rmtree(job["tmp"], ignore_errors=True)
        if tok and c.awaiting == ("range", tok):
            c.awaiting = None


def start(srv):
    """Start the bot in the background. Called by server.py when TELEGRAM_BOT_TOKEN is set."""
    cfg = srv.CFG
    if not cfg["TELEGRAM_PASSWORD"] and not cfg["TELEGRAM_ADMINS"]:
        log("not started: set TELEGRAM_PASSWORD (coworkers register with it) and/or TELEGRAM_ADMINS in config.json")
        return None
    bot = Bot(srv)
    threading.Thread(target=bot.run, daemon=True, name="telegram").start()
    return bot
