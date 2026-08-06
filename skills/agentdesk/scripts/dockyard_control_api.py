"""Loopback-only HTTP/SSE transport for the Dockyard local control plane."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
import socket
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Protocol
from urllib.parse import parse_qs, urlsplit

from dockyard_pairing import DockyardDeviceScope
from dockyard_sse import (
    DockyardSseCapacityError,
    DockyardSseCursorError,
    DockyardSseHub,
    encode_sse_event,
)

__all__ = [
    "DockyardCommandEnvelope",
    "DockyardCommandRejected",
    "DockyardCommandReceipt",
    "DockyardErrorEnvelope",
    "DockyardReadRequest",
    "DockyardJsonResponse",
    "DockyardCommandRequest",
    "DockyardApiConfig",
    "DockyardServerAddress",
    "DockyardReadService",
    "DockyardCommandGateway",
    "DockyardAuthorizer",
    "DockyardControlServer",
]

_NAMESPACE = "/api/dockyard/v1"
_READ_PATHS = frozenset({
    "health",
    "projects",
    "project",
    "overview",
    "tasks",
    "task",
    "runs",
    "approvals",
    "providers",
    "plan",
})
_COMMAND_TYPES = frozenset({
    "project.register",
    "plan.create",
    "plan.update",
    "plan.approve",
    "task.retry",
    "task.cancel",
    "dispatch.terminate",
    "delivery.accept",
    "delivery.return",
    "provider.binding.update",
    "project.remove",
})
_HEX = frozenset("0123456789abcdef")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class _ApiError(Exception):
    def __init__(self, status: int, code: str, category: str, retryable: bool = False) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.category = category
        self.retryable = retryable


class DockyardCommandRejected(Exception):
    """Typed failure surfaced by a concrete command gateway."""

    def __init__(self, status: int, code: str, category: str) -> None:
        super().__init__(code)
        if isinstance(status, bool) or not isinstance(status, int) or not 400 <= status <= 599:
            raise ValueError("status is invalid")
        _identifier(code, "code")
        _identifier(category, "category")
        self.status = status
        self.code = code
        self.category = category


def _text(value: object, field: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > maximum:
        raise ValueError(f"{field} is invalid")
    if any(ord(char) < 32 for char in value):
        raise ValueError(f"{field} is invalid")
    return value


def _identifier(value: object, field: str) -> str:
    text = _text(value, field, 128)
    if not _ID_RE.fullmatch(text):
        raise ValueError(f"{field} is invalid")
    return text


def _sha(value: object, length: int, field: str) -> str:
    if not isinstance(value, str) or len(value) != length or any(char not in _HEX for char in value):
        raise ValueError(f"{field} is invalid")
    return value


def _timestamp(value: object, field: str) -> str:
    text = _text(value, field)
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(f"{field} is invalid") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError(f"{field} is invalid")
    return text


@dataclass(frozen=True, slots=True)
class DockyardCommandEnvelope:
    schema_version: str
    command_id: str
    project_id: str
    command_type: str
    idempotency_key: str
    expected_snapshot_commit: str
    expected_revision: int
    confirmation_id: str | None
    payload_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != "dockyard.command/v1":
            raise ValueError("schema_version is invalid")
        _identifier(self.command_id, "command_id")
        _identifier(self.project_id, "project_id")
        if self.command_type not in _COMMAND_TYPES:
            raise ValueError("command_type is invalid")
        _identifier(self.idempotency_key, "idempotency_key")
        _sha(self.expected_snapshot_commit, 40, "expected_snapshot_commit")
        if isinstance(self.expected_revision, bool) or not isinstance(self.expected_revision, int) or self.expected_revision < 1:
            raise ValueError("expected_revision is invalid")
        if self.confirmation_id is not None:
            _identifier(self.confirmation_id, "confirmation_id")
        _sha(self.payload_digest, 64, "payload_digest")


@dataclass(frozen=True, slots=True)
class DockyardCommandReceipt:
    schema_version: str
    command_id: str
    project_id: str
    command_type: str
    outcome: str
    canonical_event_id: str | None
    snapshot_commit: str
    replayed: bool
    completed_at: str
    content_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != "dockyard.command-receipt/v1":
            raise ValueError("schema_version is invalid")
        _identifier(self.command_id, "command_id")
        _identifier(self.project_id, "project_id")
        if self.command_type not in _COMMAND_TYPES:
            raise ValueError("command_type is invalid")
        _text(self.outcome, "outcome", 128)
        if self.canonical_event_id is not None:
            _identifier(self.canonical_event_id, "canonical_event_id")
        _sha(self.snapshot_commit, 40, "snapshot_commit")
        if not isinstance(self.replayed, bool):
            raise ValueError("replayed is invalid")
        _timestamp(self.completed_at, "completed_at")
        _sha(self.content_digest, 64, "content_digest")


@dataclass(frozen=True, slots=True)
class DockyardErrorEnvelope:
    schema_version: str
    request_id: str
    error_code: str
    category: str
    retryable: bool
    safe_message: str
    correlation_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != "dockyard.error/v1":
            raise ValueError("schema_version is invalid")
        _identifier(self.request_id, "request_id")
        _identifier(self.error_code, "error_code")
        _identifier(self.category, "category")
        if not isinstance(self.retryable, bool):
            raise ValueError("retryable is invalid")
        _text(self.safe_message, "safe_message", 256)
        _sha(self.correlation_digest, 64, "correlation_digest")


@dataclass(frozen=True, slots=True)
class DockyardReadRequest:
    endpoint: str
    project_id: str | None
    resource_id: str | None

    def __post_init__(self) -> None:
        if self.endpoint not in _READ_PATHS:
            raise ValueError("endpoint is invalid")
        if self.project_id is not None:
            _identifier(self.project_id, "project_id")
        if self.resource_id is not None:
            _identifier(self.resource_id, "resource_id")


@dataclass(frozen=True, slots=True)
class DockyardJsonResponse:
    status_code: int
    body: bytes
    etag: str | None

    def __post_init__(self) -> None:
        if isinstance(self.status_code, bool) or not isinstance(self.status_code, int) or not 100 <= self.status_code <= 599:
            raise ValueError("status_code is invalid")
        if not isinstance(self.body, bytes):
            raise ValueError("body must be bytes")
        if self.etag is not None:
            _text(self.etag, "etag", 256)


@dataclass(frozen=True, slots=True)
class DockyardCommandRequest:
    envelope: DockyardCommandEnvelope
    resource_id: str | None
    payload_json: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.envelope, DockyardCommandEnvelope):
            raise ValueError("envelope is invalid")
        if self.resource_id is not None:
            _identifier(self.resource_id, "resource_id")
        if not isinstance(self.payload_json, bytes) or not self.payload_json:
            raise ValueError("payload_json is invalid")


@dataclass(frozen=True, slots=True)
class DockyardApiConfig:
    bind_host: str
    port: int
    allowed_hosts: tuple[str, ...]
    allowed_origins: tuple[str, ...]
    max_body_bytes: int = 65536
    max_json_depth: int = 16
    sse_idle_seconds: float = 1.0

    def __post_init__(self) -> None:
        if self.bind_host not in {"127.0.0.1", "::1"}:
            raise ValueError("bind_host must be loopback")
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not 0 <= self.port <= 65535:
            raise ValueError("port is invalid")
        for field, values in (("allowed_hosts", self.allowed_hosts), ("allowed_origins", self.allowed_origins)):
            if not isinstance(values, tuple) or not values:
                raise ValueError(f"{field} must be a non-empty tuple")
            for value in values:
                _text(value, field, 256)
        if isinstance(self.max_body_bytes, bool) or not isinstance(self.max_body_bytes, int) or self.max_body_bytes < 1:
            raise ValueError("max_body_bytes is invalid")
        if isinstance(self.max_json_depth, bool) or not isinstance(self.max_json_depth, int) or self.max_json_depth < 1:
            raise ValueError("max_json_depth is invalid")
        if isinstance(self.sse_idle_seconds, bool) or not isinstance(self.sse_idle_seconds, (int, float)) or self.sse_idle_seconds <= 0:
            raise ValueError("sse_idle_seconds is invalid")


@dataclass(frozen=True, slots=True)
class DockyardServerAddress:
    host: str
    port: int

    def __post_init__(self) -> None:
        try:
            if not ipaddress.ip_address(self.host).is_loopback:
                raise ValueError("host is not loopback")
        except ValueError as exc:
            raise ValueError("host is invalid") from exc
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not 1 <= self.port <= 65535:
            raise ValueError("port is invalid")


class DockyardReadService(Protocol):
    def read(self, request: DockyardReadRequest) -> DockyardJsonResponse: ...


class DockyardCommandGateway(Protocol):
    def execute(self, request: DockyardCommandRequest) -> DockyardCommandReceipt: ...


class DockyardAuthorizer(Protocol):
    def authorize(
        self,
        *,
        device_id: str,
        bearer_token: str,
        csrf_value: str,
        required_scope: DockyardDeviceScope,
        operation_id: str,
    ) -> None: ...


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _ApiError(400, "DUPLICATE_JSON_KEY", "input")
        result[key] = value
    return result


def _depth(value: object, maximum: int, current: int = 0) -> None:
    if current > maximum:
        raise _ApiError(400, "JSON_DEPTH_EXCEEDED", "input")
    if isinstance(value, dict):
        for item in value.values():
            _depth(item, maximum, current + 1)
    elif isinstance(value, list):
        for item in value:
            _depth(item, maximum, current + 1)


def _json(raw: bytes, maximum_depth: int) -> object:
    if raw.startswith(b"\xef\xbb\xbf"):
        raise _ApiError(400, "JSON_BOM_FORBIDDEN", "input")
    try:
        text = raw.decode("utf-8")
        value = json.loads(text, object_pairs_hook=_pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except _ApiError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise _ApiError(400, "INVALID_JSON", "input") from None
    _depth(value, maximum_depth)
    return value


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _scope(command_type: str) -> DockyardDeviceScope:
    if command_type == "task.retry":
        return DockyardDeviceScope.RETRY
    if command_type == "dispatch.terminate":
        return DockyardDeviceScope.TERMINATE
    return DockyardDeviceScope.APPROVE


def _read_route(path: str) -> DockyardReadRequest | None:
    if path == f"{_NAMESPACE}/health":
        return DockyardReadRequest("health", None, None)
    if path == f"{_NAMESPACE}/projects":
        return DockyardReadRequest("projects", None, None)
    match = re.fullmatch(rf"{re.escape(_NAMESPACE)}/projects/([^/]+)(?:/(.*))?", path)
    if match is None:
        return None
    project_id = _identifier(match.group(1), "project_id")
    suffix = match.group(2)
    if suffix is None:
        return DockyardReadRequest("project", project_id, None)
    if suffix in {"overview", "tasks", "runs", "approvals", "providers"}:
        return DockyardReadRequest(suffix, project_id, None)
    task = re.fullmatch(r"tasks/([^/]+)", suffix)
    if task:
        return DockyardReadRequest("task", project_id, _identifier(task.group(1), "task_id"))
    plan = re.fullmatch(r"plans/([^/]+)", suffix)
    if plan:
        return DockyardReadRequest("plan", project_id, _identifier(plan.group(1), "plan_id"))
    return None


def _command_route(method: str, path: str) -> tuple[str, str | None, str | None] | None:
    if method == "POST" and path == f"{_NAMESPACE}/projects/register":
        return "project.register", None, None
    patterns = (
        ("POST", r"plans", "plan.create", None),
        ("PATCH", r"plans/([^/]+)", "plan.update", 1),
        ("POST", r"plans/([^/]+)/approve", "plan.approve", 1),
        ("POST", r"tasks/([^/]+)/retry", "task.retry", 1),
        ("POST", r"tasks/([^/]+)/cancel", "task.cancel", 1),
        ("POST", r"dispatches/([^/]+)/terminate", "dispatch.terminate", 1),
        ("POST", r"deliveries/([^/]+)/accept", "delivery.accept", 1),
        ("POST", r"deliveries/([^/]+)/return", "delivery.return", 1),
        ("PUT", r"providers/([^/]+)/binding", "provider.binding.update", 1),
        ("POST", r"remove", "project.remove", None),
    )
    base = re.fullmatch(rf"{re.escape(_NAMESPACE)}/projects/([^/]+)/(.*)", path)
    if base is None:
        return None
    project_id = _identifier(base.group(1), "project_id")
    suffix = base.group(2)
    for expected_method, pattern, command_type, resource_group in patterns:
        match = re.fullmatch(pattern, suffix)
        if expected_method == method and match:
            resource = None if resource_group is None else _identifier(match.group(resource_group), "resource_id")
            return command_type, project_id, resource
    return None


class _IPv6ThreadingHTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_INET6


class DockyardControlServer:
    def __init__(
        self,
        config: DockyardApiConfig,
        read_service: DockyardReadService,
        command_gateway: DockyardCommandGateway,
        authorizer: DockyardAuthorizer,
        sse_hub: DockyardSseHub,
    ) -> None:
        if not isinstance(config, DockyardApiConfig):
            raise TypeError("config must be DockyardApiConfig")
        self._config = config
        self._read_service = read_service
        self._gateway = command_gateway
        self._authorizer = authorizer
        self._sse = sse_hub
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> DockyardServerAddress:
        if self._server is not None:
            raise RuntimeError("server already started")
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "Dockyard/1"
            sys_version = ""

            def log_message(self, format: str, *args: object) -> None:
                return

            def _request_digest(self) -> str:
                return hashlib.sha256(f"{self.command}\x1f{self.path}".encode("utf-8")).hexdigest()

            def _error(self, error: _ApiError) -> None:
                digest = self._request_digest()
                envelope = DockyardErrorEnvelope(
                    "dockyard.error/v1",
                    "REQ-" + digest[:16],
                    error.code,
                    error.category,
                    error.retryable,
                    "请求未被 Dockyard 接受。",
                    digest,
                )
                self._send_json(error.status, _canonical(asdict(envelope)))

            def _send_json(self, status: int, body: bytes, etag: str | None = None) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self._send_cors_origin()
                if etag is not None:
                    self.send_header("ETag", etag)
                self.end_headers()
                self.wfile.write(body)

            def _send_cors_origin(self) -> None:
                origin = self.headers.get("Origin")
                if origin in owner._config.allowed_origins:
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Vary", "Origin")

            def _guard_transport(self, *, command: bool) -> None:
                try:
                    if not ipaddress.ip_address(self.client_address[0]).is_loopback:
                        raise _ApiError(403, "NON_LOOPBACK_PEER", "security")
                except ValueError:
                    raise _ApiError(403, "UNKNOWN_PEER", "security") from None
                host = self.headers.get("Host")
                if host is None:
                    raise _ApiError(400, "HOST_REQUIRED", "security")
                try:
                    hostname = urlsplit("//" + host).hostname
                except ValueError:
                    raise _ApiError(400, "HOST_INVALID", "security") from None
                if hostname not in owner._config.allowed_hosts:
                    raise _ApiError(403, "HOST_FORBIDDEN", "security")
                origin = self.headers.get("Origin")
                if origin is not None and origin not in owner._config.allowed_origins:
                    raise _ApiError(403, "ORIGIN_FORBIDDEN", "security")
                if command and origin not in owner._config.allowed_origins:
                    raise _ApiError(403, "ORIGIN_REQUIRED", "security")
                if self.headers.get("X-HTTP-Method-Override") is not None:
                    raise _ApiError(400, "METHOD_OVERRIDE_FORBIDDEN", "security")

            def _body(self) -> bytes:
                if self.headers.get("Transfer-Encoding") is not None:
                    raise _ApiError(400, "TRANSFER_ENCODING_FORBIDDEN", "input")
                if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                    raise _ApiError(415, "CONTENT_TYPE_REQUIRED", "input")
                length_text = self.headers.get("Content-Length")
                try:
                    length = int(length_text or "")
                except ValueError:
                    raise _ApiError(400, "CONTENT_LENGTH_INVALID", "input") from None
                if length < 1:
                    raise _ApiError(400, "BODY_REQUIRED", "input")
                if length > owner._config.max_body_bytes:
                    raise _ApiError(413, "BODY_TOO_LARGE", "input")
                body = self.rfile.read(length)
                if len(body) != length:
                    raise _ApiError(400, "BODY_TRUNCATED", "input")
                return body

            def _events(self, project_id: str, query: str) -> None:
                if "text/event-stream" not in self.headers.get("Accept", ""):
                    raise _ApiError(406, "SSE_ACCEPT_REQUIRED", "input")
                parsed = parse_qs(query, strict_parsing=True) if query else {}
                if set(parsed) - {"after"}:
                    raise _ApiError(400, "SSE_QUERY_INVALID", "input")
                header_cursor = self.headers.get("Last-Event-ID")
                query_cursor = parsed.get("after", [None])[0]
                if header_cursor is not None and query_cursor is not None and header_cursor != query_cursor:
                    raise _ApiError(409, "SSE_CURSOR_DIVERGENT", "conflict")
                cursor_text = header_cursor if header_cursor is not None else query_cursor
                try:
                    cursor = 0 if cursor_text is None else int(cursor_text)
                except ValueError:
                    raise _ApiError(400, "SSE_CURSOR_INVALID", "input") from None
                try:
                    owner._sse.acquire_stream()
                    events = owner._sse.replay(project_id, cursor)
                except DockyardSseCapacityError:
                    raise _ApiError(503, "SSE_CAPACITY", "capacity", True) from None
                except DockyardSseCursorError:
                    raise _ApiError(409, "SSE_CURSOR_STALE", "conflict") from None
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self._send_cors_origin()
                self.end_headers()
                try:
                    if not events:
                        events = owner._sse.wait_for_events(project_id, cursor, owner._config.sse_idle_seconds)
                    for event in events:
                        self.wfile.write(encode_sse_event(event))
                        cursor = event.event_cursor
                    if not events:
                        self.wfile.write(b": idle\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    owner._sse.release_stream()

            def do_GET(self) -> None:
                try:
                    self._guard_transport(command=False)
                    parsed = urlsplit(self.path)
                    event_match = re.fullmatch(rf"{re.escape(_NAMESPACE)}/projects/([^/]+)/events", parsed.path)
                    if event_match:
                        self._events(_identifier(event_match.group(1), "project_id"), parsed.query)
                        return
                    if parsed.query:
                        raise _ApiError(400, "QUERY_FORBIDDEN", "input")
                    request = _read_route(parsed.path)
                    if request is None or request.endpoint not in _READ_PATHS:
                        raise _ApiError(404, "ENDPOINT_NOT_FOUND", "routing")
                    response = owner._read_service.read(request)
                    if not isinstance(response, DockyardJsonResponse):
                        raise _ApiError(500, "READ_SERVICE_INVALID", "internal")
                    _json(response.body, owner._config.max_json_depth)
                    self._send_json(response.status_code, response.body, response.etag)
                except _ApiError as error:
                    self._error(error)
                except Exception:
                    self._error(_ApiError(500, "READ_FAILED", "internal"))

            def _command(self) -> None:
                try:
                    self._guard_transport(command=True)
                    parsed = urlsplit(self.path)
                    if parsed.query:
                        raise _ApiError(400, "QUERY_FORBIDDEN", "input")
                    route = _command_route(self.command, parsed.path)
                    if route is None:
                        raise _ApiError(404, "ENDPOINT_NOT_FOUND", "routing")
                    body = _json(self._body(), owner._config.max_json_depth)
                    if not isinstance(body, dict) or set(body) != {"envelope", "payload"}:
                        raise _ApiError(400, "COMMAND_BODY_INVALID", "input")
                    raw_envelope = body["envelope"]
                    payload = body["payload"]
                    if not isinstance(raw_envelope, dict) or not isinstance(payload, dict):
                        raise _ApiError(400, "COMMAND_BODY_INVALID", "input")
                    try:
                        envelope = DockyardCommandEnvelope(**raw_envelope)
                    except (TypeError, ValueError):
                        raise _ApiError(400, "COMMAND_ENVELOPE_INVALID", "input") from None
                    command_type, project_id, resource_id = route
                    if envelope.command_type != command_type:
                        raise _ApiError(409, "COMMAND_ROUTE_DIVERGENCE", "conflict")
                    if project_id is not None and envelope.project_id != project_id:
                        raise _ApiError(409, "PROJECT_ID_DIVERGENCE", "conflict")
                    header_key = self.headers.get("X-Dockyard-Idempotency-Key")
                    if header_key != envelope.idempotency_key:
                        raise _ApiError(409, "IDEMPOTENCY_DIVERGENCE", "conflict")
                    payload_json = _canonical(payload)
                    if not hmac.compare_digest(hashlib.sha256(payload_json).hexdigest(), envelope.payload_digest):
                        raise _ApiError(409, "PAYLOAD_DIGEST_DIVERGENCE", "conflict")
                    authorization = self.headers.get("Authorization", "")
                    if not authorization.startswith("Bearer "):
                        raise _ApiError(401, "AUTH_REQUIRED", "authentication")
                    token = authorization[7:]
                    device_id = self.headers.get("X-Dockyard-Device-Id", "")
                    csrf = self.headers.get("X-Dockyard-CSRF", "")
                    try:
                        owner._authorizer.authorize(
                            device_id=_identifier(device_id, "device_id"),
                            bearer_token=_text(token, "bearer_token", 512),
                            csrf_value=_identifier(csrf, "csrf"),
                            required_scope=_scope(command_type),
                            operation_id=envelope.command_id,
                        )
                    except Exception:
                        raise _ApiError(403, "AUTH_FORBIDDEN", "authentication") from None
                    receipt = owner._gateway.execute(
                        DockyardCommandRequest(envelope, resource_id, payload_json)
                    )
                    if not isinstance(receipt, DockyardCommandReceipt):
                        raise _ApiError(500, "COMMAND_GATEWAY_INVALID", "internal")
                    self._send_json(200, _canonical(asdict(receipt)))
                except _ApiError as error:
                    self._error(error)
                except DockyardCommandRejected as error:
                    self._error(_ApiError(error.status, error.code, error.category))
                except Exception:
                    self._error(_ApiError(500, "COMMAND_FAILED", "internal"))

            do_POST = _command
            do_PATCH = _command
            do_PUT = _command

            def do_OPTIONS(self) -> None:
                try:
                    self._guard_transport(command=True)
                    requested_method = self.headers.get("Access-Control-Request-Method")
                    if requested_method not in {"POST", "PATCH", "PUT"}:
                        raise _ApiError(405, "PREFLIGHT_METHOD_FORBIDDEN", "security")
                    requested_headers = {
                        value.strip().lower()
                        for value in self.headers.get("Access-Control-Request-Headers", "").split(",")
                        if value.strip()
                    }
                    allowed_headers = {
                        "authorization",
                        "content-type",
                        "x-dockyard-device-id",
                        "x-dockyard-csrf",
                        "x-dockyard-idempotency-key",
                    }
                    if not requested_headers or not requested_headers <= allowed_headers:
                        raise _ApiError(403, "PREFLIGHT_HEADERS_FORBIDDEN", "security")
                    self.send_response(204)
                    self._send_cors_origin()
                    self.send_header("Access-Control-Allow-Methods", "POST, PATCH, PUT")
                    self.send_header(
                        "Access-Control-Allow-Headers",
                        "Authorization, Content-Type, X-Dockyard-Device-Id, "
                        "X-Dockyard-CSRF, X-Dockyard-Idempotency-Key",
                    )
                    self.send_header("Access-Control-Max-Age", "0")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                except _ApiError as error:
                    self._error(error)

            def do_DELETE(self) -> None:
                self._error(_ApiError(405, "METHOD_NOT_ALLOWED", "routing"))

        server_class = _IPv6ThreadingHTTPServer if self._config.bind_host == "::1" else ThreadingHTTPServer
        self._server = server_class((self._config.bind_host, self._config.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        host, port = self._server.server_address[:2]
        return DockyardServerAddress(str(host), int(port))

    def stop(self) -> None:
        if self._server is None or self._thread is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        self._server = None
        self._thread = None
