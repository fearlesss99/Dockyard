"""AgentDesk MAD refs runtime store — agentdesk.mad-refs/v1 (TC-13.6).

A gitignored, append-only, concurrency-safe record of every MAD
subprocess invocation.  Written as JSON‑compatible YAML (UTF-8,
``ensure_ascii=False``, stable indentation, trailing newline) at
``.agentdesk/runtime/mad-refs.yaml``.

Public API
----------
``MadRefEntry``          – frozen :class:`dataclass` with the 10
                          required fields.
``read_mad_refs``        – read & validate the runtime file.
``append_mad_ref``       – lock → read → validate → dedupe → append →
                          atomic write → unlock.
``validate_mad_refs``    – pure validation, returns a list of error
                          messages (empty = valid).
``find_existing_ref``    – lookup by ``(dispatch_id, purpose)``.
``LockContentionError``  – raised when another writer holds the lock.
``MadRefsValidationError`` – raised for schema / content violations.
``RefIntegrityError``    – raised for duplicate ref entries.

Non‑goals
---------
* MAD subprocess invocation, Gateway, WorkerAdapter, budget, slot,
  lease, scheduling, retry, escalation, or rate‑limit logic.
* Importing MAD internal modules.
* Subprocess, network, or third‑party library calls.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import warnings
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = [
    "ALLOWED_DEPTHS",
    "ALLOWED_PURPOSES",
    "LockContentionError",
    "MadRefEntry",
    "MadRefsValidationError",
    "RefIntegrityError",
    "SCHEMA_VERSION",
    "append_mad_ref",
    "find_existing_ref",
    "read_mad_refs",
    "validate_mad_refs",
]

# ── constants ─────────────────────────────────────────────────────────────

SCHEMA_VERSION = "agentdesk.mad-refs/v1"

ALLOWED_PURPOSES: frozenset[str] = frozenset({"planning", "audit"})
ALLOWED_DEPTHS: frozenset[str] = frozenset({"fast", "balanced", "deep"})

_RUNTIME_RELATIVE = Path(".agentdesk") / "runtime" / "mad-refs.yaml"

_REF_FIELD_NAMES: tuple[str, ...] = (
    "task_id",
    "dispatch_id",
    "purpose",
    "deliberation_id",
    "depth",
    "stdout_sha256",
    "report_sha256",
    "status",
    "archive_path",
    "created_at",
)
_REF_FIELD_SET: frozenset[str] = frozenset(_REF_FIELD_NAMES)

_ROOT_FIELD_NAMES: tuple[str, ...] = ("schema_version", "updated_at", "refs")
_ROOT_FIELD_SET: frozenset[str] = frozenset(_ROOT_FIELD_NAMES)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# RFC 3339 with mandatory timezone: ``Z`` or ``±HH:MM``.
_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)

# Sentinel used during fresh-file creation so the caller can distinguish
# "created new" from "appended to existing".
_NEW_FILE_SENTINEL = "__mad_refs_new_file__"


# ── data types ────────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class MadRefEntry:
    """One immutable MAD invocation record — exactly 10 fields."""

    task_id: str
    dispatch_id: str
    purpose: str                     # "planning" | "audit"
    deliberation_id: str
    depth: str                       # "fast" | "balanced" | "deep"
    stdout_sha256: str               # 64 lowercase hex
    report_sha256: str               # 64 lowercase hex
    status: str                      # from MAD stdout (Chinese preserved)
    archive_path: str                # absolute path, runtime-only
    created_at: str                  # RFC 3339 UTC


# ── errors ────────────────────────────────────────────────────────────────


class MadRefsError(Exception):
    """Base for all ``mad_refs`` errors."""


class LockContentionError(MadRefsError):
    """Another writer holds the ``.mad-refs.lock`` lock file."""


class MadRefsValidationError(MadRefsError):
    """Schema or field validation failure in ``mad-refs.yaml``."""


class RefIntegrityError(MadRefsError):
    """Duplicate ref detected — insertion refused."""


# ── lock helpers ──────────────────────────────────────────────────────────


def _lock_path(runtime_dir: Path) -> Path:
    """Return the filesystem path of the advisory lock file."""
    return runtime_dir / ".mad-refs.lock"


def _acquire_lock(runtime_dir: Path) -> Path:
    """Atomically create the lock file.

    Returns the *lock_path* so the caller can release it later.
    Raises :exc:`LockContentionError` when the lock already exists.
    """
    lock = _lock_path(runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(
            str(lock),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o644,
        )
    except FileExistsError:
        raise LockContentionError(
            f"another writer holds the lock: {lock}"
        ) from None
    except OSError as exc:
        raise MadRefsError(f"cannot create lock file {lock}: {exc}") from exc
    os.close(fd)
    return lock


def _release_lock(lock_path: Path) -> None:
    """Remove the advisory lock file.

    Idempotent — does not raise if the lock is already gone.
    """
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise MadRefsError(
            f"cannot release lock {lock_path}: {exc}"
        ) from exc


# ── validation ────────────────────────────────────────────────────────────


def _validate_root_keys(data: dict[str, Any]) -> list[str]:
    """Validate the root object has exactly the three allowed keys."""
    errors: list[str] = []
    actual = set(data.keys())
    missing = _ROOT_FIELD_SET - actual
    extra = actual - _ROOT_FIELD_SET
    if missing:
        errors.append(
            f"missing root key(s): {', '.join(sorted(missing))}"
        )
    if extra:
        errors.append(
            f"forbidden root key(s): {', '.join(sorted(extra))}"
        )
    return errors


def _validate_ref_keys(ref: dict[str, Any], index: int) -> list[str]:
    """Validate a single ref entry has exactly the 10 allowed fields."""
    errors: list[str] = []
    actual = set(ref.keys())
    missing = _REF_FIELD_SET - actual
    extra = actual - _REF_FIELD_SET
    prefix = f"refs[{index}]"
    if missing:
        errors.append(
            f"{prefix} missing field(s): {', '.join(sorted(missing))}"
        )
    if extra:
        errors.append(
            f"{prefix} forbidden field(s): {', '.join(sorted(extra))}"
        )
    return errors


def _validate_ref_values(ref: dict[str, Any], index: int) -> list[str]:
    """Validate field values within a ref entry."""
    errors: list[str] = []
    prefix = f"refs[{index}]"

    # ── string fields that must be non-empty ──────────────────────────
    _required_non_empty = (
        "task_id",
        "dispatch_id",
        "deliberation_id",
        "status",
        "archive_path",
        "created_at",
    )
    for key in _required_non_empty:
        value = ref.get(key)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{prefix}.{key} must be a non-empty string")

    # ── purpose ───────────────────────────────────────────────────────
    purpose = ref.get("purpose")
    if not isinstance(purpose, str) or purpose not in ALLOWED_PURPOSES:
        errors.append(
            f"{prefix}.purpose must be one of "
            f"{', '.join(sorted(ALLOWED_PURPOSES))}, got {purpose!r}"
        )

    # ── depth ─────────────────────────────────────────────────────────
    depth = ref.get("depth")
    if not isinstance(depth, str) or depth not in ALLOWED_DEPTHS:
        errors.append(
            f"{prefix}.depth must be one of "
            f"{', '.join(sorted(ALLOWED_DEPTHS))}, got {depth!r}"
        )

    # ── SHA-256 fields (64 lowercase hex) ─────────────────────────────
    stdout_sha = ref.get("stdout_sha256")
    if not isinstance(stdout_sha, str) or not _SHA256_RE.fullmatch(stdout_sha):
        errors.append(
            f"{prefix}.stdout_sha256 must be 64 lowercase hex characters"
        )

    report_sha = ref.get("report_sha256")
    if not isinstance(report_sha, str) or not _SHA256_RE.fullmatch(report_sha):
        errors.append(
            f"{prefix}.report_sha256 must be 64 lowercase hex characters"
        )

    # ── created_at RFC 3339 ───────────────────────────────────────────
    created = ref.get("created_at")
    if isinstance(created, str) and not _RFC3339_RE.fullmatch(created):
        errors.append(
            f"{prefix}.created_at must be RFC 3339, got {created!r}"
        )

    # ── archive_path must be absolute ─────────────────────────────────
    archive = ref.get("archive_path")
    if isinstance(archive, str) and archive.strip():
        p = Path(archive)
        if not p.is_absolute():
            errors.append(
                f"{prefix}.archive_path must be absolute, got {archive!r}"
            )

    return errors


def validate_mad_refs(data: dict[str, Any]) -> list[str]:
    """Validate *data* against the ``agentdesk.mad-refs/v1`` schema.

    Returns a list of human-readable error messages.  An empty list
    means *data* is valid.

    This is a pure function — no I/O, no side effects.
    """
    errors: list[str] = []

    # ── top-level must be a dict ──────────────────────────────────────
    if not isinstance(data, dict):
        return ["root must be an object"]

    # ── schema_version ────────────────────────────────────────────────
    sv = data.get("schema_version")
    if sv != SCHEMA_VERSION:
        errors.append(
            f"schema_version must be {SCHEMA_VERSION!r}, got {sv!r}"
        )

    # ── updated_at ────────────────────────────────────────────────────
    ua = data.get("updated_at")
    if not isinstance(ua, str) or not ua.strip():
        errors.append("updated_at must be a non-empty RFC 3339 string")

    # ── refs must be a list ───────────────────────────────────────────
    refs = data.get("refs")
    if not isinstance(refs, list):
        errors.append("refs must be an array")
        # Cannot validate further — return early.
        errors.extend(_validate_root_keys(data))
        return errors

    # ── exact root keys ───────────────────────────────────────────────
    errors.extend(_validate_root_keys(data))

    # ── each ref ──────────────────────────────────────────────────────
    seen_pairs: set[tuple[str, str]] = set()
    for i, ref in enumerate(refs):
        if not isinstance(ref, dict):
            errors.append(f"refs[{i}] must be an object")
            continue
        errors.extend(_validate_ref_keys(ref, i))
        errors.extend(_validate_ref_values(ref, i))

        # ── duplicate record check ─────────────────────────────────
        did = ref.get("dispatch_id")
        purp = ref.get("purpose")
        if isinstance(did, str) and isinstance(purp, str):
            pair = (did, purp)
            if pair in seen_pairs:
                errors.append(
                    f"refs[{i}] duplicate (dispatch_id={did!r}, "
                    f"purpose={purp!r})"
                )
            seen_pairs.add(pair)

    return errors


# ── read / find ───────────────────────────────────────────────────────────


def read_mad_refs(project_root: Path) -> dict[str, Any]:
    """Read & validate ``.agentdesk/runtime/mad-refs.yaml``.

    Returns the parsed data dict.  Raises :exc:`MadRefsValidationError`
    for schema violations, :exc:`FileNotFoundError` when the file does
    not exist, or :exc:`OSError` / :exc:`json.JSONDecodeError` for
    filesystem / parse errors.

    This function does **not** acquire the lock — it is read‑only.
    """
    path = project_root / _RUNTIME_RELATIVE
    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MadRefsValidationError(
            f"mad-refs.yaml is not valid JSON: {exc}"
        ) from exc
    errors = validate_mad_refs(data)
    if errors:
        raise MadRefsValidationError(
            f"mad-refs.yaml validation failed:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )
    return data


def find_existing_ref(
    refs_data: dict[str, Any],
    dispatch_id: str,
    purpose: str,
) -> MadRefEntry | None:
    """Return the ref matching ``(dispatch_id, purpose)``, or ``None``.

    The caller should already have validated *refs_data*.
    """
    refs: list[dict[str, Any]] = refs_data.get("refs", [])
    for ref in refs:
        if (
            isinstance(ref, dict)
            and ref.get("dispatch_id") == dispatch_id
            and ref.get("purpose") == purpose
        ):
            return MadRefEntry(**{k: ref[k] for k in _REF_FIELD_NAMES})
    return None


# ── persist ───────────────────────────────────────────────────────────────


def _fresh_document() -> dict[str, Any]:
    """Return a new, empty ``mad-refs/v1`` document."""
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": _utc_now(),
        "refs": [],
    }


def _utc_now() -> str:
    """RFC 3339 UTC timestamp string."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _serialize(data: dict[str, Any]) -> str:
    """Serialize *data* to JSON‑compatible YAML text."""
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def _ref_to_dict(entry: MadRefEntry) -> dict[str, str]:
    """Convert a :class:`MadRefEntry` to a plain dict."""
    return {
        "task_id": entry.task_id,
        "dispatch_id": entry.dispatch_id,
        "purpose": entry.purpose,
        "deliberation_id": entry.deliberation_id,
        "depth": entry.depth,
        "stdout_sha256": entry.stdout_sha256,
        "report_sha256": entry.report_sha256,
        "status": entry.status,
        "archive_path": entry.archive_path,
        "created_at": entry.created_at,
    }


def _atomic_write(path: Path, content: str) -> None:
    """Atomically replace *path* with *content* using a unique temp file.

    Follows the same pattern as :func:`render_views._atomic_write`.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    # Preserve previous mode if the file exists.
    previous_mode: int | None = None
    try:
        previous_mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        pass

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, previous_mode if previous_mode is not None else 0o644)
        os.replace(tmp, path)
        # fsync parent directory.
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
        except OSError:
            dir_fd = None
        if dir_fd is not None:
            try:
                os.fsync(dir_fd)
            except OSError:
                pass
            finally:
                os.close(dir_fd)
    except BaseException:
        # Clean up temp file on any failure — the original file is
        # untouched because ``os.replace`` never ran.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def append_mad_ref(
    project_root: Path,
    entry: MadRefEntry,
) -> dict[str, Any]:
    """Append one ref to ``mad-refs.yaml`` with full concurrency safety.

    The operation is:

    1. Acquire exclusive lock.
    2. Read existing document (or create fresh).
    3. Validate existing document.
    4. Check for idempotent replay (same ``dispatch_id`` + ``purpose``).
    5. Check for duplicate ``deliberation_id``.
    6. Validate the new entry in isolation.
    7. Re‑validate the combined document.
    8. Atomically write.
    9. Release lock.

    Returns the full refs data dict (with the new entry appended).
    Raises :exc:`LockContentionError`, :exc:`MadRefsValidationError`,
    or :exc:`RefIntegrityError` on failure — the original file bytes
    are never modified.
    """
    runtime_dir = project_root / ".agentdesk" / "runtime"
    refs_path = project_root / _RUNTIME_RELATIVE
    lock_path = _acquire_lock(runtime_dir)
    try:
        # ── 1. Read existing (or create fresh) ───────────────────────
        created_new = False
        try:
            raw = refs_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            data = _fresh_document()
            created_new = True
        else:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise MadRefsValidationError(
                    f"mad-refs.yaml is not valid JSON: {exc}"
                ) from exc

        # ── 2. Validate existing document ────────────────────────────
        existing_errors = validate_mad_refs(data)
        if existing_errors:
            raise MadRefsValidationError(
                "existing mad-refs.yaml is invalid — refusing to append:\n"
                + "\n".join(f"  - {e}" for e in existing_errors)
            )

        # ── 3. Idempotent replay check ───────────────────────────────
        refs: list[dict[str, Any]] = data.setdefault("refs", [])
        existing = find_existing_ref(data, entry.dispatch_id, entry.purpose)
        if existing is not None:
            # Idempotent — the exact same logical invocation is already
            # recorded.  Return existing data unchanged.
            return data

        # ── 4. Duplicate deliberation_id check ───────────────────────
        for ref in refs:
            if (
                isinstance(ref, dict)
                and ref.get("deliberation_id") == entry.deliberation_id
                and ref.get("dispatch_id") != entry.dispatch_id
            ):
                warnings.warn(
                    f"deliberation_id {entry.deliberation_id!r} is shared "
                    f"by multiple dispatches: "
                    f"{ref.get('dispatch_id')!r} and {entry.dispatch_id!r}",
                    UserWarning,
                )

        # ── 5. Validate new entry in isolation ───────────────────────
        new_dict = _ref_to_dict(entry)
        single_errors = _validate_ref_keys(new_dict, 0)
        single_errors.extend(_validate_ref_values(new_dict, 0))
        if single_errors:
            raise MadRefsValidationError(
                "new ref entry is invalid:\n"
                + "\n".join(f"  - {e}" for e in single_errors)
            )

        # ── 6. Check for exact duplicate (all 10 fields equal) ───────
        for ref in refs:
            if all(
                isinstance(ref, dict) and ref.get(k) == v
                for k, v in new_dict.items()
            ):
                raise RefIntegrityError(
                    f"identical ref already exists for "
                    f"dispatch_id={entry.dispatch_id!r}, "
                    f"purpose={entry.purpose!r}"
                )

        # ── 7. Append & re‑validate combined document ─────────────────
        refs.append(new_dict)
        data["updated_at"] = _utc_now()

        combined_errors = validate_mad_refs(data)
        if combined_errors:
            # Roll back the in‑memory append before raising.
            refs.pop()
            # Restore old updated_at.
            raise MadRefsValidationError(
                "combined document would be invalid after append:\n"
                + "\n".join(f"  - {e}" for e in combined_errors)
            )

        # ── 8. Atomically write ──────────────────────────────────────
        content = _serialize(data)
        _atomic_write(refs_path, content)

        return data

    finally:
        _release_lock(lock_path)
