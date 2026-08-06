"""Dockyard provider health and local model configuration service.

The service projects only typed, non-secret provider evidence.  It never runs
a provider, reads credentials, calls a model/API, or exposes environment
variables.  Local model-binding updates use a digest CAS and an atomic replace.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

import select_model

__all__ = [
    "ProviderEvidenceState",
    "ProviderHealthStatus",
    "ProviderRuntimeEvidence",
    "ProviderBinding",
    "ProviderHealth",
    "ProviderCatalog",
    "ModelBindingUpdateRequest",
    "ModelBindingUpdateResult",
    "DockyardProviderError",
    "DockyardProviderInputError",
    "DockyardProviderConflictError",
    "DockyardProviderFilesystemError",
    "project_provider_health",
    "update_model_binding",
]

_MODEL_BINDINGS = Path(".agentdesk/runtime/model-bindings.yaml")
_PROVIDERS = ("claude", "codex", "reasonix")
_EXECUTABLES = {
    "claude": "claude",
    "codex": "codex",
    "reasonix": "reasonix",
}
_EXPECTED_CLI_VERSIONS = {
    "claude": "2.1.214",
    "codex": "0.144.6",
    "reasonix": "1.19.1",
}
_EXPECTED_DECODER_VERSIONS = dict(_EXPECTED_CLI_VERSIONS)
_PROVIDER_REQUIRED_CAPABILITIES = {
    "claude": ("structured_output",),
    "codex": ("structured_output",),
    "reasonix": ("real_api_evidence", "structured_output"),
}
_TIER_INDEX = {tier: index for index, tier in enumerate(select_model.MODEL_TIERS)}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_OPERATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class ProviderEvidenceState(str, Enum):
    PRESENT = "present"
    MISSING = "missing"
    UNKNOWN = "unknown"


class ProviderHealthStatus(str, Enum):
    READY = "ready"
    DEGRADED = "degraded"
    NOT_READY = "not_ready"
    UNKNOWN = "unknown"


class DockyardProviderError(Exception):
    """Base for typed provider service failures."""


class DockyardProviderInputError(DockyardProviderError):
    """Caller supplied invalid typed evidence or configuration."""


class DockyardProviderConflictError(DockyardProviderError):
    """Digest CAS, replay identity, or temp-file ownership conflicted."""


class DockyardProviderFilesystemError(DockyardProviderError):
    """The local binding path cannot be verified or updated safely."""


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise DockyardProviderInputError(f"{field} must be a non-empty trimmed str")
    if any(ord(char) < 32 for char in value):
        raise DockyardProviderInputError(f"{field} contains a control character")
    return value


def _provider(value: object) -> str:
    result = _text(value, "provider_id")
    if result not in _PROVIDERS:
        raise DockyardProviderInputError("provider_id is not supported")
    return result


def _tuple_text(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise DockyardProviderInputError(f"{field} must be tuple[str, ...]")
    result = tuple(_text(item, f"{field} item") for item in value)
    if len(set(result)) != len(result):
        raise DockyardProviderInputError(f"{field} contains duplicates")
    if result != tuple(sorted(result)):
        raise DockyardProviderInputError(f"{field} must be sorted")
    return result


@dataclass(frozen=True, slots=True)
class ProviderRuntimeEvidence:
    provider_id: str
    executable_state: ProviderEvidenceState
    executable_version_state: ProviderEvidenceState
    executable_version: str | None
    decoder_state: ProviderEvidenceState
    decoder_version: str | None
    capability_state: ProviderEvidenceState
    capabilities: tuple[str, ...]

    def __post_init__(self) -> None:
        _provider(self.provider_id)
        for field in (
            "executable_state",
            "executable_version_state",
            "decoder_state",
            "capability_state",
        ):
            if not isinstance(getattr(self, field), ProviderEvidenceState):
                raise DockyardProviderInputError(f"{field} has an invalid type")
        for state_field, value_field in (
            ("executable_version_state", "executable_version"),
            ("decoder_state", "decoder_version"),
        ):
            state = getattr(self, state_field)
            value = getattr(self, value_field)
            if state is ProviderEvidenceState.PRESENT:
                _text(value, value_field)
            elif value is not None:
                raise DockyardProviderInputError(
                    f"{value_field} must be None unless evidence is present"
                )
        _tuple_text(self.capabilities, "capabilities")
        if self.capability_state is not ProviderEvidenceState.PRESENT and self.capabilities:
            raise DockyardProviderInputError(
                "capabilities must be empty unless capability evidence is present"
            )


@dataclass(frozen=True, slots=True)
class ProviderBinding:
    binding_id: str
    provider_id: str
    model_id: str
    tier: str
    deliberation_tier: str
    context_window_tokens: int
    capabilities: tuple[str, ...]
    enabled: bool


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    provider_id: str
    executable: str
    status: ProviderHealthStatus
    reason_code: str
    executable_version: str | None
    decoder_version: str | None
    bindings: tuple[ProviderBinding, ...]
    missing_capabilities: tuple[str, ...]
    requires_approval: bool


@dataclass(frozen=True, slots=True)
class ProviderCatalog:
    schema_version: str
    binding_digest: str
    preferred_tier: str
    providers: tuple[ProviderHealth, ...]


@dataclass(frozen=True, slots=True)
class ModelBindingUpdateRequest:
    expected_digest: str
    operation_id: str
    updated_at: str
    binding_id: str
    provider_id: str
    model_id: str
    tier: str
    deliberation_tier: str
    context_window_tokens: int
    capabilities: tuple[str, ...]
    enabled: bool

    def __post_init__(self) -> None:
        if not isinstance(self.expected_digest, str) or not _SHA256_RE.fullmatch(
            self.expected_digest
        ):
            raise DockyardProviderInputError("expected_digest must be lowercase SHA-256")
        if not isinstance(self.operation_id, str) or not _OPERATION_RE.fullmatch(
            self.operation_id
        ):
            raise DockyardProviderInputError("operation_id has an invalid format")
        _require_utc(self.updated_at)
        _text(self.binding_id, "binding_id")
        _provider(self.provider_id)
        _text(self.model_id, "model_id")
        if self.tier not in select_model.MODEL_TIERS:
            raise DockyardProviderInputError("tier is invalid")
        if self.deliberation_tier not in select_model.DELIBERATION_TIERS:
            raise DockyardProviderInputError("deliberation_tier is invalid")
        if isinstance(self.context_window_tokens, bool) or not isinstance(
            self.context_window_tokens, int
        ) or self.context_window_tokens < 1:
            raise DockyardProviderInputError(
                "context_window_tokens must be a positive non-bool int"
            )
        _tuple_text(self.capabilities, "capabilities")
        if not isinstance(self.enabled, bool):
            raise DockyardProviderInputError("enabled must be bool")


@dataclass(frozen=True, slots=True)
class ModelBindingUpdateResult:
    operation_id: str
    binding_id: str
    previous_digest: str
    current_digest: str
    replayed: bool


def _require_utc(value: object) -> str:
    text = _text(value, "updated_at")
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise DockyardProviderInputError("updated_at must be RFC3339 UTC") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise DockyardProviderInputError("updated_at must be RFC3339 UTC")
    return text


def _is_reparse(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def _binding_path(project_root: Path) -> Path:
    if not isinstance(project_root, Path) or not project_root.is_absolute():
        raise DockyardProviderInputError("project_root must be an absolute Path")
    current = project_root
    try:
        root_metadata = current.lstat()
    except OSError as exc:
        raise DockyardProviderFilesystemError("project root cannot be inspected") from exc
    if not stat.S_ISDIR(root_metadata.st_mode) or stat.S_ISLNK(root_metadata.st_mode) or _is_reparse(root_metadata):
        raise DockyardProviderFilesystemError("project root must be a plain directory")
    for index, part in enumerate(_MODEL_BINDINGS.parts):
        current = current / part
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise DockyardProviderFilesystemError("model binding path is missing") from exc
        final = index == len(_MODEL_BINDINGS.parts) - 1
        wanted = stat.S_ISREG(metadata.st_mode) if final else stat.S_ISDIR(metadata.st_mode)
        if not wanted or stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
            raise DockyardProviderFilesystemError("model binding path is not plain")
    return current


def _read_raw(path: Path) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise DockyardProviderFilesystemError("model bindings cannot be opened") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _is_reparse(opened):
            raise DockyardProviderFilesystemError("opened model bindings are not plain")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(descriptor)


def _document(raw: bytes) -> dict[str, object]:
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DockyardProviderFilesystemError("model bindings are not valid UTF-8 JSON") from exc
    if not isinstance(parsed, dict):
        raise DockyardProviderFilesystemError("model bindings root must be object")
    try:
        select_model._validated_bindings(parsed)
    except select_model.SelectionError as exc:
        raise DockyardProviderFilesystemError("model bindings failed schema validation") from exc
    return parsed


def _bindings(document: dict[str, object]) -> tuple[ProviderBinding, ...]:
    validated = select_model._validated_bindings(document)
    result: list[ProviderBinding] = []
    for item in validated:
        provider_id = item["provider"]
        if provider_id not in _PROVIDERS:
            continue
        result.append(ProviderBinding(
            binding_id=item["binding_id"],
            provider_id=provider_id,
            model_id=item["model_id"],
            tier=item["tier"],
            deliberation_tier=item["deliberation_tier"],
            context_window_tokens=item["context_window_tokens"],
            capabilities=tuple(sorted(item["capabilities"])),
            enabled=item["enabled"],
        ))
    return tuple(result)


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def project_provider_health(
    project_root: Path,
    evidence: tuple[ProviderRuntimeEvidence, ...],
    *,
    preferred_tier: str = "standard",
) -> ProviderCatalog:
    """Project the three local providers without reading credentials."""
    if preferred_tier not in select_model.MODEL_TIERS:
        raise DockyardProviderInputError("preferred_tier is invalid")
    if not isinstance(evidence, tuple):
        raise DockyardProviderInputError("evidence must be tuple")
    evidence_by_provider: dict[str, ProviderRuntimeEvidence] = {}
    for item in evidence:
        if not isinstance(item, ProviderRuntimeEvidence):
            raise DockyardProviderInputError("evidence item has an invalid type")
        if item.provider_id in evidence_by_provider:
            raise DockyardProviderInputError("duplicate provider evidence")
        evidence_by_provider[item.provider_id] = item

    path = _binding_path(project_root)
    raw = _read_raw(path)
    bindings = _bindings(_document(raw))
    health: list[ProviderHealth] = []
    for provider_id in _PROVIDERS:
        provider_bindings = tuple(
            item for item in bindings if item.provider_id == provider_id and item.enabled
        )
        item = evidence_by_provider.get(provider_id)
        status = ProviderHealthStatus.UNKNOWN
        reason = "EVIDENCE_UNAVAILABLE"
        missing: tuple[str, ...] = ()
        approval = False
        executable_version = None if item is None else item.executable_version
        decoder_version = None if item is None else item.decoder_version
        if item is not None:
            states = (
                item.executable_state,
                item.executable_version_state,
                item.decoder_state,
                item.capability_state,
            )
            if ProviderEvidenceState.UNKNOWN in states:
                status = ProviderHealthStatus.UNKNOWN
                reason = "EVIDENCE_UNKNOWN"
            elif not provider_bindings:
                status = ProviderHealthStatus.NOT_READY
                reason = "NO_ENABLED_BINDING"
            elif ProviderEvidenceState.MISSING in states:
                status = ProviderHealthStatus.NOT_READY
                reason = "REQUIRED_EVIDENCE_MISSING"
            elif item.executable_version != _EXPECTED_CLI_VERSIONS[provider_id]:
                status = ProviderHealthStatus.NOT_READY
                reason = "CLI_VERSION_UNSUPPORTED"
            elif item.decoder_version != _EXPECTED_DECODER_VERSIONS[provider_id]:
                status = ProviderHealthStatus.NOT_READY
                reason = "DECODER_VERSION_UNSUPPORTED"
            else:
                required = set(_PROVIDER_REQUIRED_CAPABILITIES[provider_id])
                for binding in provider_bindings:
                    required.update(binding.capabilities)
                missing = tuple(sorted(required - set(item.capabilities)))
                if missing:
                    status = ProviderHealthStatus.NOT_READY
                    reason = "CAPABILITY_EVIDENCE_MISSING"
                elif max(_TIER_INDEX[binding.tier] for binding in provider_bindings) < _TIER_INDEX[preferred_tier]:
                    status = ProviderHealthStatus.DEGRADED
                    reason = "BELOW_PREFERRED_TIER"
                    approval = True
                else:
                    status = ProviderHealthStatus.READY
                    reason = "ALL_EVIDENCE_PRESENT"
        health.append(ProviderHealth(
            provider_id=provider_id,
            executable=_EXECUTABLES[provider_id],
            status=status,
            reason_code=reason,
            executable_version=executable_version,
            decoder_version=decoder_version,
            bindings=provider_bindings,
            missing_capabilities=missing,
            requires_approval=approval,
        ))
    return ProviderCatalog(
        schema_version="dockyard.provider-catalog/v1",
        binding_digest=_digest(raw),
        preferred_tier=preferred_tier,
        providers=tuple(health),
    )


def _desired_document(
    current: dict[str, object], request: ModelBindingUpdateRequest
) -> dict[str, object]:
    bindings = current.get("bindings")
    if not isinstance(bindings, dict):
        raise DockyardProviderFilesystemError("model bindings object is missing")
    updated_bindings = dict(bindings)
    updated_bindings[request.binding_id] = {
        "provider": request.provider_id,
        "model_id": request.model_id,
        "tier": request.tier,
        "deliberation_tier": request.deliberation_tier,
        "context_window_tokens": request.context_window_tokens,
        "capabilities": list(request.capabilities),
        "enabled": request.enabled,
    }
    desired: dict[str, object] = {
        "schema_version": select_model.MODEL_BINDINGS_SCHEMA_VERSION,
        "updated_at": request.updated_at,
        "bindings": updated_bindings,
    }
    try:
        select_model._validated_bindings(desired)
    except select_model.SelectionError as exc:
        raise DockyardProviderInputError("requested binding is not schema-valid") from exc
    return desired


def _encode(document: dict[str, object]) -> bytes:
    return (json.dumps(
        document,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n").encode("utf-8")


def _acquire_update_lock(path: Path, operation_id: str) -> tuple[Path, int]:
    lock_path = path.with_name(f".{path.name}.lock")
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        descriptor = os.open(lock_path, flags, 0o600)
    except FileExistsError as exc:
        raise DockyardProviderConflictError("model binding update lock is held") from exc
    except OSError as exc:
        raise DockyardProviderFilesystemError("model binding lock cannot be created") from exc
    try:
        payload = operation_id.encode("ascii")
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    except OSError as exc:
        os.close(descriptor)
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise DockyardProviderFilesystemError("model binding lock cannot be written") from exc
    return lock_path, descriptor


def _release_update_lock(lock_path: Path, descriptor: int, operation_id: str) -> None:
    os.close(descriptor)
    try:
        metadata = lock_path.lstat()
        if (
            stat.S_ISREG(metadata.st_mode)
            and not stat.S_ISLNK(metadata.st_mode)
            and not _is_reparse(metadata)
            and lock_path.read_bytes() == operation_id.encode("ascii")
        ):
            lock_path.unlink()
    except OSError:
        pass


def _update_model_binding_locked(
    path: Path,
    request: ModelBindingUpdateRequest,
) -> ModelBindingUpdateResult:
    current_raw = _read_raw(path)
    current_digest = _digest(current_raw)
    desired_raw = _encode(_desired_document(_document(current_raw), request))
    desired_digest = _digest(desired_raw)
    if not hmac.compare_digest(current_digest, request.expected_digest):
        if hmac.compare_digest(current_digest, desired_digest):
            return ModelBindingUpdateResult(
                operation_id=request.operation_id,
                binding_id=request.binding_id,
                previous_digest=request.expected_digest,
                current_digest=current_digest,
                replayed=True,
            )
        raise DockyardProviderConflictError("model binding digest CAS failed")
    if hmac.compare_digest(current_digest, desired_digest):
        return ModelBindingUpdateResult(
            operation_id=request.operation_id,
            binding_id=request.binding_id,
            previous_digest=current_digest,
            current_digest=current_digest,
            replayed=True,
        )

    temporary = path.with_name(f".{path.name}.{request.operation_id}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    try:
        descriptor = os.open(temporary, flags, 0o600)
    except FileExistsError as exc:
        raise DockyardProviderConflictError("operation temp file already exists") from exc
    except OSError as exc:
        raise DockyardProviderFilesystemError("operation temp file cannot be created") from exc
    try:
        offset = 0
        while offset < len(desired_raw):
            offset += os.write(descriptor, desired_raw[offset:])
        os.fsync(descriptor)
    except OSError as exc:
        raise DockyardProviderFilesystemError("model binding write failed") from exc
    finally:
        os.close(descriptor)
    try:
        latest_digest = _digest(_read_raw(path))
        if not hmac.compare_digest(latest_digest, current_digest):
            raise DockyardProviderConflictError("model binding changed before replace")
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return ModelBindingUpdateResult(
        operation_id=request.operation_id,
        binding_id=request.binding_id,
        previous_digest=current_digest,
        current_digest=desired_digest,
        replayed=False,
    )


def update_model_binding(
    project_root: Path,
    request: ModelBindingUpdateRequest,
) -> ModelBindingUpdateResult:
    """Atomically update one non-secret local binding using digest CAS."""
    if not isinstance(request, ModelBindingUpdateRequest):
        raise DockyardProviderInputError("request has an invalid type")
    path = _binding_path(project_root)
    lock_path, descriptor = _acquire_update_lock(path, request.operation_id)
    try:
        return _update_model_binding_locked(path, request)
    finally:
        _release_update_lock(lock_path, descriptor, request.operation_id)
