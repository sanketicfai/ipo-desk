#!/bin/sh
cd "$(dirname "$0")"
python3 -c "import requests, bs4" 2>/dev/null || python3 -m pip install -r requirements.txt
python3 ipo_desk.py
