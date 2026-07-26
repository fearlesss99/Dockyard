from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / "skills" / "agentdesk"
SCRIPT_NAMES = (
    "init_project.py",
    "render_views.py",
    "select_model.py",
    "validate_project.py",
    "validate_runtime.py",
)


def run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        cwd=cwd,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=30,
    )


def tracked_skill_files() -> list[Path]:
    result = run(
        "git",
        "ls-files",
        "-z",
        "--",
        SKILL_ROOT.relative_to(REPO_ROOT).as_posix(),
        cwd=REPO_ROOT,
    )
    if result.returncode != 0:
        raise AssertionError(result.stdout)
    return [REPO_ROOT / value for value in result.stdout.split("\0") if value]


class ReleaseSmokeTests(unittest.TestCase):
    def test_repository_release_files_and_install_command(self) -> None:
        for relative in (
            "README.md",
            "LICENSE",
            ".gitignore",
            ".github/workflows/validate.yml",
        ):
            self.assertTrue((REPO_ROOT / relative).is_file(), relative)

        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("--repo LeviXDD/AgentDeskSkill", readme)
        self.assertIn("--path skills/agentdesk", readme)
        self.assertIn("--ref v0.1.0-beta", readme)
        self.assertIn("$agentdesk", readme)

        template_runtime = (
            "skills/agentdesk/assets/project-template/.agentdesk/runtime/routes.yaml"
        )
        template_ignore = run(
            "git",
            "check-ignore",
            "--quiet",
            "--no-index",
            template_runtime,
            cwd=REPO_ROOT,
        )
        self.assertEqual(template_ignore.returncode, 1, template_ignore.stdout)

        root_runtime_ignore = run(
            "git",
            "check-ignore",
            "--quiet",
            "--no-index",
            ".agentdesk/runtime/routes.yaml",
            cwd=REPO_ROOT,
        )
        self.assertEqual(root_runtime_ignore.returncode, 0, root_runtime_ignore.stdout)

    def test_skill_metadata_and_interface_match_public_name(self) -> None:
        skill_md = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(skill_md.startswith("---\n"))
        self.assertIn("\nname: agentdesk\n", skill_md)
        self.assertRegex(skill_md, r"\ndescription:\s*\S")

        interface = (SKILL_ROOT / "agents" / "openai.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn("$agentdesk", interface)

    def test_installable_skill_tree_is_complete_and_clean(self) -> None:
        required = (
            "SKILL.md",
            "agents/openai.yaml",
            "scripts/init_project.py",
            "scripts/render_views.py",
            "scripts/select_model.py",
            "scripts/validate_project.py",
            "scripts/validate_runtime.py",
            "assets/project-template/.agentdesk/runtime/model-bindings.yaml",
            "assets/project-template/.agentdesk/runtime/routes.yaml",
            "assets/project-template/.agentdesk/runtime/transport-receipts.yaml",
        )
        for relative in required:
            self.assertTrue((SKILL_ROOT / relative).is_file(), relative)

        self.assertFalse((SKILL_ROOT / "README.md").exists())
        for path in tracked_skill_files():
            self.assertFalse(path.is_symlink(), str(path))
            self.assertNotIn("__pycache__", path.parts)
            self.assertNotEqual(path.name, ".DS_Store")
            self.assertNotEqual(path.suffix, ".pyc")

    def test_public_snapshot_contains_no_obvious_private_material(self) -> None:
        mac_home = re.escape("/" + "Users" + "/")
        linux_home = re.escape("/" + "home" + "/")
        patterns = (
            re.compile(mac_home + r"(?!example(?:/|\b))[^/\s]+(?:/|\b)", re.IGNORECASE),
            re.compile(linux_home + r"(?!example(?:/|\b))[^/\s]+(?:/|\b)", re.IGNORECASE),
            re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
            re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
            re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
        )
        for path in tracked_skill_files():
            content = path.read_text(encoding="utf-8")
            for pattern in patterns:
                self.assertIsNone(pattern.search(content), f"{pattern.pattern}: {path}")

    def test_all_scripts_expose_help(self) -> None:
        for name in SCRIPT_NAMES:
            result = run(sys.executable, str(SKILL_ROOT / "scripts" / name), "--help")
            self.assertEqual(result.returncode, 0, f"{name}:\n{result.stdout}")
            self.assertIn("usage:", result.stdout.lower())

    def _adr_path(self) -> Path:
        return SKILL_ROOT / "references" / "adr" / "001-mad-agentdesk-integration.md"

    def _cli_contract_path(self) -> Path:
        return (
            SKILL_ROOT
            / "references"
            / "public-interfaces"
            / "mad-cli-contract.md"
        )

    # -- MAD × AgentDesk integration contract tests -----------------------

    def test_mad_agentdesk_integration_documents_are_inside_skill_tree(self) -> None:
        """ADR and CLI contract must live under skills/agentdesk/references/."""
        adr = self._adr_path()
        self.assertTrue(adr.is_file(), f"Missing ADR: {adr}")
        contract = self._cli_contract_path()
        self.assertTrue(contract.is_file(), f"Missing CLI contract: {contract}")

        # Neither document may use a top-level docs/ path.
        top_level_adr = REPO_ROOT / "docs" / "adr" / "001-mad-agentdesk-integration.md"
        self.assertFalse(top_level_adr.exists(),
                         f"ADR must not be at top-level docs/: {top_level_adr}")
        top_level_contract = (
            REPO_ROOT / "docs" / "public-interfaces" / "mad-cli-contract.md"
        )
        self.assertFalse(top_level_contract.exists(),
                         f"CLI contract must not be at top-level docs/: {top_level_contract}")

    def test_adr_distinguishes_current_target_and_implemented_by(self) -> None:
        adr_text = self._adr_path().read_text(encoding="utf-8")
        self.assertIn("Current", adr_text,
                      "ADR must contain 'Current' status marker")
        self.assertIn("Target", adr_text,
                      "ADR must contain 'Target' status marker")
        self.assertIn("Implemented by", adr_text,
                      "ADR must contain 'Implemented by' column")

    def test_adr_does_not_claim_target_interfaces_as_current(self) -> None:
        adr_text = self._adr_path().read_text(encoding="utf-8")
        # Neither 'mad audit' nor 'mad agents --format json' may be
        # listed as Current anywhere in the ADR interface status table.
        import re

        # Find the interface status table section (between "## Interface Status"
        # and the next "## " heading).
        table_match = re.search(
            r"## Interface Status\s*\n(.*?)(?=\n## |\Z)",
            adr_text,
            re.DOTALL,
        )
        self.assertIsNotNone(table_match,
                             "ADR must have an '## Interface Status' section")
        table_text = table_match.group(1)

        # For each row containing 'mad audit' or 'mad agents --format json',
        # the Status column must NOT be 'Current'.
        for line in table_text.splitlines():
            if "mad audit" in line and "Target" not in line and "Current" not in line:
                continue  # not a status row
            if "mad agents --format json" in line:
                self.assertNotIn("Current", line,
                                 f"'mad agents --format json' must not be Current: {line.strip()}")
            if "mad audit" in line and "mad audit" not in line.split("|")[0].strip():
                # Row mentions 'mad audit' in the description, not the interface name
                pass
            elif "| mad audit " in line or line.strip().startswith("| `mad audit"):
                self.assertNotIn("Current", line,
                                 f"'mad audit' must not be Current: {line.strip()}")

        # Also check the CLI contract
        contract_text = self._cli_contract_path().read_text(encoding="utf-8")
        self.assertIn("Target", contract_text,
                      "CLI contract must mark Target interfaces")
        # The contract must not have 'mad audit' under a 'Current' heading
        # in its status table.
        status_section = re.search(
            r"## 1\. Interface Status Summary\s*\n(.*?)(?=\n## |\Z)",
            contract_text,
            re.DOTALL,
        )
        self.assertIsNotNone(status_section,
                             "CLI contract must have 'Interface Status Summary'")
        for line in status_section.group(1).splitlines():
            if "mad audit" in line:
                self.assertNotIn(
                    "Current",
                    line,
                    f"CLI contract: 'mad audit' must not be Current: {line.strip()}",
                )
            if "mad agents --format json" in line:
                self.assertNotIn(
                    "Current",
                    line,
                    f"CLI contract: 'mad agents --format json' must not be Current: {line.strip()}",
                )

    def test_adr_and_cli_contract_use_skill_internal_paths(self) -> None:
        for path in (self._adr_path(), self._cli_contract_path()):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("docs/adr/001-mad-agentdesk-integration.md", text,
                             f"{path.name}: must not reference top-level docs/ path")
            self.assertNotIn("docs/public-interfaces/mad-cli-contract.md", text,
                             f"{path.name}: must not reference top-level docs/ path")

    def test_skill_md_loads_integration_references_progressively(self) -> None:
        skill_text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("references/adr/001-mad-agentdesk-integration.md", skill_text,
                      "SKILL.md must reference the MAD integration ADR")
        self.assertIn("references/public-interfaces/mad-cli-contract.md", skill_text,
                      "SKILL.md must reference the MAD CLI contract")
        self.assertIn("MAD integration", skill_text,
                      "SKILL.md must mention MAD integration in the routing description")
        self.assertIn("MAD Gateway", skill_text,
                      "SKILL.md must mention MAD Gateway trigger words")

    def test_initializer_renderer_and_validator_smoke_flow(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agentdesk-release-smoke-") as temp:
            project = Path(temp) / "demo"
            init_result = run(
                sys.executable,
                str(SKILL_ROOT / "scripts" / "init_project.py"),
                "--project",
                str(project),
                "--project-id",
                "release-smoke",
                "--mode",
                "standard",
                "--pm-holder-id",
                "pm-release-smoke",
                "--init-git",
            )
            self.assertEqual(init_result.returncode, 0, init_result.stdout)

            render_result = run(
                sys.executable,
                str(SKILL_ROOT / "scripts" / "render_views.py"),
                "--project",
                str(project),
            )
            self.assertEqual(render_result.returncode, 0, render_result.stdout)

            validate_result = run(
                sys.executable,
                str(SKILL_ROOT / "scripts" / "validate_project.py"),
                "--project",
                str(project),
                "--pre-commit",
            )
            self.assertEqual(validate_result.returncode, 0, validate_result.stdout)
            self.assertRegex(validate_result.stdout, r"SUMMARY \d+ pass\(es\), \d+ warning\(s\), 0 error\(s\)")

            ignored = run(
                "git",
                "-C",
                str(project),
                "check-ignore",
                "--quiet",
                "--no-index",
                ".agentdesk/runtime/routes.yaml",
            )
            self.assertEqual(ignored.returncode, 0, ignored.stdout)


if __name__ == "__main__":
    unittest.main()
