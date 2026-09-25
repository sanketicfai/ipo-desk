@echo off
REM Double-click: copies only the files GitHub needs into ..\ipo-desk-upload\ipo-desk
REM (your big ipo_desk.db is left behind) and opens that folder for you.
cd /d "%~dp0"
python make_upload_folder.py
echo.
pause
