# naps2-wrapper

A PDF24-style web page: press **Scan** to add a page from the flatbed (glass) using NAPS2's console CLI, arrange pages
(drag and drop, undo), then download the PDF or save it into a folder on the scanner PC. Saved PDFs can be opened
again to add or reorder pages. Python standard library only, no `pip install`, and it works offline (Tailwind and
Font Awesome are bundled in `static/`; copy that folder along with `server.py`, `epson_scan.py`, `pause_apps.py` and `index.html`).

Made by [PolyLvst](https://github.com/PolyLvst).

## Windows setup
1. Install [NAPS2](https://www.naps2.com/) and Python 3 (Epson Scan 2 / the L3110 driver must already work).
2. Find the scanner name:
   ```
   "C:\Program Files\NAPS2\NAPS2.Console.exe" --noprofile --driver wia --listdevices
   ```
   (Try `--driver twain` if WIA doesn't list it.)
3. Copy `config.example.json` to `config.json` and set `SCANNER_DRIVER`. The page has a **Scanner** dropdown filled from `--listdevices`, so `SCANNER_DEVICE` is only needed as a default.
4. Sanity check by hand:
   ```
   "C:\Program Files\NAPS2\NAPS2.Console.exe" -o test.pdf -f --noprofile --driver wia --device "EPSON L3110 Series" --source glass --dpi 300
   ```
5. Run `run.bat`, then open `http://localhost:8080` (or `http://<pc-ip>:8080` from another device).

## Faster scanning with Epson Scan 2 (optional, Epson scanners)
NAPS2's WIA driver took about 25 s for a 150 dpi colour page on an Epson L3110; Epson's own driver takes about 11 s.
Epson Scan 2 has no scanning command line, so `epson_scan.py` opens its window, picks Image Type, Resolution and
JPEG in the dropdowns, clicks **Scan**, and collects the file it saves. To turn it on, put this in `config.json`:
```json
{ "SCAN_ENGINE": "epson" }
```
- The server must run in the logged-in desktop session (the logon task does); the window is visible on that PC while scanning.
- The window opens on the first scan (about 5 s), stays open between scans, and closes after 2 minutes idle.
- After each page the scanner is busy for about 10 s (head returning); the server keeps re-clicking until Epson accepts the click.
- Epson Scan 2 saves into your Documents folder and the server moves each file away immediately.
- It changes Epson Scan 2's last-used settings (image type, resolution, format).
- If Epson Scan 2 fails, the page is scanned with NAPS2/WIA instead and a note says so.
- Never kill `es2utility.exe` / `es2launcher.exe` by force: that left the scanner unreachable until it was power-cycled.
- Tested with Epson Scan 2 on Windows 10 and an L3110; other versions may label controls differently.

## How the page follows a scan
`POST /scan` starts a background job. The server pushes its progress over a WebSocket (`/ws`: opening Epson Scan 2,
waiting for the scanner, scanning, done or error), and the page also polls `GET /scan/<job>` as a fallback (every
1.5 s while the socket is down, every 5 s while it is up), so a dropped socket cannot lose a scan. The scanner list
works the same way. Only the "scanning" stage is timed for the estimates. If the Epson engine fails, the page is
scanned with NAPS2/WIA and a message says so. Only pages served by this server may open the socket (Origin check).

## Login (optional)
Add both to `config.json` to require a sign-in page before anything else works:
```json
{ "AUTH_USER": "alice", "AUTH_PASSWORD": "a long password", "AUTH_SESSION_HOURS": 12 }
```
With both empty (the default) there is no login. Setting only one of them stops the server at startup with an error.
- A login lasts `AUTH_SESSION_HOURS` (default 12) and is forgotten when the server restarts. File > **Log out** ends it.
- After 5 wrong attempts from one address, that address is locked out for 60 s.
- The session cookie is `HttpOnly` and `SameSite=Strict`. Pages, API calls and the WebSocket all require it.
- **There is no HTTPS**: the password and cookie travel in clear text over the LAN. Treat this as a door lock for a trusted
  network, not as protection on the internet; for that, put the page behind a TLS reverse proxy.
- The password is stored as plain text in `config.json`; keep that file private.

## Free the CPU while scanning (optional)
A video viewer such as the Synology Surveillance Station client can double a scan's time on a slow PC. List it in
`config.json` and the server closes it when a scan starts and starts it again after the scanner has been idle:
```json
{
  "PAUSE_APPS": [{
    "exe": "C:\\Program Files\\Synology\\SynologySurveillanceStationClient\\bin\\Synology Surveillance Station Client.exe",
    "processes": ["Synology Surveillance Station Client.exe", "NativeUI.exe"]
  }],
  "PAUSE_RESUME_AFTER": 120
}
```
- Apps are asked to close first (like clicking X) and force-closed only if still running after 8 s.
- An app is only reopened if it was running and we closed it; one you had closed yourself is left alone.
- The page shows "Closing apps that slow the scan down…" while it waits (not counted in the time estimate).
- If closing fails, the error goes to `server.log` and the scan continues.

## Autostart
Double-click `install-task.bat` (as the user who is logged in at the scanner PC). It creates the `naps2-wrapper` task
(trigger **At log on**, action `run-server.bat` without a console window, output in `server.log`) and starts it now. `install-task.bat uninstall`
removes it. Doing it by hand in Task Scheduler works too.
(Use a logon task rather than a Windows service: WIA/TWAIN generally need an interactive session.)

## LAN access
Allow inbound TCP 8080 in Windows Firewall. Without a login (see below) there is no authentication, so only expose it on a trusted network.

## Testing without a scanner
```
SCAN_CMD="python3 stub_scan.py scan {out}" MERGE_CMD="python3 stub_scan.py merge {out} {inputs}" \
OPEN_CMD="python3 stub_scan.py split {in} {outdir}" python3 server.py
```

## Config
`config.json` keys or environment variables of the same name: `NAPS2_PATH`, `SCANNER_DRIVER`, `SCANNER_DEVICE`, `SCAN_ENGINE` (`wia` default, or `epson`), `AUTH_USER` / `AUTH_PASSWORD` / `AUTH_SESSION_HOURS`, `PAUSE_APPS` / `PAUSE_RESUME_AFTER`, `EPSON_SCAN2_PATH` (path to `es2launcher.exe`; empty = usual Program Files folders), `PORT`, `HOST`, `TIMEOUT`, `SAVE_DIR` (default `scans/`), and the test overrides `SCAN_CMD`, `MERGE_CMD`, `OPEN_CMD`.

## NAPS2 commands the server runs
Checked against NAPS2 8.4.1 on Windows with an Epson L3110 (`-n 0` makes `-i` import only, without scanning):
- Scan one page: `NAPS2.Console.exe --noprofile --driver wia -o page.jpg -f -n 1 --source glass --dpi 300 --bitdepth color --device "EPSON L3110 Series"`
- Combine pages into a PDF: `NAPS2.Console.exe --noprofile -n 0 -i "a.jpg;b.jpg" -o out.pdf -f`
- Split a PDF into page images (for "open in workspace"): `NAPS2.Console.exe --noprofile -n 0 -i in.pdf -o "page$(nnnn).jpg" -f`
