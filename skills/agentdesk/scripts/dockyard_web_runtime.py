"""Loopback-only static Web shell for the in-memory Dockyard bootstrap."""

from __future__ import annotations

import hashlib
import mimetypes
import json
import http.client
import os
import stat
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse, urlsplit

_SPA_ROUTES = frozenset({
    "/",
    "/requirements",
    "/tasks",
    "/runs",
    "/reviews",
    "/workers",
    "/projects",
    "/diagnostics",
    "/settings",
})

_UPSTREAM_READ_TIMEOUT_SECONDS = 15
_UPSTREAM_COMMAND_TIMEOUT_SECONDS = 195


def _upstream_timeout(method: str) -> int:
    """Keep long PM/provider commands inside the Web proxy's wait window."""
    return (
        _UPSTREAM_COMMAND_TIMEOUT_SECONDS
        if method in {"POST", "PATCH", "PUT"}
        else _UPSTREAM_READ_TIMEOUT_SECONDS
    )


def _proxy_error_body(
    method: str,
    route: str,
    error_code: str,
    *,
    retryable: bool,
) -> bytes:
    digest = hashlib.sha256(f"{method}\x1f{route}".encode("utf-8")).hexdigest()
    return json.dumps(
        {
            "schema_version": "dockyard.error/v1",
            "request_id": "REQ-" + digest[:16],
            "error_code": error_code,
            "category": "transport",
            "retryable": retryable,
            "safe_message": "Dockyard upstream request did not complete.",
            "correlation_digest": digest,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class DockyardWebRuntimeError(ValueError):
    pass


class DockyardWebRuntimeInputError(DockyardWebRuntimeError):
    pass


class DockyardWebRuntimeSecurityError(DockyardWebRuntimeError):
    pass


def _safe_text(value: object, field_name: str, maximum: int = 512) -> str:
    if type(value) is not str or not value or value != value.strip() or len(value) > maximum:
        raise DockyardWebRuntimeInputError(f"web_runtime:{field_name}")
    if any(ord(character) < 32 for character in value):
        raise DockyardWebRuntimeInputError(f"web_runtime:{field_name}")
    return value


@dataclass(frozen=True, slots=True)
class DockyardBrowserBootstrap:
    api_base_url: str
    project_id: str
    device_id: str
    device_token: str = field(repr=False)
    csrf_value: str = field(repr=False)

    def __post_init__(self) -> None:
        parsed = urlparse(_safe_text(self.api_base_url, "api_base_url", 256))
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise DockyardWebRuntimeInputError("web_runtime:api_base_url")
        if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment or parsed.port is None:
            raise DockyardWebRuntimeInputError("web_runtime:api_base_url")
        _safe_text(self.project_id, "project_id", 128)
        _safe_text(self.device_id, "device_id", 128)
        _safe_text(self.device_token, "device_token")
        _safe_text(self.csrf_value, "csrf_value", 256)

    def as_window_value(self) -> dict[str, str]:
        return {
            "apiBaseUrl": self.api_base_url,
            "projectId": self.project_id,
            "deviceId": self.device_id,
            "deviceToken": self.device_token,
            "csrfValue": self.csrf_value,
        }

    def script_bytes(self) -> bytes:
        payload = json.dumps(
            self.as_window_value(), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        )
        return f"window.__DOCKYARD_LOOPBACK__={payload};\n".encode("utf-8")


def _is_reparse(path: Path) -> bool:
    metadata = path.lstat()
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & 0x400
    )


def _plain_directory(path: object) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise DockyardWebRuntimeInputError("web_runtime:asset_root")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise DockyardWebRuntimeInputError("web_runtime:asset_root") from exc
    if not resolved.is_dir() or _is_reparse(path):
        raise DockyardWebRuntimeSecurityError("web_runtime:asset_root")
    index = resolved / "index.html"
    if not index.is_file() or _is_reparse(index):
        raise DockyardWebRuntimeSecurityError("web_runtime:index")
    return resolved


@dataclass(frozen=True, slots=True)
class DockyardWebRuntimeConfig:
    asset_root: Path
    bootstrap: DockyardBrowserBootstrap = field(repr=False)
    bind_host: str = "127.0.0.1"
    port: int = 0
    api_upstream: str | None = None

    def __post_init__(self) -> None:
        _plain_directory(self.asset_root)
        if type(self.bootstrap) is not DockyardBrowserBootstrap:
            raise DockyardWebRuntimeInputError("web_runtime:bootstrap")
        if self.bind_host not in {"127.0.0.1", "::1"}:
            raise DockyardWebRuntimeInputError("web_runtime:host")
        if type(self.port) is not int or not 0 <= self.port <= 65535:
            raise DockyardWebRuntimeInputError("web_runtime:port")
        if self.api_upstream is not None:
            parsed = urlparse(_safe_text(self.api_upstream, "api_upstream", 256))
            if (
                parsed.scheme != "http"
                or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
                or parsed.port is None
                or parsed.path not in {"", "/"}
                or parsed.params
                or parsed.query
                or parsed.fragment
            ):
                raise DockyardWebRuntimeInputError("web_runtime:api_upstream")


@dataclass(frozen=True, slots=True)
class DockyardWebRuntimeResult:
    host: str
    port: int
    browser_url: str


def _host_text(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _browser_url(host: str, port: int) -> str:
    return f"http://{_host_text(host, port)}/"


class _WebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


class DockyardWebRuntime:
    def __init__(self, config: DockyardWebRuntimeConfig) -> None:
        if type(config) is not DockyardWebRuntimeConfig:
            raise DockyardWebRuntimeInputError("web_runtime:config")
        self._config = config
        self._root = _plain_directory(config.asset_root)
        self._server: _WebServer | None = None
        self._thread: threading.Thread | None = None
        self._result: DockyardWebRuntimeResult | None = None

    def start(self) -> DockyardWebRuntimeResult:
        if self._result is not None:
            return self._result
        root = self._root
        bootstrap = self._config.bootstrap
        upstream = None if self._config.api_upstream is None else urlparse(self._config.api_upstream)

        class Handler(BaseHTTPRequestHandler):
            server_version = "DockyardWeb/1"
            sys_version = ""

            def log_message(self, format: str, *args: object) -> None:
                return

            def _send(self, status: int, body: bytes, content_type: str, *, no_store: bool = False) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Cross-Origin-Resource-Policy", "same-origin")
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src http://127.0.0.1:* http://[::1]:* http://localhost:*; img-src 'self' data:; font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
                self.send_header("Cache-Control", "no-store" if no_store else "no-cache")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _reject(self, status: int) -> None:
                self._send(status, b"Dockyard request rejected\n", "text/plain; charset=utf-8", no_store=True)

            def _valid_host(self) -> bool:
                expected = _host_text(self.server.server_address[0], self.server.server_address[1])
                return self.headers.get("Host") == expected

            def _asset(self, route: str) -> tuple[bytes, str] | None:
                if route in _SPA_ROUTES:
                    route = "/index.html"
                try:
                    decoded = unquote(route, errors="strict")
                except (UnicodeError, ValueError):
                    return None
                if "\\" in decoded or "\x00" in decoded:
                    return None
                parts = tuple(part for part in decoded.split("/") if part)
                if not parts or any(part in {".", ".."} for part in parts):
                    return None
                target = root.joinpath(*parts)
                try:
                    resolved = target.resolve(strict=True)
                    resolved.relative_to(root)
                except (OSError, ValueError):
                    return None
                current = root
                for part in parts:
                    current = current / part
                    if _is_reparse(current):
                        return None
                if not resolved.is_file():
                    return None
                try:
                    body = resolved.read_bytes()
                except OSError:
                    return None
                kind = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
                if kind.startswith("text/") or kind in {"application/javascript", "application/json"}:
                    kind += "; charset=utf-8"
                return body, kind

            def _handle(self) -> None:
                if not self._valid_host():
                    self._reject(421)
                    return
                origin = self.headers.get("Origin")
                expected_origin = _browser_url(
                    self.server.server_address[0], self.server.server_address[1]
                ).rstrip("/")
                if origin is not None and origin != expected_origin:
                    self._reject(403)
                    return
                parsed = urlsplit(self.path)
                if self.command not in {"GET", "HEAD"} and not parsed.path.startswith(
                    "/api/dockyard/v1/"
                ):
                    self._reject(405)
                    return
                if parsed.fragment:
                    self._reject(404)
                    return
                if parsed.path.startswith("/api/dockyard/v1/"):
                    route = parsed.path + ("?" + parsed.query if parsed.query else "")
                    self._proxy(route)
                    return
                if parsed.query:
                    self._reject(404)
                    return
                if parsed.path == "/dockyard-runtime.js":
                    self._send(
                        200,
                        bootstrap.script_bytes(),
                        "application/javascript; charset=utf-8",
                        no_store=True,
                    )
                    return
                asset = self._asset(parsed.path)
                if asset is None:
                    self._reject(404)
                    return
                self._send(200, asset[0], asset[1])

            def _proxy(self, route: str) -> None:
                if upstream is None:
                    self._reject(404)
                    return
                if self.headers.get("Transfer-Encoding") is not None:
                    self._reject(400)
                    return
                length_text = self.headers.get("Content-Length")
                try:
                    length = 0 if length_text is None else int(length_text)
                except ValueError:
                    self._reject(400)
                    return
                if length < 0 or length > 1024 * 1024:
                    self._reject(413)
                    return
                body = self.rfile.read(length) if length else None
                headers: dict[str, str] = {}
                for name in (
                    "Accept",
                    "Authorization",
                    "Content-Type",
                    "Last-Event-ID",
                    "X-Dockyard-Device-Id",
                    "X-Dockyard-CSRF",
                    "X-Dockyard-Idempotency-Key",
                ):
                    value = self.headers.get(name)
                    if value is not None:
                        headers[name] = value
                headers["Origin"] = _browser_url(
                    self.server.server_address[0], self.server.server_address[1]
                ).rstrip("/")
                connection_class = http.client.HTTPSConnection if upstream.scheme == "https" else http.client.HTTPConnection
                host = upstream.hostname
                assert host is not None and upstream.port is not None
                connection = connection_class(
                    host,
                    upstream.port,
                    timeout=_upstream_timeout(self.command),
                )
                try:
                    connection.request(self.command, route, body=body, headers=headers)
                    response = connection.getresponse()
                    response_body = response.read(2 * 1024 * 1024 + 1)
                    if len(response_body) > 2 * 1024 * 1024:
                        self._reject(502)
                        return
                    content_type = response.getheader("Content-Type") or "application/octet-stream"
                    self.send_response(response.status)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(response_body)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Referrer-Policy", "no-referrer")
                    etag = response.getheader("ETag")
                    if etag is not None:
                        self.send_header("ETag", etag)
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(response_body)
                except TimeoutError:
                    self._send(
                        504,
                        _proxy_error_body(
                            self.command,
                            route,
                            "UPSTREAM_TIMEOUT",
                            retryable=True,
                        ),
                        "application/json; charset=utf-8",
                        no_store=True,
                    )
                except (OSError, http.client.HTTPException):
                    self._send(
                        502,
                        _proxy_error_body(
                            self.command,
                            route,
                            "UPSTREAM_UNAVAILABLE",
                            retryable=True,
                        ),
                        "application/json; charset=utf-8",
                        no_store=True,
                    )
                finally:
                    connection.close()

            def do_GET(self) -> None:
                self._handle()

            def do_HEAD(self) -> None:
                self._handle()

            def do_POST(self) -> None:
                self._handle()

            def do_PATCH(self) -> None:
                self._handle()

            def do_PUT(self) -> None:
                self._handle()

            def do_OPTIONS(self) -> None:
                self._handle()

        try:
            server = _WebServer((self._config.bind_host, self._config.port), Handler)
        except OSError as exc:
            raise DockyardWebRuntimeError("web_runtime:bind") from exc
        host, port = server.server_address[:2]
        result = DockyardWebRuntimeResult(host, port, _browser_url(host, port))
        thread = threading.Thread(target=server.serve_forever, name="dockyard-web-runtime", daemon=True)
        thread.start()
        self._server = server
        self._thread = thread
        self._result = result
        return result

    def stop(self) -> None:
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        self._result = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5)


__all__ = [
    "DockyardWebRuntimeError",
    "DockyardWebRuntimeInputError",
    "DockyardWebRuntimeSecurityError",
    "DockyardBrowserBootstrap",
    "DockyardWebRuntimeConfig",
    "DockyardWebRuntimeResult",
    "DockyardWebRuntime",
]
