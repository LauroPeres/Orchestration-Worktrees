"""Tiny HTTP server for tests: `python tests/serve.py <port>`, listening on 0.0.0.0.

`python -m http.server` calls socket.getfqdn() while binding, which can take minutes on macOS
runners (reverse DNS), so the tests use this instead.
"""

import http.server
import socketserver
import sys


class Server(http.server.ThreadingHTTPServer):
    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)  # skips the getfqdn() lookup
        self.server_name = "localhost"
        self.server_port = int(self.socket.getsockname()[1])


if __name__ == "__main__":
    Server(("0.0.0.0", int(sys.argv[1])), http.server.SimpleHTTPRequestHandler).serve_forever()
