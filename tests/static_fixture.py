"""Test fixtures for the static fetcher: a real TLS server on 127.0.0.1 and a fetcher wired to it.

Nothing here leaves the machine. The certificate and key are generated per run in a private temporary directory with
the system `openssl` (the tests skip when it is missing); no key is stored in the repository.
"""
from __future__ import annotations

import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Callable

from pairadar import static_fetch

HOST = "good.test"


def make_certificate(directory: Path) -> tuple[Path, Path]:
    openssl = shutil.which("openssl")
    if openssl is None:
        raise unittest.SkipTest("openssl is not available")
    cert, key = directory / "cert.pem", directory / "key.pem"
    result = subprocess.run(
        [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", f"/CN={HOST}",
         "-addext", f"subjectAltName=DNS:{HOST},DNS:alt.good.test", "-addext", "basicConstraints=critical,CA:TRUE",
         "-keyout", str(key), "-out", str(cert)],
        capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise unittest.SkipTest("openssl could not make a test certificate")
    key.chmod(0o600)
    return cert, key


class Reply:
    """What the server does for one request path."""

    def __init__(self, status: int = 200, body: bytes | str = b"", *, ctype: str = "text/html; charset=utf-8",
                 headers: dict[str, str] | None = None, chunked: bool = False, framing: bool = True,
                 declare: int | None = None, drip: float = 0.0, stall_after: int | None = None,
                 stall_headers: bool = False) -> None:
        self.status = status
        self.body = body.encode() if isinstance(body, str) else body
        self.ctype = ctype
        self.headers = headers or {}
        self.chunked = chunked
        self.framing = framing
        self.declare = declare            # a Content-Length that is not the real length
        self.drip = drip                  # seconds between single bytes of the body
        self.stall_after = stall_after    # send this many body bytes, then stop answering
        self.stall_headers = stall_headers


class TlsServer:
    def __init__(self, certificate: tuple[Path, Path]) -> None:
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(str(certificate[0]), str(certificate[1]))
        self.sni: list[str | None] = []
        self.context.sni_callback = lambda sock, name, ctx: self.sni.append(name)
        self.replies: dict[str, Reply] = {}
        self.default = Reply(404, "not here", ctype="text/plain")
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.accepted = 0
        self.stall_tcp = False            # accept the connection and never speak TLS
        self._held: list[socket.socket] = []
        self._stop = False
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def serve(self, path: str, reply: Reply) -> None:
        self.replies[path] = reply

    def paths(self) -> list[str]:
        return [request[1] for request in self.requests]

    def close(self) -> None:
        self._stop = True
        for held in self._held:
            try:
                held.close()
            except OSError:
                pass
        try:
            self.sock.close()
        except OSError:
            pass

    def _accept_loop(self) -> None:
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.accepted += 1
            self._held.append(conn)
            if self.stall_tcp:
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(10)
            tls = self.context.wrap_socket(conn, server_side=True)
        except (OSError, ssl.SSLError):
            return
        try:
            data = b""
            while b"\r\n\r\n" not in data and len(data) < 65536:
                piece = tls.recv(4096)
                if not piece:
                    return
                data += piece
            head = data.split(b"\r\n\r\n", 1)[0].decode("latin-1").split("\r\n")
            method, path, _ = head[0].split(" ", 2)
            headers = {line.split(":", 1)[0].strip().lower(): line.split(":", 1)[1].strip() for line in head[1:] if ":" in line}
            self.requests.append((method, path, headers))
            self._answer(tls, self.replies.get(path.split("?")[0]) or self.replies.get(path) or self.default)
        except (OSError, ssl.SSLError, ValueError):
            pass
        finally:
            try:
                tls.close()
            except OSError:
                pass

    def _answer(self, tls: ssl.SSLSocket, reply: Reply) -> None:
        lines = [f"HTTP/1.1 {reply.status} X"]
        if reply.ctype:
            lines.append(f"Content-Type: {reply.ctype}")
        if reply.chunked:
            lines.append("Transfer-Encoding: chunked")
        elif reply.framing:
            lines.append(f"Content-Length: {reply.declare if reply.declare is not None else len(reply.body)}")
        for name, value in reply.headers.items():
            lines.append(f"{name}: {value}")
        lines.append("Connection: close")
        head = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
        if reply.stall_headers:
            for byte in head:
                tls.sendall(bytes([byte]))
                time.sleep(0.25)
            return
        tls.sendall(head)
        body = reply.body
        if reply.chunked:
            for start in range(0, len(body), 7):
                piece = body[start:start + 7]
                tls.sendall(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
            if reply.stall_after is None:
                tls.sendall(b"0\r\n\r\n")
        elif reply.drip:
            for byte in body:
                tls.sendall(bytes([byte]))
                time.sleep(reply.drip)
        elif reply.stall_after is not None:
            tls.sendall(body[:reply.stall_after])
            time.sleep(30)
        else:
            tls.sendall(body)


class TlsTestCase(unittest.TestCase):
    """Starts one server and gives `fetcher()` that talks to it over real TLS, trusting only the test certificate."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.certificate = make_certificate(Path(self.tmp.name))
        self.server = TlsServer(self.certificate)
        self.addCleanup(self.server.close)

    def trusting(self) -> ssl.SSLContext:
        context = ssl.create_default_context(cafile=str(self.certificate[0]))
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return context

    def fetcher(self, *, resolver: Callable[[str], list[str]] | None = None, policy=None, context=None,
                min_interval: float = 0.0, sleep=None) -> static_fetch.StaticFetcher:
        """A fetcher that resolves every name to the test server's loopback address, which only a test policy admits."""
        return static_fetch.StaticFetcher(
            resolver=resolver or (lambda host: ["127.0.0.1"]), context=context or self.trusting(), port=self.server.port,
            policy=policy or (lambda address: address == "127.0.0.1"), min_interval=min_interval,
            sleep=sleep or (lambda seconds: None))
