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

    # -- Item 1: No Target interface may claim TC-13.1 --------------------

    def test_target_interfaces_do_not_reference_completed_tc13_1(self) -> None:
        """No Target 'Implemented by' may point to TC-13.1."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        contract_text = self._cli_contract_path().read_text(encoding="utf-8")

        # Find all table rows with 'Target' status and check their
        # Implemented-by column does not contain TC-13.1.
        for text, name in ((adr_text, "ADR"), (contract_text, "CLI contract")):
            # Look for lines like "| ... | **Target** | TC-13.1 |"
            for line in text.splitlines():
                if "Target" in line and "TC-13.1" in line:
                    # Make sure TC-13.1 is NOT in the Implemented-by column
                    # by checking it's not a status-table row claiming TC-13.1
                    # as the implementer for a Target.
                    pass  # we check below with the negative assertion
                if re.match(r"^\|\s+\d+\s+\|", line):
                    # Status table row
                    if "**Target**" in line and "TC-13.1" in line:
                        self.fail(
                            f"{name}: Target row must not reference TC-13.1 "
                            f"as Implemented by: {line.strip()}"
                        )

        # Also ensure no Target interface says "to be implemented by TC-13.1"
        for text, name in ((adr_text, "ADR"), (contract_text, "CLI contract")):
            pattern = re.compile(r"TC-13\.1\b")
            matches = pattern.findall(text)
            for _ in matches:
                # TC-13.1 may appear in prose but not as an Implemented-by target
                pass
            # Negative check: "Implemented by TC-13.1" in a Target context
            bogus = re.findall(
                r"(?:Implemented by|implemented by).*?TC-13\.1\b",
                text,
            )
            self.assertEqual(
                [],
                bogus,
                f"{name}: 'Implemented by' must not reference TC-13.1: {bogus}",
            )

    # -- Item 2: mad.agents/v1 uses root object with schema_version --------

    def test_mad_agents_v1_uses_root_object_with_schema_version(self) -> None:
        contract_text = self._cli_contract_path().read_text(encoding="utf-8")
        # The mad.agents/v1 example must show a root object, not a bare array.
        self.assertIn('"schema_version": "mad.agents/v1"', contract_text,
                      "CLI contract: mad.agents/v1 must declare schema_version")
        self.assertIn('"agents": [', contract_text,
                      "CLI contract: mad.agents/v1 must use root object with 'agents' array")
        # The example must NOT start with a bare array after the code fence.
        # Check that the JSON block after "### 3.1" contains a root object.
        section_31 = re.search(
            r"### 3\.1.*?```json\n(.*?)```",
            contract_text,
            re.DOTALL,
        )
        self.assertIsNotNone(section_31,
                             "CLI contract must have mad.agents/v1 section 3.1")
        json_block = section_31.group(1).strip()
        self.assertTrue(json_block.startswith("{"),
                        f"mad.agents/v1 JSON must start with root object '{{', got: {json_block[:80]}")

    def test_mad_agents_v1_forbids_executable_and_extra_args(self) -> None:
        contract_text = self._cli_contract_path().read_text(encoding="utf-8")
        adr_text = self._adr_path().read_text(encoding="utf-8")

        for text, name in ((contract_text, "CLI contract"), (adr_text, "ADR")):
            # The agent example in mad.agents/v1 must not expose executable.
            # Find the agent object example and verify it lacks forbidden fields.
            agent_example = re.search(
                r'"agents":\s*\[\s*\{.*?\}\s*\]',
                text,
                re.DOTALL,
            )
            if agent_example:
                example = agent_example.group(0)
                self.assertNotIn('"executable"', example,
                                 f"{name}: mad.agents/v1 example must not expose 'executable'")
                self.assertNotIn('"extra_args"', example,
                                 f"{name}: mad.agents/v1 example must not expose 'extra_args'")
                self.assertNotIn('"role"', example,
                                 f"{name}: mad.agents/v1 example must not expose 'role'")

            # Also verify the contract explicitly forbids these fields.
            self.assertIn("forbidden", text.lower(),
                          f"{name}: must document forbidden public fields for mad.agents/v1")

    def test_mad_agents_v1_does_not_claim_full_agentprofile(self) -> None:
        contract_text = self._cli_contract_path().read_text(encoding="utf-8")
        self.assertNotIn(
            "all fields of AgentProfile",
            contract_text,
            "CLI contract: must not claim mad.agents/v1 outputs all AgentProfile fields",
        )

    # -- Item 3: run-result/v1 backward compatibility ----------------------

    def test_run_result_v1_maintains_backward_compat(self) -> None:
        contract_text = self._cli_contract_path().read_text(encoding="utf-8")

        # V1 must explicitly state participants is list[str].
        self.assertIn('list[str]', contract_text,
                      "CLI contract: must document participants as list[str] in V1")

        # V1 must not change status to English enums.
        self.assertIn("完成", contract_text,
                      "CLI contract: run-result/v1 must keep Chinese status strings")
        self.assertNotIn(
            '"status": "completed"',
            contract_text,
            "CLI contract: run-result/v1 must not use English status 'completed'",
        )
        self.assertNotIn(
            '"completed_with_warnings"',
            contract_text,
            "CLI contract: run-result/v1 must not use English status 'completed_with_warnings'",
        )

        # V1 must keep participants as list[str], not object array.
        agent_object_participants = re.search(
            r'"participants":\s*\[.*?"agent_id".*?\]',
            contract_text,
            re.DOTALL,
        )
        # Only the audit-result section has object participants — run-result must not.
        # Check that the run-result/v1 JSON example has a simple string array.
        run_result_section = re.search(
            r"### 3\.2.*?```json\n(.*?)```",
            contract_text,
            re.DOTALL,
        )
        self.assertIsNotNone(run_result_section,
                             "CLI contract must have run-result/v1 section 3.2")
        run_result_json = run_result_section.group(1)
        self.assertIn('"participants": ["', run_result_json,
                      "run-result/v1: participants must be list[str]")
        self.assertNotIn('"agent_id"', run_result_json,
                         "run-result/v1: participants must not be object array")

    # -- Item 4: Separate exit codes for deliberate / resume / agents ------

    def test_deliberate_and_resume_exit_codes_are_documented_separately(self) -> None:
        contract_text = self._cli_contract_path().read_text(encoding="utf-8")

        # deliberate exit codes must include 0, 1, 2, 3, 130.
        for code in ("0", "1", "2", "3", "130"):
            self.assertIn(
                code,
                contract_text,
                f"CLI contract: exit code {code} must appear for deliberate",
            )

        # resume section must document its own exit codes.
        self.assertIn("### 2.3", contract_text,
                      "CLI contract: must have a separate resume section (2.3)")

        # resume must explicitly state it does NOT have exit code 3.
        resume_section = re.search(
            r"### 2\.3.*?(?=### |## )",
            contract_text,
            re.DOTALL,
        )
        self.assertIsNotNone(resume_section,
                             "CLI contract: must have resume exit codes section")
        resume_text = resume_section.group(0)
        self.assertIn("not", resume_text.lower(),
                      "CLI contract: resume must document absence of exit code 3")
        # resume exit codes are 0, 1, 130 (and 2 for argparse).
        self.assertIn("`0`", resume_text)
        self.assertIn("`1`", resume_text)
        self.assertIn("`130`", resume_text)

    # -- Item 5: agentdesk.mad-refs/v1 runtime schema ----------------------

    def test_cli_contract_contains_mad_refs_v1_schema(self) -> None:
        """CLI contract must document agentdesk.mad-refs/v1 with key fields."""
        contract_text = self._cli_contract_path().read_text(encoding="utf-8")

        self.assertIn(
            "agentdesk.mad-refs/v1",
            contract_text,
            "CLI contract: must define agentdesk.mad-refs/v1 schema",
        )
        self.assertIn(
            "stdout_sha256",
            contract_text,
            "CLI contract: must document stdout_sha256 in mad-refs/v1",
        )
        self.assertIn(
            "report_sha256",
            contract_text,
            "CLI contract: must document report_sha256 in mad-refs/v1",
        )
        self.assertIn(
            "archive_path",
            contract_text,
            "CLI contract: must document archive_path in mad-refs/v1",
        )
        self.assertIn(
            "runtime-only",
            contract_text,
            "CLI contract: must state archive_path is runtime-only",
        )

    def test_adr_mentions_mad_refs_v1(self) -> None:
        adr_text = self._adr_path().read_text(encoding="utf-8")
        self.assertIn(
            "agentdesk.mad-refs/v1",
            adr_text,
            "ADR: must reference agentdesk.mad-refs/v1 runtime schema",
        )

    def test_mad_refs_archive_path_is_runtime_only_and_never_in_git(self) -> None:
        contract_text = self._cli_contract_path().read_text(encoding="utf-8")
        # Must explicitly prohibit committing archive_path to Git.
        self.assertIn(
            "never",
            contract_text,
            "CLI contract: mad-refs must state 'never' for Git inclusion",
        )

    # -- Item 6: audit-result/v1 semantics ---------------------------------

    def test_audit_result_v1_status_vs_verdict_separation(self) -> None:
        contract_text = self._cli_contract_path().read_text(encoding="utf-8")

        # status describes the process; verdict describes business finding.
        self.assertIn('"status"', contract_text)
        self.assertIn('"verdict"', contract_text)

        # verdict=blocked is NOT an infrastructure failure.
        self.assertIn(
            "blocked",
            contract_text.lower(),
            "CLI contract: must document verdict: blocked",
        )

        # participants is list[str] in audit-result/v1.
        audit_section = re.search(
            r"### 3\.3.*?```json\n(.*?)```",
            contract_text,
            re.DOTALL,
        )
        self.assertIsNotNone(audit_section,
                             "CLI contract: must have mad audit section 3.3")
        audit_json = audit_section.group(1)
        self.assertIn('"participants": ["', audit_json,
                      "audit-result/v1: participants must be list[str]")

    def test_audit_exit_zero_includes_verdict_fail_and_blocked(self) -> None:
        contract_text = self._cli_contract_path().read_text(encoding="utf-8")
        self.assertIn(
            "exit code is still 0",
            contract_text,
            "CLI contract: must state exit 0 for verdict=fail/blocked",
        )

    # -- Item 7: ADR does not reference non-existent TC-12.3.1 dependency ---

    def test_adr_does_not_reference_nonexistent_tc12_3_1_dependency(self) -> None:
        adr_text = self._adr_path().read_text(encoding="utf-8")
        # No task card should depend on TC-12.3.1.
        self.assertNotIn("TC-12.3.1", adr_text,
                         "ADR: must not reference non-existent TC-12.3.1")

    # -- Preserved original tests ------------------------------------------

    def test_adr_does_not_claim_target_interfaces_as_current(self) -> None:
        adr_text = self._adr_path().read_text(encoding="utf-8")
        contract_text = self._cli_contract_path().read_text(encoding="utf-8")

        # Find the interface status table in ADR.
        table_match = re.search(
            r"## Interface Status\s*\n(.*?)(?=\n## |\Z)",
            adr_text,
            re.DOTALL,
        )
        self.assertIsNotNone(table_match,
                             "ADR must have an '## Interface Status' section")
        table_text = table_match.group(1)

        for line in table_text.splitlines():
            if "mad agents --format json" in line:
                self.assertNotIn("**Current**", line,
                                 f"'mad agents --format json' must not be Current: {line.strip()}")
            if "| `mad audit " in line or line.strip().startswith("| `mad audit"):
                self.assertNotIn("**Current**", line,
                                 f"'mad audit' must not be Current: {line.strip()}")

        # Also check the CLI contract status summary.
        status_section = re.search(
            r"## 1\. Interface Status Summary\s*\n(.*?)(?=\n## |\Z)",
            contract_text,
            re.DOTALL,
        )
        self.assertIsNotNone(status_section,
                             "CLI contract must have 'Interface Status Summary'")
        for line in status_section.group(1).splitlines():
            if "mad audit" in line and "Target" not in line:
                pass
            elif "mad audit" in line:
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
