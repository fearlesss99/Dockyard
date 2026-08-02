from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "agentdesk" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pm_external_worker_runtime as runtime
import validate_runtime
from core_types import WorkerKind
from portfolio_scheduler_worker_handoff_runtime import (
    AdmittedDispatchCompletionResult,
    AdmittedDispatchStartRequest,
    AdmittedDispatchStartOutcome,
    AdmittedDispatchStartResult,
    ScheduledDispatchHandoffPhase,
)
from workflow_orchestrator import (
    DispatchCycleRequest,
    DispatchCycleResult,
    WorkflowOrchestrator,
)


class _Clock:
    def now(self):
        raise AssertionError
    def monotonic(self):
        return 0.0
    async def sleep(self, seconds):
        return None


def _write_config(root: Path, executable: Path) -> None:
    path = root / ".agentdesk" / "runtime"
    path.mkdir(parents=True)
    data = {
        "schema_version": runtime.CONFIG_SCHEMA,
        "updated_at": "2026-08-02T11:00:00Z",
        "workers": {
            "DEV": {
                "endpoint_id": "EXT-DEV-1", "role_id": "DEV",
                "provider_id": "claude", "model_binding_id": "BIND-1",
                "executable": str(executable), "permission_mode": "default",
                "status": "verified",
            }
        },
    }
    (path / "external-workers.yaml").write_text(
        json.dumps(data, indent=2) + "\n", encoding="utf-8"
    )


def _handoff(root: Path) -> AdmittedDispatchStartRequest:
    cycle = object.__new__(DispatchCycleRequest)
    identity = SimpleNamespace(task_id="TC-026", revision=1, attempt=1, dispatch_id="DSP-26")
    snapshot = SimpleNamespace(selected_model_provider="claude", model_binding_id="BIND-1")
    dispatch = SimpleNamespace(identity=identity, workspace=root, model_selection=snapshot)
    object.__setattr__(cycle, "dispatch_request", dispatch)
    object.__setattr__(cycle, "holder_instance_id", "INSTANCE-1")
    return AdmittedDispatchStartRequest(
        "agentdesk.scheduler-worker-handoff-runtime/v1", "OP-HANDOFF", str(root),
        "Q-1", "RCP-1", "TC-026", 1, 1, "DSP-26", "EVT-26", "MSG-26",
        1, "sha256:" + "1" * 64, "HNDF-26", "WT-26", "WSL-26", 1,
        "INSTANCE-1", "2026-08-02T11:00:00Z", cycle,
    )


def _request(root: Path) -> runtime.PmExternalWorkerRequest:
    return runtime.PmExternalWorkerRequest(
        "OP-PM-1", str(root), "DEV", "PM", "2026-08-02T11:01:00Z",
        _handoff(root),
    )


class PmExternalWorkerRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_external_delivery_enters_mad_acceptance_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            dispatch_cycle_result = object.__new__(DispatchCycleResult)
            worker_run = SimpleNamespace(
                dispatch_cycle_result=dispatch_cycle_result,
                receipt=SimpleNamespace(),
            )
            plan = runtime.PmExternalAcceptancePlan(
                audit_input=None,
                acceptance_transition_request=None,
                integration_transition_request=None,
                worker_kind=WorkerKind.BASIC_AGENT,
                holder_instance_id="INSTANCE-1",
            )
            acceptance_request = object()
            acceptance_result = object()
            orchestrator = WorkflowOrchestrator(root, _Clock())
            with (
                patch.object(runtime, "run_pm_external_worker", AsyncMock(return_value=worker_run)),
                patch.object(runtime, "AcceptanceCycleRequest", return_value=acceptance_request) as build,
                patch.object(WorkflowOrchestrator, "run_acceptance_cycle", AsyncMock(return_value=acceptance_result)) as accept,
            ):
                result = await runtime.run_pm_external_worker_acceptance(
                    _request(root), plan, {}, orchestrator, object()
                )
            self.assertIs(result.worker_run, worker_run)
            self.assertIs(result.acceptance_cycle, acceptance_result)
            build.assert_called_once()
            accept.assert_awaited_once_with(acceptance_request, ANY)

    async def test_replayed_external_delivery_cannot_forge_mad_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            worker_run = SimpleNamespace(
                dispatch_cycle_result=None,
                receipt=SimpleNamespace(),
            )
            plan = runtime.PmExternalAcceptancePlan(
                None, None, None, WorkerKind.BASIC_AGENT, "INSTANCE-1"
            )
            orchestrator = WorkflowOrchestrator(root, _Clock())
            accept = AsyncMock()
            with (
                patch.object(runtime, "run_pm_external_worker", AsyncMock(return_value=worker_run)),
                patch.object(WorkflowOrchestrator, "run_acceptance_cycle", accept),
            ):
                with self.assertRaises(runtime.PmExternalWorkerConflictError):
                    await runtime.run_pm_external_worker_acceptance(
                        _request(root), plan, {}, orchestrator, object()
                    )
            accept.assert_not_awaited()

    async def test_fresh_delivery_result_is_returned_to_pm_and_bound_in_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            executable = root / "claude.exe"
            executable.write_bytes(b"")
            _write_config(root, executable)
            provider = SimpleNamespace(provider_id="claude", executable=str(executable))
            start_result = AdmittedDispatchStartResult(
                "agentdesk.scheduler-worker-handoff-runtime/v1", "OP-HANDOFF",
                "TC-026", 1, 1, "DSP-26", "HNDF-26", "GEN-1",
                ScheduledDispatchHandoffPhase.FINALIZED,
                AdmittedDispatchStartOutcome.STARTED, "FINALIZED",
                "sha256:" + "2" * 64,
            )
            delivery_result = object.__new__(DispatchCycleResult)
            object.__setattr__(delivery_result, "delivery_receipt", SimpleNamespace(
                implementation_commit="a" * 40, report_commit="b" * 40,
            ))
            completion = AdmittedDispatchCompletionResult(
                start_result, delivery_result
            )
            with patch.object(
                runtime,
                "start_admitted_dispatch_completion",
                AsyncMock(return_value=completion),
            ):
                result = await runtime.run_pm_external_worker(
                    _request(root), {"claude": provider},
                    WorkflowOrchestrator(root, _Clock()),
                )
            self.assertIs(result.dispatch_cycle_result, delivery_result)
            self.assertTrue(result.receipt.delivery_result_available)
            self.assertEqual(result.receipt.implementation_commit, "a" * 40)
            self.assertEqual(result.receipt.report_commit, "b" * 40)

    async def test_happy_path_returns_and_persists_compact_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            executable = root / "claude.exe"
            executable.write_bytes(b"")
            _write_config(root, executable)
            provider = SimpleNamespace(provider_id="claude", executable=str(executable))
            result = AdmittedDispatchStartResult(
                "agentdesk.scheduler-worker-handoff-runtime/v1", "OP-HANDOFF",
                "TC-026", 1, 1, "DSP-26", "HNDF-26", "GEN-1",
                ScheduledDispatchHandoffPhase.FINALIZED,
                AdmittedDispatchStartOutcome.STARTED, "FINALIZED",
                "sha256:" + "2" * 64,
            )
            completion = AdmittedDispatchCompletionResult(result, None)
            with patch.object(runtime, "start_admitted_dispatch_completion", AsyncMock(return_value=completion)) as start:
                run_result = await runtime.run_pm_external_worker(
                    _request(root), {"claude": provider}, WorkflowOrchestrator(root, _Clock())
                )
            receipt = run_result.receipt
            self.assertEqual(receipt.return_channel, "synchronous_pm")
            self.assertEqual(receipt.handoff_outcome, "STARTED")
            start.assert_awaited_once()
            raw = (root / ".agentdesk/runtime/external-worker-receipts/DSP-26.json").read_text(encoding="utf-8")
            self.assertNotIn("prompt", raw)
            self.assertNotIn("stdout", raw)
            self.assertNotIn("stderr", raw)

    async def test_identical_replay_does_not_restart_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            executable = root / "claude.exe"
            executable.write_bytes(b"")
            _write_config(root, executable)
            provider = SimpleNamespace(provider_id="claude", executable=str(executable))
            result = AdmittedDispatchStartResult(
                "agentdesk.scheduler-worker-handoff-runtime/v1", "OP-HANDOFF",
                "TC-026", 1, 1, "DSP-26", "HNDF-26", "GEN-1",
                ScheduledDispatchHandoffPhase.FINALIZED,
                AdmittedDispatchStartOutcome.STARTED, "FINALIZED",
                "sha256:" + "2" * 64,
            )
            start = AsyncMock(return_value=AdmittedDispatchCompletionResult(result, None))
            with patch.object(runtime, "start_admitted_dispatch_completion", start):
                first = await runtime.run_pm_external_worker(_request(root), {"claude": provider}, WorkflowOrchestrator(root, _Clock()))
                second = await runtime.run_pm_external_worker(_request(root), {"claude": provider}, WorkflowOrchestrator(root, _Clock()))
            self.assertEqual(first.receipt, second.receipt)
            self.assertEqual(start.await_count, 1)

    async def test_divergent_operation_is_typed_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            executable = root / "claude.exe"
            executable.write_bytes(b"")
            _write_config(root, executable)
            provider = SimpleNamespace(provider_id="claude", executable=str(executable))
            result = AdmittedDispatchStartResult(
                "agentdesk.scheduler-worker-handoff-runtime/v1", "OP-HANDOFF",
                "TC-026", 1, 1, "DSP-26", "HNDF-26", "GEN-1",
                ScheduledDispatchHandoffPhase.FINALIZED,
                AdmittedDispatchStartOutcome.STARTED, "FINALIZED",
                "sha256:" + "2" * 64,
            )
            with patch.object(runtime, "start_admitted_dispatch_completion", AsyncMock(return_value=AdmittedDispatchCompletionResult(result, None))):
                await runtime.run_pm_external_worker(_request(root), {"claude": provider}, WorkflowOrchestrator(root, _Clock()))
                bad = runtime.PmExternalWorkerRequest("OP-PM-2", str(root), "DEV", "PM", "2026-08-02T11:01:00Z", _handoff(root))
                with self.assertRaises(runtime.PmExternalWorkerConflictError):
                    await runtime.run_pm_external_worker(bad, {"claude": provider}, WorkflowOrchestrator(root, _Clock()))

    async def test_model_binding_mismatch_fails_before_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            executable = root / "claude.exe"
            executable.write_bytes(b"")
            _write_config(root, executable)
            request = _request(root)
            request.handoff_request.dispatch_cycle_request.dispatch_request.model_selection.model_binding_id = "OTHER"
            start = AsyncMock()
            with patch.object(runtime, "start_admitted_dispatch_completion", start):
                with self.assertRaises(runtime.PmExternalWorkerConflictError):
                    await runtime.run_pm_external_worker(
                        request,
                        {"claude": SimpleNamespace(provider_id="claude", executable=str(executable))},
                        WorkflowOrchestrator(root, _Clock()),
                    )
            start.assert_not_awaited()

    async def test_pm_cannot_be_external_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            executable = root / "claude.exe"
            executable.write_bytes(b"")
            _write_config(root, executable)
            request = runtime.PmExternalWorkerRequest(
                "OP-PM-1", str(root), "PM", "PM", "2026-08-02T11:01:00Z", _handoff(root)
            )
            with self.assertRaises(runtime.PmExternalWorkerInputError):
                await runtime.run_pm_external_worker(
                    request, {}, WorkflowOrchestrator(root, _Clock())
                )

    def test_endpoint_config_is_exact_and_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            executable = root / "claude.exe"
            executable.write_bytes(b"")
            _write_config(root, executable)
            endpoint = runtime.load_external_worker_endpoint(root, "DEV")
            self.assertEqual(endpoint.provider_id, "claude")
            self.assertEqual(endpoint.status, "verified")

    def test_runtime_validator_accepts_external_worker_without_codex_thread(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            executable = root / "claude.exe"
            executable.write_bytes(b"")
            _write_config(root, executable)
            (root / "worker").mkdir()
            roles = {
                "PM": validate_runtime.Role(
                    "PM", "PM", "项目经理", "Active"
                ),
                "DEV": validate_runtime.Role(
                    "R1", "DEV", "开发工程师", "Active"
                )
            }
            routes_path = root / ".agentdesk/runtime/routes.yaml"
            routes_path.write_text(
                json.dumps({
                    "schema_version": validate_runtime.ROUTES_SCHEMA,
                    "routes": {
                        "PM": {
                            "role_no": "PM", "role_name": "项目经理",
                            "expected_title": "PM . 项目经理",
                            "actual_title": "PM . 项目经理",
                            "status": "verified",
                            "verified_at": "2026-08-02T11:00:00Z",
                            "host_id": "local", "worktree": str(root),
                            "thread_id": "019fc1f0-707b-7ab3-ad6a-5a95024001aa",
                            "transport": "codex",
                        }
                    },
                }),
                encoding="utf-8",
            )
            reporter = validate_runtime.Reporter()
            _, endpoint_ids = validate_runtime._validate_routes(
                root, roles, {"DEV"}, reporter
            )
            self.assertEqual(reporter.errors, 0)
            self.assertIn("DEV", endpoint_ids)
            self.assertEqual(endpoint_ids["DEV"], "EXT-DEV-1")


if __name__ == "__main__":
    unittest.main()
