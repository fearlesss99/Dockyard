"""Pure PM requirement decomposition and difficulty-based route planning."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from agent_capability_registry import AgentCapabilityRegistry, AgentRoute
from core_types import TaskDifficulty
from difficulty_assessor import assess_task_difficulty
from dockyard_plan_store import DockyardPlanTask
from portfolio_scheduler_store import BusinessPriority
from pm_repository_context import PmRepositoryInventory

__all__ = [
    "PmTaskRoutingOverride",
    "PmTaskBlueprint",
    "PmDecompositionResult",
    "PmDecompositionError",
    "PmDecompositionInputError",
    "decompose_requirement",
]


class PmDecompositionError(ValueError):
    """Base error for PM decomposition."""


class PmDecompositionInputError(PmDecompositionError):
    """Requirement or override input is malformed."""


_SHA = frozenset("0123456789abcdef")
_BULLET = re.compile(r"^(?:[-*•]|\d+[.)])\s+")
_DIFFICULTY_RISK = {
    TaskDifficulty.BASIC: "L0",
    TaskDifficulty.STANDARD: "L1",
    TaskDifficulty.ADVANCED: "L2",
    TaskDifficulty.EXPERT: "L3",
}
_BUDGET = {
    TaskDifficulty.BASIC: 16000,
    TaskDifficulty.STANDARD: 32000,
    TaskDifficulty.ADVANCED: 64000,
    TaskDifficulty.EXPERT: 128000,
}


def _text(value: object, field: str, maximum: int = 8192) -> str:
    if type(value) is not str or not value or value != value.strip() or len(value) > maximum:
        raise PmDecompositionInputError(f"{field} is invalid")
    if any(ord(character) < 32 for character in value):
        raise PmDecompositionInputError(f"{field} contains control characters")
    return value


def _document_text(value: object, field: str, maximum: int = 8192) -> str:
    if type(value) is not str or not value or value != value.strip() or len(value) > maximum:
        raise PmDecompositionInputError(f"{field} is invalid")
    if any(ord(character) < 32 and character not in "\r\n\t" for character in value):
        raise PmDecompositionInputError(f"{field} contains control characters")
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _sha(value: object, field: str) -> str:
    text = _text(value, field, 64)
    if len(text) != 40 or any(character not in _SHA for character in text):
        raise PmDecompositionInputError(f"{field} is invalid")
    return text


@dataclass(frozen=True, slots=True)
class PmTaskRoutingOverride:
    task_hint: str
    forced_agent: str | None = None
    preferred_agent: str | None = None
    forbidden_agents: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.task_hint, "task_hint", 256)
        if self.forced_agent is not None:
            _text(self.forced_agent, "forced_agent", 256)
        if self.preferred_agent is not None:
            _text(self.preferred_agent, "preferred_agent", 256)
        if type(self.forbidden_agents) is not tuple:
            raise PmDecompositionInputError("forbidden_agents must be tuple")
        for agent_id in self.forbidden_agents:
            _text(agent_id, "forbidden_agent", 256)
        if len(set(self.forbidden_agents)) != len(self.forbidden_agents):
            raise PmDecompositionInputError("forbidden_agents contain duplicates")


@dataclass(frozen=True, slots=True)
class PmTaskBlueprint:
    task_id: str
    title: str
    description: str
    dependencies: tuple[str, ...]
    execution_mode: str
    task_type: str
    difficulty: TaskDifficulty
    rationale_keys: tuple[str, ...]
    risk: str
    capabilities: tuple[str, ...]
    route: AgentRoute
    plan_task: DockyardPlanTask


@dataclass(frozen=True, slots=True)
class PmDecompositionResult:
    schema_version: str
    plan_id: str
    tasks: tuple[PmTaskBlueprint, ...]
    max_concurrency: int
    content_digest: str
    repository_digest: str | None


def _contains(text: str, *terms: str) -> bool:
    return any(term in text for term in terms)


def _rationale(
    segment: str,
    full_requirement: str,
    repository_inventory: PmRepositoryInventory | None,
) -> tuple[str, ...]:
    text = f"{segment} {full_requirement}"
    broad_scope = repository_inventory is not None and _contains(text, "项目", "全仓", "全部")
    return (
        "scope.architecture" if _contains(text, "架构", "重构", "迁移") else
        "scope.cross_module" if _contains(text, "跨模块", "多个模块", "全仓", "仓库") or broad_scope else
        "scope.single_module" if _contains(text, "组件", "模块", "接口", "前端", "后端") else
        "scope.bounded",
        "clarity.open_decision" if _contains(text, "不确定", "选型", "设计") else
        "clarity.ambiguous_constraints" if _contains(text, "约束", "兼容", "边界") else
        "clarity.known_pattern" if _contains(text, "实现", "增加", "接入", "修复") else
        "clarity.explicit",
        "concurrency.cross_process_recovery" if _contains(text, "进程", "恢复", "崩溃") else
        "concurrency.lock_cas" if _contains(text, "并发", "锁", "cas", "CAS") else
        "concurrency.shared_state" if _contains(text, "共享", "状态") else
        "concurrency.single_writer",
        "contract.breaking_migration" if _contains(text, "破坏", "迁移") else
        "contract.compatibility_sensitive" if _contains(text, "兼容", "接口") else
        "contract.additive" if _contains(text, "协议", "契约", "API") else
        "contract.internal",
        "impact.security_boundary" if _contains(text, "权限", "安全", "密钥") else
        "impact.data_corruption" if _contains(text, "数据丢失", "损坏") else
        "impact.service_degradation" if _contains(text, "服务", "不可用") else
        "impact.local_failure" if _contains(text, "失败", "错误") else
        "impact.informational",
        "rollback.irreversible" if _contains(text, "删除", "不可逆") else
        "rollback.multi_step" if _contains(text, "多步", "闭环", "流水线") else
        "rollback.tested" if _contains(text, "测试", "验证") else
        "rollback.simple",
        "dependency.unresolved" if _contains(text, "未知依赖", "待确认") else
        "dependency.cross_repository" if _contains(text, "跨仓库", "外部仓库") else
        "dependency.known" if _contains(text, "依赖", "provider", "模型") else
        "dependency.none",
    )


def _segments(requirement: str) -> tuple[str, ...]:
    raw_lines = tuple(line.strip() for line in requirement.splitlines() if line.strip())
    if len(raw_lines) <= 1 or not all(_BULLET.match(line) for line in raw_lines):
        return (requirement,)
    return tuple(_BULLET.sub("", line) for line in raw_lines)


def _override_for(segment: str, overrides: tuple[PmTaskRoutingOverride, ...]) -> PmTaskRoutingOverride | None:
    lowered = segment.casefold()
    return next((item for item in overrides if item.task_hint.casefold() in lowered), None)


def decompose_requirement(
    *,
    plan_id: str,
    requirement: str,
    snapshot_commit: str,
    registry: AgentCapabilityRegistry,
    routing_overrides: tuple[PmTaskRoutingOverride, ...] = (),
    repository_inventory: PmRepositoryInventory | None = None,
) -> PmDecompositionResult:
    """Split a requirement into bounded tasks and choose routes deterministically."""
    _text(plan_id, "plan_id", 128)
    requirement = _document_text(requirement, "requirement")
    snapshot_commit = _sha(snapshot_commit, "snapshot_commit")
    if type(registry) is not AgentCapabilityRegistry:
        raise PmDecompositionInputError("registry is invalid")
    if repository_inventory is not None and type(repository_inventory) is not PmRepositoryInventory:
        raise PmDecompositionInputError("repository_inventory is invalid")
    if type(routing_overrides) is not tuple or any(
        type(item) is not PmTaskRoutingOverride for item in routing_overrides
    ):
        raise PmDecompositionInputError("routing_overrides are invalid")
    segments = _segments(requirement)
    blueprints: list[PmTaskBlueprint] = []
    for index, segment in enumerate(segments, start=1):
        digest = hashlib.sha256(f"{plan_id}:{index}:{segment}".encode("utf-8")).hexdigest()
        task_id = f"TC-{digest[:20]}"
        rationale_keys = _rationale(segment, requirement, repository_inventory)
        assessment = assess_task_difficulty(
            assessment_id=f"ASM-{digest[:24]}",
            task_id=task_id,
            revision=1,
            rationale_keys=rationale_keys,
        )
        difficulty = assessment.selected_difficulty
        capabilities = (
            "testing" if _contains(segment, "测试", "验证", "审议", "验收") else "implementation",
        )
        override = _override_for(segment, routing_overrides)
        declared_capabilities = frozenset(
            capability
            for item in registry.capabilities
            for capability in item.capabilities
        )
        route_capabilities = capabilities if set(capabilities).issubset(declared_capabilities) else ()
        route = registry.route(
            difficulty,
            route_capabilities,
            forced_agent=None if override is None else override.forced_agent,
            preferred_agent=None if override is None else override.preferred_agent,
            forbidden_agents=() if override is None else override.forbidden_agents,
        )
        dependencies = ()
        if index > 1 and _contains(segment, "测试", "验证", "审议", "验收", "集成", "文档"):
            dependencies = (blueprints[-1].task_id,)
        execution_mode = "serial" if dependencies else "parallel"
        task_type = "validation" if "testing" in capabilities else "implementation"
        plan_task = DockyardPlanTask(
            task_id,
            segment[:256],
            segment,
            dependencies,
            execution_mode,
            "R1",
            task_type,
            route.provider_id,
            route.model_id,
            difficulty.value,
            _BUDGET[difficulty],
            3,
            snapshot_commit,
            BusinessPriority.P1,
            rationale_keys,
            difficulty,
            None,
            None,
            _DIFFICULTY_RISK[difficulty],
            capabilities,
            None,
            0,
            1,
        )
        blueprints.append(PmTaskBlueprint(
            task_id, segment[:256], segment, dependencies, execution_mode, task_type,
            difficulty, rationale_keys, _DIFFICULTY_RISK[difficulty], capabilities,
            route, plan_task,
        ))
    serialized = json.dumps(
        {
            "repository_digest": None if repository_inventory is None else repository_inventory.content_digest,
            "tasks": tuple((item.task_id, item.dependencies, item.route.agent_id, item.difficulty.value) for item in blueprints),
        },
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    return PmDecompositionResult(
        "agentdesk.pm-decomposition/v1",
        plan_id,
        tuple(blueprints),
        registry.max_concurrency,
        hashlib.sha256(serialized).hexdigest(),
        None if repository_inventory is None else repository_inventory.content_digest,
    )
