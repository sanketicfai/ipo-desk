@echo off
REM Double-click this before dragging your folder into github.com.
REM It names any file that is too big to upload (usually ipo_desk.db).
cd /d "%~dp0"
python check_upload.py "%~1"
echo.
pause
