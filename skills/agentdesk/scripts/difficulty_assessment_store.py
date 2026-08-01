"""Durable filesystem and Git-ancestry boundary for TaskDifficulty evidence.

This module owns only persistence and replay of the already-validated,
canonical evidence envelope.  It does not assess difficulty, dispatch work,
or evaluate approvals.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from approval_gate import (
    ApprovalSnapshotConflictError,
    _exclusive_state_lock,
)
from difficulty_assessment_evidence import (
    DifficultyAssessmentEvidence,
    decode_difficulty_assessment_evidence,
    encode_difficulty_assessment_evidence,
)
from dispatch_supervisor_evidence import (
    _atomic_write_bytes,
    _is_symlink_or_reparse,
)

__all__ = [
    "DifficultyAssessmentStoreRequest",
    "DifficultyAssessmentStoreResult",
    "DifficultyAssessmentStore",
    "DifficultyAssessmentStoreError",
    "DifficultyAssessmentStoreInputError",
    "DifficultyAssessmentStoreSecurityError",
    "DifficultyAssessmentStoreConflictError",
    "DifficultyAssessmentStoreAncestryError",
    "read_by_task_revision",
    "validate_ancestry_for_dispatch",
]


_ASSESSMENTS_RELATIVE = Path("docs") / "pm" / "assessments"
_SHA40_RE = re.compile(r"[0-9a-f]{40}\Z")
_COMPONENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_LOCK_CONTENTION = "control-plane state lock is currently held"
_LOCK_WAIT = threading.Event()


@dataclass(frozen=True, slots=True)
class DifficultyAssessmentStoreRequest:
    project_root: Path
    evidence: DifficultyAssessmentEvidence
    expected_head: str


@dataclass(frozen=True, slots=True)
class DifficultyAssessmentStoreResult:
    assessment_id: str
    relative_path: str
    content_sha256: str
    replayed: bool


class DifficultyAssessmentStoreError(ValueError):
    """Base class for typed evidence-store failures."""


class DifficultyAssessmentStoreInputError(DifficultyAssessmentStoreError):
    """Raised for invalid requests or malformed stored evidence."""


class DifficultyAssessmentStoreSecurityError(DifficultyAssessmentStoreError):
    """Raised when a filesystem or input boundary cannot be trusted."""


class DifficultyAssessmentStoreConflictError(DifficultyAssessmentStoreError):
    """Raised when immutable evidence identity or content conflicts."""


class DifficultyAssessmentStoreAncestryError(DifficultyAssessmentStoreError):
    """Raised when Git snapshot identity or ancestry cannot be verified."""


def _error(error_type: type[DifficultyAssessmentStoreError], code: str) -> None:
    raise error_type(f"difficulty_assessment_store:{code}")


def _validate_component(value: object, field: str) -> str:
    if type(value) is not str:
        _error(DifficultyAssessmentStoreInputError, f"{field}_type")
    if not value or "\x00" in value or "\r" in value or "\n" in value:
        _error(DifficultyAssessmentStoreInputError, f"{field}_unsafe")
    if value in (".", "..") or "/" in value or "\\" in value:
        _error(DifficultyAssessmentStoreSecurityError, f"{field}_path")
    if _COMPONENT_RE.fullmatch(value) is None:
        _error(DifficultyAssessmentStoreInputError, f"{field}_format")
    return value


def _validate_revision(value: object) -> int:
    if type(value) is not int or value < 1:
        _error(DifficultyAssessmentStoreInputError, "revision")
    return value


def _validate_head(value: object) -> str:
    if type(value) is not str or _SHA40_RE.fullmatch(value) is None:
        _error(DifficultyAssessmentStoreAncestryError, "expected_head")
    return value


def _validate_project_root(project_root: object) -> Path:
    if not isinstance(project_root, Path):
        _error(DifficultyAssessmentStoreInputError, "project_root_type")
    if not project_root.is_absolute():
        _error(DifficultyAssessmentStoreInputError, "project_root_absolute")
    try:
        if _is_symlink_or_reparse(project_root):
            _error(DifficultyAssessmentStoreSecurityError, "project_root_link")
        root_stat = os.lstat(str(project_root))
    except DifficultyAssessmentStoreError:
        raise
    except OSError:
        _error(DifficultyAssessmentStoreInputError, "project_root_missing")
    if not stat.S_ISDIR(root_stat.st_mode):
        _error(DifficultyAssessmentStoreInputError, "project_root_directory")
    return project_root


def _ensure_directory(path: Path, *, create: bool) -> None:
    try:
        if _is_symlink_or_reparse(path):
            _error(DifficultyAssessmentStoreSecurityError, "directory_link")
        entry_stat = os.lstat(str(path))
    except FileNotFoundError:
        if not create:
            _error(DifficultyAssessmentStoreInputError, "evidence_missing")
        try:
            path.mkdir()
        except FileExistsError:
            return _ensure_directory(path, create=False)
        except OSError:
            _error(DifficultyAssessmentStoreSecurityError, "directory_create")
        return
    except DifficultyAssessmentStoreError:
        raise
    except OSError:
        _error(DifficultyAssessmentStoreSecurityError, "directory_unreadable")
    if not stat.S_ISDIR(entry_stat.st_mode):
        _error(DifficultyAssessmentStoreSecurityError, "directory_not_directory")


def _resolved_within(path: Path, root: Path, *, strict: bool) -> None:
    try:
        resolved_root = root.resolve(strict=True)
        resolved_path = path.resolve(strict=strict)
        resolved_path.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        _error(DifficultyAssessmentStoreSecurityError, "path_escape")


def _target_path(
    project_root: object,
    task_id: object,
    revision: object,
    assessment_id: object,
    *,
    create: bool,
) -> tuple[Path, Path, Path, str]:
    root = _validate_project_root(project_root)
    task = _validate_component(task_id, "task_id")
    rev = _validate_revision(revision)
    assessment = _validate_component(assessment_id, "assessment_id")

    current = root
    for part in ("docs", "pm", "assessments"):
        current = current / part
        _ensure_directory(current, create=create)
    assessments_root = current
    task_dir = assessments_root / task
    _ensure_directory(task_dir, create=create)
    revision_dir = task_dir / f"r{rev}"
    _ensure_directory(revision_dir, create=create)

    target = revision_dir / f"{assessment}.yaml"
    _resolved_within(assessments_root, assessments_root, strict=True)
    _resolved_within(target.parent, assessments_root, strict=True)
    relative_path = target.relative_to(root).as_posix()
    return root, assessments_root, target, relative_path


def _read_regular_bytes(path: Path, *, missing_is_error: bool) -> bytes | None:
    try:
        if _is_symlink_or_reparse(path):
            _error(DifficultyAssessmentStoreSecurityError, "file_link")
        entry_stat = os.lstat(str(path))
    except FileNotFoundError:
        if missing_is_error:
            _error(DifficultyAssessmentStoreInputError, "evidence_missing")
        return None
    except DifficultyAssessmentStoreError:
        raise
    except OSError:
        _error(DifficultyAssessmentStoreSecurityError, "file_unreadable")
    if not stat.S_ISREG(entry_stat.st_mode):
        _error(DifficultyAssessmentStoreSecurityError, "file_not_regular")
    try:
        with path.open("rb") as handle:
            return handle.read()
    except FileNotFoundError:
        _error(DifficultyAssessmentStoreSecurityError, "file_changed")
    except OSError:
        _error(DifficultyAssessmentStoreSecurityError, "file_unreadable")


def _decode_stored(data: bytes) -> DifficultyAssessmentEvidence:
    try:
        return decode_difficulty_assessment_evidence(data)
    except Exception:
        _error(DifficultyAssessmentStoreInputError, "evidence_invalid")


def _path_identity(relative_path: Path, evidence: DifficultyAssessmentEvidence) -> None:
    assessment = evidence.assessment
    task = _validate_component(assessment.task_id, "stored_task_id")
    revision = _validate_revision(assessment.revision)
    assessment_id = _validate_component(assessment.assessment_id, "stored_assessment_id")
    expected = Path(task) / f"r{revision}" / f"{assessment_id}.yaml"
    if relative_path != expected:
        _error(DifficultyAssessmentStoreSecurityError, "stored_identity")


def _scan_yaml_files(
    assessments_root: Path,
) -> list[tuple[Path, bytes, DifficultyAssessmentEvidence]]:
    records: list[tuple[Path, bytes, DifficultyAssessmentEvidence]] = []

    def visit(directory: Path) -> None:
        try:
            entries = list(os.scandir(str(directory)))
        except OSError:
            _error(DifficultyAssessmentStoreSecurityError, "directory_unreadable")
        for entry in entries:
            path = Path(entry.path)
            try:
                if _is_symlink_or_reparse(path):
                    _error(DifficultyAssessmentStoreSecurityError, "scan_link")
                entry_stat = os.lstat(str(path))
            except DifficultyAssessmentStoreError:
                raise
            except OSError:
                _error(DifficultyAssessmentStoreSecurityError, "scan_changed")
            if stat.S_ISDIR(entry_stat.st_mode):
                visit(path)
            elif stat.S_ISREG(entry_stat.st_mode) and path.suffix == ".yaml":
                data = _read_regular_bytes(path, missing_is_error=True)
                assert data is not None
                evidence = _decode_stored(data)
                try:
                    relative_path = path.relative_to(assessments_root)
                except ValueError:
                    _error(DifficultyAssessmentStoreSecurityError, "scan_escape")
                _path_identity(relative_path, evidence)
                records.append((relative_path, data, evidence))

    visit(assessments_root)
    return records


def _git_command(project_root: Path, argv: list[str]) -> tuple[int, bytes, bytes]:
    try:
        result = subprocess.run(
            argv,
            cwd=str(project_root),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        _error(DifficultyAssessmentStoreAncestryError, "git_command")
    stdout = result.stdout if type(result.stdout) is bytes else b""
    stderr = result.stderr if type(result.stderr) is bytes else b""
    return result.returncode, stdout, stderr


def _git_stdout_sha(project_root: Path, argv: list[str]) -> str:
    returncode, stdout, _ = _git_command(project_root, argv)
    if returncode != 0:
        _error(DifficultyAssessmentStoreAncestryError, "git_command")
    try:
        value = stdout.decode("utf-8").strip()
    except UnicodeDecodeError:
        _error(DifficultyAssessmentStoreAncestryError, "git_output")
    if _SHA40_RE.fullmatch(value) is None:
        _error(DifficultyAssessmentStoreAncestryError, "git_output")
    return value


def _validate_ancestry(
    project_root: Path,
    evidence: DifficultyAssessmentEvidence,
    expected_head: str,
) -> None:
    expected = _validate_head(expected_head)
    actual = _git_stdout_sha(project_root, ["git", "rev-parse", "HEAD"])
    if actual != expected:
        _error(DifficultyAssessmentStoreAncestryError, "head_mismatch")

    snapshot = evidence.snapshot_commit
    if type(snapshot) is not str or _SHA40_RE.fullmatch(snapshot) is None:
        _error(DifficultyAssessmentStoreAncestryError, "snapshot_format")
    returncode, stdout, _ = _git_command(
        project_root,
        ["git", "cat-file", "-t", snapshot],
    )
    if returncode != 0 or stdout.strip() != b"commit":
        _error(DifficultyAssessmentStoreAncestryError, "snapshot_not_commit")
    if snapshot == expected:
        return
    returncode, _, _ = _git_command(
        project_root,
        ["git", "merge-base", "--is-ancestor", snapshot, expected],
    )
    if returncode != 0:
        _error(DifficultyAssessmentStoreAncestryError, "snapshot_not_ancestor")


@contextmanager
def _blocking_state_lock(project_root: Path) -> Iterator[None]:
    while True:
        try:
            with _exclusive_state_lock(project_root):
                yield
            return
        except ApprovalSnapshotConflictError as exc:
            if str(exc) != _LOCK_CONTENTION:
                _error(DifficultyAssessmentStoreSecurityError, "lock_unavailable")
            _LOCK_WAIT.wait(0.01)


def _validate_request(request: object) -> tuple[Path, DifficultyAssessmentEvidence, str, bytes]:
    if type(request) is not DifficultyAssessmentStoreRequest:
        _error(DifficultyAssessmentStoreInputError, "request_type")
    evidence = request.evidence
    if type(evidence) is not DifficultyAssessmentEvidence:
        _error(DifficultyAssessmentStoreInputError, "evidence_type")
    try:
        encoded = encode_difficulty_assessment_evidence(evidence)
    except Exception:
        _error(DifficultyAssessmentStoreInputError, "evidence_invalid")
    root = _validate_project_root(request.project_root)
    expected_head = _validate_head(request.expected_head)
    _validate_component(evidence.assessment.task_id, "task_id")
    _validate_component(evidence.assessment.assessment_id, "assessment_id")
    _validate_revision(evidence.assessment.revision)
    return root, evidence, expected_head, encoded


class DifficultyAssessmentStore:
    """Persist and replay canonical TaskDifficulty assessment evidence."""

    def write(self, request: DifficultyAssessmentStoreRequest) -> DifficultyAssessmentStoreResult:
        root, evidence, expected_head, encoded = _validate_request(request)
        assessment = evidence.assessment
        _, assessments_root, target, relative_path = _target_path(
            root,
            assessment.task_id,
            assessment.revision,
            assessment.assessment_id,
            create=True,
        )
        content_sha256 = hashlib.sha256(encoded).hexdigest()

        with _blocking_state_lock(root):
            _validate_ancestry(root, evidence, expected_head)
            records = _scan_yaml_files(assessments_root)
            target_relative = target.relative_to(assessments_root)
            target_was_scanned = False
            for existing_relative, existing_data, existing_evidence in records:
                if existing_evidence.assessment.assessment_id != assessment.assessment_id:
                    continue
                if existing_relative == target_relative:
                    target_was_scanned = True
                if existing_relative != target_relative or existing_data != encoded:
                    _error(DifficultyAssessmentStoreConflictError, "assessment_id_conflict")

            existing = _read_regular_bytes(target, missing_is_error=target_was_scanned)
            if existing is not None:
                if existing == encoded:
                    return DifficultyAssessmentStoreResult(
                        assessment_id=assessment.assessment_id,
                        relative_path=relative_path,
                        content_sha256=content_sha256,
                        replayed=True,
                    )
                _error(DifficultyAssessmentStoreConflictError, "content_conflict")

            try:
                _atomic_write_bytes(target, encoded)
            except DifficultyAssessmentStoreError:
                raise
            except OSError:
                _error(DifficultyAssessmentStoreError, "atomic_write")
            written = _read_regular_bytes(target, missing_is_error=True)
            if written != encoded:
                _error(DifficultyAssessmentStoreError, "atomic_write_verification")
            return DifficultyAssessmentStoreResult(
                assessment_id=assessment.assessment_id,
                relative_path=relative_path,
                content_sha256=content_sha256,
                replayed=False,
            )

    def read(
        self,
        project_root: Path,
        task_id: str,
        revision: int,
        assessment_id: str,
    ) -> DifficultyAssessmentEvidence:
        _, assessments_root, target, _ = _target_path(
            project_root,
            task_id,
            revision,
            assessment_id,
            create=False,
        )
        _resolved_within(target, assessments_root, strict=False)
        data = _read_regular_bytes(target, missing_is_error=True)
        assert data is not None
        evidence = _decode_stored(data)
        if (
            evidence.assessment.task_id != task_id
            or evidence.assessment.revision != revision
            or evidence.assessment.assessment_id != assessment_id
        ):
            _error(DifficultyAssessmentStoreSecurityError, "read_identity")
        _resolved_within(target, assessments_root, strict=True)
        return evidence


def read_by_task_revision(
    project_root: Path,
    task_id: str,
    revision: int,
) -> DifficultyAssessmentEvidence | None:
    """Read the single assessment evidence for a task revision, or None.

    This is a read-only lookup — no lock, no git, no write.  It scans
    the revision directory for one ``.yaml`` evidence file.  If zero or
    more than one file exists it returns ``None`` (ambiguous evidence is
    not valid dispatch input).
    """
    if not isinstance(project_root, Path):
        return None
    if not project_root.is_absolute():
        return None
    try:
        if _is_symlink_or_reparse(project_root):
            return None
        root_stat = os.lstat(str(project_root))
    except OSError:
        return None
    if not stat.S_ISDIR(root_stat.st_mode):
        return None

    task = _validate_component(task_id, "task_id")
    rev = _validate_revision(revision)
    revision_dir = (
        project_root / _ASSESSMENTS_RELATIVE / task / f"r{rev}"
    )

    try:
        if _is_symlink_or_reparse(revision_dir):
            return None
        entries = list(os.scandir(str(revision_dir)))
    except (OSError, DifficultyAssessmentStoreError):
        return None

    candidates: list[tuple[str, bytes]] = []
    for entry in entries:
        path = Path(entry.path)
        try:
            if _is_symlink_or_reparse(path):
                return None
            entry_stat = os.lstat(str(path))
        except OSError:
            return None
        if stat.S_ISREG(entry_stat.st_mode) and path.suffix == ".yaml":
            try:
                data = path.read_bytes()
            except OSError:
                return None
            candidates.append((path.name, data))

    if len(candidates) != 1:
        return None

    try:
        evidence = _decode_stored(candidates[0][1])
    except Exception:
        return None
    if (
        evidence.assessment.task_id != task_id
        or evidence.assessment.revision != revision
    ):
        return None
    return evidence


def validate_ancestry_for_dispatch(
    project_root: Path,
    snapshot_commit: str,
    expected_head: str,
) -> None:
    """Validate snapshot_commit is an ancestor of expected_head.

    Raises ``DifficultyAssessmentStoreAncestryError`` on failure.
    Read-only dispatch-side validator — no lock, no write.
    """
    if type(snapshot_commit) is not str or _SHA40_RE.fullmatch(snapshot_commit) is None:
        _error(DifficultyAssessmentStoreAncestryError, "snapshot_format")
    if type(expected_head) is not str or _SHA40_RE.fullmatch(expected_head) is None:
        _error(DifficultyAssessmentStoreAncestryError, "expected_head_format")
    returncode, stdout, _ = _git_command(
        project_root,
        ["git", "-C", str(project_root), "cat-file", "-t", snapshot_commit],
    )
    if returncode != 0 or stdout.strip() != b"commit":
        _error(DifficultyAssessmentStoreAncestryError, "snapshot_not_commit")
    if snapshot_commit == expected_head:
        return
    returncode, _, _ = _git_command(
        project_root,
        [
            "git", "-C", str(project_root),
            "merge-base", "--is-ancestor",
            snapshot_commit, expected_head,
        ],
    )
    if returncode != 0:
        _error(DifficultyAssessmentStoreAncestryError, "snapshot_not_ancestor")
