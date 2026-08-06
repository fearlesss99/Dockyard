"""Durable local project registry for Dockyard.

The registry records only explicitly supplied Git repositories.  It never scans
the filesystem and its removal lifecycle never deletes or modifies a Git
repository, worktree, ref, branch, commit, task card, or AgentDesk evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import threading
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Iterator

from dispatch_supervisor_evidence import _fsync_parent_directory, _is_symlink_or_reparse


SCHEMA_VERSION = "agentdesk.dockyard-project/v1"
REGISTRY_DIRECTORY = "projects"
LOCK_FILENAME = ".dockyard-project-registry.lock"
REPOSITORY_MARKER_FILENAME = "agentdesk-dockyard-repository.marker"
REPOSITORY_MARKER_SCHEMA = "agentdesk.dockyard-repository-marker/v1"
MAX_FILE_BYTES = 32 * 1024
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_OPERATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MARKER_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
)


class DockyardProjectRegistryError(ValueError):
    """Base registry error."""


class DockyardProjectInputError(DockyardProjectRegistryError):
    """Invalid typed request."""


class DockyardProjectConflictError(DockyardProjectRegistryError):
    """Replay, CAS, phase, or repository identity conflict."""


class DockyardProjectSecurityError(DockyardProjectRegistryError):
    """Unsafe path, reparse point, or repository replacement."""


class DockyardProjectStoreError(DockyardProjectRegistryError):
    """Corrupt or unavailable durable evidence."""


class DockyardProjectPhase(str, Enum):
    REGISTERED = "REGISTERED"
    DISABLED = "DISABLED"
    REMOVAL_PENDING = "REMOVAL_PENDING"
    REMOVED = "REMOVED"


_RECORD_FIELDS = (
    "schema_version",
    "project_id",
    "display_name",
    "repository_root",
    "repository_common_dir",
    "repository_identity",
    "phase",
    "generation",
    "registered_at",
    "updated_at",
    "removed_at",
    "last_operation_id",
    "last_request_digest",
    "content_digest",
)


def _fail(error_type: type[DockyardProjectRegistryError], code: str) -> None:
    raise error_type(code)


def _text(value: object, code: str, *, maximum: int = 512) -> str:
    if type(value) is not str:
        _fail(DockyardProjectInputError, code)
    if not value or len(value) > maximum or value != value.strip():
        _fail(DockyardProjectInputError, code)
    if any(ord(char) < 0x20 for char in value):
        _fail(DockyardProjectInputError, code)
    return value


def _project_id(value: object) -> str:
    text = _text(value, "project_id", maximum=128)
    if _ID_RE.fullmatch(text) is None:
        _fail(DockyardProjectInputError, "project_id")
    if text.split(".", 1)[0].upper() in _WINDOWS_DEVICE_NAMES:
        _fail(DockyardProjectInputError, "project_id_device")
    return text


def _operation_id(value: object) -> str:
    text = _text(value, "operation_id", maximum=192)
    if _OPERATION_RE.fullmatch(text) is None:
        _fail(DockyardProjectInputError, "operation_id")
    return text


def _timestamp(value: object, code: str) -> str:
    text = _text(value, code, maximum=64)
    if not text.endswith("Z"):
        _fail(DockyardProjectInputError, code)
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError:
        _fail(DockyardProjectInputError, code)
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        _fail(DockyardProjectInputError, code)
    return text


def _positive(value: object, code: str, *, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        _fail(DockyardProjectInputError, code)
    return value


def _digest(value: object, code: str) -> str:
    text = _text(value, code, maximum=71)
    if _SHA256_RE.fullmatch(text) is None:
        _fail(DockyardProjectInputError, code)
    return text


def _absolute_path(value: object, code: str) -> Path:
    if type(value) is not str:
        _fail(DockyardProjectInputError, code)
    path = Path(value)
    if not path.is_absolute():
        _fail(DockyardProjectInputError, code)
    if ".." in path.parts:
        _fail(DockyardProjectSecurityError, f"{code}_traversal")
    return path


def _canonical_json(values: tuple[tuple[str, object], ...]) -> bytes:
    return (
        json.dumps(dict(values), ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _hash_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _identity(
    root: Path,
    common_dir: Path,
    marker_id: str,
    root_node: tuple[int, int],
    common_node: tuple[int, int],
) -> str:
    values = (
        os.path.normcase(str(root)),
        os.path.normcase(str(common_dir)),
        marker_id,
        str(root_node[0]),
        str(root_node[1]),
        str(common_node[0]),
        str(common_node[1]),
    )
    data = "".join(f"{len(value)}:{value}" for value in values).encode("utf-8")
    return _hash_bytes(data)


def _directory_node(path: Path, code: str) -> tuple[int, int]:
    try:
        metadata = os.stat(path, follow_symlinks=False)
    except OSError:
        _fail(DockyardProjectStoreError, code + "_stat")
    if not stat.S_ISDIR(metadata.st_mode):
        _fail(DockyardProjectSecurityError, code + "_type")
    device = int(metadata.st_dev)
    inode = int(metadata.st_ino)
    if device < 0 or inode < 0:
        _fail(DockyardProjectStoreError, code + "_identity")
    return device, inode


def _marker_bytes(marker_id: str) -> bytes:
    if _MARKER_ID_RE.fullmatch(marker_id) is None:
        _fail(DockyardProjectStoreError, "repository_marker_id")
    return _canonical_json((
        ("schema_version", REPOSITORY_MARKER_SCHEMA),
        ("repository_id", marker_id),
    ))


def _read_repository_marker(common_dir: Path) -> str:
    path = common_dir / REPOSITORY_MARKER_FILENAME
    try:
        if _is_symlink_or_reparse(path):
            _fail(DockyardProjectSecurityError, "repository_marker_reparse")
        metadata = os.lstat(path)
        if not stat.S_ISREG(metadata.st_mode):
            _fail(DockyardProjectSecurityError, "repository_marker_type")
        data = path.read_bytes()
    except FileNotFoundError:
        _fail(DockyardProjectStoreError, "repository_marker_missing")
    except DockyardProjectRegistryError:
        raise
    except OSError:
        _fail(DockyardProjectStoreError, "repository_marker_read")
    if not data or len(data) > 256:
        _fail(DockyardProjectStoreError, "repository_marker_bytes")
    try:
        raw = json.loads(data.decode("utf-8"), object_pairs_hook=_object_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail(DockyardProjectStoreError, "repository_marker_json")
    if (
        type(raw) is not dict
        or tuple(raw) != ("schema_version", "repository_id")
        or raw.get("schema_version") != REPOSITORY_MARKER_SCHEMA
        or type(raw.get("repository_id")) is not str
        or _MARKER_ID_RE.fullmatch(raw["repository_id"]) is None
    ):
        _fail(DockyardProjectStoreError, "repository_marker_schema")
    marker_id = raw["repository_id"]
    if data != _marker_bytes(marker_id):
        _fail(DockyardProjectStoreError, "repository_marker_noncanonical")
    return marker_id


def _create_or_read_repository_marker(common_dir: Path) -> str:
    path = common_dir / REPOSITORY_MARKER_FILENAME
    if path.exists():
        return _read_repository_marker(common_dir)
    marker_id = secrets.token_hex(32)
    data = _marker_bytes(marker_id)
    temporary = common_dir / (
        "." + REPOSITORY_MARKER_FILENAME + "." + secrets.token_hex(16) + ".tmp"
    )
    try:
        descriptor = os.open(
            str(temporary),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
            _fsync_parent_directory(path)
        except FileExistsError:
            pass
    except DockyardProjectRegistryError:
        raise
    except OSError:
        _fail(DockyardProjectStoreError, "repository_marker_create")
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return _read_repository_marker(common_dir)


@dataclass(frozen=True, slots=True)
class DockyardProjectRecord:
    schema_version: str
    project_id: str
    display_name: str
    repository_root: str
    repository_common_dir: str
    repository_identity: str
    phase: DockyardProjectPhase
    generation: int
    registered_at: str
    updated_at: str
    removed_at: str | None
    last_operation_id: str
    last_request_digest: str
    content_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            _fail(DockyardProjectInputError, "record_schema")
        _project_id(self.project_id)
        _text(self.display_name, "record_display_name", maximum=128)
        _absolute_path(self.repository_root, "record_repository_root")
        _absolute_path(self.repository_common_dir, "record_common_dir")
        _digest(self.repository_identity, "record_repository_identity")
        if type(self.phase) is not DockyardProjectPhase:
            _fail(DockyardProjectInputError, "record_phase")
        _positive(self.generation, "record_generation")
        _timestamp(self.registered_at, "record_registered_at")
        _timestamp(self.updated_at, "record_updated_at")
        if self.removed_at is not None:
            _timestamp(self.removed_at, "record_removed_at")
        if (self.phase is DockyardProjectPhase.REMOVED) != (self.removed_at is not None):
            _fail(DockyardProjectInputError, "record_removed_time")
        _operation_id(self.last_operation_id)
        _digest(self.last_request_digest, "record_request_digest")
        _digest(self.content_digest, "record_content_digest")


@dataclass(frozen=True, slots=True)
class DockyardRegisterProjectRequest:
    project_id: str
    display_name: str
    repository_root: str
    operation_id: str
    registered_at: str

    def __post_init__(self) -> None:
        _project_id(self.project_id)
        _text(self.display_name, "display_name", maximum=128)
        _absolute_path(self.repository_root, "repository_root")
        _operation_id(self.operation_id)
        _timestamp(self.registered_at, "registered_at")


@dataclass(frozen=True, slots=True)
class DockyardProjectCommand:
    project_id: str
    expected_repository_identity: str
    expected_generation: int
    operation_id: str
    occurred_at: str

    def __post_init__(self) -> None:
        _project_id(self.project_id)
        _digest(self.expected_repository_identity, "expected_repository_identity")
        _positive(self.expected_generation, "expected_generation")
        _operation_id(self.operation_id)
        _timestamp(self.occurred_at, "occurred_at")


@dataclass(frozen=True, slots=True)
class DockyardProjectResult:
    record: DockyardProjectRecord
    changed: bool
    replayed: bool

    def __post_init__(self) -> None:
        if type(self.record) is not DockyardProjectRecord:
            _fail(DockyardProjectInputError, "result_record")
        if type(self.changed) is not bool or type(self.replayed) is not bool:
            _fail(DockyardProjectInputError, "result_flags")
        if self.changed and self.replayed:
            _fail(DockyardProjectInputError, "result_conflicting_flags")


def _record_pairs(record: DockyardProjectRecord, include_digest: bool) -> tuple[tuple[str, object], ...]:
    pairs: tuple[tuple[str, object], ...] = (
        ("schema_version", record.schema_version),
        ("project_id", record.project_id),
        ("display_name", record.display_name),
        ("repository_root", record.repository_root),
        ("repository_common_dir", record.repository_common_dir),
        ("repository_identity", record.repository_identity),
        ("phase", record.phase.value),
        ("generation", record.generation),
        ("registered_at", record.registered_at),
        ("updated_at", record.updated_at),
        ("removed_at", record.removed_at),
        ("last_operation_id", record.last_operation_id),
        ("last_request_digest", record.last_request_digest),
    )
    if include_digest:
        return pairs + (("content_digest", record.content_digest),)
    return pairs


def _with_record_digest(record: DockyardProjectRecord) -> DockyardProjectRecord:
    digest = _hash_bytes(_canonical_json(_record_pairs(record, False)))
    return replace(record, content_digest=digest)


def encode_project_record(record: DockyardProjectRecord) -> bytes:
    if type(record) is not DockyardProjectRecord:
        _fail(DockyardProjectInputError, "encode_record_type")
    expected = _with_record_digest(record)
    if record.content_digest != expected.content_digest:
        _fail(DockyardProjectConflictError, "record_digest")
    return _canonical_json(_record_pairs(record, True))


def _object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail(DockyardProjectStoreError, "duplicate_key")
        result[key] = value
    return result


def decode_project_record(data: bytes) -> DockyardProjectRecord:
    if type(data) is not bytes or not data or len(data) > MAX_FILE_BYTES:
        _fail(DockyardProjectStoreError, "record_bytes")
    try:
        raw = json.loads(data.decode("utf-8"), object_pairs_hook=_object_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail(DockyardProjectStoreError, "record_json")
    if type(raw) is not dict or tuple(raw.keys()) != _RECORD_FIELDS:
        _fail(DockyardProjectStoreError, "record_fields")
    try:
        phase = DockyardProjectPhase(raw["phase"])
        record = DockyardProjectRecord(
            schema_version=raw["schema_version"],
            project_id=raw["project_id"],
            display_name=raw["display_name"],
            repository_root=raw["repository_root"],
            repository_common_dir=raw["repository_common_dir"],
            repository_identity=raw["repository_identity"],
            phase=phase,
            generation=raw["generation"],
            registered_at=raw["registered_at"],
            updated_at=raw["updated_at"],
            removed_at=raw["removed_at"],
            last_operation_id=raw["last_operation_id"],
            last_request_digest=raw["last_request_digest"],
            content_digest=raw["content_digest"],
        )
    except (KeyError, TypeError, ValueError, DockyardProjectRegistryError):
        _fail(DockyardProjectStoreError, "record_schema")
    try:
        canonical = encode_project_record(record)
    except DockyardProjectConflictError:
        _fail(DockyardProjectStoreError, "record_digest")
    if canonical != data:
        _fail(DockyardProjectStoreError, "record_noncanonical")
    return record


def _run_git(repository_root: Path, arguments: tuple[str, ...]) -> str:
    try:
        result = subprocess.run(
            ("git", "-C", str(repository_root), *arguments),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            check=False,
            timeout=15.0,
        )
    except (OSError, subprocess.SubprocessError):
        _fail(DockyardProjectStoreError, "git_unavailable")
    if result.returncode != 0:
        _fail(DockyardProjectInputError, "not_git_repository")
    try:
        text = result.stdout.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError:
        _fail(DockyardProjectStoreError, "git_output_encoding")
    if not text or "\n" in text or "\r" in text:
        _fail(DockyardProjectStoreError, "git_output_shape")
    return text


def _repository_evidence(
    repository_root: str,
    *,
    create_marker: bool = False,
) -> tuple[Path, Path, str]:
    requested = _absolute_path(repository_root, "repository_root")
    if not requested.exists() or not requested.is_dir():
        _fail(DockyardProjectInputError, "repository_missing")
    if _is_symlink_or_reparse(requested):
        _fail(DockyardProjectSecurityError, "repository_reparse")
    top = Path(_run_git(requested, ("rev-parse", "--show-toplevel")))
    if not top.is_absolute():
        _fail(DockyardProjectStoreError, "repository_top_relative")
    canonical_root = top.resolve(strict=True)
    if _is_symlink_or_reparse(canonical_root):
        _fail(DockyardProjectSecurityError, "repository_top_reparse")
    common_text = _run_git(canonical_root, ("rev-parse", "--git-common-dir"))
    common = Path(common_text)
    if not common.is_absolute():
        common = canonical_root / common
    canonical_common = common.resolve(strict=True)
    if _is_symlink_or_reparse(canonical_common):
        _fail(DockyardProjectSecurityError, "repository_common_reparse")
    marker_id = (
        _create_or_read_repository_marker(canonical_common)
        if create_marker
        else _read_repository_marker(canonical_common)
    )
    identity = _identity(
        canonical_root,
        canonical_common,
        marker_id,
        _directory_node(canonical_root, "repository_root"),
        _directory_node(canonical_common, "repository_common"),
    )
    return canonical_root, canonical_common, identity


def _request_digest(action: str, values: tuple[str, ...]) -> str:
    encoded = "".join(
        f"{len(value)}:{value}" for value in (action, *values)
    ).encode("utf-8")
    return _hash_bytes(encoded)


_THREAD_LOCK_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.Lock] = {}


def _thread_lock(root: Path) -> threading.Lock:
    key = os.path.normcase(str(root))
    with _THREAD_LOCK_GUARD:
        current = _THREAD_LOCKS.get(key)
        if current is None:
            current = threading.Lock()
            _THREAD_LOCKS[key] = current
        return current


@contextmanager
def _registry_lock(root: Path) -> Iterator[None]:
    with _thread_lock(root):
        path = root / LOCK_FILENAME
        token = secrets.token_bytes(32)
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            _fail(DockyardProjectConflictError, "lock_contention")
        except OSError:
            _fail(DockyardProjectStoreError, "lock_create")
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
                    _fail(DockyardProjectSecurityError, "lock_replaced")
                if path.read_bytes() != token:
                    _fail(DockyardProjectConflictError, "lock_token")
                path.unlink()
                _fsync_parent_directory(path)
            except DockyardProjectRegistryError:
                raise
            except OSError:
                _fail(DockyardProjectStoreError, "lock_release")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if _is_symlink_or_reparse(path.parent):
        _fail(DockyardProjectSecurityError, "store_parent_reparse")
    temp = path.parent / f".{path.name}.{secrets.token_hex(12)}.tmp"
    try:
        fd = os.open(str(temp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists() and _is_symlink_or_reparse(path):
            _fail(DockyardProjectSecurityError, "record_reparse")
        os.replace(str(temp), str(path))
        _fsync_parent_directory(path)
    except DockyardProjectRegistryError:
        raise
    except OSError:
        _fail(DockyardProjectStoreError, "record_write")
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


class DockyardProjectRegistry:
    def __init__(self, registry_root: Path) -> None:
        if not isinstance(registry_root, Path) or not registry_root.is_absolute():
            _fail(DockyardProjectInputError, "registry_root")
        registry_root.mkdir(parents=True, exist_ok=True)
        if not registry_root.is_dir() or _is_symlink_or_reparse(registry_root):
            _fail(DockyardProjectSecurityError, "registry_root_reparse")
        self._root = registry_root.resolve(strict=True)
        self._projects = self._root / REGISTRY_DIRECTORY
        self._projects.mkdir(exist_ok=True)
        if _is_symlink_or_reparse(self._projects):
            _fail(DockyardProjectSecurityError, "projects_reparse")

    @property
    def root(self) -> Path:
        return self._root

    def _path(self, project_id: str) -> Path:
        return self._projects / f"{_project_id(project_id)}.json"

    def read(self, project_id: str) -> DockyardProjectRecord | None:
        path = self._path(project_id)
        if not path.exists():
            return None
        if not path.is_file() or _is_symlink_or_reparse(path):
            _fail(DockyardProjectSecurityError, "record_path")
        try:
            data = path.read_bytes()
        except OSError:
            _fail(DockyardProjectStoreError, "record_read")
        return decode_project_record(data)

    def register(self, request: DockyardRegisterProjectRequest) -> DockyardProjectResult:
        if type(request) is not DockyardRegisterProjectRequest:
            _fail(DockyardProjectInputError, "register_request")
        root, common, identity = _repository_evidence(
            request.repository_root,
            create_marker=True,
        )
        request_digest = _request_digest(
            "register",
            (
                request.project_id,
                request.display_name,
                str(root),
                str(common),
                identity,
                request.registered_at,
            ),
        )
        with _registry_lock(self._root):
            current = self.read(request.project_id)
            if current is not None:
                return self._replay(current, request.operation_id, request_digest)
            empty_digest = "sha256:" + "0" * 64
            record = _with_record_digest(
                DockyardProjectRecord(
                    schema_version=SCHEMA_VERSION,
                    project_id=request.project_id,
                    display_name=request.display_name,
                    repository_root=str(root),
                    repository_common_dir=str(common),
                    repository_identity=identity,
                    phase=DockyardProjectPhase.REGISTERED,
                    generation=1,
                    registered_at=request.registered_at,
                    updated_at=request.registered_at,
                    removed_at=None,
                    last_operation_id=request.operation_id,
                    last_request_digest=request_digest,
                    content_digest=empty_digest,
                )
            )
            _atomic_write(self._path(request.project_id), encode_project_record(record))
            return DockyardProjectResult(record, True, False)

    def disable(self, command: DockyardProjectCommand) -> DockyardProjectResult:
        return self._advance(command, "disable", DockyardProjectPhase.REGISTERED, DockyardProjectPhase.DISABLED)

    def request_removal(self, command: DockyardProjectCommand) -> DockyardProjectResult:
        return self._advance(
            command,
            "request_removal",
            (DockyardProjectPhase.REGISTERED, DockyardProjectPhase.DISABLED),
            DockyardProjectPhase.REMOVAL_PENDING,
        )

    def finalize_removal(self, command: DockyardProjectCommand) -> DockyardProjectResult:
        return self._advance(
            command,
            "finalize_removal",
            DockyardProjectPhase.REMOVAL_PENDING,
            DockyardProjectPhase.REMOVED,
        )

    def _replay(
        self,
        current: DockyardProjectRecord,
        operation_id: str,
        request_digest: str,
    ) -> DockyardProjectResult:
        if current.last_operation_id != operation_id:
            _fail(DockyardProjectConflictError, "project_exists")
        if current.last_request_digest != request_digest:
            _fail(DockyardProjectConflictError, "divergent_replay")
        return DockyardProjectResult(current, False, True)

    def _advance(
        self,
        command: DockyardProjectCommand,
        action: str,
        allowed_phase: DockyardProjectPhase | tuple[DockyardProjectPhase, ...],
        new_phase: DockyardProjectPhase,
    ) -> DockyardProjectResult:
        if type(command) is not DockyardProjectCommand:
            _fail(DockyardProjectInputError, "command_type")
        allowed = allowed_phase if type(allowed_phase) is tuple else (allowed_phase,)
        request_digest = _request_digest(
            action,
            (
                command.project_id,
                command.expected_repository_identity,
                str(command.expected_generation),
                command.occurred_at,
            ),
        )
        with _registry_lock(self._root):
            current = self.read(command.project_id)
            if current is None:
                _fail(DockyardProjectConflictError, "project_missing")
            if current.last_operation_id == command.operation_id:
                if current.last_request_digest != request_digest:
                    _fail(DockyardProjectConflictError, "divergent_replay")
                return DockyardProjectResult(current, False, True)
            if current.repository_identity != command.expected_repository_identity:
                _fail(DockyardProjectConflictError, "repository_identity")
            if current.generation != command.expected_generation:
                _fail(DockyardProjectConflictError, "stale_generation")
            if current.phase not in allowed:
                _fail(DockyardProjectConflictError, "phase")
            root, common, observed_identity = _repository_evidence(current.repository_root)
            if (
                str(root) != current.repository_root
                or str(common) != current.repository_common_dir
                or observed_identity != current.repository_identity
            ):
                _fail(DockyardProjectSecurityError, "repository_replaced")
            record = _with_record_digest(
                replace(
                    current,
                    phase=new_phase,
                    generation=current.generation + 1,
                    updated_at=command.occurred_at,
                    removed_at=(
                        command.occurred_at
                        if new_phase is DockyardProjectPhase.REMOVED
                        else None
                    ),
                    last_operation_id=command.operation_id,
                    last_request_digest=request_digest,
                )
            )
            _atomic_write(self._path(command.project_id), encode_project_record(record))
            return DockyardProjectResult(record, True, False)


__all__ = [
    "DockyardProjectCommand",
    "DockyardProjectConflictError",
    "DockyardProjectInputError",
    "DockyardProjectPhase",
    "DockyardProjectRecord",
    "DockyardProjectRegistry",
    "DockyardProjectRegistryError",
    "DockyardProjectResult",
    "DockyardProjectSecurityError",
    "DockyardProjectStoreError",
    "DockyardRegisterProjectRequest",
    "SCHEMA_VERSION",
    "decode_project_record",
    "encode_project_record",
]
