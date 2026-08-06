"""Durable local Git Integration owner (TC-13.29l.5h.2)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, fields, replace
from datetime import datetime
from enum import Enum
from pathlib import Path

SCHEMA_VERSION = "agentdesk.git-integration/v1"
_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")


class GitIntegrationError(RuntimeError):
    pass


class GitIntegrationInputError(GitIntegrationError):
    pass


class GitIntegrationConflictError(GitIntegrationError):
    pass


class GitIntegrationRecoveryRequiredError(GitIntegrationError):
    pass


class GitIntegrationMethod(str, Enum):
    FAST_FORWARD = "FAST_FORWARD"
    MERGE_TREE = "MERGE_TREE"


class GitIntegrationPhase(str, Enum):
    RESERVED = "RESERVED"
    VALIDATED = "VALIDATED"
    TREE_PREPARED = "TREE_PREPARED"
    REF_UPDATED = "REF_UPDATED"
    FINALIZED = "FINALIZED"


class GitIntegrationOutcome(str, Enum):
    RESERVED = "RESERVED"
    FAST_FORWARDED = "FAST_FORWARDED"
    MERGED = "MERGED"
    ALREADY_INTEGRATED = "ALREADY_INTEGRATED"
    CONFLICT = "CONFLICT"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    REJECTED = "REJECTED"
    FINALIZED = "FINALIZED"


@dataclass(frozen=True, slots=True)
class GitIntegrationRequest:
    schema_version: str
    operation_id: str
    project_root: Path
    task_id: str
    revision: int
    attempt: int
    dispatch_id: str
    source_worktree: Path
    source_branch: str
    base_commit: str
    implementation_commit: str
    report_commit: str
    target_branch: str
    expected_target_head: str
    method: GitIntegrationMethod
    commit_message: str
    requested_at: str
    content_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise GitIntegrationInputError("integration:schema")
        for name in ("operation_id", "task_id", "dispatch_id"):
            value = getattr(self, name)
            if type(value) is not str or _SAFE_ID.fullmatch(value) is None:
                raise GitIntegrationInputError(f"integration:{name}")
        for name in ("revision", "attempt"):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= 3:
                raise GitIntegrationInputError(f"integration:{name}")
        for name in ("project_root", "source_worktree"):
            value = getattr(self, name)
            if not isinstance(value, Path) or not value.is_absolute():
                raise GitIntegrationInputError(f"integration:{name}")
        for name in ("base_commit", "implementation_commit", "report_commit", "expected_target_head"):
            if type(getattr(self, name)) is not str or _SHA.fullmatch(getattr(self, name)) is None:
                raise GitIntegrationInputError(f"integration:{name}")
        for name in ("source_branch", "target_branch", "commit_message", "requested_at"):
            value = getattr(self, name)
            if type(value) is not str or not value or value != value.strip() or "\0" in value or "\r" in value:
                raise GitIntegrationInputError(f"integration:{name}")
        if "\n" in self.source_branch or "\n" in self.target_branch or "\n" in self.requested_at:
            raise GitIntegrationInputError("integration:text")
        try:
            stamp = datetime.fromisoformat(self.requested_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise GitIntegrationInputError("integration:requested_at") from exc
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise GitIntegrationInputError("integration:requested_at")
        if type(self.method) is not GitIntegrationMethod:
            raise GitIntegrationInputError("integration:method")
        if type(self.content_digest) is not str or _DIGEST.fullmatch(self.content_digest) is None:
            raise GitIntegrationInputError("integration:content_digest")


@dataclass(frozen=True, slots=True)
class GitIntegrationReceipt:
    schema_version: str
    operation_id: str
    request_content_digest: str
    task_id: str
    revision: int
    attempt: int
    dispatch_id: str
    source_branch: str
    report_commit: str
    target_branch: str
    target_head_before: str
    phase: GitIntegrationPhase
    method: GitIntegrationMethod
    outcome: GitIntegrationOutcome
    integrated_commit: str | None
    integrated_tree: str | None
    conflict_digest: str | None
    reserved_at: str
    ref_updated_at: str | None
    finalized_at: str | None
    updated_at: str
    content_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise GitIntegrationInputError("integration:receipt_schema")
        for name in ("operation_id", "task_id", "dispatch_id"):
            value = getattr(self, name)
            if type(value) is not str or _SAFE_ID.fullmatch(value) is None:
                raise GitIntegrationInputError("integration:receipt_identity")
        if type(self.revision) is not int or type(self.attempt) is not int or not 1 <= self.attempt <= 3:
            raise GitIntegrationInputError("integration:receipt_number")
        for name in ("request_content_digest", "content_digest"):
            if type(getattr(self, name)) is not str or _DIGEST.fullmatch(getattr(self, name)) is None:
                raise GitIntegrationInputError("integration:receipt_digest")
        for name in ("report_commit", "target_head_before"):
            if type(getattr(self, name)) is not str or _SHA.fullmatch(getattr(self, name)) is None:
                raise GitIntegrationInputError("integration:receipt_commit")
        for name in ("integrated_commit", "integrated_tree"):
            value = getattr(self, name)
            if value is not None and (type(value) is not str or _SHA.fullmatch(value) is None):
                raise GitIntegrationInputError("integration:receipt_optional_commit")
        if self.conflict_digest is not None and (type(self.conflict_digest) is not str or _DIGEST.fullmatch(self.conflict_digest) is None):
            raise GitIntegrationInputError("integration:receipt_conflict")
        if type(self.phase) is not GitIntegrationPhase or type(self.method) is not GitIntegrationMethod or type(self.outcome) is not GitIntegrationOutcome:
            raise GitIntegrationInputError("integration:receipt_enum")


_REQUEST_FIELDS = tuple(field.name for field in fields(GitIntegrationRequest))
_RECEIPT_FIELDS = tuple(field.name for field in fields(GitIntegrationReceipt))


def _scalar(value: object) -> object:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    return value


def _bytes(value: object, names: tuple[str, ...], include_digest: bool) -> bytes:
    lines: list[str] = []
    for name in names:
        if not include_digest and name == "content_digest":
            continue
        lines.append(f"{name}: {json.dumps(_scalar(getattr(value, name)), ensure_ascii=False, separators=(',', ':'))}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _digest(value: object, names: tuple[str, ...]) -> str:
    return "sha256:" + hashlib.sha256(_bytes(value, names, False)).hexdigest()


def with_request_digest(value: GitIntegrationRequest) -> GitIntegrationRequest:
    return replace(value, content_digest=_digest(value, _REQUEST_FIELDS))


def _with_receipt_digest(value: GitIntegrationReceipt) -> GitIntegrationReceipt:
    return replace(value, content_digest=_digest(value, _RECEIPT_FIELDS))


def _decode(data: bytes, cls: type, names: tuple[str, ...]) -> object:
    try:
        text = data.decode("utf-8")
    except UnicodeError as exc:
        raise GitIntegrationConflictError("integration:encoding") from exc
    if not text.endswith("\n") or text.endswith("\n\n"):
        raise GitIntegrationConflictError("integration:canonical")
    raw: dict[str, object] = {}
    order: list[str] = []
    try:
        for line in text[:-1].split("\n"):
            name, encoded = line.split(": ", 1)
            if name in raw:
                raise ValueError
            order.append(name)
            raw[name] = json.loads(encoded)
    except (ValueError, json.JSONDecodeError) as exc:
        raise GitIntegrationConflictError("integration:parse") from exc
    if tuple(order) != names:
        raise GitIntegrationConflictError("integration:fields")
    if cls is GitIntegrationRequest:
        raw["project_root"] = Path(str(raw["project_root"]))
        raw["source_worktree"] = Path(str(raw["source_worktree"]))
        raw["method"] = GitIntegrationMethod(str(raw["method"]))
    else:
        raw["phase"] = GitIntegrationPhase(str(raw["phase"]))
        raw["method"] = GitIntegrationMethod(str(raw["method"]))
        raw["outcome"] = GitIntegrationOutcome(str(raw["outcome"]))
    try:
        value = cls(**raw)
    except (TypeError, ValueError) as exc:
        raise GitIntegrationConflictError("integration:typed") from exc
    if _digest(value, names) != getattr(value, "content_digest") or _bytes(value, names, True) != data:
        raise GitIntegrationConflictError("integration:digest")
    return value


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or (path.exists() and (path.is_symlink() or not path.is_file())):
        raise GitIntegrationConflictError("integration:path")
    fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def _lock(root: Path):
    runtime = root / ".agentdesk" / "runtime" / "git-integration"
    for parent in (root / ".agentdesk", root / ".agentdesk" / "runtime", runtime):
        if parent.exists() and (parent.is_symlink() or not parent.is_dir()):
            raise GitIntegrationConflictError("integration:runtime_path")
    runtime.mkdir(parents=True, exist_ok=True)
    lock = runtime / ".lock"
    deadline = time.monotonic() + 5.0
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise GitIntegrationConflictError("integration:lock")
            time.sleep(0.01)
    try:
        yield runtime
    finally:
        os.close(descriptor)
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


def _git(root: Path, *args: str, input_text: str | None = None, env: dict[str, str] | None = None, ok: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args], cwd=root, input=input_text, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        shell=False, check=False,
    )
    if result.returncode not in ok:
        raise GitIntegrationConflictError("integration:git")
    return result


def _common(root: Path) -> Path:
    raw = _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    return Path(raw).resolve()


class GitIntegrationOwner:
    def __init__(self, project_root: Path) -> None:
        if not isinstance(project_root, Path) or not project_root.is_absolute() or not project_root.is_dir():
            raise GitIntegrationInputError("integration:project_root")
        self.root = project_root.resolve()

    def _paths(self, operation_id: str) -> tuple[Path, Path]:
        base = self.root / ".agentdesk" / "runtime" / "git-integration"
        return base / "requests" / f"{operation_id}.yaml", base / "receipts" / f"{operation_id}.yaml"

    def _read_request(self, path: Path) -> GitIntegrationRequest:
        return _decode(path.read_bytes(), GitIntegrationRequest, _REQUEST_FIELDS)  # type: ignore[return-value]

    def _read_receipt(self, path: Path) -> GitIntegrationReceipt:
        return _decode(path.read_bytes(), GitIntegrationReceipt, _RECEIPT_FIELDS)  # type: ignore[return-value]

    def _write_receipt(self, path: Path, receipt: GitIntegrationReceipt) -> GitIntegrationReceipt:
        receipt = _with_receipt_digest(receipt)
        _atomic_write(path, _bytes(receipt, _RECEIPT_FIELDS, True))
        return receipt

    def integrate(self, request: GitIntegrationRequest) -> GitIntegrationReceipt:
        if type(request) is not GitIntegrationRequest or request.content_digest != _digest(request, _REQUEST_FIELDS):
            raise GitIntegrationInputError("integration:request")
        if request.project_root.resolve() != self.root:
            raise GitIntegrationInputError("integration:root")
        request_path, receipt_path = self._paths(request.operation_id)
        with _lock(self.root):
            if request_path.exists():
                existing = self._read_request(request_path)
                if _bytes(existing, _REQUEST_FIELDS, True) != _bytes(request, _REQUEST_FIELDS, True):
                    raise GitIntegrationConflictError("integration:divergent")
            else:
                _atomic_write(request_path, _bytes(request, _REQUEST_FIELDS, True))
            if receipt_path.exists():
                receipt = self._read_receipt(receipt_path)
                if receipt.request_content_digest != request.content_digest:
                    raise GitIntegrationConflictError("integration:receipt_binding")
                if receipt.phase is GitIntegrationPhase.FINALIZED:
                    return receipt
            else:
                receipt = self._write_receipt(receipt_path, GitIntegrationReceipt(
                    SCHEMA_VERSION, request.operation_id, request.content_digest,
                    request.task_id, request.revision, request.attempt,
                    request.dispatch_id, request.source_branch,
                    request.report_commit, request.target_branch,
                    request.expected_target_head, GitIntegrationPhase.RESERVED,
                    request.method, GitIntegrationOutcome.RESERVED,
                    None, None, None, request.requested_at, None, None,
                    request.requested_at, "sha256:" + "0" * 64,
                ))

        source = request.source_worktree.resolve()
        if not source.is_dir() or source.is_symlink() or _common(source) != _common(self.root):
            raise GitIntegrationConflictError("integration:source")
        if _git(source, "status", "--porcelain").stdout:
            raise GitIntegrationConflictError("integration:dirty")
        for branch in (request.source_branch, request.target_branch):
            _git(self.root, "check-ref-format", "--branch", branch)
        source_head = _git(source, "rev-parse", "HEAD").stdout.strip()
        if source_head != request.report_commit:
            raise GitIntegrationConflictError("integration:source_head")
        source_ref = _git(
            self.root, "rev-parse", f"refs/heads/{request.source_branch}"
        ).stdout.strip()
        if source_ref != request.report_commit:
            raise GitIntegrationConflictError("integration:source_branch")
        target_ref = f"refs/heads/{request.target_branch}"
        target_head = _git(self.root, "rev-parse", target_ref).stdout.strip()
        worktrees = _git(self.root, "worktree", "list", "--porcelain").stdout
        if f"branch {target_ref}\n" in worktrees.replace("\r\n", "\n"):
            raise GitIntegrationConflictError("integration:target_checked_out")

        with _lock(self.root):
            receipt = self._read_receipt(receipt_path)
            if receipt.phase is GitIntegrationPhase.RESERVED:
                receipt = self._write_receipt(receipt_path, replace(
                    receipt, phase=GitIntegrationPhase.VALIDATED,
                    updated_at=request.requested_at,
                ))

        for commit in (request.base_commit, request.implementation_commit, request.report_commit, request.expected_target_head):
            _git(self.root, "cat-file", "-e", f"{commit}^{{commit}}")
        _git(self.root, "merge-base", "--is-ancestor", request.base_commit, request.report_commit)
        _git(self.root, "merge-base", "--is-ancestor", request.implementation_commit, request.report_commit)

        already = _git(self.root, "merge-base", "--is-ancestor", request.report_commit, target_head, ok=(0, 1)).returncode == 0
        integrated_commit: str
        integrated_tree: str
        outcome: GitIntegrationOutcome
        conflict_digest: str | None = None
        if already:
            integrated_commit = target_head
            integrated_tree = _git(self.root, "rev-parse", f"{target_head}^{{tree}}").stdout.strip()
            outcome = GitIntegrationOutcome.ALREADY_INTEGRATED
        else:
            if target_head != request.expected_target_head:
                raise GitIntegrationConflictError("integration:stale_target")
            fast_forward = _git(self.root, "merge-base", "--is-ancestor", target_head, request.report_commit, ok=(0, 1)).returncode == 0
            if fast_forward and request.method is GitIntegrationMethod.FAST_FORWARD:
                integrated_commit = request.report_commit
                integrated_tree = _git(self.root, "rev-parse", f"{integrated_commit}^{{tree}}").stdout.strip()
                outcome = GitIntegrationOutcome.FAST_FORWARDED
            elif request.method is GitIntegrationMethod.MERGE_TREE:
                merged = _git(self.root, "merge-tree", "--write-tree", target_head, request.report_commit, ok=(0, 1))
                if merged.returncode == 1:
                    conflict_digest = "sha256:" + hashlib.sha256((merged.stdout + merged.stderr).encode("utf-8")).hexdigest()
                    integrated_commit = ""
                    integrated_tree = ""
                    outcome = GitIntegrationOutcome.CONFLICT
                else:
                    integrated_tree = merged.stdout.splitlines()[0].strip()
                    environment = os.environ.copy()
                    environment.update({
                        "GIT_AUTHOR_NAME": "AgentDesk Integration",
                        "GIT_AUTHOR_EMAIL": "agentdesk@local.invalid",
                        "GIT_COMMITTER_NAME": "AgentDesk Integration",
                        "GIT_COMMITTER_EMAIL": "agentdesk@local.invalid",
                        "GIT_AUTHOR_DATE": request.requested_at,
                        "GIT_COMMITTER_DATE": request.requested_at,
                    })
                    integrated_commit = _git(
                        self.root, "commit-tree", integrated_tree,
                        "-p", target_head, "-p", request.report_commit,
                        input_text=request.commit_message + "\n", env=environment,
                    ).stdout.strip()
                    outcome = GitIntegrationOutcome.MERGED
            else:
                raise GitIntegrationConflictError("integration:method_not_applicable")

        with _lock(self.root):
            receipt = self._read_receipt(receipt_path)
            if receipt.phase in (GitIntegrationPhase.VALIDATED, GitIntegrationPhase.TREE_PREPARED):
                receipt = self._write_receipt(receipt_path, replace(
                    receipt, phase=GitIntegrationPhase.TREE_PREPARED,
                    outcome=outcome,
                    integrated_commit=integrated_commit or None,
                    integrated_tree=integrated_tree or None,
                    conflict_digest=conflict_digest,
                    updated_at=request.requested_at,
                ))

        ref_updated_at: str | None = None
        if outcome not in (GitIntegrationOutcome.ALREADY_INTEGRATED, GitIntegrationOutcome.CONFLICT):
            current = _git(self.root, "rev-parse", target_ref).stdout.strip()
            if current == integrated_commit:
                ref_updated_at = request.requested_at
            elif current == request.expected_target_head:
                update = _git(
                    self.root, "update-ref", target_ref, integrated_commit,
                    request.expected_target_head, ok=(0, 1, 128),
                )
                if update.returncode == 0:
                    ref_updated_at = request.requested_at
                else:
                    after = _git(self.root, "rev-parse", target_ref).stdout.strip()
                    if after == integrated_commit:
                        ref_updated_at = request.requested_at
                    else:
                        raise GitIntegrationRecoveryRequiredError(
                            "integration:ref_unknown"
                        )
            else:
                raise GitIntegrationRecoveryRequiredError("integration:ref_unknown")

        with _lock(self.root):
            receipt = self._read_receipt(receipt_path)
            receipt = self._write_receipt(receipt_path, replace(
                receipt, phase=GitIntegrationPhase.REF_UPDATED,
                ref_updated_at=ref_updated_at,
                updated_at=request.requested_at,
            ))
            receipt = self._write_receipt(receipt_path, replace(
                receipt, phase=GitIntegrationPhase.FINALIZED,
                outcome=GitIntegrationOutcome.FINALIZED if outcome is not GitIntegrationOutcome.CONFLICT else outcome,
                finalized_at=request.requested_at,
                updated_at=request.requested_at,
            ))
            return receipt


__all__ = [
    "SCHEMA_VERSION", "GitIntegrationMethod", "GitIntegrationPhase",
    "GitIntegrationOutcome", "GitIntegrationRequest", "GitIntegrationReceipt",
    "GitIntegrationOwner", "GitIntegrationError", "GitIntegrationInputError",
    "GitIntegrationConflictError", "GitIntegrationRecoveryRequiredError",
    "with_request_digest",
]
