@echo off
rem Registers (or removes) the "naps2-wrapper" Task Scheduler task that starts run-server.bat at log on.
rem   install-task.bat             create the task and start it now
rem   install-task.bat uninstall   stop and delete the task
rem /delay 0000:20 waits 20 s after log on so the desktop is ready. conhost --headless keeps the console window from showing. A logon task (not a service) because WIA/TWAIN and Epson Scan 2 need the interactive desktop session.
setlocal
set TASK=naps2-wrapper

if /i "%~1"=="uninstall" (
    schtasks /end /tn %TASK% >nul 2>&1
    schtasks /delete /tn %TASK% /f
    echo Note: a server that is already running keeps running until you stop it.
    exit /b %errorlevel%
)

schtasks /create /tn %TASK% /tr "conhost.exe --headless \"%~dp0run-server.bat\"" /sc onlogon /delay 0000:20 /ru "%USERNAME%" /it /rl limited /f
if errorlevel 1 (
    echo Could not create the task.
    exit /b 1
)
schtasks /run /tn %TASK%
echo.
echo Task "%TASK%" will start the server at every log on of %USERNAME%. Output goes to server.log.
echo The page needs about 45 seconds to answer after starting. Remove the task with: install-task.bat uninstall
