"""TC-13.25d adversarial verification for managed worktree lifecycle."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

SCRIPT_ROOT = Path(__file__).resolve().parents[1] / "skills" / "agentdesk" / "scripts"
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

import worktree_lifecycle_manager as manager_module
from dispatch_supervisor_evidence import (
    SCHEMA_VERSION as SUPERVISOR_SCHEMA_VERSION,
    DispatchProcessReceipt,
    ProcessLiveness,
)
from worktree_lifecycle_manager import (
    WorktreeLifecycleGitError,
    WorktreeLifecycleManager,
    WorktreeLifecycleReleaseRefusedError,
)
from worktree_lifecycle_store import (
    SCHEMA_VERSION,
    WorktreeLifecycleAction,
    WorktreeLifecycleOutcome,
    WorktreeLifecycleRequest,
    derive_worktree_id,
    expected_branch,
    expected_worktree_path,
)

STAMP = "2026-08-02T02:00:00.000000Z"


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args], cwd=root, check=check, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
    )


class WorktreeLifecycleAdversarialTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        _git(self.root, "init", "-b", "main")
        _git(self.root, "config", "user.email", "agentdesk@example.invalid")
        _git(self.root, "config", "user.name", "AgentDesk Test")
        (self.root / "README.md").write_text("base\n", encoding="utf-8")
        _git(self.root, "add", "README.md")
        _git(self.root, "commit", "-m", "initial")
        self.head = _git(self.root, "rev-parse", "HEAD").stdout.decode().strip()
        self.common = str(Path(_git(
            self.root, "rev-parse", "--path-format=absolute", "--git-common-dir"
        ).stdout.decode().strip()).resolve())
        self.branch = expected_branch("TC-025", 1, 1, "DSP-1")
        self.worktree_id = derive_worktree_id(
            self.common, str(self.root), self.branch, self.head,
            "TC-025", 1, 1, "DSP-1",
        )
        self.path = expected_worktree_path(str(self.root), self.worktree_id)
        self.request = WorktreeLifecycleRequest(
            SCHEMA_VERSION, "OP-CREATE", WorktreeLifecycleAction.CREATE,
            self.common, str(self.root), self.path, self.branch, self.head,
            "TC-025", 1, 1, "DSP-1", self.worktree_id, STAMP,
        )
        self.manager = WorktreeLifecycleManager(self.root)

    def tearDown(self) -> None:
        subprocess.run(
            ["git", "worktree", "remove", "--force", self.path], cwd=self.root,
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=10,
        )
        self.temp.cleanup()

    def _release_request(self) -> WorktreeLifecycleRequest:
        return replace(
            self.request, operation_id="OP-RELEASE",
            action=WorktreeLifecycleAction.RELEASE,
            requested_at="2026-08-02T02:00:01.000000Z",
        )

    def _receipt(self) -> DispatchProcessReceipt:
        return DispatchProcessReceipt(
            SUPERVISOR_SCHEMA_VERSION, "TC-025", 1, 1, "DSP-1", 1,
            "INSTANCE-1", "GENERATION-1", "win32", "BOOT-1",
            "SUPERVISOR_READY", 100, "creator", 101, "supervisor",
            None, None, None, STAMP,
        )

    def test_identity_substitution_is_rejected_before_git_side_effect(self) -> None:
        mutations = (
            {"task_id": "TC-026"}, {"revision": 2}, {"attempt": 2},
            {"dispatch_id": "DSP-2"}, {"base_commit": "0" * 40},
        )
        for mutation in mutations:
            with self.subTest(mutation=tuple(mutation)):
                with self.assertRaises(Exception):
                    self.manager.create(replace(self.request, **mutation))
                self.assertFalse(Path(self.path).exists())

    def test_path_escape_and_preexisting_path_are_rejected(self) -> None:
        escaped = replace(self.request, worktree_path=str(self.root.parent / "escape"))
        with self.assertRaises(Exception):
            self.manager.create(escaped)
        Path(self.path).mkdir(parents=True)
        with self.assertRaises(Exception):
            self.manager.create(self.request)

    def test_dirty_worktree_release_is_refused_without_remove(self) -> None:
        self.manager.create(self.request)
        Path(self.path, "untracked.txt").write_text("evidence", encoding="utf-8")
        with patch.object(self.manager, "_verify_release_ownership"):
            with self.assertRaises(WorktreeLifecycleReleaseRefusedError):
                self.manager.release(self._release_request())
        self.assertTrue(Path(self.path).is_dir())

    def test_pid_reuse_or_unknown_liveness_fails_closed(self) -> None:
        self.manager.create(self.request)
        empty = {"leases": {}}
        with (
            patch.object(manager_module, "read_worker_slot_leases", return_value=empty),
            patch.object(manager_module, "read_dispatch_receipt", return_value=self._receipt()),
            patch.object(manager_module, "read_dispatch_tombstone", return_value=None),
            patch.object(manager_module, "probe_dispatch_process_tree", return_value=ProcessLiveness.UNKNOWN),
        ):
            with self.assertRaises(WorktreeLifecycleReleaseRefusedError):
                self.manager.release(self._release_request())
        self.assertTrue(Path(self.path).is_dir())

    def test_live_lease_refuses_release_before_process_probe(self) -> None:
        self.manager.create(self.request)
        leases = {"leases": {"basic_agent-1": {
            "holder_dispatch_id": "DSP-1", "canonical_worktree": self.path,
        }}}
        with (
            patch.object(manager_module, "read_worker_slot_leases", return_value=leases),
            patch.object(manager_module, "read_dispatch_receipt") as receipt_read,
        ):
            with self.assertRaises(WorktreeLifecycleReleaseRefusedError):
                self.manager.release(self._release_request())
        receipt_read.assert_not_called()

    def test_concurrent_identical_create_has_one_winner(self) -> None:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(lambda _: self.manager.create(self.request), (1, 2)))
        self.assertEqual(sum(r.outcome is WorktreeLifecycleOutcome.READY for r in results), 1)
        self.assertEqual(sum(r.outcome is WorktreeLifecycleOutcome.REPLAYED for r in results), 1)

    def test_crash_after_git_add_replays_without_second_worktree(self) -> None:
        original = self.manager.store.advance_record
        failed = False

        def interrupt(*args, **kwargs):
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("crash")
            return original(*args, **kwargs)

        with patch.object(self.manager.store, "advance_record", side_effect=interrupt):
            with self.assertRaises(RuntimeError):
                self.manager.create(self.request)
        result = WorktreeLifecycleManager(self.root).create(self.request)
        self.assertEqual(result.outcome, WorktreeLifecycleOutcome.READY)
        listed = _git(self.root, "worktree", "list", "--porcelain", "-z").stdout.decode()
        listed_paths = tuple(
            Path(field.removeprefix("worktree ")).resolve()
            for field in listed.split("\0")
            if field.startswith("worktree ")
        )
        self.assertEqual(
            sum(path == Path(self.path).resolve() for path in listed_paths),
            1,
        )

    def test_malformed_git_output_and_permission_error_are_typed(self) -> None:
        with patch.object(manager_module, "_run_git", return_value=b"not-a-path\nsecond-line"):
            with self.assertRaises(WorktreeLifecycleGitError):
                self.manager.create(self.request)
        with patch.object(manager_module.subprocess, "run", side_effect=PermissionError("secret")):
            with self.assertRaises(WorktreeLifecycleGitError) as caught:
                manager_module._run_git(self.root, ("status",), 1.0)
        self.assertNotIn("secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
