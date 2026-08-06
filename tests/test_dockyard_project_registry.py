from __future__ import annotations

import dataclasses
import inspect
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "agentdesk" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from dockyard_project_registry import (  # noqa: E402
    DockyardProjectCommand,
    DockyardProjectConflictError,
    DockyardProjectInputError,
    DockyardProjectPhase,
    DockyardProjectRecord,
    DockyardProjectRegistry,
    DockyardProjectRegistryError,
    DockyardProjectResult,
    DockyardProjectSecurityError,
    DockyardProjectStoreError,
    DockyardRegisterProjectRequest,
    REPOSITORY_MARKER_FILENAME,
    decode_project_record,
    encode_project_record,
)


NOW = "2026-08-02T12:00:00Z"


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(cwd), *args),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise AssertionError(result.stdout)
    return result.stdout.strip()


class RegistryFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.repository = self.root / "repository"
        self.repository.mkdir()
        _git(self.repository, "init")
        _git(self.repository, "config", "user.name", "Dockyard Test")
        _git(self.repository, "config", "user.email", "dockyard@test.invalid")
        (self.repository / "README.md").write_text("test\n", encoding="utf-8")
        _git(self.repository, "add", "README.md")
        _git(self.repository, "commit", "-m", "baseline")
        self.registry = DockyardProjectRegistry(self.root / "runtime" / "dockyard")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def register_request(self, operation_id: str = "OP-REGISTER-1") -> DockyardRegisterProjectRequest:
        return DockyardRegisterProjectRequest(
            project_id="project-1",
            display_name="Dockyard Project",
            repository_root=str(self.repository),
            operation_id=operation_id,
            registered_at=NOW,
        )

    def register(self) -> DockyardProjectRecord:
        return self.registry.register(self.register_request()).record

    def command(
        self,
        record: DockyardProjectRecord,
        operation_id: str,
        occurred_at: str,
    ) -> DockyardProjectCommand:
        return DockyardProjectCommand(
            project_id=record.project_id,
            expected_repository_identity=record.repository_identity,
            expected_generation=record.generation,
            operation_id=operation_id,
            occurred_at=occurred_at,
        )


class DockyardProjectRegistryTypeTests(RegistryFixture):
    def test_record_fields_are_exact(self) -> None:
        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(DockyardProjectRecord)),
            (
                "schema_version", "project_id", "display_name", "repository_root",
                "repository_common_dir", "repository_identity", "phase", "generation",
                "registered_at", "updated_at", "removed_at", "last_operation_id",
                "last_request_digest", "content_digest",
            ),
        )

    def test_public_values_are_frozen_and_slotted(self) -> None:
        for value_type in (
            DockyardProjectRecord,
            DockyardRegisterProjectRequest,
            DockyardProjectCommand,
            DockyardProjectResult,
        ):
            self.assertTrue(value_type.__dataclass_params__.frozen)
            self.assertTrue(hasattr(value_type, "__slots__"))

    def test_bool_generation_is_rejected(self) -> None:
        with self.assertRaisesRegex(DockyardProjectInputError, "expected_generation"):
            DockyardProjectCommand(
                "project-1", "sha256:" + "0" * 64, True, "OP-X", NOW
            )

    def test_invalid_utc_timestamp_is_rejected(self) -> None:
        with self.assertRaisesRegex(DockyardProjectInputError, "registered_at"):
            DockyardRegisterProjectRequest(
                "project-1", "name", str(self.repository), "OP-X", "not-a-timeZ"
            )

    def test_malicious_str_repr_are_not_called(self) -> None:
        class Evil:
            def __str__(self) -> str:
                raise AssertionError("str called")

            def __repr__(self) -> str:
                raise AssertionError("repr called")

        with self.assertRaisesRegex(DockyardProjectInputError, "project_id"):
            DockyardRegisterProjectRequest(Evil(), "name", str(self.repository), "OP", NOW)  # type: ignore[arg-type]


class DockyardProjectRegistryCodecTests(RegistryFixture):
    def test_codec_roundtrip_is_byte_exact(self) -> None:
        record = self.register()
        encoded = encode_project_record(record)
        self.assertEqual(encode_project_record(decode_project_record(encoded)), encoded)
        self.assertTrue(encoded.endswith(b"\n"))

    def test_codec_key_order_is_exact(self) -> None:
        encoded = encode_project_record(self.register())
        raw = json.loads(encoded)
        self.assertEqual(tuple(raw), tuple(field.name for field in dataclasses.fields(DockyardProjectRecord)))

    def test_extra_key_is_rejected(self) -> None:
        raw = json.loads(encode_project_record(self.register()))
        raw["extra"] = "no"
        data = (json.dumps(raw, separators=(",", ":")) + "\n").encode()
        with self.assertRaisesRegex(DockyardProjectStoreError, "record_fields"):
            decode_project_record(data)

    def test_duplicate_key_is_rejected(self) -> None:
        data = b'{"schema_version":"x","schema_version":"y"}\n'
        with self.assertRaisesRegex(DockyardProjectStoreError, "duplicate_key"):
            decode_project_record(data)

    def test_digest_tamper_is_rejected(self) -> None:
        data = encode_project_record(self.register()).replace(b"Dockyard Project", b"Dockyard Tamper")
        with self.assertRaises(DockyardProjectStoreError):
            decode_project_record(data)


class DockyardProjectRegistryLifecycleTests(RegistryFixture):
    def test_fresh_registration_binds_real_git_identity(self) -> None:
        result = self.registry.register(self.register_request())
        self.assertTrue(result.changed)
        self.assertFalse(result.replayed)
        self.assertIs(result.record.phase, DockyardProjectPhase.REGISTERED)
        self.assertEqual(result.record.generation, 1)
        self.assertEqual(Path(result.record.repository_root), self.repository.resolve())
        self.assertTrue(result.record.repository_identity.startswith("sha256:"))

    def test_same_operation_is_byte_exact_replay(self) -> None:
        first = self.registry.register(self.register_request())
        second = self.registry.register(self.register_request())
        self.assertEqual(first.record, second.record)
        self.assertTrue(second.replayed)
        self.assertFalse(second.changed)

    def test_different_registration_operation_is_conflict(self) -> None:
        self.register()
        with self.assertRaisesRegex(DockyardProjectConflictError, "project_exists"):
            self.registry.register(self.register_request("OP-REGISTER-2"))

    def test_forward_only_disable_remove_finalize(self) -> None:
        registered = self.register()
        disabled = self.registry.disable(
            self.command(registered, "OP-DISABLE", "2026-08-02T12:01:00Z")
        ).record
        pending = self.registry.request_removal(
            self.command(disabled, "OP-REMOVE", "2026-08-02T12:02:00Z")
        ).record
        removed = self.registry.finalize_removal(
            self.command(pending, "OP-FINALIZE", "2026-08-02T12:03:00Z")
        ).record
        self.assertEqual(
            (registered.phase, disabled.phase, pending.phase, removed.phase),
            (
                DockyardProjectPhase.REGISTERED,
                DockyardProjectPhase.DISABLED,
                DockyardProjectPhase.REMOVAL_PENDING,
                DockyardProjectPhase.REMOVED,
            ),
        )
        self.assertEqual((registered.generation, disabled.generation, pending.generation, removed.generation), (1, 2, 3, 4))
        self.assertEqual(removed.removed_at, "2026-08-02T12:03:00Z")

    def test_removed_project_cannot_reopen(self) -> None:
        record = self.register()
        pending = self.registry.request_removal(
            self.command(record, "OP-REMOVE", "2026-08-02T12:01:00Z")
        ).record
        removed = self.registry.finalize_removal(
            self.command(pending, "OP-FINALIZE", "2026-08-02T12:02:00Z")
        ).record
        with self.assertRaisesRegex(DockyardProjectConflictError, "phase"):
            self.registry.disable(
                self.command(removed, "OP-REOPEN", "2026-08-02T12:03:00Z")
            )

    def test_stale_generation_is_rejected(self) -> None:
        record = self.register()
        command = dataclasses.replace(
            self.command(record, "OP-DISABLE", "2026-08-02T12:01:00Z"),
            expected_generation=2,
        )
        with self.assertRaisesRegex(DockyardProjectConflictError, "stale_generation"):
            self.registry.disable(command)

    def test_repository_identity_substitution_is_rejected(self) -> None:
        record = self.register()
        command = dataclasses.replace(
            self.command(record, "OP-DISABLE", "2026-08-02T12:01:00Z"),
            expected_repository_identity="sha256:" + "f" * 64,
        )
        with self.assertRaisesRegex(DockyardProjectConflictError, "repository_identity"):
            self.registry.disable(command)

    def test_removal_never_deletes_repository(self) -> None:
        record = self.register()
        pending = self.registry.request_removal(
            self.command(record, "OP-REMOVE", "2026-08-02T12:01:00Z")
        ).record
        self.registry.finalize_removal(
            self.command(pending, "OP-FINALIZE", "2026-08-02T12:02:00Z")
        )
        self.assertTrue(self.repository.is_dir())
        self.assertTrue((self.repository / ".git").exists())
        self.assertEqual((self.repository / "README.md").read_text(encoding="utf-8"), "test\n")


class DockyardProjectRegistryDefenseTests(RegistryFixture):
    def test_relative_repository_path_is_rejected(self) -> None:
        with self.assertRaisesRegex(DockyardProjectInputError, "repository_root"):
            DockyardRegisterProjectRequest("project-1", "name", "relative", "OP", NOW)

    def test_project_id_traversal_is_rejected(self) -> None:
        with self.assertRaisesRegex(DockyardProjectInputError, "project_id"):
            DockyardRegisterProjectRequest("../evil", "name", str(self.repository), "OP", NOW)

    def test_windows_device_project_id_is_rejected(self) -> None:
        with self.assertRaisesRegex(DockyardProjectInputError, "project_id_device"):
            DockyardRegisterProjectRequest("CON", "name", str(self.repository), "OP", NOW)

    def test_non_git_directory_is_rejected(self) -> None:
        directory = self.root / "not-git"
        directory.mkdir()
        with self.assertRaisesRegex(DockyardProjectInputError, "not_git_repository"):
            self.registry.register(
                DockyardRegisterProjectRequest("project-2", "name", str(directory), "OP", NOW)
            )

    def test_repository_symlink_is_rejected_when_supported(self) -> None:
        link = self.root / "repo-link"
        try:
            link.symlink_to(self.repository, target_is_directory=True)
        except OSError:
            self.skipTest("directory symlink unavailable")
        with self.assertRaisesRegex(DockyardProjectSecurityError, "repository_reparse"):
            self.registry.register(
                DockyardRegisterProjectRequest("project-2", "name", str(link), "OP", NOW)
            )

    def test_corrupt_durable_file_fails_closed(self) -> None:
        self.register()
        path = self.registry.root / "projects" / "project-1.json"
        path.write_bytes(b"not-json")
        with self.assertRaisesRegex(DockyardProjectStoreError, "record_json"):
            self.registry.read("project-1")

    def test_normal_commit_keeps_registered_repository_identity(self) -> None:
        record = self.register()
        (self.repository / "SECOND.md").write_text("second\n", encoding="utf-8")
        _git(self.repository, "add", "SECOND.md")
        _git(self.repository, "commit", "-m", "second")
        disabled = self.registry.disable(
            self.command(record, "OP-DISABLE-AFTER-COMMIT", "2026-08-02T12:01:00Z")
        ).record
        self.assertEqual(disabled.repository_identity, record.repository_identity)

    def test_same_path_repository_replacement_is_fail_closed(self) -> None:
        record = self.register()
        original = self.repository.with_name("repository-original")
        self.repository.rename(original)
        self.repository.mkdir()
        _git(self.repository, "init")
        try:
            with self.assertRaises(DockyardProjectRegistryError):
                self.registry.disable(
                    self.command(record, "OP-DISABLE-REPLACED", "2026-08-02T12:01:00Z")
                )
            self.assertEqual(self.registry.read(record.project_id), record)
        finally:
            shutil.rmtree(self.repository, ignore_errors=True)
            original.rename(self.repository)

    def test_marker_missing_and_replaced_are_fail_closed(self) -> None:
        record = self.register()
        marker = Path(record.repository_common_dir) / REPOSITORY_MARKER_FILENAME
        marker.unlink()
        with self.assertRaisesRegex(DockyardProjectStoreError, "repository_marker_missing"):
            self.registry.disable(
                self.command(record, "OP-MARKER-MISSING", "2026-08-02T12:01:00Z")
            )
        marker.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(DockyardProjectStoreError, "repository_marker_schema"):
            self.registry.disable(
                self.command(record, "OP-MARKER-REPLACED", "2026-08-02T12:02:00Z")
            )

    def test_marker_symlink_is_rejected_when_supported(self) -> None:
        record = self.register()
        marker = Path(record.repository_common_dir) / REPOSITORY_MARKER_FILENAME
        marker.unlink()
        target = self.root / "outside-marker"
        target.write_text(
            '{"schema_version":"agentdesk.dockyard-repository-marker/v1",'
            '"repository_id":"' + "a" * 64 + '"}\n',
            encoding="utf-8",
        )
        try:
            marker.symlink_to(target)
        except OSError:
            self.skipTest("marker symlink creation unavailable")
        with self.assertRaisesRegex(DockyardProjectSecurityError, "repository_marker_reparse"):
            self.registry.disable(
                self.command(record, "OP-MARKER-LINK", "2026-08-02T12:01:00Z")
            )

    def test_source_has_no_scan_or_destructive_repository_operations(self) -> None:
        source = inspect.getsource(sys.modules[DockyardProjectRegistry.__module__])
        for forbidden in (
            "os.walk", ".glob(", ".rglob(", "shutil.rmtree", "rm -rf",
            '"reset"', '"clean"', '"prune"', '"worktree"', "--force",
        ):
            self.assertNotIn(forbidden, source)


class DockyardProjectRegistryConcurrencyTests(RegistryFixture):
    def test_same_registration_has_one_changed_winner(self) -> None:
        barrier = threading.Barrier(2)
        results: list[DockyardProjectResult] = []
        errors: list[BaseException] = []

        def run() -> None:
            try:
                barrier.wait(timeout=5)
                results.append(self.registry.register(self.register_request()))
            except BaseException as exc:  # test capture only
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(sum(result.changed for result in results), 1)
        self.assertEqual(sum(result.replayed for result in results), 1)
        self.assertEqual(results[0].record, results[1].record)


if __name__ == "__main__":
    unittest.main()
