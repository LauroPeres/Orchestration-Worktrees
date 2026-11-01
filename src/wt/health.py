"""HTTP checks through Traefik. "Container running" is not "service healthy"."""

import http.client
import socket
import ssl
import time
from dataclasses import dataclass

HEALTHY = {401, 403}  # plus every 2xx/3xx. 404 is not healthy: Traefik answers 404 for no route.


@dataclass(frozen=True)
class Check:
    ok: bool
    detail: str


class _LoopbackHTTPS(http.client.HTTPSConnection):
    """HTTPS to 127.0.0.1 with the real host name as SNI, so the OS resolver is not involved."""

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self._ctx = ssl.create_default_context()
        self._ctx.check_hostname = False
        self._ctx.verify_mode = ssl.CERT_NONE  # health only; `wt doctor` checks the CA
        super().__init__(host, port, timeout=timeout, context=self._ctx)

    def connect(self) -> None:
        sock = socket.create_connection(("127.0.0.1", self.port), self.timeout)
        self.sock = self._ctx.wrap_socket(sock, server_hostname=self.host)


def status(host: str, path: str, port: int, timeout: float = 3.0) -> int:
    """HTTP status of `GET path` for `host` through the loopback port; network errors propagate."""
    conn = _LoopbackHTTPS(host, port, timeout)
    try:
        conn.request("GET", path, headers={"Host": host, "User-Agent": "wt-health"})
        return conn.getresponse().status
    finally:
        conn.close()


def probe(host: str, path: str, port: int, timeout: float = 3.0) -> Check:
    try:
        code = status(host, path, port, timeout)
    except (OSError, http.client.HTTPException) as exc:
        return Check(False, f"no response ({exc.__class__.__name__})")
    ok = 200 <= code < 400 or code in HEALTHY
    return Check(ok, f"HTTP {code}")


def wait_healthy(host: str, path: str, port: int, timeout: float) -> Check:
    deadline = time.monotonic() + timeout
    while True:
        check = probe(host, path, port)
        if check.ok or time.monotonic() >= deadline:
            return check
        time.sleep(max(0.0, min(1.0, deadline - time.monotonic())))
