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

import errno
import json
import os
import re
import secrets
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
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
        raise ValueError(
            "slot_id has no WorkerKind mapping: "
            + type(_sid).__name__
        )

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

# ── Windows directory-fsync errno allowlist ─────────────────────────────

# On Windows, os.fsync on a directory handle may raise OSError with these
# errno values because the underlying Windows API does not support flushing
# directory metadata.  We only suppress these specific errno codes.
_WIN_DIR_FSYNC_ALLOWLIST: frozenset[int] = frozenset({
    errno.EACCES,       # 13 — Permission denied (handle may be read‑only)
    errno.EBADF,        # 9  — Bad file descriptor (dir fd not flushable)
    errno.EINVAL,       # 22 — Invalid argument (dir fsync unsupported)
})


def _is_dir_fsync_allowed_error(exc: OSError) -> bool:
    """Return True if *exc* is a directory‑fsync error we can safely ignore."""
    return (
        os.name == "nt"
        and exc.errno is not None
        and exc.errno in _WIN_DIR_FSYNC_ALLOWLIST
    )


# ── safe error messaging helpers ────────────────────────────────────────────


def _safe_type_name(value: object) -> str:
    """Return ``type(value).__name__`` — never calls ``repr`` or ``str``
    on the value itself."""
    return type(value).__name__


def _safe_error_val(value: object) -> str:
    """Return a safe representation of *value* for error messages.

    For stable slot IDs (already validated as a known constant), the value
    may be included directly.  For unknown values, only the type name is
    returned — never the raw value via repr/str.
    """
    return _safe_type_name(value)


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
                f"got {_safe_type_name(self.lease_id)}"
            )
        if _LEASE_ID_RE.fullmatch(self.lease_id) is None:
            raise ValueError(
                "lease_id must match WSL-<32 hex>, "
                f"got {_safe_type_name(self.lease_id)}"
            )

        # ── lease_epoch ────────────────────────────────────────────────
        if isinstance(self.lease_epoch, bool) or not isinstance(
            self.lease_epoch, int
        ):
            raise TypeError(
                "lease_epoch must be a non-bool int >= 1, "
                f"got {_safe_type_name(self.lease_epoch)}"
            )
        if self.lease_epoch < 1:
            raise ValueError(
                "lease_epoch must be >= 1, "
                f"got {self.lease_epoch}"
            )

        # ── slot_id ────────────────────────────────────────────────────
        if not isinstance(self.slot_id, str) or not self.slot_id:
            raise TypeError(
                "slot_id must be a non-empty str, "
                f"got {_safe_type_name(self.slot_id)}"
            )
        if self.slot_id not in _STABLE_SLOT_SET:
            raise ValueError(
                "slot_id must be one of the eight stable slots, "
                f"got {_safe_type_name(self.slot_id)}"
            )

        # ── worker_kind ────────────────────────────────────────────────
        if not isinstance(self.worker_kind, WorkerKind):
            raise TypeError(
                "worker_kind must be a WorkerKind enum member, "
                f"got {_safe_type_name(self.worker_kind)}"
            )

        # ── worker_kind ↔ slot_id consistency ──────────────────────────
        expected_kind = _SLOT_KIND.get(self.slot_id)
        if expected_kind is not None and self.worker_kind != expected_kind:
            raise ValueError(
                f"slot_id requires worker_kind="
                f"{expected_kind.value!r}, "
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
            f"got {_safe_type_name(value)}"
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
            f"got {_safe_type_name(value)}"
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
            "canonical_worktree must be an absolute path, "
            f"got {_safe_type_name(value)}"
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
            f"got {_safe_type_name(value)}"
        )
    # Parse with real datetime parser — reject non‑UTC offsets,
    # naive timestamps, and invalid calendar dates.
    parsed = _parse_rfc3339_utc(value)
    if parsed is None:
        raise ValueError(
            f"{field_name} must be RFC 3339 UTC (Z or +00:00), "
            f"got {_safe_type_name(value)}"
        )
    # Verify offset is UTC.
    offset = parsed.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise ValueError(
            f"{field_name} must be UTC (Z or +00:00), "
            f"got {_safe_type_name(value)}"
        )


def _parse_rfc3339_utc(value: str) -> datetime | None:
    """Parse an RFC 3339 UTC string to a timezone‑aware datetime, or None.

    Accepts only Z or +00:00.  Rejects -00:00, non‑zero offsets,
    naive timestamps, and invalid calendar dates.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    # Reject -00:00 — it is not a valid UTC offset.
    if candidate.endswith("-00:00"):
        return None
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    offset = parsed.utcoffset()
    if offset is None:
        return None
    # Reject non‑zero offsets.
    if offset.total_seconds() != 0:
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
            "lease data must be a dict, "
            f"got {_safe_type_name(data)}"
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
            "lease.worker_kind must be a str, "
            f"got {_safe_type_name(raw_kind)}"
        )
    try:
        worker_kind = WorkerKind(raw_kind)
    except ValueError:
        raise ValueError(
            "lease.worker_kind must be a valid WorkerKind value, "
            f"got {_safe_type_name(raw_kind)}"
        ) from None

    # lease_epoch — must be non‑bool int.
    raw_epoch = data["lease_epoch"]
    if isinstance(raw_epoch, bool) or not isinstance(raw_epoch, int):
        raise TypeError(
            "lease.lease_epoch must be a non-bool int, "
            f"got {_safe_type_name(raw_epoch)}"
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
                f"lease.{key} must be a str, "
                f"got {_safe_type_name(val)}"
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
    values.  Error messages must NOT call ``repr()``, ``str()``, or ``{!r}``
    on untrusted input values — only ``type(x).__name__`` is safe.
    """
    errors: list[str] = []

    # ── top‑level must be a dict ───────────────────────────────────────
    if not isinstance(data, dict):
        return ["root must be an object"]

    # ── Guard: verify all root keys are str before any set operations.
    #         This prevents malicious keys from breaking sorted().
    # ── schema_version ─────────────────────────────────────────────────
    sv = data.get("schema_version")
    if sv != SCHEMA_VERSION:
        errors.append(
            "root.schema_version must be "
            + SCHEMA_VERSION
            + ", got "
            + _safe_type_name(sv)
        )

    # ── exact root keys ────────────────────────────────────────────────
    # Collect only str keys for safe comparison.
    str_keys: set[str] = set()
    non_str_keys = 0
    for k in data:
        if isinstance(k, str):
            str_keys.add(k)
        else:
            non_str_keys += 1
    missing_root = _ROOT_FIELD_SET - str_keys
    extra_root = str_keys - _ROOT_FIELD_SET
    if missing_root:
        errors.append(
            f"root missing key(s): {', '.join(sorted(missing_root))}"
        )
    if extra_root:
        errors.append(
            f"root forbidden key(s): {', '.join(sorted(extra_root))}"
        )
    if non_str_keys > 0:
        errors.append(
            f"root contains {non_str_keys} non-str key(s)"
        )

    # ── updated_at ─────────────────────────────────────────────────────
    ua = data.get("updated_at")
    if not isinstance(ua, str) or not ua.strip():
        errors.append("root.updated_at must be a non-empty RFC 3339 string")
    else:
        parsed_ua = _parse_rfc3339_utc(ua)
        if parsed_ua is None:
            errors.append(
                "root.updated_at must be RFC 3339 UTC (Z or +00:00), "
                f"got {_safe_type_name(ua)}"
            )
        else:
            offset = parsed_ua.utcoffset()
            if offset is None or offset.total_seconds() != 0:
                errors.append(
                    "root.updated_at must be UTC (Z or +00:00), "
                    f"got {_safe_type_name(ua)}"
                )

    # ── slot_epochs ────────────────────────────────────────────────────
    slot_epochs = data.get("slot_epochs")
    if not isinstance(slot_epochs, dict):
        errors.append("root.slot_epochs must be an object")
    else:
        # Collect only str keys for safe set operations.
        se_str_keys: set[str] = set()
        for k in slot_epochs:
            if isinstance(k, str):
                se_str_keys.add(k)
        if se_str_keys != _STABLE_SLOT_SET:
            se_missing = sorted(_STABLE_SLOT_SET - se_str_keys)
            se_extra = sorted(se_str_keys - _STABLE_SLOT_SET)
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
                    f"got {_safe_type_name(val)}"
                )
            elif val < 0:
                errors.append(
                    f"root.slot_epochs.{sid} must be >= 0, got {val}"
                )

    # ── leases ─────────────────────────────────────────────────────────
    leases = data.get("leases")
    if not isinstance(leases, dict):
        errors.append("root.leases must be an object")
        return errors

    # Collect active lease metadata for cross‑lease checks.
    seen_lease_ids: set[str] = set()
    seen_pairs: set[tuple[str, str]] = set()  # (worker_kind, worktree)

    for slot_key, raw_lease in leases.items():
        prefix = f"root.leases"

        if not isinstance(slot_key, str) or not slot_key:
            errors.append(
                f"{prefix}[non-str-key] slot key must be a non-empty string, "
                f"got {_safe_type_name(slot_key)}"
            )
            continue
        prefix = f"root.leases.{slot_key}"
        if slot_key not in _STABLE_SLOT_SET:
            errors.append(
                f"{prefix} is not a valid stable slot ID"
            )
            continue

        if not isinstance(raw_lease, dict):
            errors.append(f"{prefix} must be an object")
            continue

        # ── exact lease keys ───────────────────────────────────────
        # Only compare str keys.
        la_str: set[str] = set()
        for lk in raw_lease:
            if isinstance(lk, str):
                la_str.add(lk)
        lm = _LEASE_FIELD_SET - la_str
        le = la_str - _LEASE_FIELD_SET
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

        # ── lease.slot_id must match the key ───────────────────────
        lid_val = raw_lease.get("slot_id")
        if isinstance(lid_val, str) and lid_val != slot_key:
            errors.append(
                f"{prefix}.slot_id does not match "
                f"the leases key {slot_key!r}"
            )
        elif not isinstance(lid_val, str):
            errors.append(
                f"{prefix}.slot_id must be a str, "
                f"got {_safe_type_name(lid_val)}"
            )

        # ── lease_id uniqueness ────────────────────────────────────
        lease_id = raw_lease.get("lease_id")
        if isinstance(lease_id, str) and lease_id:
            if lease_id in seen_lease_ids:
                errors.append(
                    f"{prefix}.lease_id is duplicated"
                )
            seen_lease_ids.add(lease_id)

        # ── per‑field type checks ──────────────────────────────────
        # lease_epoch
        ep = raw_lease.get("lease_epoch")
        if isinstance(ep, bool) or not isinstance(ep, int):
            errors.append(
                f"{prefix}.lease_epoch must be a non-bool int, "
                f"got {_safe_type_name(ep)}"
            )

        # worker_kind
        wk = raw_lease.get("worker_kind")
        if isinstance(wk, str):
            try:
                WorkerKind(wk)
            except ValueError:
                errors.append(
                    f"{prefix}.worker_kind must be a valid WorkerKind, "
                    f"got {_safe_type_name(wk)}"
                )
        elif wk is not None:
            errors.append(
                f"{prefix}.worker_kind must be a str, "
                f"got {_safe_type_name(wk)}"
            )

        # worker_kind ↔ slot consistency.
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
                            f"{prefix}.worker_kind does not match "
                            f"slot {slot_key!r} "
                            f"(expected {expected.value!r})"
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
                    f"got {_safe_type_name(fv)}"
                )

        # Timestamp UTC validation (real parser, not just regex).
        for fn in ("acquired_at", "heartbeat_at", "expires_at"):
            fv = raw_lease.get(fn)
            if isinstance(fv, str):
                parsed_ts = _parse_rfc3339_utc(fv)
                if parsed_ts is None:
                    errors.append(
                        f"{prefix}.{fn} must be RFC 3339 UTC "
                        f"(Z or +00:00), "
                        f"got {_safe_type_name(fv)}"
                    )
                else:
                    offset = parsed_ts.utcoffset()
                    if offset is None or offset.total_seconds() != 0:
                        errors.append(
                            f"{prefix}.{fn} must be UTC "
                            f"(Z or +00:00), "
                            f"got {_safe_type_name(fv)}"
                        )

        # ── temporal ordering (when all three parse as UTC) ─────────
        at_ts = (
            _parse_rfc3339_utc(raw_lease["acquired_at"])
            if isinstance(raw_lease.get("acquired_at"), str)
            else None
        )
        ht_ts = (
            _parse_rfc3339_utc(raw_lease["heartbeat_at"])
            if isinstance(raw_lease.get("heartbeat_at"), str)
            else None
        )
        et_ts = (
            _parse_rfc3339_utc(raw_lease["expires_at"])
            if isinstance(raw_lease.get("expires_at"), str)
            else None
        )
        if at_ts is not None and ht_ts is not None and at_ts > ht_ts:
            errors.append(
                f"{prefix}.acquired_at must be <= heartbeat_at"
            )
        if ht_ts is not None and et_ts is not None and ht_ts >= et_ts:
            errors.append(
                f"{prefix}.heartbeat_at must be < expires_at"
            )

        # ── per‑(worker_kind, worktree) uniqueness ─────────────────
        cw = raw_lease.get("canonical_worktree")
        if isinstance(wk, str) and isinstance(cw, str):
            pair = (wk, cw)
            if pair in seen_pairs:
                errors.append(
                    f"{prefix} duplicate (worker_kind, "
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
            "project_root must be a Path, "
            f"got {_safe_type_name(project_root)}"
        )
    if not project_root.is_absolute():
        raise ValueError(
            "project_root must be absolute, "
            f"got {_safe_type_name(project_root)}"
        )
    if not project_root.is_dir():
        raise ValueError(
            "project_root does not exist or is not a directory: "
            f"{_safe_type_name(project_root)}"
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

    **Exception propagation rule:** If the caller's ``with`` block raises
    an exception, release errors (missing / unreadable / mismatched lock)
    are logged but the original caller exception is **always** propagated
    unchanged.  If the caller's ``with`` block succeeds but the release
    step detects a problem (lock missing, token mismatch, or unlink
    failure), a :exc:`WorkerSlotLeaseError` is raised.
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
    body_exception: BaseException | None = None
    try:
        yield
    except BaseException as _exc:
        body_exception = _exc
    finally:
        # ── release (with token check) ─────────────────────────────
        release_error: str | None = None
        try:
            stored = lock.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            release_error = (
                "worker-slot-lease lock file disappeared while held"
            )
        except OSError:
            release_error = (
                "worker-slot-lease lock file is unreadable while held"
            )
        else:
            if stored == token:
                try:
                    lock.unlink()
                except FileNotFoundError:
                    # Already gone — no action needed.
                    pass
                except OSError:
                    release_error = (
                        "worker-slot-lease lock file could not be removed"
                    )
            else:
                # Token mismatch — lock was taken over.
                release_error = (
                    "worker-slot-lease lock file token mismatch — "
                    "another process has taken over"
                )

        # If the body raised, propagate that — release errors are
        # secondary (the original exception is always preserved).
        # If the body succeeded but release detected a problem,
        # raise a new exception.
        if body_exception is not None:
            raise body_exception
        if release_error is not None:
            raise WorkerSlotLeaseError(release_error)


# ── directory fsync helper ─────────────────────────────────────────────────


def _fsync_parent_directory(path: Path) -> None:
    """Perform a best‑effort fsync on *path*'s parent directory.

    On POSIX: any OSError is propagated — directory fsync must succeed.
    On Windows: specific errno values (EACCES, EBADF, EINVAL) that
    indicate the OS does not support directory fsync are silently
    accepted at both the ``os.open`` and ``os.fsync`` stages; all other
    errors are propagated.

    The directory file descriptor is always closed, even on error.
    """
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
    except OSError as exc:
        if not _is_dir_fsync_allowed_error(exc):
            raise
        # Windows can't even open the dir — safe to ignore.
        return
    try:
        os.fsync(dir_fd)
    except OSError as exc:
        if not _is_dir_fsync_allowed_error(exc):
            raise
    finally:
        os.close(dir_fd)


# ── private atomic write ────────────────────────────────────────────────────


def _serialize_store(data: object) -> str:
    """Serialize store *data* to JSON‑compatible YAML text."""
    if not isinstance(data, dict):
        raise TypeError(
            "store data must be a dict, "
            f"got {_safe_type_name(data)}"
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
        # Directory fsync (with platform‑specific allowlist).
        _fsync_parent_directory(path)
    except BaseException:
        # Clean up temp file — the original file was never touched
        # because ``os.replace`` never ran.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
