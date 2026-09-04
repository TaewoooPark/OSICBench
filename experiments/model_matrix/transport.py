"""Local CONNECT gateway for subscription clients inside one OS sandbox.

TLS remains end-to-end: the gateway never decrypts requests, credentials,
prompts, or responses. Only exact approved destinations on port 443 pass.
"""
from __future__ import annotations

import collections
import select
import socket
import socketserver
import threading


DESTINATIONS = {
    "openai": {"chatgpt.com", "auth.openai.com", "api.openai.com"},
    "anthropic": {"api.anthropic.com", "claude.ai", "platform.claude.com",
                  "console.anthropic.com", "auth.anthropic.com"},
}


class TransportProxy:
    def __init__(self, provider: str):
        self.allowed_hosts = frozenset(DESTINATIONS[provider])
        self.allowed, self.denied = collections.Counter(), collections.Counter()
        self.lock = threading.Lock()
        owner = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.settimeout(15)
                request = b""
                try:
                    while b"\r\n\r\n" not in request and len(request) < 16384:
                        chunk = self.request.recv(4096)
                        if not chunk:
                            return
                        request += chunk
                    header, separator, tail = request.partition(b"\r\n\r\n")
                    first = header.split(b"\r\n", 1)[0].decode("ascii")
                    method, authority, _version = first.split()
                    host, port_text = authority.rsplit(":", 1)
                    host = host.lower()
                    if not separator or method != "CONNECT" or port_text != "443" or host not in owner.allowed_hosts:
                        with owner.lock:
                            owner.denied[host if host in owner.allowed_hosts else "unapproved_destination"] += 1
                        self.request.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
                        return
                    with owner.lock:
                        owner.allowed[host] += 1
                    with socket.create_connection((host, 443), timeout=20) as upstream:
                        self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                        if tail:
                            upstream.sendall(tail)
                        self.request.settimeout(None)
                        upstream.settimeout(None)
                        sockets = [self.request, upstream]
                        while True:
                            readable, _, _ = select.select(sockets, [], [], 60)
                            if not readable:
                                continue
                            for connection in readable:
                                data = connection.recv(65536)
                                if not data:
                                    return
                                destination = upstream if connection is self.request else self.request
                                destination.sendall(data)
                except (OSError, ValueError, UnicodeError):
                    return

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = False

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def environment(self) -> dict:
        url = f"http://127.0.0.1:{self.port}"
        return {"HTTP_PROXY": url, "HTTPS_PROXY": url, "ALL_PROXY": url,
                "http_proxy": url, "https_proxy": url, "all_proxy": url,
                "NO_PROXY": "", "no_proxy": ""}

    def audit(self) -> dict:
        with self.lock:
            return {"allowed_hosts": sorted(self.allowed_hosts),
                    "allowed_connections": dict(self.allowed), "denied_connections": dict(self.denied),
                    "tls_interception": False}
