"""Loopback HTTP dashboard; research access is supplied only through host ports."""

from __future__ import annotations

import errno
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import secrets
from socketserver import TCPServer
import threading
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .interfaces import DashboardReadPort, MonitorPort, OperatorCommandPort
from .monitor import ResearchMonitor


class _HTTPError(Exception):
    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(message)


class _HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False

    def server_bind(self) -> None:
        # HTTPServer.server_bind performs a reverse-DNS lookup for server_name.
        # This dashboard binds a fixed numeric loopback address; DNS is unused
        # and can stall startup for tens of seconds on disconnected/CI hosts.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class DashboardServer:
    def __init__(
        self,
        read_port: DashboardReadPort,
        command_port: OperatorCommandPort,
        monitor_port: MonitorPort,
        cache_dir: Path,
        port: int = 1113,
        *,
        auto_refresh_seconds: float = 5400,
        auto_start: bool = True,
    ) -> None:
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("Dashboard port must be between 1 and 65535")
        self.read_port = read_port
        self.command_port = command_port
        self.instance_id = secrets.token_hex(16)
        self._token = secrets.token_urlsafe(32)
        self._static = (Path(__file__).parent / "static").resolve()
        self._serving = threading.Event()
        self._closed = threading.Event()
        self._lifecycle_lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self) -> None:
                super().setup()
                self.connection.settimeout(10)

            def log_message(self, format: str, *args: Any) -> None:
                pass

            def _json(self, status: int, payload: Any) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self._respond(status, body, "application/json; charset=utf-8")

            def _respond(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Frame-Options", "DENY")
                self.end_headers()
                self.wfile.write(body)

            def _authority(self) -> str:
                authorities = {f"127.0.0.1:{owner.address[1]}", f"localhost:{owner.address[1]}"}
                hosts = self.headers.get_all("Host", [])
                if len(hosts) != 1 or hosts[0] not in authorities:
                    raise _HTTPError(403, "Host must identify this local dashboard")
                return hosts[0]

            def _post_auth(self) -> None:
                authority = self._authority()
                origins = self.headers.get_all("Origin", [])
                if origins and (len(origins) != 1 or origins[0] != f"http://{authority}"):
                    raise _HTTPError(403, "Origin must match this local dashboard")
                tokens = self.headers.get_all("X-Dashboard-Token", [])
                if len(tokens) != 1 or not tokens[0].isascii() or not secrets.compare_digest(tokens[0], owner._token):
                    raise _HTTPError(403, "Missing or invalid dashboard session token")
                if self.headers.get("Sec-Fetch-Site") == "cross-site":
                    raise _HTTPError(403, "Cross-site commands are not accepted")

            def _body(self) -> dict[str, Any]:
                if self.headers.get("Transfer-Encoding"):
                    raise _HTTPError(400, "Chunked requests are not supported")
                lengths = self.headers.get_all("Content-Length", [])
                if len(lengths) != 1:
                    raise _HTTPError(411, "One Content-Length header is required")
                try:
                    length = int(lengths[0])
                except ValueError as exc:
                    raise _HTTPError(400, "Invalid Content-Length") from exc
                if length < 0:
                    raise _HTTPError(400, "Invalid Content-Length")
                if length > 1024 * 1024:
                    raise _HTTPError(413, "Dashboard commands are limited to 1 MiB")
                if self.headers.get_content_type() != "application/json":
                    raise _HTTPError(415, "Dashboard commands require application/json")
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                except (UnicodeDecodeError, ValueError) as exc:
                    raise _HTTPError(400, "Request body must be valid UTF-8 JSON") from exc
                if not isinstance(body, dict):
                    raise _HTTPError(400, "Request body must be a JSON object")
                return body

            def _handle(self, method: str) -> None:
                try:
                    self._authority()
                    parsed = urlsplit(self.path)
                    if parsed.scheme or parsed.netloc:
                        raise _HTTPError(400, "Use a local relative request path")
                    path = unquote(parsed.path)
                    if method == "GET":
                        self._get(path, parse_qs(parsed.query, keep_blank_values=True))
                    else:
                        self._post_auth()
                        self._post(path, self._body())
                except _HTTPError as exc:
                    self._json(exc.status, {"error": str(exc)})
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
                except RuntimeError as exc:
                    self._json(409, {"error": str(exc)})
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception:
                    self._json(500, {"error": "Dashboard operation failed; research continues independently"})

            def _get(self, path: str, query: dict[str, list[str]]) -> None:
                def argument(name: str, default: str) -> str:
                    values = query.get(name, [default])
                    if len(values) != 1:
                        raise _HTTPError(400, f"Use only one {name} parameter")
                    return values[0]

                def pagination() -> dict[str, Any]:
                    try:
                        offset = int(argument("offset", "0"))
                        limit = int(argument("limit", "50"))
                    except ValueError as exc:
                        raise _HTTPError(400, "Pagination values must be integers") from exc
                    if offset < 0 or not 1 <= limit <= 200:
                        raise _HTTPError(400, "Offset must be nonnegative and limit between 1 and 200")
                    return {"query": argument("q", ""), "offset": offset, "limit": limit}

                if path == "/api/health":
                    self._json(200, {"instance_id": owner.instance_id})
                elif path == "/api/session":
                    self._json(200, {"csrf_token": owner._token})
                elif path == "/api/overview":
                    self._json(200, owner.read_port.overview())
                elif path == "/api/main-memory":
                    self._json(200, owner.read_port.main_memory(kind=argument("kind", "all"), **pagination()))
                elif path == "/api/memory-graph":
                    self._json(200, owner.read_port.memory_graph())
                elif path.startswith("/api/main-memory/"):
                    record_id = path.removeprefix("/api/main-memory/")
                    if not record_id or "/" in record_id:
                        raise _HTTPError(404, "Memory record not found")
                    record = owner.read_port.main_record(record_id)
                    if record is None:
                        raise _HTTPError(404, "Memory record not found")
                    self._json(200, record)
                elif path == "/api/explorer-memory":
                    record_type = argument("type", "scratch")
                    if record_type not in {"scratch", "summary"}:
                        raise _HTTPError(400, "Explorer type must be scratch or summary")
                    self._json(200, owner.read_port.explorer_memory(record_type=record_type, **pagination()))
                elif path == "/api/monitor":
                    self._json(200, owner.monitor.status())
                else:
                    self._asset(path)

            def _asset(self, path: str) -> None:
                if path.startswith("/static/"):
                    path = path.removeprefix("/static")
                relative = "index.html" if path in {"/", "/main-memory", "/explorer-memory"} else path.lstrip("/")
                parts = relative.split("/")
                if any(not part or part.startswith(".") or "\\" in part or "\x00" in part for part in parts):
                    raise _HTTPError(404, "Page not found")
                if relative not in {"index.html", "app.js", "style.css"} and not relative.startswith("vendor/"):
                    raise _HTTPError(404, "Page not found")
                candidate = (owner._static / relative).resolve()
                if not candidate.is_relative_to(owner._static) or not candidate.is_file():
                    raise _HTTPError(404, "Page not found")
                content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
                if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
                    content_type += "; charset=utf-8"
                self._respond(200, candidate.read_bytes(), content_type)

            def _post(self, path: str, body: dict[str, Any]) -> None:
                if path == "/api/monitor/refresh":
                    if body:
                        raise _HTTPError(400, "Monitor refresh takes an empty object")
                    self._json(202, owner.monitor.refresh())
                elif path == "/api/guidance":
                    if set(body) != {"text"} or not isinstance(body["text"], str) or not body["text"].strip():
                        raise _HTTPError(400, "Guidance requires nonempty text")
                    self._json(202, owner.command_port.submit_guidance(body["text"]))
                elif path == "/api/advisor-feedback":
                    if (set(body) != {"request_id", "response"}
                        or not isinstance(body["request_id"], str) or not body["request_id"].strip()
                        or not isinstance(body["response"], dict)):
                        raise _HTTPError(400, "Advisor feedback requires request_id and response")
                    self._json(202, owner.command_port.submit_advisor_feedback(body["request_id"], body["response"]))
                else:
                    raise _HTTPError(404, "Command not found")

            def do_GET(self) -> None:
                self._handle("GET")

            def do_POST(self) -> None:
                self._handle("POST")

        while True:
            try:
                self._httpd = _HTTPServer(("127.0.0.1", port), Handler)
                break
            except OSError as exc:
                if exc.errno != errno.EADDRINUSE or port == 65535:
                    raise
                port += 1
        try:
            self.monitor = ResearchMonitor(
                read_port, monitor_port, cache_dir,
                auto_refresh_seconds=auto_refresh_seconds, auto_start=auto_start,
            )
        except Exception:
            self._httpd.server_close()
            raise

    @property
    def address(self) -> tuple[str, int]:
        return self._httpd.server_address[:2]

    @property
    def url(self) -> str:
        return f"http://{self.address[0]}:{self.address[1]}"

    def serve_forever(self) -> None:
        with self._lifecycle_lock:
            if self._closed.is_set():
                return
            self._serving.set()
        try:
            self._httpd.serve_forever(poll_interval=0.1)
        finally:
            self._serving.clear()

    def shutdown(self) -> None:
        with self._lifecycle_lock:
            if self._closed.is_set():
                return
            self._closed.set()
            serving = self._serving.is_set()
        self.monitor.stop()
        if serving:
            self._httpd.shutdown()
        self._httpd.server_close()

    close = shutdown


__all__ = ["DashboardServer"]
