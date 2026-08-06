"""Directed real-Git tests for TC-13.29l.5h.2."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields, replace
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "agentdesk" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from git_integration_owner import (
    SCHEMA_VERSION,
    GitIntegrationConflictError,
    GitIntegrationMethod,
    GitIntegrationOutcome,
    GitIntegrationOwner,
    GitIntegrationPhase,
    GitIntegrationRequest,
    GitIntegrationReceipt,
    with_request_digest,
)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


class GitIntegrationOwnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="git-integration-"))
        _git(self.root, "init", "-b", "controller")
        _git(self.root, "config", "user.name", "Test User")
        _git(self.root, "config", "user.email", "test@example.invalid")
        (self.root / "shared.txt").write_text("base\n", encoding="utf-8")
        _git(self.root, "add", "shared.txt")
        _git(self.root, "commit", "-m", "base")
        self.base = _git(self.root, "rev-parse", "HEAD")
        _git(self.root, "branch", "integration", self.base)
        _git(self.root, "branch", "feature", self.base)
        self.source = self.root.parent / f"{self.root.name}-source"
        _git(self.root, "worktree", "add", str(self.source), "feature")
        (self.source / "implementation.txt").write_text("implementation\n", encoding="utf-8")
        _git(self.source, "add", "implementation.txt")
        _git(self.source, "commit", "-m", "implementation")
        self.implementation = _git(self.source, "rev-parse", "HEAD")
        (self.source / "report.md").write_text("report\n", encoding="utf-8")
        _git(self.source, "add", "report.md")
        _git(self.source, "commit", "-m", "report")
        self.report = _git(self.source, "rev-parse", "HEAD")
        self.owner = GitIntegrationOwner(self.root.resolve())

    def tearDown(self) -> None:
        shutil.rmtree(self.source, ignore_errors=True)
        shutil.rmtree(self.root, ignore_errors=True)

    def request(
        self,
        *,
        operation_id: str = "OP-INTEGRATE-001",
        target_branch: str = "integration",
        expected_target_head: str | None = None,
        method: GitIntegrationMethod = GitIntegrationMethod.FAST_FORWARD,
    ) -> GitIntegrationRequest:
        request = GitIntegrationRequest(
            SCHEMA_VERSION, operation_id, self.root.resolve(), "TC-001", 1, 1,
            "DSP-001", self.source.resolve(), "feature", self.base,
            self.implementation, self.report, target_branch,
            expected_target_head or _git(self.root, "rev-parse", f"refs/heads/{target_branch}"),
            method, "integrate TC-001", "2026-08-03T10:00:00Z",
            "sha256:" + "0" * 64,
        )
        return with_request_digest(request)

    def diverge_target(self, *, conflict: bool = False) -> str:
        target = self.root.parent / f"{self.root.name}-target"
        _git(self.root, "worktree", "add", str(target), "integration")
        path = target / ("shared.txt" if conflict else "target.txt")
        path.write_text("target change\n", encoding="utf-8")
        _git(target, "add", path.name)
        _git(target, "commit", "-m", "target change")
        head = _git(target, "rev-parse", "HEAD")
        _git(self.root, "worktree", "remove", str(target))
        return head

    def test_01_types_fields_and_fast_forward(self) -> None:
        self.assertEqual(len(fields(GitIntegrationRequest)), 18)
        self.assertEqual(len(fields(GitIntegrationReceipt)), 22)
        request = self.request()
        result = self.owner.integrate(request)
        self.assertIs(result.phase, GitIntegrationPhase.FINALIZED)
        self.assertIs(result.outcome, GitIntegrationOutcome.FINALIZED)
        self.assertEqual(result.integrated_commit, self.report)
        self.assertEqual(_git(self.root, "rev-parse", "refs/heads/integration"), self.report)
        self.assertEqual(_git(self.root, "rev-parse", "refs/heads/controller"), self.base)

    def test_02_byte_exact_replay_does_not_move_ref_again(self) -> None:
        request = self.request()
        first = self.owner.integrate(request)
        second = self.owner.integrate(request)
        self.assertEqual(first, second)
        self.assertEqual(_git(self.root, "reflog", "show", "--format=%H", "integration").splitlines().count(self.report), 1)

    def test_03_divergent_same_operation_rejected(self) -> None:
        request = self.request()
        self.owner.integrate(request)
        divergent = with_request_digest(replace(
            request, commit_message="different message",
            content_digest="sha256:" + "0" * 64,
        ))
        with self.assertRaises(GitIntegrationConflictError):
            self.owner.integrate(divergent)

    def test_04_attempt_four_rejected_before_files(self) -> None:
        request = self.request()
        with self.assertRaises(Exception):
            replace(request, attempt=4)
        self.assertFalse((self.root / ".agentdesk").exists())

    def test_05_checked_out_target_is_rejected_without_mutation(self) -> None:
        request = self.request(
            target_branch="controller",
            expected_target_head=self.base,
        )
        with self.assertRaises(GitIntegrationConflictError):
            self.owner.integrate(request)
        self.assertEqual(_git(self.root, "rev-parse", "HEAD"), self.base)
        self.assertEqual(_git(self.root, "status", "--porcelain"), "?? .agentdesk/")

    def test_06_non_conflicting_merge_tree_creates_two_parent_commit(self) -> None:
        target_head = self.diverge_target()
        request = self.request(
            expected_target_head=target_head,
            method=GitIntegrationMethod.MERGE_TREE,
        )
        result = self.owner.integrate(request)
        self.assertIs(result.phase, GitIntegrationPhase.FINALIZED)
        parents = _git(self.root, "show", "-s", "--format=%P", result.integrated_commit).split()
        self.assertEqual(parents, [target_head, self.report])
        self.assertEqual(_git(self.root, "rev-parse", "refs/heads/integration"), result.integrated_commit)

    def test_07_merge_conflict_writes_receipt_but_not_ref(self) -> None:
        (self.source / "shared.txt").write_text("source change\n", encoding="utf-8")
        _git(self.source, "add", "shared.txt")
        _git(self.source, "commit", "-m", "source conflict")
        self.report = _git(self.source, "rev-parse", "HEAD")
        target_head = self.diverge_target(conflict=True)
        request = self.request(
            expected_target_head=target_head,
            method=GitIntegrationMethod.MERGE_TREE,
        )
        result = self.owner.integrate(request)
        self.assertIs(result.outcome, GitIntegrationOutcome.CONFLICT)
        self.assertIsNone(result.integrated_commit)
        self.assertIsNotNone(result.conflict_digest)
        self.assertEqual(_git(self.root, "rev-parse", "refs/heads/integration"), target_head)

    def test_08_stale_target_cas_rejected_without_ref_update(self) -> None:
        target_head = self.diverge_target()
        request = self.request(expected_target_head=self.base)
        with self.assertRaises(GitIntegrationConflictError):
            self.owner.integrate(request)
        self.assertEqual(_git(self.root, "rev-parse", "refs/heads/integration"), target_head)

    def test_09_source_branch_substitution_rejected(self) -> None:
        request = self.request()
        bad = with_request_digest(replace(
            request, source_branch="integration",
            content_digest="sha256:" + "0" * 64,
        ))
        with self.assertRaises(GitIntegrationConflictError):
            self.owner.integrate(bad)
        self.assertEqual(_git(self.root, "rev-parse", "refs/heads/integration"), self.base)

    def test_10_concurrent_identical_operations_have_one_ref_result(self) -> None:
        request = self.request()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(lambda _: self.owner.integrate(request), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(_git(self.root, "rev-parse", "refs/heads/integration"), self.report)
        self.assertEqual(_git(self.root, "reflog", "show", "--format=%H", "integration").splitlines().count(self.report), 1)

    def test_11_tampered_receipt_is_fail_closed_on_replay(self) -> None:
        request = self.request()
        self.owner.integrate(request)
        receipt = self.root / ".agentdesk" / "runtime" / "git-integration" / "receipts" / f"{request.operation_id}.yaml"
        data = receipt.read_bytes()
        receipt.write_bytes(data.replace(b"phase: \"FINALIZED\"", b"phase: \"REF_UPDATED\""))
        with self.assertRaises(GitIntegrationConflictError):
            self.owner.integrate(request)


if __name__ == "__main__":
    unittest.main()
