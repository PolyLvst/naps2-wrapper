"""Print PDFs and pictures. Standard library only. Two ways:

- SumatraPDF (default, Windows): the portable SumatraPDF.exe prints straight to a Windows printer with page ranges,
  copies, colour, paper, orientation and scaling, without changing the document.
- IPP (e.g. PaperCut Mobility Print's queues): a small binary request posted over HTTP. Only the two operations needed
  are built: Get-Printer-Attributes (what the printer supports) and Print-Job (send a PDF with settings).
"""
import http.client
import os
import re
import shlex
import ssl
import struct
import subprocess
import threading
import time
import zlib
from pathlib import Path
from urllib.parse import urlparse

PRINT_JOB, GET_PRINTER_ATTRIBUTES = 0x0002, 0x000B
OPERATION, JOB, END = 0x01, 0x02, 0x03
INTEGER, BOOLEAN, ENUM, RANGE = 0x21, 0x22, 0x23, 0x33
TEXT, NAME, KEYWORD, URI, CHARSET, LANGUAGE, MIME = 0x41, 0x42, 0x44, 0x45, 0x47, 0x48, 0x49

QUALITY = {"draft": 3, "normal": 4, "high": 5}
ORIENTATION = {"portrait": 3, "landscape": 4}  # "auto" sends nothing
# paper sizes worth offering, in the order they are shown; others the printer lists are left out.
# Standard IPP names first, then the Windows-driver names PaperCut Mobility Print reports.
MEDIA_LABELS = {
    "iso_a4_210x297mm": "A4", "ISO_A4": "A4",
    "na_letter_8.5x11in": "Letter", "NA_LETTER": "Letter",
    "om_folio_210x330mm": "F4", "na_foolscap_8.5x13in": "F4", "NA_FOOLSCAP": "F4",
    "na_legal_8.5x14in": "Legal", "NA_LEGAL": "Legal",
    "iso_a5_148x210mm": "A5", "ISO_A5": "A5",
    # Windows paper ids (DMPAPER_*), used with SumatraPDF; 14 is 8.5 x 13 in, which is F4/folio here
    "paper:9": "A4", "paper:1": "Letter", "paper:14": "F4", "paper:5": "Legal", "paper:11": "A5",
}
A4 = ("iso_a4_210x297mm", "ISO_A4", "paper:9")
DEFAULT_SETTINGS = {"copies": 1, "color": True, "pages": None, "media": "iso_a4_210x297mm",
                    "orientation": "auto", "quality": "normal", "scaling": "fit"}


class PrintError(Exception):
    pass


# --- IPP encoding ------------------------------------------------------------------------------------------
def _attr(tag, name, values):
    out = b""
    for i, v in enumerate(values if isinstance(values, list) else [values]):
        if tag in (INTEGER, ENUM):
            data = struct.pack(">i", v)
        elif tag == BOOLEAN:
            data = bytes([1 if v else 0])
        elif tag == RANGE:
            data = struct.pack(">ii", *v)
        else:
            data = str(v).encode("utf-8")
        key = name.encode() if i == 0 else b""  # further values of the same attribute have an empty name
        out += bytes([tag]) + struct.pack(">H", len(key)) + key + struct.pack(">H", len(data)) + data
    return out


def encode_request(op, request_id, operation_attrs, job_attrs=()):
    body = struct.pack(">BBHi", 2, 0, op, request_id) + bytes([OPERATION])
    body += b"".join(_attr(*a) for a in operation_attrs)
    if job_attrs:
        body += bytes([JOB]) + b"".join(_attr(*a) for a in job_attrs)
    return body + bytes([END])


def decode_response(data):
    """Return (status code, {group tag: {name: [values]}}). Repeated groups (several jobs) are merged, which is
    fine for the printer and Print-Job replies read here."""
    if len(data) < 9:
        raise PrintError("the printer sent an empty or broken IPP reply")
    status = struct.unpack(">H", data[2:4])[0]
    groups, current, name, i = {}, None, None, 8
    while i < len(data):
        tag = data[i]
        i += 1
        if tag == END:
            break
        if tag <= 0x0F:  # start of a new attribute group
            current = groups.setdefault(tag, {})
            continue
        if i + 2 > len(data):
            break
        n = struct.unpack(">H", data[i:i + 2])[0]
        key = data[i + 2:i + 2 + n].decode("utf-8", "replace")
        i += 2 + n
        n = struct.unpack(">H", data[i:i + 2])[0]
        raw = data[i + 2:i + 2 + n]
        i += 2 + n
        if key:
            name = key
        if current is None or name is None:
            continue
        if tag in (INTEGER, ENUM) and len(raw) == 4:
            value = struct.unpack(">i", raw)[0]
        elif tag == BOOLEAN and len(raw) == 1:
            value = bool(raw[0])
        elif tag == RANGE and len(raw) == 8:
            value = struct.unpack(">ii", raw)
        elif 0x40 <= tag <= 0x4A:
            value = raw.decode("utf-8", "replace")
        else:
            value = raw  # collections and other types are not used here
        current.setdefault(name, []).append(value)
    return status, groups


# --- talking to the printer --------------------------------------------------------------------------------
class IPPPrinter:
    def __init__(self, url, timeout=60):
        u = urlparse(url)
        if u.scheme not in ("http", "https", "ipp", "ipps") or not u.hostname:
            raise PrintError(f"PRINT_IPP_URL must look like http://host:9163/printers/NAME, not {url!r}")
        self.secure = u.scheme in ("https", "ipps")
        self.host, self.port = u.hostname, u.port or (443 if u.scheme == "https" else 631)
        self.path = u.path or "/"
        self.uri = f"{'ipps' if self.secure else 'ipp'}://{u.netloc}{self.path}"
        self.timeout = timeout
        self._id = 0
        self._lock = threading.Lock()

    def _next_id(self):
        with self._lock:
            self._id += 1
            return self._id

    def _post(self, body):
        if self.secure:  # local print servers use self-signed certificates
            conn = http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout,
                                               context=ssl._create_unverified_context())
        else:
            conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            conn.request("POST", self.path, body, {"Content-Type": "application/ipp"})
            resp = conn.getresponse()
            data = resp.read()
        except OSError as e:
            raise PrintError(f"cannot reach the printer at {self.host}:{self.port} ({e})")
        finally:
            conn.close()
        if resp.status != 200:
            raise PrintError(f"the printer answered HTTP {resp.status} {resp.reason}")
        status, groups = decode_response(data)
        if status >= 0x0100:
            msg = groups.get(OPERATION, {}).get("status-message", [""])[0]
            raise PrintError(f"the printer refused the request (IPP status 0x{status:04x}{': ' + msg if msg else ''})")
        return groups

    def _op_attrs(self):
        return [(CHARSET, "attributes-charset", "utf-8"), (LANGUAGE, "attributes-natural-language", "en"),
                (URI, "printer-uri", self.uri)]

    def attributes(self):
        body = encode_request(GET_PRINTER_ATTRIBUTES, self._next_id(), self._op_attrs() + [
            (KEYWORD, "requested-attributes", "all")])
        return self._post(body).get(0x04, {})  # printer-attributes group

    def print_pdf(self, pdf_bytes, job_name, user, job_attrs):
        ops = self._op_attrs() + [(NAME, "requesting-user-name", user[:255] or "telegram"),
                                  (NAME, "job-name", job_name[:255] or "Telegram"),
                                  (MIME, "document-format", "application/pdf")]
        body = encode_request(PRINT_JOB, self._next_id(), ops, job_attrs) + pdf_bytes
        groups = self._post(body)
        return groups.get(JOB, {}).get("job-id", ["?"])[0]


def options_from_attributes(attrs):
    """Which settings to offer, from a Get-Printer-Attributes reply."""
    def get(name, default=None):
        return attrs.get(name, default)

    formats = get("document-format-supported", [])
    if formats and "application/pdf" not in formats and "application/octet-stream" not in formats:
        raise PrintError("this printer queue does not accept PDF files (it takes: " + ", ".join(formats) + ")")
    copies = get("copies-supported", [(1, 1)])[0]
    color_modes = get("print-color-mode-supported", [])
    media_supported = get("media-supported", [])
    media, labels = [], set()
    for kw in MEDIA_LABELS:  # in display order, one entry per label (F4 has two common keywords)
        if kw in media_supported and MEDIA_LABELS[kw] not in labels:
            media.append(kw)
            labels.add(MEDIA_LABELS[kw])
    media = media[:4]
    # A4 first unless the printer has no A4 (its own default is often Letter, whatever paper is loaded)
    default_media = next((m for m in media if m in A4), get("media-default", [""])[0])
    qualities = [q for q, v in QUALITY.items() if v in get("print-quality-supported", [])]
    orientations = get("orientation-requested-supported", [])
    scalings = [s for s in ("fit", "none") if s in get("print-scaling-supported", [])]
    name = (get("printer-info") or get("printer-name") or get("printer-make-and-model") or ["printer"])[0]
    return {
        "name": name,
        "copies_max": copies[1] if isinstance(copies, tuple) else 1,
        "color": "color" in color_modes and "monochrome" in color_modes,
        "page_ranges": bool(get("page-ranges-supported", [False])[0]),
        "media": media,
        "media_default": default_media if default_media in media else (media[0] if media else None),
        "orientation": 3 in orientations and 4 in orientations,
        "quality": qualities if len(qualities) > 1 else [],
        "scaling": scalings if len(scalings) > 1 else [],
    }


def job_attributes(settings, options):
    """IPP job attributes for the chosen settings; anything the printer did not list is left out."""
    attrs = []
    if settings["copies"] > 1:
        attrs.append((INTEGER, "copies", settings["copies"]))
    if options["color"]:
        attrs.append((KEYWORD, "print-color-mode", "color" if settings["color"] else "monochrome"))
    if settings["pages"] and options["page_ranges"]:
        attrs.append((RANGE, "page-ranges", list(settings["pages"])))
    if settings["media"] in options["media"]:
        attrs.append((KEYWORD, "media", settings["media"]))
    if options["orientation"] and settings["orientation"] in ORIENTATION:
        attrs.append((ENUM, "orientation-requested", ORIENTATION[settings["orientation"]]))
    if settings["quality"] in options["quality"]:
        attrs.append((ENUM, "print-quality", QUALITY[settings["quality"]]))
    if settings["scaling"] in options["scaling"]:
        attrs.append((KEYWORD, "print-scaling", settings["scaling"]))
    return attrs


# what PRINT_CMD (test override) pretends the printer supports
CMD_OPTIONS = {"name": "test printer (PRINT_CMD)", "copies_max": 99, "color": True, "page_ranges": True,
               "media": ["iso_a4_210x297mm", "na_letter_8.5x11in", "om_folio_210x330mm"],
               "media_default": "iso_a4_210x297mm", "orientation": True,
               "quality": ["draft", "normal", "high"], "scaling": ["fit", "none"], "images": True}


# --- SumatraPDF ----------------------------------------------------------------------------------------------
SUMATRA_PLACES = [r"%LOCALAPPDATA%\SumatraPDF\SumatraPDF.exe", r"%USERPROFILE%\SumatraPDF\SumatraPDF.exe",
                  r"%ProgramFiles%\SumatraPDF\SumatraPDF.exe"]
SUMATRA_EXIT = {1: "SumatraPDF could not print the file"}
def windows_printer_info(name):
    """{'valid', 'name', 'color', 'papers'} straight from the Windows print spooler (winspool), in well under a
    second; asking through PowerShell/.NET took over 10 s on the scanner PC. name "" = the default printer."""
    import ctypes
    from ctypes import wintypes
    ws = ctypes.WinDLL("winspool.drv")
    ws.GetDefaultPrinterW.argtypes = [wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    ws.OpenPrinterW.argtypes = [wintypes.LPWSTR, ctypes.POINTER(wintypes.HANDLE), ctypes.c_void_p]
    ws.ClosePrinter.argtypes = [wintypes.HANDLE]
    caps = ws.DeviceCapabilitiesW
    caps.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.WORD, ctypes.c_void_p, ctypes.c_void_p]
    caps.restype = ctypes.c_int
    if not name:
        n = wintypes.DWORD(0)
        ws.GetDefaultPrinterW(None, ctypes.byref(n))
        buf = ctypes.create_unicode_buffer(n.value or 1)
        if not ws.GetDefaultPrinterW(buf, ctypes.byref(n)):
            return {"valid": False, "name": ""}
        name = buf.value
    handle = wintypes.HANDLE()
    if not ws.OpenPrinterW(name, ctypes.byref(handle), None):
        return {"valid": False, "name": name}
    ws.ClosePrinter(handle)
    DC_PAPERS, DC_COLORDEVICE = 2, 32
    n = caps(name, None, DC_PAPERS, None, None)
    papers = (wintypes.WORD * max(n, 0))()
    if n > 0:
        caps(name, None, DC_PAPERS, papers, None)
    return {"valid": True, "name": name, "color": caps(name, None, DC_COLORDEVICE, None, None) == 1,
            "papers": list(papers)}


def find_sumatra(configured=""):
    for place in ([configured] if configured else SUMATRA_PLACES):
        path = Path(os.path.expandvars(place))
        if path.is_file():
            return path
    return None


class SumatraPrinter:
    def __init__(self, exe, printer_name, timeout):
        self.exe, self.printer_name, self.timeout = exe, printer_name, timeout

    def info(self):
        try:
            info = windows_printer_info(self.printer_name)
        except (OSError, AttributeError) as e:  # AttributeError: no ctypes.WinDLL (not Windows)
            raise PrintError(f"cannot ask Windows about the printer ({e})")
        if not info.get("valid"):
            who = f'"{self.printer_name}"' if self.printer_name else "the default printer"
            raise PrintError(f"Windows has no printer {who} (PRINT_PRINTER in config.json)")
        return info

    def options(self):
        info = self.info()
        papers = {f"paper:{p}" for p in info.get("papers") or []}
        media = [m for m in MEDIA_LABELS if m in papers][:4]
        default = f"paper:{info.get('default')}"  # not reported by winspool; A4 is preferred anyway
        return {"name": info["name"], "copies_max": 999, "color": bool(info.get("color")), "page_ranges": True,
                "media": media, "media_default": next((m for m in media if m in A4), default if default in media else None),
                "orientation": True, "quality": [], "scaling": ["fit", "none"], "images": True}

    def print_file(self, path, settings, options):
        parts = [f"{a}-{b}" for a, b in settings["pages"] or []]
        if settings["copies"] > 1:
            parts.append(f"{settings['copies']}x")
        if options["color"]:
            parts.append("color" if settings["color"] else "monochrome")
        if settings["media"] in options["media"]:
            parts.append("paper=" + settings["media"].split(":")[1])
        if settings["orientation"] in ("portrait", "landscape"):
            parts.append(settings["orientation"])
        # "Actual size" = Sumatra's shrink: real size, only made smaller if it would not fit on the paper
        parts.append("fit" if settings["scaling"] == "fit" else "shrink")
        target = ["-print-to", self.printer_name] if self.printer_name else ["-print-to-default"]
        cmd = [str(self.exe), *target, "-print-settings", ",".join(parts), "-silent", str(path)]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=self.timeout,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            raise PrintError(f"SumatraPDF did not finish within {self.timeout} s")
        except OSError as e:
            raise PrintError(f"cannot run SumatraPDF: {e}")
        if proc.returncode != 0:
            raise PrintError(SUMATRA_EXIT.get(proc.returncode, f"SumatraPDF failed (exit code {proc.returncode})")
                             + " (is the file damaged or protected?)")
        return None  # Windows does not hand back a job number here


class Printing:
    """The configured printer: SumatraPDF (default), IPP (PRINT_ENGINE "ipp") or a command (PRINT_CMD, for tests)."""

    def __init__(self, engine="sumatra", ipp_url="", cmd="", timeout=120, sumatra_path="", printer_name=""):
        self.cmd, self.ipp, self.sumatra, self.problem = cmd, None, None, None
        if cmd:
            pass
        elif engine.lower() == "ipp":
            if ipp_url:
                self.ipp = IPPPrinter(ipp_url, timeout)
            else:
                self.problem = "PRINT_ENGINE is ipp but PRINT_IPP_URL is empty"
        else:
            exe = find_sumatra(sumatra_path)
            if exe:
                self.sumatra = SumatraPrinter(exe, printer_name, timeout)
            else:
                self.problem = (f"SumatraPDF.exe not found at {sumatra_path}" if sumatra_path
                                else "SumatraPDF.exe not found (set SUMATRA_PATH in config.json)")
        self._options, self._when = None, 0.0

    def configured(self):
        return bool(self.cmd or self.ipp or self.sumatra)

    def options(self, fresh=False):
        if self.cmd:
            return CMD_OPTIONS
        if not self.configured():
            raise PrintError(self.problem or "no printer is set up")
        if fresh or self._options is None or time.monotonic() - self._when > 600:
            if self.sumatra:
                self._options = self.sumatra.options()
            else:
                self._options = {**options_from_attributes(self.ipp.attributes()), "images": False}
            self._when = time.monotonic()
        return self._options

    def print_pdf(self, path, settings, job_name, user):
        """Print the file (a PDF, or a picture if options()['images']). Returns the job id, or None if unknown."""
        options = self.options()
        if self.sumatra:
            return self.sumatra.print_file(path, settings, options)
        if self.cmd:
            pages = ",".join(f"{a}-{b}" for a, b in settings["pages"] or []) or "all"
            fields = {"{in}": str(path), "{copies}": str(settings["copies"]), "{color}": "color" if settings["color"] else "gray",
                      "{pages}": pages, "{media}": settings["media"], "{orientation}": settings["orientation"],
                      "{quality}": settings["quality"], "{scaling}": settings["scaling"]}
            cmd = []
            for part in shlex.split(self.cmd):
                for k, v in fields.items():
                    part = part.replace(k, v)
                cmd.append(part)
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if proc.returncode != 0:
                raise PrintError((proc.stderr or proc.stdout or "print command failed").strip())
            return proc.stdout.strip() or "1"
        return self.ipp.print_pdf(path.read_bytes(), job_name, user, job_attributes(settings, options))


# --- PDFs ----------------------------------------------------------------------------------------------------
PAGE_RE = re.compile(rb"/Type\s*/Page(?![a-zA-Z])")


def pdf_page_count(path):
    """Number of pages, or None if it cannot be told. Counts page objects, also inside compressed object streams
    (most PDFs made since 2005 keep them there)."""
    data = path.read_bytes()
    count = len(PAGE_RE.findall(data))
    for m in re.finditer(rb"<<((?:(?!>>\s*stream).){0,2000}?/Type\s*/ObjStm.{0,2000}?)>>\s*stream\r?\n", data, re.S):
        if b"/FlateDecode" not in m.group(1):
            continue
        try:
            count += len(PAGE_RE.findall(zlib.decompressobj().decompress(data[m.end():m.end() + 20_000_000])))
        except zlib.error:
            continue
    return count or None


def parse_ranges(text, page_count=None):
    """'1-3, 5' -> [(1, 3), (5, 5)]: sorted, merged. Raises ValueError with a message for the user."""
    ranges = []
    for part in re.split(r"[,;\s]+", text.strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            raise ValueError(f"'{part}' is not a page or range like 2 or 1-3")
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        if a < 1 or b < a:
            raise ValueError(f"'{part}' is not a valid range")
        if page_count and b > page_count:
            raise ValueError(f"the file has only {page_count} page{'s' if page_count != 1 else ''}")
        ranges.append((a, b))
    if not ranges:
        raise ValueError("no pages given")
    ranges.sort()
    merged = [ranges[0]]
    for a, b in ranges[1:]:
        if a <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    return merged


def ranges_text(ranges):
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in ranges)


def selected_pages(ranges):
    return [p for a, b in ranges for p in range(a, b + 1)]
