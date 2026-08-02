"""TC-13.25b durable WorktreeLifecycle Store and pure reconciliation tests."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields, replace
from pathlib import Path
from unittest.mock import patch

SCRIPT_ROOT = Path(__file__).resolve().parents[1] / "skills" / "agentdesk" / "scripts"
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

import worktree_lifecycle_store as store_module
from worktree_lifecycle_store import (
    SCHEMA_VERSION,
    WorktreeInventoryEntry,
    WorktreeLifecycleAction,
    WorktreeLifecycleConflictError,
    WorktreeLifecycleInputError,
    WorktreeLifecyclePermissionError,
    WorktreeLifecycleRequest,
    WorktreeLifecycleSecurityError,
    WorktreeLifecycleStore,
    WorktreeLifecycleTransitionError,
    WorktreePhase,
    WorktreeReconciliationAction,
    WorktreeReconciliationDecision,
    WorktreeRecord,
    WorktreeReservation,
    decode_decision,
    decode_record,
    decode_reservation,
    derive_worktree_id,
    encode_decision,
    encode_record,
    encode_reservation,
    expected_branch,
    expected_worktree_path,
    inventory_digest,
    make_record,
    parse_worktree_inventory,
    reconcile_worktree,
)

STAMP = "2026-08-02T00:00:00.000000Z"
STAMP_2 = "2026-08-02T00:00:01.000000Z"
STAMP_3 = "2026-08-02T00:00:02.000000Z"


def _run_git(root: Path, *args: str, binary: bool = False):
    return subprocess.run(
        ["git", *args], cwd=root, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=not binary, timeout=10,
    ).stdout


class WorktreeLifecycleStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        _run_git(self.root, "init", "-b", "main")
        _run_git(self.root, "config", "user.email", "agentdesk@example.invalid")
        _run_git(self.root, "config", "user.name", "AgentDesk Test")
        (self.root / "README.md").write_text("test\n", encoding="utf-8")
        _run_git(self.root, "add", "README.md")
        _run_git(self.root, "commit", "-m", "initial")
        self.head = _run_git(self.root, "rev-parse", "HEAD").strip()
        self.common_dir = str(Path(_run_git(
            self.root, "rev-parse", "--path-format=absolute", "--git-common-dir"
        ).strip()).resolve())
        self.branch = expected_branch("TC-025", 1, 1, "DSP-1")
        self.worktree_id = derive_worktree_id(
            self.common_dir, str(self.root), self.branch, self.head,
            "TC-025", 1, 1, "DSP-1",
        )
        self.worktree_path = expected_worktree_path(str(self.root), self.worktree_id)
        self.request = WorktreeLifecycleRequest(
            SCHEMA_VERSION, "OP-1", WorktreeLifecycleAction.CREATE,
            self.common_dir, str(self.root), self.worktree_path, self.branch,
            self.head, "TC-025", 1, 1, "DSP-1", self.worktree_id, STAMP,
        )
        self.store = WorktreeLifecycleStore(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _inventory(self) -> tuple[WorktreeInventoryEntry, ...]:
        raw = _run_git(
            self.root, "worktree", "list", "--porcelain", "-z", binary=True
        )
        return parse_worktree_inventory(raw, self.common_dir)

    def _reservation(self) -> WorktreeReservation:
        return self.store.reserve(self.request)

    def _creating_record(self) -> WorktreeRecord:
        reservation = self._reservation()
        return self.store.create_record(
            make_record(
                reservation, WorktreePhase.CREATING,
                inventory_digest(self._inventory()),
            )
        )

    def test_public_types_are_frozen_slotted_and_exact(self) -> None:
        self.assertEqual(len(fields(WorktreeReservation)), 15)
        self.assertEqual(len(fields(WorktreeRecord)), 19)
        self.assertEqual(len(fields(WorktreeLifecycleRequest)), 14)
        self.assertEqual(len(fields(WorktreeReconciliationDecision)), 10)
        for value_type in (
            WorktreeInventoryEntry, WorktreeReservation, WorktreeRecord,
            WorktreeLifecycleRequest, WorktreeReconciliationDecision,
        ):
            self.assertTrue(value_type.__dataclass_params__.frozen)
            self.assertIn("__slots__", value_type.__dict__)

    def test_identity_branch_and_path_derivation_are_stable(self) -> None:
        self.assertEqual(
            derive_worktree_id(
                self.common_dir, str(self.root), self.branch, self.head,
                "TC-025", 1, 1, "DSP-1",
            ),
            self.worktree_id,
        )
        self.assertTrue(self.worktree_path.endswith(self.worktree_id))
        self.assertEqual(self.branch, "agentdesk/TC-025/r1/a1/DSP-1")

    def test_bool_as_int_attempt_four_and_malicious_object_are_rejected(self) -> None:
        with self.assertRaises(WorktreeLifecycleInputError):
            replace(self.request, revision=True)
        with self.assertRaises(WorktreeLifecycleInputError):
            replace(self.request, attempt=4)

        class Malicious:
            def __str__(self) -> str:
                raise AssertionError("must not stringify")

        with self.assertRaises(WorktreeLifecycleInputError):
            replace(self.request, task_id=Malicious())

    def test_reservation_create_read_and_byte_exact_replay(self) -> None:
        first = self._reservation()
        path = self.root / "docs" / "pm" / "worktree-lifecycle" / "reservations" / f"{self.worktree_id}.yaml"
        first_bytes = path.read_bytes()
        second = self._reservation()
        self.assertEqual(first, second)
        self.assertEqual(path.read_bytes(), first_bytes)
        self.assertEqual(self.store.read_reservation(self.worktree_id), first)

    def test_divergent_reservation_replay_is_typed_conflict(self) -> None:
        self._reservation()
        divergent = replace(self.request, requested_at=STAMP_2)
        with self.assertRaises(WorktreeLifecycleConflictError):
            self.store.reserve(divergent)

    def test_reservation_codec_is_canonical_and_strict(self) -> None:
        value = self._reservation()
        encoded = encode_reservation(value)
        self.assertEqual(decode_reservation(encoded), value)
        self.assertTrue(encoded.endswith(b"\n"))
        self.assertNotIn(b"\r", encoded)
        with self.assertRaises(Exception):
            decode_reservation(b"\xef\xbb\xbf" + encoded)
        with self.assertRaises(Exception):
            decode_reservation(encoded + b"extra: \"x\"\n")
        with self.assertRaises(Exception):
            decode_reservation(encoded.replace(b"sha256:", b"sha256:0", 1))

    def test_real_git_porcelain_inventory_projection(self) -> None:
        entries = self._inventory()
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(Path(entry.worktree_path), self.root)
        self.assertEqual(entry.head_commit, self.head)
        self.assertEqual(entry.branch, "main")
        self.assertFalse(entry.is_detached)
        self.assertTrue(inventory_digest(entries).startswith("sha256:"))

    def test_inventory_rejects_duplicates_unknown_fields_and_case_collisions(self) -> None:
        path = str(self.root)
        duplicate = (
            f"worktree {path}\0worktree {path}\0HEAD {self.head}\0branch refs/heads/main\0\0"
        ).encode()
        with self.assertRaises(Exception):
            parse_worktree_inventory(duplicate, self.common_dir)
        unknown = (
            f"worktree {path}\0HEAD {self.head}\0branch refs/heads/main\0mystery x\0\0"
        ).encode()
        with self.assertRaises(Exception):
            parse_worktree_inventory(unknown, self.common_dir)
        collision = (
            f"worktree {path}\0HEAD {self.head}\0branch refs/heads/main\0\0"
            f"worktree {path.upper()}\0HEAD {self.head}\0branch refs/heads/other\0\0"
        ).encode()
        if os.name == "nt":
            with self.assertRaises(WorktreeLifecycleConflictError):
                parse_worktree_inventory(collision, self.common_dir)

    def test_record_codec_and_forward_only_phases(self) -> None:
        creating = self._creating_record()
        self.assertEqual(decode_record(encode_record(creating)), creating)
        digest = inventory_digest(self._inventory())
        ready = self.store.advance_record(
            self.worktree_id, WorktreePhase.CREATING, WorktreePhase.READY,
            digest, STAMP_2, self.head,
        )
        self.assertEqual(ready.phase, WorktreePhase.READY)
        releasing = self.store.advance_record(
            self.worktree_id, WorktreePhase.READY, WorktreePhase.RELEASING,
            digest, STAMP_3,
        )
        self.assertEqual(releasing.phase, WorktreePhase.RELEASING)
        released = self.store.advance_record(
            self.worktree_id, WorktreePhase.RELEASING, WorktreePhase.RELEASED,
            digest, "2026-08-02T00:00:03.000000Z",
        )
        self.assertEqual(released.phase, WorktreePhase.RELEASED)
        with self.assertRaises(WorktreeLifecycleTransitionError):
            self.store.advance_record(
                self.worktree_id, WorktreePhase.RELEASED, WorktreePhase.READY,
                digest, "2026-08-02T00:00:04.000000Z",
            )

    def test_phase_skip_is_rejected(self) -> None:
        self._creating_record()
        with self.assertRaises(WorktreeLifecycleTransitionError):
            self.store.advance_record(
                self.worktree_id, WorktreePhase.CREATING, WorktreePhase.RELEASING,
                inventory_digest(self._inventory()), STAMP_2,
            )

    def test_reconciliation_create_adopt_ready_and_fail_closed(self) -> None:
        reservation = self._reservation()
        decision = reconcile_worktree(
            self.worktree_id, reservation, None, tuple(), None
        )
        self.assertEqual(decision.action, WorktreeReconciliationAction.RESUME_CREATE)
        entry = WorktreeInventoryEntry(
            self.common_dir, self.worktree_path, self.head, self.branch,
            False, False, False, False,
        )
        adopted = reconcile_worktree(
            self.worktree_id, reservation, None, (entry,), None
        )
        self.assertEqual(adopted.action, WorktreeReconciliationAction.ADOPT_READY)
        divergent = replace(entry, head_commit="f" * 40)
        closed = reconcile_worktree(
            self.worktree_id, reservation, None, (divergent,), None
        )
        self.assertEqual(closed.action, WorktreeReconciliationAction.FAIL_CLOSED)

    def test_reconciliation_ready_release_and_unknown(self) -> None:
        creating = self._creating_record()
        digest = inventory_digest(self._inventory())
        ready = replace(
            creating, phase=WorktreePhase.READY, created_at=STAMP_2,
            content_digest="sha256:" + "0" * 64,
        )
        ready = store_module._with_record_digest(ready)
        entry = WorktreeInventoryEntry(
            self.common_dir, self.worktree_path, self.head, self.branch,
            False, False, False, False,
        )
        no_op = reconcile_worktree(
            self.worktree_id, self.store.read_reservation(self.worktree_id),
            ready, (entry,), True,
        )
        self.assertEqual(no_op.action, WorktreeReconciliationAction.NO_OP)
        unknown = reconcile_worktree(
            self.worktree_id, self.store.read_reservation(self.worktree_id),
            ready, (entry,), None,
        )
        self.assertEqual(unknown.action, WorktreeReconciliationAction.FAIL_CLOSED)
        releasing = store_module._with_record_digest(
            replace(ready, phase=WorktreePhase.RELEASING, release_requested_at=STAMP_3)
        )
        resume = reconcile_worktree(
            self.worktree_id, self.store.read_reservation(self.worktree_id),
            releasing, (entry,), True,
        )
        self.assertEqual(resume.action, WorktreeReconciliationAction.RESUME_RELEASE)
        absent = reconcile_worktree(
            self.worktree_id, self.store.read_reservation(self.worktree_id),
            releasing, tuple(), True,
        )
        self.assertEqual(absent.action, WorktreeReconciliationAction.NO_OP)
        self.assertEqual(digest, ready.inventory_digest)

    def test_decision_codec_is_canonical(self) -> None:
        decision = reconcile_worktree(self.worktree_id, None, None, tuple(), None)
        self.assertEqual(decode_decision(encode_decision(decision)), decision)

    def test_extra_file_and_unc_paths_are_rejected(self) -> None:
        self._reservation()
        directory = self.root / "docs" / "pm" / "worktree-lifecycle" / "reservations"
        (directory / "unexpected.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(WorktreeLifecycleSecurityError):
            self.store.read_reservation(self.worktree_id)
        with self.assertRaises(WorktreeLifecycleSecurityError):
            replace(self.request, worktree_path=r"\\server\share\WT-1")

    def test_symlink_evidence_is_rejected_when_supported(self) -> None:
        self._reservation()
        path = self.root / "docs" / "pm" / "worktree-lifecycle" / "reservations" / f"{self.worktree_id}.yaml"
        target = path.with_suffix(".target")
        original = path.read_bytes()
        path.unlink()
        target.write_bytes(original)
        try:
            path.symlink_to(target)
        except OSError:
            self.skipTest("symlink creation is unavailable")
        with self.assertRaises(WorktreeLifecycleSecurityError):
            self.store.read_reservation(self.worktree_id)

    def test_atomic_write_failure_leaves_no_evidence_file(self) -> None:
        with patch.object(store_module.os, "replace", side_effect=OSError("blocked")):
            with self.assertRaises(WorktreeLifecyclePermissionError):
                self._reservation()
        path = self.root / "docs" / "pm" / "worktree-lifecycle" / "reservations" / f"{self.worktree_id}.yaml"
        self.assertFalse(path.exists())

    def test_concurrent_identical_reservations_replay_one_identity(self) -> None:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(lambda _: self.store.reserve(self.request), (1, 2)))
        self.assertEqual(results[0], results[1])
        directory = self.root / "docs" / "pm" / "worktree-lifecycle" / "reservations"
        self.assertEqual(len(tuple(directory.glob("*.yaml"))), 1)

    def test_concurrent_divergent_reservations_have_one_winner(self) -> None:
        divergent = replace(self.request, requested_at=STAMP_2)

        def reserve(request: WorktreeLifecycleRequest) -> str:
            try:
                self.store.reserve(request)
                return "winner"
            except WorktreeLifecycleConflictError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(executor.map(reserve, (self.request, divergent)))
        self.assertEqual(outcomes.count("winner"), 1)
        self.assertEqual(outcomes.count("conflict"), 1)

    def test_source_boundary_has_no_git_worktree_side_effect(self) -> None:
        source = (SCRIPT_ROOT / "worktree_lifecycle_store.py").read_text(encoding="utf-8")
        self.assertNotIn("subprocess", source)
        self.assertNotIn("git worktree add", source)
        self.assertNotIn("git worktree remove", source)
        self.assertNotIn("ControlPlaneTransitionService", source)


if __name__ == "__main__":
    unittest.main()
