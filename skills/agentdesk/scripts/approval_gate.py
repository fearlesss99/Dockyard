"""AgentDesk ApprovalGate — typed models, evidence store, writer (TC-13.12b).

Production module for ``agentdesk.task-approval/v1`` evidence.  Provides:

* 5 immutable typed models (ApprovalScope, ApprovalSubject,
  ApprovalCheckRequest, ApprovalEvidence, ApprovalCheckResult)
* 7 exception types rooted at ApprovalError
* Private evidence-store loader for ``docs/pm/approvals/``
* ``write_grant()`` / ``write_revoke()`` with state-lock, Git CAS,
  duplicate detection, and atomic write

Non-goals (explicitly excluded from this module — deferred to later TCs):

* ApprovalGate.check / require matching logic (→ TC-13.12c)
* ControlPlaneTransitionService integration (→ TC-13.12c)
* Offline validator integration (→ TC-13.12d)
* Subprocess invocation for model/CLI calls
* Network access
* Secret / auth management
"""

from __future__ import annotations

import enum
import errno
import io
import json
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

__all__ = [
    "ApprovalScope",
    "ApprovalSubject",
    "ApprovalCheckRequest",
    "ApprovalEvidence",
    "ApprovalCheckResult",
    "ApprovalGate",
    "ApprovalError",
    "ApprovalValidationError",
    "ApprovalNotFoundError",
    "ApprovalAmbiguousError",
    "ApprovalExpiredError",
    "ApprovalRevokedError",
    "ApprovalSnapshotConflictError",
    "write_grant",
    "write_revoke",
]


# ═══════════════════════════════════════════════════════════════════════════════
# 1.  Constants
# ═══════════════════════════════════════════════════════════════════════════════

_SCHEMA_VERSION = "agentdesk.task-approval/v1"
_RECORD_TYPE_GRANT = "grant"
_RECORD_TYPE_REVOKE = "revoke"
_FIXED_ACTOR_ROLE_ID = "PM"

_APPROVALS_DIR_RELATIVE = Path("docs") / "pm" / "approvals"

_GRANT_ROOT_KEYS: frozenset[str] = frozenset({
    "schema_version",
    "record_type",
    "approval_id",
    "event_id",
    "scope",
    "task_id",
    "revision",
    "attempt",
    "dispatch_id",
    "accepted_commit",
    "actor_role_id",
    "lease_epoch",
    "granted_at",
    "expires_at",
    "reason",
    "snapshot_commit",
})

_REVOKE_ROOT_KEYS: frozenset[str] = frozenset({
    "schema_version",
    "record_type",
    "approval_id",
    "event_id",
    "task_id",
    "actor_role_id",
    "lease_epoch",
    "revoked_at",
    "reason",
    "snapshot_commit",
})

_TASK_ID_RE = re.compile(r"^TC-[0-9]{3,}$")
_EVENT_ID_RE = re.compile(r"^EVT-.+$")
_APPROVAL_ID_RE = re.compile(r"^APR-.+$")
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)

# Windows directory-fsync errno allowlist (same semantics as worker_slot_lease.py).
_WIN_DIR_FSYNC_ALLOWLIST: frozenset[int] = frozenset({
    errno.EACCES,
    errno.EBADF,
    errno.EINVAL,
})

# Runtime state lock — same stable path as TC-13.11.
_RUNTIME_RELATIVE = Path(".agentdesk") / "runtime"
_STATE_LOCK_NAME = ".state-transition.lock"


# ═══════════════════════════════════════════════════════════════════════════════
# 2.  Safe error-messaging helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _safe_type_name(value: object) -> str:
    """Return ``type(value).__name__`` — never calls ``repr`` or ``str``."""
    return type(value).__name__


# ═══════════════════════════════════════════════════════════════════════════════
# 3.  Timestamp helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _parse_rfc3339_utc(value: str) -> datetime | None:
    """Parse an RFC 3339 UTC string to a timezone-aware datetime, or None.

    Accepts only Z or +00:00.  Rejects -00:00, non-zero offsets,
    naive timestamps, and invalid calendar dates.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip()
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
    if offset.total_seconds() != 0:
        return None
    return parsed


def _dt_to_utc_str(dt: datetime) -> str:
    """Convert a UTC :class:`datetime` to an RFC 3339 Z string."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_utc_now(now: datetime) -> None:
    """Validate *now* is a timezone-aware UTC :class:`datetime`.

    Rejects booleans, strings, naive datetimes, and non-UTC offsets.
    """
    if isinstance(now, bool) or not isinstance(now, datetime):
        raise TypeError(
            "now must be a datetime, "
            f"got {_safe_type_name(now)}"
        )
    if now.tzinfo is None:
        raise TypeError(
            "now must be timezone-aware UTC, got naive datetime"
        )
    offset = now.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise TypeError(
            "now must be UTC (Z or +00:00), "
            f"got {_safe_type_name(now)}"
        )


def _validate_rfc3339_utc_strictly_after(
    earlier_str: str,
    later_str: str,
    *,
    earlier_label: str = "earlier",
    later_label: str = "later",
) -> None:
    """Raise ValueError if *later_str* is not strictly after *earlier_str*.

    Both must parse as RFC 3339 UTC.  ``later > earlier`` is required.
    """
    earlier_dt = _parse_rfc3339_utc(earlier_str)
    later_dt = _parse_rfc3339_utc(later_str)
    if earlier_dt is None:
        raise ValueError(
            f"{earlier_label} must be RFC 3339 UTC, "
            f"got {_safe_type_name(earlier_str)}"
        )
    if later_dt is None:
        raise ValueError(
            f"{later_label} must be RFC 3339 UTC, "
            f"got {_safe_type_name(later_str)}"
        )
    if later_dt <= earlier_dt:
        raise ValueError(
            f"{later_label} must be strictly after {earlier_label}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 4.  Typed models
# ═══════════════════════════════════════════════════════════════════════════════


@enum.unique
class ApprovalScope(str, enum.Enum):
    """Closed scope enum for task-action approval.

    Exactly three members — no more, no less.  ``str(member) == member.value``.
    Bare strings are rejected at the public API boundary.
    """

    DISPATCH = "dispatch"
    ACCEPT = "accept"
    INTEGRATE = "integrate"


@dataclass(frozen=True, slots=True)
class ApprovalSubject:
    """Immutable five-field identity for a control-plane action to be authorised.

    Exactly five fields — no more, no less.  Scope-independent structural
    validation is performed at construction; scope-specific rules are enforced
    by the evidence store and writer.
    """

    task_id: str
    revision: int
    attempt: int
    dispatch_id: str
    accepted_commit: str | None

    def __post_init__(self) -> None:
        # ── task_id ───────────────────────────────────────────────────────
        if not isinstance(self.task_id, str) or not self.task_id:
            raise TypeError(
                "task_id must be a non-empty str, "
                f"got {_safe_type_name(self.task_id)}"
            )
        if _TASK_ID_RE.fullmatch(self.task_id) is None:
            raise ValueError(
                "task_id must match ^TC-[0-9]{3,}$, "
                f"got {_safe_type_name(self.task_id)}"
            )

        # ── revision ──────────────────────────────────────────────────────
        if isinstance(self.revision, bool) or not isinstance(self.revision, int):
            raise TypeError(
                "revision must be a non-bool int >= 1, "
                f"got {_safe_type_name(self.revision)}"
            )
        if self.revision < 1:
            raise ValueError(
                f"revision must be >= 1, got {self.revision}"
            )

        # ── attempt ───────────────────────────────────────────────────────
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int):
            raise TypeError(
                "attempt must be a non-bool int >= 1, "
                f"got {_safe_type_name(self.attempt)}"
            )
        if self.attempt < 1:
            raise ValueError(
                f"attempt must be >= 1, got {self.attempt}"
            )

        # ── dispatch_id ───────────────────────────────────────────────────
        if not isinstance(self.dispatch_id, str) or not self.dispatch_id:
            raise TypeError(
                "dispatch_id must be a non-empty str, "
                f"got {_safe_type_name(self.dispatch_id)}"
            )
        if self.dispatch_id != self.dispatch_id.strip():
            raise ValueError(
                "dispatch_id must not have leading or trailing whitespace"
            )
        if "\0" in self.dispatch_id or "\r" in self.dispatch_id or "\n" in self.dispatch_id:
            raise ValueError(
                "dispatch_id must not contain NUL, CR, or LF"
            )

        # ── accepted_commit ───────────────────────────────────────────────
        if self.accepted_commit is not None:
            if not isinstance(self.accepted_commit, str):
                raise TypeError(
                    "accepted_commit must be str or None, "
                    f"got {_safe_type_name(self.accepted_commit)}"
                )
            if _SHA40_RE.fullmatch(self.accepted_commit) is None:
                raise ValueError(
                    "accepted_commit must be 40 lowercase hex chars, "
                    f"got {_safe_type_name(self.accepted_commit)}"
                )


@dataclass(frozen=True, slots=True)
class ApprovalCheckRequest:
    """Immutable three-field input to ``ApprovalGate.check()`` / ``.require()``.

    Exactly three fields — no more, no less.
    """

    scope: ApprovalScope
    subject: ApprovalSubject
    expected_snapshot_commit: str

    def __post_init__(self) -> None:
        if not isinstance(self.scope, ApprovalScope):
            raise TypeError(
                "scope must be an ApprovalScope member, "
                f"got {_safe_type_name(self.scope)}"
            )

        if not isinstance(self.subject, ApprovalSubject):
            raise TypeError(
                "subject must be an ApprovalSubject, "
                f"got {_safe_type_name(self.subject)}"
            )

        if not isinstance(self.expected_snapshot_commit, str):
            raise TypeError(
                "expected_snapshot_commit must be a str, "
                f"got {_safe_type_name(self.expected_snapshot_commit)}"
            )
        if _SHA40_RE.fullmatch(self.expected_snapshot_commit) is None:
            raise ValueError(
                "expected_snapshot_commit must be 40 lowercase hex chars, "
                f"got {_safe_type_name(self.expected_snapshot_commit)}"
            )


@dataclass(frozen=True, slots=True)
class ApprovalEvidence:
    """Immutable ten-field typed model for a validated grant evidence record.

    Represents a single ``TASK_APPROVAL_GRANTED`` record.  Does **not** wrap
    revoke records.  Carries only business fields — storage-envelope fields
    (``schema_version``, ``record_type``) are stripped.

    Exactly ten fields — no more, no less.
    """

    approval_id: str
    event_id: str
    scope: ApprovalScope
    subject: ApprovalSubject
    actor_role_id: str
    lease_epoch: int
    granted_at: str
    expires_at: str | None
    reason: str
    snapshot_commit: str

    def __post_init__(self) -> None:
        # ── approval_id ───────────────────────────────────────────────────
        if not isinstance(self.approval_id, str) or not self.approval_id:
            raise TypeError(
                "approval_id must be a non-empty str, "
                f"got {_safe_type_name(self.approval_id)}"
            )
        if _APPROVAL_ID_RE.fullmatch(self.approval_id) is None:
            raise ValueError(
                "approval_id must match APR-*, "
                f"got {_safe_type_name(self.approval_id)}"
            )

        # ── event_id ──────────────────────────────────────────────────────
        if not isinstance(self.event_id, str) or not self.event_id:
            raise TypeError(
                "event_id must be a non-empty str, "
                f"got {_safe_type_name(self.event_id)}"
            )
        if _EVENT_ID_RE.fullmatch(self.event_id) is None:
            raise ValueError(
                "event_id must match EVT-*, "
                f"got {_safe_type_name(self.event_id)}"
            )

        # ── scope ─────────────────────────────────────────────────────────
        if not isinstance(self.scope, ApprovalScope):
            raise TypeError(
                "scope must be an ApprovalScope member, "
                f"got {_safe_type_name(self.scope)}"
            )

        # ── subject ───────────────────────────────────────────────────────
        if not isinstance(self.subject, ApprovalSubject):
            raise TypeError(
                "subject must be an ApprovalSubject, "
                f"got {_safe_type_name(self.subject)}"
            )

        # ── actor_role_id ─────────────────────────────────────────────────
        if not isinstance(self.actor_role_id, str):
            raise TypeError(
                "actor_role_id must be a str, "
                f"got {_safe_type_name(self.actor_role_id)}"
            )
        if self.actor_role_id != _FIXED_ACTOR_ROLE_ID:
            raise ValueError(
                f"actor_role_id must be {_FIXED_ACTOR_ROLE_ID!r}, "
                f"got {_safe_type_name(self.actor_role_id)}"
            )

        # ── lease_epoch ───────────────────────────────────────────────────
        if isinstance(self.lease_epoch, bool) or not isinstance(self.lease_epoch, int):
            raise TypeError(
                "lease_epoch must be a non-bool int >= 1, "
                f"got {_safe_type_name(self.lease_epoch)}"
            )
        if self.lease_epoch < 1:
            raise ValueError(
                f"lease_epoch must be >= 1, got {self.lease_epoch}"
            )

        # ── granted_at ────────────────────────────────────────────────────
        if not isinstance(self.granted_at, str) or not self.granted_at:
            raise TypeError(
                "granted_at must be a non-empty str, "
                f"got {_safe_type_name(self.granted_at)}"
            )
        if _parse_rfc3339_utc(self.granted_at) is None:
            raise ValueError(
                "granted_at must be RFC 3339 UTC, "
                f"got {_safe_type_name(self.granted_at)}"
            )

        # ── expires_at ────────────────────────────────────────────────────
        if self.expires_at is not None:
            if not isinstance(self.expires_at, str):
                raise TypeError(
                    "expires_at must be str or None, "
                    f"got {_safe_type_name(self.expires_at)}"
                )
            if _parse_rfc3339_utc(self.expires_at) is None:
                raise ValueError(
                    "expires_at must be RFC 3339 UTC or None, "
                    f"got {_safe_type_name(self.expires_at)}"
                )
            _validate_rfc3339_utc_strictly_after(
                self.granted_at,
                self.expires_at,
                earlier_label="granted_at",
                later_label="expires_at",
            )

        # ── reason ────────────────────────────────────────────────────────
        if not isinstance(self.reason, str) or not self.reason:
            raise TypeError(
                "reason must be a non-empty str, "
                f"got {_safe_type_name(self.reason)}"
            )
        if self.reason != self.reason.strip():
            raise ValueError(
                "reason must not have leading or trailing whitespace"
            )
        if "\0" in self.reason or "\r" in self.reason or "\n" in self.reason:
            raise ValueError(
                "reason must not contain NUL, CR, or LF"
            )

        # ── snapshot_commit ───────────────────────────────────────────────
        if not isinstance(self.snapshot_commit, str):
            raise TypeError(
                "snapshot_commit must be a str, "
                f"got {_safe_type_name(self.snapshot_commit)}"
            )
        if _SHA40_RE.fullmatch(self.snapshot_commit) is None:
            raise ValueError(
                "snapshot_commit must be 40 lowercase hex chars, "
                f"got {_safe_type_name(self.snapshot_commit)}"
            )


_LEGAL_FAILURE_CODES: frozenset[str] = frozenset({
    "not_found",
    "expired",
    "revoked",
    "wrong_scope",
    "wrong_subject",
})


@dataclass(frozen=True, slots=True)
class ApprovalCheckResult:
    """Immutable four-field result from ``ApprovalGate.check()``.

    Exactly four fields — no more, no less.  Internally consistent:

    * ``passed=True`` → ``failure_code is None`` and ``matched_evidence`` non-empty.
    * ``passed=False`` → ``failure_code`` is a legal code and ``matched_evidence is None``.
    """

    passed: bool
    failure_code: str | None
    matched_evidence: ApprovalEvidence | None
    checked_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.passed, bool):
            raise TypeError(
                "passed must be bool, "
                f"got {_safe_type_name(self.passed)}"
            )

        if self.passed:
            if self.failure_code is not None:
                raise ValueError(
                    "failure_code must be None when passed=True"
                )
            if not isinstance(self.matched_evidence, ApprovalEvidence):
                raise ValueError(
                    "matched_evidence must be ApprovalEvidence when passed=True"
                )
        else:
            if not isinstance(self.failure_code, str) or not self.failure_code:
                raise TypeError(
                    "failure_code must be a non-empty str when passed=False, "
                    f"got {_safe_type_name(self.failure_code)}"
                )
            if self.failure_code not in _LEGAL_FAILURE_CODES:
                raise ValueError(
                    f"illegal failure_code {self.failure_code!r}; "
                    f"must be one of {sorted(_LEGAL_FAILURE_CODES)}"
                )
            if self.matched_evidence is not None:
                raise ValueError(
                    "matched_evidence must be None when passed=False"
                )

        if not isinstance(self.checked_at, str) or not self.checked_at:
            raise TypeError(
                "checked_at must be a non-empty str, "
                f"got {_safe_type_name(self.checked_at)}"
            )
        if _parse_rfc3339_utc(self.checked_at) is None:
            raise ValueError(
                "checked_at must be RFC 3339 UTC, "
                f"got {_safe_type_name(self.checked_at)}"
            )


# ═══════════════════════════════════════════════════════════════════════════════
# 5.  Exception hierarchy
# ═══════════════════════════════════════════════════════════════════════════════


class ApprovalError(Exception):
    """Base for all ApprovalGate errors."""


class ApprovalValidationError(ApprovalError):
    """Schema violation, unknown version, extra/missing keys, malformed fields,
    illegal actor_role_id, bad lease_epoch."""


class ApprovalNotFoundError(ApprovalError):
    """No matching grant evidence exists."""


class ApprovalAmbiguousError(ApprovalError):
    """Multiple active grants for the same scope + subject."""


class ApprovalExpiredError(ApprovalError):
    """Grant exists but now >= expires_at."""


class ApprovalRevokedError(ApprovalError):
    """Grant exists but a revoke record also exists with now >= revoked_at."""


class ApprovalSnapshotConflictError(ApprovalError):
    """expected_snapshot_commit != HEAD, or evidence snapshot_commit not an
    ancestor of expected_snapshot_commit."""


# ═══════════════════════════════════════════════════════════════════════════════
# 6.  ApprovalGate service (stub — TC-13.12b boundary)
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class ApprovalGate:
    """Read-only approval gate for control-plane actions.

    Takes a ``project_root`` and does NOT store mutable state.
    Every ``check`` / ``require`` call is self-contained.

    **TC-13.12b boundary**: construction validates project_root only.
    ``check()`` and ``require()`` are stubs — implemented by TC-13.12c.
    Interface #17 remains Target.
    """

    project_root: Path

    def __post_init__(self) -> None:
        if not isinstance(self.project_root, Path):
            raise TypeError(
                "project_root must be a Path, "
                f"got {_safe_type_name(self.project_root)}"
            )
        if not self.project_root.is_absolute():
            raise ValueError(
                "project_root must be absolute, "
                f"got {_safe_type_name(self.project_root)}"
            )
        if not self.project_root.is_dir():
            raise ValueError(
                "project_root does not exist or is not a directory"
            )

    def check(
        self,
        request: ApprovalCheckRequest,
        now: datetime,
    ) -> ApprovalCheckResult:
        """Validate approval for *request*.  (Stub — TC-13.12c)."""
        raise NotImplementedError(
            "ApprovalGate.check is implemented by TC-13.12c"
        )

    def require(
        self,
        request: ApprovalCheckRequest,
        now: datetime,
    ) -> ApprovalEvidence:
        """Require a valid approval for *request*.  (Stub — TC-13.12c)."""
        raise NotImplementedError(
            "ApprovalGate.require is implemented by TC-13.12c"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 7.  Project root validation helper
# ═══════════════════════════════════════════════════════════════════════════════


def _validate_project_root(project_root: Path) -> None:
    """Validate *project_root*: absolute Path, existing directory."""
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
            "project_root does not exist or is not a directory"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 8.  Private Git helper
# ═══════════════════════════════════════════════════════════════════════════════


def _git_rev_parse_head(project_root: Path) -> str:
    """Return the 40-char hex SHA of HEAD via ``git rev-parse HEAD``.

    Uses argv array, ``shell=False``.  No remote, no fetch, no network.
    Raises ``ApprovalSnapshotConflictError`` on any failure.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(project_root),
            capture_output=True,
            timeout=10,
            shell=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise ApprovalSnapshotConflictError(
            "git rev-parse HEAD failed"
        ) from exc

    if result.returncode != 0:
        raise ApprovalSnapshotConflictError(
            "git rev-parse HEAD returned non-zero exit"
        )

    # Decode — fail-closed on non-UTF-8.
    try:
        sha = result.stdout.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise ApprovalSnapshotConflictError(
            "git rev-parse HEAD output is not UTF-8"
        )

    if _SHA40_RE.fullmatch(sha) is None:
        raise ApprovalSnapshotConflictError(
            "git rev-parse HEAD output is not a valid 40-char hex SHA"
        )

    return sha


# ═══════════════════════════════════════════════════════════════════════════════
# 9.  Private state lock (same lock file as TC-13.11)
# ═══════════════════════════════════════════════════════════════════════════════


def _is_dir_fsync_allowed_error(exc: OSError) -> bool:
    """Return True if *exc* is a directory-fsync error we can safely ignore."""
    return (
        os.name == "nt"
        and exc.errno is not None
        and exc.errno in _WIN_DIR_FSYNC_ALLOWLIST
    )


def _fsync_parent_directory(path: Path) -> None:
    """Best-effort fsync on *path*'s parent directory.

    On POSIX: any OSError is propagated.
    On Windows: specific errno values are silently accepted.
    """
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
    except OSError as exc:
        if not _is_dir_fsync_allowed_error(exc):
            raise
        return
    try:
        os.fsync(dir_fd)
    except OSError as exc:
        if not _is_dir_fsync_allowed_error(exc):
            raise
    finally:
        os.close(dir_fd)


def _acquire_os_lock(fd: int) -> None:
    """Acquire a non-blocking OS advisory lock on *fd*.

    * Windows: ``msvcrt.locking(LK_NBLCK)``.
    * POSIX: ``fcntl.flock(LOCK_EX | LOCK_NB)``.
    * Contention → ``ApprovalSnapshotConflictError``.
    """
    if os.name == "nt":
        try:
            import msvcrt
        except ImportError:
            raise ApprovalSnapshotConflictError(
                "cannot acquire OS advisory lock: msvcrt not available"
            ) from None
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise ApprovalSnapshotConflictError(
                "control-plane state lock is currently held"
            ) from exc
        return

    # POSIX
    try:
        import fcntl
    except ImportError:
        raise ApprovalSnapshotConflictError(
            "cannot acquire OS advisory lock: fcntl not available"
        ) from None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        err = exc.errno
        if err in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
            raise ApprovalSnapshotConflictError(
                "control-plane state lock is currently held"
            ) from exc
        raise ApprovalSnapshotConflictError(
            "cannot acquire control-plane state lock"
        ) from exc
    except AttributeError:
        raise ApprovalSnapshotConflictError(
            "OS advisory lock not available on this platform"
        ) from None


def _release_os_lock(fd: int) -> None:
    """Release an OS advisory lock held on *fd*."""
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return

    import fcntl
    fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def _exclusive_state_lock(project_root: Path) -> Iterator[None]:
    """Acquire the project-level control-plane state lock.

    Uses the stable lock file at
    ``.agentdesk/runtime/.state-transition.lock`` with a non-blocking
    OS advisory lock — the same file as TC-13.11.

    The lock file is **never deleted**.  Only the OS advisory lock decides
    ownership.  Contention raises ``ApprovalSnapshotConflictError`` immediately.
    """
    runtime_dir = project_root / _RUNTIME_RELATIVE
    runtime_dir.mkdir(parents=True, exist_ok=True)
    lock_path = runtime_dir / _STATE_LOCK_NAME

    # Open stable lock file (create if missing, never truncate).
    fd: int | None = None
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    except OSError as exc:
        raise ApprovalSnapshotConflictError(
            "cannot acquire control-plane state lock"
        ) from exc

    # Acquire non-blocking OS lock.
    try:
        _acquire_os_lock(fd)
    except ApprovalSnapshotConflictError:
        os.close(fd)
        raise

    # Yield to caller.
    body_exception: BaseException | None = None
    try:
        yield
    except BaseException as _exc:
        body_exception = _exc
    finally:
        # Release OS lock.
        release_error: BaseException | None = None
        try:
            _release_os_lock(fd)
        except BaseException as _exc:
            release_error = _exc

        # Close fd.
        close_error: BaseException | None = None
        try:
            os.close(fd)
        except BaseException as _exc:
            close_error = _exc

        if body_exception is not None:
            raise body_exception
        if release_error is not None:
            raise ApprovalSnapshotConflictError(
                "failed to release control-plane state lock"
            ) from release_error
        if close_error is not None:
            raise ApprovalSnapshotConflictError(
                "failed to close control-plane state lock file"
            ) from close_error


# ═══════════════════════════════════════════════════════════════════════════════
# 10. Private evidence store loader
# ═══════════════════════════════════════════════════════════════════════════════


def _validate_string_field(
    value: object,
    field_name: str,
    *,
    allow_empty: bool = False,
) -> str:
    """Validate *value* is a non-empty str (by default) with no NUL/CR/LF.

    Returns the validated str.  Raises TypeError / ValueError on violation.
    """
    if not isinstance(value, str):
        raise ApprovalValidationError(
            f"{field_name} must be a str, "
            f"got {_safe_type_name(value)}"
        )
    if not allow_empty and not value:
        raise ApprovalValidationError(
            f"{field_name} must be non-empty"
        )
    if "\0" in value or "\r" in value or "\n" in value:
        raise ApprovalValidationError(
            f"{field_name} must not contain NUL, CR, or LF"
        )
    return value


def _validate_non_bool_int(value: object, field_name: str) -> int:
    """Validate *value* is a non-bool int.  Returns the validated int."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApprovalValidationError(
            f"{field_name} must be a non-bool int, "
            f"got {_safe_type_name(value)}"
        )
    return value


def _validate_grant_record(data: dict[str, object]) -> dict[str, object]:
    """Validate a grant record against the frozen 16-key schema.

    Returns the validated dict unchanged on success.
    Raises ``ApprovalValidationError`` on any violation.
    """
    if not isinstance(data, dict):
        raise ApprovalValidationError(
            "grant record must be an object, "
            f"got {_safe_type_name(data)}"
        )

    # Collect only str keys for safe comparison.
    str_keys: set[str] = set()
    non_str_keys = 0
    for k in data:
        if isinstance(k, str):
            str_keys.add(k)
        else:
            non_str_keys += 1

    missing = _GRANT_ROOT_KEYS - str_keys
    extra = str_keys - _GRANT_ROOT_KEYS
    if missing:
        raise ApprovalValidationError(
            f"grant record missing key(s): {', '.join(sorted(missing))}"
        )
    if extra:
        raise ApprovalValidationError(
            f"grant record forbidden key(s): {', '.join(sorted(extra))}"
        )
    if non_str_keys > 0:
        raise ApprovalValidationError(
            f"grant record contains {non_str_keys} non-str key(s)"
        )

    # schema_version
    sv = data["schema_version"]
    if sv != _SCHEMA_VERSION:
        raise ApprovalValidationError(
            f"schema_version must be {_SCHEMA_VERSION!r}, "
            f"got {_safe_type_name(sv)}"
        )

    # record_type
    rt = data["record_type"]
    if rt != _RECORD_TYPE_GRANT:
        raise ApprovalValidationError(
            f"record_type must be {_RECORD_TYPE_GRANT!r}, "
            f"got {_safe_type_name(rt)}"
        )

    # approval_id
    raw_approval_id = data["approval_id"]
    _validate_string_field(raw_approval_id, "approval_id")
    if _APPROVAL_ID_RE.fullmatch(str(raw_approval_id)) is None:
        raise ApprovalValidationError(
            "approval_id must match APR-*, "
            f"got {_safe_type_name(raw_approval_id)}"
        )

    # event_id
    raw_event_id = data["event_id"]
    _validate_string_field(raw_event_id, "event_id")
    if _EVENT_ID_RE.fullmatch(str(raw_event_id)) is None:
        raise ApprovalValidationError(
            "event_id must match EVT-*, "
            f"got {_safe_type_name(raw_event_id)}"
        )

    # scope
    raw_scope = data["scope"]
    _validate_string_field(raw_scope, "scope")
    try:
        scope = ApprovalScope(str(raw_scope))
    except ValueError:
        raise ApprovalValidationError(
            "scope must be a valid ApprovalScope value, "
            f"got {_safe_type_name(raw_scope)}"
        ) from None

    # task_id
    raw_task_id = data["task_id"]
    _validate_string_field(raw_task_id, "task_id")
    if _TASK_ID_RE.fullmatch(str(raw_task_id)) is None:
        raise ApprovalValidationError(
            "task_id must match ^TC-[0-9]{3,}$, "
            f"got {_safe_type_name(raw_task_id)}"
        )

    # revision
    raw_revision = data["revision"]
    rev = _validate_non_bool_int(raw_revision, "revision")
    if rev < 1:
        raise ApprovalValidationError(
            f"revision must be >= 1, got {rev}"
        )

    # attempt
    raw_attempt = data["attempt"]
    att = _validate_non_bool_int(raw_attempt, "attempt")
    if att < 1:
        raise ApprovalValidationError(
            f"attempt must be >= 1, got {att}"
        )

    # dispatch_id
    raw_dispatch_id = data["dispatch_id"]
    _validate_string_field(raw_dispatch_id, "dispatch_id")
    did = str(raw_dispatch_id)
    if did != did.strip():
        raise ApprovalValidationError(
            "dispatch_id must not have leading or trailing whitespace"
        )

    # accepted_commit
    raw_ac = data["accepted_commit"]
    if raw_ac is not None:
        if not isinstance(raw_ac, str):
            raise ApprovalValidationError(
                "accepted_commit must be str or null, "
                f"got {_safe_type_name(raw_ac)}"
            )
        if _SHA40_RE.fullmatch(raw_ac) is None:
            raise ApprovalValidationError(
                "accepted_commit must be 40 lowercase hex chars or null, "
                f"got {_safe_type_name(raw_ac)}"
            )

    # actor_role_id
    raw_actor = data["actor_role_id"]
    _validate_string_field(raw_actor, "actor_role_id")
    if str(raw_actor) != _FIXED_ACTOR_ROLE_ID:
        raise ApprovalValidationError(
            f"actor_role_id must be {_FIXED_ACTOR_ROLE_ID!r}, "
            f"got {_safe_type_name(raw_actor)}"
        )

    # lease_epoch
    raw_le = data["lease_epoch"]
    le = _validate_non_bool_int(raw_le, "lease_epoch")
    if le < 1:
        raise ApprovalValidationError(
            f"lease_epoch must be >= 1, got {le}"
        )

    # granted_at
    raw_ga = data["granted_at"]
    _validate_string_field(raw_ga, "granted_at")
    ga_dt = _parse_rfc3339_utc(str(raw_ga))
    if ga_dt is None:
        raise ApprovalValidationError(
            "granted_at must be RFC 3339 UTC, "
            f"got {_safe_type_name(raw_ga)}"
        )

    # expires_at
    raw_ea = data["expires_at"]
    if raw_ea is not None:
        if not isinstance(raw_ea, str):
            raise ApprovalValidationError(
                "expires_at must be str or null, "
                f"got {_safe_type_name(raw_ea)}"
            )
        ea_dt = _parse_rfc3339_utc(raw_ea)
        if ea_dt is None:
            raise ApprovalValidationError(
                "expires_at must be RFC 3339 UTC or null, "
                f"got {_safe_type_name(raw_ea)}"
            )
        if ea_dt <= ga_dt:
            raise ApprovalValidationError(
                "expires_at must be strictly after granted_at"
            )

    # reason
    raw_reason = data["reason"]
    _validate_string_field(raw_reason, "reason")
    reason_str = str(raw_reason)
    if reason_str != reason_str.strip():
        raise ApprovalValidationError(
            "reason must not have leading or trailing whitespace"
        )

    # snapshot_commit
    raw_sc = data["snapshot_commit"]
    _validate_string_field(raw_sc, "snapshot_commit")
    if _SHA40_RE.fullmatch(str(raw_sc)) is None:
        raise ApprovalValidationError(
            "snapshot_commit must be 40 lowercase hex chars, "
            f"got {_safe_type_name(raw_sc)}"
        )

    return data


def _validate_revoke_record(data: dict[str, object]) -> dict[str, object]:
    """Validate a revoke record against the frozen 10-key schema.

    Returns the validated dict unchanged on success.
    Raises ``ApprovalValidationError`` on any violation.
    """
    if not isinstance(data, dict):
        raise ApprovalValidationError(
            "revoke record must be an object, "
            f"got {_safe_type_name(data)}"
        )

    str_keys: set[str] = set()
    non_str_keys = 0
    for k in data:
        if isinstance(k, str):
            str_keys.add(k)
        else:
            non_str_keys += 1

    missing = _REVOKE_ROOT_KEYS - str_keys
    extra = str_keys - _REVOKE_ROOT_KEYS
    if missing:
        raise ApprovalValidationError(
            f"revoke record missing key(s): {', '.join(sorted(missing))}"
        )
    if extra:
        raise ApprovalValidationError(
            f"revoke record forbidden key(s): {', '.join(sorted(extra))}"
        )
    if non_str_keys > 0:
        raise ApprovalValidationError(
            f"revoke record contains {non_str_keys} non-str key(s)"
        )

    # schema_version
    sv = data["schema_version"]
    if sv != _SCHEMA_VERSION:
        raise ApprovalValidationError(
            f"schema_version must be {_SCHEMA_VERSION!r}, "
            f"got {_safe_type_name(sv)}"
        )

    # record_type
    rt = data["record_type"]
    if rt != _RECORD_TYPE_REVOKE:
        raise ApprovalValidationError(
            f"record_type must be {_RECORD_TYPE_REVOKE!r}, "
            f"got {_safe_type_name(rt)}"
        )

    # approval_id
    raw_approval_id = data["approval_id"]
    _validate_string_field(raw_approval_id, "approval_id")
    if _APPROVAL_ID_RE.fullmatch(str(raw_approval_id)) is None:
        raise ApprovalValidationError(
            "approval_id must match APR-*, "
            f"got {_safe_type_name(raw_approval_id)}"
        )

    # event_id
    raw_event_id = data["event_id"]
    _validate_string_field(raw_event_id, "event_id")
    if _EVENT_ID_RE.fullmatch(str(raw_event_id)) is None:
        raise ApprovalValidationError(
            "event_id must match EVT-*, "
            f"got {_safe_type_name(raw_event_id)}"
        )

    # task_id
    raw_task_id = data["task_id"]
    _validate_string_field(raw_task_id, "task_id")
    if _TASK_ID_RE.fullmatch(str(raw_task_id)) is None:
        raise ApprovalValidationError(
            "task_id must match ^TC-[0-9]{3,}$, "
            f"got {_safe_type_name(raw_task_id)}"
        )

    # actor_role_id
    raw_actor = data["actor_role_id"]
    _validate_string_field(raw_actor, "actor_role_id")
    if str(raw_actor) != _FIXED_ACTOR_ROLE_ID:
        raise ApprovalValidationError(
            f"actor_role_id must be {_FIXED_ACTOR_ROLE_ID!r}, "
            f"got {_safe_type_name(raw_actor)}"
        )

    # lease_epoch
    raw_le = data["lease_epoch"]
    le = _validate_non_bool_int(raw_le, "lease_epoch")
    if le < 1:
        raise ApprovalValidationError(
            f"lease_epoch must be >= 1, got {le}"
        )

    # revoked_at
    raw_ra = data["revoked_at"]
    _validate_string_field(raw_ra, "revoked_at")
    ra_dt = _parse_rfc3339_utc(str(raw_ra))
    if ra_dt is None:
        raise ApprovalValidationError(
            "revoked_at must be RFC 3339 UTC, "
            f"got {_safe_type_name(raw_ra)}"
        )

    # reason
    raw_reason = data["reason"]
    _validate_string_field(raw_reason, "reason")
    reason_str = str(raw_reason)
    if reason_str != reason_str.strip():
        raise ApprovalValidationError(
            "reason must not have leading or trailing whitespace"
        )

    # snapshot_commit
    raw_sc = data["snapshot_commit"]
    _validate_string_field(raw_sc, "snapshot_commit")
    if _SHA40_RE.fullmatch(str(raw_sc)) is None:
        raise ApprovalValidationError(
            "snapshot_commit must be 40 lowercase hex chars, "
            f"got {_safe_type_name(raw_sc)}"
        )

    return data


def _grant_record_to_evidence(data: dict[str, object]) -> ApprovalEvidence:
    """Construct an :class:`ApprovalEvidence` from a validated grant record dict.

    The caller must have already validated the record via
    :func:`_validate_grant_record`.
    """
    scope = ApprovalScope(str(data["scope"]))
    subject = ApprovalSubject(
        task_id=str(data["task_id"]),
        revision=int(data["revision"]),  # type: ignore[arg-type]
        attempt=int(data["attempt"]),  # type: ignore[arg-type]
        dispatch_id=str(data["dispatch_id"]),
        accepted_commit=str(data["accepted_commit"]) if data["accepted_commit"] is not None else None,
    )
    return ApprovalEvidence(
        approval_id=str(data["approval_id"]),
        event_id=str(data["event_id"]),
        scope=scope,
        subject=subject,
        actor_role_id=str(data["actor_role_id"]),
        lease_epoch=int(data["lease_epoch"]),  # type: ignore[arg-type]
        granted_at=str(data["granted_at"]),
        expires_at=str(data["expires_at"]) if data["expires_at"] is not None else None,
        reason=str(data["reason"]),
        snapshot_commit=str(data["snapshot_commit"]),
    )


def _is_safe_filename(name: str) -> bool:
    """Return True if *name* matches ``EVT-*.yaml`` and contains no path
    separators or dangerous characters."""
    if not name.startswith("EVT-") or not name.endswith(".yaml"):
        return False
    stem = name[:-5]  # strip ".yaml"
    if _EVENT_ID_RE.fullmatch(stem) is None:
        return False
    # Reject any path separators, NUL.
    if "/" in name or "\\" in name or "\0" in name:
        return False
    return True


def _load_evidence_store(
    project_root: Path,
) -> tuple[dict[str, ApprovalEvidence], dict[str, dict[str, object]]]:
    """Load and validate all evidence from ``docs/pm/approvals/``.

    Returns ``(grants_by_approval_id, revokes_by_approval_id)`` where
    grants are typed ``ApprovalEvidence`` and revokes are private internal
    dicts.

    Rules:
    * Directory does not exist → returns empty collections; does not create dir.
    * Only reads files matching ``EVT-*.yaml``.
    * Rejects symlinks / reparse points.
    * UTF-8 strict decoding.
    * YAML root must be a mapping.
    * Dispatches on ``record_type`` to Grant/Revoke validation.
    * Unknown schema/version/record type → fail-closed.
    * All Grants converted to ``ApprovalEvidence``.
    """
    grants: dict[str, ApprovalEvidence] = {}
    revokes: dict[str, dict[str, object]] = {}

    approvals_dir = project_root / _APPROVALS_DIR_RELATIVE

    if not approvals_dir.is_dir():
        return grants, revokes

    # List files in approvals directory.
    try:
        entries = list(approvals_dir.iterdir())
    except OSError as exc:
        raise ApprovalValidationError(
            f"cannot read approvals directory: {exc}"
        ) from exc

    for entry in entries:
        if not entry.is_file():
            continue
        name = entry.name

        # Only EVT-*.yaml.
        if not _is_safe_filename(name):
            continue

        # Reject symlinks / reparse points.
        if entry.is_symlink():
            raise ApprovalValidationError(
                f"approval evidence must not be a symlink"
            )

        # On Windows, also detect junctions via resolve comparison.
        if os.name == "nt":
            try:
                resolved = entry.resolve(strict=True)
                orig_norm = os.path.normcase(str(entry))
                res_norm = os.path.normcase(str(resolved))
                if orig_norm != res_norm:
                    raise ApprovalValidationError(
                        "approval evidence must not be a reparse point"
                    )
            except OSError:
                raise ApprovalValidationError(
                    "approval evidence could not be resolved"
                ) from None

        # Read with strict UTF-8.
        try:
            raw_text = entry.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise ApprovalValidationError(
                "approval evidence file is not valid UTF-8"
            ) from None
        except OSError as exc:
            raise ApprovalValidationError(
                f"cannot read approval evidence file: {exc}"
            ) from exc

        # Parse YAML root as mapping.
        try:
            import yaml
            data = yaml.safe_load(io.StringIO(raw_text))
        except ImportError:
            # Fallback: treat as JSON (JSON is valid YAML subset).
            try:
                data = json.loads(raw_text)
            except json.JSONDecodeError as exc:
                raise ApprovalValidationError(
                    "approval evidence file is not valid YAML/JSON"
                ) from exc
        except yaml.YAMLError as exc:
            raise ApprovalValidationError(
                f"approval evidence file is not valid YAML: {exc}"
            ) from exc

        if not isinstance(data, dict):
            raise ApprovalValidationError(
                "approval evidence root must be a mapping, "
                f"got {_safe_type_name(data)}"
            )

        # Dispatch on record_type.
        raw_record_type = data.get("record_type")
        if not isinstance(raw_record_type, str):
            raise ApprovalValidationError(
                "approval evidence record_type must be a str, "
                f"got {_safe_type_name(raw_record_type)}"
            )

        if raw_record_type == _RECORD_TYPE_GRANT:
            validated = _validate_grant_record(data)
            evidence = _grant_record_to_evidence(validated)
            # Check for duplicate approval_id.
            if evidence.approval_id in grants:
                raise ApprovalValidationError(
                    "duplicate approval_id in evidence store: "
                    + evidence.approval_id
                )
            # Check for duplicate event_id.
            # We need to check across both grants and revokes.
            grants[evidence.approval_id] = evidence
        elif raw_record_type == _RECORD_TYPE_REVOKE:
            validated = _validate_revoke_record(data)
            aid = str(validated["approval_id"])
            if aid in revokes:
                raise ApprovalValidationError(
                    "duplicate revoke for approval_id: " + aid
                )
            revokes[aid] = validated
        else:
            raise ApprovalValidationError(
                "unknown record_type in evidence store, "
                f"got {_safe_type_name(raw_record_type)}"
            )

    # Cross-validate: check for duplicate event_ids across grants and revokes.
    grant_event_ids: set[str] = set()
    for ev in grants.values():
        if ev.event_id in grant_event_ids:
            raise ApprovalValidationError(
                "duplicate event_id in evidence store: " + ev.event_id
            )
        grant_event_ids.add(ev.event_id)

    for revoke_dict in revokes.values():
        eid = str(revoke_dict["event_id"])
        if eid in grant_event_ids:
            raise ApprovalValidationError(
                "duplicate event_id in evidence store: " + eid
            )
        grant_event_ids.add(eid)

    return grants, revokes


# ═══════════════════════════════════════════════════════════════════════════════
# 11. Serialization helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _serialize_evidence(data: dict[str, object]) -> str:
    """Serialize evidence dict to deterministic YAML/JSON text.

    Uses ``json.dumps`` with ``ensure_ascii=False``, indent=2, trailing LF.
    Python-specific tags are never emitted.
    """
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def _atomic_write(
    project_root: Path,
    relative_path: Path,
    content: str,
) -> None:
    """Atomically write *content* to *relative_path* inside *project_root*.

    Uses ``tempfile.mkstemp`` → write/fsync → ``os.replace`` → directory fsync.
    The original file bytes are never modified on failure.
    Temporary files are cleaned up on any error before ``os.replace``.
    """
    target = project_root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", dir=str(target.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        _fsync_parent_directory(target)
    except BaseException:
        # Clean up temp file — the original file was never touched.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


# ═══════════════════════════════════════════════════════════════════════════════
# 12. Writer API
# ═══════════════════════════════════════════════════════════════════════════════


def write_grant(
    project_root: Path,
    approval_id: str,
    event_id: str,
    scope: ApprovalScope,
    subject: ApprovalSubject,
    lease_epoch: int,
    now: datetime,
    reason: str,
    expires_at: str | None,
    expected_snapshot_commit: str,
) -> tuple[Path, str]:
    """Atomically write a TASK_APPROVAL grant evidence file.

    Fixed execution order:
    1. Validate all call parameters.
    2. Normalise and validate project_root.
    3. Acquire ``.agentdesk/runtime/.state-transition.lock``.
    4. ``git rev-parse HEAD`` inside lock.
    5. Exact compare against ``expected_snapshot_commit``.
    6. Load and strictly validate all existing evidence.
    7. Perform ID, reference, and duplicate detection.
    8. Build full mapping.
    9. Re-validate mapping in memory.
    10. Determine target path ``docs/pm/approvals/<event-id>.yaml``.
    11. Atomic write.
    12. Release lock.
    13. Return ``(absolute_path, expected_snapshot_commit)``.

    Raises ``ApprovalValidationError`` on schema/duplicate violations.
    Raises ``ApprovalSnapshotConflictError`` on CAS failure.
    Returns ``(file_path, snapshot_commit_written)``.
    """
    # ── 1. Validate all call parameters ───────────────────────────────────
    if not isinstance(approval_id, str) or not approval_id:
        raise TypeError(
            "approval_id must be a non-empty str, "
            f"got {_safe_type_name(approval_id)}"
        )
    if _APPROVAL_ID_RE.fullmatch(approval_id) is None:
        raise ValueError(
            "approval_id must match APR-*, "
            f"got {_safe_type_name(approval_id)}"
        )

    if not isinstance(event_id, str) or not event_id:
        raise TypeError(
            "event_id must be a non-empty str, "
            f"got {_safe_type_name(event_id)}"
        )
    if _EVENT_ID_RE.fullmatch(event_id) is None:
        raise ValueError(
            "event_id must match EVT-*, "
            f"got {_safe_type_name(event_id)}"
        )

    if not isinstance(scope, ApprovalScope):
        raise TypeError(
            "scope must be an ApprovalScope member, "
            f"got {_safe_type_name(scope)}"
        )

    if not isinstance(subject, ApprovalSubject):
        raise TypeError(
            "subject must be an ApprovalSubject, "
            f"got {_safe_type_name(subject)}"
        )

    # Scope-specific subject validation.
    if scope in (ApprovalScope.DISPATCH, ApprovalScope.ACCEPT):
        if subject.accepted_commit is not None:
            raise ValueError(
                f"accepted_commit must be None for scope {scope.value!r}"
            )
    elif scope == ApprovalScope.INTEGRATE:
        if subject.accepted_commit is None:
            raise ValueError(
                "accepted_commit must be a 40-char hex SHA for scope integrate"
            )
        if _SHA40_RE.fullmatch(subject.accepted_commit) is None:
            raise ValueError(
                "accepted_commit must be 40 lowercase hex chars for scope integrate"
            )

    if isinstance(lease_epoch, bool) or not isinstance(lease_epoch, int):
        raise TypeError(
            "lease_epoch must be a non-bool int >= 1, "
            f"got {_safe_type_name(lease_epoch)}"
        )
    if lease_epoch < 1:
        raise ValueError(
            f"lease_epoch must be >= 1, got {lease_epoch}"
        )

    _validate_utc_now(now)

    if not isinstance(reason, str) or not reason:
        raise TypeError(
            "reason must be a non-empty str, "
            f"got {_safe_type_name(reason)}"
        )
    if reason != reason.strip():
        raise ValueError(
            "reason must not have leading or trailing whitespace"
        )
    if "\0" in reason or "\r" in reason or "\n" in reason:
        raise ValueError(
            "reason must not contain NUL, CR, or LF"
        )

    if expires_at is not None:
        if not isinstance(expires_at, str):
            raise TypeError(
                "expires_at must be str or None, "
                f"got {_safe_type_name(expires_at)}"
            )
        if _parse_rfc3339_utc(expires_at) is None:
            raise ValueError(
                "expires_at must be RFC 3339 UTC, "
                f"got {_safe_type_name(expires_at)}"
            )

    if not isinstance(expected_snapshot_commit, str):
        raise TypeError(
            "expected_snapshot_commit must be a str, "
            f"got {_safe_type_name(expected_snapshot_commit)}"
        )
    if _SHA40_RE.fullmatch(expected_snapshot_commit) is None:
        raise ValueError(
            "expected_snapshot_commit must be 40 lowercase hex chars, "
            f"got {_safe_type_name(expected_snapshot_commit)}"
        )

    # ── 2. Normalise and validate project_root ────────────────────────────
    _validate_project_root(project_root)

    # ── Build the grant mapping early for re-check. ───────────────────────
    granted_at_str = _dt_to_utc_str(now)

    grant_dict: dict[str, object] = {
        "schema_version": _SCHEMA_VERSION,
        "record_type": _RECORD_TYPE_GRANT,
        "approval_id": approval_id,
        "event_id": event_id,
        "scope": scope.value,
        "task_id": subject.task_id,
        "revision": subject.revision,
        "attempt": subject.attempt,
        "dispatch_id": subject.dispatch_id,
        "accepted_commit": subject.accepted_commit,
        "actor_role_id": _FIXED_ACTOR_ROLE_ID,
        "lease_epoch": lease_epoch,
        "granted_at": granted_at_str,
        "expires_at": expires_at,
        "reason": reason,
        "snapshot_commit": "",  # placeholder — filled after CAS
    }

    # Validate grant record in memory.
    # (snapshot_commit is empty; we'll re-validate after filling.)

    with _exclusive_state_lock(project_root):
        # ── 4. git rev-parse HEAD ────────────────────────────────────────
        head_sha = _git_rev_parse_head(project_root)

        # ── 5. Exact compare expected_snapshot_commit ─────────────────────
        if head_sha != expected_snapshot_commit:
            raise ApprovalSnapshotConflictError(
                "expected_snapshot_commit does not match current HEAD"
            )

        # Fill snapshot_commit in mapping.
        grant_dict["snapshot_commit"] = head_sha

        # ── 9. Re-validate mapping in memory ──────────────────────────────
        _validate_grant_record(grant_dict)

        # ── 9b. Build ApprovalEvidence for scope+subject duplicate check.
        _ = _grant_record_to_evidence(grant_dict)

        # ── 6. Load and strictly validate all existing evidence ───────────
        grants, revokes = _load_evidence_store(project_root)

        # ── 7. ID / reference / duplicate detection ───────────────────────
        # approval_id already exists?
        if approval_id in grants:
            raise ApprovalValidationError(
                "duplicate approval_id: " + approval_id
            )

        # event_id already exists in grants?
        for ev in grants.values():
            if ev.event_id == event_id:
                raise ApprovalValidationError(
                    "duplicate event_id: " + event_id
                )

        # event_id already exists in revokes?
        for rd in revokes.values():
            if str(rd["event_id"]) == event_id:
                raise ApprovalValidationError(
                    "duplicate event_id: " + event_id
                )

        # Already an active grant for same scope + subject?
        for ev in grants.values():
            if ev.scope == scope and ev.subject == subject:
                # Check it's not revoked (revokes check later)
                if approval_id not in revokes:
                    raise ApprovalValidationError(
                        "active grant already exists for this scope + subject"
                    )

        # Malformed evidence already checked by _load_evidence_store.

        # Target file already exists?
        target_rel = _APPROVALS_DIR_RELATIVE / f"{event_id}.yaml"
        target_abs = project_root / target_rel
        if target_abs.exists():
            raise ApprovalValidationError(
                "target evidence file already exists"
            )

        # ── 11. Atomic write ─────────────────────────────────────────────
        content = _serialize_evidence(grant_dict)
        _atomic_write(project_root, target_rel, content)

    # ── 12. Lock already released by context manager ──────────────────────
    # ── 13. Return (absolute_path, expected_snapshot_commit) ──────────────
    return (target_abs.resolve(), head_sha)


def write_revoke(
    project_root: Path,
    approval_id: str,
    event_id: str,
    lease_epoch: int,
    now: datetime,
    reason: str,
    expected_snapshot_commit: str,
) -> tuple[Path, str]:
    """Atomically write a TASK_APPROVAL revoke evidence file.

    Fixed execution order mirrors :func:`write_grant`.

    Raises ``ApprovalNotFoundError`` if the grant does not exist.
    Raises ``ApprovalAmbiguousError`` if the grant is already revoked.
    Returns ``(file_path, snapshot_commit_written)``.
    """
    # ── 1. Validate all call parameters ───────────────────────────────────
    if not isinstance(approval_id, str) or not approval_id:
        raise TypeError(
            "approval_id must be a non-empty str, "
            f"got {_safe_type_name(approval_id)}"
        )
    if _APPROVAL_ID_RE.fullmatch(approval_id) is None:
        raise ValueError(
            "approval_id must match APR-*, "
            f"got {_safe_type_name(approval_id)}"
        )

    if not isinstance(event_id, str) or not event_id:
        raise TypeError(
            "event_id must be a non-empty str, "
            f"got {_safe_type_name(event_id)}"
        )
    if _EVENT_ID_RE.fullmatch(event_id) is None:
        raise ValueError(
            "event_id must match EVT-*, "
            f"got {_safe_type_name(event_id)}"
        )

    if isinstance(lease_epoch, bool) or not isinstance(lease_epoch, int):
        raise TypeError(
            "lease_epoch must be a non-bool int >= 1, "
            f"got {_safe_type_name(lease_epoch)}"
        )
    if lease_epoch < 1:
        raise ValueError(
            f"lease_epoch must be >= 1, got {lease_epoch}"
        )

    _validate_utc_now(now)

    if not isinstance(reason, str) or not reason:
        raise TypeError(
            "reason must be a non-empty str, "
            f"got {_safe_type_name(reason)}"
        )
    if reason != reason.strip():
        raise ValueError(
            "reason must not have leading or trailing whitespace"
        )
    if "\0" in reason or "\r" in reason or "\n" in reason:
        raise ValueError(
            "reason must not contain NUL, CR, or LF"
        )

    if not isinstance(expected_snapshot_commit, str):
        raise TypeError(
            "expected_snapshot_commit must be a str, "
            f"got {_safe_type_name(expected_snapshot_commit)}"
        )
    if _SHA40_RE.fullmatch(expected_snapshot_commit) is None:
        raise ValueError(
            "expected_snapshot_commit must be 40 lowercase hex chars, "
            f"got {_safe_type_name(expected_snapshot_commit)}"
        )

    # ── 2. Normalise and validate project_root ────────────────────────────
    _validate_project_root(project_root)

    # ── Build revoke mapping early. ───────────────────────────────────────
    revoked_at_str = _dt_to_utc_str(now)

    with _exclusive_state_lock(project_root):
        # ── 4. git rev-parse HEAD ────────────────────────────────────────
        head_sha = _git_rev_parse_head(project_root)

        # ── 5. Exact compare expected_snapshot_commit ─────────────────────
        if head_sha != expected_snapshot_commit:
            raise ApprovalSnapshotConflictError(
                "expected_snapshot_commit does not match current HEAD"
            )

        # ── 6. Load and strictly validate all existing evidence ───────────
        grants, revokes = _load_evidence_store(project_root)

        # ── 7. ID / reference / duplicate detection ───────────────────────
        # Grant must exist.
        target_grant = grants.get(approval_id)
        if target_grant is None:
            raise ApprovalNotFoundError(
                "no grant found for approval_id: " + approval_id
            )

        # Already revoked?
        if approval_id in revokes:
            raise ApprovalAmbiguousError(
                "grant already revoked for approval_id: " + approval_id
            )

        # event_id already exists in grants?
        for ev in grants.values():
            if ev.event_id == event_id:
                raise ApprovalValidationError(
                    "duplicate event_id: " + event_id
                )

        # event_id already exists in revokes?
        for rd in revokes.values():
            if str(rd["event_id"]) == event_id:
                raise ApprovalValidationError(
                    "duplicate event_id: " + event_id
                )

        # task_id from grant.
        task_id = target_grant.subject.task_id

        # Target file already exists?
        target_rel = _APPROVALS_DIR_RELATIVE / f"{event_id}.yaml"
        target_abs = project_root / target_rel
        if target_abs.exists():
            raise ApprovalValidationError(
                "target evidence file already exists"
            )

        # Build revoke mapping.
        revoke_dict: dict[str, object] = {
            "schema_version": _SCHEMA_VERSION,
            "record_type": _RECORD_TYPE_REVOKE,
            "approval_id": approval_id,
            "event_id": event_id,
            "task_id": task_id,
            "actor_role_id": _FIXED_ACTOR_ROLE_ID,
            "lease_epoch": lease_epoch,
            "revoked_at": revoked_at_str,
            "reason": reason,
            "snapshot_commit": head_sha,
        }

        # ── 9. Re-validate mapping in memory ──────────────────────────────
        _validate_revoke_record(revoke_dict)

        # ── 11. Atomic write ─────────────────────────────────────────────
        content = _serialize_evidence(revoke_dict)
        _atomic_write(project_root, target_rel, content)

    # ── 12. Lock already released by context manager ──────────────────────
    return (target_abs.resolve(), head_sha)
