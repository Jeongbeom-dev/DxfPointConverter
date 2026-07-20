@echo off
REM DXF 포인트 변환기 GUI 실행
cd /d "%~dp0"
python gui.py
if errorlevel 1 pause
