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
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/agentdesk/scripts"
sys.path.insert(0, str(SCRIPTS))

from dockyard_plan_store import DockyardPlanStore, DockyardPlanTask
from dockyard_pm_service import (
    DockyardPlanApprovalRequest,
    DockyardPlanDraftRequest,
    DockyardPlanMaterializeRequest,
    DockyardPlanTransitionRequest,
    DockyardPmService,
)
from dockyard_task_admission_composition import (
    COMPOSITION_SCHEMA_VERSION,
    DockyardAdmissionCompositionRecoveryRequired,
    DockyardAdmissionContextSnapshot,
    DockyardTaskAdmissionCompositionRuntime,
    DockyardTaskAdmissionPhase,
    DockyardTaskAdmissionProgress,
)
from pm_materialization_admission_handoff import (
    DISPATCH_AUTHORIZATION_SCHEMA_VERSION,
    HANDOFF_SCHEMA_VERSION,
    DispatchApprovalAuthorization,
    MaterializedTaskEvidence,
    canonical_worktree_identity,
)
from pm_task_admission_preparation import (
    PmTaskAdmissionPreparationRuntime,
    with_content_digest,
)
from portfolio_scheduler_store import BusinessPriority

STAMP = "2026-08-03T12:00:00Z"
RATIONALE = (
    "scope.bounded", "clarity.explicit", "concurrency.single_writer",
    "contract.internal", "impact.informational", "rollback.simple",
    "dependency.none",
)


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True, timeout=20).stdout.strip()


class FakeHandoff:
    def __init__(self, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    def execute(self, request, evidence, template, authorization, context, worktree):
        self.calls += 1
        if self.fail:
            raise RuntimeError("deterministic handoff interruption")
        return SimpleNamespace(receipt_id="MAR-TEST")


class Fixture:
    def __init__(self, dependencies: tuple[str, ...] = ()) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = (Path(self.temp.name) / "project").resolve()
        self.root.mkdir()
        git(self.root, "init", "-q")
        git(self.root, "config", "user.email", "composition@test.invalid")
        git(self.root, "config", "user.name", "Composition Test")
        policy = {
            "schema_version": "agentdesk.role-policies/v1",
            "tier_order": ["basic", "standard", "advanced", "expert"],
            "deliberation_tier_order": ["efficient", "balanced", "deep"],
            "risk_floors": {"L0": "basic", "L1": "standard", "L2": "advanced", "L3": "expert", "L4": "expert"},
            "roles": {"R1": {"default_tier": "basic", "minimum_tier": "basic", "deliberation_tier": "efficient", "required_capabilities": ["coding"], "degradation_policy": "block"}},
        }
        policy_path = self.root / "docs/pm/ROLE-POLICIES.yaml"
        policy_path.parent.mkdir(parents=True)
        policy_path.write_text(json.dumps(policy) + "\n", encoding="utf-8")
        state_path = self.root / "docs/pm/state/tasks.yaml"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(json.dumps({"schema_version": "agentdesk.tasks/v2", "project_id": "PRJ-1", "adoption_level": "standard", "updated_at": STAMP, "pm_control": {"holder_id": "PM-1", "lease_epoch": 1, "mode": "manual"}, "tasks": []}) + "\n", encoding="utf-8")
        for path in ("docs/pm/events", "docs/pm/outbox", "docs/pm/acceptances"):
            directory = self.root / path
            directory.mkdir(parents=True)
            (directory / ".gitkeep").write_text("", encoding="utf-8")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-m", "initialize canonical project")
        self.head = git(self.root, "rev-parse", "HEAD")
        bindings = self.root / ".agentdesk/runtime/model-bindings.yaml"
        bindings.parent.mkdir(parents=True)
        bindings.write_text(json.dumps({"schema_version": "agentdesk.model-bindings/v2", "updated_at": STAMP, "bindings": {"aaa-codex": {"provider": "codex", "model_id": "test-alt", "tier": "basic", "deliberation_tier": "efficient", "context_window_tokens": 64000, "capabilities": ["coding"], "enabled": True}, "basic": {"provider": "claude", "model_id": "test-basic", "tier": "basic", "deliberation_tier": "efficient", "context_window_tokens": 64000, "capabilities": ["coding"], "enabled": True}}}) + "\n", encoding="utf-8")
        self.task = DockyardPlanTask("TC-002", "Task", "Implement.", dependencies, "serial" if dependencies else "parallel", "R1", "implementation", "claude", "test-basic", "basic", 1000, 3, self.head, BusinessPriority.P1, RATIONALE, None, None, None, "L0", ("coding",), None, 0, 1)
        tasks = (self.task,)
        if dependencies:
            dependency = dataclasses.replace(self.task, task_id=dependencies[0], dependencies=())
            tasks = (dependency, self.task)
        pm = DockyardPmService(DockyardPlanStore(self.root / ".agentdesk/runtime/plans"), self.root / "docs/pm/tasks")
        draft = pm.draft(DockyardPlanDraftRequest("PLAN-1", "PRJ-1", "Requirement", tasks, "PM-1", STAMP, "OP-DRAFT"))
        pending = pm.request_approval(DockyardPlanTransitionRequest(draft.plan_id, draft.revision, draft.content_digest, STAMP, "OP-PENDING"))
        approved = pm.approve(DockyardPlanApprovalRequest(pending.plan_id, pending.revision, pending.content_digest, pending.plan_digest, len(tasks), STAMP, "CONF-1", "OP-APPROVE"))
        result = pm.materialize(DockyardPlanMaterializeRequest(approved.plan_id, approved.revision, approved.content_digest, approved.plan_digest, STAMP, "OP-MATERIALIZE"))
        self.plan = result.plan
        card_rel = "docs/pm/tasks/TC-002-r1-dockyard.md"
        card = self.root / card_rel
        evidence = MaterializedTaskEvidence(HANDOFF_SCHEMA_VERSION, "PRJ-1", "PLAN-1", self.plan.revision, "sha256:" + self.plan.content_digest, "OP-MATERIALIZE", "TC-002", 1, card_rel, "sha256:" + hashlib.sha256(card.read_bytes()).hexdigest(), "PM-1", STAMP, "sha256:" + "0" * 64)
        self.evidence = with_content_digest(evidence)
        self.authorization = with_content_digest(DispatchApprovalAuthorization(
            DISPATCH_AUTHORIZATION_SCHEMA_VERSION, self.plan.plan_id,
            self.plan.revision - 1, "sha256:" + self.plan.plan_digest,
            "CONF-1", "OP-APPROVE", STAMP, "sha256:" + "0" * 64,
        ))

    def close(self) -> None:
        self.temp.cleanup()


class DockyardTaskAdmissionCompositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = Fixture()

    def tearDown(self) -> None:
        self.fx.close()

    def test_public_types_are_exact_frozen_and_slotted(self) -> None:
        self.assertEqual(len(dataclasses.fields(DockyardAdmissionContextSnapshot)), 15)
        self.assertEqual(len(dataclasses.fields(DockyardTaskAdmissionProgress)), 25)
        for value in (DockyardAdmissionContextSnapshot, DockyardTaskAdmissionProgress):
            self.assertTrue(value.__dataclass_params__.frozen)
            self.assertTrue(hasattr(value, "__slots__"))

    def test_happy_path_uses_real_context_and_preparation_then_finalizes(self) -> None:
        handoff = FakeHandoff()
        runtime = DockyardTaskAdmissionCompositionRuntime(self.fx.root, handoff=handoff)
        progress = runtime.execute(self.fx.plan, self.fx.task, self.fx.evidence, self.fx.authorization)
        self.assertIs(progress.phase, DockyardTaskAdmissionPhase.FINALIZED)
        self.assertEqual(progress.handoff_receipt_id, "MAR-TEST")
        self.assertEqual(handoff.calls, 1)
        self.assertTrue((self.fx.root / ".agentdesk/runtime/dockyard-admission/context" / f"{progress.context_snapshot_id}.yaml").is_file())
        self.assertTrue((self.fx.root / ".agentdesk/runtime/admission-preparation/handoff-inputs" / f"{progress.preparation_id}.yaml").is_file())

    def test_pm_selected_provider_and_model_are_preserved_at_admission(self) -> None:
        # The unfenced selector would prefer the lexically-first Codex binding.
        # The durable plan names Claude, so preparation must not apply a second
        # unrelated ranking at admission time.
        self.fx.task = dataclasses.replace(
            self.fx.task, provider_id="claude", model_id="test-basic"
        )
        self.fx.plan = dataclasses.replace(self.fx.plan, tasks=(self.fx.task,))
        runtime = DockyardTaskAdmissionCompositionRuntime(self.fx.root, handoff=FakeHandoff())
        progress = runtime.execute(self.fx.plan, self.fx.task, self.fx.evidence, self.fx.authorization)
        inputs = PmTaskAdmissionPreparationRuntime(self.fx.root).read_handoff_inputs(progress.preparation_id)
        self.assertEqual(inputs.admission_plan_template.model_selection.selected_model_provider, "claude")
        self.assertEqual(inputs.admission_plan_template.model_selection.selected_model_id, "test-basic")

    def test_finalized_replay_does_not_repeat_downstream_calls(self) -> None:
        handoff = FakeHandoff()
        runtime = DockyardTaskAdmissionCompositionRuntime(self.fx.root, handoff=handoff)
        first = runtime.execute(self.fx.plan, self.fx.task, self.fx.evidence, self.fx.authorization)
        second = runtime.execute(self.fx.plan, self.fx.task, self.fx.evidence, self.fx.authorization)
        self.assertEqual(first, second)
        self.assertEqual(handoff.calls, 1)

    def test_divergent_confirmation_replay_is_rejected(self) -> None:
        runtime = DockyardTaskAdmissionCompositionRuntime(self.fx.root, handoff=FakeHandoff())
        runtime.execute(self.fx.plan, self.fx.task, self.fx.evidence, self.fx.authorization)
        divergent = with_content_digest(dataclasses.replace(
            self.fx.authorization,
            confirmation_id="CONF-EVIL",
            content_digest="sha256:" + "0" * 64,
        ))
        with self.assertRaisesRegex(
            Exception, "dispatch_authorization_replay"
        ):
            runtime.execute(self.fx.plan, self.fx.task, self.fx.evidence, divergent)

    def test_dependency_missing_waits_without_preparation_or_handoff(self) -> None:
        self.fx.close()
        self.fx = Fixture(("TC-001",))
        handoff = FakeHandoff()
        progress = DockyardTaskAdmissionCompositionRuntime(self.fx.root, handoff=handoff).execute(self.fx.plan, self.fx.task, self.fx.evidence, self.fx.authorization)
        self.assertIs(progress.phase, DockyardTaskAdmissionPhase.MATERIALIZED)
        self.assertEqual(progress.outcome.value, "WAITING_DEPENDENCIES")
        self.assertEqual(handoff.calls, 0)
        self.assertFalse((self.fx.root / ".agentdesk/runtime/admission-preparation").exists())

    def test_handoff_interruption_is_not_blindly_repeated(self) -> None:
        handoff = FakeHandoff(True)
        runtime = DockyardTaskAdmissionCompositionRuntime(self.fx.root, handoff=handoff)
        with self.assertRaises(RuntimeError):
            runtime.execute(self.fx.plan, self.fx.task, self.fx.evidence, self.fx.authorization)
        with self.assertRaises(DockyardAdmissionCompositionRecoveryRequired):
            runtime.execute(self.fx.plan, self.fx.task, self.fx.evidence, self.fx.authorization)
        self.assertEqual(handoff.calls, 1)

    def test_attempt_four_is_rejected_before_context_or_progress(self) -> None:
        bad_task = object.__new__(DockyardPlanTask)
        for field in dataclasses.fields(DockyardPlanTask):
            object.__setattr__(bad_task, field.name, getattr(self.fx.task, field.name))
        object.__setattr__(bad_task, "expected_task_attempt", 3)
        object.__setattr__(bad_task, "new_attempt", 4)
        bad_plan = dataclasses.replace(self.fx.plan, tasks=(bad_task,))
        with self.assertRaisesRegex(Exception, "attempt_limit"):
            DockyardTaskAdmissionCompositionRuntime(self.fx.root, handoff=FakeHandoff()).execute(bad_plan, bad_task, self.fx.evidence, self.fx.authorization)
        self.assertFalse((self.fx.root / ".agentdesk/runtime/dockyard-admission").exists())

    def test_two_identical_callers_invoke_handoff_once(self) -> None:
        handoff = FakeHandoff()
        runtime = DockyardTaskAdmissionCompositionRuntime(self.fx.root, handoff=handoff)
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def run() -> None:
            try:
                barrier.wait(timeout=5)
                results.append(runtime.execute(self.fx.plan, self.fx.task, self.fx.evidence, self.fx.authorization))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])
        self.assertEqual(handoff.calls, 1)

    def test_later_preparation_keeps_worktree_identity_on_current_cas_base(self) -> None:
        # Simulate an earlier card's evidence commit before this card starts.
        # The current HEAD is now newer than the frozen task base, yet both
        # the preparation and handoff paths must derive the same worktree.
        advanced = self.fx.root / "advanced-by-prior-card.txt"
        advanced.write_text("evidence\n", encoding="utf-8")
        git(self.fx.root, "add", advanced.name)
        git(self.fx.root, "commit", "-m", "docs: prior card evidence")
        runtime = DockyardTaskAdmissionCompositionRuntime(self.fx.root, handoff=FakeHandoff())
        progress = runtime.execute(self.fx.plan, self.fx.task, self.fx.evidence, self.fx.authorization)
        inputs = PmTaskAdmissionPreparationRuntime(self.fx.root).read_handoff_inputs(progress.preparation_id)
        self.assertNotEqual(inputs.materialization_admission_request.expected_head_commit, self.fx.task.base_commit)
        self.assertEqual(
            inputs.materialization_admission_request.expected_base_commit,
            inputs.materialization_admission_request.expected_head_commit,
        )
        worktree = runtime._worktree(
            inputs.admission_plan_template,
            inputs.materialization_admission_request,
        )
        self.assertEqual(
            inputs.admission_plan_template.canonical_worktree_identity,
            canonical_worktree_identity(worktree),
        )

    def test_context_cold_start_proves_all_empty_slots_without_sequence(self) -> None:
        handoff = FakeHandoff()
        progress = DockyardTaskAdmissionCompositionRuntime(self.fx.root, handoff=handoff).execute(self.fx.plan, self.fx.task, self.fx.evidence, self.fx.authorization)
        scheduler = self.fx.root / "docs/pm/portfolio-scheduler"
        self.assertFalse((scheduler / "sequence.yaml").exists())
        context = json.loads((self.fx.root / ".agentdesk/runtime/dockyard-admission/context" / f"{progress.context_snapshot_id}.yaml").read_text(encoding="utf-8"))
        self.assertEqual(len(context["available_worker_kinds"]), 4)

    def test_source_does_not_import_worker_provider_model_or_network_runtime(self) -> None:
        source = (SCRIPTS / "dockyard_task_admission_composition.py").read_text(encoding="utf-8").lower()
        for forbidden in ("worker_adapter", "reasonix_cli_provider", "socket", "requests", "urllib"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
