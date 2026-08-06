"""Local, account-free device pairing core for Dockyard.

Pairing codes and device tokens are returned once. Durable evidence contains
only SHA-256 verifiers. This module performs no network, HTTP, provider, model,
Git, process, or canonical AgentDesk operation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Iterator

from dispatch_supervisor_evidence import _fsync_parent_directory, _is_symlink_or_reparse


PAIRING_SCHEMA = "agentdesk.dockyard-pairing-code/v1"
DEVICE_SCHEMA = "agentdesk.dockyard-device/v1"
MAX_FILE_BYTES = 32 * 1024
MAX_TTL_SECONDS = 600
LOCK_FILENAME = ".dockyard-pairing.lock"
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")
_VERIFIER_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class DockyardPairingError(ValueError):
    """Base pairing error."""


class DockyardPairingInputError(DockyardPairingError):
    """Invalid typed input."""


class DockyardPairingConflictError(DockyardPairingError):
    """Replay, phase, or generation conflict."""


class DockyardPairingAuthenticationError(DockyardPairingError):
    """Invalid code/token, insufficient scope, expiry, or revocation."""


class DockyardPairingStoreError(DockyardPairingError):
    """Durable evidence is missing, corrupt, or unavailable."""


class DockyardDeviceScope(str, Enum):
    READ = "READ"
    APPROVE = "APPROVE"
    RETRY = "RETRY"
    TERMINATE = "TERMINATE"


_CODE_FIELDS = (
    "schema_version",
    "pairing_id",
    "code_verifier",
    "created_at",
    "expires_at",
    "consumed_at",
    "failed_attempts",
    "max_attempts",
    "last_operation_id",
    "content_digest",
)
_DEVICE_FIELDS = (
    "schema_version",
    "device_id",
    "display_name",
    "pairing_id",
    "token_verifier",
    "scopes",
    "issued_at",
    "last_used_at",
    "revoked_at",
    "generation",
    "last_operation_id",
    "last_request_digest",
    "content_digest",
)


def _raise(error_type: type[DockyardPairingError], code: str) -> None:
    raise error_type(code)


def _text(value: object, code: str, maximum: int = 512) -> str:
    if type(value) is not str:
        _raise(DockyardPairingInputError, code)
    if not value or len(value) > maximum or value != value.strip():
        _raise(DockyardPairingInputError, code)
    if any(ord(char) < 0x20 for char in value):
        _raise(DockyardPairingInputError, code)
    return value


def _identifier(value: object, code: str) -> str:
    text = _text(value, code, 192)
    if _ID_RE.fullmatch(text) is None:
        _raise(DockyardPairingInputError, code)
    return text


def _integer(value: object, code: str, minimum: int = 0, maximum: int | None = None) -> int:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        _raise(DockyardPairingInputError, code)
    return value


def _utc(value: object, code: str) -> tuple[str, datetime]:
    text = _text(value, code, 64)
    if not text.endswith("Z"):
        _raise(DockyardPairingInputError, code)
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError:
        _raise(DockyardPairingInputError, code)
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        _raise(DockyardPairingInputError, code)
    return text, parsed


def _verifier(value: object, code: str) -> str:
    text = _text(value, code, 71)
    if _VERIFIER_RE.fullmatch(text) is None:
        _raise(DockyardPairingInputError, code)
    return text


def _hash_secret(secret: str) -> str:
    return "sha256:" + hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _hash_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _request_digest(action: str, values: tuple[str, ...]) -> str:
    encoded = "".join(f"{len(value)}:{value}" for value in (action, *values)).encode("utf-8")
    return _hash_bytes(encoded)


def _canonical(pairs: tuple[tuple[str, object], ...]) -> bytes:
    return (json.dumps(dict(pairs), ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _pairs_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _raise(DockyardPairingStoreError, "duplicate_key")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class DockyardPairingCodeRecord:
    schema_version: str
    pairing_id: str
    code_verifier: str
    created_at: str
    expires_at: str
    consumed_at: str | None
    failed_attempts: int
    max_attempts: int
    last_operation_id: str
    content_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != PAIRING_SCHEMA:
            _raise(DockyardPairingInputError, "code_schema")
        _identifier(self.pairing_id, "pairing_id")
        _verifier(self.code_verifier, "code_verifier")
        _, created = _utc(self.created_at, "created_at")
        _, expires = _utc(self.expires_at, "expires_at")
        if not (0 < (expires - created).total_seconds() <= MAX_TTL_SECONDS):
            _raise(DockyardPairingInputError, "pairing_ttl")
        if self.consumed_at is not None:
            _, consumed = _utc(self.consumed_at, "consumed_at")
            if consumed < created:
                _raise(DockyardPairingInputError, "consumed_clock")
        _integer(self.failed_attempts, "failed_attempts", 0)
        _integer(self.max_attempts, "max_attempts", 1, 10)
        if self.failed_attempts > self.max_attempts:
            _raise(DockyardPairingInputError, "failed_attempt_limit")
        _identifier(self.last_operation_id, "last_operation_id")
        _verifier(self.content_digest, "code_content_digest")


@dataclass(frozen=True, slots=True)
class DockyardDeviceRecord:
    schema_version: str
    device_id: str
    display_name: str
    pairing_id: str
    token_verifier: str
    scopes: tuple[DockyardDeviceScope, ...]
    issued_at: str
    last_used_at: str | None
    revoked_at: str | None
    generation: int
    last_operation_id: str
    last_request_digest: str
    content_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != DEVICE_SCHEMA:
            _raise(DockyardPairingInputError, "device_schema")
        _identifier(self.device_id, "device_id")
        _text(self.display_name, "display_name", 128)
        _identifier(self.pairing_id, "device_pairing_id")
        _verifier(self.token_verifier, "token_verifier")
        if type(self.scopes) is not tuple or not self.scopes:
            _raise(DockyardPairingInputError, "scopes")
        if any(type(scope) is not DockyardDeviceScope for scope in self.scopes):
            _raise(DockyardPairingInputError, "scope_type")
        if tuple(sorted(self.scopes, key=lambda scope: scope.value)) != self.scopes:
            _raise(DockyardPairingInputError, "scope_order")
        if len(set(self.scopes)) != len(self.scopes):
            _raise(DockyardPairingInputError, "scope_duplicate")
        _, issued = _utc(self.issued_at, "issued_at")
        if self.last_used_at is not None:
            _, last_used = _utc(self.last_used_at, "last_used_at")
            if last_used < issued:
                _raise(DockyardPairingInputError, "last_used_clock")
        if self.revoked_at is not None:
            _, revoked = _utc(self.revoked_at, "revoked_at")
            if revoked < issued:
                _raise(DockyardPairingInputError, "revoked_clock")
        _integer(self.generation, "device_generation", 1)
        _identifier(self.last_operation_id, "device_operation_id")
        _verifier(self.last_request_digest, "device_request_digest")
        _verifier(self.content_digest, "device_content_digest")


@dataclass(frozen=True, slots=True)
class DockyardPairingIssueRequest:
    pairing_id: str
    created_at: str
    expires_at: str
    max_attempts: int
    operation_id: str

    def __post_init__(self) -> None:
        _identifier(self.pairing_id, "issue_pairing_id")
        _, created = _utc(self.created_at, "issue_created_at")
        _, expires = _utc(self.expires_at, "issue_expires_at")
        if not (0 < (expires - created).total_seconds() <= MAX_TTL_SECONDS):
            _raise(DockyardPairingInputError, "issue_ttl")
        _integer(self.max_attempts, "issue_max_attempts", 1, 10)
        _identifier(self.operation_id, "issue_operation_id")


@dataclass(frozen=True, slots=True)
class DockyardPairingIssueResult:
    pairing_id: str
    pairing_code: str = field(repr=False)
    expires_at: str


@dataclass(frozen=True, slots=True)
class DockyardPairingConsumeRequest:
    pairing_id: str
    pairing_code: str
    device_id: str
    display_name: str
    scopes: tuple[DockyardDeviceScope, ...]
    consumed_at: str
    operation_id: str

    def __post_init__(self) -> None:
        _identifier(self.pairing_id, "consume_pairing_id")
        _text(self.pairing_code, "pairing_code", 256)
        _identifier(self.device_id, "consume_device_id")
        _text(self.display_name, "consume_display_name", 128)
        if type(self.scopes) is not tuple or not self.scopes:
            _raise(DockyardPairingInputError, "consume_scopes")
        if any(type(scope) is not DockyardDeviceScope for scope in self.scopes):
            _raise(DockyardPairingInputError, "consume_scope_type")
        if tuple(sorted(self.scopes, key=lambda scope: scope.value)) != self.scopes:
            _raise(DockyardPairingInputError, "consume_scope_order")
        if len(set(self.scopes)) != len(self.scopes):
            _raise(DockyardPairingInputError, "consume_scope_duplicate")
        _utc(self.consumed_at, "consume_time")
        _identifier(self.operation_id, "consume_operation_id")


@dataclass(frozen=True, slots=True)
class DockyardPairingConsumeResult:
    device: DockyardDeviceRecord
    device_token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class DockyardAuthenticateRequest:
    device_id: str
    device_token: str
    required_scope: DockyardDeviceScope
    authenticated_at: str
    operation_id: str

    def __post_init__(self) -> None:
        _identifier(self.device_id, "auth_device_id")
        _text(self.device_token, "device_token", 256)
        if type(self.required_scope) is not DockyardDeviceScope:
            _raise(DockyardPairingInputError, "required_scope")
        _utc(self.authenticated_at, "authenticated_at")
        _identifier(self.operation_id, "auth_operation_id")


@dataclass(frozen=True, slots=True)
class DockyardAuthenticationResult:
    device_id: str
    granted_scope: DockyardDeviceScope
    authenticated_at: str
    generation: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class DockyardRevokeDeviceRequest:
    device_id: str
    expected_generation: int
    revoked_at: str
    operation_id: str

    def __post_init__(self) -> None:
        _identifier(self.device_id, "revoke_device_id")
        _integer(self.expected_generation, "revoke_generation", 1)
        _utc(self.revoked_at, "revoked_at")
        _identifier(self.operation_id, "revoke_operation_id")


def _code_pairs(record: DockyardPairingCodeRecord, include_digest: bool) -> tuple[tuple[str, object], ...]:
    pairs: tuple[tuple[str, object], ...] = (
        ("schema_version", record.schema_version),
        ("pairing_id", record.pairing_id),
        ("code_verifier", record.code_verifier),
        ("created_at", record.created_at),
        ("expires_at", record.expires_at),
        ("consumed_at", record.consumed_at),
        ("failed_attempts", record.failed_attempts),
        ("max_attempts", record.max_attempts),
        ("last_operation_id", record.last_operation_id),
    )
    return pairs + ((("content_digest", record.content_digest),) if include_digest else ())


def _device_pairs(record: DockyardDeviceRecord, include_digest: bool) -> tuple[tuple[str, object], ...]:
    pairs: tuple[tuple[str, object], ...] = (
        ("schema_version", record.schema_version),
        ("device_id", record.device_id),
        ("display_name", record.display_name),
        ("pairing_id", record.pairing_id),
        ("token_verifier", record.token_verifier),
        ("scopes", [scope.value for scope in record.scopes]),
        ("issued_at", record.issued_at),
        ("last_used_at", record.last_used_at),
        ("revoked_at", record.revoked_at),
        ("generation", record.generation),
        ("last_operation_id", record.last_operation_id),
        ("last_request_digest", record.last_request_digest),
    )
    return pairs + ((("content_digest", record.content_digest),) if include_digest else ())


def _with_code_digest(record: DockyardPairingCodeRecord) -> DockyardPairingCodeRecord:
    return replace(record, content_digest=_hash_bytes(_canonical(_code_pairs(record, False))))


def _with_device_digest(record: DockyardDeviceRecord) -> DockyardDeviceRecord:
    return replace(record, content_digest=_hash_bytes(_canonical(_device_pairs(record, False))))


def encode_pairing_code_record(record: DockyardPairingCodeRecord) -> bytes:
    if type(record) is not DockyardPairingCodeRecord:
        _raise(DockyardPairingInputError, "encode_code_type")
    if _with_code_digest(record).content_digest != record.content_digest:
        _raise(DockyardPairingConflictError, "code_digest")
    return _canonical(_code_pairs(record, True))


def encode_device_record(record: DockyardDeviceRecord) -> bytes:
    if type(record) is not DockyardDeviceRecord:
        _raise(DockyardPairingInputError, "encode_device_type")
    if _with_device_digest(record).content_digest != record.content_digest:
        _raise(DockyardPairingConflictError, "device_digest")
    return _canonical(_device_pairs(record, True))


def _decode_json(data: bytes, fields: tuple[str, ...]) -> dict[str, object]:
    if type(data) is not bytes or not data or len(data) > MAX_FILE_BYTES:
        _raise(DockyardPairingStoreError, "record_bytes")
    try:
        raw = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs_hook)
    except (UnicodeDecodeError, json.JSONDecodeError):
        _raise(DockyardPairingStoreError, "record_json")
    if type(raw) is not dict or tuple(raw) != fields:
        _raise(DockyardPairingStoreError, "record_fields")
    return raw


def decode_pairing_code_record(data: bytes) -> DockyardPairingCodeRecord:
    raw = _decode_json(data, _CODE_FIELDS)
    try:
        record = DockyardPairingCodeRecord(
            raw["schema_version"], raw["pairing_id"], raw["code_verifier"],
            raw["created_at"], raw["expires_at"], raw["consumed_at"],
            raw["failed_attempts"], raw["max_attempts"], raw["last_operation_id"],
            raw["content_digest"],
        )
        canonical = encode_pairing_code_record(record)
    except (TypeError, ValueError, DockyardPairingError):
        _raise(DockyardPairingStoreError, "code_schema")
    if canonical != data:
        _raise(DockyardPairingStoreError, "code_noncanonical")
    return record


def decode_device_record(data: bytes) -> DockyardDeviceRecord:
    raw = _decode_json(data, _DEVICE_FIELDS)
    try:
        raw_scopes = raw["scopes"]
        if type(raw_scopes) is not list:
            _raise(DockyardPairingStoreError, "device_scopes")
        scopes = tuple(DockyardDeviceScope(value) for value in raw_scopes)
        record = DockyardDeviceRecord(
            raw["schema_version"], raw["device_id"], raw["display_name"],
            raw["pairing_id"], raw["token_verifier"], scopes, raw["issued_at"],
            raw["last_used_at"], raw["revoked_at"], raw["generation"],
            raw["last_operation_id"], raw["last_request_digest"], raw["content_digest"],
        )
        canonical = encode_device_record(record)
    except (TypeError, ValueError, DockyardPairingError):
        _raise(DockyardPairingStoreError, "device_schema")
    if canonical != data:
        _raise(DockyardPairingStoreError, "device_noncanonical")
    return record


_THREAD_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.Lock] = {}


def _thread_lock(root: Path) -> threading.Lock:
    key = os.path.normcase(str(root))
    with _THREAD_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[key] = lock
        return lock


@contextmanager
def _store_lock(root: Path) -> Iterator[None]:
    with _thread_lock(root):
        path = root / LOCK_FILENAME
        token = secrets.token_bytes(32)
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            _raise(DockyardPairingConflictError, "lock_contention")
        except OSError:
            _raise(DockyardPairingStoreError, "lock_create")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(token)
                handle.flush()
                os.fsync(handle.fileno())
            _fsync_parent_directory(path)
            yield
        finally:
            try:
                if not path.exists() or _is_symlink_or_reparse(path):
                    _raise(DockyardPairingStoreError, "lock_replaced")
                if path.read_bytes() != token:
                    _raise(DockyardPairingConflictError, "lock_token")
                path.unlink()
                _fsync_parent_directory(path)
            except DockyardPairingError:
                raise
            except OSError:
                _raise(DockyardPairingStoreError, "lock_release")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if _is_symlink_or_reparse(path.parent):
        _raise(DockyardPairingStoreError, "store_parent_reparse")
    temp = path.parent / f".{path.name}.{secrets.token_hex(12)}.tmp"
    try:
        fd = os.open(str(temp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists() and _is_symlink_or_reparse(path):
            _raise(DockyardPairingStoreError, "record_reparse")
        os.replace(str(temp), str(path))
        _fsync_parent_directory(path)
    except DockyardPairingError:
        raise
    except OSError:
        _raise(DockyardPairingStoreError, "record_write")
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


class DockyardPairingStore:
    def __init__(self, root: Path) -> None:
        if not isinstance(root, Path) or not root.is_absolute():
            _raise(DockyardPairingInputError, "store_root")
        root.mkdir(parents=True, exist_ok=True)
        if not root.is_dir() or _is_symlink_or_reparse(root):
            _raise(DockyardPairingStoreError, "store_root_reparse")
        self._root = root.resolve(strict=True)
        self._codes = self._root / "codes"
        self._devices = self._root / "devices"
        self._codes.mkdir(exist_ok=True)
        self._devices.mkdir(exist_ok=True)
        if _is_symlink_or_reparse(self._codes) or _is_symlink_or_reparse(self._devices):
            _raise(DockyardPairingStoreError, "store_directory_reparse")

    @property
    def root(self) -> Path:
        return self._root

    def _code_path(self, pairing_id: str) -> Path:
        return self._codes / f"{_identifier(pairing_id, 'pairing_id')}.json"

    def _device_path(self, device_id: str) -> Path:
        return self._devices / f"{_identifier(device_id, 'device_id')}.json"

    def _read_code(self, pairing_id: str) -> DockyardPairingCodeRecord | None:
        path = self._code_path(pairing_id)
        if not path.exists():
            return None
        if not path.is_file() or _is_symlink_or_reparse(path):
            _raise(DockyardPairingStoreError, "code_path")
        return decode_pairing_code_record(path.read_bytes())

    def read_device(self, device_id: str) -> DockyardDeviceRecord | None:
        path = self._device_path(device_id)
        if not path.exists():
            return None
        if not path.is_file() or _is_symlink_or_reparse(path):
            _raise(DockyardPairingStoreError, "device_path")
        return decode_device_record(path.read_bytes())

    def verify_device_token(
        self,
        device_id: str,
        device_token: str,
        required_scope: DockyardDeviceScope,
    ) -> DockyardDeviceRecord:
        """Verify a paired device for runtime startup without advancing evidence."""
        _identifier(device_id, "device_id")
        _text(device_token, "device_token", 256)
        if type(required_scope) is not DockyardDeviceScope:
            _raise(DockyardPairingInputError, "required_scope")
        device = self.read_device(device_id)
        if device is None:
            _raise(DockyardPairingAuthenticationError, "device_missing")
        if device.revoked_at is not None:
            _raise(DockyardPairingAuthenticationError, "device_revoked")
        if not hmac.compare_digest(_hash_secret(device_token), device.token_verifier):
            _raise(DockyardPairingAuthenticationError, "device_token")
        if required_scope not in device.scopes:
            _raise(DockyardPairingAuthenticationError, "device_scope")
        return device

    def issue(self, request: DockyardPairingIssueRequest) -> DockyardPairingIssueResult:
        if type(request) is not DockyardPairingIssueRequest:
            _raise(DockyardPairingInputError, "issue_request")
        code = secrets.token_urlsafe(32)
        with _store_lock(self._root):
            if self._read_code(request.pairing_id) is not None:
                _raise(DockyardPairingConflictError, "pairing_exists")
            empty = "sha256:" + "0" * 64
            record = _with_code_digest(
                DockyardPairingCodeRecord(
                    PAIRING_SCHEMA,
                    request.pairing_id,
                    _hash_secret(code),
                    request.created_at,
                    request.expires_at,
                    None,
                    0,
                    request.max_attempts,
                    request.operation_id,
                    empty,
                )
            )
            _atomic_write(self._code_path(request.pairing_id), encode_pairing_code_record(record))
        return DockyardPairingIssueResult(request.pairing_id, code, request.expires_at)

    def consume(self, request: DockyardPairingConsumeRequest) -> DockyardPairingConsumeResult:
        if type(request) is not DockyardPairingConsumeRequest:
            _raise(DockyardPairingInputError, "consume_request")
        token = secrets.token_urlsafe(32)
        with _store_lock(self._root):
            code = self._read_code(request.pairing_id)
            if code is None:
                _raise(DockyardPairingAuthenticationError, "pairing_missing")
            if code.consumed_at is not None:
                _raise(DockyardPairingConflictError, "pairing_consumed")
            _, consumed = _utc(request.consumed_at, "consume_time")
            _, created = _utc(code.created_at, "created_at")
            _, expires = _utc(code.expires_at, "expires_at")
            if consumed < created:
                _raise(DockyardPairingAuthenticationError, "clock_rollback")
            if consumed > expires:
                _raise(DockyardPairingAuthenticationError, "pairing_expired")
            if code.failed_attempts >= code.max_attempts:
                _raise(DockyardPairingAuthenticationError, "pairing_exhausted")
            observed = _hash_secret(request.pairing_code)
            if not hmac.compare_digest(observed, code.code_verifier):
                failed = _with_code_digest(
                    replace(
                        code,
                        failed_attempts=code.failed_attempts + 1,
                        last_operation_id=request.operation_id,
                    )
                )
                _atomic_write(self._code_path(code.pairing_id), encode_pairing_code_record(failed))
                _raise(DockyardPairingAuthenticationError, "pairing_code")
            if self.read_device(request.device_id) is not None:
                _raise(DockyardPairingConflictError, "device_exists")
            scopes = tuple(sorted(request.scopes, key=lambda scope: scope.value))
            request_digest = _request_digest(
                "consume",
                (
                    request.pairing_id,
                    request.device_id,
                    request.display_name,
                    *tuple(scope.value for scope in scopes),
                    request.consumed_at,
                ),
            )
            empty = "sha256:" + "0" * 64
            device = _with_device_digest(
                DockyardDeviceRecord(
                    DEVICE_SCHEMA,
                    request.device_id,
                    request.display_name,
                    request.pairing_id,
                    _hash_secret(token),
                    scopes,
                    request.consumed_at,
                    None,
                    None,
                    1,
                    request.operation_id,
                    request_digest,
                    empty,
                )
            )
            consumed_code = _with_code_digest(
                replace(code, consumed_at=request.consumed_at, last_operation_id=request.operation_id)
            )
            _atomic_write(self._device_path(device.device_id), encode_device_record(device))
            _atomic_write(self._code_path(code.pairing_id), encode_pairing_code_record(consumed_code))
            return DockyardPairingConsumeResult(device, token)

    def authenticate(self, request: DockyardAuthenticateRequest) -> DockyardAuthenticationResult:
        if type(request) is not DockyardAuthenticateRequest:
            _raise(DockyardPairingInputError, "auth_request")
        request_digest = _request_digest(
            "authenticate",
            (
                request.device_id,
                request.required_scope.value,
                request.authenticated_at,
            ),
        )
        with _store_lock(self._root):
            device = self.read_device(request.device_id)
            if device is None:
                _raise(DockyardPairingAuthenticationError, "device_missing")
            if device.revoked_at is not None:
                _raise(DockyardPairingAuthenticationError, "device_revoked")
            if not hmac.compare_digest(_hash_secret(request.device_token), device.token_verifier):
                _raise(DockyardPairingAuthenticationError, "device_token")
            if request.required_scope not in device.scopes:
                _raise(DockyardPairingAuthenticationError, "device_scope")
            if device.last_operation_id == request.operation_id:
                if device.last_request_digest != request_digest:
                    _raise(DockyardPairingConflictError, "divergent_auth_replay")
                return DockyardAuthenticationResult(
                    device.device_id, request.required_scope, request.authenticated_at,
                    device.generation, True,
                )
            _, authenticated = _utc(request.authenticated_at, "authenticated_at")
            _, issued = _utc(device.issued_at, "issued_at")
            if authenticated < issued:
                _raise(DockyardPairingAuthenticationError, "clock_rollback")
            if device.last_used_at is not None:
                _, last_used = _utc(device.last_used_at, "last_used_at")
                if authenticated < last_used:
                    _raise(DockyardPairingAuthenticationError, "clock_rollback")
            updated = _with_device_digest(
                replace(
                    device,
                    last_used_at=request.authenticated_at,
                    generation=device.generation + 1,
                    last_operation_id=request.operation_id,
                    last_request_digest=request_digest,
                )
            )
            _atomic_write(self._device_path(device.device_id), encode_device_record(updated))
            return DockyardAuthenticationResult(
                updated.device_id, request.required_scope, request.authenticated_at,
                updated.generation, False,
            )

    def revoke_device(self, request: DockyardRevokeDeviceRequest) -> DockyardDeviceRecord:
        if type(request) is not DockyardRevokeDeviceRequest:
            _raise(DockyardPairingInputError, "revoke_request")
        request_digest = _request_digest(
            "revoke",
            (request.device_id, str(request.expected_generation), request.revoked_at),
        )
        with _store_lock(self._root):
            device = self.read_device(request.device_id)
            if device is None:
                _raise(DockyardPairingConflictError, "device_missing")
            if device.last_operation_id == request.operation_id:
                if device.last_request_digest != request_digest:
                    _raise(DockyardPairingConflictError, "divergent_revoke_replay")
                return device
            if device.generation != request.expected_generation:
                _raise(DockyardPairingConflictError, "stale_generation")
            if device.revoked_at is not None:
                _raise(DockyardPairingConflictError, "already_revoked")
            _, revoked = _utc(request.revoked_at, "revoked_at")
            _, issued = _utc(device.issued_at, "issued_at")
            if revoked < issued:
                _raise(DockyardPairingAuthenticationError, "clock_rollback")
            updated = _with_device_digest(
                replace(
                    device,
                    revoked_at=request.revoked_at,
                    generation=device.generation + 1,
                    last_operation_id=request.operation_id,
                    last_request_digest=request_digest,
                )
            )
            _atomic_write(self._device_path(device.device_id), encode_device_record(updated))
            return updated

    def revoke_all(self, revoked_at: str, operation_id: str) -> tuple[DockyardDeviceRecord, ...]:
        _utc(revoked_at, "revoke_all_time")
        _identifier(operation_id, "revoke_all_operation_id")
        with _store_lock(self._root):
            entries = tuple(sorted(self._devices.iterdir(), key=lambda path: path.name))
            if len(entries) > 1024:
                _raise(DockyardPairingStoreError, "device_limit")
            records: list[DockyardDeviceRecord] = []
            for path in entries:
                if not path.is_file() or path.suffix != ".json" or _is_symlink_or_reparse(path):
                    _raise(DockyardPairingStoreError, "device_entry")
                device = decode_device_record(path.read_bytes())
                if device.revoked_at is None:
                    request_digest = _request_digest(
                        "revoke_all", (device.device_id, revoked_at, operation_id)
                    )
                    device = _with_device_digest(
                        replace(
                            device,
                            revoked_at=revoked_at,
                            generation=device.generation + 1,
                            last_operation_id=operation_id,
                            last_request_digest=request_digest,
                        )
                    )
                    _atomic_write(path, encode_device_record(device))
                records.append(device)
            return tuple(records)


__all__ = [
    "DockyardAuthenticateRequest",
    "DockyardAuthenticationResult",
    "DockyardDeviceRecord",
    "DockyardDeviceScope",
    "DockyardPairingAuthenticationError",
    "DockyardPairingCodeRecord",
    "DockyardPairingConflictError",
    "DockyardPairingConsumeRequest",
    "DockyardPairingConsumeResult",
    "DockyardPairingError",
    "DockyardPairingInputError",
    "DockyardPairingIssueRequest",
    "DockyardPairingIssueResult",
    "DockyardPairingStore",
    "DockyardPairingStoreError",
    "DockyardRevokeDeviceRequest",
    "decode_device_record",
    "decode_pairing_code_record",
    "encode_device_record",
    "encode_pairing_code_record",
]
