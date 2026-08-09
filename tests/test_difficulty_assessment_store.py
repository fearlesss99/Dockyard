"""TC-13.22b.2b TaskDifficulty evidence filesystem/ancestry store tests."""

from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO_ROOT / "skills" / "agentdesk" / "scripts"
sys.path.insert(0, str(_SCRIPTS))
import core_types  # noqa: E402
import difficulty_assessment_evidence as evidence  # noqa: E402
import difficulty_assessor  # noqa: E402
import difficulty_assessment_store as store  # noqa: E402
sys.path.pop(0)


RATIONALE_KEYS = (
    "scope.bounded",
    "clarity.explicit",
    "concurrency.single_writer",
    "contract.internal",
    "impact.informational",
    "rollback.simple",
    "dependency.none",
)


def _run_git(root: Path, *argv: str) -> str:
    result = subprocess.run(
        ["git", *argv],
        cwd=str(root),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
        shell=False,
    )
    return result.stdout.decode("utf-8").strip()


class DifficultyAssessmentStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="agentdesk-assessment-store-")
        self.root = Path(self.temp.name).resolve()
        _run_git(self.root, "init", "-q")
        _run_git(self.root, "config", "user.email", "agentdesk-tests@example.invalid")
        _run_git(self.root, "config", "user.name", "AgentDesk Tests")
        (self.root / "seed.txt").write_text("seed\n", encoding="utf-8")
        _run_git(self.root, "add", "seed.txt")
        _run_git(self.root, "commit", "-q", "-m", "seed")
        self.base_head = _run_git(self.root, "rev-parse", "HEAD")
        (self.root / "second.txt").write_text("second\n", encoding="utf-8")
        _run_git(self.root, "add", "second.txt")
        _run_git(self.root, "commit", "-q", "-m", "second")
        self.head = _run_git(self.root, "rev-parse", "HEAD")
        self.store = store.DifficultyAssessmentStore()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _evidence(self, **overrides: object) -> evidence.DifficultyAssessmentEvidence:
        values: dict[str, object] = {
            "assessment_id": "ASM-TC-13.22b.2b-r1",
            "task_id": "TC-13.22b.2b",
            "revision": 1,
            "rationale_keys": RATIONALE_KEYS,
        }
        values.update(overrides)
        snapshot_commit = values.pop("snapshot_commit", self.head)
        assessment = difficulty_assessor.assess_task_difficulty(**values)  # type: ignore[arg-type]
        return evidence.DifficultyAssessmentEvidence(
            assessment=assessment,
            snapshot_commit=snapshot_commit,  # type: ignore[arg-type]
        )

    def _request(
        self,
        item: evidence.DifficultyAssessmentEvidence | None = None,
        expected_head: str | None = None,
    ) -> store.DifficultyAssessmentStoreRequest:
        return store.DifficultyAssessmentStoreRequest(
            project_root=self.root,
            evidence=item or self._evidence(),
            expected_head=expected_head or self.head,
        )

    def test_public_api_and_immutable_models(self) -> None:
        self.assertEqual(
            store.__all__,
            [
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
            ],
        )
        self.assertTrue(dataclasses.is_dataclass(store.DifficultyAssessmentStoreRequest))
        self.assertTrue(dataclasses.is_dataclass(store.DifficultyAssessmentStoreResult))
        self.assertFalse(hasattr(self._request(), "__dict__"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            self._request().expected_head = self.head  # type: ignore[misc]

    def test_write_read_happy_path_and_exact_relative_path(self) -> None:
        result = self.store.write(self._request())
        self.assertEqual(
            result.relative_path,
            "docs/pm/assessments/TC-13.22b.2b/r1/ASM-TC-13.22b.2b-r1.yaml",
        )
        self.assertFalse(result.replayed)
        self.assertEqual(len(result.content_sha256), 64)
        restored = self.store.read(
            self.root, "TC-13.22b.2b", 1, "ASM-TC-13.22b.2b-r1"
        )
        self.assertEqual(restored, self._evidence())
        self.assertEqual(
            (self.root / result.relative_path).read_bytes(),
            evidence.encode_difficulty_assessment_evidence(self._evidence()),
        )

    def test_same_bytes_are_replayed_without_rewrite(self) -> None:
        self.store.write(self._request())
        with patch.object(store, "_atomic_write_bytes") as atomic_write:
            result = self.store.write(self._request())
        self.assertTrue(result.replayed)
        atomic_write.assert_not_called()

    def test_divergent_same_identity_is_conflict(self) -> None:
        self.store.write(self._request())
        divergent = self._evidence(snapshot_commit=self.base_head)
        with self.assertRaises(store.DifficultyAssessmentStoreConflictError):
            self.store.write(self._request(divergent))

    def test_assessment_id_is_globally_unique_across_task_paths(self) -> None:
        self.store.write(self._request())
        other = self._evidence(task_id="TC-other-task")
        with self.assertRaises(store.DifficultyAssessmentStoreConflictError):
            self.store.write(self._request(other))

    def test_head_mismatch_is_fail_closed(self) -> None:
        with self.assertRaises(store.DifficultyAssessmentStoreAncestryError):
            self.store.write(self._request(expected_head="f" * 40))

    def test_snapshot_must_be_an_existing_commit_ancestor(self) -> None:
        for snapshot in (
            "e" * 40,
            _run_git(self.root, "hash-object", "-w", "seed.txt"),
            _run_git(self.root, "rev-parse", "HEAD^{tree}"),
        ):
            with self.subTest(snapshot=snapshot):
                with self.assertRaises(store.DifficultyAssessmentStoreAncestryError):
                    self.store.write(self._request(self._evidence(snapshot_commit=snapshot)))

        tree = _run_git(self.root, "rev-parse", "HEAD^{tree}")
        orphan = subprocess.run(
            ["git", "commit-tree", tree, "-m", "unrelated"],
            cwd=str(self.root),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=True,
            shell=False,
        ).stdout.decode("utf-8").strip()
        with self.assertRaises(store.DifficultyAssessmentStoreAncestryError):
            self.store.write(self._request(self._evidence(snapshot_commit=orphan)))

    def test_path_escape_and_control_characters_are_rejected(self) -> None:
        base = self._evidence()
        for task_id in ("..", "..\\escape", "../escape", "bad\nvalue", "bad\x00value"):
            with self.subTest(task_id=task_id):
                invalid_assessment = dataclasses.replace(
                    base.assessment,
                    task_id=task_id,
                )
                invalid_evidence = evidence.DifficultyAssessmentEvidence(
                    assessment=invalid_assessment,
                    snapshot_commit=base.snapshot_commit,
                )
                with self.assertRaises(store.DifficultyAssessmentStoreError):
                    self.store.write(self._request(invalid_evidence))

    def test_assessment_root_symlink_is_rejected(self) -> None:
        assessments = self.root / "docs" / "pm" / "assessments"
        outside = Path(tempfile.mkdtemp(prefix="agentdesk-outside-"))
        self.addCleanup(lambda: outside.exists() and outside.rmdir())
        assessments.parent.mkdir(parents=True)
        try:
            assessments.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable")
        with self.assertRaises(store.DifficultyAssessmentStoreSecurityError):
            self.store.write(self._request())

    def test_target_symlink_is_rejected(self) -> None:
        target_dir = self.root / "docs" / "pm" / "assessments" / "TC-13.22b.2b" / "r1"
        target_dir.mkdir(parents=True)
        outside = Path(tempfile.mkdtemp(prefix="agentdesk-outside-")) / "outside.yaml"
        self.addCleanup(shutil.rmtree, outside.parent, ignore_errors=True)
        outside.write_bytes(b"outside\n")
        target = target_dir / "ASM-TC-13.22b.2b-r1.yaml"
        try:
            target.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable")
        with self.assertRaises(store.DifficultyAssessmentStoreSecurityError):
            self.store.write(self._request())

    def test_read_missing_file_is_typed_and_does_not_write(self) -> None:
        with self.assertRaises(store.DifficultyAssessmentStoreInputError):
            self.store.read(self.root, "TC-13.22b.2b", 1, "ASM-TC-13.22b.2b-r1")
        self.assertFalse((self.root / "docs").exists())

    def test_read_returns_immutable_verified_evidence_without_git_or_write(self) -> None:
        self.store.write(self._request())
        with patch.object(store, "_atomic_write_bytes") as atomic_write, patch.object(
            store.subprocess, "run"
        ) as git_run:
            restored = self.store.read(
                self.root, "TC-13.22b.2b", 1, "ASM-TC-13.22b.2b-r1"
            )
        self.assertEqual(restored, self._evidence())
        self.assertFalse(hasattr(restored, "__dict__"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            restored.snapshot_commit = self.head  # type: ignore[misc]
        atomic_write.assert_not_called()
        git_run.assert_not_called()

    def test_malformed_or_identity_mismatched_read_fails_closed(self) -> None:
        target = (
            self.root
            / "docs"
            / "pm"
            / "assessments"
            / "TC-13.22b.2b"
            / "r1"
            / "ASM-TC-13.22b.2b-r1.yaml"
        )
        target.parent.mkdir(parents=True)
        target.write_bytes(b"not canonical evidence\n")
        with self.assertRaises(store.DifficultyAssessmentStoreInputError):
            self.store.read(self.root, "TC-13.22b.2b", 1, "ASM-TC-13.22b.2b-r1")

    def test_file_disappearing_during_verified_read_fails_closed(self) -> None:
        self.store.write(self._request())
        target = self.root / "docs/pm/assessments/TC-13.22b.2b/r1/ASM-TC-13.22b.2b-r1.yaml"
        original = store._read_regular_bytes

        def disappear(path: Path, *, missing_is_error: bool) -> bytes | None:
            data = original(path, missing_is_error=missing_is_error)
            if path == target:
                target.unlink()
            return data

        with patch.object(store, "_read_regular_bytes", side_effect=disappear):
            with self.assertRaises(store.DifficultyAssessmentStoreSecurityError):
                self.store.read(
                    self.root, "TC-13.22b.2b", 1, "ASM-TC-13.22b.2b-r1"
                )

    def test_write_failure_is_typed_and_leaves_no_temp_file(self) -> None:
        with patch.object(store, "_atomic_write_bytes", side_effect=OSError("secret")):
            with self.assertRaises(store.DifficultyAssessmentStoreError):
                self.store.write(self._request())
        parent = self.root / "docs/pm/assessments/TC-13.22b.2b/r1"
        self.assertEqual(list(parent.glob(".tmp-*.yaml")), [])

    def test_atomic_replace_failure_leaves_no_temp_file(self) -> None:
        with patch.object(store.os, "replace", side_effect=OSError("secret")):
            with self.assertRaises(store.DifficultyAssessmentStoreError):
                self.store.write(self._request())
        parent = self.root / "docs/pm/assessments/TC-13.22b.2b/r1"
        self.assertEqual(list(parent.glob(".tmp-*.yaml")), [])

    def test_errors_do_not_leak_paths_yaml_rationale_or_git_stderr(self) -> None:
        with patch.object(
            store.subprocess,
            "run",
            return_value=SimpleNamespace(
                returncode=128,
                stdout=b"",
                stderr=(f"fatal: {self.root} scope.bounded\n".encode("utf-8")),
            ),
        ):
            with self.assertRaises(store.DifficultyAssessmentStoreAncestryError) as caught:
                self.store.write(self._request())
        message = str(caught.exception)
        self.assertNotIn(str(self.root), message)
        self.assertNotIn("scope.bounded", message)
        self.assertNotIn("fatal", message)

    def test_git_calls_use_argv_shell_false_and_devnull_stdin(self) -> None:
        calls: list[tuple[list[str], dict[str, object]]] = []

        def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
            calls.append((argv, kwargs))
            if argv[-2:] == ["rev-parse", "HEAD"]:
                return SimpleNamespace(returncode=0, stdout=(self.head + "\n").encode(), stderr=b"")
            if argv[-3:-1] == ["cat-file", "-t"]:
                return SimpleNamespace(returncode=0, stdout=b"commit\n", stderr=b"")
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        with patch.object(store.subprocess, "run", side_effect=fake_run):
            store._validate_ancestry(self.root, self._evidence(snapshot_commit=self.base_head), self.head)
        self.assertEqual(len(calls), 3)
        for argv, kwargs in calls:
            self.assertIsInstance(argv, list)
            self.assertIn(f"safe.directory={self.root}", argv)
            self.assertFalse(kwargs["shell"])
            self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
            self.assertFalse(any(name.upper().startswith("GIT_") for name in kwargs["env"]))

    def test_concurrent_same_writes_produce_one_write_and_one_replay(self) -> None:
        barrier = threading.Barrier(2)
        results: list[store.DifficultyAssessmentStoreResult] = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                barrier.wait()
                results.append(self.store.write(self._request()))
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker), threading.Thread(target=worker)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(result.replayed for result in results), [False, True])

    def test_concurrent_divergent_writes_have_one_conflict(self) -> None:
        barrier = threading.Barrier(2)
        results: list[store.DifficultyAssessmentStoreResult] = []
        errors: list[BaseException] = []
        requests = [self._request(), self._request(self._evidence(snapshot_commit=self.base_head))]

        def worker(request: store.DifficultyAssessmentStoreRequest) -> None:
            try:
                barrier.wait()
                results.append(self.store.write(request))
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(request,)) for request in requests]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], store.DifficultyAssessmentStoreConflictError)


if __name__ == "__main__":
    unittest.main()
