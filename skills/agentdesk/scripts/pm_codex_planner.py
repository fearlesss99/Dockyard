"""Read-only Codex PM planner for the Dockyard requirement boundary.

Codex is the PM agent and produces the strict decomposition document.  The
local control plane only validates the result, maps its declared evidence to
the frozen capability policy, persists it, and enforces approval before any
execution decision.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from queue import Queue

from agent_capability_registry import AgentRoute, AgentCapabilityRegistry
from codex_cli_provider import CodexCliProvider
from dispatcher_gateway import (
    DispatchIdentity,
    DispatchRequest,
    DispatchNonZeroExitError,
    DispatchLaunchError,
    DispatchTimeoutError,
    ModelSelectionSnapshot,
    run_dispatch,
)
from core_types import TaskDifficulty
from pm_task_decomposer import (
    PmCodexTaskSpec,
    PmDecompositionResult,
    decompose_codex_tasks,
)
from portfolio_scheduler_store import BusinessPriority

__all__ = [
    "PmCodexPlanningError",
    "PmCodexPlanner",
]


class PmCodexPlanningError(RuntimeError):
    """Typed fail-closed error for PM-Codex planning."""


_SCHEMA = "agentdesk.pm-codex-plan/v1"
_ROOT_KEYS = frozenset({"schema_version", "plan_id", "tasks"})
_TASK_KEYS = frozenset({
    "title",
    "description",
    "dependencies",
    "execution_mode",
    "task_type",
    "capabilities",
    "rationale_keys",
    "difficulty",
    "risk",
    "business_priority",
})
_RATIONALE_OPTIONS = (
    frozenset({"scope.bounded", "scope.single_module", "scope.cross_module", "scope.architecture"}),
    frozenset({"clarity.explicit", "clarity.known_pattern", "clarity.ambiguous_constraints", "clarity.open_decision"}),
    frozenset({"concurrency.single_writer", "concurrency.shared_state", "concurrency.lock_cas", "concurrency.cross_process_recovery"}),
    frozenset({"contract.internal", "contract.additive", "contract.compatibility_sensitive", "contract.breaking_migration"}),
    frozenset({"impact.informational", "impact.local_failure", "impact.service_degradation", "impact.security_boundary", "impact.data_corruption"}),
    frozenset({"rollback.simple", "rollback.tested", "rollback.multi_step", "rollback.irreversible"}),
    frozenset({"dependency.none", "dependency.known", "dependency.cross_repository", "dependency.unresolved"}),
)
_ALLOWED_TASK_TYPES = frozenset({"implementation", "qa", "docs", "research", "integration"})
_ALLOWED_EXECUTION_MODES = frozenset({"parallel", "serial"})
_ALLOWED_RISKS = frozenset({"L0", "L1", "L2", "L3", "L4"})
_ALLOWED_DIFFICULTIES = frozenset(item.value for item in TaskDifficulty)
_REQUIRED_CONTEXT_PATHS = (
    "AGENTS.md",
    "docs/pm/ROLE-POLICIES.yaml",
    ".agentdesk/runtime/model-bindings.yaml",
)


def _run_async(coroutine):
    """Run one provider call from both HTTP threads and test event loops."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)

    result: Queue[tuple[bool, object]] = Queue(maxsize=1)

    def worker() -> None:
        try:
            result.put((True, asyncio.run(coroutine)))
        except BaseException as exc:  # pragma: no cover - defensive bridge
            result.put((False, exc))

    thread = threading.Thread(target=worker, name="dockyard-pm-codex", daemon=True)
    thread.start()
    thread.join()
    ok, value = result.get()
    if ok:
        return value
    raise value  # type: ignore[misc]


def _strict_text(value: object, field: str, maximum: int = 8192) -> str:
    if type(value) is not str or not value or value != value.strip() or len(value) > maximum:
        raise PmCodexPlanningError(f"pm_codex:{field}")
    if any(ord(character) < 32 and character not in "\n\t" for character in value):
        raise PmCodexPlanningError(f"pm_codex:{field}")
    return value


def _strict_string_tuple(value: object, field: str, *, maximum: int = 128) -> tuple[str, ...]:
    if type(value) is not list or not value:
        raise PmCodexPlanningError(f"pm_codex:{field}")
    result = tuple(_strict_text(item, field, maximum) for item in value)
    if len(set(result)) != len(result):
        raise PmCodexPlanningError(f"pm_codex:{field}")
    return result


def _decode_agent_message(stdout: bytes) -> str:
    try:
        text = stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PmCodexPlanningError("pm_codex:stdout_not_utf8") from exc
    messages: list[str] = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message" and type(item.get("text")) is str:
            messages.append(item["text"])
    if not messages:
        raise PmCodexPlanningError("pm_codex:agent_message_missing")
    candidate = messages[-1].strip()
    if candidate.startswith("```") and candidate.endswith("```"):
        lines = candidate.splitlines()
        if len(lines) < 3 or not lines[0].startswith("```") or lines[-1] != "```":
            raise PmCodexPlanningError("pm_codex:markdown_wrapper")
        candidate = "\n".join(lines[1:-1]).strip()
    return candidate


def _is_untrusted_proxy_certificate(error: DispatchNonZeroExitError) -> bool:
    """Recognize only the stable TLS certificate failures used for safe UX."""
    preview = error.stderr_preview.casefold()
    return (
        "invalid peer certificate" in preview
        or "unknownissuer" in preview
        or "unknown issuer" in preview
    )


def _decode_plan(stdout: bytes, plan_id: str) -> tuple[PmCodexTaskSpec, ...]:
    candidate = _decode_agent_message(stdout)
    try:
        raw = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise PmCodexPlanningError("pm_codex:plan_not_json") from exc
    if type(raw) is not dict or set(raw) != _ROOT_KEYS:
        raise PmCodexPlanningError("pm_codex:plan_shape")
    if raw["schema_version"] != _SCHEMA or raw["plan_id"] != plan_id:
        raise PmCodexPlanningError("pm_codex:plan_identity")
    tasks = raw["tasks"]
    if type(tasks) is not list or not 1 <= len(tasks) <= 32:
        raise PmCodexPlanningError("pm_codex:tasks")
    specs: list[PmCodexTaskSpec] = []
    for raw_task in tasks:
        if type(raw_task) is not dict or set(raw_task) != _TASK_KEYS:
            raise PmCodexPlanningError("pm_codex:task_shape")
        dependencies = raw_task["dependencies"]
        if type(dependencies) is not list:
            raise PmCodexPlanningError("pm_codex:dependencies")
        dependency_indexes = tuple(dependencies)
        if any(type(item) is not int or isinstance(item, bool) or item < 1 for item in dependency_indexes):
            raise PmCodexPlanningError("pm_codex:dependencies")
        try:
            priority = BusinessPriority(raw_task["business_priority"])
        except (TypeError, ValueError) as exc:
            raise PmCodexPlanningError("pm_codex:business_priority") from exc
        rationale_keys = _strict_string_tuple(raw_task["rationale_keys"], "rationale_keys")
        if any(key not in _RATIONALE_OPTIONS[index] for index, key in enumerate(rationale_keys)):
            raise PmCodexPlanningError("pm_codex:rationale_keys")
        execution_mode = raw_task["execution_mode"]
        task_type = raw_task["task_type"]
        risk = raw_task["risk"]
        difficulty = raw_task["difficulty"]
        if execution_mode not in _ALLOWED_EXECUTION_MODES or task_type not in _ALLOWED_TASK_TYPES or risk not in _ALLOWED_RISKS:
            raise PmCodexPlanningError("pm_codex:task_policy")
        if difficulty not in _ALLOWED_DIFFICULTIES:
            raise PmCodexPlanningError("pm_codex:difficulty")
        specs.append(PmCodexTaskSpec(
            title=_strict_text(raw_task["title"], "title", 256),
            description=_strict_text(raw_task["description"], "description"),
            dependencies=dependency_indexes,
            execution_mode=execution_mode,
            task_type=task_type,
            capabilities=_strict_string_tuple(raw_task["capabilities"], "capabilities"),
            rationale_keys=rationale_keys,
            difficulty=TaskDifficulty(difficulty),
            risk=risk,
            business_priority=priority,
        ))
    return tuple(specs)


@dataclass(frozen=True, slots=True)
class PmCodexPlanner:
    project_root: Path
    executable: str
    route: AgentRoute
    timeout_seconds: int = 180

    def plan(
        self,
        *,
        plan_id: str,
        requirement: str,
        snapshot_commit: str,
        command_id: str,
        registry: AgentCapabilityRegistry,
    ) -> PmDecompositionResult:
        if not isinstance(self.project_root, Path) or not self.project_root.is_absolute():
            raise PmCodexPlanningError("pm_codex:project_root")
        if self.route.provider_id != "codex" or self.route.difficulty.value != "expert":
            raise PmCodexPlanningError("pm_codex:route")
        _strict_text(plan_id, "plan_id", 128)
        _strict_text(requirement, "requirement", 32768)
        _strict_text(snapshot_commit, "snapshot_commit", 40)
        _strict_text(command_id, "command_id", 128)
        if type(registry) is not AgentCapabilityRegistry:
            raise PmCodexPlanningError("pm_codex:registry")
        digest = hashlib.sha256(
            f"{plan_id}:{command_id}:{snapshot_commit}".encode("utf-8")
        ).hexdigest()
        rationale_options = json.dumps(
            [sorted(options) for options in _RATIONALE_OPTIONS],
            ensure_ascii=False,
        )
        registered_bindings = tuple(
            (item.provider_id, item.model_id, tuple(value.value for value in item.supported_difficulties), item.capabilities)
            for item in registry.capabilities
            if item.enabled and item.provider_id in {"claude", "codex", "reasonix"}
        )
        if not registered_bindings:
            raise PmCodexPlanningError("pm_codex:registered_bindings_missing")
        registered_catalog = json.dumps(registered_bindings, ensure_ascii=False, separators=(",", ":"))
        required_context = ", ".join(_REQUIRED_CONTEXT_PATHS)
        prompt = f"""You are the PM-Codex planner for AgentDesk. Read the repository in the current workspace in read-only mode and decompose the user's requirement into implementation-ready task cards.

Return exactly one JSON object and no prose or markdown. The object must have this exact shape:
{{"schema_version":"{_SCHEMA}","plan_id":"{plan_id}","tasks":[{{"title":"...","description":"...","dependencies":[],"execution_mode":"parallel","task_type":"implementation","capabilities":["implementation"],"rationale_keys":["scope.bounded","clarity.explicit","concurrency.single_writer","contract.internal","impact.informational","rollback.simple","dependency.none"],"difficulty":"basic","risk":"L0","business_priority":"P1"}}]}}

Rules:
- Before planning, you MUST read these project-local files in read-only mode: {required_context}. Treat their policy and the registered binding catalog below as mandatory planning context.
- Use 1 to 32 tasks. dependencies are 1-based indexes of earlier tasks only.
- When the requirement explicitly asks for multiple Agents, parallel work, or independent workstreams, produce at least two task cards whenever the work can be safely separated. Keep independent cards in parallel and express genuine ordering with dependencies. Do not collapse distinct implementation, verification, integration, or documentation work into one card merely to minimize the task count.
- If the requirement is genuinely indivisible, one task is allowed; do not fabricate parallelism.
- Never include provider, model, shell commands, credentials, or file writes.
- Every task MUST declare difficulty as exactly one of basic, standard, advanced, expert. Difficulty is the PM's explicit cost/scope decision; it must not be lower than the evidence from rationale_keys. Prefer the lowest honest difficulty. Use basic for well-bounded, low-risk mechanical code or focused tests; use standard for routine known-pattern implementation; use advanced for cross-module/compatibility-sensitive work; reserve expert for architecture, high-risk review, security-sensitive work, or costly irreversible failure.
- PM never selects a provider or model. The local router alone chooses from the registered project bindings and rejects anything outside Reasonix, Claude Code, and Codex. For eligible basic coding/testing work it prefers Reasonix/deepseek-v4-flash; higher tiers and unsupported capabilities route only to an eligible registered binding.
- Use exactly seven rationale_keys, one from each ordered dimension: scope, clarity, concurrency, contract, impact, rollback, dependency. Use only the policy keys documented in the repository.
- The ordered policy key options are: {rationale_options}
- task_type must be one of implementation, qa, docs, research, integration; execution_mode must be parallel or serial; risk must be L0, L1, L2, L3, or L4.
- Use capabilities such as implementation, testing, planning, read, architecture, or code_review.
- Classify each task's scope and risk conservatively. The local control plane will enforce the frozen difficulty and capability policy from that evidence; it does not invent a replacement decomposition.
- Inspect the repository before deciding scope. Do not edit files, commit, or call external services.

Registered project bindings (informational; never copy provider/model into output): {registered_catalog}

Repository snapshot: {snapshot_commit}
User requirement:
{requirement}
"""
        snapshot = ModelSelectionSnapshot(
            required_model_tier="expert",
            required_model_capabilities=("planning", "read"),
            model_binding_id=self.route.agent_id,
            selected_model_provider="codex",
            selected_model_id=self.route.model_id,
            selected_model_tier="expert",
            selected_deliberation_tier=self.route.deliberation_tier,
            selected_context_window_tokens=1_000_000,
            selected_model_capabilities=("planning", "read"),
            model_degradation_approval_id=None,
        )
        request = DispatchRequest(
            identity=DispatchIdentity(
                task_id=f"PM-{digest[:24]}",
                revision=1,
                attempt=1,
                dispatch_id=f"PM-DSP-{digest[:24]}",
            ),
            workspace=self.project_root,
            prompt=prompt,
            model_selection=snapshot,
            timeout_seconds=self.timeout_seconds,
        )
        provider = CodexCliProvider("codex", self.executable, "read-only")
        # A freshly started local Runner can race the first CLI process with
        # proxy/socket readiness.  Retry exactly once at this side-effect-free
        # planning boundary; the plan is not persisted until this returns.
        # Persistent failures still fail closed with the same stable error.
        last_error: Exception | None = None
        for _attempt in range(2):
            try:
                result = _run_async(run_dispatch(request, {"codex": provider}))
                break
            except DispatchNonZeroExitError as exc:
                if _is_untrusted_proxy_certificate(exc):
                    raise PmCodexPlanningError(
                        "pm_codex:proxy_certificate_untrusted"
                    ) from exc
                last_error = exc
            except (DispatchLaunchError, DispatchTimeoutError) as exc:
                last_error = exc
            except Exception as exc:
                raise PmCodexPlanningError("pm_codex:dispatch_failed") from exc
        else:
            assert last_error is not None
            raise PmCodexPlanningError("pm_codex:dispatch_failed") from last_error
        specs = _decode_plan(result.stdout, plan_id)
        try:
            return decompose_codex_tasks(
                plan_id=plan_id,
                task_specs=specs,
                snapshot_commit=snapshot_commit,
                registry=registry,
            )
        except Exception as exc:
            raise PmCodexPlanningError("pm_codex:local_policy_rejected") from exc
