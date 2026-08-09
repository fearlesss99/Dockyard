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
SCRIPTS = ROOT / "skills" / "agentdesk" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from core_types import WorkerKind
from dispatcher_gateway import ModelSelectionSnapshot
from pm_materialization_admission_handoff import (
    DISPATCH_AUTHORIZATION_SCHEMA_VERSION,
    DispatchApprovalAuthorization,
    HANDOFF_SCHEMA_VERSION,
    MaterializationAdmissionConflictError,
    MaterializationAdmissionPhase,
    MaterializationAdmissionPlanTemplate,
    MaterializationAdmissionRequest,
    MaterializationAdmissionRuntime,
    MaterializedTaskEvidence,
    _SchedulerEvidence,
    _GitOwner,
    canonical_worktree_identity,
    repository_identity,
    with_content_digest,
)
from portfolio_scheduler_policy import AdmissionContext
from portfolio_scheduler_store import (
    SCHEMA_VERSION,
    BusinessPriority,
    PortfolioSchedulerStore,
)
from worktree_lifecycle_store import (
    derive_worktree_id,
    expected_branch as expected_dispatch_branch,
    expected_worktree_path,
)

STAMP = "2026-08-03T10:00:00Z"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True,
        timeout=20, text=True,
    ).stdout.strip()


class _FakeScheduler:
    def __init__(self, fail_once: bool = False) -> None:
        self.calls = 0
        self.fail_once = fail_once
        self.plans = []

    def submit(self, plan, context):
        self.calls += 1
        self.plans.append(plan)
        if self.fail_once and self.calls == 1:
            raise RuntimeError("deterministic scheduler boundary failure")
        return _SchedulerEvidence(
            plan.receipt_id, plan.receipt_id, plan.dispatch_id, plan.event_id
        )


class HandoffFixture:
    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = (Path(self.temp.name) / "project").resolve()
        self.root.mkdir()
        _git(self.root, "init", "-q")
        _git(self.root, "config", "user.email", "dockyard@test.invalid")
        _git(self.root, "config", "user.name", "Dockyard Test")
        _git(self.root, "config", "core.autocrlf", "false")
        state = {
            "schema_version": "agentdesk.tasks/v2",
            "project_id": "project-1",
            "adoption_level": "standard",
            "updated_at": STAMP,
            "pm_control": {"holder_id": "pm-1", "lease_epoch": 1, "mode": "manual"},
            "tasks": [],
        }
        path = self.root / "docs" / "pm" / "state" / "tasks.yaml"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        for name in ("events", "outbox", "acceptances"):
            (self.root / "docs" / "pm" / name).mkdir(parents=True)
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "initial canonical project")
        self.head = _git(self.root, "rev-parse", "HEAD")
        self.branch = _git(self.root, "branch", "--show-current")
        common_raw = Path(
            _git(self.root, "rev-parse", "--path-format=absolute", "--git-common-dir")
        )
        common = str(
            (common_raw if common_raw.is_absolute() else self.root / common_raw).resolve()
        )
        dispatch_branch = expected_dispatch_branch("TC-001", 1, 1, "DSP-001")
        worktree_id = derive_worktree_id(
            common,
            str(self.root),
            dispatch_branch,
            self.head,
            "TC-001",
            1,
            1,
            "DSP-001",
        )
        self.worktree = Path(expected_worktree_path(str(self.root), worktree_id))
        self.card_rel = "docs/pm/tasks/TC-001-r1-dockyard.md"
        self.card = self.root / self.card_rel
        self.card.parent.mkdir(parents=True)
        self.card.write_text(
            "---\nschema_version: agentdesk.task-card/v2\ntask_id: TC-001\n"
            "revision: 1\ntype: implementation\nrole_id: worker-basic\n"
            f"base_commit: {self.head}\nowner_approval:\n  gate: none\n---\n\n# Task\n",
            encoding="utf-8",
        )
        import hashlib
        card_digest = "sha256:" + hashlib.sha256(self.card.read_bytes()).hexdigest()
        self.assessment_rel = "docs/pm/assessments/TC-001/r1/ASM-001.yaml"
        self.assessment = self.root / self.assessment_rel
        self.assessment.parent.mkdir(parents=True)
        self.assessment.write_text("schema_version: assessment-test/v1\n", encoding="utf-8")
        assessment_digest = "sha256:" + hashlib.sha256(self.assessment.read_bytes()).hexdigest()
        model = ModelSelectionSnapshot(
            required_model_tier="basic",
            required_model_capabilities=("code",),
            model_binding_id="binding-basic",
            selected_model_provider="claude",
            selected_model_id="claude-test",
            selected_model_tier="basic",
            selected_deliberation_tier="fast",
            selected_context_window_tokens=64000,
            selected_model_capabilities=("code",),
            model_degradation_approval_id=None,
        )
        template = MaterializationAdmissionPlanTemplate(
            HANDOFF_SCHEMA_VERSION, "APT-001", "TC-001", 1,
            WorkerKind.BASIC_AGENT, "ASM-001", "DSP-001", "EVT-001",
            "MSG-001", "worker-basic", "reports/TC-001.md", model, "ready",
            0, 1, SCHEMA_VERSION, "holder-1",
            canonical_worktree_identity(self.worktree), STAMP, "sha256:" + "0" * 64,
        )
        self.template = with_content_digest(template)
        evidence = MaterializedTaskEvidence(
            HANDOFF_SCHEMA_VERSION, "project-1", "PLAN-001", 4,
            "sha256:" + "1" * 64, "OP-MATERIALIZE-001", "TC-001", 1,
            self.card_rel, card_digest, "pm-1", STAMP, "sha256:" + "0" * 64,
        )
        self.evidence = with_content_digest(evidence)
        request = MaterializationAdmissionRequest(
            HANDOFF_SCHEMA_VERSION, "HANDOFF-001", "project-1", "PLAN-001",
            "TC-001", 1, self.evidence.content_digest, self.template.template_id,
            self.template.content_digest, 4, evidence.plan_content_digest,
            self.card_rel, card_digest, self.assessment_rel, assessment_digest,
            repository_identity(self.root), self.head, self.head, self.branch,
            1, BusinessPriority.P1,
            WorkerKind.BASIC_AGENT, "ASM-001", SCHEMA_VERSION, STAMP,
            "sha256:" + "0" * 64,
        )
        self.request = with_content_digest(request)
        self.authorization = with_content_digest(DispatchApprovalAuthorization(
            DISPATCH_AUTHORIZATION_SCHEMA_VERSION, "PLAN-001", 3,
            "sha256:" + "2" * 64, "CONF-1", "OP-APPROVE", STAMP,
            "sha256:" + "0" * 64,
        ))
        self.context = AdmissionContext(
            SCHEMA_VERSION, STAMP, (), (), (), (WorkerKind.BASIC_AGENT,), 1
        )

    def close(self) -> None:
        self.temp.cleanup()


class MaterializationAdmissionTypeTests(unittest.TestCase):
    def test_public_types_are_frozen_slotted_and_exact(self) -> None:
        expected = {
            DispatchApprovalAuthorization: 8,
            MaterializedTaskEvidence: 13,
            MaterializationAdmissionPlanTemplate: 20,
            MaterializationAdmissionRequest: 26,
        }
        for cls, count in expected.items():
            self.assertEqual(len(dataclasses.fields(cls)), count)
            self.assertTrue(cls.__dataclass_params__.frozen)
            self.assertTrue(hasattr(cls, "__slots__"))

    def test_attempt_four_is_rejected_before_filesystem(self) -> None:
        fixture = HandoffFixture()
        try:
            with self.assertRaises(Exception) as caught:
                dataclasses.replace(
                    fixture.template, expected_task_attempt=3, new_attempt=4
                )
            self.assertIn("attempt_limit", str(caught.exception))
            self.assertFalse((fixture.root / ".agentdesk").exists())
        finally:
            fixture.close()


class MaterializationAdmissionRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = HandoffFixture()

    def tearDown(self) -> None:
        self.fx.close()

    def _run(self, scheduler: _FakeScheduler | None = None):
        scheduler = scheduler or _FakeScheduler()
        runtime = MaterializationAdmissionRuntime(self.fx.root, scheduler=scheduler)
        receipt = runtime.execute(
            self.fx.request, self.fx.evidence, self.fx.template,
            self.fx.authorization, self.fx.context, self.fx.worktree,
        )
        return receipt, scheduler

    def test_happy_path_commits_registers_queues_and_finalizes(self) -> None:
        receipt, scheduler = self._run()
        self.assertIs(receipt.phase, MaterializationAdmissionPhase.FINALIZED)
        self.assertEqual(scheduler.calls, 1)
        self.assertEqual(_git(self.fx.root, "show", f"HEAD:{self.fx.card_rel}"), self.fx.card.read_text(encoding="utf-8").strip())
        self.assertEqual(_git(self.fx.root, "show", f"HEAD:{self.fx.assessment_rel}"), self.fx.assessment.read_text(encoding="utf-8").strip())
        state = json.loads((self.fx.root / "docs/pm/state/tasks.yaml").read_text(encoding="utf-8"))
        self.assertEqual([(t["task_id"], t["state"]) for t in state["tasks"]], [("TC-001", "ready")])
        queue = PortfolioSchedulerStore(self.fx.root).enumerate_queue_snapshot()
        self.assertEqual(len(queue), 1)
        self.assertEqual((queue[0].task_id, queue[0].enqueue_sequence), ("TC-001", 1))
        self.assertEqual(scheduler.plans[0].receipt_id, f"SR-{queue[0].queue_id[2:]}-1-g1")
        self.assertTrue((self.fx.root / ".agentdesk/runtime/materialization-admission/admission-plans/HANDOFF-001.yaml").is_file())

    def test_prior_untracked_dockyard_evidence_does_not_block_current_materialization(self) -> None:
        prior_card = self.fx.root / "docs/pm/tasks/TC-12345678901234567890-r1-dockyard.md"
        prior_card.write_text("prior durable task evidence\n", encoding="utf-8")
        prior_assessment = self.fx.root / (
            "docs/pm/assessments/TC-12345678901234567890/r1/"
            "ASM-0123456789abcdef01234567.yaml"
        )
        prior_assessment.parent.mkdir(parents=True)
        prior_assessment.write_text("prior durable assessment evidence\n", encoding="utf-8")
        receipt, scheduler = self._run()
        self.assertIs(receipt.phase, MaterializationAdmissionPhase.FINALIZED)
        self.assertEqual(scheduler.calls, 1)
        self.assertTrue(prior_card.is_file())
        self.assertTrue(prior_assessment.is_file())
        self.assertNotIn(str(prior_card.relative_to(self.fx.root)).replace("\\", "/"), _git(self.fx.root, "ls-files"))

    def test_unrelated_untracked_file_is_not_committed_with_evidence(self) -> None:
        unrelated = self.fx.root / "unrelated.txt"
        unrelated.write_text("user data\n", encoding="utf-8")
        receipt, scheduler = self._run()
        self.assertIs(receipt.phase, MaterializationAdmissionPhase.FINALIZED)
        self.assertEqual(scheduler.calls, 1)
        self.assertTrue(unrelated.is_file())
        self.assertNotIn("unrelated.txt", _git(self.fx.root, "show", "--format=", "--name-only", "HEAD").splitlines())

    def test_later_card_appends_evidence_after_prior_card_advanced_head(self) -> None:
        self._run()
        task_id = "TC-002"
        card_rel = f"docs/pm/tasks/{task_id}-r1-dockyard.md"
        card = self.fx.root / card_rel
        card.write_text("second durable task card\n", encoding="utf-8")
        assessment_rel = f"docs/pm/assessments/{task_id}/r1/ASM-002.yaml"
        assessment = self.fx.root / assessment_rel
        assessment.parent.mkdir(parents=True)
        assessment.write_text("second assessment\n", encoding="utf-8")
        evidence = dataclasses.replace(
            self.fx.evidence,
            task_id=task_id,
            task_card_relative_path=card_rel,
            task_card_content_digest="sha256:" + hashlib.sha256(card.read_bytes()).hexdigest(),
            content_digest="sha256:" + "0" * 64,
        )
        evidence = with_content_digest(evidence)
        request = dataclasses.replace(
            self.fx.request,
            handoff_id="HANDOFF-002",
            task_id=task_id,
            materialized_task_content_digest=evidence.content_digest,
            expected_task_card_relative_path=card_rel,
            expected_task_card_content_digest=evidence.task_card_content_digest,
            expected_assessment_relative_path=assessment_rel,
            expected_assessment_content_digest="sha256:" + hashlib.sha256(assessment.read_bytes()).hexdigest(),
            assessment_id="ASM-002",
            content_digest="sha256:" + "0" * 64,
        )
        request = with_content_digest(request)
        commit = _GitOwner().bind(self.fx.root, request, evidence)
        self.assertEqual(commit, _git(self.fx.root, "rev-parse", "HEAD"))
        self.assertEqual(_git(self.fx.root, "show", f"HEAD:{card_rel}"), "second durable task card")
        self.assertEqual(_git(self.fx.root, "show", f"HEAD:{assessment_rel}"), "second assessment")

    def test_foreign_staged_file_remains_fail_closed(self) -> None:
        unrelated = self.fx.root / "unrelated.txt"
        unrelated.write_text("user data\n", encoding="utf-8")
        _git(self.fx.root, "add", "unrelated.txt")
        scheduler = _FakeScheduler()
        with self.assertRaisesRegex(MaterializationAdmissionConflictError, "staged_worktree"):
            self._run(scheduler)
        self.assertEqual(scheduler.calls, 0)

    def test_finalized_replay_is_zero_scheduler_calls(self) -> None:
        first, scheduler = self._run()
        replay_runtime = MaterializationAdmissionRuntime(self.fx.root, scheduler=scheduler)
        second = replay_runtime.execute(
            self.fx.request, self.fx.evidence, self.fx.template,
            self.fx.authorization, self.fx.context, self.fx.worktree,
        )
        self.assertEqual(first, second)
        self.assertEqual(scheduler.calls, 1)
        self.assertEqual(len(PortfolioSchedulerStore(self.fx.root).enumerate_queue_snapshot()), 1)

    def test_scheduler_failure_replays_durable_ready_plan(self) -> None:
        scheduler = _FakeScheduler(fail_once=True)
        with self.assertRaisesRegex(RuntimeError, "deterministic"):
            self._run(scheduler)
        receipt_path = next((self.fx.root / ".agentdesk/runtime/materialization-admission/receipts").glob("*.yaml"))
        self.assertEqual(json.loads(receipt_path.read_text(encoding="utf-8"))["phase"], "ADMISSION_READY")
        receipt, _ = self._run(scheduler)
        self.assertIs(receipt.phase, MaterializationAdmissionPhase.FINALIZED)
        self.assertEqual(scheduler.calls, 2)
        self.assertEqual(scheduler.plans[0], scheduler.plans[1])

    def test_template_identity_substitution_is_pre_git_rejected(self) -> None:
        evil = dataclasses.replace(self.fx.template, dispatch_id="DSP-EVIL", content_digest="sha256:" + "0" * 64)
        evil = with_content_digest(evil)
        runtime = MaterializationAdmissionRuntime(self.fx.root, scheduler=_FakeScheduler())
        with self.assertRaises(MaterializationAdmissionConflictError):
            runtime.execute(self.fx.request, self.fx.evidence, evil, self.fx.authorization, self.fx.context, self.fx.worktree)
        self.assertEqual(_git(self.fx.root, "rev-parse", "HEAD"), self.fx.head)
        self.assertFalse((self.fx.root / ".agentdesk/runtime/materialization-admission/receipts").exists())

    def test_card_tamper_fails_before_canonical_queue_or_scheduler(self) -> None:
        self.fx.card.write_text("tampered", encoding="utf-8")
        scheduler = _FakeScheduler()
        runtime = MaterializationAdmissionRuntime(self.fx.root, scheduler=scheduler)
        with self.assertRaises(MaterializationAdmissionConflictError):
            runtime.execute(self.fx.request, self.fx.evidence, self.fx.template, self.fx.authorization, self.fx.context, self.fx.worktree)
        state = json.loads((self.fx.root / "docs/pm/state/tasks.yaml").read_text(encoding="utf-8"))
        self.assertEqual(state["tasks"], [])
        self.assertFalse((self.fx.root / "docs/pm/portfolio-scheduler").exists())
        self.assertEqual(scheduler.calls, 0)

    def test_assessment_tamper_fails_before_canonical_queue_or_scheduler(self) -> None:
        self.fx.assessment.write_text("tampered\n", encoding="utf-8")
        scheduler = _FakeScheduler()
        runtime = MaterializationAdmissionRuntime(self.fx.root, scheduler=scheduler)
        with self.assertRaises(MaterializationAdmissionConflictError):
            runtime.execute(self.fx.request, self.fx.evidence, self.fx.template, self.fx.authorization, self.fx.context, self.fx.worktree)
        state = json.loads((self.fx.root / "docs/pm/state/tasks.yaml").read_text(encoding="utf-8"))
        self.assertEqual(state["tasks"], [])
        self.assertFalse((self.fx.root / "docs/pm/portfolio-scheduler").exists())
        self.assertEqual(scheduler.calls, 0)

    def test_repository_identity_substitution_is_fail_closed(self) -> None:
        bad = dataclasses.replace(self.fx.request, repository_identity="sha256:" + "9" * 64, content_digest="sha256:" + "0" * 64)
        bad = with_content_digest(bad)
        scheduler = _FakeScheduler()
        with self.assertRaises(MaterializationAdmissionConflictError):
            MaterializationAdmissionRuntime(self.fx.root, scheduler=scheduler).execute(
                bad, self.fx.evidence, self.fx.template, self.fx.authorization, self.fx.context, self.fx.root
            )
        self.assertEqual(scheduler.calls, 0)

    def test_scheduler_runs_after_all_owner_locks_and_ready_receipt(self) -> None:
        root = self.fx.root

        class InspectingScheduler(_FakeScheduler):
            def submit(inner, plan, context):
                receipt = json.loads(next((root / ".agentdesk/runtime/materialization-admission/receipts").glob("*.yaml")).read_text(encoding="utf-8"))
                self.assertEqual(receipt["phase"], "ADMISSION_READY")
                self.assertTrue((root / ".agentdesk/runtime/materialization-admission/admission-plans/HANDOFF-001.yaml").is_file())
                self.assertEqual(len(tuple((root / "docs/pm/approvals").glob("*.yaml"))), 1)
                return super().submit(plan, context)

        receipt, scheduler = self._run(InspectingScheduler())
        self.assertIs(receipt.phase, MaterializationAdmissionPhase.FINALIZED)
        self.assertEqual(scheduler.calls, 1)

    def test_two_concurrent_callers_create_one_identity(self) -> None:
        scheduler = _FakeScheduler()
        results = []
        errors = []
        barrier = threading.Barrier(2)

        def run() -> None:
            try:
                barrier.wait(timeout=5)
                results.append(MaterializationAdmissionRuntime(self.fx.root, scheduler=scheduler).execute(
                    self.fx.request, self.fx.evidence, self.fx.template,
                    self.fx.authorization, self.fx.context, self.fx.worktree,
                ))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertGreaterEqual(len(results), 1)
        self.assertLessEqual(scheduler.calls, 1)
        self.assertEqual(len(PortfolioSchedulerStore(self.fx.root).enumerate_queue_snapshot()), 1)
        self.assertLessEqual(len(errors), 1)


class MaterializationAdmissionSourceBoundaryTests(unittest.TestCase):
    def test_module_does_not_import_worker_provider_model_or_network_runtime(self) -> None:
        source = (SCRIPTS / "pm_materialization_admission_handoff.py").read_text(encoding="utf-8")
        for forbidden in (
            "import worker_adapter", "import workflow_orchestrator",
            "import socket", "requests", "urllib", "reasonix_cli_provider",
        ):
            self.assertNotIn(forbidden, source)
        self.assertNotIn("shell=True", source)


if __name__ == "__main__":
    unittest.main()
