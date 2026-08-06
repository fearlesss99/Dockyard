from __future__ import annotations

import dataclasses
import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/agentdesk/scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from core_types import TaskDifficulty, WorkerKind
from pm_materialization_admission_handoff import (
    HANDOFF_SCHEMA_VERSION,
    MaterializedTaskEvidence,
    canonical_worktree_identity,
    repository_identity,
)
from pm_task_admission_preparation import (
    PREPARATION_SCHEMA_VERSION,
    AdmissionPreparationConflictError,
    AdmissionPreparationHandoffInputs,
    AdmissionPreparationPhase,
    AdmissionPreparationReceipt,
    AdmissionPreparationRequest,
    PmTaskAdmissionProfile,
    PmTaskAdmissionPreparationRuntime,
    with_content_digest,
)
from portfolio_scheduler_store import BusinessPriority
from worktree_lifecycle_store import derive_worktree_id, expected_branch, expected_worktree_path

STAMP = "2026-08-03T11:00:00Z"


def git(root: Path, *args: str, binary: bool = False):
    result = subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True,
        timeout=20,
    )
    return result.stdout if binary else result.stdout.decode().strip()


class Fixture:
    def __init__(self, expected_attempt: int = 0, new_attempt: int = 1) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        git(self.root, "init", "-q")
        git(self.root, "config", "user.email", "prep@test.invalid")
        git(self.root, "config", "user.name", "Preparation Test")
        git(self.root, "config", "core.autocrlf", "false")
        policy = {
            "schema_version": "agentdesk.role-policies/v1",
            "tier_order": ["basic", "standard", "advanced", "expert"],
            "deliberation_tier_order": ["efficient", "balanced", "deep"],
            "risk_floors": {"L0": "basic", "L1": "standard", "L2": "advanced", "L3": "expert", "L4": "expert"},
            "roles": {"DEV": {"default_tier": "basic", "minimum_tier": "basic", "deliberation_tier": "efficient", "required_capabilities": ["coding"], "degradation_policy": "block"}},
        }
        policy_path = self.root / "docs/pm/ROLE-POLICIES.yaml"
        policy_path.parent.mkdir(parents=True)
        policy_path.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
        state = self.root / "docs/pm/state/tasks.yaml"
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({"schema_version": "agentdesk.tasks/v2", "project_id": "project-1", "adoption_level": "standard", "updated_at": STAMP, "pm_control": {"holder_id": "pm-1", "lease_epoch": 1, "mode": "manual"}, "tasks": []}, indent=2) + "\n", encoding="utf-8")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-m", "prepare policy and card")
        self.head = git(self.root, "rev-parse", "HEAD")
        self.branch = git(self.root, "branch", "--show-current")
        self.card_rel = "docs/pm/tasks/TC-001-r1-dockyard.md"
        card = self.root / self.card_rel
        card.parent.mkdir(parents=True)
        card.write_text(
            "---\nschema_version: agentdesk.task-card/v2\ntask_id: TC-001\n"
            "revision: 1\ntype: implementation\nrole_id: DEV\n"
            f"base_commit: {'a' * 40}\nowner_approval:\n  gate: none\n---\n\n# Task\n",
            encoding="utf-8",
        )
        bindings = {
            "schema_version": "agentdesk.model-bindings/v2",
            "updated_at": STAMP,
            "bindings": {"binding-basic": {"provider": "claude", "model_id": "test-basic", "tier": "basic", "deliberation_tier": "efficient", "context_window_tokens": 64000, "capabilities": ["coding"], "enabled": True}},
        }
        binding_path = self.root / ".agentdesk/runtime/model-bindings.yaml"
        binding_path.parent.mkdir(parents=True)
        binding_path.write_text(json.dumps(bindings, indent=2) + "\n", encoding="utf-8")
        card_bytes = card.read_bytes()
        policy_bytes = git(self.root, "show", f"{self.head}:docs/pm/ROLE-POLICIES.yaml", binary=True)
        evidence = MaterializedTaskEvidence(
            HANDOFF_SCHEMA_VERSION, "project-1", "PLAN-001", 4,
            "sha256:" + "1" * 64, "OP-MATERIALIZE", "TC-001", 1,
            self.card_rel, "sha256:" + hashlib.sha256(card_bytes).hexdigest(),
            "pm-1", STAMP, "sha256:" + "0" * 64,
        )
        self.materialized = with_content_digest(evidence)
        profile = PmTaskAdmissionProfile(
            PREPARATION_SCHEMA_VERSION, "PROFILE-001", "project-1", "PLAN-001",
            4, "TC-001", 1, BusinessPriority.P1,
            ("scope.bounded", "clarity.explicit", "concurrency.single_writer", "contract.internal", "impact.informational", "rollback.simple", "dependency.none"),
            None, None, None, "L0", ("coding",), None,
            expected_attempt, new_attempt, STAMP, "sha256:" + "0" * 64,
        )
        self.profile = with_content_digest(profile)
        request = AdmissionPreparationRequest(
            PREPARATION_SCHEMA_VERSION, "PREP-001", profile.profile_id,
            self.profile.content_digest, self.materialized.content_digest,
            repository_identity(self.root), self.head,
            "docs/pm/ROLE-POLICIES.yaml", self.head,
            "sha256:" + hashlib.sha256(policy_bytes).hexdigest(),
            ".agentdesk/runtime/model-bindings.yaml",
            "sha256:" + hashlib.sha256(binding_path.read_bytes()).hexdigest(),
            "agentdesk.model-bindings/v2", 1,
            canonical_worktree_identity(Path(expected_worktree_path(
                str(self.root), derive_worktree_id(
                    str((self.root / ".git").resolve()), str(self.root),
                    expected_branch("TC-001", 1, new_attempt, "DSP-" + hashlib.sha256(b"PREP-001\x001").hexdigest()[:20]),
                    self.head, "TC-001", 1, new_attempt,
                    "DSP-" + hashlib.sha256(b"PREP-001\x001").hexdigest()[:20],
                ),
            ))), "holder-1",
            "reports/TC-001.md", self.head, self.branch, "sha256:" + "0" * 64,
        )
        self.request = with_content_digest(request)

    def close(self) -> None:
        self.temp.cleanup()


class AdmissionPreparationTypeTests(unittest.TestCase):
    def test_public_types_have_exact_field_counts(self) -> None:
        self.assertEqual(len(dataclasses.fields(PmTaskAdmissionProfile)), 19)
        self.assertEqual(len(dataclasses.fields(AdmissionPreparationRequest)), 20)
        self.assertEqual(len(dataclasses.fields(AdmissionPreparationReceipt)), 28)
        self.assertEqual(len(dataclasses.fields(AdmissionPreparationHandoffInputs)), 13)
        for value in (PmTaskAdmissionProfile, AdmissionPreparationRequest, AdmissionPreparationReceipt, AdmissionPreparationHandoffInputs):
            self.assertTrue(value.__dataclass_params__.frozen)
            self.assertTrue(hasattr(value, "__slots__"))

    def test_attempt_four_is_prewrite_rejected(self) -> None:
        fixture = Fixture()
        try:
            with self.assertRaisesRegex(Exception, "attempt_limit"):
                dataclasses.replace(fixture.profile, expected_task_attempt=3, new_attempt=4)
            self.assertFalse((fixture.root / ".agentdesk/runtime/admission-preparation").exists())
        finally:
            fixture.close()


class AdmissionPreparationRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = Fixture()

    def tearDown(self) -> None:
        self.fx.close()

    def run_preparation(self):
        return PmTaskAdmissionPreparationRuntime(self.fx.root).prepare(
            self.fx.profile, self.fx.request, self.fx.materialized
        )

    def test_happy_path_persists_assessment_model_receipt_and_handoff_inputs(self) -> None:
        before = git(self.fx.root, "status", "--porcelain", "--untracked-files=all")
        committed = subprocess.run(
            ["git", "-C", str(self.fx.root), "cat-file", "-e", f"{self.fx.head}:{self.fx.card_rel}"],
            capture_output=True,
        )
        self.assertNotEqual(committed.returncode, 0)
        result = self.run_preparation()
        self.assertIs(result.receipt.phase, AdmissionPreparationPhase.FINALIZED)
        self.assertEqual(result.receipt.business_priority, BusinessPriority.P1)
        self.assertEqual(result.receipt.selected_difficulty, TaskDifficulty.BASIC)
        self.assertEqual(result.receipt.worker_kind, WorkerKind.BASIC_AGENT)
        self.assertEqual(result.receipt.model_binding_id, "binding-basic")
        self.assertEqual(result.template.expected_task_attempt, 0)
        self.assertEqual(result.template.new_attempt, 1)
        self.assertEqual(result.handoff_inputs.expected_task_attempt, 0)
        self.assertEqual(result.handoff_inputs.assessment_content_digest, result.receipt.assessment_content_digest)
        self.assertEqual(result.handoff_request.expected_assessment_relative_path, result.handoff_inputs.assessment_relative_path)
        self.assertEqual(result.handoff_request.expected_assessment_content_digest, result.handoff_inputs.assessment_content_digest)
        for path in (
            "profiles/PROFILE-001.yaml", "receipts/" + result.receipt.receipt_id + ".yaml",
            "model-selections/PREP-001.yaml", "handoff-inputs/PREP-001.yaml",
        ):
            self.assertTrue((self.fx.root / ".agentdesk/runtime/admission-preparation" / path).is_file())
        assessments = list((self.fx.root / "docs/pm/assessments/TC-001/r1").glob("*.yaml"))
        self.assertEqual(len(assessments), 1)
        after = git(self.fx.root, "status", "--porcelain", "--untracked-files=all")
        self.assertIn(f"?? {self.fx.card_rel}", before.replace("\\", "/"))
        self.assertIn(f"?? {self.fx.card_rel}", after.replace("\\", "/"))
        self.assertEqual(git(self.fx.root, "rev-parse", "HEAD"), self.fx.head)

    def test_attempt_three_propagates_unchanged(self) -> None:
        self.fx.close()
        self.fx = Fixture(2, 3)
        result = self.run_preparation()
        self.assertEqual((result.receipt.expected_task_attempt, result.receipt.new_attempt), (2, 3))
        self.assertEqual((result.template.expected_task_attempt, result.template.new_attempt), (2, 3))
        self.assertEqual((result.handoff_inputs.expected_task_attempt, result.handoff_inputs.new_attempt), (2, 3))

    def test_byte_exact_replay_returns_same_outputs_and_no_duplicate_evidence(self) -> None:
        first = self.run_preparation()
        second = self.run_preparation()
        self.assertEqual(first, second)
        self.assertEqual(len(list((self.fx.root / "docs/pm/assessments/TC-001/r1").glob("*.yaml"))), 1)
        self.assertEqual(len(list((self.fx.root / ".agentdesk/runtime/admission-preparation/handoff-inputs").glob("*.yaml"))), 1)

    def test_explicit_priority_is_not_changed_by_difficulty_or_model(self) -> None:
        result = self.run_preparation()
        self.assertEqual(result.receipt.business_priority, BusinessPriority.P1)
        self.assertEqual(result.handoff_request.business_priority, BusinessPriority.P1)
        self.assertEqual(result.receipt.selected_difficulty.value, "basic")

    def test_binding_digest_substitution_rejects_before_any_preparation_write(self) -> None:
        binding = self.fx.root / ".agentdesk/runtime/model-bindings.yaml"
        binding.write_text(binding.read_text(encoding="utf-8") + " ", encoding="utf-8")
        with self.assertRaises(AdmissionPreparationConflictError):
            self.run_preparation()
        self.assertFalse((self.fx.root / ".agentdesk/runtime/admission-preparation").exists())
        self.assertFalse((self.fx.root / "docs/pm/assessments").exists())

    def test_materialized_card_digest_substitution_is_prewrite_rejected(self) -> None:
        card = self.fx.root / self.fx.card_rel
        card.write_text(card.read_text(encoding="utf-8") + "divergent\n", encoding="utf-8")
        with self.assertRaises(AdmissionPreparationConflictError):
            self.run_preparation()
        self.assertFalse((self.fx.root / ".agentdesk/runtime/admission-preparation").exists())
        self.assertFalse((self.fx.root / "docs/pm/assessments").exists())

    def test_materialization_base_commit_divergence_is_prewrite_rejected(self) -> None:
        request = dataclasses.replace(
            self.fx.request,
            materialization_base_commit="b" * 40,
            content_digest="sha256:" + "0" * 64,
        )
        request = with_content_digest(request)
        with self.assertRaises(AdmissionPreparationConflictError):
            PmTaskAdmissionPreparationRuntime(self.fx.root).prepare(
                self.fx.profile, request, self.fx.materialized
            )
        self.assertFalse((self.fx.root / ".agentdesk/runtime/admission-preparation").exists())

    def test_non_regular_materialized_card_is_prewrite_rejected(self) -> None:
        card = self.fx.root / self.fx.card_rel
        card.unlink()
        card.mkdir()
        with self.assertRaises(AdmissionPreparationConflictError):
            self.run_preparation()
        self.assertFalse((self.fx.root / ".agentdesk/runtime/admission-preparation").exists())
        self.assertFalse((self.fx.root / "docs/pm/assessments").exists())

    def test_divergent_attempt_profile_rejected_by_request_binding(self) -> None:
        changed = dataclasses.replace(self.fx.profile, expected_task_attempt=1, new_attempt=2, content_digest="sha256:" + "0" * 64)
        changed = with_content_digest(changed)
        with self.assertRaises(AdmissionPreparationConflictError):
            PmTaskAdmissionPreparationRuntime(self.fx.root).prepare(changed, self.fx.request, self.fx.materialized)
        self.assertFalse((self.fx.root / ".agentdesk/runtime/admission-preparation").exists())

    def test_two_identical_preparers_converge_on_one_durable_output(self) -> None:
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def prepare() -> None:
            try:
                barrier.wait(timeout=5)
                results.append(self.run_preparation())
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=prepare) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(list((self.fx.root / ".agentdesk/runtime/admission-preparation/handoff-inputs").glob("*.yaml"))), 1)
        self.assertEqual(len(list((self.fx.root / "docs/pm/assessments/TC-001/r1").glob("*.yaml"))), 1)


class AdmissionPreparationSourceTests(unittest.TestCase):
    def test_no_provider_model_api_network_or_scheduler_runtime_import(self) -> None:
        source = (SCRIPTS / "pm_task_admission_preparation.py").read_text(encoding="utf-8")
        for value in ("worker_adapter", "workflow_orchestrator", "portfolio_scheduler_runtime", "socket", "requests", "urllib", "reasonix_cli_provider"):
            self.assertNotIn(value, source)
        self.assertNotIn("shell=True", source)


if __name__ == "__main__":
    unittest.main()
