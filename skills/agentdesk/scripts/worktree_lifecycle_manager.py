"""Create/release runtime for durable AgentDesk managed Git worktrees."""

from __future__ import annotations

import os
import stat
import subprocess
import threading
from pathlib import Path

from dispatch_supervisor_evidence import _is_symlink_or_reparse
from worktree_lifecycle_store import (
    SCHEMA_VERSION,
    WorktreeInventoryEntry,
    WorktreeLifecycleAction,
    WorktreeLifecycleConflictError,
    WorktreeLifecycleInputError,
    WorktreeLifecycleOutcome,
    WorktreeLifecyclePermissionError,
    WorktreeLifecycleRequest,
    WorktreeLifecycleResult,
    WorktreeLifecycleSecurityError,
    WorktreeLifecycleStore,
    WorktreeLifecycleStoreError,
    WorktreePhase,
    WorktreeReconciliationAction,
    expected_worktree_path,
    inventory_digest,
    make_record,
    parse_worktree_inventory,
    reconcile_worktree,
    validate_request_identity,
)


class WorktreeLifecycleManagerError(WorktreeLifecycleStoreError):
    """Stable manager failure with no Git output or path disclosure."""


class WorktreeLifecycleGitError(WorktreeLifecycleManagerError):
    pass


class WorktreeLifecycleReleaseRefusedError(WorktreeLifecycleManagerError):
    pass


def _fail(error_type: type[WorktreeLifecycleManagerError], code: str) -> None:
    raise error_type("worktree_lifecycle_manager:" + code)


_OPERATION_LOCKS: dict[str, threading.Lock] = {}
_OPERATION_LOCKS_GUARD = threading.Lock()


def _operation_lock(repository_root: str, worktree_id: str) -> threading.Lock:
    key = os.path.normcase(repository_root) + "\0" + worktree_id
    with _OPERATION_LOCKS_GUARD:
        lock = _OPERATION_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _OPERATION_LOCKS[key] = lock
        return lock


def _run_git(cwd: Path, arguments: tuple[str, ...], timeout_seconds: float) -> bytes:
    if not isinstance(cwd, Path) or not cwd.is_absolute():
        _fail(WorktreeLifecycleGitError, "cwd")
    if type(arguments) is not tuple or any(type(value) is not str for value in arguments):
        _fail(WorktreeLifecycleGitError, "argv")
    if type(timeout_seconds) not in (int, float) or isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
        _fail(WorktreeLifecycleGitError, "timeout")
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired):
        _fail(WorktreeLifecycleGitError, "execution")
    if completed.returncode != 0:
        _fail(WorktreeLifecycleGitError, "nonzero")
    return bytes(completed.stdout)


def _git_success(cwd: Path, arguments: tuple[str, ...], timeout_seconds: float) -> bool:
    try:
        _run_git(cwd, arguments, timeout_seconds)
        return True
    except WorktreeLifecycleGitError as exc:
        if str(exc) == "worktree_lifecycle_manager:nonzero":
            return False
        raise


def _decoded_single_line(data: bytes, code: str) -> str:
    try:
        value = data.decode("utf-8").strip()
    except UnicodeDecodeError:
        _fail(WorktreeLifecycleGitError, code + "_utf8")
    if not value or "\n" in value or "\r" in value or "\0" in value:
        _fail(WorktreeLifecycleGitError, code + "_shape")
    return value


def _safe_directory(path: Path, code: str) -> None:
    try:
        if _is_symlink_or_reparse(path):
            _fail(WorktreeLifecycleSecurityError, code + "_link")
        mode = os.lstat(str(path)).st_mode
    except FileNotFoundError:
        _fail(WorktreeLifecycleSecurityError, code + "_missing")
    except OSError:
        _fail(WorktreeLifecyclePermissionError, code + "_unknown")
    if not stat.S_ISDIR(mode):
        _fail(WorktreeLifecycleSecurityError, code + "_not_directory")


def _find_entry(
    inventory: tuple[WorktreeInventoryEntry, ...], worktree_path: str
) -> WorktreeInventoryEntry | None:
    matches = tuple(
        entry for entry in inventory
        if os.path.normcase(entry.worktree_path) == os.path.normcase(worktree_path)
    )
    if len(matches) > 1:
        _fail(WorktreeLifecycleGitError, "inventory_duplicate")
    return matches[0] if matches else None


class WorktreeLifecycleManager:
    """Owns only managed-worktree Git side effects and their durable evidence."""

    def __init__(self, repository_root: Path, timeout_seconds: float = 15.0) -> None:
        if not isinstance(repository_root, Path) or not repository_root.is_absolute():
            _fail(WorktreeLifecycleGitError, "repository_root")
        _safe_directory(repository_root, "repository_root")
        if type(timeout_seconds) not in (int, float) or isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
            _fail(WorktreeLifecycleGitError, "timeout")
        self._repository_root = repository_root
        self._timeout_seconds = float(timeout_seconds)
        self._store = WorktreeLifecycleStore(repository_root)

    @property
    def store(self) -> WorktreeLifecycleStore:
        return self._store

    def _repository_identity(self, request: WorktreeLifecycleRequest) -> str:
        validate_request_identity(request)
        top = _decoded_single_line(
            _run_git(
                self._repository_root,
                ("rev-parse", "--path-format=absolute", "--show-toplevel"),
                self._timeout_seconds,
            ),
            "repository_root",
        )
        common_dir = _decoded_single_line(
            _run_git(
                self._repository_root,
                ("rev-parse", "--path-format=absolute", "--git-common-dir"),
                self._timeout_seconds,
            ),
            "common_dir",
        )
        if os.path.normcase(os.path.normpath(top)) != os.path.normcase(request.repository_root):
            _fail(WorktreeLifecycleGitError, "repository_root_identity")
        if os.path.normcase(os.path.normpath(common_dir)) != os.path.normcase(request.repository_common_dir):
            _fail(WorktreeLifecycleGitError, "common_dir_identity")
        if not _git_success(
            self._repository_root,
            ("cat-file", "-e", request.base_commit + "^{commit}"),
            self._timeout_seconds,
        ):
            _fail(WorktreeLifecycleGitError, "base_missing")
        head = _decoded_single_line(
            _run_git(self._repository_root, ("rev-parse", "HEAD"), self._timeout_seconds),
            "head",
        )
        if not _git_success(
            self._repository_root,
            ("merge-base", "--is-ancestor", request.base_commit, head),
            self._timeout_seconds,
        ):
            _fail(WorktreeLifecycleConflictError, "base_ancestry")
        return head

    def _inventory(self, common_dir: str) -> tuple[WorktreeInventoryEntry, ...]:
        raw = _run_git(
            self._repository_root,
            ("worktree", "list", "--porcelain", "-z"),
            self._timeout_seconds,
        )
        return parse_worktree_inventory(raw, common_dir)

    def _clean(self, worktree_path: str) -> bool:
        path = Path(worktree_path)
        _safe_directory(path, "managed_worktree")
        return _run_git(
            path,
            ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
            self._timeout_seconds,
        ) == b""

    def _ensure_managed_parent(self, request: WorktreeLifecycleRequest) -> None:
        expected = Path(expected_worktree_path(request.repository_root, request.expected_worktree_id))
        if os.path.normcase(str(expected)) != os.path.normcase(request.worktree_path):
            _fail(WorktreeLifecycleSecurityError, "managed_path")
        parent = expected.parent
        if parent.exists():
            _safe_directory(parent, "managed_root")
            return
        try:
            parent.mkdir()
        except OSError:
            _fail(WorktreeLifecyclePermissionError, "managed_root_create")
        _safe_directory(parent, "managed_root")

    def create(self, request: WorktreeLifecycleRequest) -> WorktreeLifecycleResult:
        if type(request) is not WorktreeLifecycleRequest or request.action is not WorktreeLifecycleAction.CREATE:
            _fail(WorktreeLifecycleInputError, "create_request")
        with _operation_lock(request.repository_root, request.expected_worktree_id):
            self._repository_identity(request)
            inventory_before = self._inventory(request.repository_common_dir)
            reservation = self._store.reserve(request)
            record = self._store.read_record(request.expected_worktree_id, missing_ok=True)
            if record is None:
                record = self._store.create_record(
                    make_record(
                        reservation,
                        WorktreePhase.CREATING,
                        inventory_digest(inventory_before),
                    )
                )
            decision = reconcile_worktree(
                request.expected_worktree_id, reservation, record,
                inventory_before, None,
            )
            if record.phase is WorktreePhase.READY:
                entry = _find_entry(inventory_before, request.worktree_path)
                if entry is None or not self._clean(request.worktree_path):
                    _fail(WorktreeLifecycleConflictError, "ready_replay")
                return WorktreeLifecycleResult(
                    SCHEMA_VERSION, request.operation_id, request.action,
                    WorktreeLifecycleOutcome.REPLAYED, record.worktree_id,
                    record.phase, record.worktree_path, record.head_commit,
                    WorktreeReconciliationAction.NO_OP,
                )
            if decision.action is WorktreeReconciliationAction.ADOPT_READY:
                if not self._clean(request.worktree_path):
                    _fail(WorktreeLifecycleConflictError, "adopt_dirty")
            elif decision.action is WorktreeReconciliationAction.RESUME_CREATE:
                self._ensure_managed_parent(request)
                if Path(request.worktree_path).exists():
                    _fail(WorktreeLifecycleSecurityError, "unregistered_path")
                try:
                    _run_git(
                        self._repository_root,
                        ("worktree", "add", "-b", request.branch,
                         request.worktree_path, request.base_commit),
                        self._timeout_seconds,
                    )
                except WorktreeLifecycleGitError:
                    inventory_after_failure = self._inventory(request.repository_common_dir)
                    entry = _find_entry(inventory_after_failure, request.worktree_path)
                    if entry is None or entry.branch != request.branch or entry.head_commit != request.base_commit:
                        raise
                if not self._clean(request.worktree_path):
                    _fail(WorktreeLifecycleConflictError, "created_dirty")
            else:
                _fail(WorktreeLifecycleConflictError, "create_reconciliation")
            inventory_after = self._inventory(request.repository_common_dir)
            entry = _find_entry(inventory_after, request.worktree_path)
            if (
                entry is None or entry.branch != request.branch
                or entry.head_commit != request.base_commit or entry.is_detached
                or entry.is_bare or entry.is_prunable
            ):
                _fail(WorktreeLifecycleConflictError, "created_identity")
            ready = self._store.advance_record(
                request.expected_worktree_id, WorktreePhase.CREATING,
                WorktreePhase.READY, inventory_digest(inventory_after),
                request.requested_at, entry.head_commit,
            )
            return WorktreeLifecycleResult(
                SCHEMA_VERSION, request.operation_id, request.action,
                WorktreeLifecycleOutcome.READY, ready.worktree_id, ready.phase,
                ready.worktree_path, ready.head_commit,
                WorktreeReconciliationAction.ADOPT_READY,
            )

    def release(
        self,
        request: WorktreeLifecycleRequest,
        *,
        process_active: bool | None,
        lease_active: bool | None,
    ) -> WorktreeLifecycleResult:
        if type(request) is not WorktreeLifecycleRequest or request.action is not WorktreeLifecycleAction.RELEASE:
            _fail(WorktreeLifecycleInputError, "release_request")
        if type(process_active) is not bool or type(lease_active) is not bool:
            _fail(WorktreeLifecycleReleaseRefusedError, "ownership_unknown")
        if process_active or lease_active:
            _fail(WorktreeLifecycleReleaseRefusedError, "ownership_active")
        with _operation_lock(request.repository_root, request.expected_worktree_id):
            self._repository_identity(request)
            reservation = self._store.read_reservation(request.expected_worktree_id)
            record = self._store.read_record(request.expected_worktree_id, missing_ok=False)
            if record is None:
                _fail(WorktreeLifecycleReleaseRefusedError, "record_missing")
            if (
                reservation.worktree_id != record.worktree_id
                or reservation.reservation_id != record.reservation_id
                or reservation.worktree_path != request.worktree_path
                or reservation.branch != request.branch
                or reservation.base_commit != request.base_commit
                or reservation.task_id != request.task_id
                or reservation.revision != request.revision
                or reservation.attempt != request.attempt
                or reservation.dispatch_id != request.dispatch_id
            ):
                _fail(WorktreeLifecycleReleaseRefusedError, "identity")
            inventory_before = self._inventory(request.repository_common_dir)
            entry = _find_entry(inventory_before, request.worktree_path)
            if record.phase is WorktreePhase.RELEASED:
                if entry is not None:
                    _fail(WorktreeLifecycleReleaseRefusedError, "released_present")
                return WorktreeLifecycleResult(
                    SCHEMA_VERSION, request.operation_id, request.action,
                    WorktreeLifecycleOutcome.REPLAYED, record.worktree_id,
                    record.phase, record.worktree_path, record.head_commit,
                    WorktreeReconciliationAction.NO_OP,
                )
            if record.phase is WorktreePhase.READY:
                if entry is None:
                    _fail(WorktreeLifecycleReleaseRefusedError, "inventory_missing")
                if (
                    entry.branch != record.branch or entry.head_commit != record.head_commit
                    or entry.is_bare or entry.is_detached or entry.is_locked
                    or entry.is_prunable
                ):
                    _fail(WorktreeLifecycleReleaseRefusedError, "inventory_identity")
                if not self._clean(request.worktree_path):
                    _fail(WorktreeLifecycleReleaseRefusedError, "dirty")
                record = self._store.advance_record(
                    request.expected_worktree_id, WorktreePhase.READY,
                    WorktreePhase.RELEASING, inventory_digest(inventory_before),
                    request.requested_at,
                )
            elif record.phase is not WorktreePhase.RELEASING:
                _fail(WorktreeLifecycleReleaseRefusedError, "phase")
            if entry is not None:
                if not self._clean(request.worktree_path):
                    _fail(WorktreeLifecycleReleaseRefusedError, "dirty")
                _run_git(
                    self._repository_root,
                    ("worktree", "remove", request.worktree_path),
                    self._timeout_seconds,
                )
            inventory_after = self._inventory(request.repository_common_dir)
            if _find_entry(inventory_after, request.worktree_path) is not None:
                _fail(WorktreeLifecycleReleaseRefusedError, "remove_incomplete")
            released = self._store.advance_record(
                request.expected_worktree_id, WorktreePhase.RELEASING,
                WorktreePhase.RELEASED, inventory_digest(inventory_after),
                request.requested_at,
            )
            return WorktreeLifecycleResult(
                SCHEMA_VERSION, request.operation_id, request.action,
                WorktreeLifecycleOutcome.RELEASED, released.worktree_id,
                released.phase, released.worktree_path, released.head_commit,
                WorktreeReconciliationAction.NO_OP,
            )
