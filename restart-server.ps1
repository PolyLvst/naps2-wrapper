# Restarts the wrapper server on the Windows PC, the way a person would: stop the old server, start the
# "naps2-wrapper" scheduled task again (it runs in the logged-in desktop session, which Epson Scan 2 needs).
#
#   powershell -ExecutionPolicy Bypass -File restart-server.ps1            restart now
#   powershell -ExecutionPolicy Bypass -File restart-server.ps1 -DryRun    only show what it would stop
#
# Run it after a deploy that changed server.py, epson_scan.py, pause_apps.py or config.json (index.html and
# static/ are read on every request and need no restart). Do not run it while a page is being scanned: the scan is lost.
# It only ever stops the process listening on the port and its python parent, by PID, and only if they run server.py.
# Never `taskkill /im python.exe` and never kill Epson's es2*.exe: that once left the scanner unreachable.
param(
    [switch]$DryRun,
    [string]$Task = "naps2-wrapper",
    [int]$Port = 8080
)
$ErrorActionPreference = "Stop"

function Info($m) { Write-Host $m }
function Cmd($pid_) { (Get-CimInstance Win32_Process -Filter "ProcessId=$pid_" -ErrorAction SilentlyContinue) }

$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
$targets = @()
if ($listener) {
    $child = Cmd $listener.OwningProcess
    if ($child.Name -ne "python.exe" -or $child.CommandLine -notmatch "server\.py") {
        throw "Port $Port is held by '$($child.Name)' ($($child.CommandLine)), not the wrapper server. Not touching it."
    }
    $targets += $child
    # the python launcher starts the real server as a child; killing only the child leaves the launcher holding the port
    $parent = Cmd $child.ParentProcessId
    if ($parent -and $parent.Name -eq "python.exe" -and $parent.CommandLine -match "server\.py") { $targets += $parent }
    foreach ($t in $targets) { Info "server process: PID $($t.ProcessId)  $($t.CommandLine)" }
} else {
    Info "Nothing is listening on port $Port; starting the server only."
}

if ($DryRun) {
    Info "Dry run: would stop PID(s) $(($targets | ForEach-Object { $_.ProcessId }) -join ', ') and run task '$Task'."
    return
}

# stop the child first, then the launcher
foreach ($t in $targets) { Stop-Process -Id $t.ProcessId -Force -ErrorAction SilentlyContinue }

$deadline = (Get-Date).AddSeconds(20)
while ((Get-Date) -lt $deadline -and (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)) { Start-Sleep -Milliseconds 300 }
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) { throw "Port $Port is still in use after stopping the server." }

# the task ignores a new start while it is still marked Running (its cmd.exe wrapper is exiting)
$deadline = (Get-Date).AddSeconds(20)
while ((Get-Date) -lt $deadline) {
    $state = (schtasks /query /tn $Task /fo csv /nh) -replace '"', '' -split ',' | Select-Object -Last 1
    if ($state -ne "Running") { break }
    Start-Sleep -Milliseconds 500
}

schtasks /run /tn $Task | Out-Null
Info "Started task '$Task'; waiting for the page (about 45 s on this PC)..."

$deadline = (Get-Date).AddSeconds(120)
while ((Get-Date) -lt $deadline) {
    try {
        $r = Invoke-WebRequest -UseBasicParsing -Uri "http://localhost:$Port/" -TimeoutSec 5
        Info "Server is back: HTTP $($r.StatusCode)."
        return
    } catch { Start-Sleep -Seconds 2 }
}
Info "The server did not answer within 120 s. Last lines of server.log:"
Get-Content (Join-Path $PSScriptRoot "server.log") -Tail 15 -ErrorAction SilentlyContinue
exit 1
