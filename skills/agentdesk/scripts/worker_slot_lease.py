"""AgentDesk WorkerSlotLease store — agentdesk.worker-slot-lease/v1 (TC-13.10b).

Data model, pure validation, immutable store schema, read-only load,
exclusive file‑lock infrastructure, and atomic write helpers.

Non‑goals (deferred to TC-13.10c):
* ``acquire_worker_slot``, ``release_worker_slot``, ``renew_worker_slot``
* ``hold_worker_slot_fence``
* Stale‑lease cleanup
* Capacity allocation
* Lease ID generation
* Epoch increment operations
* Workspace normalisation
* State writes (tasks.yaml, events, outbox, reports)
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Any, Iterator

from core_types import WorkerKind

__all__ = [
    "SCHEMA_VERSION",
    "WorkerSlotLease",
    "WorkerSlotLeaseError",
    "WorkerSlotValidationError",
    "WorkerSlotCapacityError",
    "WorkerSlotContentionError",
    "WorkerSlotNotHeldError",
    "WorkerSlotFencingError",
    "validate_worker_slot_store",
    "read_worker_slot_leases",
]

# ── constants ───────────────────────────────────────────────────────────────

SCHEMA_VERSION = "agentdesk.worker-slot-lease/v1"

_STABLE_SLOT_IDS: tuple[str, ...] = (
    "basic_agent-1",
    "basic_agent-2",
    "standard_agent-1",
    "standard_agent-2",
    "advanced_agent-1",
    "advanced_agent-2",
    "expert_agent-1",
    "expert_agent-2",
)

_STABLE_SLOT_SET: frozenset[str] = frozenset(_STABLE_SLOT_IDS)

# Mapping: slot_id → WorkerKind it belongs to.
_SLOT_KIND: dict[str, WorkerKind] = {}
for _sid in _STABLE_SLOT_IDS:
    if _sid.startswith("basic_agent-"):
        _SLOT_KIND[_sid] = WorkerKind.BASIC_AGENT
    elif _sid.startswith("standard_agent-"):
        _SLOT_KIND[_sid] = WorkerKind.STANDARD_AGENT
    elif _sid.startswith("advanced_agent-"):
        _SLOT_KIND[_sid] = WorkerKind.ADVANCED_AGENT
    elif _sid.startswith("expert_agent-"):
        _SLOT_KIND[_sid] = WorkerKind.EXPERT_AGENT
    else:
        raise ValueError(f"slot_id {_sid!r} has no WorkerKind mapping")

_LEASE_TTL_SECONDS = 60
_MAX_HEARTBEAT_INTERVAL_SECONDS = 20

_RUNTIME_RELATIVE = Path(".agentdesk") / "runtime" / "worker-slot-lease.yaml"
_LOCK_NAME = ".worker-slot-lease.lock"

_LEASE_ID_RE = re.compile(r"^WSL-[0-9a-f]{32}$")

_ROOT_FIELD_NAMES: tuple[str, ...] = (
    "schema_version",
    "updated_at",
    "slot_epochs",
    "leases",
)
_ROOT_FIELD_SET: frozenset[str] = frozenset(_ROOT_FIELD_NAMES)

_LEASE_FIELD_NAMES: tuple[str, ...] = (
    "lease_id",
    "lease_epoch",
    "slot_id",
    "worker_kind",
    "holder_dispatch_id",
    "holder_instance_id",
    "canonical_worktree",
    "acquired_at",
    "heartbeat_at",
    "expires_at",
)
_LEASE_FIELD_SET: frozenset[str] = frozenset(_LEASE_FIELD_NAMES)

# RFC 3339 with mandatory timezone: Z or ±HH:MM.
_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)

# Canonical sentinel timestamp — must not be system‑time dependent.
_CANONICAL_SENTINEL_UPDATED_AT = "1970-01-01T00:00:00Z"


# ── data model ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class WorkerSlotLease:
    """Immutable 10‑field Worker slot lease value object.

    Constructed from validated fields — construction performs fail‑closed
    checks (TypeError / ValueError) but does NOT touch the filesystem.
    """

    lease_id: str
    lease_epoch: int
    slot_id: str
    worker_kind: WorkerKind
    holder_dispatch_id: str
    holder_instance_id: str
    canonical_worktree: str
    acquired_at: str
    heartbeat_at: str
    expires_at: str

    def __post_init__(self) -> None:
        # ── lease_id ───────────────────────────────────────────────────
        if not isinstance(self.lease_id, str) or not self.lease_id:
            raise TypeError(
                "lease_id must be a non-empty str, "
                f"got {type(self.lease_id).__name__}"
            )
        if _LEASE_ID_RE.fullmatch(self.lease_id) is None:
            raise ValueError(
                f"lease_id must match WSL-<32 hex>, got {self.lease_id!r}"
            )

        # ── lease_epoch ────────────────────────────────────────────────
        if isinstance(self.lease_epoch, bool) or not isinstance(
            self.lease_epoch, int
        ):
            raise TypeError(
                "lease_epoch must be a non-bool int >= 1, "
                f"got {self.lease_epoch!r}"
            )
        if self.lease_epoch < 1:
            raise ValueError(
                f"lease_epoch must be >= 1, got {self.lease_epoch}"
            )

        # ── slot_id ────────────────────────────────────────────────────
        if self.slot_id not in _STABLE_SLOT_SET:
            raise ValueError(
                f"slot_id must be one of the eight stable slots, "
                f"got {self.slot_id!r}"
            )

        # ── worker_kind ────────────────────────────────────────────────
        if not isinstance(self.worker_kind, WorkerKind):
            raise TypeError(
                "worker_kind must be a WorkerKind enum member, "
                f"got {type(self.worker_kind).__name__}"
            )

        # ── worker_kind ↔ slot_id consistency ──────────────────────────
        expected_kind = _SLOT_KIND.get(self.slot_id)
        if expected_kind is not None and self.worker_kind != expected_kind:
            raise ValueError(
                f"slot_id {self.slot_id!r} requires "
                f"worker_kind={expected_kind.value!r}, "
                f"got {self.worker_kind.value!r}"
            )

        # ── holder_dispatch_id ─────────────────────────────────────────
        _validate_holder_field(self.holder_dispatch_id, "holder_dispatch_id")

        # ── holder_instance_id ─────────────────────────────────────────
        _validate_holder_field(self.holder_instance_id, "holder_instance_id")

        # ── canonical_worktree ─────────────────────────────────────────
        _validate_worktree_string(self.canonical_worktree)

        # ── timestamps ─────────────────────────────────────────────────
        _validate_rfc3339_utc(self.acquired_at, "acquired_at")
        _validate_rfc3339_utc(self.heartbeat_at, "heartbeat_at")
        _validate_rfc3339_utc(self.expires_at, "expires_at")

        # ── temporal ordering ──────────────────────────────────────────
        at = _parse_rfc3339_utc(self.acquired_at)
        ht = _parse_rfc3339_utc(self.heartbeat_at)
        et = _parse_rfc3339_utc(self.expires_at)
        if at is not None and ht is not None and at > ht:
            raise ValueError(
                "acquired_at must be <= heartbeat_at"
            )
        if ht is not None and et is not None and ht >= et:
            raise ValueError(
                "heartbeat_at must be < expires_at"
            )


# ── field validation helpers ────────────────────────────────────────────────


def _validate_holder_field(value: str, field_name: str) -> None:
    """Validate a holder_* string field — non‑empty, no leading/trailing
    whitespace, no NUL / CR / LF."""
    if not isinstance(value, str) or not value:
        raise TypeError(
            f"{field_name} must be a non-empty str, "
            f"got {type(value).__name__}"
        )
    if value != value.strip():
        raise ValueError(
            f"{field_name} must not have leading or trailing whitespace"
        )
    if "\0" in value or "\r" in value or "\n" in value:
        raise ValueError(
            f"{field_name} must not contain NUL, CR, or LF"
        )


def _validate_worktree_string(value: str) -> None:
    """Validate canonical_worktree — non‑empty, no whitespace envelope,
    absolute path (Windows or POSIX), no NUL / CR / LF."""
    if not isinstance(value, str) or not value:
        raise TypeError(
            "canonical_worktree must be a non-empty str, "
            f"got {type(value).__name__}"
        )
    if value != value.strip():
        raise ValueError(
            "canonical_worktree must not have leading or trailing whitespace"
        )
    if "\0" in value or "\r" in value or "\n" in value:
        raise ValueError(
            "canonical_worktree must not contain NUL, CR, or LF"
        )
    # Must look like an absolute path (POSIX or Windows).
    if not _looks_absolute(value):
        raise ValueError(
            f"canonical_worktree must be an absolute path, got {value!r}"
        )


def _looks_absolute(path: str) -> bool:
    """Return True if *path* looks like an absolute path.

    Accepts POSIX (``/`` prefix) and Windows forms:
    drive‑letter (``C:\\…``) and UNC (``\\\\server\\share\\…``).
    Does NOT touch the filesystem — structural check only.
    """
    # POSIX
    if path.startswith("/"):
        return True
    # Windows drive letter — e.g.  C:\ or c:\
    if (
        len(path) >= 3
        and path[1:2] == ":"
        and path[0:1].isalpha()
        and path[2:3] in ("\\", "/")
    ):
        return True
    # Windows UNC — \\server\share\…
    if path.startswith("\\\\") and len(path) > 2:
        return True
    return False


def _validate_rfc3339_utc(value: str, field_name: str) -> None:
    """Validate *value* is an RFC 3339 UTC timestamp string."""
    if not isinstance(value, str) or not value:
        raise TypeError(
            f"{field_name} must be a non-empty str, "
            f"got {type(value).__name__}"
        )
    m = _RFC3339_RE.fullmatch(value)
    if m is None:
        raise ValueError(
            f"{field_name} must be RFC 3339 UTC, got {value!r}"
        )
    # Accept Z (UTC) or explicit +00:00
    tz_part = value[-6:] if value.endswith(("+00:00", "-00:00")) else ""
    if not (value.endswith("Z") or tz_part in ("+00:00",)):
        # Must be UTC — reject non‑zero offsets.
        raise ValueError(
            f"{field_name} must be UTC (Z or +00:00), got {value!r}"
        )


def _parse_rfc3339_utc(value: str) -> datetime | None:
    """Parse an RFC 3339 UTC string to a timezone‑aware datetime, or None."""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    # Check offset is UTC.
    if parsed.utcoffset() is None:
        return None
    return parsed


# ── serialization ───────────────────────────────────────────────────────────


def _lease_to_dict(lease: WorkerSlotLease) -> dict[str, object]:
    """Serialize a :class:`WorkerSlotLease` to a plain 10‑key dict."""
    return {
        "lease_id": lease.lease_id,
        "lease_epoch": lease.lease_epoch,
        "slot_id": lease.slot_id,
        "worker_kind": lease.worker_kind.value,
        "holder_dispatch_id": lease.holder_dispatch_id,
        "holder_instance_id": lease.holder_instance_id,
        "canonical_worktree": lease.canonical_worktree,
        "acquired_at": lease.acquired_at,
        "heartbeat_at": lease.heartbeat_at,
        "expires_at": lease.expires_at,
    }


def _lease_from_dict(data: dict[str, object]) -> WorkerSlotLease:
    """Construct a :class:`WorkerSlotLease` from a 10‑key raw dict.

    Raises :exc:`TypeError` / :exc:`ValueError` on any violation.
    """
    if not isinstance(data, dict):
        raise TypeError(
            f"lease data must be a dict, got {type(data).__name__}"
        )
    actual = set(data.keys())
    missing = _LEASE_FIELD_SET - actual
    extra = actual - _LEASE_FIELD_SET
    errors: list[str] = []
    if missing:
        errors.append(
            f"lease missing field(s): {', '.join(sorted(missing))}"
        )
    if extra:
        errors.append(
            f"lease forbidden field(s): {', '.join(sorted(extra))}"
        )
    if errors:
        raise ValueError("; ".join(errors))

    raw_kind = data["worker_kind"]
    if not isinstance(raw_kind, str):
        raise TypeError(
            f"lease.worker_kind must be a str, got {type(raw_kind).__name__}"
        )
    try:
        worker_kind = WorkerKind(raw_kind)
    except ValueError:
        raise ValueError(
            f"lease.worker_kind must be a valid WorkerKind value, "
            f"got {raw_kind!r}"
        ) from None

    # lease_epoch — must be non‑bool int.
    raw_epoch = data["lease_epoch"]
    if isinstance(raw_epoch, bool) or not isinstance(raw_epoch, int):
        raise TypeError(
            f"lease.lease_epoch must be a non-bool int, got {raw_epoch!r}"
        )

    # All string fields must be str (already checked by key validation
    # for existence; now type‑check each).
    str_fields = [
        "lease_id", "slot_id", "holder_dispatch_id",
        "holder_instance_id", "canonical_worktree",
        "acquired_at", "heartbeat_at", "expires_at",
    ]
    for key in str_fields:
        val = data[key]
        if not isinstance(val, str):
            raise TypeError(
                f"lease.{key} must be a str, got {type(val).__name__}"
            )

    return WorkerSlotLease(
        lease_id=str(data["lease_id"]),
        lease_epoch=int(raw_epoch),
        slot_id=str(data["slot_id"]),
        worker_kind=worker_kind,
        holder_dispatch_id=str(data["holder_dispatch_id"]),
        holder_instance_id=str(data["holder_instance_id"]),
        canonical_worktree=str(data["canonical_worktree"]),
        acquired_at=str(data["acquired_at"]),
        heartbeat_at=str(data["heartbeat_at"]),
        expires_at=str(data["expires_at"]),
    )


# ── canonical empty store ───────────────────────────────────────────────────


def _canonical_empty_store() -> dict[str, object]:
    """Return a fresh, independent canonical empty store document.

    Uses a sentinel ``updated_at`` that must NOT depend on system time.
    Every call returns a new ``dict`` — callers must not share mutation.
    """
    epochs: dict[str, int] = {}
    for sid in _STABLE_SLOT_IDS:
        epochs[sid] = 0
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": _CANONICAL_SENTINEL_UPDATED_AT,
        "slot_epochs": epochs,
        "leases": {},
    }


# ── pure validation ─────────────────────────────────────────────────────────


def validate_worker_slot_store(data: object) -> list[str]:
    """Validate *data* against ``agentdesk.worker-slot-lease/v1`` schema.

    Returns a list of human‑readable error messages.  An empty list means
    *data* is valid.  Pure function — no I/O, no mutation of input.

    Errors use field‑path prefixes (e.g. ``"root.slot_epochs.basic_agent-1"``)
    and must NOT contain ``holder_instance_id`` or full ``canonical_worktree``
    values.
    """
    errors: list[str] = []

    # ── top‑level must be a dict ───────────────────────────────────────
    if not isinstance(data, dict):
        return ["root must be an object"]

    # ── schema_version ─────────────────────────────────────────────────
    sv = data.get("schema_version")
    if sv != SCHEMA_VERSION:
        errors.append(
            f"root.schema_version must be {SCHEMA_VERSION!r}, got {sv!r}"
        )

    # ── exact root keys ────────────────────────────────────────────────
    actual_root = set(data.keys())
    missing_root = _ROOT_FIELD_SET - actual_root
    extra_root = actual_root - _ROOT_FIELD_SET
    if missing_root:
        errors.append(
            f"root missing key(s): {', '.join(sorted(missing_root))}"
        )
    if extra_root:
        errors.append(
            f"root forbidden key(s): {', '.join(sorted(extra_root))}"
        )

    # ── updated_at ─────────────────────────────────────────────────────
    ua = data.get("updated_at")
    if not isinstance(ua, str) or not ua.strip():
        errors.append("root.updated_at must be a non-empty RFC 3339 string")
    elif _RFC3339_RE.fullmatch(ua) is None:
        errors.append(
            f"root.updated_at must be RFC 3339, got {ua!r}"
        )

    # ── slot_epochs ────────────────────────────────────────────────────
    slot_epochs = data.get("slot_epochs")
    if not isinstance(slot_epochs, dict):
        errors.append("root.slot_epochs must be an object")
    else:
        se_actual = set(slot_epochs.keys())
        if se_actual != _STABLE_SLOT_SET:
            se_missing = sorted(_STABLE_SLOT_SET - se_actual)
            se_extra = sorted(se_actual - _STABLE_SLOT_SET)
            if se_missing:
                errors.append(
                    "root.slot_epochs missing slot(s): "
                    + ", ".join(se_missing)
                )
            if se_extra:
                errors.append(
                    "root.slot_epochs unknown slot(s): "
                    + ", ".join(se_extra)
                )
        # Validate each epoch value.
        for sid in _STABLE_SLOT_SET:
            val = slot_epochs.get(sid)
            if isinstance(val, bool) or not isinstance(val, int):
                errors.append(
                    f"root.slot_epochs.{sid} must be a non-bool int, "
                    f"got {val!r}"
                )
            elif val < 0:
                errors.append(
                    f"root.slot_epochs.{sid} must be >= 0, got {val}"
                )

    # ── leases ─────────────────────────────────────────────────────────
    leases = data.get("leases")
    if not isinstance(leases, dict):
        errors.append("root.leases must be an object")
        # Cannot validate further — return early.
        return errors

    # Collect active lease metadata for cross‑lease checks.
    seen_lease_ids: set[str] = set()
    seen_pairs: set[tuple[str, str]] = set()  # (worker_kind, worktree)

    for slot_key, raw_lease in leases.items():
        prefix = f"root.leases.{slot_key}"

        if not isinstance(slot_key, str) or not slot_key:
            errors.append(f"{prefix} slot key must be a non-empty string")
            continue
        if slot_key not in _STABLE_SLOT_SET:
            errors.append(
                f"{prefix} is not a valid stable slot ID"
            )
            continue

        if not isinstance(raw_lease, dict):
            errors.append(f"{prefix} must be an object")
            continue

        # ── exact lease keys ───────────────────────────────────────
        la = set(raw_lease.keys())
        lm = _LEASE_FIELD_SET - la
        le = la - _LEASE_FIELD_SET
        if lm:
            errors.append(
                f"{prefix} missing field(s): "
                + ", ".join(sorted(lm))
            )
        if le:
            errors.append(
                f"{prefix} forbidden field(s): "
                + ", ".join(sorted(le))
            )
        # Do not continue on key errors — remaining checks can still
        # inspect individual fields if they exist.

        # ── lease.slot_id must match the key ───────────────────────
        lid = raw_lease.get("slot_id")
        if isinstance(lid, str) and lid != slot_key:
            errors.append(
                f"{prefix}.slot_id={lid!r} does not match "
                f"the leases key {slot_key!r}"
            )

        # ── lease_id uniqueness ────────────────────────────────────
        lease_id = raw_lease.get("lease_id")
        if isinstance(lease_id, str) and lease_id:
            if lease_id in seen_lease_ids:
                errors.append(
                    f"{prefix}.lease_id={lease_id!r} is duplicated"
                )
            seen_lease_ids.add(lease_id)

        # ── per‑field type checks ──────────────────────────────────
        # lease_epoch
        ep = raw_lease.get("lease_epoch")
        if isinstance(ep, bool) or not isinstance(ep, int):
            errors.append(
                f"{prefix}.lease_epoch must be a non-bool int, "
                f"got {type(ep).__name__ if not isinstance(ep, bool) else 'bool'}"
                + (f" {ep!r}" if not isinstance(ep, bool) else f" {ep!r}")
            )

        # worker_kind
        wk = raw_lease.get("worker_kind")
        if isinstance(wk, str):
            try:
                WorkerKind(wk)
            except ValueError:
                errors.append(
                    f"{prefix}.worker_kind must be a valid WorkerKind, "
                    f"got {wk!r}"
                )
        elif wk is not None:
            errors.append(
                f"{prefix}.worker_kind must be a str, "
                f"got {type(wk).__name__}"
            )

        # worker_kind ↔ slot consistency (lightweight — no dataclass
        # construction).
        if isinstance(wk, str) and isinstance(slot_key, str):
            expected = _SLOT_KIND.get(slot_key)
            if expected is not None:
                try:
                    parsed_wk = WorkerKind(wk)
                except ValueError:
                    pass
                else:
                    if parsed_wk != expected:
                        errors.append(
                            f"{prefix}.worker_kind={wk!r} does not match "
                            f"slot {slot_key!r} (expected {expected.value!r})"
                        )

        # epoch consistency with slot_epochs (if both valid)
        if isinstance(ep, int) and not isinstance(ep, bool) and isinstance(
            slot_epochs, dict
        ):
            se_val = slot_epochs.get(slot_key)
            if isinstance(se_val, int) and not isinstance(se_val, bool):
                if ep != se_val:
                    errors.append(
                        f"{prefix}.lease_epoch={ep} does not match "
                        f"root.slot_epochs.{slot_key}={se_val}"
                    )

        # String fields.
        for fn in (
            "lease_id", "holder_dispatch_id", "holder_instance_id",
            "canonical_worktree", "acquired_at", "heartbeat_at",
            "expires_at",
        ):
            fv = raw_lease.get(fn)
            if fv is not None and not isinstance(fv, str):
                errors.append(
                    f"{prefix}.{fn} must be a str, "
                    f"got {type(fv).__name__}"
                )

        # Timestamp format checks (only when strings).
        for fn in ("acquired_at", "heartbeat_at", "expires_at"):
            fv = raw_lease.get(fn)
            if isinstance(fv, str):
                if _RFC3339_RE.fullmatch(fv) is None:
                    errors.append(
                        f"{prefix}.{fn} must be RFC 3339, got {fv!r}"
                    )

        # ── per‑(worker_kind, worktree) uniqueness ─────────────────
        cw = raw_lease.get("canonical_worktree")
        if isinstance(wk, str) and isinstance(cw, str):
            pair = (wk, cw)
            if pair in seen_pairs:
                errors.append(
                    f"{prefix} duplicate (worker_kind={wk!r}, "
                    f"canonical_worktree) pair"
                )
            seen_pairs.add(pair)

    return errors


# ── read‑only API ───────────────────────────────────────────────────────────


def read_worker_slot_leases(project_root: Path) -> dict[str, object]:
    """Read & validate ``.agentdesk/runtime/worker-slot-lease.yaml``.

    *project_root* must be an absolute, existing directory.

    Returns the parsed canonical data dict.  When the file does not exist,
    returns a fresh canonical empty store (no file or directory is created).

    Does **not** acquire the write lock.  Does **not** clean stale leases.
    Raises :exc:`WorkerSlotValidationError` for schema violations,
    :exc:`OSError` for filesystem errors, :exc:`json.JSONDecodeError` for
    parse failures.
    """
    if not isinstance(project_root, Path):
        raise TypeError(
            f"project_root must be a Path, got {type(project_root).__name__}"
        )
    if not project_root.is_absolute():
        raise ValueError(
            f"project_root must be absolute, got {project_root}"
        )
    if not project_root.is_dir():
        raise ValueError(
            f"project_root does not exist or is not a directory: "
            f"{project_root}"
        )

    path = project_root / _RUNTIME_RELATIVE
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _canonical_empty_store()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WorkerSlotValidationError(
            f"worker-slot-lease.yaml is not valid JSON: {exc}"
        ) from exc

    errors = validate_worker_slot_store(data)
    if errors:
        raise WorkerSlotValidationError(
            "worker-slot-lease.yaml validation failed:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )
    return data


# ── error hierarchy ─────────────────────────────────────────────────────────


class WorkerSlotLeaseError(Exception):
    """Base for all WorkerSlotLease errors."""


class WorkerSlotValidationError(WorkerSlotLeaseError):
    """Schema or field validation failure."""


class WorkerSlotCapacityError(WorkerSlotLeaseError):
    """No free slot available for the requested WorkerKind."""


class WorkerSlotContentionError(WorkerSlotLeaseError):
    """Another writer holds the exclusive lock."""


class WorkerSlotNotHeldError(WorkerSlotLeaseError):
    """Slot not found (release / renew of unknown slot)."""


class WorkerSlotFencingError(WorkerSlotLeaseError):
    """Epoch mismatch or expired lease."""


# ── private lock infrastructure ─────────────────────────────────────────────


def _lock_path(runtime_dir: Path) -> Path:
    """Return the filesystem path of the advisory lock file."""
    return runtime_dir / _LOCK_NAME


@contextmanager
def _exclusive_store_lock(project_root: Path) -> Iterator[None]:
    """Context manager: acquire the exclusive store lock, yield, release.

    Writes a random ownership token into the lock file.  On release,
    re‑reads the token and only deletes the file if the token matches.
    A mismatched token means another process has taken over — the lock
    file is **never** deleted in that case.

    Raises :exc:`WorkerSlotContentionError` when the lock already exists.
    No waiting, sleeping, or retrying.
    """
    runtime_dir = project_root / ".agentdesk" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    lock = _lock_path(runtime_dir)
    token = secrets.token_hex(16)  # 32 hex chars

    # ── acquire ────────────────────────────────────────────────────────
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        raise WorkerSlotContentionError(
            "another writer holds the worker-slot-lease lock"
        ) from None
    except OSError as exc:
        raise WorkerSlotLeaseError(
            f"cannot create worker-slot-lease lock: {exc}"
        ) from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        # Clean up the lock file on write failure.
        try:
            lock.unlink()
        except OSError:
            pass
        raise

    # ── yield to caller ────────────────────────────────────────────────
    try:
        yield
    finally:
        # ── release (with token check) ─────────────────────────────
        try:
            stored = lock.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            # Lock was already removed by someone else — nothing to do.
            return
        except OSError:
            # Cannot read — do NOT delete; we can't verify ownership.
            return
        if stored == token:
            try:
                lock.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
        # If token mismatches, do NOT delete — another process owns it.


# ── private atomic write ────────────────────────────────────────────────────


def _serialize_store(data: object) -> str:
    """Serialize store *data* to JSON‑compatible YAML text."""
    if not isinstance(data, dict):
        raise TypeError(
            f"store data must be a dict, got {type(data).__name__}"
        )
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def _atomic_write_store(project_root: Path, data: object) -> None:
    """Atomically replace the runtime store file with *data*.

    **Caller must already hold** :func:`_exclusive_store_lock`.  This
    function performs full validation, serialization, unique‑temp‑file
    write, fsync, and ``os.replace``.

    Raises :exc:`WorkerSlotValidationError` if *data* is invalid.
    On any failure before ``os.replace``, the original file bytes are
    untouched and the temp file is cleaned up.
    """
    # ── validate before writing ────────────────────────────────────────
    errors = validate_worker_slot_store(data)
    if errors:
        raise WorkerSlotValidationError(
            "refusing to write invalid store:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )

    content = _serialize_store(data)

    path = project_root / _RUNTIME_RELATIVE
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
        # Best‑effort directory fsync.
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
        except OSError:
            dir_fd = None
        if dir_fd is not None:
            try:
                os.fsync(dir_fd)
            except OSError:
                # On Windows, directory fsync may not be supported —
                # silently accept that specific failure.
                pass
            finally:
                os.close(dir_fd)
    except BaseException:
        # Clean up temp file — the original file was never touched
        # because ``os.replace`` never ran.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
