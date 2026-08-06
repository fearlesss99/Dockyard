"""Small durable Store for validated Dockyard MAD audit results."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import threading
from dataclasses import asdict
from pathlib import Path

from core_types import MadDeliberationDepth
from mad_audit_gateway import (
    MadAuditEvidence,
    MadAuditGatewayResult,
    MadAuditIssue,
    MadAuditIssueLocation,
    MadAuditPlan,
)


class DockyardMadResultStoreError(ValueError):
    pass


class DockyardMadResultConflictError(DockyardMadResultStoreError):
    pass


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")
_GUARD = threading.Lock()
_LOCKS: dict[str, threading.Lock] = {}


def _path(root: Path, review_id: str, create: bool) -> Path:
    if type(review_id) is not str or _SAFE_ID.fullmatch(review_id) is None:
        raise DockyardMadResultStoreError("mad_result:review_id")
    runtime = root / ".agentdesk" / "runtime"
    directory = runtime / "dockyard-mad-results"
    try:
        if create:
            directory.mkdir(parents=False, exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():
            raise DockyardMadResultStoreError("mad_result:directory")
        for item in directory.iterdir():
            if item.is_symlink() or not stat.S_ISREG(os.lstat(item).st_mode):
                raise DockyardMadResultStoreError("mad_result:entry_type")
            if re.fullmatch(r"REV-[A-Za-z0-9._:-]+\.json", item.name) is None:
                raise DockyardMadResultStoreError("mad_result:extra_entry")
    except OSError as exc:
        raise DockyardMadResultStoreError("mad_result:filesystem") from exc
    return directory / f"{review_id}.json"


def _encoded(result: MadAuditGatewayResult) -> bytes:
    if type(result) is not MadAuditGatewayResult:
        raise DockyardMadResultStoreError("mad_result:type")
    body = asdict(result)
    body["plan"]["depth"] = result.plan.depth.value
    canonical = json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    envelope = {
        "schema_version": "dockyard.mad-result/v1",
        "result": body,
        "content_digest": "sha256:" + hashlib.sha256(canonical).hexdigest(),
    }
    return (
        json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _decoded(data: bytes) -> MadAuditGatewayResult:
    try:
        raw = json.loads(data.decode("utf-8"))
        if type(raw) is not dict or set(raw) != {
            "schema_version", "result", "content_digest"
        }:
            raise ValueError
        body = raw["result"]
        if raw["schema_version"] != "dockyard.mad-result/v1" or type(body) is not dict:
            raise ValueError
        canonical = json.dumps(
            body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if raw["content_digest"] != "sha256:" + hashlib.sha256(canonical).hexdigest():
            raise ValueError
        expected = {
            "deliberation_id", "status", "verdict", "issues", "evidence",
            "warnings", "report", "archive_path", "participants", "plan",
            "stdout_sha256", "report_sha256",
        }
        if set(body) != expected or type(body["plan"]) is not dict or set(body["plan"]) != {"depth"}:
            raise ValueError
        issues = []
        for item in body["issues"]:
            location = item["location"]
            issues.append(MadAuditIssue(
                item["id"], item["severity"], item["category"], item["title"],
                item["description"],
                MadAuditIssueLocation(location["file"], location["line"], location["commit"]),
                item["recommendation"],
            ))
        evidence = tuple(MadAuditEvidence(
            item["ref"], item["type"], item["source"], item["summary"], item["verified"]
        ) for item in body["evidence"])
        return MadAuditGatewayResult(
            body["deliberation_id"], body["status"], body["verdict"],
            tuple(issues), evidence, tuple(body["warnings"]), body["report"],
            body["archive_path"], tuple(body["participants"]),
            MadAuditPlan(MadDeliberationDepth(body["plan"]["depth"])),
            body["stdout_sha256"], body["report_sha256"],
        )
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise DockyardMadResultStoreError("mad_result:parse") from exc


class DockyardMadResultStore:
    def __init__(self, project_root: Path) -> None:
        if not isinstance(project_root, Path) or not project_root.is_absolute():
            raise DockyardMadResultStoreError("mad_result:project_root")
        self.root = project_root

    def save(self, review_id: str, result: MadAuditGatewayResult) -> MadAuditGatewayResult:
        path = _path(self.root, review_id, True)
        encoded = _encoded(result)
        key = os.path.normcase(str(self.root.resolve()))
        with _GUARD:
            lock = _LOCKS.setdefault(key, threading.Lock())
        with lock:
            try:
                if path.exists():
                    existing = path.read_bytes()
                    if existing != encoded:
                        raise DockyardMadResultConflictError("mad_result:replay")
                    return _decoded(existing)
                temp = path.with_name(path.name + f".tmp-{os.getpid()}-{secrets.token_hex(8)}")
                fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp, path)
            except DockyardMadResultStoreError:
                raise
            except OSError as exc:
                raise DockyardMadResultStoreError("mad_result:write") from exc
        return result

    def read(self, review_id: str) -> MadAuditGatewayResult:
        path = _path(self.root, review_id, False)
        try:
            return _decoded(path.read_bytes())
        except FileNotFoundError as exc:
            raise DockyardMadResultStoreError("mad_result:not_found") from exc
        except OSError as exc:
            raise DockyardMadResultStoreError("mad_result:read") from exc


__all__ = [
    "DockyardMadResultStore",
    "DockyardMadResultStoreError",
    "DockyardMadResultConflictError",
]
