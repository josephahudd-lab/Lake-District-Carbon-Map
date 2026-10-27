#!/usr/bin/env python3
"""
server.py - Lightweight static HTTP server with Byte-Range request support (HTTP 206)
Enables local PMTiles streaming with MapLibre GL.
"""

import http.server
import socketserver
import os
import sys

PORT = 8088
DIRECTORY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")

class RangeRequestHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def send_head(self):
        if "Range" not in self.headers:
            self.range = None
            return super().send_head()
        
        path = self.translate_path(self.path)
        if not os.path.isfile(path):
            return super().send_head()

        f = open(path, "rb")
        total_len = os.fstat(f.fileno()).st_size
        range_header = self.headers["Range"]
        try:
            val = range_header.strip().split("=")[1]
            start_str, end_str = val.split("-")
            start = int(start_str) if start_str else 0
            end = int(end_str) if end_str else total_len - 1
        except Exception:
            self.send_error(400, "Invalid Range Header")
            f.close()
            return None

        if start >= total_len or end >= total_len or start > end:
            self.send_error(416, "Requested Range Not Satisfiable")
            self.send_header("Content-Range", f"bytes */{total_len}")
            self.end_headers()
            f.close()
            return None

        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{total_len}")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Last-Modified", self.date_time_string(os.path.getmtime(path)))
        self.end_headers()
        f.seek(start)
        self.range_len = end - start + 1
        return f

    def copyfile(self, source, outputfile):
        if not hasattr(self, "range_len") or self.range_len is None:
            return super().copyfile(source, outputfile)
        
        remaining = self.range_len
        bufsize = 64 * 1024
        while remaining > 0:
            chunk = source.read(min(remaining, bufsize))
            if not chunk:
                break
            outputfile.write(chunk)
            remaining -= len(chunk)

def run():
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("0.0.0.0", PORT), RangeRequestHandler) as httpd:
        print(f"Serving {DIRECTORY} on http://localhost:{PORT}")
        httpd.serve_forever()

if __name__ == "__main__":
    run()
