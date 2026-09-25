@echo off
cd /d "%~dp0"
python -c "import requests, bs4" 2>nul || (echo Installing dependencies... & python -m pip install -r requirements.txt)
python ipo_desk.py
pause
