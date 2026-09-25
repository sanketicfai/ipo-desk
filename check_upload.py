#!/usr/bin/env python3
"""Is this folder safe to drag into github.com?

Old GitHub's web uploader rejects any single file over 25 MB ("Yowza, that's a big file").
Your dashboard folder looks tiny, but it usually hides one huge file: ipo_desk.db -
the SQLite database the desk writes on every run. It must never be uploaded.

    python check_upload.py            # checks the folder this script sits in
    python check_upload.py C:\\path\\to\\ipo-desk
"""
import os
import sys

WEB_LIMIT = 25 * 1024 * 1024      # github.com drag & drop hard limit per file
WARN = 10 * 1024 * 1024
SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "venv"}

# names that belong to your machine only - never upload these
LEAVE_OUT = ("ipo_desk.db", "ipo_desk.db-wal", "ipo_desk.db-shm", "site", "ipo_data.json",
             "backup", "exports")


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
    root = os.path.abspath(root)
    print(f"checking {root}\n")

    files, total = [], 0
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        for f in fns:
            p = os.path.join(dp, f)
            try:
                sz = os.path.getsize(p)
            except OSError:
                continue
            files.append((sz, os.path.relpath(p, root)))
            total += sz

    files.sort(reverse=True)
    print(f"{len(files)} files, {human(total)} in total\n")

    too_big = [x for x in files if x[0] > WEB_LIMIT]
    big = [x for x in files if WARN < x[0] <= WEB_LIMIT]
    suspicious = [(sz, rel) for sz, rel in files
                  if os.path.basename(rel) in LEAVE_OUT or rel.split(os.sep)[0] in LEAVE_OUT]

    if big or too_big or suspicious:
        print("Files that should NOT be uploaded:")
        seen = set()
        for sz, rel in too_big + big + suspicious:
            if rel in seen:
                continue
            seen.add(rel)
            note = "  <<< TOO BIG for github.com" if sz > WEB_LIMIT else ""
            print(f"  {human(sz):>10}  {rel}{note}")
            if len(seen) >= 12:
                break
        print()

    print("Largest files here:")
    for sz, rel in files[:8]:
        print(f"  {human(sz):>10}  {rel}")
    print()

    if too_big:
        print("VERDICT: upload blocked. Move the flagged file(s) out of this folder (or delete them),")
        print("         then drag the folder's CONTENTS again. Do not rename them and upload anyway -")
        print("         the database is your PC's live data and is rebuilt/kept in the cloud by itself.")
        return 1
    if big:
        print("VERDICT: under the hard limit, but these files are unnecessary - leave them out.")
        return 0
    print("VERDICT: safe to drag into github.com.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
