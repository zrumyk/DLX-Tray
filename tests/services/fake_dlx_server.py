"""A stand-in for ``deeplx.exe`` that only knows ``GET /``: used to test DLXProcess.

Takes DLX's flags (``-ip``, ``-port``, ``-token``). ``FAKE_DLX_MODE`` picks the behaviour:
``ok`` (default) serves ``GET /`` with the flags it got, ``exit`` exits with code 3 at once,
``hang`` never listens. ``FAKE_DLX_PIDFILE`` makes it write its pid there first.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("-ip", required=True)
    parser.add_argument("-port", type=int, required=True)
    parser.add_argument("-token", required=True)
    argv = sys.argv[1:]
    if "-token" in argv:
        token_index = argv.index("-token")
        if token_index + 1 < len(argv):
            # DLX treats a leading hyphen in the token as part of the value.
            argv[token_index : token_index + 2] = [f"-token={argv[token_index + 1]}"]
    args = parser.parse_args(argv)

    pidfile = os.environ.get("FAKE_DLX_PIDFILE")
    if pidfile:
        Path(pidfile).write_text(str(os.getpid()), encoding="ascii")
    mode = os.environ.get("FAKE_DLX_MODE", "ok")
    if mode == "exit":
        sys.exit(3)
    if mode == "hang":
        while True:
            time.sleep(60)

    banner = json.dumps({"ip": args.ip, "port": args.port, "token": args.token}).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(banner)))
            self.end_headers()
            self.wfile.write(banner)

        def log_message(self, format: str, *args: object) -> None:
            pass

    ThreadingHTTPServer((args.ip, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
