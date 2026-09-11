#!/usr/bin/env python3
"""Tiny server for the closing-day dashboard (repo root, port 4173)."""
import http.server
import os
import socketserver

os.chdir(os.path.dirname(os.path.abspath(__file__)))

class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self.path = "/closing_day.html"
        return super().do_GET()

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

if __name__ == "__main__":
    with Server(("0.0.0.0", 4173), Handler) as httpd:
        print("Closing-day dashboard on http://0.0.0.0:4173", flush=True)
        httpd.serve_forever()
