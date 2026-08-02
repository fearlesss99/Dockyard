"""TC-13.25c real temporary-Git tests for WorktreeLifecycleManager."""

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
    WorktreePhase,
    derive_worktree_id,
    expected_branch,
    expected_worktree_path,
    parse_worktree_inventory,
)

STAMP = "2026-08-02T01:00:00.000000Z"
STAMP_2 = "2026-08-02T01:00:01.000000Z"


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args], cwd=root, check=check,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=10,
    )


class WorktreeLifecycleManagerTests(unittest.TestCase):
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
        self.common_dir = str(Path(
            _git(self.root, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.decode().strip()
        ).resolve())
        self.branch = expected_branch("TC-025", 1, 1, "DSP-1")
        self.worktree_id = derive_worktree_id(
            self.common_dir, str(self.root), self.branch, self.head,
            "TC-025", 1, 1, "DSP-1",
        )
        self.worktree_path = expected_worktree_path(str(self.root), self.worktree_id)
        self.create_request = WorktreeLifecycleRequest(
            SCHEMA_VERSION, "OP-CREATE", WorktreeLifecycleAction.CREATE,
            self.common_dir, str(self.root), self.worktree_path, self.branch,
            self.head, "TC-025", 1, 1, "DSP-1", self.worktree_id, STAMP,
        )
        self.manager = WorktreeLifecycleManager(self.root)

    def tearDown(self) -> None:
        subprocess.run(
            ["git", "worktree", "remove", "--force", self.worktree_path],
            cwd=self.root, check=False, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=10,
        )
        self.temp.cleanup()

    def _release_request(self, timestamp: str = STAMP_2) -> WorktreeLifecycleRequest:
        return replace(
            self.create_request,
            operation_id="OP-RELEASE",
            action=WorktreeLifecycleAction.RELEASE,
            requested_at=timestamp,
        )

    def _inventory(self):
        return parse_worktree_inventory(
            _git(self.root, "worktree", "list", "--porcelain", "-z").stdout,
            self.common_dir,
        )

    def _release(self, request: WorktreeLifecycleRequest):
        with patch.object(self.manager, "_verify_release_ownership"):
            return self.manager.release(request)

    def test_create_materializes_ready_worktree_after_durable_reservation(self) -> None:
        result = self.manager.create(self.create_request)
        self.assertEqual(result.outcome, WorktreeLifecycleOutcome.READY)
        self.assertEqual(result.phase, WorktreePhase.READY)
        path = Path(self.worktree_path)
        self.assertTrue((path / "README.md").is_file())
        self.assertEqual(_git(path, "branch", "--show-current").stdout.decode().strip(), self.branch)
        self.assertEqual(_git(path, "rev-parse", "HEAD").stdout.decode().strip(), self.head)
        self.assertEqual(self.manager.store.read_record(self.worktree_id).phase, WorktreePhase.READY)

    def test_reservation_and_creating_record_precede_git_add(self) -> None:
        original = manager_module._run_git
        observed = {"checked": False}

        def wrapped(cwd: Path, arguments: tuple[str, ...], timeout: float) -> bytes:
            if arguments[:2] == ("worktree", "add"):
                reservation = self.manager.store.read_reservation(self.worktree_id)
                record = self.manager.store.read_record(self.worktree_id)
                self.assertEqual(reservation.phase, WorktreePhase.RESERVED)
                self.assertEqual(record.phase, WorktreePhase.CREATING)
                observed["checked"] = True
            return original(cwd, arguments, timeout)

        with patch.object(manager_module, "_run_git", side_effect=wrapped):
            self.manager.create(self.create_request)
        self.assertTrue(observed["checked"])

    def test_create_replay_does_not_create_second_worktree(self) -> None:
        first = self.manager.create(self.create_request)
        second = self.manager.create(self.create_request)
        self.assertEqual(first.worktree_id, second.worktree_id)
        self.assertEqual(second.outcome, WorktreeLifecycleOutcome.REPLAYED)
        matches = [entry for entry in self._inventory() if entry.worktree_path == self.worktree_path]
        self.assertEqual(len(matches), 1)

    def test_crash_after_git_add_replays_by_adopting_exact_inventory(self) -> None:
        original_advance = self.manager.store.advance_record
        failed = {"done": False}

        def fail_ready(*args, **kwargs):
            if not failed["done"]:
                failed["done"] = True
                raise RuntimeError("simulated crash")
            return original_advance(*args, **kwargs)

        with patch.object(self.manager.store, "advance_record", side_effect=fail_ready):
            with self.assertRaises(RuntimeError):
                self.manager.create(self.create_request)
        self.assertTrue(Path(self.worktree_path).is_dir())
        replay_manager = WorktreeLifecycleManager(self.root)
        result = replay_manager.create(self.create_request)
        self.assertEqual(result.phase, WorktreePhase.READY)

    def test_concurrent_create_has_one_durable_identity(self) -> None:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(lambda _: self.manager.create(self.create_request), (1, 2)))
        self.assertEqual({result.worktree_id for result in results}, {self.worktree_id})
        self.assertEqual(sum(result.outcome is WorktreeLifecycleOutcome.READY for result in results), 1)
        self.assertEqual(sum(result.outcome is WorktreeLifecycleOutcome.REPLAYED for result in results), 1)

    def test_release_clean_worktree_and_replay(self) -> None:
        self.manager.create(self.create_request)
        released = self._release(self._release_request())
        self.assertEqual(released.outcome, WorktreeLifecycleOutcome.RELEASED)
        self.assertEqual(released.phase, WorktreePhase.RELEASED)
        self.assertFalse(Path(self.worktree_path).exists())
        replay = self._release(self._release_request("2026-08-02T01:00:02.000000Z"))
        self.assertEqual(replay.outcome, WorktreeLifecycleOutcome.REPLAYED)

    def test_release_refuses_dirty_and_untracked_worktree(self) -> None:
        self.manager.create(self.create_request)
        Path(self.worktree_path, "dirty.txt").write_text("dirty", encoding="utf-8")
        with self.assertRaises(WorktreeLifecycleReleaseRefusedError):
            self._release(self._release_request())
        self.assertTrue(Path(self.worktree_path).exists())
        self.assertEqual(self.manager.store.read_record(self.worktree_id).phase, WorktreePhase.READY)

    def test_release_refuses_divergent_head_even_when_clean(self) -> None:
        self.manager.create(self.create_request)
        path = Path(self.worktree_path)
        (path / "README.md").write_text("changed\n", encoding="utf-8")
        _git(path, "add", "README.md")
        _git(path, "commit", "-m", "diverge")
        with self.assertRaises(WorktreeLifecycleReleaseRefusedError):
            self._release(self._release_request())
        self.assertTrue(path.exists())

    def test_release_refuses_missing_process_evidence(self) -> None:
        self.manager.create(self.create_request)
        with self.assertRaises(WorktreeLifecycleReleaseRefusedError):
            self.manager.release(self._release_request())

    def test_wrong_identity_is_rejected_before_git_side_effect(self) -> None:
        bad = replace(self.create_request, branch="agentdesk/TC-025/r1/a1/DSP-WRONG")
        with self.assertRaises(Exception):
            self.manager.create(bad)
        self.assertFalse(Path(self.worktree_path).exists())

    def test_nonzero_git_error_does_not_leak_output(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["git"], returncode=1, stdout=b"secret-output", stderr=b"secret-error"
        )
        with patch.object(manager_module.subprocess, "run", return_value=completed):
            with self.assertRaises(WorktreeLifecycleGitError) as caught:
                manager_module._run_git(self.root, ("rev-parse", "HEAD"), 1.0)
        self.assertNotIn("secret", str(caught.exception))

    def test_source_uses_argv_no_shell_and_forbids_destructive_git(self) -> None:
        source = (SCRIPT_ROOT / "worktree_lifecycle_manager.py").read_text(encoding="utf-8")
        self.assertIn("shell=False", source)
        self.assertIn("timeout=timeout_seconds", source)
        self.assertNotIn("shell=True", source)
        for forbidden in ('("reset",', '("clean",', '("checkout",', '("worktree", "prune"', '"--force"'):
            self.assertNotIn(forbidden, source)
        self.assertNotIn("ControlPlaneTransitionService", source)
        self.assertNotIn("WorkerSlotLease", source)


if __name__ == "__main__":
    unittest.main()
