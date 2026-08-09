"""Deterministic Agent capability and route selection for PM planning.

The registry is deliberately separate from provider execution.  It only
answers which already-configured binding may own a task; process startup,
leases, retries, and approvals remain the responsibility of existing runtime
owners.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import stat

from core_types import TaskDifficulty
import select_model as _select_model

__all__ = [
    "AgentCapability",
    "AgentRoute",
    "AgentCapabilityRegistry",
    "AgentRoutingError",
    "AgentRoutingInputError",
    "AgentRoutingUnavailableError",
]


class AgentRoutingError(ValueError):
    """Base error for deterministic PM route selection."""


class AgentRoutingInputError(AgentRoutingError):
    """A route request or capability declaration is malformed."""


class AgentRoutingUnavailableError(AgentRoutingError):
    """No enabled binding satisfies the requested task constraints."""


_DIFFICULTIES = (
    TaskDifficulty.BASIC,
    TaskDifficulty.STANDARD,
    TaskDifficulty.ADVANCED,
    TaskDifficulty.EXPERT,
)
_DELIBERATION = frozenset({"efficient", "balanced", "deep"})
_PROVIDER_ORDER = {
    TaskDifficulty.BASIC: {"reasonix": 0, "claude": 1, "codex": 2},
    TaskDifficulty.STANDARD: {"claude": 0, "codex": 1, "reasonix": 2},
    TaskDifficulty.ADVANCED: {"codex": 0, "claude": 1, "reasonix": 2},
    TaskDifficulty.EXPERT: {"codex": 0, "claude": 1, "reasonix": 2},
}


def _clean_text(value: object, field: str, maximum: int = 256) -> str:
    if type(value) is not str or not value or value != value.strip() or len(value) > maximum:
        raise AgentRoutingInputError(f"{field} is invalid")
    if any(ord(character) < 32 for character in value):
        raise AgentRoutingInputError(f"{field} contains control characters")
    return value


def _difficulty_index(value: TaskDifficulty) -> int:
    try:
        return _DIFFICULTIES.index(value)
    except ValueError as exc:
        raise AgentRoutingInputError("difficulty is invalid") from exc


@dataclass(frozen=True, slots=True)
class AgentCapability:
    """One immutable, already-configured provider/model capability."""

    schema_version: str
    agent_id: str
    provider_id: str
    model_id: str
    supported_difficulties: tuple[TaskDifficulty, ...]
    capabilities: tuple[str, ...]
    deliberation_tier: str
    max_concurrency: int = 4
    can_review: bool = False
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.schema_version != "agentdesk.agent-capability/v1":
            raise AgentRoutingInputError("schema_version is invalid")
        _clean_text(self.agent_id, "agent_id")
        _clean_text(self.provider_id, "provider_id", 64)
        _clean_text(self.model_id, "model_id")
        if type(self.supported_difficulties) is not tuple or not self.supported_difficulties:
            raise AgentRoutingInputError("supported_difficulties is invalid")
        if len(set(self.supported_difficulties)) != len(self.supported_difficulties):
            raise AgentRoutingInputError("supported_difficulties contains duplicates")
        for difficulty in self.supported_difficulties:
            if type(difficulty) is not TaskDifficulty:
                raise AgentRoutingInputError("supported_difficulties contains invalid value")
        if type(self.capabilities) is not tuple:
            raise AgentRoutingInputError("capabilities is invalid")
        for capability in self.capabilities:
            _clean_text(capability, "capability", 128)
        if len(set(self.capabilities)) != len(self.capabilities):
            raise AgentRoutingInputError("capabilities contains duplicates")
        if self.deliberation_tier not in _DELIBERATION:
            raise AgentRoutingInputError("deliberation_tier is invalid")
        if type(self.max_concurrency) is not int or isinstance(self.max_concurrency, bool):
            raise AgentRoutingInputError("max_concurrency is invalid")
        if not 1 <= self.max_concurrency <= 64:
            raise AgentRoutingInputError("max_concurrency is out of range")
        if type(self.can_review) is not bool or type(self.enabled) is not bool:
            raise AgentRoutingInputError("capability flags are invalid")


@dataclass(frozen=True, slots=True)
class AgentRoute:
    """The frozen route evidence attached to one PM task."""

    schema_version: str
    agent_id: str
    provider_id: str
    model_id: str
    difficulty: TaskDifficulty
    deliberation_tier: str
    max_concurrency: int
    selection_source: str
    rationale: str

    def __post_init__(self) -> None:
        if self.schema_version != "agentdesk.agent-route/v1":
            raise AgentRoutingInputError("schema_version is invalid")
        _clean_text(self.agent_id, "agent_id")
        _clean_text(self.provider_id, "provider_id", 64)
        _clean_text(self.model_id, "model_id")
        if type(self.difficulty) is not TaskDifficulty:
            raise AgentRoutingInputError("difficulty is invalid")
        if self.deliberation_tier not in _DELIBERATION:
            raise AgentRoutingInputError("deliberation_tier is invalid")
        if type(self.max_concurrency) is not int or self.max_concurrency < 1:
            raise AgentRoutingInputError("max_concurrency is invalid")
        if self.selection_source not in {"automatic", "preferred", "forced"}:
            raise AgentRoutingInputError("selection_source is invalid")
        _clean_text(self.rationale, "rationale", 512)


class AgentCapabilityRegistry:
    """Choose one eligible binding without silently downgrading difficulty."""

    def __init__(self, capabilities: tuple[AgentCapability, ...], max_concurrency: int = 4) -> None:
        if type(capabilities) is not tuple:
            raise AgentRoutingInputError("capabilities must be a tuple")
        if any(type(item) is not AgentCapability for item in capabilities):
            raise AgentRoutingInputError("capabilities contain an invalid item")
        if len({item.agent_id for item in capabilities}) != len(capabilities):
            raise AgentRoutingInputError("agent_id values must be unique")
        if type(max_concurrency) is not int or isinstance(max_concurrency, bool) or max_concurrency < 1:
            raise AgentRoutingInputError("max_concurrency is invalid")
        self._capabilities = capabilities
        self._max_concurrency = max_concurrency

    @property
    def max_concurrency(self) -> int:
        return self._max_concurrency

    @property
    def capabilities(self) -> tuple[AgentCapability, ...]:
        return self._capabilities

    def route(
        self,
        difficulty: TaskDifficulty,
        required_capabilities: tuple[str, ...] = (),
        *,
        forced_agent: str | None = None,
        preferred_agent: str | None = None,
        forbidden_agents: tuple[str, ...] = (),
    ) -> AgentRoute:
        if type(difficulty) is not TaskDifficulty:
            raise AgentRoutingInputError("difficulty is invalid")
        if type(required_capabilities) is not tuple:
            raise AgentRoutingInputError("required_capabilities must be tuple")
        for capability in required_capabilities:
            _clean_text(capability, "required_capability", 128)
        if len(set(required_capabilities)) != len(required_capabilities):
            raise AgentRoutingInputError("required_capabilities contain duplicates")
        if type(forbidden_agents) is not tuple:
            raise AgentRoutingInputError("forbidden_agents must be tuple")
        for agent_id in forbidden_agents:
            _clean_text(agent_id, "forbidden_agent")
        if len(set(forbidden_agents)) != len(forbidden_agents):
            raise AgentRoutingInputError("forbidden_agents contain duplicates")
        if forced_agent is not None:
            _clean_text(forced_agent, "forced_agent")
        if preferred_agent is not None:
            _clean_text(preferred_agent, "preferred_agent")
        if forced_agent is not None and forced_agent in forbidden_agents:
            raise AgentRoutingUnavailableError("forced agent is forbidden")

        def eligible(item: AgentCapability) -> bool:
            capabilities = frozenset(item.capabilities)
            supports_required = all(
                capability in capabilities
                or (capability == "implementation" and "coding" in capabilities)
                for capability in required_capabilities
            )
            return (
                item.enabled
                and item.agent_id not in forbidden_agents
                and difficulty in item.supported_difficulties
                and supports_required
            )

        if forced_agent is not None:
            forced = next((item for item in self._capabilities if item.agent_id == forced_agent), None)
            if forced is None or not eligible(forced):
                raise AgentRoutingUnavailableError("forced agent is unavailable")
            selected = forced
            source = "forced"
        else:
            candidates = tuple(item for item in self._capabilities if eligible(item))
            if not candidates:
                raise AgentRoutingUnavailableError("no capable agent is available")
            selected = next((item for item in candidates if item.agent_id == preferred_agent), None)
            source = "preferred" if selected is not None else "automatic"
            if selected is None:
                selected = sorted(
                    candidates,
                    key=lambda item: (
                        _PROVIDER_ORDER[difficulty].get(item.provider_id, 99),
                        abs(_difficulty_index(difficulty) - max(_difficulty_index(value) for value in item.supported_difficulties)),
                        item.agent_id,
                    ),
                )[0]
        return AgentRoute(
            "agentdesk.agent-route/v1",
            selected.agent_id,
            selected.provider_id,
            selected.model_id,
            difficulty,
            selected.deliberation_tier,
            min(selected.max_concurrency, self._max_concurrency),
            source,
            f"{source} route for {difficulty.value} task",
        )

    @classmethod
    def from_project(
        cls,
        project_root: Path,
        provider_ids: tuple[str, ...] = (),
        max_concurrency: int = 4,
    ) -> "AgentCapabilityRegistry | None":
        """Build a registry from existing model bindings; never invokes a provider."""
        if not isinstance(project_root, Path) or not project_root.is_absolute():
            raise AgentRoutingInputError("project_root is invalid")
        bindings_path = project_root / _select_model.MODEL_BINDINGS_FILE
        try:
            bindings_metadata = bindings_path.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise AgentRoutingInputError("model bindings cannot be inspected") from exc
        if not stat.S_ISREG(bindings_metadata.st_mode):
            raise AgentRoutingInputError("model bindings must be a regular file")
        try:
            document = _select_model._load_worktree_json_object(
                project_root, _select_model.MODEL_BINDINGS_FILE, "model bindings"
            )
            bindings = _select_model._validated_bindings(document)
        except _select_model.SelectionError as exc:
            raise AgentRoutingInputError("model bindings are invalid") from exc
        allowed = frozenset(provider_ids)
        result: list[AgentCapability] = []
        for binding in bindings:
            provider_id = binding["provider"]
            if provider_id not in {"claude", "codex", "reasonix"}:
                continue
            if allowed and provider_id not in allowed:
                continue
            tier = binding["tier"]
            try:
                tier_index = _DIFFICULTIES.index(TaskDifficulty(tier))
            except (ValueError, TypeError):
                continue
            supported = _DIFFICULTIES[: tier_index + 1]
            result.append(AgentCapability(
                "agentdesk.agent-capability/v1",
                binding["binding_id"],
                provider_id,
                binding["model_id"],
                supported,
                tuple(sorted(binding["capabilities"])),
                binding["deliberation_tier"],
                max_concurrency,
                provider_id == "codex",
                binding["enabled"],
            ))
        if not result:
            return cls((), max_concurrency)
        return cls(tuple(result), max_concurrency)
