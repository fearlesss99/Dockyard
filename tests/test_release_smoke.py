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

        # TC-13.9a depends on TC-13.5.1, TC-13.7, TC-13.8, TC-13.8.4
        tc139a_dep = self._resolve_col(by_id.get("TC-13.9a", {}), "Depends on", "Dep")
        self.assertIn("TC-13.5.1", tc139a_dep,
                      "TC-13.9a must depend on TC-13.5.1")
        self.assertIn("TC-13.7", tc139a_dep,
                      "TC-13.9a must depend on TC-13.7")
        self.assertIn("TC-13.8", tc139a_dep,
                      "TC-13.9a must depend on TC-13.8")
        self.assertIn("TC-13.8.4", tc139a_dep,
                      "TC-13.9a must depend on TC-13.8.4")

        # TC-13.10 depends on TC-13.9b, not TC-13.9c
        tc1310_dep = self._resolve_col(by_id.get("TC-13.10", {}), "Depends on", "Dep")
        self.assertIn("TC-13.9b", tc1310_dep,
                      "TC-13.10 must depend on TC-13.9b (production, not decoder)")

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
            "TC-13.9a",
            section,
            "ADR §2.8.4 must reference TC-13.9a as out-of-scope",
        )

    # -- 8e: New interfaces (TC-13.7/8) remain Target; TC-13.4 and TC-13.5 are now Current

    STILL_TARGET_TC13_IDS = frozenset()

    def test_new_non_tc134_interfaces_remain_target(self) -> None:
        """TC-13.7 must be Current; TC-13.9a must still be Target.

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
        # Must reference TC-13.9a WorkerAdapter consumption.
        self.assertIn("TC-13.9a", section_29,
                       "ADR §2.9 must reference TC-13.9a consumption")

    def test_section_28_does_not_contain_tc135_public_api(self) -> None:
        """§2.8 continues to belong to TC-13.4 only; must not include
        BudgetResult as its own type or compute_budget as a function
        definition — only as a cross-reference to §2.9."""
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
        # §2.8 may mention compute_budget as a cross-reference (§2.8.1
        # explains budget relationship), but must not define the API in
        # detail.  We only check that BudgetResult is absent — the
        # compute_budget mention is a legitimate contract cross-reference.

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
        # The word "cwd" may appear in the prose rule explaining there is
        # no cwd field — but a numbered table row ``| # | ``cwd`` |``
        # must NOT exist.  Check for the table-row pattern.
        self.assertNotRegex(
            section,
            r"\|\s*\d+\s*\|\s*``cwd``\s*\|",
            "ADR §2.10.6 must NOT list cwd as a numbered AgentCliInvocation field",
        )

    # -- 10f‑bis: Workspace is the authoritative cwd -------------------------

    def test_tc137_workspace_is_authoritative_cwd(self) -> None:
        """§2.10.4 must state workspace is the authoritative cwd; §2.10.6
        must state AgentCliInvocation carries no cwd field."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        sec_request = _extract_markdown_section(
            adr_text, "#### 2.10.4"
        ) or _extract_markdown_section(adr_text, "### 2.10")
        self.assertIsNotNone(sec_request)
        self.assertIn(
            "authoritative",
            sec_request,
            "§2.10.4 must state workspace is the authoritative subprocess "
            "working directory",
        )
        self.assertIn(
            "cwd",
            sec_request,
            "§2.10.4 must reference cwd for create_subprocess_exec",
        )

    def test_tc137_agent_cli_invocation_no_cwd(self) -> None:
        """§2.10.6 must forbid a cwd field on AgentCliInvocation."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(
            adr_text, "#### 2.10.6"
        ) or _extract_markdown_section(adr_text, "### 2.10")
        self.assertIsNotNone(section)
        target = section.lower()
        self.assertTrue(
            "no ``cwd`` field" in target
            or "no cwd field" in target
            or "four fields" in target,
            "§2.10.6 must state there is no cwd field on AgentCliInvocation",
        )

    def test_tc137_dispatcher_module_has_no_adapter_cwd(self) -> None:
        """Production dispatcher_gateway.py must not expose a cwd override
        to providers."""
        dg_path = SKILL_ROOT / "scripts" / "dispatcher_gateway.py"
        src = dg_path.read_text(encoding="utf-8")
        # Strip docstrings / comments
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
        # The only ".cwd" in the production code should be Path.cwd() in
        # test assertions; the workspace=str(request.workspace) is fine.
        # invocation.cwd must never appear.
        self.assertNotIn("invocation.cwd", code,
                         "Production code must not reference invocation.cwd")
        self.assertNotIn("adapter.cwd", code,
                         "Production code must not reference adapter cwd")

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
        for tc in ("TC-13.4", "TC-13.6", "TC-13.8", "TC-13.9a",
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

    def test_tc137_tc138_139a_still_target(self) -> None:
        """TC-13.8 is now Current and TC-13.9a must still be Target."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_interface_status_table(adr_text)

        still_target = {"TC-13.9a"}
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

    # -- Item 13: TC-13.7 Current status consistency regression ----------

    def test_tc137_section_210_body_does_not_contain_stale_freezing_language(self) -> None:
        """§2.10 body must NOT contain 'does not mark TC-13.7 as Current'
        or equivalent stale wording — scoped strictly to the §2.10
        subsection body."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "### 2.10")
        self.assertIsNotNone(section, "ADR must contain §2.10")
        # Stale frozen-contract language that was removed.
        self.assertNotIn(
            "does not mark TC-13.7 as Current",
            section,
            "§2.10 must NOT contain 'does not mark TC-13.7 as Current'",
        )
        self.assertNotIn(
            "does **not** mark TC-13.7 as Current",
            section,
            "§2.10 must NOT contain 'does **not** mark TC-13.7 as Current'",
        )
        # Equivalent patterns: "TC-13.7 has not yet been"
        self.assertNotIn(
            "has not yet been",
            section,
            "§2.10 must NOT contain 'has not yet been' stale wording",
        )
        self.assertNotIn(
            "is not yet Current",
            section,
            "§2.10 must NOT contain 'is not yet Current' stale wording",
        )
        self.assertNotIn(
            "not yet marked Current",
            section,
            "§2.10 must NOT contain 'not yet marked Current' stale wording",
        )

    def test_tc137_section_210_body_asserts_current_status(self) -> None:
        """§2.10 body must affirm TC-13.7 is Current with production
        module and test suite — scoped to the §2.10 subsection."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "### 2.10")
        self.assertIsNotNone(section, "ADR must contain §2.10")
        self.assertIn(
            "Current",
            section,
            "§2.10 body must contain 'Current'",
        )
        # Must reference the production module or test suite as evidence.
        self.assertIn(
            "dispatcher_gateway.py",
            section,
            "§2.10 must reference dispatcher_gateway.py as production evidence",
        )
        # The Frozen Contract language must still be present — we're
        # preserving the contract, just updating the status marker.
        self.assertIn(
            "Frozen Contract",
            section,
            "§2.10 must preserve 'Frozen Contract' language",
        )

    def test_interface_status_row_30_claude_cli_is_current(self) -> None:
        """ADR Interface Status row #30 (Claude Code CLI contract, TC-13.8)
        must now be Current."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_interface_status_table(adr_text)

        tc138_row = None
        for row in rows:
            impl_cell = self._resolve_col(row, "Implemented by", "Impl", "Notes")
            task_ids = re.findall(r"\b(TC-\d+(?:\.\d+)*)\b", impl_cell)
            if "TC-13.8" in task_ids:
                tc138_row = row
                break

        self.assertIsNotNone(
            tc138_row,
            "ADR Interface Status must contain row #30 Implemented-by TC-13.8",
        )
        status_cell = self._resolve_col(tc138_row, "Status")
        self.assertIn(
            "Current",
            status_cell,
            f"ADR Interface Status #30 (TC-13.8) must be Current, "
            f"got status={status_cell!r}",
        )
        self.assertNotIn(
            "Target",
            status_cell,
            "ADR Interface Status #30 (TC-13.8) must NOT be marked Target",
        )

    def test_interface_status_row_30_claude_cli_row_number(self) -> None:
        """Verify row #30 in the Interface Status table is Claude Code CLI
        contract with TC-13.8 — check the row number cell directly."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_interface_status_table(adr_text)

        # Find the row whose first column (the row number) is exactly "30".
        row_30 = None
        for row in rows:
            number_cell = self._resolve_col(row, "#")
            if number_cell == "30":
                row_30 = row
                break

        self.assertIsNotNone(
            row_30,
            "ADR Interface Status must contain a row numbered 30",
        )
        # Verify this is the Claude Code CLI contract row.
        desc_or_name = self._resolve_col(
            row_30, "Interface", "Description", "Desc"
        )
        self.assertIn(
            "Claude",
            desc_or_name,
            f"ADR Interface Status row #30 must be Claude Code CLI, "
            f"got: {desc_or_name!r}",
        )
        # Verify status is Current.
        status_cell = self._resolve_col(row_30, "Status")
        self.assertIn(
            "Current",
            status_cell,
            f"ADR Interface Status row #30 must be Current, "
            f"got status={status_cell!r}",
        )
        # Verify TC-13.8 is in the Implemented-by cell.
        impl_cell = self._resolve_col(row_30, "Implemented by", "Impl", "Notes")
        self.assertIn(
            "TC-13.8",
            impl_cell,
            f"ADR Interface Status row #30 must reference TC-13.8, "
            f"got: {impl_cell!r}",
        )

    def test_tc139c_not_prematurely_marked_current_in_table(self) -> None:
        """TC-13.9c must NOT have 'Current' status in any
        Interface Status row — TC-13.9b is now Current."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_interface_status_table(adr_text)

        protected_ids = {"TC-13.9c"}
        for row in rows:
            impl_cell = self._resolve_col(row, "Implemented by", "Impl", "Notes")
            task_ids = set(re.findall(r"\b(TC-\d+(?:\.\d+)*)\b", impl_cell))
            if not (task_ids & protected_ids):
                continue
            status_cell = self._resolve_col(row, "Status")
            self.assertNotIn(
                "Current",
                status_cell,
                f"TC-{', '.join(sorted(task_ids & protected_ids))} "
                f"must NOT be Current in row: {row}",
            )

    def test_target_sections_not_marked_current(self) -> None:
        """§2.1 and §2.4 are Target sections and must not say Current.
        §2.13 is now Current (TC-13.9b)."""
        adr_text = self._adr_path().read_text(encoding="utf-8")

        # §2.1 and §2.4 are Target sections.  They must not say Current.
        for section_num in ("2.1", "2.4"):
            heading_m = re.search(
                rf"^### {re.escape(section_num)}\s.*$",
                adr_text,
                re.MULTILINE,
            )
            self.assertIsNotNone(
                heading_m,
                f"ADR must have a §{section_num} heading",
            )
            heading = heading_m.group(0)
            self.assertNotIn(
                "Current",
                heading,
                f"§{section_num} heading must NOT say Current, "
                f"got: {heading.strip()!r}",
            )
            self.assertIn(
                "Target",
                heading,
                f"§{section_num} heading must say Target, "
                f"got: {heading.strip()!r}",
            )

        # §2.13 should now be Current.
        heading_213_m = re.search(
            r"^### 2\.13\s.*$", adr_text, re.MULTILINE,
        )
        self.assertIsNotNone(heading_213_m)
        heading_213 = heading_213_m.group(0)
        self.assertIn("Current", heading_213,
                      f"§2.13 must now say Current, got: {heading_213.strip()!r}")

    # -- Item 14: TC-13.8 Claude Code CLI Provider frozen contract --------

    # ── 14a: §2.11 section existence and heading ──────────────────────────

    def test_tc138_section_211_exists_and_is_current(self) -> None:
        """ADR §2.11 must exist as an independent heading marked Current
        with TC-13.8."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "### 2.11")
        self.assertIsNotNone(
            section,
            "ADR must contain ### 2.11 Claude Code CLI Provider",
        )
        heading_m = re.search(
            r"^### 2\.11\s.*$", adr_text, re.MULTILINE,
        )
        self.assertIsNotNone(heading_m, "ADR must have a §2.11 heading")
        heading = heading_m.group(0)
        self.assertIn(
            "Current",
            heading,
            f"§2.11 heading must say Current, got: {heading.strip()!r}",
        )
        self.assertNotIn(
            "Target",
            heading,
            f"§2.11 heading must NOT say Target, got: {heading.strip()!r}",
        )
        self.assertIn(
            "TC-13.8",
            heading,
            f"§2.11 heading must reference TC-13.8, got: {heading.strip()!r}",
        )

    # ── 14b: Interface Status #30 is Current ─────────────────────────────

    def test_tc138_interface_status_row_30_is_current(self) -> None:
        """ADR Interface Status row #30 (Claude Code CLI, TC-13.8) must
        be Current."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_interface_status_table(adr_text)

        tc138_row = None
        for row in rows:
            impl_cell = self._resolve_col(row, "Implemented by", "Impl", "Notes")
            task_ids = re.findall(r"\b(TC-\d+(?:\.\d+)*)\b", impl_cell)
            if "TC-13.8" in task_ids:
                tc138_row = row
                break

        self.assertIsNotNone(
            tc138_row,
            "ADR Interface Status must contain row #30 Implemented-by TC-13.8",
        )
        status_cell = self._resolve_col(tc138_row, "Status")
        self.assertIn(
            "Current",
            status_cell,
            f"ADR Interface Status #30 (TC-13.8) must be Current, "
            f"got status={status_cell!r}",
        )
        self.assertNotIn(
            "Target",
            status_cell,
            "ADR Interface Status #30 (TC-13.8) must NOT be marked Target",
        )

    # ── 14c: AgentCliProvider reference and AgentCliInvocation fields ────

    _INVOCATION_FIELDS = frozenset(
        {"executable", "argv", "stdin", "env_overrides"}
    )

    def test_tc138_contract_references_agent_cli_provider(self) -> None:
        """§2.11 must reference AgentCliProvider Protocol from TC-13.7."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "### 2.11")
        self.assertIsNotNone(section)
        self.assertIn(
            "AgentCliProvider",
            section,
            "ADR §2.11 must reference AgentCliProvider Protocol",
        )

    def test_tc138_invocation_four_fields_no_cwd(self) -> None:
        """§2.11 must define AgentCliInvocation with four fields and
        explicitly forbid a cwd field."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "### 2.11")
        self.assertIsNotNone(section)
        for field in sorted(self._INVOCATION_FIELDS):
            self.assertIn(
                field,
                section,
                f"ADR §2.11 must reference AgentCliInvocation field '{field}'",
            )
        # Must explicitly state no cwd field.
        target = section.lower()
        self.assertTrue(
            "no ``cwd`` field" in section
            or "no cwd field" in target
            or "not have a `cwd` field" in target
            or "must **not** have a `cwd` field" in section,
            "ADR §2.11 must state AgentCliInvocation has no cwd field",
        )

    def test_tc138_workspace_cwd_is_gateway_responsibility(self) -> None:
        """§2.11 must state workspace/cwd is the Gateway's responsibility,
        not the provider's."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "### 2.11")
        self.assertIsNotNone(section)
        # Must show cwd=str(request.workspace) belongs to Gateway.
        self.assertIn(
            "cwd",
            section.lower(),
            "§2.11 must reference cwd as Gateway responsibility",
        )
        self.assertIn(
            "request.workspace",
            section,
            "§2.11 must reference request.workspace for cwd",
        )

    # ── 14d: Provider ID rules ──────────────────────────────────────────

    def test_tc138_provider_id_exact_claude_claudecode(self) -> None:
        """§2.11 must state provider_id is exactly 'claude' or
        'claudecode'."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "### 2.11")
        self.assertIsNotNone(section)
        self.assertIn('"claude"', section,
                      "§2.11 must reference provider_id 'claude'")
        self.assertIn('"claudecode"', section,
                      "§2.11 must reference provider_id 'claudecode'")

    def test_tc138_forbids_alias_normalization(self) -> None:
        """§2.11 must explicitly forbid alias normalization of provider_id."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "### 2.11")
        self.assertIsNotNone(section)
        # Must explicitly prohibit normalizing claudecode → claude.
        target = section.lower()
        has_forbid = (
            "must **not** be alias-normalized" in section
            or "must not be alias-normalized" in target
            or 'selected_provider = "claude"' in section
        )
        self.assertTrue(
            has_forbid,
            "§2.11 must explicitly forbid alias normalization of provider_id",
        )

    # ── 14e: Prompt transmission — stdin only ────────────────────────────

    def test_tc138_prompt_via_stdin_utf8_no_bom(self) -> None:
        """§2.11 must state prompt is transmitted via stdin with UTF-8
        encoding and no BOM."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "### 2.11")
        self.assertIsNotNone(section)
        self.assertIn('encode("utf-8")', section,
                      "§2.11 must show stdin = request.prompt.encode('utf-8')")
        self.assertIn("BOM", section,
                      "§2.11 must explicitly forbid BOM")
        self.assertIn("stdin", section.lower(),
                      "§2.11 must reference stdin")

    def test_tc138_prompt_not_in_argv_env_or_tempfile(self) -> None:
        """§2.11 must forbid prompt in argv, env vars, or temp files."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "### 2.11")
        self.assertIsNotNone(section)
        target = section.lower()
        self.assertTrue(
            ("argv" in target and "not" in target)
            or "must **not** appear in `argv`" in section,
            "§2.11 must forbid prompt in argv",
        )
        self.assertTrue(
            "environment variable" in target or "environment" in target,
            "§2.11 must forbid prompt in environment variables",
        )
        self.assertTrue(
            "temporary file" in target or "temp" in target,
            "§2.11 must forbid prompt in temporary files",
        )

    def test_tc138_control_prompt_is_fixed_constant(self) -> None:
        """§2.11 must define a fixed, non-sensitive control prompt
        instructing Claude to read from stdin."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "### 2.11")
        self.assertIsNotNone(section)
        self.assertIn(
            "Read the task instructions from stdin",
            section,
            "§2.11 must contain the fixed control prompt string",
        )
        self.assertIn(
            "compile-time constant",
            section,
            "§2.11 must state control prompt is a compile-time constant",
        )

    # ── 14f: CLI invocation flags ───────────────────────────────────────

    def test_tc138_cli_includes_required_flags(self) -> None:
        """§2.11 must require -p, --output-format json, --model,
        --permission-mode, --effort, --no-session-persistence."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "### 2.11")
        self.assertIsNotNone(section)
        for flag in (
            "-p",
            "--output-format",
            "json",
            "--model",
            "--permission-mode",
            "--effort",
            "--no-session-persistence",
        ):
            self.assertIn(
                flag,
                section,
                f"§2.11 must include CLI flag '{flag}'",
            )

    def test_tc138_executable_and_argv_are_separated(self) -> None:
        """§2.11 must show executable and argv are strictly separated."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "### 2.11")
        self.assertIsNotNone(section)
        target = section.lower()
        self.assertTrue(
            "does **not** include executable" in section
            or "must **not** include the executable" in section
            or ("argv" in target and "not" in target and "executable" in target),
            "§2.11 must state argv does not include executable",
        )

    # ── 14g: Model mapping ──────────────────────────────────────────────

    def test_tc138_model_from_snapshot_selected_model_id(self) -> None:
        """§2.11 must state --model comes from
        request.model_selection.selected_model_id."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "### 2.11")
        self.assertIsNotNone(section)
        self.assertIn(
            "selected_model_id",
            section,
            "§2.11 must derive --model from selected_model_id",
        )
        # Must not use required_model_tier for model.
        target = section.lower()
        self.assertTrue(
            "required_model_tier" not in section
            or "must not" in target
            or "must **not** use `required_model_tier`" in section
            or "not" in section.split("required_model_tier")[0][-50:]
            if "required_model_tier" in section
            else True,
            "§2.11 must warn against using required_model_tier for model",
        )

    def test_tc138_empty_model_id_fail_closed(self) -> None:
        """§2.11 must state empty/invalid model ID is fail-closed."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "### 2.11")
        self.assertIsNotNone(section)
        self.assertIn(
            "fail closed",
            section.lower(),
            "§2.11 must mention fail-closed for invalid model ID",
        )

    # ── 14h: Effort mapping ─────────────────────────────────────────────

    _EFFORT_MAPPING = {
        "efficient": "low",
        "balanced": "medium",
        "deep": "high",
    }

    def test_tc138_effort_mapping_exact(self) -> None:
        """§2.11 must define exact efficient→low, balanced→medium,
        deep→high mapping."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "### 2.11")
        self.assertIsNotNone(section)
        for agentdesk_tier, claude_effort in sorted(self._EFFORT_MAPPING.items()):
            self.assertIn(
                agentdesk_tier,
                section,
                f"§2.11 effort mapping missing input: {agentdesk_tier}",
            )
            self.assertIn(
                claude_effort,
                section,
                f"§2.11 effort mapping missing output: {claude_effort}",
            )

    def test_tc138_unknown_deliberation_tier_fail_closed(self) -> None:
        """§2.11 must state unknown deliberation tier is fail-closed."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "### 2.11")
        self.assertIsNotNone(section)
        target = section.lower()
        self.assertTrue(
            ("unknown" in target and "fail" in target)
            or ("unknown" in target and "closed" in target),
            "§2.11 must state unknown deliberation tier fails closed",
        )

    def test_tc138_effort_not_semantically_identical(self) -> None:
        """§2.11 must state effort mapping is provider-specific and
        does not imply semantic identity."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "### 2.11")
        self.assertIsNotNone(section)
        target = section.lower()
        self.assertTrue(
            "not" in target and "identical" in target
            or "provider-specific" in target,
            "§2.11 must note effort systems are not semantically identical",
        )

    # ── 14i: Permission mode ────────────────────────────────────────────

    _SAFE_MODES = frozenset({"default", "plan", "acceptEdits", "dontAsk"})
    _FORBIDDEN_MODES = frozenset({"bypassPermissions", "delegate"})

    def test_tc138_permission_mode_safe_set(self) -> None:
        """§2.11 must list exact allowed permission modes."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "### 2.11")
        self.assertIsNotNone(section)
        for mode in sorted(self._SAFE_MODES):
            self.assertIn(
                mode,
                section,
                f"§2.11 must allow permission mode '{mode}'",
            )

    def test_tc138_permission_mode_forbids_bypass_and_delegate(self) -> None:
        """§2.11 must explicitly forbid bypassPermissions and delegate."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "### 2.11")
        self.assertIsNotNone(section)
        self.assertIn(
            "bypassPermissions",
            section,
            "§2.11 must explicitly forbid bypassPermissions",
        )
        self.assertIn(
            "delegate",
            section,
            "§2.11 must explicitly forbid delegate",
        )
        self.assertIn(
            "--dangerously-skip-permissions",
            section,
            "§2.11 must explicitly forbid --dangerously-skip-permissions",
        )

    # ── 14j: Tool allow/deny deeply immutable tuples ────────────────────

    def test_tc138_tool_allow_deny_immutable_tuples(self) -> None:
        """§2.11 must declare allowed_tools and disallowed_tools as
        tuple[str, ...]."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "### 2.11")
        self.assertIsNotNone(section)
        self.assertIn(
            "tuple[str, ...]",
            section,
            "§2.11 must declare tool lists as tuple[str, ...]",
        )
        self.assertIn(
            "allowed_tools",
            section,
            "§2.11 must reference allowed_tools",
        )
        self.assertIn(
            "disallowed_tools",
            section,
            "§2.11 must reference disallowed_tools",
        )

    def test_tc138_tool_entries_must_be_nonempty_strings(self) -> None:
        """§2.11 must state tool entries are non-empty strings with no
        whitespace padding."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "### 2.11")
        self.assertIsNotNone(section)
        target = section.lower()
        self.assertTrue(
            "non-empty" in target or "nonempty" in target
            or "empty string" in target,
            "§2.11 must forbid empty tool entries",
        )
        self.assertTrue(
            "whitespace" in target,
            "§2.11 must forbid leading/trailing whitespace in tool entries",
        )

    def test_tc138_tool_duplicates_forbidden(self) -> None:
        """§2.11 must forbid duplicate tool entries."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "### 2.11")
        self.assertIsNotNone(section)
        self.assertIn(
            "duplicate",
            section.lower(),
            "§2.11 must forbid duplicate tool entries",
        )

    def test_tc138_tool_source_mutation_does_not_affect_provider(self) -> None:
        """§2.11 must state mutating source list after construction
        does not affect stored tuples."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "### 2.11")
        self.assertIsNotNone(section)
        target = section.lower()
        self.assertTrue(
            "mutating" in target
            or "source" in target
            or "does **not** affect" in section,
            "§2.11 must state source mutation does not affect stored config",
        )

    # ── 14k: Environment / secrets boundary ──────────────────────────────

    def test_tc138_env_overrides_empty(self) -> None:
        """§2.11 must freeze env_overrides == ()."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "### 2.11")
        self.assertIsNotNone(section)
        self.assertIn(
            "env_overrides",
            section,
            "§2.11 must reference env_overrides",
        )
        self.assertIn(
            "()",
            section,
            "§2.11 must show env_overrides == ()",
        )

    def test_tc138_no_auth_or_secrets_in_provider(self) -> None:
        """§2.11 must state provider does not handle API keys,
        secrets, or auth env vars."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "### 2.11")
        self.assertIsNotNone(section)
        target = section.lower()
        self.assertTrue(
            "api key" in target or "api_key" in target
            or "authentication" in target,
            "§2.11 must state auth boundary",
        )
        # Provider must state it does NOT set MAD_HOME (negation, not absence).
        self.assertIn(
            "does **not** set `MAD_HOME`",
            section,
            "§2.11 must state provider does NOT set MAD_HOME",
        )
        # Provider must state it does NOT set MAD_PARTICIPANT (negation).
        self.assertIn(
            "does **not** set `MAD_PARTICIPANT`",
            section,
            "§2.11 must state provider does NOT set MAD_PARTICIPANT",
        )

    # ── 14l: Forbidden flags ────────────────────────────────────────────

    _FORBIDDEN_FLAGS = frozenset({
        "--continue",
        "--resume",
        "--session-id",
        "--fork-session",
        "--remote",
        "--teleport",
    })

    def test_tc138_forbidden_session_resume_remote_flags(self) -> None:
        """§2.11 must explicitly forbid session, resume, remote, and
        teleport flags."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "### 2.11")
        self.assertIsNotNone(section)
        for flag in sorted(self._FORBIDDEN_FLAGS):
            self.assertIn(
                flag,
                section,
                f"§2.11 must explicitly forbid {flag}",
            )

    def test_tc138_forbids_add_dir_for_primary_workspace(self) -> None:
        """§2.11 must forbid --add-dir for primary workspace."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "### 2.11")
        self.assertIsNotNone(section)
        self.assertIn(
            "--add-dir",
            section,
            "§2.11 must explicitly forbid --add-dir for primary workspace",
        )

    # ── 14m: Output boundary ────────────────────────────────────────────

    def test_tc138_output_parsing_deferred_to_tc139c(self) -> None:
        """§2.11 must state output parsing belongs to TC-13.9c, not the
        Claude provider."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "### 2.11")
        self.assertIsNotNone(section)
        # Output interpretation must reference TC-13.9c.
        self.assertIn(
            "TC-13.9",
            section,
            "§2.11 must reference TC-13.9 for output interpretation",
        )
        self.assertIn(
            "opaque",
            section.lower(),
            "§2.11 must state stdout is opaque bytes (TC-13.7 contract)",
        )

    # ── 14n: Configuration object ───────────────────────────────────────

    def test_tc138_config_fields_provider_id_exec_permission_tools(self) -> None:
        """§2.11 must define config with provider_id, executable,
        permission_mode, allowed_tools, disallowed_tools."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "### 2.11")
        self.assertIsNotNone(section)
        for field in (
            "provider_id",
            "executable",
            "permission_mode",
            "allowed_tools",
            "disallowed_tools",
        ):
            self.assertIn(
                field,
                section,
                f"§2.11 config must include field '{field}'",
            )

    def test_tc138_config_forbids_secrets_and_persistence(self) -> None:
        """§2.11 must forbid API keys, tokens, session IDs, persistence
        paths in config."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "### 2.11")
        self.assertIsNotNone(section)
        target = section.lower()
        self.assertTrue(
            "api key" in target
            or "oauth" in target
            or "token" in target,
            "§2.11 must forbid auth tokens in config",
        )
        self.assertTrue(
            "session" in target or "persistence" in target,
            "§2.11 must forbid session persistence in config",
        )

    # ── 14o: TC-13.9 still Target ───────────────────────────────────────

    def test_tc139c_target_in_future_task_cards(self) -> None:
        """TC-13.9c must be in Future Task Cards §5."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_future_task_cards_table(adr_text)
        task_ids_seen = {
            self._resolve_col(row, "Task Card", "Task", "#")
            for row in rows
        }
        self.assertIn("TC-13.9c", task_ids_seen,
                      "ADR §5 Future Task Cards must contain TC-13.9c")

    # ── 14p: #29 DispatcherAgentGateway still Current ──────────────────

    def test_tc138_tc137_row_29_still_current(self) -> None:
        """#29 DispatcherAgentGateway must remain Current after TC-13.8
        contract freeze."""
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
        status_cell = self._resolve_col(tc137_row, "Status")
        self.assertIn(
            "Current",
            status_cell,
            f"#29 TC-13.7 must remain Current, got: {status_cell}",
        )

    # ── 14q: §2.11 scope exclusions ────────────────────────────────────

    def test_tc138_section_211_excludes_worker_adapter_and_budget(self) -> None:
        """§2.11 must state WorkerAdapter, ContextBudgetPolicy,
        retry, lease, slot, escalation, rate-limiting are out of scope."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "### 2.11")
        self.assertIsNotNone(section)
        target = section.lower()
        for excluded in (
            "workeradapter",
            "contextbudgetpolicy",
            "retry",
            "lease",
            "slot",
            "escalation",
            "rate limiting",
        ):
            self.assertIn(
                excluded,
                target,
                f"§2.11 must exclude '{excluded}' from TC-13.8 scope",
            )

    # -- Item 15: TC-13.8 precise public API remediation -------------------

    # ── 15a: ClaudeCodeProvider class as frozen slotted dataclass ───────

    def test_tc138_class_is_claudecodeprovider_frozen_slots(self) -> None:
        """§2.11.9 must declare ClaudeCodeProvider as
        @dataclass(frozen=True, slots=True)."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.11.9")
        self.assertIsNotNone(section, "ADR must contain §2.11.9")
        self.assertIn(
            "ClaudeCodeProvider",
            section,
            "§2.11.9 must declare ClaudeCodeProvider class",
        )
        self.assertIn(
            "frozen=True",
            section,
            "§2.11.9 must declare frozen=True",
        )
        self.assertIn(
            "slots=True",
            section,
            "§2.11.9 must declare slots=True",
        )

    def test_tc138_no_separate_config_class(self) -> None:
        """§2.11.9 must state there is no separate
        ClaudeCodeProviderConfig class — the negation must exist."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.11.9")
        self.assertIsNotNone(section)
        # The ADR says "no separate ClaudeCodeProviderConfig" — verify
        # the negation language exists.  The name appears in the negation.
        target = section.lower()
        self.assertTrue(
            "no separate" in target
            or "there is no separate" in target
            or "is no separate" in target,
            "§2.11.9 must state there is no separate config class",
        )

    def test_tc138_no_at_minimum_language(self) -> None:
        """§2.11.9 must NOT say 'at minimum these fields' — the five
        fields are exact."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.11.9")
        self.assertIsNotNone(section)
        self.assertNotIn(
            "at minimum",
            section.lower(),
            "§2.11.9 must NOT contain 'at minimum' — five fields are exact",
        )

    # ── 15b: Exactly five fields ────────────────────────────────────────

    _EXACT_FIVE_FIELDS = frozenset({
        "provider_id",
        "executable",
        "permission_mode",
        "allowed_tools",
        "disallowed_tools",
    })

    _FORBIDDEN_SIXTH = frozenset({
        "cwd", "workspace", "prompt", "timeout", "model", "model_id",
        "effort", "env", "env_overrides", "api_key", "token",
        "session_id", "resume_id", "retry", "slot", "lease",
        "persistence_path",
    })

    def test_tc138_exact_five_fields_declared(self) -> None:
        """§2.11.9 must list exactly five fields: provider_id,
        executable, permission_mode, allowed_tools, disallowed_tools."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.11.9")
        self.assertIsNotNone(section)
        for field in sorted(self._EXACT_FIVE_FIELDS):
            self.assertIn(
                field,
                section,
                f"§2.11.9 must declare field '{field}'",
            )

    def test_tc138_forbids_sixth_field_cwd_secret_model(self) -> None:
        """§2.11.9 must explicitly forbid cwd, workspace, prompt,
        timeout, model, secrets, session, persistence from
        ClaudeCodeProvider fields."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.11.9")
        self.assertIsNotNone(section)
        for field in sorted(self._FORBIDDEN_SIXTH):
            self.assertIn(
                field,
                section,
                f"§2.11.9 must forbid '{field}' as a provider field",
            )

    def test_tc138_env_overrides_is_invocation_not_provider_field(self) -> None:
        """§2.11.9 must state env_overrides is a fixed
        AgentCliInvocation value, not a sixth provider field."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.11.9")
        self.assertIsNotNone(section)
        target = section.lower()
        self.assertTrue(
            "not a sixth" in target
            or "not a provider field" in target
            or "fixed value on the generated" in target,
            "§2.11.9 must state env_overrides is not a provider field",
        )

    # ── 15c: __all__ and _CONTROL_PROMPT ─────────────────────────────────

    def test_tc138_exact_all_claudecodeprovider(self) -> None:
        """§2.11.9 must freeze __all__ = ['ClaudeCodeProvider']."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.11.9")
        self.assertIsNotNone(section)
        self.assertIn(
            '__all__ = ["ClaudeCodeProvider"]',
            section,
            "§2.11.9 must freeze exact __all__",
        )

    def test_tc138_control_prompt_is_private_not_in_all(self) -> None:
        """§2.11.9 must show _CONTROL_PROMPT as private, not in __all__."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.11.9")
        self.assertIsNotNone(section)
        self.assertIn(
            "_CONTROL_PROMPT",
            section,
            "§2.11.9 must define _CONTROL_PROMPT",
        )
        # Must be private (leading underscore) and not in __all__.
        self.assertIn(
            "private",
            section.lower(),
            "§2.11.9 must state _CONTROL_PROMPT is private",
        )

    # ── 15d: build_invocation signature ──────────────────────────────────

    def test_tc138_build_invocation_returns_agent_cli_invocation(self) -> None:
        """§2.11.9 must define build_invocation(request) -> AgentCliInvocation."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.11.9")
        self.assertIsNotNone(section)
        self.assertIn(
            "build_invocation",
            section,
            "§2.11.9 must define build_invocation method",
        )
        self.assertIn(
            "DispatchRequest",
            section,
            "§2.11.9 must show DispatchRequest parameter",
        )
        self.assertIn(
            "AgentCliInvocation",
            section,
            "§2.11.9 must show AgentCliInvocation return type",
        )

    # ── 15e: ValueError semantics ────────────────────────────────────────

    def test_tc138_construction_rejection_uses_valueerror(self) -> None:
        """§2.11.9 must state construction rejects illegal input with
        ValueError."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.11.9")
        self.assertIsNotNone(section)
        self.assertIn(
            "ValueError",
            section,
            "§2.11.9 must use ValueError for construction rejection",
        )

    def test_tc138_build_invocation_rejection_uses_valueerror(self) -> None:
        """§2.11.9 must state build_invocation raises ValueError for
        unmappable request fields."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.11.9")
        self.assertIsNotNone(section)
        target = section.lower()
        self.assertTrue(
            "build_invocation" in target and "valueerror" in target,
            "§2.11.9 must state build_invocation uses ValueError",
        )

    def test_tc138_no_parallel_exception_hierarchy(self) -> None:
        """§2.11.9 must state TC-13.8 does NOT introduce a parallel
        public exception hierarchy."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.11.9")
        self.assertIsNotNone(section)
        # Text may be split across lines — normalize whitespace.
        target = " ".join(section.split())
        self.assertIn(
            "does **not** introduce a parallel public exception hierarchy",
            target,
            "§2.11.9 must state TC-13.8 does NOT introduce a parallel "
            "exception hierarchy",
        )

    def test_tc138_gateway_wraps_provider_exceptions(self) -> None:
        """§2.11.9 must state Gateway wraps provider ValueError into
        DispatchInvocationError."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.11.9")
        self.assertIsNotNone(section)
        self.assertIn(
            "DispatchInvocationError",
            section,
            "§2.11.9 must reference DispatchInvocationError wrapping",
        )

    # ── 15f: Executable precise rules ────────────────────────────────────

    def test_tc138_executable_allows_ordinary_spaces(self) -> None:
        """§2.11.9 must state executable paths may contain ordinary
        spaces (e.g. Windows paths)."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.11.9")
        self.assertIsNotNone(section)
        target = section.lower()
        self.assertTrue(
            "program files" in target
            or "ordinary space" in target
            or "c:\\program files" in target,
            "§2.11.9 must allow ordinary spaces in executable paths",
        )

    def test_tc138_executable_forbids_nul_cr_lf(self) -> None:
        """§2.11.9 must forbid NUL, CR, LF in executable."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.11.9")
        self.assertIsNotNone(section)
        self.assertIn(
            "NUL",
            section,
            "§2.11.9 must forbid NUL in executable",
        )
        self.assertIn(
            "CR",
            section,
            "§2.11.9 must forbid CR in executable",
        )
        self.assertIn(
            "LF",
            section,
            "§2.11.9 must forbid LF in executable",
        )

    def test_tc138_executable_forbids_leading_trailing_whitespace(self) -> None:
        """§2.11.9 must forbid leading/trailing whitespace in
        executable."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.11.9")
        self.assertIsNotNone(section)
        target = section.lower()
        self.assertTrue(
            "leading or trailing" in target
            or "leading and trailing" in target
            or "not contain leading" in target,
            "§2.11.9 must forbid leading/trailing whitespace in executable",
        )

    def test_tc138_provider_does_not_resolve_executable(self) -> None:
        """§2.11.9 must state provider does not call shutil.which or
        shlex.split."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.11.9")
        self.assertIsNotNone(section)
        target = section.lower()
        self.assertTrue(
            "shutil.which" in target or "shlex.split" in target,
            "§2.11.9 must state provider does not resolve executable",
        )

    # ── 15g: Tool argv serialization ────────────────────────────────────

    def test_tc138_tool_flag_exact_casing(self) -> None:
        """§2.11.4 must use exact flag casing --allowedTools and
        --disallowedTools."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.11.4")
        self.assertIsNotNone(section, "ADR must contain §2.11.4")
        self.assertIn(
            "--allowedTools",
            section,
            "§2.11.4 must use --allowedTools (exact casing)",
        )
        self.assertIn(
            "--disallowedTools",
            section,
            "§2.11.4 must use --disallowedTools (exact casing)",
        )

    def test_tc138_tool_allowed_block_before_disallowed(self) -> None:
        """§2.11.4 must state allowed block precedes disallowed block
        in argv."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.11.4")
        self.assertIsNotNone(section)
        target = section.lower()
        self.assertTrue(
            "allowed block always precedes" in target
            or "allowed block" in target and "precedes" in target
            or ("precedes" in target and "disallowed" in target),
            "§2.11.4 must state allowed block precedes disallowed block",
        )

    def test_tc138_single_flag_per_tool_block(self) -> None:
        """§2.11.4 must state flag is emitted once per block, not
        repeated per tool."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.11.4")
        self.assertIsNotNone(section)
        target = section.lower()
        self.assertTrue(
            "flag is emitted once" in target
            or "not repeated per tool" in target
            or "not repeated" in target,
            "§2.11.4 must state flag is not repeated per tool",
        )

    def test_tc138_tool_expressions_are_separate_argv_elements(self) -> None:
        """§2.11.4 must show each tool expression is a separate argv
        element."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.11.4")
        self.assertIsNotNone(section)
        target = section.lower()
        self.assertTrue(
            "separate `argv` element" in section
            or "separate argv element" in target
            or "not joined" in target,
            "§2.11.4 must state tools are separate argv elements",
        )

    def test_tc138_empty_tool_tuple_omits_block(self) -> None:
        """§2.11.4 must state empty tuple omits the corresponding flag
        block entirely."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.11.4")
        self.assertIsNotNone(section)
        target = section.lower()
        self.assertTrue(
            "omitted entirely" in target
            or "omitted" in target,
            "§2.11.4 must state empty tuple omits flag block",
        )

    def test_tc138_tool_argv_example_allowed_and_disallowed(self) -> None:
        """§2.11.4 must contain a precise argv example with both
        --allowedTools and --disallowedTools non-empty."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.11.4")
        self.assertIsNotNone(section)
        # Must show both flags in the same example.
        self.assertIn(
            'Bash(git status:*)',
            section,
            "§2.11.4 must contain example with Bash(git status:*)",
        )
        self.assertIn(
            "WebFetch",
            section,
            "§2.11.4 must contain example disallowing WebFetch",
        )

    def test_tc138_tool_argv_example_allowed_only(self) -> None:
        """§2.11.4 must contain a precise argv example where
        disallowed_tools is empty and the block is omitted."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.11.4")
        self.assertIsNotNone(section)
        self.assertIn(
            'Bash(curl:*)',
            section,
            "§2.11.4 must contain example with Bash(curl:*) allowed only",
        )
        # The disallowed block should be absent in this example.
        self.assertIn(
            "disallowed_tools = ()",
            section,
            "§2.11.4 must show disallowed_tools = () example",
        )

    def test_tc138_tool_prompt_not_in_tool_flags(self) -> None:
        """§2.11.4 must forbid task prompt in tool flags."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.11.4")
        self.assertIsNotNone(section)
        target = section.lower()
        self.assertTrue(
            "task prompt" in target and "not" in target,
            "§2.11.4 must forbid task prompt in tool flags",
        )

    # ── 15h: Tool intersection fail-closed ──────────────────────────────

    def test_tc138_tool_intersection_must_be_disjoint(self) -> None:
        """§2.11.8 must require set(allowed).isdisjoint(disallowed)."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.11.8")
        self.assertIsNotNone(section, "ADR must contain §2.11.8")
        self.assertIn(
            "isdisjoint",
            section,
            "§2.11.8 must use isdisjoint for intersection check",
        )

    def test_tc138_tool_intersection_raises_valueerror(self) -> None:
        """§2.11.8 must state non-disjoint tools raise ValueError at
        construction."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.11.8")
        self.assertIsNotNone(section)
        self.assertIn(
            "ValueError",
            section,
            "§2.11.8 must use ValueError for non-disjoint tools",
        )

    def test_tc138_tool_intersection_no_guessing_priority(self) -> None:
        """§2.11.8 must forbid guessing deny-priority or allow-priority."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.11.8")
        self.assertIsNotNone(section)
        target = section.lower()
        self.assertTrue(
            "not guess" in target
            or "no guessing" in target
            or "must **not** guess" in section
            or ("deny" in target and "priority" in target and "not" in target),
            "§2.11.8 must forbid guessing priority",
        )

    def test_tc138_tool_intersection_no_case_folding(self) -> None:
        """§2.11.8 must forbid case-folding before intersection check."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.11.8")
        self.assertIsNotNone(section)
        target = section.lower()
        self.assertTrue(
            "case-fold" in target or "case fold" in target
            or "not case" in target,
            "§2.11.8 must forbid case-folding in intersection check",
        )

    # ── 15i: Production provider module exists ──────────────────────────────

    def test_tc138_production_provider_module_exists(self) -> None:
        """claude_code_provider.py must exist — TC-13.8 is now Current."""
        provider_path = (
            SKILL_ROOT / "scripts" / "claude_code_provider.py"
        )
        self.assertTrue(
            provider_path.is_file(),
            "claude_code_provider.py must exist — TC-13.8 is Current",
        )

    def test_tc138_test_file_exists(self) -> None:
        """test_claude_code_provider.py must exist."""
        test_path = (
            REPO_ROOT / "tests" / "test_claude_code_provider.py"
        )
        self.assertTrue(
            test_path.is_file(),
            "test_claude_code_provider.py must exist — TC-13.8 is Current",
        )

    def test_tc138_module_exports_claude_code_provider(self) -> None:
        """Module must export ClaudeCodeProvider."""
        import sys as _sys
        _sys.path.insert(0, str(SKILL_ROOT / "scripts"))
        try:
            import claude_code_provider as _ccp
            self.assertTrue(hasattr(_ccp, "ClaudeCodeProvider"),
                            "Module must export ClaudeCodeProvider")
            self.assertEqual(_ccp.__all__, ["ClaudeCodeProvider"])
        finally:
            _sys.path.pop(0)

    def test_tc138_dataclass_exact_five_fields(self) -> None:
        """ClaudeCodeProvider must have exactly five fields."""
        from dataclasses import fields as _fields
        import sys as _sys
        _sys.path.insert(0, str(SKILL_ROOT / "scripts"))
        try:
            import claude_code_provider as _ccp
            field_names = {f.name for f in _fields(_ccp.ClaudeCodeProvider)}
            self.assertSetEqual(
                field_names,
                {"provider_id", "executable", "permission_mode",
                 "allowed_tools", "disallowed_tools"},
            )
        finally:
            _sys.path.pop(0)

    def test_tc138_control_prompt_not_in_fields_or_all(self) -> None:
        """_CONTROL_PROMPT must not be a dataclass field or in __all__."""
        from dataclasses import fields as _fields
        import sys as _sys
        _sys.path.insert(0, str(SKILL_ROOT / "scripts"))
        try:
            import claude_code_provider as _ccp
            field_names = {f.name for f in _fields(_ccp.ClaudeCodeProvider)}
            self.assertNotIn("_CONTROL_PROMPT", field_names)
            self.assertNotIn("_CONTROL_PROMPT", _ccp.__all__)
        finally:
            _sys.path.pop(0)

    def test_tc138_top_level_import_correct(self) -> None:
        """Production module must use top-level 'from dispatcher_gateway import'."""
        src = (
            SKILL_ROOT / "scripts" / "claude_code_provider.py"
        ).read_text(encoding="utf-8")
        self.assertIn("from dispatcher_gateway import", src)
        self.assertNotIn("from .dispatcher_gateway import", src)

    # ── 15j: #30 and §2.11 now Current ─────────────────────────────────

    def test_tc138_interface_status_row_30_is_current(self) -> None:
        """ADR Interface Status #30 must now be Current."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_interface_status_table(adr_text)

        row_30 = None
        for row in rows:
            number_cell = self._resolve_col(row, "#")
            if number_cell == "30":
                row_30 = row
                break
        self.assertIsNotNone(row_30)
        status = self._resolve_col(row_30, "Status")
        self.assertIn("Current", status,
                      f"#30 must now be Current, got: {status}")
        self.assertNotIn("Target", status,
                         f"#30 must NOT be Target, got: {status}")

    def test_tc138_section_211_is_current(self) -> None:
        """§2.11 heading must now say Current."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        heading_m = re.search(
            r"^### 2\.11\s.*$", adr_text, re.MULTILINE,
        )
        self.assertIsNotNone(heading_m)
        heading = heading_m.group(0)
        self.assertIn(
            "Current",
            heading,
            f"§2.11 heading must now say Current, got: {heading.strip()!r}",
        )
        self.assertNotIn(
            "Target",
            heading,
            f"§2.11 heading must NOT say Target, got: {heading.strip()!r}",
        )

    def test_tc139b_row_14_now_current(self) -> None:
        """ADR Interface Status row for TC-13.9b (WorkerAdapter) must
        now be Current."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_interface_status_table(adr_text)

        tc139b_row = None
        for row in rows:
            impl_cell = self._resolve_col(row, "Implemented by", "Impl", "Notes")
            task_ids = re.findall(r"\bTC-\d+\.\d+[a-z]?\b", impl_cell)
            if "TC-13.9b" in task_ids:
                tc139b_row = row
                break

        if tc139b_row is None:
            for row in rows:
                notes = self._resolve_col(row, "Notes")
                if "TC-13.9b" in notes:
                    tc139b_row = row
                    break
        self.assertIsNotNone(tc139b_row,
                             "ADR must contain row for TC-13.9b")
        status = self._resolve_col(tc139b_row, "Status")
        self.assertIn("Current", status,
                      f"TC-13.9b must now be Current, got: {status}")
        self.assertNotIn("Target", status,
                         f"TC-13.9b must NOT be Target, got: {status}")

    # — 15k: No Codex provider started —

    def test_tc138_no_codex_provider_started(self) -> None:
        """codex_cli_provider.py must now exist — TC-13.8.4 is Current."""
        codex_path = (
            SKILL_ROOT / "scripts" / "codex_cli_provider.py"
        )
        self.assertTrue(
            codex_path.exists(),
            "codex_cli_provider.py must exist — TC-13.8.4 is Current",
        )

    # -- Item 16: TC-13.8 contract code example executability ------------

    # ── 16a: _CONTROL_PROMPT is module-level, not a class field ───────

    def test_tc138_control_prompt_is_module_level_before_class(self) -> None:
        """§2.11.9 code example must place _CONTROL_PROMPT before
        @dataclass/class, not inside the class body."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.11.9")
        self.assertIsNotNone(section)
        # _CONTROL_PROMPT must appear before the @dataclass line in the
        # code fence example.
        code_fence_m = re.search(r"```python\n(.*?)```", section, re.DOTALL)
        self.assertIsNotNone(code_fence_m,
                             "§2.11.9 must contain a Python code block")
        code = code_fence_m.group(1)
        # Find positions of _CONTROL_PROMPT and @dataclass.
        ctrl_pos = code.find("_CONTROL_PROMPT")
        dataclass_pos = code.find("@dataclass")
        self.assertGreater(ctrl_pos, -1,
                           "§2.11.9 code must contain _CONTROL_PROMPT")
        self.assertLess(
            ctrl_pos, dataclass_pos,
            "§2.11.9 _CONTROL_PROMPT must appear before @dataclass "
            "(module-level, not class-level)",
        )

    def test_tc138_control_prompt_is_module_constant_not_dataclass_field(self) -> None:
        """§2.11.9 prose must state _CONTROL_PROMPT is a module-level
        constant and explicitly NOT a dataclass field."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.11.9")
        self.assertIsNotNone(section)
        # Normalise whitespace to handle line breaks.
        flat = " ".join(section.split())
        self.assertIn(
            "not a dataclass field",
            flat,
            "§2.11.9 must state _CONTROL_PROMPT is NOT a dataclass field",
        )
        self.assertIn(
            "module-level constant",
            flat,
            "§2.11.9 must state _CONTROL_PROMPT is a module-level constant",
        )

    def test_tc138_dataclass_fields_exactly_five(self) -> None:
        """§2.11.9 must state dataclasses.fields(ClaudeCodeProvider)
        returns exactly five fields."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.11.9")
        self.assertIsNotNone(section)
        self.assertIn(
            "exactly five",
            section.lower(),
            "§2.11.9 must state dataclasses.fields() returns exactly five",
        )

    def test_tc138_class_body_does_not_contain_control_prompt_field(self) -> None:
        """§2.11.9 class body code must NOT show _CONTROL_PROMPT as a
        typed field inside ClaudeCodeProvider."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.11.9")
        self.assertIsNotNone(section)
        # Extract the class body from the code fence.
        code_fence_m = re.search(r"```python\n(.*?)```", section, re.DOTALL)
        self.assertIsNotNone(code_fence_m)
        code = code_fence_m.group(1)
        # _CONTROL_PROMPT as a class-level typed annotation would be
        # indented inside the class.  Check it's not.
        for line in code.splitlines():
            if "_CONTROL_PROMPT" in line and line.strip().startswith("_"):
                self.assertTrue(
                    line.startswith("_CONTROL_PROMPT"),
                    f"§2.11.9 _CONTROL_PROMPT must not be indented inside "
                    f"class — got: {line.strip()!r}",
                )

    # ── 16b: Top-level import from dispatcher_gateway ──────────────────

    def test_tc138_import_uses_top_level_dispatcher_gateway(self) -> None:
        """§2.11.9 must use 'from dispatcher_gateway import ...'
        (top-level), NOT relative import."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.11.9")
        self.assertIsNotNone(section)
        self.assertIn(
            "from dispatcher_gateway import",
            section,
            "§2.11.9 must use top-level 'from dispatcher_gateway import'",
        )

    def test_tc138_forbids_relative_import_of_dispatcher_gateway(self) -> None:
        """§2.11.9 must NOT contain 'from .dispatcher_gateway import'."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.11.9")
        self.assertIsNotNone(section)
        self.assertNotIn(
            "from .dispatcher_gateway import",
            section,
            "§2.11.9 must NOT use relative import for dispatcher_gateway",
        )

    def test_tc138_no_init_py_required(self) -> None:
        """§2.11.9 must NOT require adding __init__.py to
        skills/agentdesk/scripts/."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.11.9")
        self.assertIsNotNone(section)
        # Either explicitly states no __init__.py needed, or simply
        # uses top-level import without qualification.
        self.assertIn(
            "from dispatcher_gateway import",
            section,
            "§2.11.9 import style must not require __init__.py",
        )

    def test_tc138_all_still_exact_claudecodeprovider(self) -> None:
        """§2.11.9 must still freeze __all__ = ['ClaudeCodeProvider']
        exactly, with no _CONTROL_PROMPT in it."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.11.9")
        self.assertIsNotNone(section)
        self.assertIn(
            '__all__ = ["ClaudeCodeProvider"]',
            section,
            "§2.11.9 must freeze __all__ with only ClaudeCodeProvider",
        )

    # ── 16c: §2.11 and #30 now Current ────────────────────────────────

    def test_tc138_section_211_and_row_30_current_post_remediation(self) -> None:
        """After remediation, §2.11 and Interface Status #30 must now
        be Current."""
        adr_text = self._adr_path().read_text(encoding="utf-8")

        # §2.11 heading.
        heading_m = re.search(
            r"^### 2\.11\s.*$", adr_text, re.MULTILINE,
        )
        self.assertIsNotNone(heading_m)
        self.assertIn("Current", heading_m.group(0))

        # #30 in interface table.
        rows = self._parse_interface_status_table(adr_text)
        row_30 = None
        for row in rows:
            if self._resolve_col(row, "#") == "30":
                row_30 = row
                break
        self.assertIsNotNone(row_30)
        self.assertIn("Current", self._resolve_col(row_30, "Status"))

    def test_tc139b_now_current_post_remediation(self) -> None:
        """After remediation, TC-13.9b must now be Current."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_interface_status_table(adr_text)
        tc139b_row = None
        for row in rows:
            impl = self._resolve_col(row, "Implemented by", "Impl", "Notes")
            if "TC-13.9b" in re.findall(r"\bTC-\d+\.\d+[a-z]?\b", impl):
                tc139b_row = row
                break
        # Fallback: search in Notes
        if tc139b_row is None:
            for row in rows:
                notes = self._resolve_col(row, "Notes")
                if "TC-13.9b" in notes:
                    tc139b_row = row
                    break
        self.assertIsNotNone(tc139b_row,
                             "ADR must contain row for TC-13.9b")
        status = self._resolve_col(tc139b_row, "Status")
        self.assertIn("Current", status)
        self.assertNotIn("Target", status)

    def test_tc139c_still_target(self) -> None:
        """TC-13.9c must still be Target in Future Task Cards."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_future_task_cards_table(adr_text)
        by_id = {
            self._resolve_col(row, "Task Card", "Task", "#"): row
            for row in rows
        }
        tc139c = by_id.get("TC-13.9c")
        self.assertIsNotNone(tc139c, "ADR §5 must contain TC-13.9c")
        # TC-13.9c is a future task card — it does not have a dedicated
        # Interface Status row.  Verify it's in §5 only.

    def test_tc138_production_file_now_exists(self) -> None:
        """claude_code_provider.py must now exist."""
        self.assertTrue(
            (SKILL_ROOT / "scripts" / "claude_code_provider.py").is_file(),
            "claude_code_provider.py must exist — TC-13.8 is Current",
        )

    # ── Item 17: TC-13.8.3 Codex CLI Provider frozen contract ──────────────

    def test_tc1383_interface_status_row_31_exists_and_is_current(self) -> None:
        """ADR Interface Status must contain row #31 for Codex CLI Provider
        with Current status."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_interface_status_table(adr_text)

        row_31 = None
        for row in rows:
            number_cell = self._resolve_col(row, "#")
            if number_cell == "31":
                row_31 = row
                break
        self.assertIsNotNone(
            row_31,
            "ADR Interface Status must contain row #31 for Codex CLI Provider",
        )
        status = self._resolve_col(row_31, "Status")
        self.assertIn(
            "Current",
            status,
            f"#31 Codex CLI Provider must be Current, got: {status}",
        )
        impl = self._resolve_col(row_31, "Implemented by", "Impl", "Notes")
        self.assertIn(
            "TC-13.8.4",
            impl,
            f"#31 must reference TC-13.8.4, got: {impl}",
        )

    def test_tc1383_section_212_exists_and_is_current(self) -> None:
        """ADR must contain §2.12 Codex CLI Provider — Frozen Contract
        marked Current."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "### 2.12")
        self.assertIsNotNone(
            section,
            "ADR must contain ### 2.12 Codex CLI Provider frozen contract",
        )
        heading_m = re.search(
            r"^### 2\.12\s.*$", adr_text, re.MULTILINE,
        )
        self.assertIsNotNone(heading_m, "ADR must have a §2.12 heading")
        heading = heading_m.group(0)
        self.assertIn(
            "Current",
            heading,
            f"§2.12 heading must say Current, got: {heading.strip()!r}",
        )
        self.assertIn(
            "TC-13.8.4",
            heading,
            f"§2.12 heading must reference TC-13.8.4, got: {heading.strip()!r}",
        )

    def test_tc1383_type_name_is_codex_cli_provider(self) -> None:
        """§2.12.11 must freeze type name as CodexCliProvider."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.12.11")
        self.assertIsNotNone(section, "ADR must contain §2.12.11")
        self.assertIn("CodexCliProvider", section)
        self.assertNotIn("CodexCodeProvider", section)

    def test_tc1383_exact_three_fields(self) -> None:
        """§2.12.11 must freeze exactly three fields:
        provider_id, executable, sandbox_mode."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.12.11")
        self.assertIsNotNone(section)
        self.assertIn("exactly three", section.lower())
        for field in ("provider_id", "executable", "sandbox_mode"):
            self.assertIn(
                f"`{field}`",
                section,
                f"§2.12.11 must contain field {field!r}",
            )

    def test_tc1383_all_exact_one_symbol(self) -> None:
        """§2.12.11 must freeze __all__ = ['CodexCliProvider']."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.12.11")
        self.assertIsNotNone(section)
        self.assertIn(
            '__all__ = ["CodexCliProvider"]',
            section,
            "§2.12.11 must freeze __all__ with only CodexCliProvider",
        )

    def test_tc1383_provider_id_only_codex(self) -> None:
        """§2.12.2 must state provider_id is 'codex' only; all other
        values are rejected."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.12.2")
        self.assertIsNotNone(section, "ADR must contain §2.12.2")
        self.assertIn('"codex"', section)
        for forbidden in (
            "openai",
            "openai-codex",
            "codexcli",
            "Codex",
            "CODEX",
            "claude",
        ):
            self.assertIn(
                forbidden,
                section,
                f"§2.12.2 must explicitly forbid provider_id {forbidden!r}",
            )

    def test_tc1383_no_universal_cli_provider(self) -> None:
        """§2.12 must not define UniversalCliProvider; architecture is
        ClaudeCodeProvider + CodexCliProvider in parallel."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        self.assertNotIn(
            "UniversalCliProvider",
            adr_text,
            "ADR must not define UniversalCliProvider",
        )

    def test_tc1383_prompt_stdin_only_utf8(self) -> None:
        """§2.12.3 must state prompt is transmitted exclusively via
        stdin with UTF-8 strict encoding, no BOM."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.12.3")
        self.assertIsNotNone(section, "ADR must contain §2.12.3")
        self.assertIn('request.prompt.encode("utf-8")', section)
        self.assertIn("UTF-8", section)
        self.assertIn("BOM", section)

    def test_tc1383_no_fixed_control_prompt(self) -> None:
        """§2.12.3 must state no fixed control prompt is used — unlike
        Claude Provider."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.12.3")
        self.assertIsNotNone(section)
        # Must state that no control prompt is required.
        target = section.lower()
        self.assertTrue(
            "no fixed control prompt" in target
            or "no control prompt" in target
            or "no compile-time control string" in target,
            "§2.12.3 must explicitly state no fixed control prompt is used",
        )

    def test_tc1383_workspace_is_gateway_cwd_only(self) -> None:
        """§2.12.1 must state cwd is Gateway responsibility; Provider has
        no cwd field."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.12.1")
        self.assertIsNotNone(section, "ADR must contain §2.12.1")
        target = section.lower()
        self.assertTrue(
            "not have a `cwd` field" in target
            or "no ``cwd`` field" in target
            or "must not have a cwd field" in target
            or "must not have a `cwd` field" in target
            or "does **not**:\n\n* launch subprocesses" in section,
            "§2.12.1 must state Provider has no cwd field",
        )
        # Must forbid -C / --cd
        self.assertIn("-C", section)
        self.assertIn("--cd", section.lower())

    def test_tc1383_sandbox_safe_set_exact(self) -> None:
        """§2.12.7 must allow only read-only and workspace-write;
        danger-full-access must be forbidden."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.12.7")
        self.assertIsNotNone(section, "ADR must contain §2.12.7")
        self.assertIn("read-only", section)
        self.assertIn("workspace-write", section)
        self.assertIn("danger-full-access", section)

    def test_tc1383_danger_full_access_forbidden(self) -> None:
        """§2.12.7 must explicitly forbid danger-full-access."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.12.7")
        self.assertIsNotNone(section)
        target = section.lower()
        self.assertTrue(
            "forbidden" in target or "must not" in target
            or "never" in target,
            "§2.12.7 must forbid danger-full-access",
        )

    def test_tc1383_approval_fixed_never(self) -> None:
        """§2.12.8 must hard-code approval policy as 'never';
        it must not be a provider field."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.12.8")
        self.assertIsNotNone(section, "ADR must contain §2.12.8")
        self.assertIn("never", section)
        self.assertIn("hard-coded", section)

    def test_tc1383_approval_before_exec(self) -> None:
        """§2.12.4 must state --ask-for-approval never must appear
        before exec."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.12.4")
        self.assertIsNotNone(section, "ADR must contain §2.12.4")
        target = section.lower()
        self.assertTrue(
            "before `exec`" in target
            or "before exec" in target
            or "must appear **before**" in section,
            "§2.12.4 must state --ask-for-approval never precedes exec",
        )

    def test_tc1383_ephemeral_present(self) -> None:
        """§2.12.4 argv must include --ephemeral."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.12.4")
        self.assertIsNotNone(section)
        self.assertIn("--ephemeral", section)

    def test_tc1383_no_zero_file_io_claim(self) -> None:
        """§2.12.9 must NOT claim zero file I/O; only 'session files
        are not persisted'."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.12.9")
        self.assertIsNotNone(section, "ADR must contain §2.12.9")
        target = section.lower()
        # Must explicitly state it does NOT promise zero file I/O.
        self.assertIn(
            "does **not** promise zero file i/o",
            target,
            "§2.12.9 must explicitly state it does NOT promise zero file I/O",
        )
        # Must mention auth/cache/runtime caveat.
        target = section.lower()
        self.assertTrue(
            "authentication" in target
            or "cache" in target
            or "runtime data" in target,
            "§2.12.9 must mention auth/cache/runtime data caveat",
        )

    def test_tc1383_no_ignore_rules(self) -> None:
        """§2.12.10 must state --ignore-rules is NOT provided."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.12.10")
        self.assertIsNotNone(section, "ADR must contain §2.12.10")
        self.assertIn("--ignore-rules", section)

    def test_tc1383_no_arbitrary_config_or_profile(self) -> None:
        """§2.12.10 must forbid arbitrary -c, --profile, --enable,
        --disable overrides from callers."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.12.10")
        self.assertIsNotNone(section)
        self.assertIn("-c", section)
        self.assertIn("--profile", section)

    def test_tc1383_model_from_selected_model_id(self) -> None:
        """§2.12.5 must state --model value is from
        selected_model_id."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.12.5")
        self.assertIsNotNone(section, "ADR must contain §2.12.5")
        self.assertIn("selected_model_id", section)

    def test_tc1383_model_id_character_allowlist(self) -> None:
        """§2.12.4 must define strict character allowlist for model_id
        when .cmd shim is in use."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.12.4")
        self.assertIsNotNone(section)
        self.assertIn("allowlist", section.lower())

    def test_tc1383_effort_mapping_exact_three(self) -> None:
        """§2.12.6 must define exact mapping: efficient→low,
        balanced→medium, deep→high."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.12.6")
        self.assertIsNotNone(section, "ADR must contain §2.12.6")
        for src, dst in (("efficient", "low"), ("balanced", "medium"), ("deep", "high")):
            self.assertIn(
                f"`{src}`",
                section,
                f"§2.12.6 must map {src} → {dst}",
            )
            self.assertIn(
                f"`{dst}`",
                section,
                f"§2.12.6 must map {src} → {dst}",
            )

    def test_tc1383_xhigh_and_minimal_not_mapped(self) -> None:
        """§2.12.6 must state xhigh and minimal are NOT mapped."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.12.6")
        self.assertIsNotNone(section)
        self.assertIn("xhigh", section)
        self.assertIn("minimal", section)

    def test_tc1383_json_and_color_never_present(self) -> None:
        """§2.12.4 argv must include --json and --color never."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.12.4")
        self.assertIsNotNone(section)
        self.assertIn("--json", section)
        self.assertIn("--color", section)

    def test_tc1383_stdout_opaque_bytes(self) -> None:
        """§2.12.13 must state stdout/stderr remain opaque bytes;
        JSONL parsing is TC-13.9."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.12.13")
        self.assertIsNotNone(section, "ADR must contain §2.12.13")
        self.assertIn("opaque", section.lower())

    def test_tc1383_no_third_party_event_types_frozen(self) -> None:
        """§2.12.13 must state no third-party-inferred event type names
        are frozen."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.12.13")
        self.assertIsNotNone(section)
        target = section.lower()
        self.assertTrue(
            "not freeze" in target
            or "third-party" in target
            or "not invent" in target
            or "schema version" in target,
            "§2.12.13 must forbid freezing third-party event types",
        )

    def test_tc1383_no_output_schema_or_last_message(self) -> None:
        """§2.12.14 must permanently forbid --output-schema,
        --output-last-message, -o."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.12.14")
        self.assertIsNotNone(section, "ADR must contain §2.12.14")
        for flag in ("--output-schema", "--output-last-message", "-o"):
            self.assertIn(flag, section)

    def test_tc1383_env_overrides_empty(self) -> None:
        """§2.12.12 must freeze env_overrides == ()."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.12.12")
        self.assertIsNotNone(section, "ADR must contain §2.12.12")
        self.assertIn("env_overrides == ()", section)

    def test_tc1383_dangerous_flags_forbidden(self) -> None:
        """§2.12.15 must forbid the complete set of dangerous flags."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.12.15")
        self.assertIsNotNone(section, "ADR must contain §2.12.15")
        required_flags = (
            "--dangerously-bypass-approvals-and-sandbox",
            "--dangerously-bypass-hook-trust",
            "--sandbox danger-full-access",
            "--search",
            "--oss",
            "--local-provider",
            "--remote",
            "--add-dir",
            "--skip-git-repo-check",
            "-C",
            "--ignore-rules",
        )
        for flag in required_flags:
            self.assertIn(
                flag,
                section,
                f"§2.12.15 must forbid {flag}",
            )

    def test_tc1383_exact_argv_order(self) -> None:
        """§2.12.4 must freeze the exact argv tuple order."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.12.4")
        self.assertIsNotNone(section)
        # Check key ordering elements appear in the code block.
        code_m = re.search(r"```python\n(.*?)```", section, re.DOTALL)
        self.assertIsNotNone(code_m, "§2.12.4 must contain a Python code block")
        code = code_m.group(1)
        self.assertIn("--ask-for-approval", code)
        self.assertIn('"exec"', code)
        self.assertIn("--ephemeral", code)
        self.assertIn("--json", code)
        self.assertIn('"never"', code)
        self.assertIn("--model", code)
        self.assertIn("--sandbox", code)
        self.assertIn("-c", code)
        self.assertIn('"-"', code)

    def test_tc1383_stdin_marker_last(self) -> None:
        """§2.12.4 must place the stdin marker '-' as the last argv
        element."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.12.4")
        self.assertIsNotNone(section)
        target = section.lower()
        self.assertTrue(
            "must be last" in target or "last" in target,
            "§2.12.4 must state stdin marker '-' is last",
        )

    def test_tc1383_executable_separate_from_argv(self) -> None:
        """§2.12.4 must state executable is NOT in argv."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.12.4")
        self.assertIsNotNone(section)
        target = section.lower()
        self.assertTrue(
            "does not appear in `argv`" in target
            or "executable does not" in target,
            "§2.12.4 must state executable is not in argv",
        )

    def test_tc1383_cmd_shim_described_as_batch_not_pe(self) -> None:
        """§2.12.4 must describe .cmd as a batch-file shim, not a PE
        binary."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.12.4")
        self.assertIsNotNone(section)
        target = section.lower()
        self.assertTrue(
            "batch" in target or "shim" in target,
            "§2.12.4 must describe .cmd as batch-file shim",
        )

    def test_tc1383_no_claim_no_command_processor(self) -> None:
        """§2.12.4 must NOT claim the command processor is fully
        bypassed on Windows."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.12.4")
        self.assertIsNotNone(section)
        target = section.lower()
        self.assertTrue(
            "does not claim" in target
            or "not claim" in target
            or "the contract does **not** claim" in section,
            "§2.12.4 must caveat Windows command-processor behaviour",
        )

    def test_tc1383_codex_production_file_now_exists(self) -> None:
        """codex_cli_provider.py must now exist — TC-13.8.4 is Current."""
        self.assertTrue(
            (SKILL_ROOT / "scripts" / "codex_cli_provider.py").is_file(),
            "codex_cli_provider.py must exist — TC-13.8.4 is Current",
        )

    def test_tc1383_codex_test_file_exists(self) -> None:
        """test_codex_cli_provider.py must now exist."""
        self.assertTrue(
            (REPO_ROOT / "tests" / "test_codex_cli_provider.py").is_file(),
            "test_codex_cli_provider.py must exist",
        )

    def test_tc1383_module_importable(self) -> None:
        """codex_cli_provider module must be importable."""
        import sys as _sys
        _sys.path.insert(0, str(SKILL_ROOT / "scripts"))
        try:
            import codex_cli_provider as _ccp2  # noqa: F811
            self.assertIsNotNone(_ccp2)
            self.assertTrue(hasattr(_ccp2, "CodexCliProvider"))
        finally:
            _sys.path.pop(0)

    def test_tc1383_codex_cli_provider_exact_three_fields_runtime(self) -> None:
        """Runtime CodexCliProvider must have exactly three fields."""
        from dataclasses import fields as _fields
        import sys as _sys
        _sys.path.insert(0, str(SKILL_ROOT / "scripts"))
        try:
            import codex_cli_provider as _ccp2
            field_names = {f.name for f in _fields(_ccp2.CodexCliProvider)}
            self.assertSetEqual(
                field_names,
                {"provider_id", "executable", "sandbox_mode"},
            )
        finally:
            _sys.path.pop(0)

    def test_tc1383_approval_not_a_field_runtime(self) -> None:
        """approval_policy must NOT be a dataclass field on
        CodexCliProvider."""
        from dataclasses import fields as _fields
        import sys as _sys
        _sys.path.insert(0, str(SKILL_ROOT / "scripts"))
        try:
            import codex_cli_provider as _ccp2
            field_names = {f.name for f in _fields(_ccp2.CodexCliProvider)}
            self.assertNotIn("approval_policy", field_names)
        finally:
            _sys.path.pop(0)

    def test_tc1383_exact_argv_at_runtime(self) -> None:
        """Runtime argv must match the frozen contract exactly.

        Runs in an isolated subprocess so that module identity cannot
        be affected by test ordering — no sys.modules deletion."""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                ("import sys; sys.path.insert(0, r'{}'); "
                 "import dispatcher_gateway as dg; "
                 "import codex_cli_provider as ccp; "
                 "from pathlib import Path; "
                 "import tempfile; "
                 "snap = dg.ModelSelectionSnapshot.from_mapping({{"
                 "\"required_model_tier\": \"standard\", "
                 "\"required_model_capabilities\": [\"read\", \"write\"], "
                 "\"model_binding_id\": \"bind-1\", "
                 "\"selected_model_provider\": \"codex\", "
                 "\"selected_model_id\": \"gpt-5\", "
                 "\"selected_model_tier\": \"standard\", "
                 "\"selected_deliberation_tier\": \"balanced\", "
                 "\"selected_context_window_tokens\": 200000, "
                 "\"selected_model_capabilities\": [\"read\", \"write\"], "
                 "\"model_degradation_approval_id\": None}}); "
                 "req = dg.DispatchRequest("
                 "identity=dg.DispatchIdentity(task_id='TC-001', revision=1, attempt=1, dispatch_id='DSP-001'), "
                 "workspace=Path(tempfile.gettempdir()), "
                 "prompt='test prompt', "
                 "model_selection=snap, "
                 "timeout_seconds=30); "
                 "p = ccp.CodexCliProvider(provider_id='codex', executable='codex', sandbox_mode='workspace-write'); "
                 "inv = p.build_invocation(req); "
                 "expected = ("
                 "'--ask-for-approval', 'never', 'exec', '--ephemeral', '--json', "
                 "'--color', 'never', '--model', 'gpt-5', '--sandbox', 'workspace-write', "
                 "'-c', 'model_reasoning_effort=\"medium\"', '-'); "
                 "assert inv.argv == expected, f'argv mismatch: {{inv.argv}}'; "
                 "assert inv.env_overrides == (), f'env mismatch: {{inv.env_overrides}}'; "
                 "assert inv.executable == 'codex'; "
                 "print('ARGV_OK')").format(str(SKILL_ROOT / "scripts")),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0,
                         f"Subprocess failed: stderr={result.stderr}")
        self.assertIn("ARGV_OK", result.stdout)
        self.assertEqual(result.stderr.strip(), "",
                         f"Subprocess stderr: {result.stderr}")

    def test_tc1383_claude_provider_still_current(self) -> None:
        """§2.11 and Interface Status #30 must still be Current."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        heading_m = re.search(
            r"^### 2\.11\s.*$", adr_text, re.MULTILINE,
        )
        self.assertIsNotNone(heading_m)
        self.assertIn("Current", heading_m.group(0))

    def test_tc1383_tc139b_now_current(self) -> None:
        """TC-13.9b must now be Current (production module exists)."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_interface_status_table(adr_text)
        tc139b_row = None
        for row in rows:
            impl = self._resolve_col(row, "Implemented by", "Impl", "Notes")
            if "TC-13.9b" in re.findall(r"\bTC-\d+\.\d+[a-z]?\b", impl):
                tc139b_row = row
                break
        self.assertIsNotNone(tc139b_row, "ADR must contain TC-13.9b row")
        status = self._resolve_col(tc139b_row, "Status")
        self.assertIn("Current", status)
        self.assertNotIn("Target", status)

    def test_tc1383_future_task_cards_has_1383_and_1384(self) -> None:
        """ADR §5 Future Task Cards must include TC-13.8.3 and
        TC-13.8.4."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_future_task_cards_table(adr_text)
        task_ids_seen = {
            self._resolve_col(row, "Task Card", "Task", "#")
            for row in rows
        }
        for tid in ("TC-13.8.3", "TC-13.8.4"):
            self.assertIn(
                tid,
                task_ids_seen,
                f"ADR §5 Future Task Cards must contain {tid}",
            )

    def test_tc1383_tc139a_depends_on_1384(self) -> None:
        """ADR §5: TC-13.9a depends on must include TC-13.8.4."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_future_task_cards_table(adr_text)
        by_id = {
            self._resolve_col(row, "Task Card", "Task", "#"): row
            for row in rows
        }
        tc139a = by_id.get("TC-13.9a")
        self.assertIsNotNone(tc139a, "TC-13.9a must exist in Future Task Cards")
        tc139a_dep = self._resolve_col(tc139a, "Depends on", "Dep")
        self.assertIn(
            "TC-13.8.4",
            tc139a_dep,
            "TC-13.9a must depend on TC-13.8.4",
        )


    # ═════════════════════════════════════════════════════════════════════
    # TC-13.9a: WorkerAdapter Core Contract smoke tests (36 checks)
    # ═════════════════════════════════════════════════════════════════════

    def test_tc139a_interface_row_14_no_longer_claims_four_tier_slots(self) -> None:
        """Interface #14 description must NOT claim 'four-tier slots'."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_interface_status_table(adr_text)
        row_14 = None
        for row in rows:
            if self._resolve_col(row, "#") == "14":
                row_14 = row
                break
        self.assertIsNotNone(row_14, "Interface Status must have row #14")
        notes = self._resolve_col(row_14, "Notes")
        self.assertNotIn("four-tier slots", notes,
                         "Interface #14 must no longer claim 'four-tier slots'")
        self.assertIn("concurrency slots deferred to TC-13.10", notes,
                      "Interface #14 must clarify concurrency slots belong to TC-13.10")

    def test_tc139a_interface_row_14_now_current(self) -> None:
        """Interface #14 must now be Current (TC-13.9b)."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_interface_status_table(adr_text)
        row_14 = None
        for row in rows:
            if self._resolve_col(row, "#") == "14":
                row_14 = row
                break
        self.assertIsNotNone(row_14)
        status = self._resolve_col(row_14, "Status")
        self.assertIn("Current", status)
        self.assertNotIn("Target", status)
        impl = self._resolve_col(row_14, "Implemented by", "Impl", "Notes")
        self.assertIn("TC-13.9b", impl,
                      f"Interface #14 must reference TC-13.9b, got: {impl}")

    def test_tc139a_section_213_exists_and_is_current(self) -> None:
        """§2.13 must exist and be marked Current (TC-13.9b)."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        heading_m = re.search(
            r"^### 2\.13\s.*$", adr_text, re.MULTILINE,
        )
        self.assertIsNotNone(heading_m, "ADR must have a §2.13 heading")
        heading = heading_m.group(0)
        self.assertIn("Current", heading,
                      f"§2.13 heading must say Current, got: {heading.strip()!r}")
        self.assertIn("TC-13.9b", heading,
                      f"§2.13 heading must reference TC-13.9b, got: {heading.strip()!r}")
        self.assertNotIn("Target", heading,
                         f"§2.13 heading must NOT say Target, got: {heading.strip()!r}")

    def test_tc139a_run_worker_four_exact_params(self) -> None:
        """§2.13.2 must define run_worker with exactly four parameters."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.13.2")
        self.assertIsNotNone(section, "ADR must contain §2.13.2 Public Entry Point")
        # All four parameter names must appear.
        for param in ("request", "worker_kind", "task_difficulty", "providers"):
            self.assertIn(f"`{param}`", section,
                          f"§2.13.2 must document parameter '{param}'")
        # worker_kind is WorkerKind, task_difficulty is TaskDifficulty
        self.assertIn("WorkerKind", section)
        self.assertIn("TaskDifficulty", section)
        self.assertIn("Mapping[str, AgentCliProvider]", section)

    def test_tc139a_worker_result_exact_four_fields(self) -> None:
        """§2.13.3 must define WorkerResult with exactly four fields."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.13.3")
        self.assertIsNotNone(section, "ADR must contain §2.13.3 WorkerResult")
        for field in ("worker_kind", "task_difficulty", "budget", "dispatch_result"):
            self.assertIn(f"`{field}`", section,
                          f"§2.13.3 must document field '{field}'")

    def test_tc139a_worker_result_forbids_output_fields(self) -> None:
        """§2.13.3 must explicitly exclude final_text, output, events,
        executor_model, retry, slot, lease, report."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.13.3")
        self.assertIsNotNone(section)
        for forbidden in ("final_text", "output", "events", "executor_model",
                          "retry", "slot", "lease", "report"):
            self.assertIn(forbidden, section,
                          f"§2.13.3 must exclude '{forbidden}'")

    def test_tc139a_worker_kind_task_difficulty_independent_inputs(self) -> None:
        """§2.13.1 must declare WorkerKind and TaskDifficulty as
        independent inputs."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.13.1")
        self.assertIsNotNone(section, "ADR must contain §2.13.1")
        self.assertIn("independent", section.lower(),
                      "§2.13.1 must declare inputs as independent")

    def test_tc139a_no_worker_kind_to_difficulty_mapping(self) -> None:
        """§2.13.1 must forbid a WorkerKind→TaskDifficulty mapping table."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.13.1")
        self.assertIsNotNone(section)
        self.assertIn("no", section.lower(),
                      "§2.13.1 must forbid mapping (contain 'no')")
        self.assertIn("one-to-one", section.lower(),
                      "§2.13.1 must forbid one-to-one mapping")

    def test_tc139a_no_string_manipulation_derivation(self) -> None:
        """§2.13.1 must forbid deriving difficulty via _agent stripping or
        enum-value casting."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.13.1")
        self.assertIsNotNone(section)
        self.assertIn("_agent", section,
                      "§2.13.1 must explicitly forbid _agent stripping")

    def test_tc139a_budget_uses_explicit_task_difficulty(self) -> None:
        """§2.13.4 must show budget computed from task_difficulty, not
        derived from worker_kind."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.13.4")
        self.assertIsNotNone(section, "ADR must contain §2.13.4 Execution Order")
        self.assertIn("compute_budget", section)
        self.assertIn("task_difficulty", section)

    def test_tc139a_context_window_only_from_snapshot(self) -> None:
        """§2.13.5 must state context_window_tokens sourced exclusively
        from selected_context_window_tokens."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.13.5")
        self.assertIsNotNone(section, "ADR must contain §2.13.5 Budget Semantics")
        self.assertIn("selected_context_window_tokens", section)

    def test_tc139a_budget_before_dispatch(self) -> None:
        """§2.13.4 must mandate budget computed before Gateway dispatch."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.13.4")
        self.assertIsNotNone(section)
        self.assertIn("before", section.lower(),
                      "§2.13.4 must state budget is computed before dispatch")
        self.assertIn("must not be called", section.lower(),
                      "§2.13.4 must state Gateway must not be called on budget failure")

    def test_tc139a_budget_failure_no_dispatch(self) -> None:
        """§2.13.4 must state budget failure prevents Gateway call."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.13.4")
        self.assertIsNotNone(section)
        self.assertTrue(
            "propagate" in section.lower() or "must not be called" in section.lower(),
            "§2.13.4 must state budget failure propagates before Gateway call",
        )

    def test_tc139a_run_dispatch_exactly_once(self) -> None:
        """§2.13.4 must state run_dispatch called exactly once."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.13.4")
        self.assertIsNotNone(section)
        self.assertIn("exactly once", section,
                      "§2.13.4 must state run_dispatch is called exactly once")

    def test_tc139a_budget_informational_only(self) -> None:
        """§2.13.5 must declare budget is informational — not enforced."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.13.5")
        self.assertIsNotNone(section)
        self.assertIn("informational", section.lower(),
                      "§2.13.5 must state budget is informational only")
        self.assertTrue(
            "does not enforce" in section.lower() or "does **not** enforce" in section,
            "§2.13.5 must state budget is not enforced",
        )

    def test_tc139a_no_prompt_modification(self) -> None:
        """§2.13.5 must forbid prompt modification, truncation, or
        injection."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.13.5")
        self.assertIsNotNone(section)
        for term in ("does not modify", "not truncate", "not inject"):
            self.assertTrue(
                term in section.lower()
                or term.replace("not ", "not **") in section
                or term.replace("not ", "not **") in section.lower()
                or "modify" in section.lower(),  # at least one must match
                f"§2.13.5 must contain '{term}'",
            )

    def test_tc139a_no_token_estimation_char_approx(self) -> None:
        """§2.13.5 must forbid character-count approximations as token
        counts."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.13.5")
        self.assertIsNotNone(section)
        self.assertTrue(
            "character" in section.lower() or "char" in section.lower(),
            "§2.13.5 must mention character-count approximation prohibition",
        )

    def test_tc139a_no_cli_budget_flag(self) -> None:
        """§2.13.5 must state no CLI budget flag is added."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.13.5")
        self.assertIsNotNone(section)
        self.assertIn("CLI budget flag", section,
                      "§2.13.5 must mention no CLI budget flag")

    def test_tc139a_no_token_enforcement_claim(self) -> None:
        """§2.13.5 bold caveat must state 'does not enforce the token
        limit'."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.13.5")
        self.assertIsNotNone(section)
        self.assertTrue(
            "does not enforce the token limit" in section
            or "does **not** enforce" in section,
            "§2.13.5 must declare token limit is not enforced",
        )

    def test_tc139a_no_claude_json_parsing_in_core(self) -> None:
        """§2.13.6 must forbid Claude JSON parsing in the core module."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.13.6")
        self.assertIsNotNone(section, "ADR must contain §2.13.6 Output Boundary")
        self.assertIn("No Claude JSON", section,
                      "§2.13.6 must forbid Claude JSON parsing")

    def test_tc139a_no_codex_jsonl_parsing_in_core(self) -> None:
        """§2.13.6 must forbid Codex JSONL parsing in the core module."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.13.6")
        self.assertIsNotNone(section)
        self.assertIn("No Codex JSONL", section,
                      "§2.13.6 must forbid Codex JSONL parsing")

    def test_tc139a_no_worker_output_type_in_core(self) -> None:
        """§2.13.6 must forbid WorkerOutput type or decoder Protocol."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.13.6")
        self.assertIsNotNone(section)
        self.assertIn("WorkerOutput", section,
                      "§2.13.6 must mention WorkerOutput absence")
        self.assertIn("decoder", section.lower(),
                      "§2.13.6 must mention decoder Protocol absence")

    def test_tc139a_stdout_stderr_remain_opaque(self) -> None:
        """§2.13.6 must state stdout/stderr remain opaque bytes."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.13.6")
        self.assertIsNotNone(section)
        self.assertIn("opaque", section.lower(),
                      "§2.13.6 must state output remains opaque")

    def test_tc139a_gateway_exceptions_propagated_as_is(self) -> None:
        """§2.13.7 must state Gateway exceptions are propagated as-is."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.13.7")
        self.assertIsNotNone(section, "ADR must contain §2.13.7 Exception Semantics")
        self.assertIn("DispatchGatewayError", section,
                      "§2.13.7 must mention DispatchGatewayError propagation")

    def test_tc139a_no_parallel_exception_hierarchy_in_core(self) -> None:
        """§2.13.7 must state no parallel exception hierarchy in core."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.13.7")
        self.assertIsNotNone(section)
        self.assertTrue(
            "does not introduce" in section.lower() or "does **not** introduce" in section,
            "§2.13.7 must state no parallel exception hierarchy",
        )

    def test_tc139a_single_attempt(self) -> None:
        """§2.13.8 must state exactly one attempt — no retry."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.13.8")
        self.assertIsNotNone(section, "ADR must contain §2.13.8 Single-Attempt")
        self.assertIn("exactly one attempt", section,
                      "§2.13.8 must state exactly one attempt")
        self.assertIn("No retry", section,
                      "§2.13.8 must forbid retry")

    def test_tc139a_no_retry_escalation_rate_limit(self) -> None:
        """§2.13.8 must exclude retry, escalation, and rate-limit."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.13.8")
        self.assertIsNotNone(section)
        self.assertIn("TC-13.18", section,
                      "§2.13.8 must reference TC-13.18 for retry")
        self.assertIn("TC-13.13", section,
                      "§2.13.8 must reference TC-13.13 for escalation")
        self.assertIn("TC-13.14", section,
                      "§2.13.8 must reference TC-13.14 for rate limiting")

    def test_tc139a_no_slot_lease_fencing(self) -> None:
        """§2.13.9 must exclude slot, lease, and fencing."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.13.9")
        self.assertIsNotNone(section)
        self.assertIn("TC-13.10", section,
                      "§2.13.9 must reference TC-13.10")

    def test_tc139a_no_file_state_event_outbox_report_writes(self) -> None:
        """§2.13.10 must exclude all file/state/event/outbox/report
        writes."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.13.10")
        self.assertIsNotNone(section, "ADR must contain §2.13.10 State & Persistence")
        for forbidden in ("tasks.yaml", "events", "outbox", "delivery report",
                          "acceptance", "runtime files", "Git commands",
                          "worktree", "canonical state"):
            self.assertIn(forbidden, section,
                          f"§2.13.10 must forbid {forbidden}")
        self.assertIn("TC-13.11", section,
                      "§2.13.10 must reference TC-13.11")

    def test_tc139a_execution_boundary_not_state_transition(self) -> None:
        """§2.13.10 bold statement: execution-orchestration boundary,
        not state-transition boundary."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.13.10")
        self.assertIsNotNone(section)
        self.assertIn("execution-orchestration boundary", section,
                      "§2.13.10 must state 'execution-orchestration boundary'")
        self.assertIn("state-transition boundary", section,
                      "§2.13.10 must state 'not a state-transition boundary'")

    def test_tc139a_no_executor_model_construction(self) -> None:
        """§2.13.11 must state core does NOT construct executor_model."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.13.11")
        self.assertIsNotNone(section, "ADR must contain §2.13.11 executor_model")
        self.assertTrue(
            "does not construct" in section.lower() or "does **not** construct" in section,
            "§2.13.11 must state core does not construct executor_model",
        )

    def test_tc139a_cross_section_no_budget_or_worker_fields(self) -> None:
        """§2.13.11 must forbid budget/worker_kind/task_difficulty fields
        from executor_model."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.13.11")
        self.assertIsNotNone(section)
        for forbidden in ("worker_kind", "task_difficulty", "budget_percent",
                          "budget_cap_tokens", "budget_tokens", "reserved_tokens",
                          "stdout", "stderr", "provider_output"):
            self.assertIn(forbidden, section,
                          f"§2.13.11 must forbid '{forbidden}' in executor_model")

    def test_tc139a_worker_result_frozen_slots(self) -> None:
        """§2.13.12 must state WorkerResult is frozen/slots."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.13.12")
        self.assertIsNotNone(section)
        self.assertIn("frozen", section.lower(),
                      "§2.13.12 must state WorkerResult is frozen")
        self.assertIn("slots", section.lower(),
                      "§2.13.12 must state WorkerResult uses slots")

    def test_tc139a_all_exports_exactly_two_symbols(self) -> None:
        """§2.13.3 must declare __all__ = ['WorkerResult', 'run_worker']."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.13.3")
        self.assertIsNotNone(section)
        self.assertIn("WorkerResult", section)
        self.assertIn("run_worker", section)
        self.assertIn("__all__", section,
                      "§2.13.3 must declare __all__ with exactly two symbols")

    def test_tc139b_worker_adapter_py_exists(self) -> None:
        """worker_adapter.py must now exist — TC-13.9b is Current."""
        self.assertTrue(
            (SKILL_ROOT / "scripts" / "worker_adapter.py").is_file(),
            "worker_adapter.py must exist — TC-13.9b is Current",
        )

    def test_tc139a_future_task_cards_has_139a_139b_139c(self) -> None:
        """ADR §5 must contain TC-13.9a, TC-13.9b, TC-13.9c."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_future_task_cards_table(adr_text)
        task_ids_seen = {
            self._resolve_col(row, "Task Card", "Task", "#")
            for row in rows
        }
        for tid in ("TC-13.9a", "TC-13.9b", "TC-13.9c"):
            self.assertIn(tid, task_ids_seen,
                          f"ADR §5 must contain {tid}")

    def test_tc139a_tc1310_depends_on_tc139b_not_tc139c(self) -> None:
        """ADR §5: TC-13.10 depends on TC-13.9b, not TC-13.9c."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_future_task_cards_table(adr_text)
        by_id = {
            self._resolve_col(row, "Task Card", "Task", "#"): row
            for row in rows
        }
        tc1310 = by_id.get("TC-13.10")
        self.assertIsNotNone(tc1310, "TC-13.10 must exist in Future Task Cards")
        tc1310_dep = self._resolve_col(tc1310, "Depends on", "Dep")
        self.assertIn("TC-13.9b", tc1310_dep,
                      "TC-13.10 must depend on TC-13.9b")

    def test_tc139a_claude_provider_still_current(self) -> None:
        """§2.11 and Interface Status #30 must still be Current."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        heading_m = re.search(
            r"^### 2\.11\s.*$", adr_text, re.MULTILINE,
        )
        self.assertIsNotNone(heading_m)
        self.assertIn("Current", heading_m.group(0))

    def test_tc139a_codex_provider_still_current(self) -> None:
        """§2.12 and Interface Status #31 must still be Current."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        heading_m = re.search(
            r"^### 2\.12\s.*$", adr_text, re.MULTILINE,
        )
        self.assertIsNotNone(heading_m)
        self.assertIn("Current", heading_m.group(0))

    def test_tc139a_tc1310_still_target(self) -> None:
        """Interface Status row for TC-13.10 must still be Target."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_interface_status_table(adr_text)
        tc1310_row = None
        for row in rows:
            impl = self._resolve_col(row, "Implemented by", "Impl", "Notes")
            if "TC-13.10" in re.findall(r"\b(TC-\d+(?:\.\d+)*)\b", impl):
                tc1310_row = row
                break
        self.assertIsNotNone(tc1310_row, "ADR must contain TC-13.10 row")
        status = self._resolve_col(tc1310_row, "Status")
        self.assertIn("Target", status)
        self.assertNotIn("Current", status)

    def test_tc139a_section_281_budget_semantics_fixed(self) -> None:
        """§2.8.1 must no longer claim TaskDifficulty does not dictate
        budget percentage."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.8.1")
        self.assertIsNotNone(section)
        # Must now state TaskDifficulty is the primary input to budget.
        self.assertIn("ContextBudgetPolicy", section,
                      "§2.8.1 must reference ContextBudgetPolicy")
        self.assertIn("TC-13.5.1", section,
                      "§2.8.1 must reference TC-13.5.1")
        # Must NOT contain the old conflicting claim.
        self.assertNotIn("which percentage budget to apply",
                         section,
                         "§2.8.1 must not claim TaskDifficulty does not dictate budget percentage")


if __name__ == "__main__":
    unittest.main()
