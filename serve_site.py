#!/usr/bin/env python3
"""Tiny static server for the exported dashboard - for checking the online build locally.

    python serve_site.py site 8080

Adds CORS + no-cache headers so the page also works when it is embedded in an iframe
or opened from another origin. Any real static host does this for you in production.
"""
import functools
import os
import socketserver
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def log_message(self, *a):
        pass


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "site"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8080
    where = os.path.abspath(root)
    os.chdir(root)
    srv = ThreadingHTTPServer(("0.0.0.0", port), functools.partial(Handler))
    print(f"serving {where} at http://0.0.0.0:{port}/", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
