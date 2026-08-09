"""Directed tests for read-only PM Git inventory evidence."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "agentdesk" / "scripts"))
from pm_repository_context import PmRepositoryInspectionError, inspect_repository  # noqa: E402
sys.path.pop(0)


class PmRepositoryContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        subprocess.run(("git", "init", "--quiet", str(self.root)), check=True)
        (self.root / "skills").mkdir()
        (self.root / "skills" / "worker.py").write_text("print('ok')\n", encoding="utf-8")
        (self.root / "README.md").write_text("# test\n", encoding="utf-8")
        subprocess.run(("git", "-C", str(self.root), "add", "--", "."), check=True)
        subprocess.run((
            "git", "-C", str(self.root), "-c", "user.name=PM Test",
            "-c", "user.email=pm@example.invalid", "commit", "--quiet", "-m", "fixture",
        ), check=True)
        self.head = subprocess.run(
            ("git", "-C", str(self.root), "rev-parse", "HEAD"),
            check=True, stdout=subprocess.PIPE, text=True,
        ).stdout.strip()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_inventory_binds_exact_head_and_is_deterministic(self) -> None:
        first = inspect_repository(self.root, self.head)
        second = inspect_repository(self.root, self.head)
        self.assertEqual(first, second)
        self.assertEqual(first.file_count, 2)
        self.assertIn("skills", first.top_level_entries)
        self.assertEqual(first.snapshot_commit, self.head)

    def test_stale_head_is_rejected_before_decomposition(self) -> None:
        with self.assertRaises(PmRepositoryInspectionError):
            inspect_repository(self.root, "a" * 40)

    def test_inventory_ignores_inherited_git_repository_overrides(self) -> None:
        poisoned = {
            "GIT_DIR": str(self.root / "missing-git-dir"),
            "GIT_WORK_TREE": str(self.root / "missing-work-tree"),
            "GIT_INDEX_FILE": str(self.root / "missing-index"),
            "GIT_OBJECT_DIRECTORY": str(self.root / "missing-objects"),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.bare",
            "GIT_CONFIG_VALUE_0": "true",
        }
        with mock.patch.dict("os.environ", poisoned):
            inventory = inspect_repository(self.root, self.head)
        self.assertEqual(inventory.snapshot_commit, self.head)
        self.assertEqual(inventory.file_count, 2)

    def test_git_invocation_scopes_safe_directory_to_the_inspected_root(self) -> None:
        with mock.patch("pm_repository_context.subprocess.run", wraps=subprocess.run) as run:
            inspect_repository(self.root, self.head)
        command = run.call_args_list[0].args[0]
        self.assertIn("core.longpaths=true", command)
        self.assertIn(f"safe.directory={self.root.resolve()}", command)


if __name__ == "__main__":
    unittest.main()
