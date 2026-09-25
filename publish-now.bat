@echo off
REM Windows helper: build the static site into .\site from your existing database.
REM Useful for a quick check of what the cloud version will look like (open site\index.html
REM through a local web server, e.g.  python serve_site.py site 8080  then open http://localhost:8080 ).
cd /d "%~dp0"
python ipo_desk.py --once --static-dir site --budget 300
python serve_site.py site 8080
