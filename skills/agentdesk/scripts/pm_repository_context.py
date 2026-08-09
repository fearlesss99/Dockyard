"""Read-only Git inventory evidence used by the local PM decomposer."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "PmRepositoryInventory",
    "PmRepositoryInspectionError",
    "inspect_repository",
]


class PmRepositoryInspectionError(ValueError):
    """The repository inventory or snapshot binding cannot be verified."""


_SHA = re.compile(r"^[0-9a-f]{40}$")


def _sha(value: object, field: str) -> str:
    if type(value) is not str or _SHA.fullmatch(value) is None:
        raise PmRepositoryInspectionError(f"{field} is invalid")
    return value


def _root(value: object) -> Path:
    if not isinstance(value, Path) or not value.is_absolute():
        raise PmRepositoryInspectionError("project_root is invalid")
    try:
        resolved = value.resolve(strict=True)
        metadata = resolved.lstat()
    except OSError as exc:
        raise PmRepositoryInspectionError("project_root is unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise PmRepositoryInspectionError("project_root is not a regular directory")
    return resolved


def _git(root: Path, *arguments: str) -> bytes:
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith("GIT_")
    }
    environment.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
    })
    try:
        completed = subprocess.run(
            (
                "git",
                "--no-replace-objects",
                "-c",
                "core.longpaths=true",
                "-c",
                f"safe.directory={root}",
                "-C",
                str(root),
                *arguments,
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=10,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PmRepositoryInspectionError("Git inventory is unavailable") from exc
    return completed.stdout


@dataclass(frozen=True, slots=True)
class PmRepositoryInventory:
    schema_version: str
    snapshot_commit: str
    file_count: int
    top_level_entries: tuple[str, ...]
    language_counts: tuple[tuple[str, int], ...]
    content_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != "agentdesk.pm-repository-inventory/v1":
            raise PmRepositoryInspectionError("schema_version is invalid")
        _sha(self.snapshot_commit, "snapshot_commit")
        if type(self.file_count) is not int or self.file_count < 0:
            raise PmRepositoryInspectionError("file_count is invalid")
        if type(self.top_level_entries) is not tuple or tuple(sorted(self.top_level_entries)) != self.top_level_entries:
            raise PmRepositoryInspectionError("top_level_entries are invalid")
        if len(set(self.top_level_entries)) != len(self.top_level_entries):
            raise PmRepositoryInspectionError("top_level_entries contain duplicates")
        if type(self.language_counts) is not tuple:
            raise PmRepositoryInspectionError("language_counts are invalid")
        for item in self.language_counts:
            if type(item) is not tuple or len(item) != 2 or type(item[0]) is not str or type(item[1]) is not int or item[1] < 1:
                raise PmRepositoryInspectionError("language_counts contain invalid entries")
        if tuple(sorted(item[0] for item in self.language_counts)) != tuple(item[0] for item in self.language_counts):
            raise PmRepositoryInspectionError("language_counts are not ordered")
        if type(self.content_digest) is not str or len(self.content_digest) != 64 or any(char not in "0123456789abcdef" for char in self.content_digest):
            raise PmRepositoryInspectionError("content_digest is invalid")


def inspect_repository(project_root: Path, expected_snapshot_commit: str) -> PmRepositoryInventory:
    """Inventory tracked paths and bind the result to the exact Git HEAD."""
    root = _root(project_root)
    expected = _sha(expected_snapshot_commit, "expected_snapshot_commit")
    actual = _git(root, "rev-parse", "--verify", "HEAD").decode("ascii", errors="strict").strip()
    if actual != expected:
        raise PmRepositoryInspectionError("Git HEAD does not match expected snapshot")
    raw_paths = _git(root, "ls-files", "-z")
    try:
        paths = tuple(item.decode("utf-8", errors="strict") for item in raw_paths.split(b"\0") if item)
    except UnicodeDecodeError as exc:
        raise PmRepositoryInspectionError("Git path inventory is not valid UTF-8") from exc
    if any(not path or path.startswith(("/", "\\")) or ".." in Path(path).parts for path in paths):
        raise PmRepositoryInspectionError("Git path inventory contains an unsafe path")
    top_level = tuple(sorted({path.replace("\\", "/").split("/", 1)[0] for path in paths}))
    language: dict[str, int] = {}
    for path in paths:
        suffix = Path(path).suffix.lower()
        label = suffix[1:] if suffix else "[no-extension]"
        language[label] = language.get(label, 0) + 1
    language_counts = tuple(sorted(language.items()))
    digest_input = json.dumps(
        {"snapshot_commit": actual, "paths": paths, "language_counts": language_counts},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return PmRepositoryInventory(
        "agentdesk.pm-repository-inventory/v1",
        actual,
        len(paths),
        top_level,
        language_counts,
        hashlib.sha256(digest_input).hexdigest(),
    )
