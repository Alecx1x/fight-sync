"""Test the share-link import endpoint (happy path + graceful failure).
Requires the server running. The sample is served from a plain local HTTP server
(no auth) so the server-side yt-dlp can fetch it like a real external link.
"""
import functools
import http.server
import socketserver
import subprocess
import threading
import time
from pathlib import Path

import requests

from media import FFMPEG, probe

BASE = "http://127.0.0.1:8765"
T = Path(__file__).parent / "_test"
T.mkdir(exist_ok=True)


def login(s):
    s.post(f"{BASE}/login",
           data={"password": open("fightsync-secret.txt").read().strip(), "next": "/"})


def serve(directory):
    handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                directory=str(directory))
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def make_sample(port):
    out = T / "meta_sample.mp4"
    subprocess.run(
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=3",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)], check=True)
    return f"http://127.0.0.1:{port}/meta_sample.mp4"


def run(s, url):
    jid = s.post(f"{BASE}/api/import_url",
                 data={"url": url, "name": "gameplay"}).json()["job_id"]
    last = None
    for _ in range(120):
        st = s.get(f"{BASE}/api/status/{jid}").json()
        if st.get("message") != last:
            print("   ", st["percent"], st["status"], st["message"]); last = st["message"]
        if st["status"] in ("done", "error"):
            return st
        time.sleep(1)
    raise SystemExit("timed out")


def main():
    s = requests.Session(); s.trust_env = False
    login(s)
    httpd, port = serve(T)
    try:
        print("== IMPORT (happy path) ==")
        st = run(s, make_sample(port))
        assert st["status"] == "done", f"import failed: {st.get('message')}"
        info = probe(st["result"]["final"])
        print(f"  imported {info.width}x{info.height} {info.duration:.1f}s")
        assert info.width > 0
        print("  PASS - link downloaded + staged as gameplay\n")

        print("== IMPORT (bad link -> graceful error) ==")
        st = run(s, "https://www.meta.com/this-is-not-a-real-clip-12345")
        print(f"  status: {st['status']}")
        assert st["status"] == "error", "bad link should report an error, not crash"
        print("  PASS - bad link handled gracefully\n")
        print("IMPORT TESTS PASSED")
    finally:
        httpd.shutdown()


if __name__ == "__main__":
    main()
