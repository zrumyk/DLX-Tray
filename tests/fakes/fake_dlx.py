"""Scriptable loopback DLX server for pytest and manual failure checks."""

from __future__ import annotations

import argparse
import json
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Lock, Thread
from typing import cast
from urllib.parse import parse_qs, urlsplit

from dlx_tray.core.chunker import HARD_MAX_UNITS, utf16_len

DEFAULT_TOKEN = "fake-dlx"
ERRORS = {
    "401": (401, "Invalid access token"),
    "403": (403, "Forbidden"),
    "413": (413, "Text exceeds maximum length"),
    "429": (429, "Too many requests"),
    "429-retry-after": (429, "Too many requests"),
    "429-retry-after-15": (429, "Too many requests"),
    "503": (503, "Translation failed"),
}
RETRY_AFTER = {"429-retry-after": "9", "429-retry-after-15": "15"}
STEPS = ("ok", "ok-cache-hit", "reverse", "slow", "drop", *ERRORS)

type FakeDLXFactory = Callable[[Sequence[str]], AbstractContextManager[FakeDLX]]


class FakeDLX(ThreadingHTTPServer):
    def __init__(
        self,
        script: Sequence[str],
        *,
        token: str = DEFAULT_TOKEN,
        slow_seconds: float = 35.0,
    ) -> None:
        invalid = next((step for step in script if step not in STEPS), None)
        if invalid is not None:
            raise ValueError(f"Unknown DLX script step: {invalid!r}")
        self.token = token
        self.slow_seconds = slow_seconds
        self.stopping = Event()
        self._steps = deque(script)
        self.on_request: Callable[[list[str], str], str] | None = None
        self._lock = Lock()
        super().__init__(("127.0.0.1", 0), _Handler)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"

    def next_step(self) -> str:
        with self._lock:
            return self._steps.popleft() if self._steps else "ok"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        # The request URL may include the token.
        pass

    def _json(
        self,
        status: int,
        body: dict[str, object],
        *,
        retry_after: str | None = None,
        cache_hit: bool = False,
    ) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if retry_after is not None:
            self.send_header("Retry-After", retry_after)
        if cache_hit:
            self.send_header("X-DLX-Cache", "hit")
        try:
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # A client exercising a timeout may already have closed the socket.
            self.close_connection = True

    def _error(self, status: int, message: str, *, retry_after: str | None = None) -> None:
        self._json(status, {"code": status, "message": message}, retry_after=retry_after)

    def do_GET(self) -> None:
        if urlsplit(self.path).path == "/":
            self._json(200, {"code": 200, "message": "DLX Translation API, test server"})
        else:
            self._error(404, "Not found")

    def do_POST(self) -> None:
        url = urlsplit(self.path)
        if url.path != "/translate":
            self._error(404, "Not found")
            return
        server = cast(FakeDLX, self.server)
        token = server.token
        if self.headers.get("Authorization") not in (
            f"Bearer {token}",
            f"DeepL-Auth-Key {token}",
        ) and parse_qs(url.query).get("token") != [token]:
            self._error(401, "Invalid access token")
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(max(0, length)))
        except ValueError:
            self._error(400, "Invalid JSON")
            return
        if not isinstance(payload, dict):
            self._error(400, "Expected a JSON object")
            return
        text = payload.get("text", "")
        source = payload.get("source_lang", "auto")
        target = payload.get("target_lang")
        texts = [text] if isinstance(text, str) else text
        if (
            not isinstance(texts, list)
            or not all(isinstance(item, str) for item in texts)
            or not isinstance(source, str)
            or not isinstance(target, str)
            or not target
        ):
            self._error(400, "Expected text, source and target languages")
            return
        if not texts or (isinstance(text, str) and not text.strip()):
            self._error(404, "No text to translate")
            return
        units = sum(utf16_len(item) for item in texts if item.strip())
        if units > HARD_MAX_UNITS:
            self._error(
                413,
                f"text exceeds maximum length: {units} characters "
                f"(anonymous oneshot limit is {HARD_MAX_UNITS})",
            )
            return

        step = server.next_step()
        if server.on_request is not None:
            step = server.on_request(texts, step)
        if step in ERRORS:
            status, message = ERRORS[step]
            self._error(status, message, retry_after=RETRY_AFTER.get(step))
            return
        if step == "drop" or (step == "slow" and server.stopping.wait(server.slow_seconds)):
            self.close_connection = True
            return

        translated = [
            f"[{target}] {item[::-1] if step == 'reverse' else item}" if item.strip() else item
            for item in texts
        ]
        self._json(
            200,
            {
                "code": 200,
                "data": translated[0] if isinstance(text, str) else translated,
                "source_lang": "EN" if source.lower() == "auto" else source,
                "target_lang": target,
                "alternatives": None,
                "id": 123,
                "method": "Free",
            },
            cache_hit=step == "ok-cache-hit",
        )


@contextmanager
def running_fake_dlx(
    script: Sequence[str],
    *,
    token: str = DEFAULT_TOKEN,
    slow_seconds: float = 35.0,
) -> Iterator[FakeDLX]:
    with FakeDLX(script, token=token, slow_seconds=slow_seconds) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
        thread.start()
        try:
            yield server
        finally:
            server.stopping.set()
            server.shutdown()
            thread.join()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--script", default="ok", help=f"Comma-separated steps: {', '.join(STEPS)}")
    parser.add_argument("--token", default=DEFAULT_TOKEN, help="Expected test token")
    args = parser.parse_args()
    script = [step.strip() for step in args.script.split(",")]
    try:
        server = FakeDLX(script, token=args.token)
    except ValueError as exc:
        parser.error(str(exc))

    with server:
        print(f"DLX_DEEPLX_URL={server.url}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.stopping.set()


if __name__ == "__main__":
    main()
