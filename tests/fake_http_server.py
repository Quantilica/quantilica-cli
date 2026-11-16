"""Servidor HTTP local para testes de tráfego: mede simultaneidade e chegadas."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
BODY = b"a;b\n" + b"1;2\n" * 1024


class FakeServer:
    """Registra o instante de cada requisição e o máximo de simultâneas."""

    def __init__(self, *, body_delay: float = 0.15) -> None:
        self.body_delay = body_delay
        self.arrivals: list[tuple[float, str, str]] = []
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        self.responder: Callable[[BaseHTTPRequestHandler, str, str], bool] | None = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:
                pass

            def _handle(self, method: str) -> None:
                with outer.lock:
                    outer.arrivals.append((time.monotonic(), method, self.path))
                    outer.active += 1
                    outer.max_active = max(outer.max_active, outer.active)
                try:
                    if outer.responder is not None and outer.responder(
                        self, method, self.path
                    ):
                        return
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(BODY)))
                    self.end_headers()
                    if method == "HEAD":
                        return
                    half = len(BODY) // 2
                    self.wfile.write(BODY[:half])
                    self.wfile.flush()
                    time.sleep(outer.body_delay)
                    self.wfile.write(BODY[half:])
                finally:
                    with outer.lock:
                        outer.active -= 1

            def do_GET(self) -> None:  # noqa: N802
                self._handle("GET")

            def do_HEAD(self) -> None:  # noqa: N802
                self._handle("HEAD")

        self.httpd = ThreadingHTTPServer((HOST, 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def url(self, path: str = "/f") -> str:
        return f"http://{HOST}:{self.port}{path}"

    def times(self) -> list[float]:
        return sorted(t for t, _, _ in self.arrivals)

    def __enter__(self) -> FakeServer:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
