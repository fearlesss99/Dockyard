from __future__ import annotations

import json
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


def _extract_markdown_section(text: str, heading: str) -> str | None:
    """Return the body of *heading* up to the next same-or-higher-level heading.

    ``heading`` must include the leading ``#`` characters, e.g. ``"### 3.2"``.
    The heading line itself is *not* included in the returned text.
    Matches any heading line that *starts with* ``heading`` followed by a space
    or end-of-line — so ``"### 3.2"`` matches both ``"### 3.2 Foo"`` and
    ``"### 3.2"``.
    """
    heading_level = len(heading) - len(heading.lstrip("#"))
    escaped = re.escape(heading)
    start_m = re.search(rf"^{escaped}(?:\s.*)?$", text, re.MULTILINE)
    if not start_m:
        return None
    body_start = start_m.end()
    remaining = text[body_start:]
    # Match any heading at the same level or higher (fewer #).
    stop_pat = re.compile(rf"^#{{1,{heading_level}}}\s+\S", re.MULTILINE)
    stop_m = stop_pat.search(remaining)
    if stop_m:
        return remaining[: stop_m.start()].rstrip()
    return remaining.rstrip()


def _strip_md_fmt(cell: str) -> str:
    """Strip common markdown inline formatting from a table cell.

    Removes backticks, bold/italic markers, and collapses whitespace.
    """
    result = cell.replace("`", "")
    result = result.replace("**", "")
    result = result.replace("*", "")
    result = result.replace("_", "")
    return " ".join(result.split())


def _find_exit_code_table_row(section_text: str, target_code: str) -> str | None:
    """Parse a markdown exit-code table and return the condition cell for
    *target_code* (e.g. ``"0"``).

    Recognises code cells like ``| `0` |`` and ``| 0 |`` equally — the
    comparison is done after stripping backticks and whitespace from the
    first cell.
    """
    # Find the first markdown table that looks like an exit-code table.
    in_table = False
    header: list[str] = []
    for raw in section_text.splitlines():
        stripped = raw.strip()
        if not in_table:
            # Heuristic: a table whose header includes "Exit" and "Condition"
            # or "Exit" and "Meaning".
            if (
                stripped.startswith("|")
                and ("Exit" in stripped)
                and ("Condition" in stripped or "Meaning" in stripped)
            ):
                in_table = True
                header = [c.strip() for c in stripped.strip("|").split("|")]
                continue
            continue
        # End of table.
        if stripped == "" or stripped.startswith("#"):
            break
        # Separator line.
        if re.match(r"^\|[\s\-:|]+\|", stripped):
            continue
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) < 2:
            continue
        code_cell = _strip_md_fmt(cells[0])
        # Compare after stripping to normalise `0` vs `` `0` ``.
        if code_cell == target_code:
            # Return the condition/meaning cell (second column).
            return cells[1].strip()
    return None


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
        """No Target 'Implemented by' may point to TC-13.1.

        Parses the interface-status table by header-column positions
        (Status / Implemented by), then only inspects rows whose Status
        cell semantics is Target.  Exact task-id matching ensures that
        TC-13.10 / TC-13.11 / TC-13.15 are not mistaken for TC-13.1.
        """
        adr_text = self._adr_path().read_text(encoding="utf-8")
        contract_text = self._cli_contract_path().read_text(encoding="utf-8")

        def _parse_status_table(text: str) -> list[dict[str, str]]:
            """Extract rows from the first Interface-Status-like markdown table.

            Returns a list of dicts keyed by normalised header column names.
            All non-separator, non-empty rows are parsed — no first-column
            shape gate.  Rows that start with ``|`` and are not a separator
            line or header are treated as data.
            """
            rows: list[dict[str, str]] = []
            header: list[str] = []
            in_table = False
            for raw in text.splitlines():
                stripped = raw.strip()
                # Look for a table header row that contains the expected columns.
                if not in_table and stripped.startswith("|") and "Status" in stripped and "Implemented by" in stripped:
                    in_table = True
                    header = [c.strip() for c in stripped.strip("|").split("|")]
                    continue
                if not in_table:
                    continue
                # End of table: blank line or next heading.
                if stripped == "" or stripped.startswith("#"):
                    break
                # Separator line: skip.
                if re.match(r"^\|[\s\-:|]+\|", stripped):
                    continue
                if not stripped.startswith("|"):
                    continue
                cells = [c.strip() for c in stripped.strip("|").split("|")]
                # Accept any non-empty row whose cell count matches the header.
                if len(cells) < 2:
                    continue
                row = {header[i]: cells[i] for i in range(min(len(header), len(cells)))}
                rows.append(row)
            return rows

        for text, name in ((adr_text, "ADR"), (contract_text, "CLI contract")):
            rows = _parse_status_table(text)
            self.assertGreater(len(rows), 0,
                               f"{name}: must contain a parseable interface-status table")

            status_col = "#"  # first numeric column, or the Status column
            impl_col = "Implemented by"

            for row in rows:
                # Determine the status cell — either a dedicated "Status" column
                # or the column named after the row-number header (which is "#"
                # per the ADR table header).
                status_cell = row.get("Status", row.get(status_col, "")).strip()
                # Only inspect Target rows.
                if "Target" not in status_cell:
                    continue

                impl_cell = row.get(impl_col, "").strip()
                # Exact task-id matching: TC-13.1 must NOT appear as an
                # Implemented-by value.  This rejects "TC-13.1" while accepting
                # "TC-13.10", "TC-13.11", "TC-13.15", etc.
                task_ids = re.findall(r"\b(TC-\d+(?:\.\d+)*)\b", impl_cell)
                for tid in task_ids:
                    self.assertNotEqual(
                        "TC-13.1", tid,
                        f"{name}: Target row {row} must not be "
                        f"Implemented by TC-13.1",
                    )

        # Also enforce: the prose must never claim "Implemented by TC-13.1"
        # in any Target context.  This is a secondary check covering the
        # target-interface descriptions (section 3) that don't use the
        # status table.
        for text, name in ((adr_text, "ADR"), (contract_text, "CLI contract")):
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
        """Verify run-result/v1 section keeps participants=list[str] and
        Chinese-status semantics — scoped strictly to the 3.2 subsection.

        Uses JSON parsing on the embedded code-fence example so that
        object-array participants are caught regardless of field names.
        """
        contract_text = self._cli_contract_path().read_text(encoding="utf-8")

        section_32 = _extract_markdown_section(contract_text, "### 3.2")
        self.assertIsNotNone(
            section_32,
            "CLI contract must have run-result/v1 section 3.2",
        )
        text: str = section_32  # type: ignore[assignment]

        # ── (A) participants must be list[str] — JSON-parse the example ──
        # Extract the first JSON code block inside the section.
        json_m = re.search(r"```json\n(.*?)```", text, re.DOTALL)
        self.assertIsNotNone(
            json_m,
            "run-result/v1 section: must contain a JSON code block",
        )
        try:
            example = json.loads(json_m.group(1))
        except json.JSONDecodeError as exc:
            self.fail(
                f"run-result/v1 section: JSON example is not valid JSON: {exc}"
            )

        participants = example.get("participants")
        self.assertIsInstance(
            participants,
            list,
            "run-result/v1 section: 'participants' must be a list",
        )
        self.assertGreater(
            len(participants),
            0,
            "run-result/v1 section: 'participants' must be a non-empty list",
        )
        for i, p in enumerate(participants):
            self.assertIsInstance(
                p,
                str,
                f"run-result/v1 section: participants[{i}] must be str, "
                f"got {type(p).__name__}: {p!r}",
            )

        # (B) status must keep Chinese string semantics.
        chinese_ok = ("完成" in text) or ("<MAD status" in text)
        self.assertTrue(
            chinese_ok,
            "run-result/v1 section: status must use Chinese semantics "
            "(e.g. 完成 / 带警告完成) or a MAD-status placeholder",
        )

        # (C) V1 must NOT change status to English enums.
        english_status = re.search(
            r'"status":\s*"(?:completed|completed_with_warnings|failed|blocked)"',
            text,
        )
        if english_status is not None:
            self.fail(
                f"run-result/v1 section: status must not be an English enum "
                f"(found {english_status.group(0)!r} in run-result/v1 section)"
            )

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
        """Exit code 0 for 'mad audit' must cover pass, fail, and blocked
        business verdicts — scoped strictly to the 3.3 subsection.

        Parses the exit-code Markdown table by extracting its cells,
        normalises the code cell (strips backticks and whitespace), then
        asserts the matching condition cell contains all three verdict
        tokens.  Also verifies the prose concretely links exit 0 to
        fail/blocked.
        """
        contract_text = self._cli_contract_path().read_text(encoding="utf-8")

        section_33 = _extract_markdown_section(contract_text, "### 3.3")
        self.assertIsNotNone(
            section_33,
            "CLI contract must have mad audit section 3.3",
        )
        text: str = section_33  # type: ignore[assignment]

        # ── Parse the exit-code table ──
        exit_row = _find_exit_code_table_row(text, "0")
        self.assertIsNotNone(
            exit_row,
            "audit section 3.3: must have an exit-code table with a row "
            "for code 0",
        )
        condition_cell: str = exit_row  # type: ignore[assignment]

        # The condition cell must mention all three business verdicts.
        normalised = _strip_md_fmt(condition_cell)
        for verdict in ("pass", "fail", "blocked"):
            self.assertIn(
                verdict,
                normalised,
                f"audit section 3.3: exit-code-0 condition must mention "
                f"verdict '{verdict}'; got: {condition_cell.strip()!r}",
            )

        # ── Prose: find a sentence that explicitly links exit 0 to
        #    verdict=fail or verdict=blocked (not just any "exit code … 0"
        #    mention). ──
        linked = re.findall(
            r"(?:exit code.*?0|exit\s+0).*?(?:fail|blocked)",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        self.assertGreater(
            len(linked),
            0,
            "audit section 3.3: prose must explicitly state that exit code 0 "
            "covers verdict=fail/blocked scenarios",
        )

    # -- Item 8: TC-13.4 core type contract -------------------------------

    TC13_4_VALUES = frozenset(
        {
            "TaskDifficulty",
            "MadDeliberationDepth",
            "WorkerKind",
        }
    )

    TASK_DIFFICULTY_VALUES = frozenset(
        {"basic", "standard", "advanced", "expert"}
    )

    MAD_DELIBERATION_DEPTH_VALUES = frozenset(
        {"fast", "balanced", "deep"}
    )

    WORKER_KIND_VALUES = frozenset(
        {"basic_agent", "standard_agent", "advanced_agent", "expert_agent"}
    )

    NEW_TC13_IDS = frozenset({"TC-13.4", "TC-13.5.1", "TC-13.7", "TC-13.8"})

    def _parse_future_task_cards_table(self, text: str) -> list[dict[str, str]]:
        """Parse the ADR §5 Future Task Cards markdown table.

        Returns a list of dicts keyed by normalised header column names
        (Task Card, Description, Depends on).  Only rows with a TC-*
        task id are returned.
        """
        rows: list[dict[str, str]] = []
        header: list[str] = []
        in_table = False
        for raw in text.splitlines():
            stripped = raw.strip()
            if not in_table:
                if (
                    stripped.startswith("|")
                    and "Task Card" in stripped
                    and "Depends on" in stripped
                ):
                    in_table = True
                    header = [c.strip() for c in stripped.strip("|").split("|")]
                continue
            if stripped == "" or stripped.startswith("#"):
                break
            if re.match(r"^\|[\s\-:|]+\|", stripped):
                continue
            if not stripped.startswith("|"):
                continue
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if len(cells) < 2:
                continue
            # Only rows whose first cell looks like a task id.
            if not re.match(r"\bTC-\d+(?:\.\d+)?\b", cells[0]):
                continue
            row = {
                header[i]: cells[i]
                for i in range(min(len(header), len(cells)))
            }
            rows.append(row)
        return rows

    def _parse_interface_status_table(self, text: str) -> list[dict[str, str]]:
        """Parse the ADR §1 Interface Status markdown table.

        Returns a list of dicts keyed by normalised header names. Rows
        whose first cell is a numeric interface number are data rows.
        """
        rows: list[dict[str, str]] = []
        header: list[str] = []
        in_table = False
        for raw in text.splitlines():
            stripped = raw.strip()
            if not in_table:
                if (
                    stripped.startswith("|")
                    and "Status" in stripped
                    and "Implemented by" in stripped
                ):
                    in_table = True
                    header = [c.strip() for c in stripped.strip("|").split("|")]
                continue
            if stripped == "" or stripped.startswith("#"):
                break
            if re.match(r"^\|[\s\-:|]+\|", stripped):
                continue
            if not stripped.startswith("|"):
                continue
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if len(cells) < 2:
                continue
            # Only rows whose first cell is a number.
            if not re.match(r"^\d+$", cells[0]):
                continue
            row = {
                header[i]: cells[i]
                for i in range(min(len(header), len(cells)))
            }
            rows.append(row)
        return rows

    def _resolve_col(self, row: dict[str, str], *names: str) -> str:
        for name in names:
            val = row.get(name, "")
            if val.strip():
                return val.strip()
        return ""

    # -- 8a: Future Task Cards table includes the four new cards ----------

    def test_future_task_cards_include_tc134_1351_137_138(self) -> None:
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_future_task_cards_table(adr_text)
        self.assertGreater(
            len(rows),
            5,
            "ADR §5 must contain a parseable Future Task Cards table",
        )

        task_ids_seen = {
            self._resolve_col(row, "Task Card", "Task", "#")
            for row in rows
        }
        tc_ids_required = frozenset({"TC-13.4", "TC-13.5.1", "TC-13.7", "TC-13.8"})
        missing = tc_ids_required - task_ids_seen
        self.assertSetEqual(
            missing,
            set(),
            f"ADR §5 Future Task Cards is missing: {', '.join(sorted(missing))}",
        )

    def test_tc134_1351_137_138_descriptions_are_accurate(self) -> None:
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_future_task_cards_table(adr_text)
        by_id = {
            self._resolve_col(row, "Task Card", "Task", "#"): row
            for row in rows
        }

        tc134 = by_id.get("TC-13.4")
        self.assertIsNotNone(tc134, "TC-13.4 must exist in Future Task Cards")
        desc_134 = self._resolve_col(tc134, "Description", "Desc")
        self.assertIn("TaskDifficulty", desc_134)
        self.assertIn("MadDeliberationDepth", desc_134)
        self.assertIn("WorkerKind", desc_134)

        tc1351 = by_id.get("TC-13.5.1")
        self.assertIsNotNone(tc1351, "TC-13.5.1 must exist in Future Task Cards")
        desc_1351 = self._resolve_col(tc1351, "Description", "Desc")
        self.assertIn("ContextBudgetPolicy", desc_1351)
        self.assertIn("64", desc_1351)  # hard cap mention

        tc137 = by_id.get("TC-13.7")
        self.assertIsNotNone(tc137, "TC-13.7 must exist in Future Task Cards")
        desc_137 = self._resolve_col(tc137, "Description", "Desc")
        self.assertIn("DispatcherAgentGateway", desc_137)

        tc138 = by_id.get("TC-13.8")
        self.assertIsNotNone(tc138, "TC-13.8 must exist in Future Task Cards")
        desc_138 = self._resolve_col(tc138, "Description", "Desc")
        self.assertIn("Claude", desc_138)
        self.assertIn("CLI", desc_138)

    def test_future_task_cards_dependencies_are_accurate(self) -> None:
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_future_task_cards_table(adr_text)
        by_id = {
            self._resolve_col(row, "Task Card", "Task", "#"): row
            for row in rows
        }

        # TC-13.4 depends on TC-13.3
        tc134_dep = self._resolve_col(by_id.get("TC-13.4", {}), "Depends on", "Dep")
        self.assertIn("TC-13.3", tc134_dep,
                      "TC-13.4 must depend on TC-13.3")

        # TC-13.5.1 depends on TC-13.4
        tc1351_dep = self._resolve_col(by_id.get("TC-13.5.1", {}), "Depends on", "Dep")
        self.assertIn("TC-13.4", tc1351_dep,
                      "TC-13.5.1 must depend on TC-13.4")

        # TC-13.6 depends on TC-13.2 and TC-13.4
        tc136_dep = self._resolve_col(by_id.get("TC-13.6", {}), "Depends on", "Dep")
        self.assertIn("TC-13.2", tc136_dep,
                      "TC-13.6 must depend on TC-13.2")
        self.assertIn("TC-13.4", tc136_dep,
                      "TC-13.6 must depend on TC-13.4")

        # TC-13.7 depends on TC-13.4 and TC-13.6
        tc137_dep = self._resolve_col(by_id.get("TC-13.7", {}), "Depends on", "Dep")
        self.assertIn("TC-13.4", tc137_dep,
                      "TC-13.7 must depend on TC-13.4")
        self.assertIn("TC-13.6", tc137_dep,
                      "TC-13.7 must depend on TC-13.6")

        # TC-13.8 depends on TC-13.4
        tc138_dep = self._resolve_col(by_id.get("TC-13.8", {}), "Depends on", "Dep")
        self.assertIn("TC-13.4", tc138_dep,
                      "TC-13.8 must depend on TC-13.4")

        # TC-13.9 depends on TC-13.5.1, TC-13.7, TC-13.8
        tc139_dep = self._resolve_col(by_id.get("TC-13.9", {}), "Depends on", "Dep")
        self.assertIn("TC-13.5.1", tc139_dep,
                      "TC-13.9 must depend on TC-13.5.1")
        self.assertIn("TC-13.7", tc139_dep,
                      "TC-13.9 must depend on TC-13.7")
        self.assertIn("TC-13.8", tc139_dep,
                      "TC-13.9 must depend on TC-13.8")

    # -- 8b: TC-13.4 section contains all three enums with exact values ----

    def test_tc134_section_defines_three_enums(self) -> None:
        adr_text = self._adr_path().read_text(encoding="utf-8")
        # Extract the TC-13.4 section — either a dedicated sub-section or
        # the §2.8 heading.
        section = _extract_markdown_section(adr_text, "### 2.8")
        self.assertIsNotNone(section, "ADR must contain a §2.8 for TC-13.4")
        self.assertIn("TC-13.4", section,
                      "ADR §2.8 must reference TC-13.4")

        for enum_name in sorted(self.TC13_4_VALUES):
            self.assertIn(
                enum_name,
                section,
                f"ADR §2.8 must define {enum_name}",
            )

    def test_task_difficulty_exact_values(self) -> None:
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.8.1")
        self.assertIsNotNone(section, "ADR must contain §2.8.1 TaskDifficulty")
        for value in sorted(self.TASK_DIFFICULTY_VALUES):
            # Look for the value as a code-quoted key or table cell.
            self.assertIn(
                f"`{value}`",
                section,
                f"ADR §2.8.1 must contain `{value}`",
            )

    def test_mad_deliberation_depth_exact_values(self) -> None:
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.8.2")
        self.assertIsNotNone(section, "ADR must contain §2.8.2 MadDeliberationDepth")
        for value in sorted(self.MAD_DELIBERATION_DEPTH_VALUES):
            self.assertIn(
                f"`{value}`",
                section,
                f"ADR §2.8.2 must contain `{value}`",
            )

    def test_worker_kind_exact_values(self) -> None:
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.8.3")
        self.assertIsNotNone(section, "ADR must contain §2.8.3 WorkerKind")
        for value in sorted(self.WORKER_KIND_VALUES):
            self.assertIn(
                f"`{value}`",
                section,
                f"ADR §2.8.3 must contain `{value}`",
            )

    # -- 8c: Conceptual separation from model tier / deliberation tier ----

    def test_task_difficulty_is_distinct_from_model_tier(self) -> None:
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.8.1")
        self.assertIsNotNone(section)

        # Must explicitly state it is NOT a model tier.
        self.assertIn(
            "not",
            section.lower(),
            "ADR §2.8.1 must explicitly state TaskDifficulty is not a model tier",
        )
        # Must warn against interchanging the two even when strings coincide.
        self.assertIn(
            "interchangeable",
            section.lower(),
            "ADR §2.8.1 must warn against interchanging with model tier",
        )
        # Must name model-bindings/v2 or "model tier".
        has_model_ref = (
            "model-bindings/v2" in section
            or "model tier" in section.lower()
        )
        self.assertTrue(
            has_model_ref,
            "ADR §2.8.1 must reference model tier or model-bindings/v2",
        )

    def test_mad_deliberation_depth_is_distinct_from_deliberation_tier(self) -> None:
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.8.2")
        self.assertIsNotNone(section)

        # Must name the AgentDesk deliberation_tier enum for contrast.
        self.assertIn(
            "efficient",
            section,
            "ADR §2.8.2 must reference 'efficient' to contrast with 'fast'",
        )
        # Must state the two sets are separate / distinct.
        self.assertTrue(
            "separate" in section.lower() or "distinct" in section.lower()
            or "different" in section.lower()
            or "not an agentdesk" in section.lower(),
            "ADR §2.8.2 must state MadDeliberationDepth is distinct from "
            "AgentDesk deliberation_tier",
        )

    # -- 8d: TC-13.4 explicitly excludes budget calc and WorkerAdapter ----

    def test_tc134_excludes_context_budget_policy(self) -> None:
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.8.4")
        self.assertIsNotNone(section, "ADR must contain §2.8.4 Non-Goals")

        # Must reference TC-13.5 as the owner of budget calculation.
        self.assertIn(
            "TC-13.5",
            section,
            "ADR §2.8.4 must reference TC-13.5 for budget calculation",
        )

    def test_tc134_excludes_worker_adapter_implementation(self) -> None:
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.8.4")
        self.assertIsNotNone(section)

        # Must explicitly exclude Worker scheduling / slot / lease.
        self.assertIn(
            "TC-13.9",
            section,
            "ADR §2.8.4 must reference TC-13.9 as out-of-scope",
        )

    # -- 8e: New interfaces (TC-13.7/8) remain Target; TC-13.4 and TC-13.5 are now Current

    STILL_TARGET_TC13_IDS = frozenset({"TC-13.8"})

    def test_new_non_tc134_interfaces_remain_target(self) -> None:
        """TC-13.7, TC-13.8 must still be Target.

        TC-13.4 and TC-13.5 are now Current (completed), so they are
        excluded from this check.
        """
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_interface_status_table(adr_text)
        self.assertGreater(
            len(rows),
            0,
            "ADR must contain a parseable Interface Status table",
        )

        for row in rows:
            impl_cell = self._resolve_col(row, "Implemented by", "Impl", "Notes")
            task_ids = re.findall(r"\b(TC-\d+(?:\.\d+)*)\b", impl_cell)
            still_target = [
                tid for tid in task_ids if tid in self.STILL_TARGET_TC13_IDS
            ]
            if not still_target:
                continue

            status_cell = self._resolve_col(row, "Status")
            self.assertIn(
                "Target",
                status_cell,
                f"ADR Interface Status: {', '.join(still_target)} "
                f"must be Target, got status={status_cell!r}",
            )
            self.assertNotIn(
                "Current",
                status_cell,
                f"ADR Interface Status: {', '.join(still_target)} "
                "must NOT be marked Current",
            )

    def test_tc134_interface_row_is_now_current(self) -> None:
        """TC-13.4's Interface Status row (#27) must be Current."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_interface_status_table(adr_text)

        tc134_row = None
        for row in rows:
            impl_cell = self._resolve_col(row, "Implemented by", "Impl", "Notes")
            task_ids = re.findall(r"\b(TC-\d+(?:\.\d+)*)\b", impl_cell)
            if "TC-13.4" in task_ids:
                tc134_row = row
                break

        self.assertIsNotNone(
            tc134_row,
            "ADR Interface Status must contain a row Implemented-by TC-13.4",
        )
        status_cell = self._resolve_col(tc134_row, "Status")
        self.assertIn(
            "Current",
            status_cell,
            f"ADR Interface Status #27 (TC-13.4) must be Current, "
            f"got status={status_cell!r}",
        )

    def test_section_28_heading_is_current(self) -> None:
        """§2.8 heading must read (Current — TC-13.4)."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "### 2.8")
        self.assertIsNotNone(section, "ADR must contain §2.8")

        # Re-read the heading line directly so we don't rely on
        # section-body-only extraction.
        heading_m = re.search(
            r"^### 2\.8\s.*$", adr_text, re.MULTILINE
        )
        self.assertIsNotNone(heading_m, "ADR must have a §2.8 heading")
        heading = heading_m.group(0)
        self.assertIn(
            "Current",
            heading,
            f"§2.8 heading must say Current, got: {heading.strip()!r}",
        )
        self.assertNotIn(
            "Target",
            heading,
            f"§2.8 heading must NOT say Target, got: {heading.strip()!r}",
        )

    # -- 8e2: TC-13.5 interface row is now Current ------------------------

    def test_tc1351_interface_row_is_now_current(self) -> None:
        """TC-13.5.1's Interface Status row (#28) must be Current."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_interface_status_table(adr_text)

        tc1351_row = None
        for row in rows:
            impl_cell = self._resolve_col(row, "Implemented by", "Impl", "Notes")
            task_ids = re.findall(r"\b(TC-\d+(?:\.\d+)*)\b", impl_cell)
            if "TC-13.5.1" in task_ids:
                tc1351_row = row
                break

        self.assertIsNotNone(
            tc1351_row,
            "ADR Interface Status must contain a row Implemented-by TC-13.5.1",
        )
        status_cell = self._resolve_col(tc1351_row, "Status")
        self.assertIn(
            "Current",
            status_cell,
            f"ADR Interface Status #28 (TC-13.5.1) must be Current, "
            f"got status={status_cell!r}",
        )

    def test_context_budget_policy_section_is_standalone_section_29(self) -> None:
        """ADR must contain independent §2.9 ContextBudgetPolicy, not nested
        under §2.8."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        # Verify §2.9 exists as an independent heading.
        section_29 = _extract_markdown_section(adr_text, "### 2.9")
        self.assertIsNotNone(
            section_29,
            "ADR must contain independent ### 2.9 ContextBudgetPolicy",
        )
        # Must be marked Current.
        heading_m = re.search(
            r"^### 2\.9\s.*$", adr_text, re.MULTILINE
        )
        self.assertIsNotNone(heading_m, "ADR must have a §2.9 heading")
        heading = heading_m.group(0)
        self.assertIn(
            "Current",
            heading,
            f"§2.9 heading must say Current, got: {heading.strip()!r}",
        )
        # Must NOT be nested as #### 2.8.5.
        self.assertNotIn(
            "#### 2.8.5 ContextBudgetPolicy",
            adr_text,
            "ADR must NOT contain #### 2.8.5 — now §2.9",
        )
        # Must reference TC-13.5.1.
        self.assertIn("TC-13.5.1", section_29)
        # Must mention all four percentages.
        for pct in ("20", "35", "50", "65"):
            self.assertIn(
                pct,
                section_29,
                f"ADR §2.9 must mention {pct}%",
            )
        # Must include the min(floor %, cap) formula.
        self.assertIn("min(", section_29,
                       "ADR §2.9 must contain min(percentage_budget, cap) formula")
        self.assertIn("// 100", section_29,
                       "ADR §2.9 must contain floor integer formula")
        # Must include the reserved tokens formula.
        self.assertIn("reserved_tokens", section_29,
                       "ADR §2.9 must contain reserved_tokens formula")
        # Must reference BudgetResult as six-field.
        self.assertIn("BudgetResult", section_29,
                       "ADR §2.9 must reference BudgetResult")
        self.assertIn("six-field", section_29.lower(),
                       "ADR §2.9 must describe BudgetResult as six-field")
        # Must mention all four hard caps.
        for cap in ("64,000", "128,000", "256,000", "512,000"):
            self.assertIn(
                cap,
                section_29,
                f"ADR §2.9 must mention cap {cap}",
            )
        # Must reference TC-13.9 WorkerAdapter consumption.
        self.assertIn("TC-13.9", section_29,
                       "ADR §2.9 must reference TC-13.9 consumption")

    def test_section_28_does_not_contain_tc135_public_api(self) -> None:
        """§2.8 continues to belong to TC-13.4 only; must not include
        BudgetResult, compute_budget, or ContextBudgetPolicy public API."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section_28 = _extract_markdown_section(adr_text, "### 2.8")
        self.assertIsNotNone(section_28,
                             "ADR must contain §2.8 for TC-13.4")
        # §2.8 must NOT contain BudgetResult (the NamedTuple).
        self.assertNotIn(
            "BudgetResult",
            section_28,
            "ADR §2.8 must NOT contain BudgetResult — that is §2.9",
        )
        # §2.8 must NOT contain compute_budget.
        self.assertNotIn(
            "compute_budget",
            section_28,
            "ADR §2.8 must NOT contain compute_budget — that is §2.9",
        )
        # §2.8 may mention ContextBudgetPolicy as a cross-reference
        # (e.g. in §2.8.3 WorkerKind non-goals), but must not describe
        # the public API in detail.

    def test_section_24_and_29_percentages_are_identical(self) -> None:
        """§2.4 Worker Tiers and §2.9 ContextBudgetPolicy must use the same
        percentages."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section_24 = _extract_markdown_section(adr_text, "### 2.4")
        section_29 = _extract_markdown_section(adr_text, "### 2.9")
        self.assertIsNotNone(section_24, "ADR must contain §2.4 Worker Tiers")
        self.assertIsNotNone(section_29, "ADR must contain §2.9 ContextBudgetPolicy")

        # §2.4 uses "20%" / "35%" etc in table cells.
        for pct in ("20%", "35%", "50%", "65%"):
            self.assertIn(pct, section_24,
                          f"ADR §2.4 must mention {pct}")
        # §2.9 uses bare numbers in table, but the numbers must match.
        for pct in ("20", "35", "50", "65"):
            self.assertIn(pct, section_29,
                          f"ADR §2.9 must mention {pct}")

    # -- 8f: Interface Status rows for the new cards -----------------------

    def test_interface_status_contains_new_card_rows(self) -> None:
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_interface_status_table(adr_text)

        impl_cells = [
            self._resolve_col(row, "Implemented by", "Impl", "Notes")
            for row in rows
        ]
        impl_text = " ".join(impl_cells)

        for tid in sorted(self.NEW_TC13_IDS):
            self.assertIn(
                tid,
                impl_text,
                f"ADR Interface Status must contain {tid}",
            )

    # -- 8g: WorkerKind is a logical label, not a runtime binding ----------

    def _extract_table_cells(self, section: str, col_idx: int = 0) -> set[str]:
        """Extract stripped cell values from a given column of the first
        markdown table in *section*.

        Returns the set of unique, stripped cell values (backtick-free
        but preserving underscores).
        """
        values: set[str] = set()
        in_table = False
        for raw in section.splitlines():
            stripped = raw.strip()
            if not in_table:
                if stripped.startswith("|") and not re.match(
                    r"^\|[\s\-:|]+\|", stripped
                ):
                    in_table = True
            if not in_table:
                continue
            if re.match(r"^\|[\s\-:|]+\|", stripped):
                continue
            if stripped == "" or not stripped.startswith("|"):
                break
            parts = [c.strip() for c in stripped.strip("|").split("|")]
            if col_idx < len(parts):
                # Only strip backticks and whitespace; keep underscores.
                cell = parts[col_idx].strip().replace("`", "")
                if cell and cell != "—" and cell.lower() not in ("value", "semantic"):
                    values.add(cell)
        return values

    def test_worker_kind_does_not_contain_runtime_fields(self) -> None:
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.8.3")
        self.assertIsNotNone(section)

        # Only inspect the *value* column of the WorkerKind table,
        # not the prose that explains what WorkerKind excludes.
        value_cells = self._extract_table_cells(section, col_idx=0)
        self.assertSetEqual(
            value_cells,
            self.WORKER_KIND_VALUES,
            f"ADR §2.8.3 WorkerKind table must contain exactly "
            f"{sorted(self.WORKER_KIND_VALUES)}",
        )

        # Verify that the prose explicitly states WorkerKind is a
        # logical label, not a binding.
        self.assertIn(
            "logical",
            section.lower(),
            "ADR §2.8.3 must state WorkerKind is a logical label",
        )

    # -- 8h: ContextBudgetPolicy percentages and reserved rule --------------

    def test_context_budget_policy_percentages_in_tc1351_description(self) -> None:
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_future_task_cards_table(adr_text)
        by_id = {
            self._resolve_col(row, "Task Card", "Task", "#"): row
            for row in rows
        }
        tc1351 = by_id.get("TC-13.5.1")
        self.assertIsNotNone(tc1351)

        desc = self._resolve_col(tc1351, "Description", "Desc")
        for pct in ("20%", "35%", "50%", "65%"):
            self.assertIn(
                pct,
                desc,
                f"TC-13.5.1 description must mention {pct}",
            )
        self.assertIn(
            "35%",
            desc,
            "TC-13.5.1 description must mention ≥35% reserved",
        )

    # -- Item 9: §2.7 Skill and Dashboard Data Sharing restored -----------

    def test_adr_section_27_data_sharing_contract(self) -> None:
        """ADR §2.7 must define the read-only data-sharing contract.

        Extracts only the §2.7 subsection and verifies:
        - Skill and HTML Dashboard consume data through StateProvider.
        - Neither writes to canonical state directly.
        - All writes go through ControlPlaneTransitionService.

        Deliberately scoped to §2.7; does not search the whole ADR.
        """
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "### 2.7")
        self.assertIsNotNone(section,
                             "ADR must contain §2.7 Skill and Dashboard Data Sharing")
        self.assertIn(
            "StateProvider",
            section,
            "ADR §2.7 must reference StateProvider",
        )
        self.assertIn(
            "TC-13.17",
            section,
            "ADR §2.7 must reference TC-13.17 (StateProvider)",
        )
        self.assertIn(
            "ControlPlaneTransitionService",
            section,
            "ADR §2.7 must reference ControlPlaneTransitionService",
        )
        self.assertIn(
            "TC-13.11",
            section,
            "ADR §2.7 must reference TC-13.11 (ControlPlaneTransitionService)",
        )
        # Must state that neither writes to canonical state directly.
        target = section.lower()
        self.assertTrue(
            "neither writes" in target
            or "do not write" in target
            or "does not write" in target,
            "ADR §2.7 must state neither writes to canonical state directly",
        )
        # All writes go through ControlPlaneTransitionService.
        self.assertIn(
            "all writes",
            target,
            "ADR §2.7 must route all writes through ControlPlaneTransitionService",
        )
        # Must mention both Skill and HTML Dashboard.
        self.assertIn("Skill", section)
        self.assertIn("HTML Dashboard", section)

    # -- Item 7 (restored): non-existent TC-12.3.1 guard -------------------

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

    # ── Item 10: TC-13.7 DispatcherAgentGateway ────────────────────────────

    # -- 10a: §2.10 heading must exist and now be Current ----------------

    def test_tc137_section_210_heading_exists_and_is_current(self) -> None:
        """ADR §2.10 must exist as an independent heading, now marked Current."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "### 2.10")
        self.assertIsNotNone(
            section,
            "ADR must contain ### 2.10 DispatcherAgentGateway frozen contract",
        )
        heading_m = re.search(
            r"^### 2\.10\s.*$", adr_text, re.MULTILINE,
        )
        self.assertIsNotNone(heading_m, "ADR must have a §2.10 heading")
        heading = heading_m.group(0)
        self.assertIn(
            "Current",
            heading,
            f"§2.10 heading must say Current, got: {heading.strip()!r}",
        )
        self.assertNotIn(
            "Target",
            heading,
            f"§2.10 heading must NOT say Target, got: {heading.strip()!r}",
        )
        self.assertIn(
            "TC-13.7",
            heading,
            f"§2.10 heading must reference TC-13.7, got: {heading.strip()!r}",
        )

    def test_tc137_section_210_contains_frozen_contract_markers(self) -> None:
        """§2.10 must contain Frozen Contract in the heading or nearby prose."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "### 2.10")
        self.assertIsNotNone(section)
        # The heading or opening paragraph must use "Frozen Contract" language.
        self.assertIn(
            "Frozen Contract",
            section,
            "ADR §2.10 must mention 'Frozen Contract'",
        )

    # -- 10b: Interface Status #29 now Current ------------------------------

    def test_tc137_interface_status_row_29_is_current(self) -> None:
        """ADR Interface Status row #29 (TC-13.7) must now be Current."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_interface_status_table(adr_text)

        tc137_row = None
        for row in rows:
            impl_cell = self._resolve_col(row, "Implemented by", "Impl", "Notes")
            task_ids = re.findall(r"\b(TC-\d+(?:\.\d+)*)\b", impl_cell)
            if "TC-13.7" in task_ids:
                tc137_row = row
                break

        self.assertIsNotNone(
            tc137_row,
            "ADR Interface Status must contain row #29 Implemented-by TC-13.7",
        )
        status_cell = self._resolve_col(tc137_row, "Status")
        self.assertIn(
            "Current",
            status_cell,
            f"ADR Interface Status #29 (TC-13.7) must be Current, "
            f"got status={status_cell!r}",
        )
        self.assertNotIn(
            "Target",
            status_cell,
            "ADR Interface Status #29 (TC-13.7) must NOT be marked Target",
        )

    # -- 10c: DispatchIdentity — exactly four fields, includes revision -

    _DISPATCH_IDENTITY_FIELDS = frozenset(
        {"task_id", "revision", "attempt", "dispatch_id"}
    )

    def test_tc137_dispatch_identity_exact_four_fields(self) -> None:
        """§2.10.2 must define DispatchIdentity with exactly task_id,
        revision, attempt, dispatch_id."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(
            adr_text, "#### 2.10.2"
        ) or _extract_markdown_section(adr_text, "### 2.10")
        self.assertIsNotNone(section, "ADR §2.10 must contain a DispatchIdentity section")

        for field in sorted(self._DISPATCH_IDENTITY_FIELDS):
            self.assertIn(
                field,
                section,
                f"ADR §2.10.2 must reference DispatchIdentity field '{field}'",
            )

    def test_tc137_dispatch_identity_includes_revision(self) -> None:
        """DispatchIdentity must include revision — this was missing in
        the prior planning draft."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(
            adr_text, "#### 2.10.2"
        ) or _extract_markdown_section(adr_text, "### 2.10")
        self.assertIsNotNone(section)
        self.assertIn(
            "revision",
            section,
            "ADR §2.10.2 must include 'revision' in DispatchIdentity",
        )

    # -- 10d: ModelSelectionSnapshot — exact ten fields ------------------

    _SNAPSHOT_KEYS = frozenset({
        "required_model_tier", "required_model_capabilities",
        "model_binding_id", "selected_model_provider",
        "selected_model_id", "selected_model_tier",
        "selected_deliberation_tier", "selected_context_window_tokens",
        "selected_model_capabilities", "model_degradation_approval_id",
    })

    def test_tc137_snapshot_exact_ten_fields(self) -> None:
        """§2.10.3 must define ModelSelectionSnapshot with exactly ten fields."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(
            adr_text, "#### 2.10.3"
        ) or _extract_markdown_section(adr_text, "### 2.10")
        self.assertIsNotNone(
            section, "ADR §2.10 must contain a ModelSelectionSnapshot section",
        )
        for key in sorted(self._SNAPSHOT_KEYS):
            self.assertIn(
                key,
                section,
                f"ADR §2.10.3 must reference snapshot field '{key}'",
            )

    def test_tc137_snapshot_no_extra_keys(self) -> None:
        """The snapshot field table in §2.10.3 must list exactly the ten
        frozen fields — no 11th field row."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(
            adr_text, "#### 2.10.3"
        ) or _extract_markdown_section(adr_text, "### 2.10")
        self.assertIsNotNone(section)

        # Parse the table rows for field names — each table row
        # must be one of the known 10 fields.  No 11th row.
        import re as _re
        field_names_in_table: set[str] = set()
        for raw in section.splitlines():
            stripped = raw.strip()
            # Match a table row whose second cell is a ``...`` field name.
            m = _re.match(
                r"^\|\s*\d+\s*\|\s*``([a-z_]+)``\s*\|",
                stripped,
            )
            if m:
                field_names_in_table.add(m.group(1))
        if field_names_in_table:
            extra = field_names_in_table - self._SNAPSHOT_KEYS
            self.assertSetEqual(
                extra,
                set(),
                f"§2.10.3 table contains field(s) outside frozen ten: "
                f"{', '.join(sorted(extra))}",
            )

    # -- 10d‑bis: Tuple deep-immutability for capability fields ------------

    def test_tc137_capability_fields_are_tuple_not_list(self) -> None:
        """§2.10.3 must declare required_model_capabilities and
        selected_model_capabilities as tuple[str, ...], not list[str]."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(
            adr_text, "#### 2.10.3"
        ) or _extract_markdown_section(adr_text, "### 2.10")
        self.assertIsNotNone(section)
        self.assertIn(
            "tuple[str, ...]",
            section,
            "§2.10.3 must declare capability fields as tuple[str, ...]",
        )
        # Explicitly forbid list[str] for the capability columns.
        import re as _re
        cap_rows = _re.findall(
            r"\|\s*\d+\s*\|\s*``(?:required_model_capabilities|selected_model_capabilities)``\s*\|\s*``(.*?)``",
            section,
        )
        for row_type in cap_rows:
            self.assertNotIn(
                "list",
                row_type,
                f"§2.10.3 capability field type must be tuple, got: {row_type}",
            )
            self.assertIn(
                "tuple",
                row_type,
                f"§2.10.3 capability field type must be tuple, got: {row_type}",
            )

    def test_tc137_snapshot_forbids_mutable_collections(self) -> None:
        """§2.10.3 must state the snapshot contains no mutable list, dict,
        or set — all collection fields are immutable."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(
            adr_text, "#### 2.10.3"
        ) or _extract_markdown_section(adr_text, "### 2.10")
        self.assertIsNotNone(section)
        target = section.lower()
        self.assertTrue(
            "mutable" in target or "immutable" in target,
            "§2.10.3 must state snapshot is deeply immutable (no mutable collections)",
        )

    def test_tc137_snapshot_constructor_copies_arrays_to_tuple(self) -> None:
        """§2.10.3 must state that the snapshot constructor copies external
        JSON arrays into tuple, preserving order."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(
            adr_text, "#### 2.10.3"
        ) or _extract_markdown_section(adr_text, "### 2.10")
        self.assertIsNotNone(section)
        self.assertIn(
            "copy",
            section.lower(),
            "§2.10.3 must state that arrays are copied into tuples at construction time",
        )
        self.assertIn(
            "order",
            section.lower(),
            "§2.10.3 must state that original order is preserved",
        )

    # -- 10e: DispatchRequest — exactly five fields, no deferred extras –

    _REQUEST_REQUIRED_FIELDS = frozenset(
        {"identity", "workspace", "prompt", "model_selection", "timeout_seconds"}
    )
    _REQUEST_FORBIDDEN_FIELDS = frozenset({
        "provider_config_id", "environment_allowlist", "stdin_bytes",
        "project_root", "task_difficulty", "worker_kind",
        "budget_tokens", "lease_id", "slot_id", "retry_count",
        "escalation_level", "approval_id", "rate_limit_token",
    })

    def test_tc137_dispatch_request_exact_five_fields(self) -> None:
        """§2.10.4 must define DispatchRequest with exactly five fields."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(
            adr_text, "#### 2.10.4"
        ) or _extract_markdown_section(adr_text, "### 2.10")
        self.assertIsNotNone(
            section, "ADR §2.10 must contain a DispatchRequest section",
        )
        for field in sorted(self._REQUEST_REQUIRED_FIELDS):
            self.assertIn(
                field,
                section,
                f"ADR §2.10.4 must reference DispatchRequest field '{field}'",
            )

    def test_tc137_dispatch_request_forbids_deferred_fields(self) -> None:
        """§2.10.4 request field table must contain exactly the five
        frozen fields — no provider_config_id, environment_allowlist,
        stdin_bytes, or other deferred fields."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(
            adr_text, "#### 2.10.4"
        ) or _extract_markdown_section(adr_text, "### 2.10")
        self.assertIsNotNone(section)

        # Parse the request field table — each numbered row must be one
        # of the 5 frozen fields.  provider_config_id must not be a row.
        import re as _re
        field_names: set[str] = set()
        for raw in section.splitlines():
            stripped = raw.strip()
            m = _re.match(
                r"^\|\s*\d+\s*\|\s*``([a-z_]+)``\s*\|",
                stripped,
            )
            if m:
                field_names.add(m.group(1))
        if field_names:
            extra = field_names - self._REQUEST_REQUIRED_FIELDS
            self.assertSetEqual(
                extra,
                set(),
                f"§2.10.4 request table has extra field(s): "
                f"{', '.join(sorted(extra))}",
            )
            missing = self._REQUEST_REQUIRED_FIELDS - field_names
            self.assertSetEqual(
                missing,
                set(),
                f"§2.10.4 request table is missing field(s): "
                f"{', '.join(sorted(missing))}",
            )

    # -- 10f: DispatchResult — exactly eight fields, no timed_out etc. ---

    _RESULT_REQUIRED = frozenset({
        "identity", "provider", "model_id", "duration_seconds",
        "stdout", "stderr", "stdout_sha256", "stderr_sha256",
    })
    _RESULT_FORBIDDEN = frozenset({
        "timed_out", "cancelled", "process_id", "command_receipt",
        "archive_path", "report_sha256", "deliberation_id",
        "executor_model",
    })

    def test_tc137_dispatch_result_exact_eight_fields(self) -> None:
        """§2.10.7 must define DispatchResult with exactly eight fields."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(
            adr_text, "#### 2.10.7"
        ) or _extract_markdown_section(adr_text, "### 2.10")
        self.assertIsNotNone(
            section, "ADR §2.10 must contain a DispatchResult section",
        )
        for field in sorted(self._RESULT_REQUIRED):
            self.assertIn(
                field,
                section,
                f"ADR §2.10.7 must reference DispatchResult field '{field}'",
            )

    def test_tc137_dispatch_result_forbids_timed_out_and_cancelled(self) -> None:
        """§2.10.7 result field table must NOT contain timed_out or
        cancelled — those outcomes use exceptions."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(
            adr_text, "#### 2.10.7"
        ) or _extract_markdown_section(adr_text, "### 2.10")
        self.assertIsNotNone(section)

        # Parse the result field table rows.
        import re as _re
        field_names: set[str] = set()
        for raw in section.splitlines():
            stripped = raw.strip()
            m = _re.match(
                r"^\|\s*\d+\s*\|\s*``([a-z_]+)``\s*\|",
                stripped,
            )
            if m:
                field_names.add(m.group(1))
        self.assertNotIn(
            "timed_out",
            field_names,
            "§2.10.7 result table must NOT include 'timed_out' — timeout uses exceptions",
        )
        self.assertNotIn(
            "cancelled",
            field_names,
            "§2.10.7 result table must NOT include 'cancelled' — cancellation uses exceptions",
        )

    def test_tc137_dispatch_result_forbids_process_id_and_command_receipt(self) -> None:
        """§2.10.7 result field table must NOT contain process_id or
        command_receipt — runtime-only identifiers not exposed."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(
            adr_text, "#### 2.10.7"
        ) or _extract_markdown_section(adr_text, "### 2.10")
        self.assertIsNotNone(section)

        import re as _re
        field_names: set[str] = set()
        for raw in section.splitlines():
            stripped = raw.strip()
            m = _re.match(
                r"^\|\s*\d+\s*\|\s*``([a-z_]+)``\s*\|",
                stripped,
            )
            if m:
                field_names.add(m.group(1))
        self.assertNotIn(
            "process_id",
            field_names,
            "§2.10.7 result table must NOT include 'process_id' — runtime-only identifier",
        )
        self.assertNotIn(
            "command_receipt",
            field_names,
            "§2.10.7 result table must NOT include 'command_receipt' — runtime-only identifier",
        )

    # -- 10g: Exception-only failure semantics ---------------------------

    def test_tc137_failure_uses_exceptions_not_flags(self) -> None:
        """§2.10.8 must state that non‑zero exit, timeout, and cancellation
        all use exceptions, not success‑result flags."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(
            adr_text, "#### 2.10.8"
        ) or _extract_markdown_section(adr_text, "### 2.10")
        self.assertIsNotNone(
            section, "ADR §2.10 must contain a Failure Semantics section",
        )
        self.assertIn(
            "DispatchTimeoutError",
            section,
            "ADR §2.10.8 must reference DispatchTimeoutError",
        )
        self.assertIn(
            "DispatchCancelledError",
            section,
            "ADR §2.10.8 must reference DispatchCancelledError",
        )
        self.assertIn(
            "DispatchNonZeroExitError",
            section,
            "ADR §2.10.8 must reference DispatchNonZeroExitError",
        )

    # -- 10h: Stdout/stderr are opaque bytes -----------------------------

    def test_tc137_stdout_stderr_are_opaque_bytes(self) -> None:
        """§2.10.7 and §2.10.8 together must state stdout/stderr are
        opaque bytes — no decoding is attempted by the Gateway."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        # Only check the output/failure subsections — the intro list
        # mentions "opaque bytes" but §2.10.8 explicitly says
        # "There is no DispatchOutputDecodeError".  The contract is
        # that DispatchResult.stdout/stderr are ``bytes`` and the
        # Gateway never decodes them.
        sec_result = _extract_markdown_section(adr_text, "#### 2.10.7")
        self.assertIsNotNone(sec_result)
        self.assertIn(
            "bytes",
            sec_result,
            "§2.10.7 stdout/stderr fields must be of type bytes",
        )
        # §2.10.8 must state there is no OutputDecodeError (i.e. it
        # is explicitly excluded — the word appears in the negation).
        sec_failure = _extract_markdown_section(adr_text, "#### 2.10.8")
        self.assertIsNotNone(sec_failure)
        self.assertIn(
            "DispatchOutputDecodeError",
            sec_failure,
            "§2.10.8 must explicitly note that DispatchOutputDecodeError does not exist",
        )

    # -- 10i: Executable path allows spaces but forbids shell command ----

    def test_tc137_executable_path_allows_spaces_no_shell_command(self) -> None:
        """§2.10.9 must allow spaces in executable path but forbid
        embedding arguments or using shell=True."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(
            adr_text, "#### 2.10.9"
        ) or _extract_markdown_section(adr_text, "### 2.10")
        self.assertIsNotNone(
            section, "ADR §2.10 must contain an Executable Security section",
        )
        self.assertIn(
            "shell=",
            section.lower(),
            "ADR §2.10.9 must reference shell=True/shell=False",
        )
        self.assertIn(
            "which(",
            section.replace("shutil.which()", "which("),
            "ADR §2.10.9 must reference shutil.which for executable resolution",
        )

    # -- 10j: Fake adapter not in production planning --------------------

    def test_tc137_fake_adapter_not_in_production(self) -> None:
        """§2.10.5 must state that FakeAgentCliProvider may exist only in
        tests/, never in a production module."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(
            adr_text, "#### 2.10.5"
        ) or _extract_markdown_section(adr_text, "### 2.10")
        self.assertIsNotNone(
            section, "ADR §2.10 must contain a Provider Adapter Protocol section",
        )
        self.assertIn(
            "Fake",
            section,
            "ADR §2.10.5 must mention FakeAgentCliProvider restriction",
        )

    # -- 10k: TC-13.8 / TC-13.9 responsibilities not pre-empted ----------

    def test_tc137_does_not_preempt_tc138_tc139(self) -> None:
        """§2.10 must not define Claude‑specific parameters or WorkerKind
        mapping or budget logic — those belong to TC-13.8/13.9.

        Uses structural parsing: the responsibility boundary list and
        exclusion list in §2.10.1 provide the authoritative scope."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.10.1")
        self.assertIsNotNone(section)
        # §2.10.1 exclusion list must explicitly state TC-13.7 does NOT
        # consume WorkerKind, TaskDifficulty, ContextBudgetPolicy, or
        # BudgetResult.
        self.assertIn(
            "WorkerKind",
            section,
            "§2.10.1 exclusion list must mention WorkerKind",
        )
        self.assertIn(
            "ContextBudgetPolicy",
            section,
            "§2.10.1 exclusion list must mention ContextBudgetPolicy",
        )
        # §2.10.1 must state TC-13.7 does NOT know Claude specifics.
        prohibited_claude = (
            "--permission-mode" in section
            or "permission-mode" in section
        )
        self.assertTrue(
            prohibited_claude,
            "§2.10.1 must mention that Claude CLI specifics are excluded "
            "(e.g. '--permission-mode')",
        )

    # -- 10ℓ: Protocol definition checks ---------------------------------

    def test_tc137_agent_cli_provider_protocol_defined(self) -> None:
        """§2.10.5 must define AgentCliProvider Protocol with provider_id
        and build_invocation.  parse_result must not be a listed method."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(
            adr_text, "#### 2.10.5"
        ) or _extract_markdown_section(adr_text, "### 2.10")
        self.assertIsNotNone(section)
        self.assertIn(
            "AgentCliProvider",
            section,
            "ADR §2.10.5 must define AgentCliProvider",
        )
        self.assertIn(
            "build_invocation",
            section,
            "ADR §2.10.5 must reference build_invocation method",
        )
        self.assertIn(
            "provider_id",
            section,
            "ADR §2.10.5 must reference provider_id attribute",
        )
        # parse_result may be mentioned in the exclusion note, but must
        # NOT appear as a required method in the Protocol *table*.
        # Parse the method table rows.
        import re as _re
        method_names: set[str] = set()
        for raw in section.splitlines():
            stripped = raw.strip()
            m = _re.match(
                r"^\|\s*``([a-z_]+)\([^)]*\)``\s*\|",
                stripped,
            )
            if m:
                method_names.add(m.group(1))
        self.assertNotIn(
            "parse_result",
            method_names,
            "§2.10.5 Protocol method table must NOT list parse_result",
        )

    def test_tc137_agent_cli_invocation_frozen_fields(self) -> None:
        """§2.10.6 must define AgentCliInvocation with exact fields."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(
            adr_text, "#### 2.10.6"
        ) or _extract_markdown_section(adr_text, "### 2.10")
        self.assertIsNotNone(
            section, "ADR §2.10 must contain an AgentCliInvocation section",
        )
        for field in ("executable", "argv", "stdin", "env_overrides"):
            self.assertIn(
                field,
                section,
                f"ADR §2.10.6 must reference AgentCliInvocation field '{field}'",
            )

    # -- 10ℓ‑bis: Provider mapping — explicit run_dispatch signature --------

    def test_tc137_run_dispatch_signature_frozen(self) -> None:
        """§2.10.5 must freeze run_dispatch(request, providers) with
        Mapping[str, AgentCliProvider]."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.10.5")
        self.assertIsNotNone(section)
        self.assertIn(
            "run_dispatch",
            section,
            "§2.10.5 must freeze run_dispatch public entry point",
        )
        self.assertIn(
            "Mapping",
            section,
            "§2.10.5 must reference Mapping[str, AgentCliProvider]",
        )
        self.assertIn(
            "providers",
            section,
            "§2.10.5 must reference 'providers' parameter",
        )

    def test_tc137_no_module_level_mutable_registry(self) -> None:
        """§2.10.5 must state there is NO register_provider() or
        unregister_provider() — the ADR says 'There is **no**
        ``register_provider()``, ``unregister_provider()``'."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.10.5")
        self.assertIsNotNone(section)
        # The ADR says "There is **no** ``register_provider()``,
        # ``unregister_provider()``" — this correctly asserts they don't
        # exist.  The test checks that the section uses that language.
        target = section.lower()
        self.assertTrue(
            "no module" in target
            or "does not provide" in target
            or "no global" in target,
            "§2.10.5 must state there is no module-level mutable registry",
        )

    def test_tc137_provider_lookup_key_is_selected_model_provider(self) -> None:
        """§2.10.5 must state lookup key is selected_model_provider."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.10.5")
        self.assertIsNotNone(section)
        self.assertIn(
            "selected_model_provider",
            section,
            "§2.10.5 must use selected_model_provider as provider lookup key",
        )

    def test_tc137_adapter_provider_id_must_match_key(self) -> None:
        """§2.10.5 must state adapter provider_id equals its mapping key."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.10.5")
        self.assertIsNotNone(section)
        target = section.lower()
        self.assertTrue(
            ("provider_id" in target and "equal" in target)
            or ("provider_id" in target and "match" in target),
            "§2.10.5 must state adapter provider_id must equal key",
        )

    # -- 10ℓ‑ter: argv excludes executable --------------------------------

    def test_tc137_argv_excludes_executable(self) -> None:
        """§2.10.6 must state argv does NOT contain executable — the
        'argv does **not** include executable' language in the table."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.10.6")
        self.assertIsNotNone(section)
        # The table says "Arguments only — does **not** include executable".
        # Check for the bold negation.
        target = section.lower()
        self.assertTrue(
            "does **not** include executable" in section
            or "must **not** contain the executable" in section
            or ("argv" in target and "not" in target and "executable" in target),
            "§2.10.6 must state argv does not include executable",
        )
        # Must show create_subprocess_exec with *invocation.argv.
        self.assertIn(
            "create_subprocess_exec",
            section,
            "§2.10.6 must reference asyncio.create_subprocess_exec",
        )

    def test_tc137_subprocess_exec_model_is_exe_star_argv(self) -> None:
        """§2.10.6 must show the invocation model: executable, *argv."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.10.6")
        self.assertIsNotNone(section)
        self.assertIn(
            "*",
            section,
            "§2.10.6 must show *invocation.argv unpacking in subprocess call",
        )
        self.assertIn(
            "executable",
            section.lower(),
            "§2.10.6 must show executable as separate first argument",
        )

    def test_tc137_empty_argv_is_legal(self) -> None:
        """§2.10.6 must state empty argv tuple is legal."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.10.6")
        self.assertIsNotNone(section)
        target = section.lower()
        self.assertTrue(
            "empty" in target or "zero arguments" in target,
            "§2.10.6 must state empty argv is legal",
        )

    # -- 10m: Dependency boundary table exists ---------------------------

    def test_tc137_dependency_boundary_table_exists(self) -> None:
        """§2.10.12 must contain a dependency boundary table listing
        TC-13.4, TC-13.6–TC-13.11, TC-13.18."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(
            adr_text, "#### 2.10.12"
        ) or _extract_markdown_section(adr_text, "### 2.10")
        self.assertIsNotNone(
            section, "ADR §2.10 must contain a Dependency Boundary section",
        )
        for tc in ("TC-13.4", "TC-13.6", "TC-13.8", "TC-13.9",
                   "TC-13.10", "TC-13.11", "TC-13.18"):
            self.assertIn(
                tc,
                section,
                f"ADR §2.10.12 must reference {tc} in dependency boundary",
            )

    # -- Item 11: TC-13.7 Implementation Status (Current) ------------------

    @staticmethod
    def _dispatcher_gateway_path() -> Path:
        return SKILL_ROOT / "scripts" / "dispatcher_gateway.py"

    def test_tc137_dispatcher_gateway_file_exists(self) -> None:
        """dispatcher_gateway.py must exist as a regular file."""
        dg_path = self._dispatcher_gateway_path()
        self.assertTrue(dg_path.is_file(),
                        f"dispatcher_gateway.py must exist at {dg_path}")

    def test_tc137_public_api_exists(self) -> None:
        """All frozen public API symbols must be importable."""
        import sys
        sys.path.insert(0, str(SKILL_ROOT / "scripts"))
        try:
            import dispatcher_gateway as dg  # type: ignore[import-untyped]
            symbols = (
                "DispatchIdentity",
                "ModelSelectionSnapshot",
                "DispatchRequest",
                "AgentCliInvocation",
                "DispatchResult",
                "AgentCliProvider",
                "run_dispatch",
                "DispatchGatewayError",
                "DispatchInputError",
                "DispatchSnapshotError",
                "ProviderNotSupportedError",
                "ExecutableNotFoundError",
                "DispatchInvocationError",
                "DispatchLaunchError",
                "DispatchTimeoutError",
                "DispatchCancelledError",
                "DispatchNonZeroExitError",
            )
            for sym in symbols:
                self.assertTrue(hasattr(dg, sym), f"Missing public API: {sym}")
        finally:
            sys.path.pop(0)
            for m in list(sys.modules):
                if m.startswith("dispatcher_gateway"):
                    del sys.modules[m]

    def test_tc137_fake_provider_not_in_production(self) -> None:
        """FakeAgentCliProvider must NOT exist in production module."""
        src = self._dispatcher_gateway_path().read_text(encoding="utf-8")
        self.assertNotIn("FakeAgentCliProvider", src,
                         "FakeAgentCliProvider must not be in dispatcher_gateway.py")

    def test_tc137_adr_29_is_current_tc137(self) -> None:
        """ADR Interface Status row #29 must be Current with TC-13.7."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_interface_status_table(adr_text)
        tc137_row = None
        for row in rows:
            impl_cell = self._resolve_col(row, "Implemented by", "Impl", "Notes")
            task_ids = re.findall(r"\b(TC-\d+(?:\.\d+)*)\b", impl_cell)
            if "TC-13.7" in task_ids:
                tc137_row = row
                break
        self.assertIsNotNone(tc137_row)
        status = self._resolve_col(tc137_row, "Status")
        self.assertIn("Current", status,
                      f"#29 must be Current, got: {status}")

    def test_tc137_tc138_139_still_target(self) -> None:
        """TC-13.8 and TC-13.9 must still be Target."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_interface_status_table(adr_text)

        still_target = {"TC-13.8", "TC-13.9"}
        for row in rows:
            impl_cell = self._resolve_col(row, "Implemented by", "Impl", "Notes")
            task_ids = set(re.findall(r"\b(TC-\d+(?:\.\d+)*)\b", impl_cell))
            matching = task_ids & still_target
            if not matching:
                continue
            status = self._resolve_col(row, "Status")
            self.assertIn("Target", status,
                          f"{', '.join(sorted(matching))} must be Target, got: {status}")
            self.assertNotIn("Current", status,
                             f"{', '.join(sorted(matching))} must NOT be Current")

    def test_tc137_no_claude_specific_params_in_production(self) -> None:
        """Production module must not reference Claude-specific parameters."""
        src = self._dispatcher_gateway_path().read_text(encoding="utf-8")
        # Remove docstring/comment lines
        in_docstring = False
        lines: list[str] = []
        for line in src.splitlines():
            stripped = line.strip()
            if '"""' in stripped:
                in_docstring = not in_docstring
                continue
            if in_docstring:
                continue
            if stripped.startswith("#"):
                continue
            lines.append(line)
        code = "\n".join(lines)
        self.assertNotIn("MAD_HOME", code)
        self.assertNotIn("MAD_PARTICIPANT", code)
        self.assertNotIn("--permission-mode", code)

    def test_tc137_no_mad_gateway_or_mad_refs_import(self) -> None:
        """Production module must not import mad_gateway or mad_refs."""
        src = self._dispatcher_gateway_path().read_text(encoding="utf-8")
        self.assertNotIn("mad_gateway", src)
        self.assertNotIn("mad_refs", src)


if __name__ == "__main__":
    unittest.main()
