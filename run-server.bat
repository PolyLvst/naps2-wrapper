@echo off
cd /d "%~dp0"
python server.py >> server.log 2>&1
