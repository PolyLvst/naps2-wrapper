"""Turn Word / Excel / PowerPoint files into PDF with the Microsoft Office installed on this PC (Windows only).

Office is driven through COM from a small PowerShell script, in the background (no window). Only the Office
processes the script itself started are ever ended, so a Word or Excel you have open is left alone.
"""
import os
import shlex
import subprocess
import tempfile
import threading
from pathlib import Path

KINDS = {
    "word": {".doc", ".docx", ".docm", ".dot", ".dotx", ".rtf", ".odt"},
    "excel": {".xls", ".xlsx", ".xlsm", ".xlsb", ".ods", ".csv"},
    "powerpoint": {".ppt", ".pptx", ".pptm", ".pps", ".ppsx", ".odp"},
}
APP_NAMES = {"word": "Word", "excel": "Excel", "powerpoint": "PowerPoint"}

# $In/$Out are passed as parameters, never pasted into the script text.
SCRIPT = r"""
param([string]$In, [string]$Out, [string]$Kind, [string]$PidFile)
$ErrorActionPreference = 'Stop'
$proc = @{ word = 'WINWORD'; excel = 'EXCEL'; powerpoint = 'POWERPNT' }[$Kind]
$before = @(Get-Process -Name $proc -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
$app = $null
try {
  if ($Kind -eq 'word') { $app = New-Object -ComObject Word.Application }
  elseif ($Kind -eq 'excel') { $app = New-Object -ComObject Excel.Application }
  else { $app = New-Object -ComObject PowerPoint.Application }
  # remember which Office process is ours, so the server can end it (and only it) if this hangs
  $mine = @(Get-Process -Name $proc -ErrorAction SilentlyContinue | Where-Object { $before -notcontains $_.Id } | ForEach-Object { $_.Id })
  Set-Content -Path $PidFile -Value ($mine -join ' ')
  if ($Kind -eq 'word') {
    $app.Visible = $false
    $app.DisplayAlerts = 0
    $app.AutomationSecurity = 3   # macros off
    # Open(FileName, ConfirmConversions, ReadOnly, AddToRecentFiles, PasswordDocument): a wrong dummy password
    # makes a protected file fail at once instead of showing a password prompt nobody can see
    $doc = $app.Documents.Open($In, $false, $true, $false, 'x-no-password')
    try { $doc.ExportAsFixedFormat($Out, 17) } finally { $doc.Saved = $true; $doc.Close() }
  } elseif ($Kind -eq 'excel') {
    $app.Visible = $false
    $app.DisplayAlerts = $false
    $app.AskToUpdateLinks = $false
    $app.AutomationSecurity = 3
    # Open(Filename, UpdateLinks, ReadOnly, Format, Password, WriteResPassword, IgnoreReadOnlyRecommended)
    $wb = $app.Workbooks.Open($In, 0, $true, [Type]::Missing, 'x-no-password', 'x-no-password', $true)
    try { $wb.ExportAsFixedFormat(0, $Out) } finally { $wb.Close($false) }
  } else {
    $app.DisplayAlerts = 1        # ppAlertsNone
    $app.AutomationSecurity = 3
    # Open(FileName, ReadOnly, Untitled, WithWindow): no window
    $p = $app.Presentations.Open($In, -1, 0, 0)
    try { $p.SaveAs($Out, 32) } finally { $p.Close() }
  }
} finally {
  if ($app -ne $null) {
    # PowerPoint runs only once per user: quit it only if this script started it
    $quit = ($Kind -ne 'powerpoint') -or ($before.Count -eq 0)
    if ($quit) { try { $app.Quit() } catch {} }
    [void][Runtime.InteropServices.Marshal]::ReleaseComObject($app)
  }
}
"""

_lock = threading.Lock()  # one conversion at a time: Office is heavy on this PC


class OfficeError(Exception):
    pass


def kind_of(name):
    ext = Path(name).suffix.lower()
    return next((k for k, exts in KINDS.items() if ext in exts), None)


def to_pdf(src, out, timeout=120, test_cmd=""):
    """Convert src (an Office file) to the PDF out. Raises OfficeError with a message for the user."""
    kind = kind_of(src.name)
    if not kind:
        raise OfficeError(f"{src.suffix} is not an Office file")
    if test_cmd:
        cmd = [p.replace("{in}", str(src)).replace("{out}", str(out)) for p in shlex.split(test_cmd)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0 or not out.exists():
            raise OfficeError((proc.stderr or proc.stdout or "conversion failed").strip())
        return
    if os.name != "nt":
        raise OfficeError("Office files can only be converted on Windows")
    with _lock, tempfile.TemporaryDirectory(prefix="office-") as tmp:
        script, pid_file = Path(tmp) / "to_pdf.ps1", Path(tmp) / "pids.txt"
        script.write_text(SCRIPT, encoding="utf-8-sig")
        cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script),
               "-In", str(src.resolve()), "-Out", str(out.resolve()), "-Kind", kind, "-PidFile", str(pid_file)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            _end_office(pid_file)
            raise OfficeError(f"{APP_NAMES[kind]} took longer than {timeout} s (the file may ask for a password "
                              "or show a message); it was closed")
        except OSError as e:
            raise OfficeError(f"cannot run PowerShell: {e}")
        if proc.returncode != 0 or not out.exists():  # the script has already quit its Office instance
            detail = _short_error(proc.stderr or proc.stdout)
            if "80040154" in detail or "Class not registered" in detail:
                raise OfficeError(f"Microsoft {APP_NAMES[kind]} is not installed on the scanner PC")
            raise OfficeError(f"{APP_NAMES[kind]} could not open or convert this file"
                              f"{' (' + detail + ')' if detail else ''}")


def _short_error(text):
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    return lines[0][:200] if lines else ""


def _end_office(pid_file):
    """End the Office processes the script started (their ids are in pid_file), never any others."""
    try:
        pids = [p for p in pid_file.read_text(encoding="utf-8-sig").split() if p.isdigit()]
    except OSError:
        return
    for pid in pids:
        subprocess.run(["taskkill", "/pid", pid, "/t", "/f"], capture_output=True)
