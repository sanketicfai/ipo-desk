#!/usr/bin/env python3
"""Is this folder safe to drag into github.com?

Old GitHub's web uploader rejects any single file over 25 MB ("Yowza, that's a big file").
Your dashboard folder looks tiny, but it usually hides one huge file: ipo_desk.db -
the SQLite database the desk writes on every run. It must never be uploaded.

    python check_upload.py            # checks the folder this script sits in
    python check_upload.py C:\\path\\to\\ipo-desk
"""
import ast
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



def audit_imports(root):
    """Catch the classic 'works on my PC, ModuleNotFoundError in the cloud' miss:
    a .py file imports a module that is not in this folder and is not installed
    by requirements.txt."""
    modules = {f[:-3] for f in os.listdir(root) if f.endswith(".py")}
    reqs = ""
    rp = os.path.join(root, "requirements.txt")
    if os.path.exists(rp):
        reqs = open(rp, encoding="utf-8").read().lower()
    supplied = {"requests", "bs4", "beautifulsoup4", "openpyxl", "lxml", "pandas", "yaml", "pyyaml"}
    missing = {}
    for f in sorted(os.listdir(root)):
        if not f.endswith(".py"):
            continue
        try:
            tree = ast.parse(open(os.path.join(root, f), encoding="utf-8").read())
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            for n in names:
                if n in modules or n in sys.stdlib_module_names or n in supplied:
                    continue
                if n.split(".")[0] in reqs:
                    continue
                missing.setdefault(n, set()).add(f)
    return missing


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

    missing_mods = audit_imports(root)
    if missing_mods:
        print("Modules imported by this folder's python files but NOT here and not installed")
        print("by requirements.txt - the cloud job would crash with ModuleNotFoundError:")
        for mod, users in sorted(missing_mods.items()):
            print(f"  {mod}.py  <- imported by {', '.join(sorted(users))}")
        print()

    if too_big or missing_mods:
        print("VERDICT: upload blocked - fix the list above first:")
        print("  * a file that must not be uploaded -> move it out of the folder (or delete it);")
        print("  * a module that is missing        -> copy <module>.py in from the project folder.")
        print("  Then drag the folder's CONTENTS into github.com again.")
        return 1
    if big:
        print("VERDICT: under the hard limit, but these files are unnecessary - leave them out.")
        return 0
    print("VERDICT: safe to drag into github.com.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
