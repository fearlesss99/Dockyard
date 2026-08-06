#!/usr/bin/env python3
"""AgentDesk Provider Doctor — TC-13.21e.1.

Read-only pre-start diagnostics for AgentDesk external provider and MAD
configuration.  Twelve fixed checks (D001-D012) let an ordinary user confirm
— without running a real model, exposing an API key, or touching the
network — whether a project satisfies the startup conditions for AgentDesk
provider execution and the MAD gateway.

Security boundaries (hard guarantees):

  * zero file writes
  * zero network access
  * zero subprocess execution (including Git)
  * zero model / API calls
  * never reads, stores, or displays API keys or tokens
  * never emits absolute workspace paths, config contents, env values,
    or user task payloads
  * exceptions use fixed messages; user-controlled objects are never
    repr()-ed

Determinism: identical file state always produces identical check order,
statuses, summaries, and remediations.  No wall-clock time, randomness, or
UUIDs are used.

Public API (exact)::

    DoctorCheckStatus, DoctorCheck, DoctorRequest, DoctorReport,
    DoctorError, DoctorInputError, run_doctor, main
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

try:  # Python >= 3.11
    import tomllib
except ImportError:  # pragma: no cover - only reachable on Python < 3.11
    tomllib = None  # type: ignore[assignment]

import init_project
import mad_gateway
import select_model

__all__ = [
    "DoctorCheckStatus",
    "DoctorCheck",
    "DoctorRequest",
    "DoctorReport",
    "DoctorError",
    "DoctorInputError",
    "run_doctor",
    "main",
]


# ── constants ─────────────────────────────────────────────────────────────

DOCTOR_REPORT_SCHEMA = "agentdesk.doctor-report/v1"

_RUNTIME_DIR = Path(".agentdesk/runtime")
_GATEWAY_CONFIG_FILE = Path(".agentdesk/runtime/gateway.yaml")

# Currently supported worker providers and their canonical local CLI names.
# Pinned by tests against claude_code_provider / codex_cli_provider and by
# the provider-doctor contract; worker provider executables are call-level
# configuration, so the CLI name is the only on-disk-resolvable artifact.
_SUPPORTED_PROVIDER_CLIS = {
    "claude": "claude",
    "claudecode": "claude",
    "codex": "codex",
}

# Providers with a worker-output decoder (TC-13.9c.1, version-locked
# Claude 2.1.214).  Codex remains Target/deferred (TC-13.9c.2).  Pinned by
# tests against worker_output_decoder._SUPPORTED_PROVIDERS.
_DECODER_SUPPORTED_PROVIDERS = frozenset({"claude", "claudecode"})

# Explicit placeholder marker used by the bundled gateway.yaml template.
_PLACEHOLDER_MARKER = "<configure-"

# Credential-like key segments that must never appear in config schemas.
_SECRET_KEY_SEGMENTS = frozenset(
    {
        "apikey",
        "key",
        "secret",
        "token",
        "password",
        "passwd",
        "credential",
        "credentials",
    }
)

# Runtime lock / evidence files that must never be links.
_RUNTIME_LOCK_EVIDENCE_FILES = (
    Path(".agentdesk/runtime/model-bindings.yaml"),
    Path(".agentdesk/runtime/gateway.yaml"),
    Path(".agentdesk/runtime/routes.yaml"),
    Path(".agentdesk/runtime/transport-receipts.yaml"),
    Path(".agentdesk/runtime/pm-lease.yaml"),
    Path(".agentdesk/runtime/mad-refs.yaml"),
    Path(".agentdesk/runtime/worker-slot-lease.yaml"),
)

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


# ── public data types ─────────────────────────────────────────────────────


class DoctorCheckStatus(str, Enum):
    """Outcome of one doctor check."""

    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    """One immutable, deterministic diagnostic result."""

    check_id: str
    status: DoctorCheckStatus
    summary: str
    remediation: str | None


@dataclass(frozen=True, slots=True)
class DoctorRequest:
    """Immutable doctor invocation request."""

    project_root: Path
    mad_home: Path | None


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """Immutable doctor report; ``ready`` is False iff any check FAILs."""

    ready: bool
    checks: tuple[DoctorCheck, ...]


# ── error hierarchy ───────────────────────────────────────────────────────


class DoctorError(Exception):
    """Base for all Doctor errors.  Messages are fixed strings."""


class DoctorInputError(DoctorError):
    """Invalid request shape or argument types (never echoes user data)."""


# ── small helpers ─────────────────────────────────────────────────────────


def _pass(check_id: str, summary: str) -> DoctorCheck:
    return DoctorCheck(check_id, DoctorCheckStatus.PASS, summary, None)


def _warn(check_id: str, summary: str, remediation: str) -> DoctorCheck:
    return DoctorCheck(check_id, DoctorCheckStatus.WARN, summary, remediation)


def _fail(check_id: str, summary: str, remediation: str) -> DoctorCheck:
    return DoctorCheck(check_id, DoctorCheckStatus.FAIL, summary, remediation)


def _lstat_or_none(path: Path):
    try:
        return os.lstat(path)
    except OSError:
        return None


def _is_link_or_reparse(path: Path) -> bool:
    """True if *path* itself is a symlink or (Windows) reparse point."""
    metadata = _lstat_or_none(path)
    if metadata is None:
        return False
    if stat.S_ISLNK(metadata.st_mode):
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(
        isinstance(attributes, int) and (attributes & _FILE_ATTRIBUTE_REPARSE_POINT)
    )


def _is_plain_file(path: Path) -> bool:
    metadata = _lstat_or_none(path)
    if metadata is None or not stat.S_ISREG(metadata.st_mode):
        return False
    return not _is_link_or_reparse(path)


def _is_plain_path_deep(root: Path, relative: Path, want_dir: bool) -> bool:
    """True if every component of *relative* under *root* exists, is not a
    symlink/reparse point, and the final component has the wanted type."""
    current = root
    parts = relative.parts
    for index, part in enumerate(parts):
        current = current / part
        metadata = _lstat_or_none(current)
        if metadata is None or _is_link_or_reparse(current):
            return False
        last = index == len(parts) - 1
        if last:
            if want_dir:
                return stat.S_ISDIR(metadata.st_mode)
            return stat.S_ISREG(metadata.st_mode)
        if not stat.S_ISDIR(metadata.st_mode):
            return False
    return False


def _is_within(path: Path, root: Path) -> bool:
    """Lexical containment check without resolving symlinks."""
    candidate = os.path.normcase(os.path.normpath(str(path)))
    base = os.path.normcase(os.path.normpath(str(root)))
    return candidate == base or candidate.startswith(base + os.sep)


def _read_text_no_follow(path: Path) -> str | None:
    """Read UTF-8 text from a regular file without following symlinks."""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        opened_metadata = os.fstat(descriptor)
        if not stat.S_ISREG(opened_metadata.st_mode):
            return None
        try:
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                descriptor = -1
                return handle.read()
        except (UnicodeDecodeError, OSError):
            return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _contains_placeholder(value) -> bool:
    if isinstance(value, str):
        return _PLACEHOLDER_MARKER in value
    if isinstance(value, dict):
        return any(
            _contains_placeholder(key) or _contains_placeholder(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_placeholder(item) for item in value)
    return False


def _has_secret_like_keys(value) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                segments = re.split(r"[_\-\s]+", key.lower())
                if any(segment in _SECRET_KEY_SEGMENTS for segment in segments):
                    return True
            if _has_secret_like_keys(item):
                return True
        return False
    if isinstance(value, list):
        return any(_has_secret_like_keys(item) for item in value)
    return False


def _extract_registry_ids(text: str) -> tuple[str, ...] | None:
    """Extract agent IDs (non-secret identifiers only) from agents.toml.

    Returns ``None`` for malformed content, non-list agents, entries
    without a non-empty string ``id``, or duplicate IDs.
    """
    if tomllib is None:
        return None
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    agents = raw.get("agents")
    if not isinstance(agents, list):
        return None
    ids: list[str] = []
    for entry in agents:
        if not isinstance(entry, dict):
            return None
        agent_id = entry.get("id")
        if not isinstance(agent_id, str) or not agent_id.strip():
            return None
        ids.append(agent_id)
    if len(set(ids)) != len(ids):
        return None
    return tuple(ids)


# ── shared check context (internal) ───────────────────────────────────────


@dataclass(slots=True)
class _Context:
    project_root: Path
    request_mad_home: Path | None
    bindings_raw: dict | None = None
    bindings: list | None = None
    role_eligible_providers: dict | None = None
    gateway_raw: dict | None = None
    registry_ids: tuple | None = None


def _effective_mad_home(ctx: _Context) -> Path | None:
    if ctx.request_mad_home is not None:
        return ctx.request_mad_home
    raw = ctx.gateway_raw
    if isinstance(raw, dict):
        value = raw.get("mad_home")
        if (
            isinstance(value, str)
            and value.strip()
            and _PLACEHOLDER_MARKER not in value
        ):
            return Path(value)
    return None


# ── checks D001-D012 (fixed order) ────────────────────────────────────────


def _check_project_root(ctx: _Context) -> DoctorCheck:
    check_id = "D001"
    root = ctx.project_root
    metadata = _lstat_or_none(root)
    if metadata is None:
        return _fail(
            check_id,
            "project root does not exist",
            "pass an existing absolute --project path",
        )
    if _is_link_or_reparse(root):
        return _fail(
            check_id,
            "project root must not be a symlink or reparse point",
            "pass the real project directory instead of a link",
        )
    if not stat.S_ISDIR(metadata.st_mode):
        return _fail(
            check_id,
            "project root is not a directory",
            "pass the project directory, not a file",
        )
    if not _is_plain_path_deep(root, Path("docs/pm"), want_dir=True) or (
        not _is_plain_path_deep(root, Path(".agentdesk"), want_dir=True)
    ):
        return _fail(
            check_id,
            "project root does not contain AgentDesk project structure",
            "run init_project.py to adopt the AgentDesk workflow first",
        )
    return _pass(check_id, "project root is a valid AgentDesk project directory")


def _check_runtime_dir(ctx: _Context) -> DoctorCheck:
    check_id = "D002"
    if not _is_plain_path_deep(ctx.project_root, _RUNTIME_DIR, want_dir=True):
        return _fail(
            check_id,
            "runtime directory is missing or is not a plain directory",
            "run init_project.py to create .agentdesk/runtime (doctor never creates it)",
        )
    return _pass(check_id, "runtime directory exists and is not a link")


def _check_model_bindings(ctx: _Context) -> DoctorCheck:
    check_id = "D003"
    try:
        document = select_model._load_worktree_json_object(
            ctx.project_root, select_model.MODEL_BINDINGS_FILE, "model bindings"
        )
    except select_model.SelectionError:
        return _fail(
            check_id,
            "model bindings file is missing or unreadable",
            "restore .agentdesk/runtime/model-bindings.yaml from the project template",
        )
    try:
        bindings = select_model._validated_bindings(document)
    except select_model.SelectionError:
        return _fail(
            check_id,
            "model bindings failed schema validation",
            "fix model-bindings.yaml to match agentdesk.model-bindings/v2",
        )
    ctx.bindings_raw = document
    ctx.bindings = bindings
    if not bindings:
        return _warn(
            check_id,
            "no model bindings configured",
            "add at least one enabled binding for the local providers",
        )
    if not any(binding["enabled"] for binding in bindings):
        return _warn(
            check_id,
            "all model bindings are disabled",
            "enable at least one binding that satisfies role policies",
        )
    return _pass(check_id, "model bindings are present and schema-valid")


def _check_role_policies(ctx: _Context) -> DoctorCheck:
    check_id = "D004"
    try:
        policy = select_model._load_worktree_json_object(
            ctx.project_root, select_model.ROLE_POLICY_FILE, "role policy"
        )
    except select_model.SelectionError:
        return _fail(
            check_id,
            "role policy file is missing or unreadable",
            "restore docs/pm/ROLE-POLICIES.yaml from the project template",
        )
    roles = policy.get("roles")
    if not isinstance(roles, dict) or not roles:
        return _fail(
            check_id,
            "role policy defines no roles",
            "define at least one role in ROLE-POLICIES.yaml",
        )
    requirements = {}
    for role_id in sorted(roles):
        if not isinstance(role_id, str):
            return _fail(
                check_id,
                "role policy contains a non-string role identifier",
                "fix role identifiers in ROLE-POLICIES.yaml",
            )
        try:
            requirements[role_id] = select_model._policy_requirements(
                policy, role_id, "L0", "inherit", frozenset()
            )
        except select_model.SelectionError:
            return _fail(
                check_id,
                "role policy failed schema validation",
                "fix ROLE-POLICIES.yaml to match agentdesk.role-policies/v1",
            )
    bindings = ctx.bindings or []
    enabled = [binding for binding in bindings if binding["enabled"]]
    if not enabled:
        return _warn(
            check_id,
            "role policies are valid but no enabled bindings can satisfy them",
            "configure and enable model bindings before dispatch",
        )
    eligible: dict[str, tuple[str, ...]] = {}
    for role_id in sorted(requirements):
        required_tier, _preferred, required_deliberation, required_caps, _dp = (
            requirements[role_id]
        )
        matched_providers = sorted(
            {
                binding["provider"]
                for binding in enabled
                if not select_model._rejection_reasons(
                    binding, required_tier, required_deliberation, required_caps
                )
            }
        )
        eligible[role_id] = tuple(matched_providers)
    ctx.role_eligible_providers = eligible
    if any(not providers for providers in eligible.values()):
        return _fail(
            check_id,
            "one or more roles have no eligible enabled binding",
            "add or enable a binding meeting each role's minimum tier, "
            "deliberation tier, and required capabilities",
        )
    return _pass(check_id, "role policies are valid and every role has an eligible binding")


def _check_gateway_config(ctx: _Context) -> DoctorCheck:
    check_id = "D005"
    try:
        raw = select_model._load_worktree_json_object(
            ctx.project_root, _GATEWAY_CONFIG_FILE, "gateway config"
        )
    except select_model.SelectionError:
        return _fail(
            check_id,
            "gateway config is missing or unreadable",
            "copy the template gateway.yaml into .agentdesk/runtime/ and "
            "configure every field",
        )
    ctx.gateway_raw = raw
    if _contains_placeholder(raw):
        return _fail(
            check_id,
            "gateway config contains unconfigured template placeholders",
            "replace every <configure-*> value in .agentdesk/runtime/gateway.yaml",
        )
    try:
        mad_gateway.validate_gateway_config(raw)
    except mad_gateway.GatewayConfigError:
        return _fail(
            check_id,
            "gateway config failed validation",
            "configure gateway.yaml per agentdesk.gateway-config/v1: mad "
            "executable, absolute mad_home containing config/agents.toml, "
            "positive timeout, and consistent agent IDs",
        )
    return _pass(check_id, "gateway config is valid")


def _check_mad_executable(ctx: _Context) -> DoctorCheck:
    check_id = "D006"
    raw = ctx.gateway_raw
    if not isinstance(raw, dict):
        return _fail(
            check_id,
            "mad executable is not configured",
            "configure mad_executable in .agentdesk/runtime/gateway.yaml",
        )
    candidate = raw.get("mad_executable")
    if not isinstance(candidate, str) or not candidate.strip():
        return _fail(
            check_id,
            "mad executable is not configured",
            "set mad_executable to the mad CLI name or its absolute path",
        )
    if _PLACEHOLDER_MARKER in candidate:
        return _fail(
            check_id,
            "mad executable still has its template placeholder",
            "set mad_executable to the real mad CLI location",
        )
    if mad_gateway._resolve_executable(candidate) is None:
        return _fail(
            check_id,
            "mad executable cannot be resolved locally",
            "install mad or point mad_executable at the real CLI (never executed)",
        )
    return _pass(check_id, "mad executable resolves locally")


def _check_mad_home(ctx: _Context) -> DoctorCheck:
    check_id = "D007"
    home = _effective_mad_home(ctx)
    if home is None:
        return _fail(
            check_id,
            "MAD home is not configured",
            "set mad_home in gateway.yaml or pass --mad-home",
        )
    if not home.is_absolute():
        return _fail(
            check_id,
            "MAD home must be an absolute path",
            "configure an absolute mad_home",
        )
    metadata = _lstat_or_none(home)
    if metadata is None or not stat.S_ISDIR(metadata.st_mode) or _is_link_or_reparse(home):
        return _fail(
            check_id,
            "MAD home is missing or is not a plain directory",
            "initialize MAD home outside the AgentDesk worktree",
        )
    if _is_within(home, ctx.project_root):
        return _fail(
            check_id,
            "MAD home must not live inside the AgentDesk worktree",
            "move MAD home outside the project so registry data is never Git-tracked",
        )
    registry = home / "config" / "agents.toml"
    if not _is_plain_file(registry):
        return _fail(
            check_id,
            "MAD agent registry is missing or is not a regular file",
            "initialize MAD so config/agents.toml exists",
        )
    if tomllib is None:
        return _fail(
            check_id,
            "reading the MAD agent registry requires Python 3.11 or newer",
            "run doctor with Python 3.11+",
        )
    text = _read_text_no_follow(registry)
    if text is None:
        return _fail(
            check_id,
            "MAD agent registry is unreadable",
            "fix config/agents.toml permissions and UTF-8 encoding",
        )
    ids = _extract_registry_ids(text)
    if ids is None:
        return _fail(
            check_id,
            "MAD agent registry is malformed or contains duplicate agent IDs",
            "fix config/agents.toml so every agent has a unique id",
        )
    if not ids:
        return _fail(
            check_id,
            "MAD agent registry contains no agents",
            "register at least one agent in config/agents.toml",
        )
    ctx.registry_ids = ids
    return _pass(check_id, "MAD home and agent registry are valid")


def _check_audit_agents(ctx: _Context) -> DoctorCheck:
    check_id = "D008"
    raw = ctx.gateway_raw
    if not isinstance(raw, dict):
        return _fail(
            check_id,
            "audit agents are not configured",
            "configure audit_agent_ids in .agentdesk/runtime/gateway.yaml",
        )
    ids_value = raw.get("audit_agent_ids")
    if (
        not isinstance(ids_value, list)
        or not ids_value
        or not all(isinstance(item, str) and item.strip() for item in ids_value)
    ):
        return _fail(
            check_id,
            "audit_agent_ids must be a non-empty list of agent IDs",
            "configure audit_agent_ids in gateway.yaml",
        )
    report_id = raw.get("audit_report_agent_id")
    if not isinstance(report_id, str) or not report_id.strip():
        return _fail(
            check_id,
            "audit_report_agent_id is not configured",
            "set audit_report_agent_id in gateway.yaml",
        )
    if _contains_placeholder(ids_value) or _PLACEHOLDER_MARKER in report_id:
        return _fail(
            check_id,
            "audit agent IDs still have template placeholders",
            "replace <configure-*> agent IDs with real MAD agent IDs",
        )
    if len(set(ids_value)) != len(ids_value):
        return _fail(
            check_id,
            "audit_agent_ids contains duplicate IDs",
            "remove duplicate audit agent IDs",
        )
    if report_id not in ids_value:
        return _fail(
            check_id,
            "audit report agent is not part of the audit agent set",
            "set audit_report_agent_id to one of audit_agent_ids",
        )
    registry = ctx.registry_ids
    if registry is None:
        return _fail(
            check_id,
            "MAD agent registry is unavailable; audit agents cannot be verified",
            "fix MAD home so config/agents.toml is readable",
        )
    if any(agent_id not in registry for agent_id in ids_value):
        return _fail(
            check_id,
            "one or more audit agents are not registered in MAD",
            "register every audit_agent_id in config/agents.toml",
        )
    return _pass(check_id, "audit agents are registered and the report agent is in the set")


def _check_provider_executables(ctx: _Context) -> DoctorCheck:
    check_id = "D009"
    bindings = ctx.bindings
    if bindings is None:
        return _fail(
            check_id,
            "model bindings are unavailable; provider executables cannot be verified",
            "fix model-bindings.yaml first",
        )
    enabled = [binding for binding in bindings if binding["enabled"]]
    if not enabled:
        return _warn(
            check_id,
            "no enabled bindings; provider executables are not required",
            "enable a binding before dispatch",
        )
    providers = sorted({binding["provider"] for binding in enabled})
    for provider in providers:
        if provider not in _SUPPORTED_PROVIDER_CLIS:
            return _fail(
                check_id,
                "an enabled binding uses an unsupported provider",
                "use claude, claudecode, or codex bindings only",
            )
    for provider in providers:
        if mad_gateway._resolve_executable(_SUPPORTED_PROVIDER_CLIS[provider]) is None:
            return _fail(
                check_id,
                "a provider CLI cannot be resolved locally",
                "install the provider CLI or disable its bindings (never executed)",
            )
    return _pass(check_id, "every enabled binding maps to a supported, resolvable provider CLI")


def _check_decoder_eligibility(ctx: _Context) -> DoctorCheck:
    check_id = "D010"
    bindings = ctx.bindings
    if bindings is None:
        return _fail(
            check_id,
            "model bindings are unavailable; decoder eligibility cannot be verified",
            "fix model-bindings.yaml first",
        )
    enabled = [binding for binding in bindings if binding["enabled"]]
    if not enabled:
        return _warn(
            check_id,
            "no enabled bindings; decoder eligibility is not required",
            "enable a binding before dispatch",
        )
    providers = {binding["provider"] for binding in enabled}
    if "codex" not in providers:
        if providers <= _DECODER_SUPPORTED_PROVIDERS:
            return _pass(
                check_id,
                "all enabled bindings use decoder-supported providers",
            )
        return _fail(
            check_id,
            "an enabled binding uses a provider without decoder support",
            "use claude or claudecode bindings for decodable worker output",
        )
    eligible = ctx.role_eligible_providers
    if eligible is not None and any(
        role_providers and all(provider == "codex" for provider in role_providers)
        for role_providers in eligible.values()
    ):
        return _fail(
            check_id,
            "codex bindings are required by role policy but no codex decoder exists",
            "add an eligible claude/claudecode binding; the codex decoder is "
            "deferred (Target TC-13.9c.2)",
        )
    return _warn(
        check_id,
        "codex bindings are enabled but the codex worker-output decoder is unavailable",
        "codex output cannot be decoded today (Target TC-13.9c.2); ensure "
        "claude/claudecode bindings cover every role",
    )


def _check_dashboard_availability(ctx: _Context) -> DoctorCheck:
    check_id = "D011"
    scripts_dir = Path(__file__).resolve().parent
    if not _is_plain_file(scripts_dir / "html_dashboard.py"):
        return _fail(
            check_id,
            "dashboard renderer module is missing",
            "restore html_dashboard.py in the skill scripts directory",
        )
    scripts_path = str(scripts_dir)
    added = False
    if scripts_path not in sys.path:
        sys.path.insert(0, scripts_path)
        added = True
    try:
        importlib.import_module("html_dashboard")
    except Exception:
        return _fail(
            check_id,
            "dashboard renderer module is not importable",
            "fix html_dashboard.py imports; no HTML is generated by doctor",
        )
    finally:
        if added:
            try:
                sys.path.remove(scripts_path)
            except ValueError:
                pass
    return _pass(check_id, "dashboard renderer module is importable")


def _runtime_gitignored(root: Path) -> bool:
    entry = init_project.IGNORE_ENTRY
    for candidate in (root / ".gitignore", root / ".git" / "info" / "exclude"):
        if not _is_plain_file(candidate):
            continue
        text = _read_text_no_follow(candidate)
        if text is None:
            continue
        for line in text.splitlines():
            if line.strip() == entry:
                return True
    return False


def _check_runtime_safety(ctx: _Context) -> DoctorCheck:
    check_id = "D012"
    root = ctx.project_root
    if not _runtime_gitignored(root):
        return _fail(
            check_id,
            "runtime directory is not covered by an exact Git ignore entry",
            "add '.agentdesk/runtime/' to .gitignore (init_project.py does this)",
        )
    if _has_secret_like_keys(ctx.gateway_raw) or _has_secret_like_keys(ctx.bindings_raw):
        return _fail(
            check_id,
            "configuration schema contains credential-like field names",
            "remove key/token fields; providers manage their own credentials",
        )
    for relative in _RUNTIME_LOCK_EVIDENCE_FILES:
        candidate = root / relative
        if _lstat_or_none(candidate) is not None and _is_link_or_reparse(candidate):
            return _fail(
                check_id,
                "a runtime lock or evidence file is a symlink or reparse point",
                "replace the link with a regular file",
            )
    return _pass(
        check_id,
        "runtime state is Git-ignored and free of credential fields and link attacks",
    )


_CHECKS = (
    _check_project_root,
    _check_runtime_dir,
    _check_model_bindings,
    _check_role_policies,
    _check_gateway_config,
    _check_mad_executable,
    _check_mad_home,
    _check_audit_agents,
    _check_provider_executables,
    _check_decoder_eligibility,
    _check_dashboard_availability,
    _check_runtime_safety,
)


def _guarded(check, ctx: _Context) -> DoctorCheck:
    """Run one check; convert unexpected errors into a fixed FAIL."""
    try:
        return check(ctx)
    except Exception:
        return _fail(
            "D000",
            "check aborted on an internal error",
            "re-run doctor; if the problem persists, inspect the configuration manually",
        )


# ── main entry points ─────────────────────────────────────────────────────


def run_doctor(request: DoctorRequest) -> DoctorReport:
    """Run all twelve checks in fixed order and return an immutable report.

    Raises :exc:`DoctorInputError` (fixed messages) for request shape or
    type violations.  Filesystem-level problems are reported as check
    results, not exceptions.
    """
    if not isinstance(request, DoctorRequest):
        raise DoctorInputError("request must be a DoctorRequest")
    project_root = request.project_root
    if isinstance(project_root, bool) or not isinstance(project_root, Path):
        raise DoctorInputError("project_root must be a pathlib.Path")
    if not project_root.is_absolute():
        raise DoctorInputError("project_root must be an absolute path")
    mad_home = request.mad_home
    if mad_home is not None:
        if isinstance(mad_home, bool) or not isinstance(mad_home, Path):
            raise DoctorInputError("mad_home must be a pathlib.Path or None")
        if not mad_home.is_absolute():
            raise DoctorInputError("mad_home must be an absolute path")

    ctx = _Context(project_root=project_root, request_mad_home=mad_home)
    checks: list[DoctorCheck] = []
    for check in _CHECKS:
        result = _guarded(check, ctx)
        if result.check_id == "D000":
            result = DoctorCheck(
                check.__name__, result.status, result.summary, result.remediation
            )
        checks.append(result)
    ready = all(check.status is not DoctorCheckStatus.FAIL for check in checks)
    return DoctorReport(ready=ready, checks=tuple(checks))


def _report_to_json(report: DoctorReport) -> dict:
    return {
        "schema_version": DOCTOR_REPORT_SCHEMA,
        "ready": report.ready,
        "checks": [
            {
                "check_id": check.check_id,
                "status": check.status.value,
                "summary": check.summary,
                "remediation": check.remediation,
            }
            for check in report.checks
        ],
    }


def _print_text(report: DoctorReport) -> None:
    for check in report.checks:
        line = f"{check.status.value.upper()} {check.check_id} {check.summary}"
        if check.remediation is not None:
            line += f" | remediation: {check.remediation}"
        print(line)
    print("READY" if report.ready else "NOT READY")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only AgentDesk provider and MAD configuration doctor. "
            "No file writes, no network, no subprocess, no model calls."
        )
    )
    parser.add_argument(
        "--project",
        required=True,
        help="absolute path to the AgentDesk project root",
    )
    parser.add_argument(
        "--mad-home",
        default=None,
        help="absolute path to MAD home (overrides gateway.yaml mad_home)",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="output format (default: text)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: 0 = ready, 1 = not ready, 2 = argument/input error."""
    args = _build_parser().parse_args(argv)
    try:
        request = DoctorRequest(
            project_root=Path(args.project).expanduser(),
            mad_home=(
                Path(args.mad_home).expanduser() if args.mad_home is not None else None
            ),
        )
        report = run_doctor(request)
    except DoctorInputError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    if args.format == "json":
        json.dump(_report_to_json(report), sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        _print_text(report)
    return 0 if report.ready else 1


if __name__ == "__main__":
    sys.exit(main())
