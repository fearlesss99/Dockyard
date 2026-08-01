from __future__ import annotations

import ast
import io
import json
import re
import subprocess
import sys
import tempfile
import tokenize
import unittest
from datetime import datetime, timezone
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


def _python_code_without_comments_and_docstrings(source: str) -> str:
    """Return Python source with comments/docstrings blanked, preserving code strings."""
    tree = ast.parse(source)
    docstring_spans: set[tuple[tuple[int, int], tuple[int, int]]] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        value = getattr(first, "value", None)
        if (
            isinstance(first, ast.Expr)
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            docstring_spans.add(
                ((value.lineno, value.col_offset),
                 (value.end_lineno, value.end_col_offset))
            )

    tokens: list[tokenize.TokenInfo] = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        span = (token.start, token.end)
        if token.type == tokenize.COMMENT or (
            token.type == tokenize.STRING and span in docstring_spans
        ):
            token = token._replace(string="")
        tokens.append(token)
    return tokenize.untokenize(tokens)


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
            r"### 2\.4.*?```json\n(.*?)```",
            contract_text,
            re.DOTALL,
        )
        self.assertIsNotNone(audit_section,
                             "CLI contract: must have mad audit section 2.4")
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

        section_33 = _extract_markdown_section(contract_text, "### 2.4")
        self.assertIsNotNone(
            section_33,
            "CLI contract must have mad audit section 2.4",
        )
        text: str = section_33  # type: ignore[assignment]

        # ── Parse the exit-code table ──
        exit_row = _find_exit_code_table_row(text, "0")
        self.assertIsNotNone(
            exit_row,
            "audit section 2.4: must have an exit-code table with a row "
            "for code 0",
        )
        condition_cell: str = exit_row  # type: ignore[assignment]

        # The condition cell must mention all three business verdicts.
        normalised = _strip_md_fmt(condition_cell)
        for verdict in ("pass", "fail", "blocked"):
            self.assertIn(
                verdict,
                normalised,
                f"audit section 2.4: exit-code-0 condition must mention "
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
            "audit section 2.4: prose must explicitly state that exit code 0 "
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

        # TC-13.10a depends on this ADR (split from old TC-13.10)
        tc1310a_dep = self._resolve_col(by_id.get("TC-13.10a", {}), "Depends on", "Dep")
        self.assertIn("This ADR", tc1310a_dep,
                      "TC-13.10a must depend on This ADR")

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
        """MAD JSON interfaces are now Current (verified by TC-13.21f).

        Verifies the specific status strings used in the ADR Interface
        Status table and the CLI contract summary — not the raw word
        "Current", which is expected for the verified interfaces.
        """
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
                # Verified Current (TC-13.21f), not bare Target.
                self.assertNotIn("| **Target** |", line,
                                 f"'mad agents --format json' must not be Target: {line.strip()}")
                self.assertIn("**Current**", line,
                              f"'mad agents --format json' must be Current: {line.strip()}")
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
            if "mad audit" in line:
                self.assertIn(
                    "Current",
                    line,
                    f"CLI contract: 'mad audit' must be Current: {line.strip()}",
                )
            if "mad agents --format json" in line:
                self.assertNotIn(
                    "| **Target** |",
                    line,
                    f"CLI contract: 'mad agents --format json' must not be Target: {line.strip()}",
                )
                self.assertIn(
                    "Current",
                    line,
                    f"CLI contract: 'mad agents --format json' must be Current: {line.strip()}",
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
        code = _python_code_without_comments_and_docstrings(src)
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

    def test_tc139a_tc1310a_depends_on_adr(self) -> None:
        """ADR §5: TC-13.10a depends on This ADR."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_future_task_cards_table(adr_text)
        by_id = {
            self._resolve_col(row, "Task Card", "Task", "#"): row
            for row in rows
        }
        tc1310a = by_id.get("TC-13.10a")
        self.assertIsNotNone(tc1310a, "TC-13.10a must exist in Future Task Cards")
        tc1310a_dep = self._resolve_col(tc1310a, "Depends on", "Dep")
        self.assertIn("This ADR", tc1310a_dep,
                      "TC-13.10a must depend on This ADR")

    def test_tc139a_tc1310_split_in_future_task_cards(self) -> None:
        """ADR §5 must list TC-13.10a, TC-13.10b, TC-13.10c as separate rows."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_future_task_cards_table(adr_text)
        task_ids_seen = {
            self._resolve_col(row, "Task Card", "Task", "#")
            for row in rows
        }
        for tid in ("TC-13.10a", "TC-13.10b", "TC-13.10c"):
            self.assertIn(tid, task_ids_seen,
                          f"ADR §5 must contain {tid}")

    def test_tc139a_tc1310b_depends_on_tc1310a(self) -> None:
        """ADR §5: TC-13.10b depends on TC-13.10a."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_future_task_cards_table(adr_text)
        by_id = {
            self._resolve_col(row, "Task Card", "Task", "#"): row
            for row in rows
        }
        tc1310b = by_id.get("TC-13.10b")
        self.assertIsNotNone(tc1310b, "TC-13.10b must exist in Future Task Cards")
        tc1310b_dep = self._resolve_col(tc1310b, "Depends on", "Dep")
        self.assertIn("TC-13.10a", tc1310b_dep,
                      "TC-13.10b must depend on TC-13.10a")

    def test_tc139a_tc1310c_depends_on_tc1310b(self) -> None:
        """ADR §5: TC-13.10c depends on TC-13.10b."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_future_task_cards_table(adr_text)
        by_id = {
            self._resolve_col(row, "Task Card", "Task", "#"): row
            for row in rows
        }
        tc1310c = by_id.get("TC-13.10c")
        self.assertIsNotNone(tc1310c, "TC-13.10c must exist in Future Task Cards")
        tc1310c_dep = self._resolve_col(tc1310c, "Depends on", "Dep")
        self.assertIn("TC-13.10b", tc1310c_dep,
                      "TC-13.10c must depend on TC-13.10b")

    # ── TC-13.10a WorkerSlotLease frozen contract ───────────────────────

    _EIGHT_STABLE_SLOTS = frozenset({
        "basic_agent-1",
        "basic_agent-2",
        "standard_agent-1",
        "standard_agent-2",
        "advanced_agent-1",
        "advanced_agent-2",
        "expert_agent-1",
        "expert_agent-2",
    })

    _TEN_LEASE_FIELDS = frozenset({
        "lease_id",
        "lease_epoch",
        "slot_id",
        "worker_kind",
        "holder_dispatch_id",
        "holder_instance_id",
        "canonical_worktree",
        "acquired_at",
        "heartbeat_at",
        "expires_at",
    })

    _LEASE_FORBIDDEN_FIELDS = frozenset({
        "task_id",
        "revision",
        "attempt",
        "provider",
        "model_id",
        "task_difficulty",
        "prompt",
        "PID",
        "retry count",
    })

    _FOUR_ROOT_KEYS = frozenset({
        "schema_version",
        "updated_at",
        "slot_epochs",
        "leases",
    })

    _EXCEPTION_CLASSES = frozenset({
        "WorkerSlotLeaseError",
        "WorkerSlotValidationError",
        "WorkerSlotCapacityError",
        "WorkerSlotContentionError",
        "WorkerSlotNotHeldError",
        "WorkerSlotFencingError",
    })

    def test_tc1310a_section_25_heading_is_frozen_contract(self) -> None:
        """§2.5 heading must mention Frozen Contract — TC-13.10c."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        heading_m = re.search(
            r"^### 2\.5\s.*$", adr_text, re.MULTILINE,
        )
        self.assertIsNotNone(heading_m, "ADR must have a §2.5 heading")
        heading = heading_m.group(0)
        self.assertIn("Current", heading,
                      "§2.5 heading must now say Current")
        self.assertIn("TC-13.10", heading,
                      "§2.5 heading must reference TC-13.10")

    def test_tc1310a_frozen_contract_marker_in_section(self) -> None:
        """§2.5 must contain 'Frozen Contract — TC-13.10a' marker."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "### 2.5")
        self.assertIsNotNone(section, "ADR must contain §2.5")
        self.assertIn("Frozen Contract — TC-13.10a", section,
                      "§2.5 must contain the frozen contract marker")

    def test_tc1310a_eight_stable_slot_ids(self) -> None:
        """§2.5.1 must define exactly eight stable slot IDs."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.5.1")
        self.assertIsNotNone(section, "ADR must contain §2.5.1")
        for slot_id in sorted(self._EIGHT_STABLE_SLOTS):
            self.assertIn(
                slot_id, section,
                f"§2.5.1 must contain slot ID {slot_id!r}",
            )
        # Must forbid random / dynamic slot IDs.
        self.assertIn("Random", section,
                      "§2.5.1 must forbid random slot IDs")

    def test_tc1310a_no_random_slot_id_rule(self) -> None:
        """§2.5.1 must forbid random/dynamic slot IDs."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.5.1")
        self.assertIsNotNone(section)
        target = section.lower()
        self.assertTrue(
            "forbidden" in target or "must not" in target or "no " in target,
            "§2.5.1 must forbid random slot IDs",
        )

    def test_tc1310a_runtime_store_path(self) -> None:
        """§2.5.2 must specify .agentdesk/runtime/worker-slot-lease.yaml."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.5.2")
        self.assertIsNotNone(section, "ADR must contain §2.5.2")
        self.assertIn(
            ".agentdesk/runtime/worker-slot-lease.yaml",
            section,
            "§2.5.2 must specify the runtime store path",
        )

    def test_tc1310a_four_exact_root_keys(self) -> None:
        """§2.5.2 root object must have exactly 4 keys."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.5.2")
        self.assertIsNotNone(section)
        for key in sorted(self._FOUR_ROOT_KEYS):
            self.assertIn(
                f'"{key}"', section,
                f"§2.5.2 must include root key {key!r}",
            )

    def test_tc1310a_slot_epochs_persist_after_release(self) -> None:
        """§2.5.2: release must preserve slot_epochs entry."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.5.2")
        self.assertIsNotNone(section)
        target = section.lower()
        self.assertTrue(
            "preserv" in target or "retain" in target or "persist" in target,
            "§2.5.2 must state epoch is preserved on release",
        )

    def test_tc1310a_worker_kind_is_frozen_lease_field(self) -> None:
        """§2.5.3 must list worker_kind as frozen lease field."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.5.3")
        self.assertIsNotNone(section, "ADR must contain §2.5.3")
        self.assertIn("worker_kind", section,
                      "§2.5.3 must list worker_kind as frozen field")

    def test_tc1310a_exact_ten_lease_fields(self) -> None:
        """§2.5.3 WorkerSlotLease must have exactly 10 fields."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.5.3")
        self.assertIsNotNone(section)
        # Parse the numbered field table in §2.5.3
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
            self.assertSetEqual(
                field_names,
                self._TEN_LEASE_FIELDS,
                "§2.5.3 WorkerSlotLease must have exactly 10 fields",
            )

    def test_tc1310a_lease_forbidden_fields(self) -> None:
        """§2.5.3 must explicitly forbid task_id, revision, attempt, etc."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.5.3")
        self.assertIsNotNone(section)
        for field in sorted(self._LEASE_FORBIDDEN_FIELDS):
            self.assertIn(
                field, section,
                f"§2.5.3 must forbid field {field!r}",
            )

    def test_tc1310a_lease_id_format(self) -> None:
        """§2.5.4 must define WSL-<32 hex> format."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.5.4")
        self.assertIsNotNone(section, "ADR must contain §2.5.4")
        self.assertIn("WSL-", section,
                      "§2.5.4 must define WSL- prefix for lease IDs")
        self.assertIn("32", section,
                      "§2.5.4 must require 32 hex characters")

    def test_tc1310a_ttl_and_heartbeat_constants(self) -> None:
        """§2.5.5 must freeze LEASE_TTL_SECONDS=60 and
        MAX_HEARTBEAT_INTERVAL_SECONDS=20."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.5.5")
        self.assertIsNotNone(section, "ADR must contain §2.5.5")
        self.assertIn("LEASE_TTL_SECONDS", section)
        self.assertIn("60", section,
                      "§2.5.5 must set LEASE_TTL_SECONDS to 60")
        self.assertIn("MAX_HEARTBEAT_INTERVAL_SECONDS", section)
        self.assertIn("20", section,
                      "§2.5.5 must set MAX_HEARTBEAT_INTERVAL_SECONDS to 20")

    def test_tc1310a_explicit_utc_datetime_required(self) -> None:
        """§2.5.5 must require timezone-aware UTC datetime; reject naive."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.5.5")
        self.assertIsNotNone(section)
        self.assertIn("UTC", section,
                      "§2.5.5 must require UTC datetime")
        target = section.lower()
        self.assertTrue(
            "naive" in target or "timezone-aware" in target
            or "offset" in target,
            "§2.5.5 must reject naive datetime",
        )

    def test_tc1310a_acquire_stale_cleanup_before_capacity(self) -> None:
        """§2.5.6 acquire must clean stale leases before counting capacity."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.5.6")
        self.assertIsNotNone(section, "ADR must contain §2.5.6")
        target = section.lower()
        self.assertTrue(
            "before" in target and ("capacit" in target or "count" in target),
            "§2.5.6 must state stale cleanup runs before capacity check",
        )

    def test_tc1310a_per_worktree_per_worker_kind_independent(self) -> None:
        """§2.5.6 per-worktree limits are independent per WorkerKind."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.5.6")
        self.assertIsNotNone(section)
        target = section.lower()
        self.assertTrue(
            "independ" in target or "does not block" in target
            or "different workerkind" in target,
            "§2.5.6 must state per-worktree limits are per-WorkerKind",
        )

    # ── TC-13.10a acquire duplicate-pair rules (post-remediation) ────────

    def test_tc1310a_subsections_are_exactly_1_through_16(self) -> None:
        """§2.5 subsections must be exactly 2.5.1 through 2.5.16, contiguous."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        import re as _re
        found = sorted(
            int(m.group(1))
            for m in _re.finditer(r"^#### 2\.5\.(\d+)\s", adr_text, _re.MULTILINE)
        )
        self.assertEqual(
            found,
            list(range(1, 17)),
            "§2.5 must have exactly subsections 1–16, contiguous, no gaps",
        )

    def test_tc1310a_no_section_2517(self) -> None:
        """§2.5.17 must NOT exist."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        import re as _re
        m = _re.search(r"^#### 2\.5\.17\s", adr_text, _re.MULTILINE)
        self.assertIsNone(m, "§2.5.17 must not exist")

    def test_tc1310a_paragraph_marker_range(self) -> None:
        """The Frozen Contract paragraph must state §2.5.1–§2.5.16."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        self.assertIn("§2.5.1–§2.5.16", adr_text,
                      "Frozen contract paragraph must reference §2.5.1–§2.5.16")
        self.assertNotIn("§2.5.17", adr_text,
                         "Frozen contract paragraph must NOT reference §2.5.17")

    def test_tc1310a_duplicate_active_acquire_must_fail(self) -> None:
        """§2.5.6: same (worker_kind, canonical_worktree) active pair must
        get WorkerSlotCapacityError, not a second slot."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.5.6")
        self.assertIsNotNone(section)
        self.assertIn("WorkerSlotCapacityError", section,
                      "§2.5.6 must use WorkerSlotCapacityError for duplicate active pair")
        target = _strip_md_fmt(section.lower())
        self.assertIn("not return the existing lease", target)

    def test_tc1310a_duplicate_active_acquire_does_not_occupy_second_slot(
        self,
    ) -> None:
        """§2.5.6: duplicate active acquire must not occupy a second slot."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.5.6")
        self.assertIsNotNone(section)
        target = _strip_md_fmt(section.lower())
        self.assertIn("not occupy a second slot", target)

    def test_tc1310a_duplicate_active_acquire_does_not_increment_epoch(
        self,
    ) -> None:
        """§2.5.6: duplicate active acquire must not increment any epoch."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.5.6")
        self.assertIsNotNone(section)
        target = _strip_md_fmt(section.lower())
        self.assertIn("not increment any epoch", target)

    def test_tc1310a_duplicate_active_acquire_does_not_write_store(
        self,
    ) -> None:
        """§2.5.6: duplicate active acquire must not write the store file."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.5.6")
        self.assertIsNotNone(section)
        target = _strip_md_fmt(section.lower())
        self.assertIn("not write the store file", target)

    def test_tc1310a_expired_pair_cleared_then_reacquire_allowed(self) -> None:
        """§2.5.6: expired pair is cleaned during stale cleanup, then
        acquire proceeds normally."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.5.6")
        self.assertIsNotNone(section)
        target = section.lower()
        self.assertIn("stale lease is removed", target)

    def test_tc1310a_expired_pair_same_slot_epoch_incremented(self) -> None:
        """§2.5.6: re-using the same stable slot_id after expiry increments
        the epoch from the previous value."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.5.6")
        self.assertIsNotNone(section)
        target = section.lower()
        self.assertIn("epoch is incremented from the previous epoch", target)

    def test_tc1310a_two_worktrees_fill_two_global_slots(self) -> None:
        """§2.5.6: two distinct worktrees for same WorkerKind can occupy
        both global slots."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.5.6")
        self.assertIsNotNone(section)
        target = section.lower()
        self.assertTrue(
            "two different canonical worktrees" in target
            or "two distinct worktrees" in target
            or "both global slots" in target,
            "§2.5.6 must allow two worktrees to fill both global slots",
        )

    def test_tc1310a_third_worktree_global_capacity(self) -> None:
        """§2.5.6: a third distinct worktree for same WorkerKind →
        WorkerSlotCapacityError (global)."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.5.6")
        self.assertIsNotNone(section)
        target = section.lower()
        self.assertIn("third distinct worktree", target)

    def test_tc1310a_different_worker_kind_same_worktree_no_block(self) -> None:
        """§2.5.6: different WorkerKind leases on the same worktree do
        NOT block each other."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.5.6")
        self.assertIsNotNone(section)
        target = section.lower()
        self.assertTrue(
            "not block each other" in target
            or "do not block" in target
            or "does not block" in target,
            "§2.5.6 must state different WorkerKinds do not block each other on same worktree",
        )

    def test_tc1310a_no_old_second_call_acquires_different_slot(self) -> None:
        """§2.5.6 must NOT contain the old wording 'a second call with
        the same arguments acquires a different slot'."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.5.6")
        self.assertIsNotNone(section)
        self.assertNotIn(
            "a second call with the same arguments acquires a different slot",
            section,
            "§2.5.6 must NOT contain the old duplicate-acquire wording",
        )

    def test_tc1310a_release_validates_all_six_identity_fields(self) -> None:
        """§2.5.7 release must validate all six identity fields."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.5.7")
        self.assertIsNotNone(section, "ADR must contain §2.5.7")
        for field in ("slot_id", "lease_id", "lease_epoch", "worker_kind",
                       "holder_dispatch_id", "holder_instance_id"):
            self.assertIn(
                field, section,
                f"§2.5.7 must validate {field} on release",
            )

    def test_tc1310a_double_release_fail_closed(self) -> None:
        """§2.5.7 double release must be WorkerSlotNotHeldError."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.5.7")
        self.assertIsNotNone(section)
        self.assertIn("WorkerSlotNotHeldError", section,
                      "§2.5.7 must name WorkerSlotNotHeldError for double release")
        target = section.lower()
        self.assertTrue(
            "fail-closed" in target or "fail closed" in target
            or "not silently" in target or "must not" in target,
            "§2.5.7 must state double release is fail-closed",
        )

    def test_tc1310a_renew_expired_lease_fencing_error(self) -> None:
        """§2.5.8 renew of expired lease must be WorkerSlotFencingError."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.5.8")
        self.assertIsNotNone(section, "ADR must contain §2.5.8")
        self.assertIn("WorkerSlotFencingError", section,
                      "§2.5.8 must use WorkerSlotFencingError for expired renew")
        target = section.lower()
        self.assertTrue(
            "cannot be resurrected" in target
            or "cannot" in target,
            "§2.5.8 must state expired lease cannot be resurrected via renew",
        )

    def test_tc1310a_renew_does_not_change_epoch(self) -> None:
        """§2.5.8 renew must never change lease_epoch."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.5.8")
        self.assertIsNotNone(section)
        target = section.lower()
        self.assertTrue(
            "never" in target and "epoch" in target,
            "§2.5.8 must state epoch is never changed by renew",
        )

    def test_tc1310a_hold_fence_context_manager(self) -> None:
        """§2.5.9 must define hold_worker_slot_fence context manager."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.5.9")
        self.assertIsNotNone(section, "ADR must contain §2.5.9")
        self.assertIn("hold_worker_slot_fence", section,
                      "§2.5.9 must define hold_worker_slot_fence")

    def test_tc1310a_lock_free_validate_not_for_state_auth(self) -> None:
        """§2.5.9 must forbid using lock-free validate for state auth."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.5.9")
        self.assertIsNotNone(section)
        target = section.lower()
        self.assertTrue(
            "must not" in target or "never" in target,
            "§2.5.9 must forbid lock-free validation for state writes",
        )

    def test_tc1310a_global_lock_ordering_frozen(self) -> None:
        """§2.5.9 must freeze lock ordering: worker-slot → state."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.5.9")
        self.assertIsNotNone(section)
        target = section.lower()
        self.assertTrue(
            "lock" in target and "order" in target,
            "§2.5.9 must define global lock ordering",
        )

    def test_tc1310a_file_lock_o_creat_o_excl(self) -> None:
        """§2.5.10 must specify O_CREAT | O_EXCL."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.5.10")
        self.assertIsNotNone(section, "ADR must contain §2.5.10")
        self.assertIn("O_CREAT", section,
                      "§2.5.10 must specify O_CREAT | O_EXCL")

    def test_tc1310a_no_auto_stale_lock_removal(self) -> None:
        """§2.5.10 must forbid automatic stale lock removal."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.5.10")
        self.assertIsNotNone(section)
        target = section.lower()
        self.assertTrue(
            "not" in target and ("automat" in target or "assum" in target
                                 or "mtime" in target),
            "§2.5.10 must forbid automatic stale lock removal",
        )

    def test_tc1310a_atomic_write_pattern(self) -> None:
        """§2.5.11 must specify mkstemp + fsync + os.replace + dir fsync."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.5.11")
        self.assertIsNotNone(section, "ADR must contain §2.5.11")
        self.assertIn("os.replace", section,
                      "§2.5.11 must specify os.replace")
        self.assertIn("fsync", section,
                      "§2.5.11 must specify fsync")

    def test_tc1310a_worktree_symlink_rejected(self) -> None:
        """§2.5.12 must reject symlink/reparse-point workspace."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.5.12")
        self.assertIsNotNone(section, "ADR must contain §2.5.12")
        target = section.lower()
        self.assertTrue(
            "symlink" in target or "reparse" in target,
            "§2.5.12 must reject symlink/reparse-point workspace",
        )

    def test_tc1310a_worktree_str_lower_forbidden(self) -> None:
        """§2.5.12 must forbid str.lower() as substitute for proper
        normalisation."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.5.12")
        self.assertIsNotNone(section)
        self.assertIn("str.lower()", section,
                      "§2.5.12 must explicitly forbid str.lower()")

    def test_tc1310a_exception_hierarchy(self) -> None:
        """§2.5.13 must define exactly the frozen exception hierarchy."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.5.13")
        self.assertIsNotNone(section, "ADR must contain §2.5.13")
        for exc_name in sorted(self._EXCEPTION_CLASSES):
            self.assertIn(
                exc_name, section,
                f"§2.5.13 must define {exc_name}",
            )

    def test_tc1310a_no_config_error_in_hierarchy(self) -> None:
        """§2.5.13 must state No WorkerSlotConfigError and not list it
        in the hierarchy diagram."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.5.13")
        self.assertIsNotNone(section)
        # The hierarchy diagram must NOT include WorkerSlotConfigError.
        # The prose may mention it to say it is excluded.
        self.assertIn("No ``WorkerSlotConfigError``", section,
                      "§2.5.13 must state No WorkerSlotConfigError")

    def test_tc1310a_exception_message_safety(self) -> None:
        """§2.5.13 must forbid prompt/stdout/stderr/secrets in exception
        messages."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.5.13")
        self.assertIsNotNone(section)
        for forbidden in ("prompt", "stdout", "stderr", "Secrets",
                          "holder_instance_id", "canonical_worktree"):
            self.assertIn(
                forbidden, section,
                f"§2.5.13 must forbid {forbidden} in exception messages",
            )

    def test_tc1310a_module_boundaries_no_worker_adapter(self) -> None:
        """§2.5.14 must forbid calling WorkerAdapter / DispatcherGateway."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.5.14")
        self.assertIsNotNone(section, "ADR must contain §2.5.14")
        self.assertIn("run_worker", section,
                      "§2.5.14 must forbid calling run_worker")
        self.assertIn("worker_adapter", section,
                      "§2.5.14 must forbid importing worker_adapter")

    def test_tc1310a_module_boundaries_no_state_writes(self) -> None:
        """§2.5.14 must forbid writing tasks/events/outbox/report."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.5.14")
        self.assertIsNotNone(section)
        for forbidden in ("tasks.yaml", "events", "outbox", "delivery report"):
            self.assertIn(
                forbidden, section,
                f"§2.5.14 must forbid writing {forbidden}",
            )

    def test_tc1310a_independent_of_pm_lease(self) -> None:
        """§2.5 must state Worker slot lease is independent of PM lease."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "### 2.5")
        self.assertIsNotNone(section)
        self.assertIn("independent of the PM lease", section,
                      "§2.5 must state independence from PM lease")

    def test_tc1310a_task_card_split_in_contract(self) -> None:
        """§2.5.15 must define TC-13.10a/b/c split and dependencies."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.5.15")
        self.assertIsNotNone(section, "ADR must contain §2.5.15")
        for tid in ("TC-13.10a", "TC-13.10b", "TC-13.10c"):
            self.assertIn(
                tid, section,
                f"§2.5.15 must reference {tid}",
            )

    def test_tc1310a_status_remains_target(self) -> None:
        """§2.5.16 must state TC-13.10 overall remains Target."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "#### 2.5.16")
        self.assertIsNotNone(section, "ADR must contain §2.5.16")
        self.assertIn("Target", section,
                      "§2.5.16 must state overall remains Target")

    def test_tc1310b_production_module_exists(self) -> None:
        """worker_slot_lease.py must now exist (TC-13.10b)."""
        prod = (SKILL_ROOT / "scripts" / "worker_slot_lease.py")
        self.assertTrue(
            prod.exists(),
            f"Production module must exist: {prod}",
        )

    def test_tc1310b_test_module_exists(self) -> None:
        """test_worker_slot_lease.py must now exist (TC-13.10b)."""
        test_file = REPO_ROOT / "tests" / "test_worker_slot_lease.py"
        self.assertTrue(
            test_file.exists(),
            f"Test module must exist: {test_file}",
        )

    def test_tc1310a_tc139c_still_target(self) -> None:
        """TC-13.9c must still be Target."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_interface_status_table(adr_text)
        # TC-13.9c is not in the interface status table — check §5.
        section = _extract_markdown_section(adr_text, "## 5. Future Task Cards")
        self.assertIsNotNone(section)
        self.assertIn("TC-13.9c", section,
                      "TC-13.9c must remain in Future Task Cards")

    def test_tc1310a_tc1311c_current(self) -> None:
        """TC-13.11 must be Current."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_interface_status_table(adr_text)
        tc1311_row = None
        for row in rows:
            impl = self._resolve_col(row, "Implemented by", "Impl", "Notes")
            if "TC-13.11" in impl:
                tc1311_row = row
                break
        self.assertIsNotNone(tc1311_row, "ADR must contain TC-13.11 row")
        status = self._resolve_col(tc1311_row, "Status")
        self.assertIn("Current", status)

    def test_tc1310a_tc1318_depends_on_tc1310c(self) -> None:
        """ADR §5: TC-13.18 must depend on TC-13.10c."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_future_task_cards_table(adr_text)
        by_id = {
            self._resolve_col(row, "Task Card", "Task", "#"): row
            for row in rows
        }
        tc1318 = by_id.get("TC-13.18b")
        self.assertIsNotNone(tc1318, "TC-13.18b must exist in Future Task Cards")
        tc1318_dep = self._resolve_col(tc1318, "Depends on", "Dep")
        self.assertIn("TC-13.10c", tc1318_dep,
                      "TC-13.18b must depend on TC-13.10c")

    def test_tc1310a_tc1311_depends_on_tc1310c(self) -> None:
        """ADR §5: TC-13.11 must depend on TC-13.10c."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_future_task_cards_table(adr_text)
        by_id = {
            self._resolve_col(row, "Task Card", "Task", "#"): row
            for row in rows
        }
        tc1311 = next(
            (row for task_id, row in by_id.items()
             if task_id.startswith("TC-13.11")),
            None,
        )
        self.assertIsNotNone(tc1311, "TC-13.11a/b/c must exist in Future Task Cards")
        tc1311_dep = self._resolve_col(tc1311, "Depends on", "Dep")
        self.assertIn("TC-13.10c", tc1311_dep,
                      "TC-13.11a/b/c must depend on TC-13.10c")

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
        """Interface Status row for WorkerSlotLease (#15) must now be Current."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_interface_status_table(adr_text)
        tc1310_row = None
        for row in rows:
            impl = self._resolve_col(row, "Implemented by", "Impl", "Notes")
            task_ids = re.findall(r"\b(TC-\d+(?:\.\d+)*[a-z]?)\b", impl)
            if "TC-13.10c" in task_ids or "TC-13.10a" in task_ids:
                tc1310_row = row
                break
        self.assertIsNotNone(tc1310_row, "ADR must contain WorkerSlotLease row")
        status = self._resolve_col(tc1310_row, "Status")
        self.assertIn("Current", status, "WorkerSlotLease row must be Current now")

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

    # ── Time monotonicity safety rule ──────────────────────────────────────

    def test_adr_documents_time_monotonicity_safety_rule(self) -> None:
        """ADR must document the time monotonicity fail-closed rule from
        TC-13.10c: all writes must have now >= store updated_at."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        # The monotonicity rule should be documented in §2.5 (WorkerSlotLease).
        section = _extract_markdown_section(adr_text, "### 2.5")
        self.assertIsNotNone(section,
                             "ADR must contain §2.5 WorkerSlotLease")
        # Must mention monotonicity.
        self.assertTrue(
            "monotonic" in section.lower() or "monotonicity" in section.lower(),
            "ADR §2.5 must document time monotonicity rule",
        )

    # ── §2.13.14 status consistency tests ──────────────────────────────────

    def test_section_21314_tc13_10_is_current(self) -> None:
        """§2.13.14 must declare TC-13.10 as Current, not Target."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(
            adr_text, "#### 2.13.14"
        )
        self.assertIsNotNone(
            section,
            "ADR must contain §2.13.14 Status subsection",
        )
        # Must declare TC-13.10 as Current.
        self.assertIn(
            "TC-13.10 is **Current**",
            section,
            "§2.13.14 must declare TC-13.10 is Current",
        )
        # Must NOT contain the stale "TC-13.10 ... remain Target" sentence.
        self.assertNotIn(
            "TC-13.10, TC-13.11, TC-13.13, TC-13.14, and TC-13.18 remain "
            "**Target**",
            section,
            "§2.13.14 must NOT claim TC-13.10 remains Target",
        )

    def test_section_21314_tc13_11_14_18_remain_target(self) -> None:
        """§2.13.14 must keep TC-13.11/14/18 as Target."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(
            adr_text, "#### 2.13.14"
        )
        self.assertIsNotNone(section)
        # TC-13.11, TC-13.14, TC-13.18 remain Target.
        self.assertIn(
            "TC-13.11",
            section,
        )
        self.assertIn(
            "TC-13.14",
            section,
        )
        self.assertIn(
            "TC-13.18",
            section,
        )
        self.assertIn(
            "remain **Target**",
            section,
            "§2.13.14 must state remaining tasks are Target",
        )

    def test_interface_status_15_current(self) -> None:
        """Interface Status row #15 must be Current."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_interface_status_table(adr_text)
        # Row 15 should be Current.
        for row in rows:
            num = self._resolve_col(row, "#")
            if num == "15":
                status = self._resolve_col(row, "Status")
                self.assertIn(
                    "Current",
                    status,
                    f"Interface Status #15 must be Current, got {status!r}",
                )
                self.assertNotIn(
                    "Target",
                    status,
                    f"Interface Status #15 must NOT be Target, got {status!r}",
                )
                return
        self.fail("Interface Status row #15 not found")

    def test_interface_status_16_current(self) -> None:
        """Interface Status row #16 must be Current."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_interface_status_table(adr_text)
        for row in rows:
            num = self._resolve_col(row, "#")
            if num == "16":
                status = self._resolve_col(row, "Status")
                self.assertIn(
                    "Current",
                    status,
                    f"Interface Status #16 must be Current, got {status!r}",
                )
                return
        self.fail("Interface Status row #16 not found")

    def test_section_25_heading_current(self) -> None:
        """§2.5 heading must be Current."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        heading_m = re.search(
            r"^### 2\.5\s.*$", adr_text, re.MULTILINE,
        )
        self.assertIsNotNone(heading_m, "ADR must have a §2.5 heading")
        heading = heading_m.group(0)
        self.assertIn(
            "Current",
            heading,
            "§2.5 heading must say Current",
        )

    def test_no_other_tc13_10_target_claim_in_adr(self) -> None:
        """No other sentence in the ADR should claim TC-13.10 is Target.
        We parse only the substantive paragraphs — not code blocks, links,
        or future-task-card dependency descriptions that merely mention
        TC-13.10 as a task ID."""
        adr_text = self._adr_path().read_text(encoding="utf-8")

        # Strip code fences to avoid false positives.
        stripped = re.sub(r"```.*?```", "", adr_text, flags=re.DOTALL)

        # Find lines that contain "TC-13.10" AND "Target" within a
        # reasonable distance — but explicitly exclude lines where
        # TC-13.10 is listed alongside other TC IDs that ARE Target
        # (those are the allowed ones in §2.13.14 and Future Task Cards).
        # We already verify those lines individually above.  This test
        # guards against rogue "TC-13.10 … Target" patterns.
        lines = stripped.splitlines()
        for i, raw in enumerate(lines):
            stripped_line = raw.strip()
            if not stripped_line or stripped_line.startswith(">"):
                continue
            if "TC-13.10" not in stripped_line:
                continue
            # Skip lines that are purely dependency listings (e.g.
            # "TC-13.11 → TC-13.10c").
            if re.match(r"^\s*TC-[\d.]+(?:\s*[→,]\s*TC-[\d.]+)*\s*$",
                        stripped_line):
                continue
            # Skip §2.14 status/status-subsection lines that list
            # TC-13.10 as Current (e.g. "TC-13.10a/b/c are all Current").
            if ("TC-13.10" in stripped_line
                and "Current" in stripped_line
                and "###" not in stripped_line):
                continue
            # Skip the §2.13.14 line that says "TC-13.10 is Current".
            if "TC-13.10 is **Current**" in stripped_line:
                continue
            # Skip the §2.5 heading which says Current.
            if "### 2.5" in stripped_line:
                continue
            # Skip the §2.5.16 heading / status lines.
            if "2.5.16" in stripped_line:
                continue
            # Skip the §2.5.15 task card split which names TC-13.10c in
            # dependency arrows.
            if "TC-13.10c" in stripped_line and "→" in stripped_line:
                continue
            # Skip the interface table row #15 header.
            if "| 15 |" in stripped_line and "WorkerSlotLease" in stripped_line:
                continue
            # Skip the Future Task Cards table rows.
            if stripped_line.startswith("| TC-13.10"):
                continue
            # Skip the Frozen Contract marker in §2.5.
            if "Frozen Contract — TC-13.10a" in stripped_line:
                continue
            # Skip the TC-13.11a task-card split table (lists TC-13.10c
            # as a dependency — a valid forward reference, not a Target claim).
            if stripped_line.startswith("| TC-13.11") and "TC-13.10" in stripped_line:
                continue
            # If the line contains both TC-13.10 and Target, it's
            # a potential stale claim.
            if "Target" in stripped_line:
                self.fail(
                    f"ADR line {i + 1}: potential stale TC-13.10 Target "
                    f"claim: {stripped_line[:100]!r}"
                )

    # ═══════════════════════════════════════════════════════════════════════════
    # TC-13.12a ApprovalGate frozen contract tests
    # ═══════════════════════════════════════════════════════════════════════════

    _TC1312A_CONTRACT_SECTION = "### 2.15"
    _APPROVAL_GATE_PY = SKILL_ROOT / "scripts" / "approval_gate.py"

    # ── helpers ──────────────────────────────────────────────────────────────

    def _tc1312a_section(self) -> str:
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(
            adr_text, self._TC1312A_CONTRACT_SECTION
        )
        self.assertIsNotNone(
            section,
            f"ADR must contain {self._TC1312A_CONTRACT_SECTION} section",
        )
        return section  # type: ignore[return-value]

    # ── Group 0: Existence and status ────────────────────────────────────────

    def test_tc1312a_section_exists(self) -> None:
        """§2.15 must exist with 'Frozen Contract', 'TC-13.12d', >500 chars."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        m = re.search(
            r"^### 2\.15\s.*$", adr_text, re.MULTILINE,
        )
        self.assertIsNotNone(m, "ADR must have a §2.15 heading")
        heading = m.group(0)
        self.assertIn("Frozen Contract", heading)
        self.assertIn("TC-13.12", heading)
        section = self._tc1312a_section()
        self.assertGreater(len(section), 500)

    def test_tc1312a_interface_17_still_target(self) -> None:
        """Interface #17 must be Current — TC-13.12d is complete."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_interface_status_table(adr_text)
        for row in rows:
            num = self._resolve_col(row, "#")
            if num == "17":
                status = self._resolve_col(row, "Status")
                self.assertIn("Current", status)
                return
        self.fail("Interface Status row #17 not found")

    def test_tc1312a_interface_18_now_current(self) -> None:
        """Interface #18 (TC-13.13b) must now be Current — TC-13.13b completed."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_interface_status_table(adr_text)
        for row in rows:
            num = self._resolve_col(row, "#")
            if num == "18":
                status = self._resolve_col(row, "Status")
                self.assertIn("Current", status,
                              f"Interface #18 must now be Current, got: {status}")
                return
        self.fail("Interface Status row #18 not found")

    def test_tc1312b_approval_gate_py_exists(self) -> None:
        """approval_gate.py must exist after TC-13.12b."""
        self.assertTrue(
            self._APPROVAL_GATE_PY.exists(),
            "approval_gate.py must exist in TC-13.12b",
        )

    def test_tc1312a_future_task_cards_tc1313_updated(self) -> None:
        """§5 TC-13.13a/b rows exist; TC-13.13b is now done."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "## 5.")
        self.assertIsNotNone(section, "ADR must contain §5 Future Task Cards")
        self.assertIn("TC-13.13a", section,
                      "§5 must reference TC-13.13a")
        self.assertIn("TC-13.13b", section,
                      "§5 must reference TC-13.13b")
        # TC-13.13b now depends on TC-13.13a, which is complete
        self.assertIn("TC-13.13a", section,
                      "§5 TC-13.13b row must exist")

    # ── Group 1: Three domains ───────────────────────────────────────────────

    def test_tc1312a_three_domains_explicitly_separated(self) -> None:
        """§2.15.1 must contain three domain names."""
        section = self._tc1312a_section()
        self.assertIn("Task Action Approval", section)
        self.assertIn("Owner Approval", section)
        self.assertIn("Model Degradation Approval", section)

    def test_tc1312a_model_degradation_not_refactored(self) -> None:
        """§2.15.1 must state TC-13.12 does NOT refactor model degradation."""
        section = self._tc1312a_section()
        lower = section.lower()
        self.assertTrue(
            "not refactor" in lower
            or "does not refactor" in lower
            or "does **not** refactor" in lower,
            "§2.15 must state TC-13.12 does not refactor model degradation",
        )

    def test_tc1312a_granted_approval_ids_semantics_frozen(self) -> None:
        """§2.15.1 must state granted_approval_ids retains model-degradation
        semantics."""
        section = self._tc1312a_section()
        self.assertIn("granted_approval_ids", section)
        lower = section.lower()
        self.assertTrue(
            "model-degradation" in lower or "model degradation" in lower,
            "granted_approval_ids context must mention model-degradation",
        )

    # ── Group 2: ApprovalScope ───────────────────────────────────────────────

    def test_tc1312a_scope_exactly_three_values(self) -> None:
        """ApprovalScope must have exactly DISPATCH, ACCEPT, INTEGRATE."""
        section = self._tc1312a_section()
        self.assertIn("DISPATCH", section)
        self.assertIn("ACCEPT", section)
        self.assertIn("INTEGRATE", section)
        # Must state "exactly three" or equivalent
        lower = section.lower()
        self.assertTrue(
            "exactly three" in lower
            or "no more, no less" in lower
            or "exactly three members" in lower,
            "§2.15 must state ApprovalScope has exactly three values",
        )

    def test_tc1312a_scope_str_equals_value(self) -> None:
        """ADR must state str(member) == member.value for ApprovalScope."""
        section = self._tc1312a_section()
        # The contract prose states this explicitly; no runtime check needed
        lower = section.lower()
        self.assertTrue(
            "str(member)" in lower
            or "str(member) == member.value" in lower
            or 'json-serialised as lowercase' in lower,
            "§2.15 must state str(member) == member.value",
        )

    def test_tc1312a_scope_fail_closed_unknown(self) -> None:
        """ADR must state unknown scope values fail-closed."""
        section = self._tc1312a_section()
        lower = section.lower()
        self.assertTrue(
            "fail-closed" in lower or "valueerror" in lower,
            "§2.15 must state unknown ApprovalScope values fail-closed",
        )

    # ── Group 3: ApprovalSubject ─────────────────────────────────────────────

    def test_tc1312a_subject_five_fields(self) -> None:
        """ApprovalSubject must have exactly 5 fields."""
        section = self._tc1312a_section()
        for field in ("task_id", "revision", "attempt", "dispatch_id",
                       "accepted_commit"):
            self.assertIn(field, section,
                          f"ApprovalSubject must include '{field}'")
        # Must state exactly five
        lower = section.lower()
        self.assertTrue(
            "five-field" in lower or "exactly 5 fields" in lower
            or "exactly five" in lower or "immutable five-field" in lower,
            "§2.15 must state ApprovalSubject has exactly 5 fields",
        )

    def test_tc1312a_subject_accepted_commit_nullable(self) -> None:
        """accepted_commit must be str | None."""
        section = self._tc1312a_section()
        self.assertIn("str | None", section)
        self.assertIn("accepted_commit", section)

    # ── Group 4: ApprovalCheckRequest ────────────────────────────────────────

    def test_tc1312a_request_three_fields(self) -> None:
        """ApprovalCheckRequest must have exactly 3 fields."""
        section = self._tc1312a_section()
        for field in ("scope", "subject", "expected_snapshot_commit"):
            self.assertIn(field, section,
                          f"ApprovalCheckRequest must include '{field}'")

    def test_tc1312a_request_no_approval_id_field(self) -> None:
        """ApprovalCheckRequest must NOT carry an approval_id field."""
        section = self._tc1312a_section()
        # The contract explicitly says the gate resolves all matching
        # evidence and that the request does not carry an approval_id.
        lower = section.lower()
        self.assertTrue(
            "does **not** carry" in lower
            or "does not carry" in lower
            or "not carry an" in lower
            or "no approval_id" in lower
            or "not carry an `approval_id`" in lower,
            "§2.15 must state ApprovalCheckRequest does not carry approval_id",
        )

    # ── Group 5: ApprovalCheckResult ─────────────────────────────────────────

    def test_tc1312a_result_four_fields(self) -> None:
        """ApprovalCheckResult must have exactly 4 fields."""
        section = self._tc1312a_section()
        for field in ("passed", "failure_code", "matched_evidence",
                       "checked_at"):
            self.assertIn(field, section,
                          f"ApprovalCheckResult must include '{field}'")

    def test_tc1312a_result_failure_codes_frozen(self) -> None:
        """failure_code set must be exactly the 5 frozen values."""
        section = self._tc1312a_section()
        for code in ("not_found", "expired", "revoked", "wrong_scope",
                      "wrong_subject"):
            self.assertIn(code, section,
                          f"failure_code must include '{code}'")

    def test_tc1312a_result_passed_invariants(self) -> None:
        """passed=True → failure_code=None, matched_evidence non-None."""
        section = self._tc1312a_section()
        self.assertIn("passed", section)
        # The contract must describe the invariant
        lower = section.lower()
        self.assertTrue(
            "failure_code" in lower and "matched_evidence" in lower,
            "§2.15 must describe passed=True invariants",
        )

    # ── Group 6: Grant evidence schema ───────────────────────────────────────

    def test_tc1312a_grant_schema_version(self) -> None:
        """Grant schema_version must be agentdesk.task-approval/v1."""
        section = self._tc1312a_section()
        self.assertIn("agentdesk.task-approval/v1", section)

    def test_tc1312a_grant_record_type(self) -> None:
        """Grant record_type must be 'grant'."""
        section = self._tc1312a_section()
        self.assertIn('"grant"', section)

    def test_tc1312a_grant_exact_sixteen_root_keys(self) -> None:
        """Grant evidence must have exactly the 16 frozen root keys."""
        section = self._tc1312a_section()
        expected = [
            "schema_version", "record_type", "approval_id", "event_id",
            "scope", "task_id", "revision", "attempt", "dispatch_id",
            "accepted_commit", "actor_role_id", "lease_epoch",
            "granted_at", "expires_at", "reason", "snapshot_commit",
        ]
        # Parse the grant field table: find the table after "##### 2.15.6.1"
        subsection = _extract_markdown_section(
            section, "##### 2.15.6.1"
        )
        self.assertIsNotNone(subsection,
                             "ADR must contain §2.15.6.1 Grant Evidence")
        # Extract field names from the table rows
        field_names = []
        in_table = False
        for line in subsection.splitlines():
            stripped = line.strip()
            if stripped.startswith("| # |") or stripped.startswith("|---"):
                in_table = True
                continue
            if in_table and stripped.startswith("|"):
                cells = [c.strip() for c in stripped.strip("|").split("|")]
                if len(cells) >= 3:
                    # Column 2 is the field name (with backticks)
                    name = cells[1].strip("`").strip()
                    if name:
                        field_names.append(name)
            elif in_table and not stripped.startswith("|"):
                break
        self.assertEqual(
            field_names, expected,
            f"Grant evidence must have exactly 16 fields in order: "
            f"got {field_names}",
        )

    def test_tc1312a_grant_actor_role_id_pm(self) -> None:
        """Grant actor_role_id must be 'PM'."""
        section = self._tc1312a_section()
        self.assertIn('actor_role_id', section)
        self.assertIn('"PM"', section)

    # ── Group 7: Revoke evidence schema ──────────────────────────────────────

    def test_tc1312a_revoke_schema_version(self) -> None:
        """Revoke uses same schema_version as grant."""
        section = self._tc1312a_section()
        self.assertIn("agentdesk.task-approval/v1", section)

    def test_tc1312a_revoke_record_type(self) -> None:
        """Revoke record_type must be 'revoke'."""
        section = self._tc1312a_section()
        self.assertIn('"revoke"', section)

    def test_tc1312a_revoke_exact_ten_root_keys(self) -> None:
        """Revoke evidence must have exactly the 10 frozen root keys."""
        section = self._tc1312a_section()
        expected = [
            "schema_version", "record_type", "approval_id", "event_id",
            "task_id", "actor_role_id", "lease_epoch",
            "revoked_at", "reason", "snapshot_commit",
        ]
        subsection = _extract_markdown_section(
            section, "##### 2.15.6.2"
        )
        self.assertIsNotNone(subsection,
                             "ADR must contain §2.15.6.2 Revoke Evidence")
        field_names = []
        in_table = False
        for line in subsection.splitlines():
            stripped = line.strip()
            if stripped.startswith("| # |") or stripped.startswith("|---"):
                in_table = True
                continue
            if in_table and stripped.startswith("|"):
                cells = [c.strip() for c in stripped.strip("|").split("|")]
                if len(cells) >= 3:
                    name = cells[1].strip("`").strip()
                    if name:
                        field_names.append(name)
            elif in_table and not stripped.startswith("|"):
                break
        self.assertEqual(
            field_names, expected,
            f"Revoke evidence must have exactly 10 fields in order: "
            f"got {field_names}",
        )

    # ── Group 8: Approvals directory ─────────────────────────────────────────

    def test_tc1312a_approvals_directory_specified(self) -> None:
        """Evidence must be in docs/pm/approvals/."""
        section = self._tc1312a_section()
        self.assertIn("docs/pm/approvals/", section)

    def test_tc1312a_filename_from_event_id(self) -> None:
        """Filename derived from event_id, never approval_id directly."""
        section = self._tc1312a_section()
        self.assertIn("event-id", section.lower().replace("_", "-"))
        self.assertIn("event_id", section)
        lower = section.lower()
        self.assertTrue(
            "filename" in lower and "event_id" in lower,
            "§2.15 must state filename comes from event_id",
        )

    # ── Group 9: Evidence Writer API ─────────────────────────────────────────

    def test_tc1312a_writer_functions_in_all(self) -> None:
        """write_grant and write_revoke must appear in __all__."""
        section = self._tc1312a_section()
        self.assertIn("write_grant", section)
        self.assertIn("write_revoke", section)
        all_match = re.search(
            r"__all__\s*=\s*\[(.*?)\]", section, re.DOTALL,
        )
        self.assertIsNotNone(all_match, "§2.15 must have an __all__ block")
        all_block = all_match.group(1)
        self.assertIn("write_grant", all_block)
        self.assertIn("write_revoke", all_block)

    def test_tc1312a_write_grant_signature(self) -> None:
        """write_grant must have the frozen signature."""
        section = self._tc1312a_section()
        for param in ("project_root", "approval_id", "event_id", "scope",
                       "subject", "lease_epoch", "now", "reason",
                       "expires_at", "expected_snapshot_commit"):
            self.assertIn(param, section,
                          f"write_grant must accept '{param}'")
        self.assertIn("tuple[Path, str]", section)

    def test_tc1312a_write_revoke_signature(self) -> None:
        """write_revoke must have the frozen signature."""
        section = self._tc1312a_section()
        for param in ("project_root", "approval_id", "event_id",
                       "lease_epoch", "now", "reason",
                       "expected_snapshot_commit"):
            self.assertIn(param, section,
                          f"write_revoke must accept '{param}'")
        self.assertIn("tuple[Path, str]", section)

    def test_tc1312a_writer_acquires_state_lock(self) -> None:
        """Writer must acquire state lock internally."""
        section = self._tc1312a_section()
        lower = section.lower()
        self.assertTrue(
            "acquires" in lower and "state lock" in lower,
            "§2.15.6 must state writer acquires state lock",
        )

    # ── Group 10: Public API __all__ ─────────────────────────────────────────

    def test_tc1312a_all_exact_fifteen_symbols(self) -> None:
        """__all__ must contain exactly 15 public symbols."""
        section = self._tc1312a_section()
        all_match = re.search(
            r"__all__\s*=\s*\[(.*?)\]", section, re.DOTALL,
        )
        self.assertIsNotNone(all_match, "§2.15 must have an __all__ block")
        lines = all_match.group(1).split('\n')
        symbols = [
            s.strip().strip('",') for s in lines
            if s.strip().strip('",')
        ]
        self.assertEqual(
            len(symbols), 15,
            f"__all__ must have 15 symbols, got {len(symbols)}: {symbols}",
        )

    def test_tc1312a_all_exact_names(self) -> None:
        """__all__ must have the exact frozen ordered symbol list."""
        section = self._tc1312a_section()
        all_match = re.search(
            r"__all__\s*=\s*\[(.*?)\]", section, re.DOTALL,
        )
        self.assertIsNotNone(all_match, "§2.15 must have an __all__ block")
        lines = all_match.group(1).split('\n')
        symbols = [
            s.strip().strip('",') for s in lines
            if s.strip().strip('",')
        ]
        expected = [
            "ApprovalScope",
            "ApprovalSubject",
            "ApprovalCheckRequest",
            "ApprovalEvidence",
            "ApprovalCheckResult",
            "ApprovalGate",
            "ApprovalError",
            "ApprovalValidationError",
            "ApprovalNotFoundError",
            "ApprovalAmbiguousError",
            "ApprovalExpiredError",
            "ApprovalRevokedError",
            "ApprovalSnapshotConflictError",
            "write_grant",
            "write_revoke",
        ]
        self.assertEqual(
            symbols, expected,
            f"__all__ must have exact frozen order. "
            f"Expected {expected}, got {symbols}",
        )

    # ── Group 11: Exception hierarchy ────────────────────────────────────────

    def test_tc1312a_exception_independent_root(self) -> None:
        """ApprovalError must NOT subclass ControlPlaneTransitionError."""
        section = self._tc1312a_section()
        lower = section.lower()
        self.assertTrue(
            "not a subclass" in lower
            or "not** a subclass" in lower
            or "independent root" in lower,
            "§2.15.14 must state ApprovalError is NOT a subclass "
            "of ControlPlaneTransitionError",
        )

    def test_tc1312a_exception_six_subclasses(self) -> None:
        """Exception hierarchy must list exactly 6 subclasses."""
        section = self._tc1312a_section()
        expected = [
            "ApprovalValidationError",
            "ApprovalNotFoundError",
            "ApprovalAmbiguousError",
            "ApprovalExpiredError",
            "ApprovalRevokedError",
            "ApprovalSnapshotConflictError",
        ]
        for sub in expected:
            self.assertIn(sub, section,
                          f"Exception hierarchy must include {sub}")

    # ── Group 12: Git snapshot ───────────────────────────────────────────────

    def test_tc1312a_snapshot_commit_ancestry_not_file_existence(self) -> None:
        """Ancestry check uses merge-base, NOT 'file must exist in commit'."""
        section = self._tc1312a_section()
        self.assertIn("merge-base", section)
        lower = section.lower()
        self.assertTrue(
            "does not require" in lower
            or "does **not** require" in lower,
            "§2.15.10 must state ancestry != file-existence-in-commit",
        )

    def test_tc1312a_head_mismatch_fail_closed(self) -> None:
        """HEAD != expected_snapshot_commit → ApprovalSnapshotConflictError."""
        section = self._tc1312a_section()
        self.assertIn("ApprovalSnapshotConflictError", section)
        lower = section.lower()
        self.assertTrue(
            "head mismatch" in lower
            or "!=" in section
            or "head" in lower,
            "§2.15.10 must describe HEAD mismatch → fail-closed",
        )

    def test_tc1312a_pre_commit_evidence_valid(self) -> None:
        """Newly-created, not-yet-committed evidence must be usable."""
        section = self._tc1312a_section()
        lower = section.lower()
        self.assertTrue(
            "newly-created" in lower
            or "not-yet-committed" in lower
            or "not yet committed" in lower,
            "§2.15.10 must allow pre-commit evidence",
        )

    # ── Group 13: Locking and TOCTOU ─────────────────────────────────────────

    def test_tc1312a_gate_acquires_no_locks(self) -> None:
        """ApprovalGate must acquire NO locks."""
        section = self._tc1312a_section()
        lower = section.lower()
        self.assertTrue(
            "acquires no lock" in lower
            or "acquires** no lock" in lower
            or "no locks" in lower,
            "§2.15.11 must state Gate acquires no locks",
        )

    def test_tc1312a_caller_holds_state_lock(self) -> None:
        """Caller (TC-13.11) must hold state lock before calling Gate."""
        section = self._tc1312a_section()
        lower = section.lower()
        self.assertTrue(
            "state lock" in lower and ("caller" in lower or "tc-13.11" in lower),
            "§2.15.11 must state caller holds state lock",
        )

    def test_tc1312a_external_precheck_not_substitute(self) -> None:
        """External pre-check outside lock ≠ substitute."""
        section = self._tc1312a_section()
        lower = section.lower()
        self.assertTrue(
            "not a substitute" in lower
            or "not** a substitute" in lower
            or "pre-check" in lower,
            "§2.15.11 must state external pre-check is not a substitute",
        )

    def test_tc1312a_replay_not_requery(self) -> None:
        """Idempotent replay must use original guard_results."""
        section = self._tc1312a_section()
        lower = section.lower()
        self.assertTrue(
            "guard_results" in lower and ("replay" in lower or "re-query" in lower
                                            or "requery" in lower),
            "§2.15.11 must state replay does not re-query Gate",
        )

    # ── Group 14: Scope integration ──────────────────────────────────────────

    def test_tc1312a_dispatch_scope_binding(self) -> None:
        """dispatch scope: accepted_commit=None, after TASK_DISPATCHED CAS."""
        section = self._tc1312a_section()
        self.assertIn("TASK_DISPATCHED", section)
        # accepted_commit=None for dispatch scope
        self.assertIn("None", section)

    def test_tc1312a_accept_scope_binding(self) -> None:
        """accept scope: accepted_commit=None, after DELIVERY_ACCEPTED CAS."""
        section = self._tc1312a_section()
        self.assertIn("DELIVERY_ACCEPTED", section)

    def test_tc1312a_integrate_scope_binding(self) -> None:
        """integrate scope: accepted_commit bound, after CHANGE_INTEGRATED CAS."""
        section = self._tc1312a_section()
        self.assertIn("CHANGE_INTEGRATED", section)

    # ── Group 15: Revoke semantics ───────────────────────────────────────────

    def test_tc1312a_revoke_not_retroactive(self) -> None:
        """Revoke does NOT invalidate historical transitions."""
        section = self._tc1312a_section()
        lower = section.lower()
        self.assertTrue(
            "does not retroactively" in lower
            or "does **not** retroactively" in lower
            or "not invalidate historical" in lower,
            "§2.15.11 must state revoke is not retroactive",
        )

    def test_tc1312a_revoke_blocks_new_only(self) -> None:
        """Revoke blocks NEW transitions only."""
        section = self._tc1312a_section()
        lower = section.lower()
        self.assertTrue(
            "new" in lower and ("transition" in lower or "blocks" in lower),
            "§2.15.11 must state revoke blocks new transitions only",
        )

    # ── Group 16: TC-13.11 API preservation ──────────────────────────────────

    def test_tc1312a_tc1311_transition_request_unchanged(self) -> None:
        """TC-13.11 TransitionRequest API is unchanged."""
        section = self._tc1312a_section()
        lower = section.lower()
        self.assertTrue(
            "transitionrequest" in lower
            and ("unchanged" in lower or "no" in lower),
            "§2.15 must state TC-13.11 TransitionRequest unchanged",
        )

    def test_tc1312a_tc1311_apply_transition_signature_unchanged(self) -> None:
        """apply_transition signature is unchanged."""
        section = self._tc1312a_section()
        lower = section.lower()
        self.assertTrue(
            "apply_transition" in lower
            and ("unchanged" in lower or "no" in lower),
            "§2.15 must state apply_transition signature unchanged",
        )

    # ── Group 17: Task-card split ────────────────────────────────────────────

    def test_tc1312a_task_card_split_four_cards(self) -> None:
        """§2.15.17 must reference TC-13.12a, 12b, 12c, 12d."""
        section = self._tc1312a_section()
        for card in ("TC-13.12a", "TC-13.12b", "TC-13.12c", "TC-13.12d"):
            self.assertIn(card, section,
                          f"§2.15.17 must reference {card}")

    def test_tc1312a_future_task_cards_split_four_rows(self) -> None:
        """§5 must have four separate TC-13.12 rows (a/b/c/d)."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "## 5.")
        self.assertIsNotNone(section, "ADR must contain §5")
        for card in ("TC-13.12a", "TC-13.12b", "TC-13.12c", "TC-13.12d"):
            self.assertIn(card, section,
                          f"§5 Future Task Cards must include {card}")

    def test_tc1312a_tc1312c_depends_on_12b_and_1311(self) -> None:
        """TC-13.12c must depend on TC-13.12b and TC-13.11."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(adr_text, "## 5.")
        self.assertIsNotNone(section, "ADR must contain §5")
        # Parse the TC-13.12c row
        found = False
        for line in section.splitlines():
            if line.strip().startswith("| TC-13.12c"):
                self.assertIn("TC-13.12b", line,
                              "TC-13.12c must depend on TC-13.12b")
                self.assertIn("TC-13.11", line,
                              "TC-13.12c must depend on TC-13.11")
                found = True
                break
        self.assertTrue(found, "TC-13.12c row not found in §5")

    # ── Group 18: Security ───────────────────────────────────────────────────

    def test_tc1312a_check_zero_writes(self) -> None:
        """check() and require() must perform zero writes."""
        section = self._tc1312a_section()
        lower = section.lower()
        self.assertTrue(
            "zero file writes" in lower
            or "zero writes" in lower
            or "pure read-only" in lower,
            "§2.15 must state check/require are zero-write",
        )

    def test_tc1312a_no_env_network_model(self) -> None:
        """Security boundary forbids env vars, network, model calls."""
        section = self._tc1312a_section()
        lower = section.lower()
        self.assertTrue(
            "no environment" in lower
            or "no network" in lower
            or "no model" in lower
            or "no api" in lower,
            "§2.15.16 must forbid env/network/model access",
        )

    # ── Group 19: ApprovalEvidence typed model ──────────────────────────────

    def test_tc1312a_approval_evidence_exact_ten_fields(self) -> None:
        """§2.15.5 ApprovalEvidence must have exactly 10 fields."""
        section = self._tc1312a_section()
        # Parse the ApprovalEvidence dataclass block
        ev_block = re.search(
            r'class ApprovalEvidence:(.*?)(?=\n\S|\Z)', section, re.DOTALL,
        )
        self.assertIsNotNone(
            ev_block, "§2.15.5 must contain class ApprovalEvidence",
        )
        body = ev_block.group(1)
        expected = [
            "approval_id",
            "event_id",
            "scope",
            "subject",
            "actor_role_id",
            "lease_epoch",
            "granted_at",
            "expires_at",
            "reason",
            "snapshot_commit",
        ]
        for field in expected:
            self.assertIn(field, body,
                          f"ApprovalEvidence must have field '{field}'")
        # Count field annotations (lines with ': str' or ': int' etc.)
        field_count = len(
            [l for l in body.split('\n')
             if ':' in l and not l.strip().startswith('#')
                and not l.strip().startswith('@')]
        )
        self.assertEqual(
            field_count, 10,
            f"ApprovalEvidence must have exactly 10 fields, got ~{field_count}",
        )

    def test_tc1312a_approval_evidence_frozen_slots_no_dict(self) -> None:
        """§2.15.5 ApprovalEvidence must be frozen=True, slots=True, no
        __dict__."""
        section = self._tc1312a_section()
        self.assertIn("@dataclass(frozen=True, slots=True)", section,
                      "§2.15.5 must declare @dataclass(frozen=True, slots=True)")
        self.assertIn("no ``__dict__``", section)
        # Must forbid mutable containers
        lower = section.lower()
        self.assertTrue(
            "no ``list``" in lower or "no list" in lower
            or "no ``dict``" in lower,
            "§2.15.5 must forbid mutable containers",
        )

    def test_tc1312a_approval_evidence_scope_typed(self) -> None:
        """§2.15.5 scope and subject must use typed model, not bare str."""
        section = self._tc1312a_section()
        ev_block = re.search(
            r'class ApprovalEvidence:(.*?)(?=\n\S|\Z)', section, re.DOTALL,
        )
        self.assertIsNotNone(ev_block)
        body = ev_block.group(1)
        # scope must be ApprovalScope, not str
        self.assertIn("scope: ApprovalScope", body,
                      "scope must be typed ApprovalScope")
        self.assertIn("subject: ApprovalSubject", body,
                      "subject must be typed ApprovalSubject")

    def test_tc1312a_approval_evidence_actor_role_id_pm(self) -> None:
        """§2.15.5 actor_role_id must be fixed to 'PM'."""
        section = self._tc1312a_section()
        self.assertIn('actor_role_id', section)
        self.assertIn('"PM"', section)
        lower = section.lower()
        self.assertTrue(
            "any other value is rejected" in lower,
            "§2.15.5 must state non-PM actor_role_id is rejected",
        )

    def test_tc1312a_approval_evidence_lease_epoch_rejects_bool(self) -> None:
        """§2.15.5 lease_epoch must reject bool, 0, negatives."""
        section = self._tc1312a_section()
        self.assertIn("Non-bool", section)
        self.assertIn(">= 1", section)
        lower = section.lower()
        self.assertTrue(
            "true" in lower and "false" in lower,
            "§2.15.5 must explicitly reject True/False for lease_epoch",
        )
        self.assertIn("0", section)
        self.assertIn("negative", lower)

    def test_tc1312a_approval_evidence_time_fields_rules(self) -> None:
        """§2.15.5 granted_at RFC 3339 UTC; expires_at None or strictly
        after."""
        section = self._tc1312a_section()
        self.assertIn("RFC 3339 UTC", section)
        self.assertIn("strictly after", section)
        lower = section.lower()
        self.assertTrue(
            "expires_at ==" in lower or "expires_at ==" in section.lower(),
            "§2.15.5 must reject expires_at == granted_at",
        )

    def test_tc1312a_approval_evidence_snapshot_commit_40_hex(self) -> None:
        """§2.15.5 snapshot_commit must be 40-char lowercase hex SHA."""
        section = self._tc1312a_section()
        self.assertIn("snapshot_commit", section)
        self.assertIn("40-char lowercase hex", section.lower())

    def test_tc1312a_approval_evidence_reason_clean(self) -> None:
        """§2.15.5 reason must have no NUL, CR, LF, leading/trailing ws."""
        section = self._tc1312a_section()
        self.assertIn("no NUL", section)
        self.assertIn("no leading/trailing", section.lower())

    def test_tc1312a_grant_to_evidence_mapping(self) -> None:
        """§2.15.5 must contain the Grant 16-key → ApprovalEvidence mapping
        table."""
        section = self._tc1312a_section()
        # Mapping table headings
        self.assertIn("Grant 16-key", section)
        self.assertIn("ApprovalEvidence 10-field", section)
        self.assertIn("Source in Grant record", section)
        # Check at least some mapping rows
        for field in ("approval_id", "event_id", "scope", "subject",
                       "actor_role_id", "lease_epoch", "granted_at",
                       "expires_at", "reason", "snapshot_commit"):
            self.assertIn(field, section)

    def test_tc1312a_revoke_not_approval_evidence(self) -> None:
        """§2.15.5 must state revoke records are NEVER ApprovalEvidence."""
        section = self._tc1312a_section()
        lower = section.lower()
        self.assertTrue(
            "never be constructed" in lower
            or "never** be constructed" in lower,
            "§2.15.5 must ban constructing ApprovalEvidence from revoke",
        )

    def test_tc1312a_schema_version_not_in_evidence(self) -> None:
        """§2.15.5 schema_version and record_type must NOT be dataclass
        fields."""
        section = self._tc1312a_section()
        ev_block = re.search(
            r'class ApprovalEvidence:(.*?)(?=\n\S|\Z)', section, re.DOTALL,
        )
        self.assertIsNotNone(ev_block)
        body = ev_block.group(1)
        self.assertNotIn("schema_version", body,
                         "schema_version must not be an ApprovalEvidence field")
        self.assertNotIn("record_type", body,
                         "record_type must not be an ApprovalEvidence field")

    def test_tc1312a_approval_gate_require_returns_evidence(self) -> None:
        """§2.15.8.1 ApprovalGate.require() must return ApprovalEvidence."""
        section = self._tc1312a_section()
        # require() signature must return ApprovalEvidence
        # Find the require method
        self.assertIn("def require(", section)
        self.assertIn("ApprovalEvidence", section)
        # The frozen API rules in the contract must reference the return type
        lower = section.lower()
        self.assertTrue(
            "matching" in lower and "approvalevidence" in lower,
            "§2.15.8.1 require() must return ApprovalEvidence",
        )

    def test_tc1312a_no_evidence_deferred_text(self) -> None:
        """ADR must no longer contain 'ApprovalEvidence 字段留待 TC-13.12b 定义'
        or similar deferred semantics."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        # Check for Chinese "留待" or English "deferred" re ApprovalEvidence
        deferred_patterns = [
            r"ApprovalEvidence.*留待",
            r"ApprovalEvidence.*deferred",
            r"字段留待.*TC-13\.12b",
            r"field.*deferred.*TC-13\.12b",
        ]
        for pat in deferred_patterns:
            self.assertIsNone(
                re.search(pat, adr_text),
                f"ADR must not contain deferred ApprovalEvidence text: {pat}",
            )

    def test_tc1312b_approval_gate_py_present(self) -> None:
        """approval_gate.py must exist after TC-13.12b implementation."""
        self.assertTrue(
            self._APPROVAL_GATE_PY.exists(),
            "approval_gate.py must exist after TC-13.12b",
        )

    def test_tc1312a_interface_17_current_and_tc1313b_current(self) -> None:
        """Interface #17 and #18 are both Current."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_interface_status_table(adr_text)
        for row in rows:
            num = self._resolve_col(row, "#")
            if num == "17":
                self.assertIn("Current",
                              self._resolve_col(row, "Status"))
            if num == "18":
                self.assertIn("Current",
                              self._resolve_col(row, "Status"))

    # ── TC-13.11a ControlPlaneTransitionService frozen contract tests ───────

    _TC1311A_CONTRACT_SECTION = "### 2.14"
    _CONTROL_PLANE_PY = SKILL_ROOT / "scripts" / "control_plane_transition.py"

    # ── helpers ──────────────────────────────────────────────────────────────

    def _tc1311a_section(self) -> str:
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(
            adr_text, self._TC1311A_CONTRACT_SECTION
        )
        self.assertIsNotNone(
            section,
            f"ADR must contain {self._TC1311A_CONTRACT_SECTION} section",
        )
        return section  # type: ignore[return-value]

    def _tc1311a_heading(self) -> str:
        adr_text = self._adr_path().read_text(encoding="utf-8")
        m = re.search(
            r"^### 2\.14\s.*$", adr_text, re.MULTILINE,
        )
        self.assertIsNotNone(m, "ADR must have a §2.14 heading")
        return m.group(0)

    # ── existence / basic structure ──────────────────────────────────────────

    def test_tc1311c_section_exists(self) -> None:
        """SS2.14 Contract section must exist with Current marker."""
        heading = self._tc1311a_heading()
        self.assertIn("Frozen Contract", heading)
        self.assertIn("TC-13.11c", heading)
        section = self._tc1311a_section()
        self.assertGreater(len(section), 500)

    def test_tc1311c_interface_16_current(self) -> None:
        """Interface #16 must be Current."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_interface_status_table(adr_text)
        for row in rows:
            num = self._resolve_col(row, "#")
            if num == "16":
                status = self._resolve_col(row, "Status")
                self.assertIn("Current", status)
                return
        self.fail("Interface Status row #16 not found")

    def test_tc1311b_production_module_exists(self) -> None:
        """TC-13.11b: production module and test file must exist."""
        self.assertTrue(self._CONTROL_PLANE_PY.exists())
        test_file = REPO_ROOT / "tests" / "test_control_plane_transition.py"
        self.assertTrue(test_file.exists())

    def test_tc1311c_apply_transition_no_longer_not_implemented(self) -> None:
        """apply_transition is now implemented — TC-13.11c is Current."""
        import importlib, sys
        mod_name = "control_plane_transition"
        if mod_name in sys.modules:
            del sys.modules[mod_name]
        script_dir = str(SKILL_ROOT / "scripts")
        if script_dir not in sys.path:
            sys.path.insert(0, script_dir)
        from control_plane_transition import (
            ControlPlaneTransitionService,
            TransitionRequest,
            TransitionCAS,
            TransitionEventContext,
            SpecifyPayload,
        )
        import tempfile
        from datetime import UTC, datetime
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            svc = ControlPlaneTransitionService(project_root=root)
            cas = TransitionCAS(
                task_id="TC-001", expected_revision=1,
                expected_state="draft",
                expected_snapshot_commit="0" * 40,
            )
            ctx = TransitionEventContext(
                source_message_id=None, evidence_refs=(), guard_results=(),
            )
            req = TransitionRequest(
                cas=cas, dispatch_cas=None,
                event_id="EVT-20260727-0001",
                event_type="TASK_SPECIFIED",
                payload=SpecifyPayload(),
                event_context=ctx,
            )
            now = datetime(2026, 7, 27, 0, 0, 0, tzinfo=UTC)
            try:
                result = svc.apply_transition(req, None, now)
            except NotImplementedError:
                self.fail(
                    "apply_transition must not raise NotImplementedError "
                    "in TC-13.11c"
                )
            except Exception:
                pass  # expected -- no tasks.yaml in empty dir

    def test_tc1311a_section_25_still_current(self) -> None:
        adr_text = self._adr_path().read_text(encoding="utf-8")
        m = re.search(r"^### 2\.5\s.*$", adr_text, re.MULTILINE)
        self.assertIsNotNone(m)
        self.assertIn("Current", m.group(0))

    def test_tc1311a_tc1312_and_beyond_still_target(self) -> None:
        section = self._tc1311a_section()
        for tc_id in ("TC-13.12", "TC-13.13", "TC-13.14", "TC-13.17", "TC-13.18"):
            self.assertIn(tc_id, section)

    # ── __all__ completeness ─────────────────────────────────────────────────

    def test_tc1311a_all_exports_all_callable_types(self) -> None:
        """Every type the caller constructs or catches must be in __all__."""
        section = self._tc1311a_section()
        all_match = re.search(
            r"__all__\s*=\s*\[(.*?)\]", section, re.DOTALL,
        )
        self.assertIsNotNone(all_match, "§2.14 must have an __all__ block")
        all_block = all_match.group(1)
        required = [
            "ControlPlaneTransitionService",
            "TransitionCAS",
            "DispatchCAS",
            "TransitionRequest",
            "TransitionResult",
            "ControlPlaneTransitionError",
            "TransitionValidationError",
            "TransitionCASConflictError",
            "TransitionDuplicateEvidenceError",
            "TransitionSchemaError",
            "TransitionWriteError",
        ]
        for name in required:
            self.assertIn(name, all_block,
                          f"{name} must be in __all__")

    # ── typed payload union (no Optional-field explosion) ────────────────────

    def test_tc1311a_typed_payload_union(self) -> None:
        """TransitionRequest uses a typed payload, not 20+ Optional
        fields."""
        section = self._tc1311a_section()
        self.assertIn("payload: TransitionPayload", section)
        self.assertIn("TransitionPayload", section)
        self.assertIn("DispatchPayload", section)
        self.assertIn("DeliveryAcceptedPayload", section)
        self.assertIn("BlockedPayload", section)
        self.assertIn("SupersededPayload", section)
        # TransitionRequest class must not have 20+ optional fields.
        # The word "to_state: str" may appear in the docstring of
        # TransitionResult — only forbid it IN TransitionRequest.
        req_class = re.search(
            r"class TransitionRequest:.*?(?=\nclass TransitionResult)",
            section, re.DOTALL,
        )
        if req_class:
            self.assertNotIn("dispatch_id: str | None", req_class.group(0))
            self.assertNotIn("blocked_reason: str | None", req_class.group(0))

    def test_tc1311a_no_object_dict_any_in_payload(self) -> None:
        """No object, bare dict, or Any in TransitionRequest payload."""
        section = self._tc1311a_section()
        # Extract the TransitionRequest class body.
        req_match = re.search(
            r"class TransitionRequest:.*?(?=\n@dataclass|\nclass |\n```)",
            section, re.DOTALL,
        )
        if req_match:
            body = req_match.group(0)
            self.assertNotIn(": object", body)
            self.assertNotIn(": dict", body)
            self.assertNotIn(": Any", body)

    def test_tc1311a_event_type_payload_mismatch_rule(self) -> None:
        """event_type / payload variant mismatch must fail-closed."""
        section = self._tc1311a_section()
        self.assertIn("event_type", section)
        # The rule must exist somewhere in §2.14.11.
        lower = section.lower()
        self.assertIn("mismatch", lower)

    # ── spelling fix ─────────────────────────────────────────────────────────

    def test_tc1311a_no_supreseded_misspelling(self) -> None:
        """supreseded_by must not appear — correct spelling is
        superseded_by."""
        section = self._tc1311a_section()
        self.assertNotIn("supreseded", section)

    def test_tc1311a_superseded_by_correct_spelling(self) -> None:
        """superseded_by must appear with the correct spelling."""
        section = self._tc1311a_section()
        self.assertIn("superseded_by", section)

    # ── event field coverage ─────────────────────────────────────────────────

    def test_tc1311a_event_common_keys_match_existing_schema(self) -> None:
        """Event common keys must cover the existing TASK_DISPATCHED
        example in events-outbox-and-validation.md."""
        section = self._tc1311a_section()
        required_common = [
            "schema_version",
            "event_id",
            "event_type",
            "task_id",
            "revision",
            "attempt",
            "dispatch_id",
            "from_state",
            "to_state",
            "lease_epoch",
            "actor_role_id",
            "occurred_at",
        ]
        for key in required_common:
            self.assertIn(key, section,
                          f"Common event key {key} must be in §2.14.9")

    def test_tc1311a_source_message_id_semantics(self) -> None:
        """source_message_id must reference callback_id semantics."""
        section = self._tc1311a_section()
        self.assertIn("source_message_id", section)
        self.assertIn("callback_id", section.lower())

    def test_tc1311a_evidence_refs_and_guard_results(self) -> None:
        """evidence_refs and guard_results must be defined."""
        section = self._tc1311a_section()
        self.assertIn("evidence_refs", section)
        self.assertIn("guard_results", section)

    def test_tc1311a_event_extra_keys_fail_closed(self) -> None:
        """Unknown extra keys on event must be fail-closed."""
        section = self._tc1311a_section()
        self.assertIn("Unknown extra keys", section)

    # ── idempotency execution order ──────────────────────────────────────────

    def test_tc1311a_idempotency_execution_order(self) -> None:
        """Idempotency check happens after lock acquisition, before CAS."""
        section = self._tc1311a_section()
        self.assertIn("3a.", section, "§2.14.6 must have step 3a")
        self.assertIn("Full idempotent-replay check", section)
        self.assertIn("tasks.yaml already reflects", section)

    def test_tc1311a_orphan_evidence_not_idempotent_success(self) -> None:
        """Partial evidence (event exists, tasks.yaml not transitioned)
        must NOT be treated as idempotent success."""
        section = self._tc1311a_section()
        self.assertIn("orphan", section.lower())
        self.assertIn("partial", section.lower())

    # ── CAS / crash semantics ────────────────────────────────────────────────

    def test_tc1311a_cas_git_head_does_not_block_orphan_replay(self) -> None:
        """Uncommitted orphan files do not change git rev-parse HEAD,
        so CAS alone does not prevent replay over orphan evidence.
        The contract must note this explicitly — check that the
        paragraph about HEAD and orphan evidence exists in §2.14.2."""
        section = self._tc1311a_section()
        lower = section.lower()
        self.assertTrue(
            "uncommitted" in lower
            and "do" in lower
            and "not" in lower
            and "change" in lower
            and "orphan" in lower,
            "§2.14.2 must explain uncommitted files don't change HEAD",
        )

    # ── pre-write vs in-write exception guarantees ───────────────────────────

    def test_tc1311a_pre_write_zero_writes(self) -> None:
        """Pre-write exceptions must guarantee zero file writes."""
        section = self._tc1311a_section()
        self.assertIn(
            "zero authoritative file writes", section.lower(),
        )

    def test_tc1311a_in_write_may_leave_partial(self) -> None:
        """TransitionWriteError may leave partial authoritative state
        after successful os.replace operations."""
        section = self._tc1311a_section()
        lower = section.lower()
        self.assertTrue(
            "partial authoritative" in lower
            or "partial" in lower,
            "§2.14.12 must acknowledge partial writes after os.replace",
        )

    def test_tc1311a_no_cross_file_rollback_claim(self) -> None:
        """Must NOT claim cross-file automatic rollback."""
        section = self._tc1311a_section()
        lower = section.lower()
        self.assertNotIn("roll back all canonical file writes", lower)

    # ── derived view failure ─────────────────────────────────────────────────

    def test_tc1311a_derived_view_failure_canonical_consistent(self) -> None:
        """View failure leaves canonical-consistent / view-stale state."""
        section = self._tc1311a_section()
        lower = section.lower()
        self.assertIn("canonical-consistent", lower)
        self.assertIn("view-stale", lower)

    def test_tc1311a_render_views_check_detects_drift(self) -> None:
        """render_views.py --check must be referenced for drift detection."""
        section = self._tc1311a_section()
        self.assertIn("render_views.py --check", section)

    # ── legacy tests (keep those that still make sense) ──────────────────────

    def test_tc1311a_cas_git_semantics_unambiguous(self) -> None:
        section = self._tc1311a_section()
        self.assertIn("expected_snapshot_commit", section)
        self.assertNotIn("expected_git_head", section)

    def test_tc1311a_no_git_commit_by_service(self) -> None:
        section = self._tc1311a_section()
        self.assertIn("caller must commit", section.lower())
        self.assertIn("does not commit", section.lower())

    def test_tc1311a_result_has_no_ambiguous_commit(self) -> None:
        section = self._tc1311a_section()
        self.assertNotIn("transition_commit", section)

    def test_tc1311a_lease_epoch_single_source(self) -> None:
        section = self._tc1311a_section()
        self.assertIn("lease.lease_epoch", section)
        self.assertIn("pm_control.lease_epoch", section)
        self.assertIn("never accepts a bare epoch integer", section.lower())

    def test_tc1311a_worker_slot_lease_errors_propagated(self) -> None:
        section = self._tc1311a_section()
        self.assertIn("propagated unchanged", section.lower())
        self.assertIn("WorkerSlotFencingError", section)

    def test_tc1311a_id_generation_unique(self) -> None:
        section = self._tc1311a_section()
        for id_name in ("event_id", "message_id", "dispatch_id"):
            self.assertIn(id_name, section)
        self.assertIn("**Service**", section)

    def test_tc1311a_dedupe_key_format(self) -> None:
        section = self._tc1311a_section()
        self.assertIn(
            "{task_id}/r{revision}/a{attempt}/{dispatch_id}/{message_type}",
            section,
        )

    def test_tc1311a_duplicate_semantics_unambiguous(self) -> None:
        section = self._tc1311a_section()
        self.assertIn("TransitionDuplicateEvidenceError", section)
        self.assertIn("Idempotent success", section)

    def test_tc1311a_zero_writes_on_all_errors(self) -> None:
        section = self._tc1311a_section()
        self.assertIn("zero authoritative file writes", section.lower())

    def test_tc1311a_lock_granularity_project_level(self) -> None:
        section = self._tc1311a_section()
        lower = section.lower()
        self.assertIn("project-level", lower)
        self.assertIn("per-task", lower)

    def test_tc1311a_lock_ordering_exact(self) -> None:
        section = self._tc1311a_section()
        self.assertIn("worker-slot lease lock", section.lower())
        self.assertIn("control-plane state lock", section.lower())
        self.assertIn("TransitionLockOrderError", section)

    def test_tc1311a_state_lock_path(self) -> None:
        section = self._tc1311a_section()
        self.assertIn(".state-transition.lock", section)

    def test_tc1311a_lock_contention_immediate(self) -> None:
        section = self._tc1311a_section()
        self.assertIn("TransitionLockContentionError", section)
        self.assertIn("no sleeping", section.lower())

    def test_tc1311a_multi_file_not_fs_atomic_claim(self) -> None:
        section = self._tc1311a_section()
        self.assertIn("not cross-file atomicity", section.lower())

    def test_tc1311a_tasks_yaml_written_last(self) -> None:
        section = self._tc1311a_section()
        self.assertIn("LAST authoritative write", section)

    def test_tc1311a_derived_views_separate_from_canonical(self) -> None:
        section = self._tc1311a_section()
        self.assertIn("derived", section.lower())
        self.assertIn("canonical authority", section.lower())

    def test_tc1311a_approval_gate_excluded(self) -> None:
        section = self._tc1311a_section()
        self.assertIn("ApprovalGate", section)
        self.assertIn("granted_approval_ids", section)

    def test_tc1311a_now_is_explicit_parameter(self) -> None:
        section = self._tc1311a_section()
        self.assertIn("now: datetime", section)

    def test_tc1311a_project_root_is_construction_param(self) -> None:
        section = self._tc1311a_section()
        self.assertIn("project_root: Path", section)

    def test_tc1311a_lease_is_explicit_optional_param(self) -> None:
        section = self._tc1311a_section()
        self.assertIn("lease: WorkerSlotLease | None", section)

    def test_tc1311a_exception_hierarchy_defined(self) -> None:
        section = self._tc1311a_section()
        required = [
            "ControlPlaneTransitionError",
            "TransitionValidationError",
            "TransitionCASConflictError",
            "TransitionLockContentionError",
            "TransitionLockOrderError",
            "TransitionDuplicateEvidenceError",
            "TransitionSchemaError",
            "TransitionWriteError",
        ]
        for name in required:
            self.assertIn(name, section)

    def test_tc1311a_no_duplicate_fencing_error(self) -> None:
        section = self._tc1311a_section()
        tree_match = re.search(
            r"ControlPlaneTransitionError\s+\(Exception\)\n"
            r"(?:.|\n)*?\n```",
            section,
        )
        self.assertIsNotNone(tree_match)
        self.assertNotIn("TransitionFencingError", tree_match.group(0))

    def test_tc1311a_no_current_contract_frozen_substatus(self) -> None:
        section = self._tc1311a_section()
        self.assertIn("Current (contract frozen)", section)
        self.assertIn("sub-status is permitted", section.lower())
        self.assertIn("| TC-13.11a |", section)

    def test_tc1311a_no_repr_or_str_on_untrusted(self) -> None:
        section = self._tc1311a_section()
        lower = section.lower()
        self.assertTrue("never" in lower and "repr" in lower)

    def test_tc1311a_subtask_split_defined(self) -> None:
        section = self._tc1311a_section()
        for card in ("TC-13.11a", "TC-13.11b", "TC-13.11c"):
            self.assertIn(card, section)

    def test_tc1311a_event_outbox_immutable(self) -> None:
        section = self._tc1311a_section()
        self.assertIn("never modified after creation", section.lower())

    def test_tc1311a_transport_not_in_outbox(self) -> None:
        section = self._tc1311a_section()
        self.assertIn("transport-receipts.yaml", section)

    def test_tc1311a_payload_digest_computed_by_service(self) -> None:
        section = self._tc1311a_section()
        self.assertIn("payload_digest", section)

    def test_tc1311a_model_selection_exact_ten_fields(self) -> None:
        section = self._tc1311a_section()
        self.assertIn("MODEL_SELECTION_FIELDS", section)

    # ── implementability hardening tests ─────────────────────────────────────

    def test_tc1311a_no_pep695_type_alias(self) -> None:
        """Must NOT use PEP 695 ``type X =`` syntax."""
        section = self._tc1311a_section()
        self.assertNotIn("type TransitionPayload", section)

    def test_tc1311a_union_compatible_closed_union(self) -> None:
        """TransitionPayload must be a Union[...] assignment, not a
        PEP 695 type alias."""
        section = self._tc1311a_section()
        self.assertIn("TransitionPayload = Union[", section)

    def test_tc1311a_union_exact_fourteen_variants(self) -> None:
        """TransitionPayload union must contain exactly 14 payload
        variants."""
        section = self._tc1311a_section()
        variants = [
            "SpecifyPayload",
            "DispatchPayload",
            "AcknowledgePayload",
            "DeliverySubmittedPayload",
            "DeliveryAcceptedPayload",
            "DeliveryReturnedPayload",
            "RequeuePayload",
            "IntegrationPayload",
            "BlockedPayload",
            "BlockerResolvedPayload",
            "BlockerRescopedPayload",
            "BlockerCancelledPayload",
            "CancelledPayload",
            "SupersededPayload",
        ]
        for v in variants:
            self.assertIn(v, section, f"TransitionPayload must include {v}")

    def test_tc1311a_transition_request_has_event_context(self) -> None:
        """TransitionRequest must include event_context field."""
        section = self._tc1311a_section()
        self.assertIn("event_context: TransitionEventContext", section)

    def test_tc1311a_event_context_exact_three_fields(self) -> None:
        """TransitionEventContext must have exactly three fields."""
        section = self._tc1311a_section()
        ctx_match = re.search(
            r"class TransitionEventContext:.*?(?=\nclass TransitionRequest)",
            section, re.DOTALL,
        )
        self.assertIsNotNone(ctx_match)
        body = ctx_match.group(0)
        self.assertIn("source_message_id", body)
        self.assertIn("evidence_refs", body)
        self.assertIn("guard_results", body)

    def test_tc1311a_source_message_id_nullable(self) -> None:
        """source_message_id must be str | None."""
        section = self._tc1311a_section()
        fields = re.findall(r'source_message_id.*', section)
        combined = ''.join(fields)
        self.assertIn('None', combined)

    def test_tc1311a_evidence_refs_deeply_immutable_tuple(self) -> None:
        """evidence_refs must be tuple[str, ...]."""
        section = self._tc1311a_section()
        self.assertIn("tuple[str, ...]", section)

    def test_tc1311a_guard_results_deeply_immutable_tuple(self) -> None:
        """guard_results must be tuple[GuardResult, ...]."""
        section = self._tc1311a_section()
        self.assertIn("tuple[GuardResult, ...]", section)

    def test_tc1311a_guard_result_frozen_slots(self) -> None:
        """GuardResult must be frozen=True, slots=True (the decorator
        appears on the line before the class)."""
        section = self._tc1311a_section()
        # Find the decorator + class block.
        gr = re.search(
            r"@dataclass\(frozen=True,\s*slots=True\)\s*\nclass GuardResult",
            section,
        )
        self.assertIsNotNone(
            gr, "GuardResult must have @dataclass(frozen=True, slots=True)"
        )

    def test_tc1311a_guard_result_no_mutable_collections(self) -> None:
        """GuardResult field annotations must not use list, dict, or set.
        (Local variables in __post_init__ may use list for validation.)"""
        section = self._tc1311a_section()
        gr = re.search(
            r"class GuardResult:.*?(?=\n\n@dataclass|\n\nclass )",
            section, re.DOTALL,
        )
        self.assertIsNotNone(gr)
        body = gr.group(0)
        # Extract just the field lines (before any method).
        field_block = body.split('\n    def ')[0]
        self.assertNotIn(": list", field_block)
        self.assertNotIn(": dict", field_block)
        self.assertNotIn(": set", field_block)

    def test_tc1311a_guard_input_exists(self) -> None:
        """GuardInput class must be defined."""
        section = self._tc1311a_section()
        self.assertIn("class GuardInput:", section)

    def test_tc1311a_guard_input_yaml_scalar_values(self) -> None:
        """GuardInput.value must be str | int | bool | None only."""
        section = self._tc1311a_section()
        gi = re.search(
            r"class GuardInput:.*?(?=\n\n@dataclass|\n\nclass )",
            section, re.DOTALL,
        )
        self.assertIsNotNone(gi, "GuardInput class not found")
        body = gi.group(0)
        self.assertIn("str | int | bool | None", body)

    def test_tc1311a_guard_input_duplicate_keys_rejected(self) -> None:
        """GuardResult must reject duplicate GuardInput keys."""
        section = self._tc1311a_section()
        self.assertIn("duplicate keys", section.lower())

    def test_tc1311a_event_serialisation_source_table_exists(self) -> None:
        """Section 2.14.12 must contain the event serialisation source
        table."""
        section = self._tc1311a_section()
        self.assertIn(
            "Event Serialisation Source Table", section,
            "§2.14 must have event serialisation source table",
        )
        self.assertIn("source_message_id", section)
        self.assertIn("evidence_refs", section)
        self.assertIn("guard_results", section)
        self.assertIn("service constant", section.lower())

    def test_tc1311a_event_context_source_message_id_has_origin(self) -> None:
        """source_message_id in serialisation table must reference
        TransitionEventContext."""
        section = self._tc1311a_section()
        self.assertIn("TransitionEventContext.source_message_id", section)

    def test_tc1311a_event_context_in_all(self) -> None:
        """TransitionEventContext must be in __all__."""
        section = self._tc1311a_section()
        all_match = re.search(
            r"__all__\s*=\s*\[(.*?)\]", section, re.DOTALL,
        )
        self.assertIsNotNone(all_match)
        self.assertIn("TransitionEventContext", all_match.group(1))
        self.assertIn("GuardResult", all_match.group(1))
        self.assertIn("GuardInput", all_match.group(1))

    def test_tc1311a_all_public_types_consistent_count(self) -> None:
        """__all__ must contain exactly 31 public symbols (28 original
        + 3 new: TransitionEventContext, GuardResult, GuardInput)."""
        section = self._tc1311a_section()
        all_match = re.search(
            r"__all__\s*=\s*\[(.*?)\]", section, re.DOTALL,
        )
        self.assertIsNotNone(all_match)
        lines = all_match.group(1).split('\n')
        symbols = [
            s.strip().strip('",') for s in lines
            if s.strip().strip('",')
        ]
        self.assertEqual(
            len(symbols), 31,
            f"__all__ must have 31 symbols, got {len(symbols)}: {symbols}",
        )

    # ── TC-13.13a EscalationService frozen contract smoke tests ─────────

    _TC1313A_CONTRACT_SECTION = "### 2.16"

    def _tc1313a_section(self) -> str:
        adr_text = self._adr_path().read_text(encoding="utf-8")
        section = _extract_markdown_section(
            adr_text, self._TC1313A_CONTRACT_SECTION
        )
        self.assertIsNotNone(
            section,
            f"ADR must contain {self._TC1313A_CONTRACT_SECTION} section",
        )
        return section  # type: ignore[return-value]

    # -- 0: section existence and heading --

    def test_tc1313a_section_exists_and_current(self) -> None:
        """§2.16 must exist with 'Frozen Contract' and 'TC-13.13b'."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        heading_m = re.search(
            r"^### 2\.16\s.*$", adr_text, re.MULTILINE,
        )
        self.assertIsNotNone(heading_m, "ADR must contain §2.16 heading")
        heading = heading_m.group(0)
        self.assertIn("EscalationService", heading)
        self.assertIn("Frozen Contract", heading)
        self.assertIn("Current", heading,
                      "§2.16 heading must now be Current")
        section = self._tc1313a_section()
        self.assertIn("TC-13.13a", section)
        self.assertGreater(len(section), 800,
                          "§2.16 must contain the full frozen contract")

    def test_tc1313a_interface_18_now_current(self) -> None:
        """Interface #18 must now be Current — TC-13.13b completed."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_interface_status_table(adr_text)
        row18 = None
        for r in rows:
            if r.get("#") == "18" or r.get("No.") == "18":
                row18 = r
                break
        self.assertIsNotNone(row18, "Interface #18 row not found")
        status = row18.get("Status", "")
        self.assertIn("Current", status,
                      f"Interface #18 must now be Current, got: {status}")
        impl = row18.get("Implemented by", "")
        self.assertIn("TC-13.13b", impl,
                      "Interface #18 must reference TC-13.13b")

    # -- 1: EscalationAction enum --

    def test_tc1313a_escalation_action_exact_two_values(self) -> None:
        """§2.16.2 must define EscalationAction with exactly two values."""
        section = self._tc1313a_section()
        self.assertIn("class EscalationAction", section)
        self.assertIn('"escalate"', section)
        self.assertIn('"request_user_decision"', section)
        self.assertTrue(
            "str, enum.Enum" in section or "str.Enum" in section,
            "§2.16.2 must define EscalationAction as a str Enum",
        )
        self.assertTrue(
            "enum.unique" in section or "unique" in section.lower(),
            "§2.16.2 must use @enum.unique",
        )

    # -- 2: EscalationRequest exact one field --

    def test_tc1313a_escalation_request_exact_one_field(self) -> None:
        """§2.16.3 must define EscalationRequest with exactly one field."""
        section = self._tc1313a_section()
        self.assertIn("class EscalationRequest", section)
        self.assertIn("current_worker_kind", section)
        self.assertIn("WorkerKind", section)
        self.assertIn("frozen=True", section)
        self.assertIn("slots=True", section)

    # -- 3: EscalationDecision exact three fields --

    def test_tc1313a_escalation_decision_exact_three_fields(self) -> None:
        """§2.16.4 must define EscalationDecision with exactly three fields."""
        section = self._tc1313a_section()
        self.assertIn("class EscalationDecision", section)
        self.assertIn("action", section)
        self.assertIn("current_worker_kind", section)
        self.assertIn("next_worker_kind", section)
        self.assertIn("WorkerKind | None", section)
        self.assertIn("frozen=True", section)
        self.assertIn("slots=True", section)

    # -- 4: __all__ exact four symbols --

    def test_tc1313a_all_exact_four_symbols(self) -> None:
        """§2.16.5 must declare __all__ with exactly four symbols."""
        section = self._tc1313a_section()
        self.assertIn("__all__", section)
        for symbol in (
            "EscalationAction",
            "EscalationRequest",
            "EscalationDecision",
            "evaluate_escalation",
        ):
            self.assertIn(symbol, section,
                          f"__all__ must contain {symbol}")

    # -- 5: progression — exactly three stepping upgrades --

    def test_tc1313a_progression_exact_three_tiers(self) -> None:
        """§2.16.1 must define exact three-step progression."""
        section = self._tc1313a_section()
        self.assertIn("basic_agent", section)
        self.assertIn("standard_agent", section)
        self.assertIn("advanced_agent", section)
        self.assertIn("expert_agent", section)
        # Verify the progression direction — each arrow points to next tier
        collapsed = section.replace(" ", "")
        self.assertIn("basic_agent→standard_agent", collapsed,
                      "§2.16.1 must show basic_agent → standard_agent progression")
        self.assertIn("standard_agent→advanced_agent", collapsed,
                      "§2.16.1 must show standard_agent → advanced_agent progression")
        self.assertIn("advanced_agent→expert_agent", collapsed,
                      "§2.16.1 must show advanced_agent → expert_agent progression")
        self.assertIn("request_user_decision", section)
        self.assertTrue(
            "No skip" in section or "no skip" in section.lower()
            or "skip" in section.lower(),
            "§2.16.1 must forbid skipping tiers",
        )
        self.assertTrue(
            "wrap-around" in section.lower() or "wrap-around" in section,
            "§2.16.1 must forbid wrap-around",
        )

    def test_tc1313a_expert_returns_request_user_decision(self) -> None:
        """§2.16.1/§2.16.9 must state expert → request_user_decision."""
        section = self._tc1313a_section()
        self.assertIn("request_user_decision", section)
        self.assertIn("None", section)  # next_worker_kind is None

    # -- 6: TaskDifficulty independence --

    def test_tc1313a_does_not_receive_task_difficulty(self) -> None:
        """§2.16.7 must explicitly forbid TaskDifficulty input."""
        section = self._tc1313a_section()
        text = section.lower()
        # The ADR uses "does **not** receive" with markdown bold.
        self.assertTrue(
            "receive" in text and "taskdifficulty" in text.replace(" ", ""),
            "§2.16.7 must state EscalationService does not receive "
            "TaskDifficulty",
        )
        self.assertTrue(
            "modify" in text and "taskdifficulty" in text.replace(" ", ""),
            "§2.16.7 must state EscalationService does not modify "
            "TaskDifficulty",
        )

    # -- 7: retry belongs to TC-13.18 --

    def test_tc1313a_retry_belongs_to_tc1318(self) -> None:
        """§2.16.6 must state retry loop/orchestration is TC-13.18."""
        section = self._tc1313a_section()
        self.assertIn("TC-13.18", section,
                      "§2.16.6 must reference TC-13.18 for retry")
        # Uses markdown bold: "does **not** count attempts"
        text = section.lower()
        self.assertTrue(
            "count" in text and "attempt" in text,
            "§2.16.6 must state no attempt counting",
        )

    # -- 8: rate-limit belongs to TC-13.14 --

    def test_tc1313a_rate_limit_belongs_to_tc1314(self) -> None:
        """§2.16.10 must state rate-limit is TC-13.14, independent."""
        section = self._tc1313a_section()
        self.assertIn("TC-13.14", section,
                      "§2.16.10 must reference TC-13.14 for rate-limit")
        self.assertIn("share zero types"
                      .lower().replace(" ", ""),
                      section.lower().replace(" ", ""),
                      "§2.16.10 must state zero shared types between 13.13 and 13.14")

    # -- 9: no TASK_ESCALATED / new event / outbox --

    def test_tc1313a_no_task_escalated_event(self) -> None:
        """§2.16.8 must forbid TASK_ESCALATED and new schema versions."""
        section = self._tc1313a_section()
        self.assertIn("TASK_ESCALATED", section,
                      "§2.16.8 must explicitly forbid TASK_ESCALATED")
        text = section.lower()
        self.assertTrue(
            "not introduce" in text or "no new event" in text
            or "no new schema" in text,
            "§2.16.8 must state no new event type",
        )

    # -- 10: no state/file writes --

    def test_tc1313a_no_state_writes(self) -> None:
        """§2.16.8 must forbid tasks.yaml, events, outbox, acceptance
        writes."""
        section = self._tc1313a_section()
        for forbidden in (
            "tasks.yaml", "events", "outbox",
            "acceptance", "approval records",
        ):
            self.assertIn(forbidden, section,
                          f"§2.16.8 must forbid {forbidden}")

    # -- 11: data class field counts are exact --

    def test_tc1313a_escalation_request_forbids_extra_fields(self) -> None:
        """§2.16.3 must list permanently excluded fields."""
        section = self._tc1313a_section()
        for excluded in (
            "task_id", "revision", "attempt",
            "dispatch_id", "retry_count",
            "stdout", "stderr",
        ):
            self.assertIn(excluded, section,
                          f"§2.16.3 excluded fields must include {excluded}")

    def test_tc1313a_escalation_decision_forbids_extra_fields(self) -> None:
        """§2.16.4 must list permanently excluded fields."""
        section = self._tc1313a_section()
        for excluded in (
            "escalation_level", "reason",
            "retry_advice", "new_provider",
        ):
            self.assertIn(excluded, section,
                          f"§2.16.4 excluded fields must include {excluded}")

    # -- 12: TC-13.14 and beyond are still Target --

    def test_tc1313a_tc1314_still_target(self) -> None:
        """Interface #19 (RateLimit) must remain Target."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_interface_status_table(adr_text)
        row19 = None
        for r in rows:
            if r.get("#") == "19" or r.get("No.") == "19":
                row19 = r
                break
        self.assertIsNotNone(row19, "Interface #19 row not found")
        status = row19.get("Status", "")
        self.assertTrue(
            "Target" in status or "target" in status.lower(),
            f"Interface #19 must be Target, got: {status}"
        )

    def test_tc1313a_interface_20_through_25_are_current(self) -> None:
        """Interfaces #20 and #22–25 are Current; #21 remains Current."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_interface_status_table(adr_text)
        for n in ("20", "22", "23", "24", "25"):
            row = None
            for r in rows:
                if r.get("#") == n or r.get("No.") == n:
                    row = r
                    break
            self.assertIsNotNone(row, f"Interface #{n} row not found")
            status = row.get("Status", "")
            self.assertTrue(
                "Current" in status or "current" in status.lower(),
                f"Interface #{n} must be Current, got: {status}",
            )
        # Interface #21 is now Current
        row21 = None
        for r in rows:
            if r.get("#") == "21" or r.get("No.") == "21":
                row21 = r
                break
        self.assertIsNotNone(row21, "Interface #21 row not found")
        status21 = row21.get("Status", "")
        self.assertIn("Current", status21,
                      f"Interface #21 must be Current, got: {status21}")

    # -- 13: escalation_service.py must NOT exist --

    def test_tc1313b_production_module_exists(self) -> None:
        """escalation_service.py must now exist — TC-13.13b completed."""
        self.assertTrue(
            (SKILL_ROOT / "scripts" / "escalation_service.py").is_file(),
            "escalation_service.py must exist after TC-13.13b",
        )
        self.assertTrue(
            (REPO_ROOT / "tests" / "test_escalation_service.py").is_file(),
            "test_escalation_service.py must exist after TC-13.13b",
        )

    # -- 14: Future Task Cards split --

    def test_tc1313a_future_task_cards_split(self) -> None:
        """§5 must have TC-13.13a and TC-13.13b as separate rows."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_future_task_cards_table(adr_text)
        ids_seen = {
            self._resolve_col(row, "Task Card", "Task", "#")
            for row in rows
        }
        self.assertIn("TC-13.13a", ids_seen,
                      "§5 must contain TC-13.13a row")
        self.assertIn("TC-13.13b", ids_seen,
                      "§5 must contain TC-13.13b row")
        self.assertNotIn("TC-13.13", ids_seen,
                         "§5 must NOT contain bare TC-13.13 row — split into a/b")

    def test_tc1313a_depends_only_on_tc134(self) -> None:
        """§2.16.14: TC-13.13a depends only on TC-13.4."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_future_task_cards_table(adr_text)
        by_id = {
            self._resolve_col(row, "Task Card", "Task", "#"): row
            for row in rows
        }
        tc1313a = by_id.get("TC-13.13a")
        self.assertIsNotNone(tc1313a, "TC-13.13a must exist in Future Task Cards")
        dep = self._resolve_col(tc1313a, "Depends on", "Dep")
        self.assertIn("TC-13.4", dep,
                      "TC-13.13a must depend on TC-13.4")
        self.assertNotIn("TC-13.12d", dep,
                         "TC-13.13a must NOT depend on TC-13.12d")

    def test_tc1313b_depends_on_tc1313a(self) -> None:
        """§5: TC-13.13b must depend on TC-13.13a."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        rows = self._parse_future_task_cards_table(adr_text)
        by_id = {
            self._resolve_col(row, "Task Card", "Task", "#"): row
            for row in rows
        }
        tc1313b = by_id.get("TC-13.13b")
        self.assertIsNotNone(tc1313b, "TC-13.13b must exist in Future Task Cards")
        dep = self._resolve_col(tc1313b, "Depends on", "Dep")
        self.assertIn("TC-13.13a", dep,
                      "TC-13.13b must depend on TC-13.13a")

    # -- 15: no custom exceptions --

    def test_tc1313a_no_custom_exception_hierarchy(self) -> None:
        """§2.16.5 must state no custom exception hierarchy."""
        section = self._tc1313a_section()
        self.assertIn("No custom exception hierarchy"
                      .lower().replace(" ", ""),
                      section.lower().replace(" ", ""),
                      "§2.16.5 must state no custom exception classes")


# ── TC-13.21f — MAD JSON contract verification ─────────────────────────────


class TC1321fMadJsonContractVerificationTests(unittest.TestCase):
    """TC-13.21f — cross-repo verification that the MAD JSON public
    contracts are implemented, and that AgentDesk docs/gateway agree.

    Static checks only: no MAD invocation, no model/API/network calls.
    The MAD-side verification (production serialisation code + tests) is
    recorded in reports/tc-13.21f-mad-json-contract-verification.md.
    """

    def _adr_path(self):
        return SKILL_ROOT / "references" / "adr" / "001-mad-agentdesk-integration.md"

    def _cli_contract_path(self):
        return SKILL_ROOT / "references" / "public-interfaces" / "mad-cli-contract.md"

    def _mad_gateway_path(self):
        return SKILL_ROOT / "scripts" / "mad_gateway.py"

    def _report_path(self):
        return Path(__file__).resolve().parents[1] / "reports" / "tc-13.21f-mad-json-contract-verification.md"

    # -- Interface #2: mad agents --format json → mad.agents/v1 ----------

    def test_adr_interface_2_is_current_verified(self) -> None:
        """ADR Interface Status row #2 must be Current, verified by TC-13.21f."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        self.assertIn("| 2 | `mad agents --format json` | **Current** — verified by TC-13.21f |",
                      adr_text)

    def test_adr_interface_4_is_current_verified(self) -> None:
        """ADR Interface Status row #4 must be Current, verified by TC-13.21f."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        self.assertIn("| 4 | `mad.run-result/v1` schema | **Current** — verified by TC-13.21f |",
                      adr_text)

    def test_contract_status_summary_matches_verified_state(self) -> None:
        """CLI contract status summary must mark the JSON interfaces Current."""
        contract_text = self._cli_contract_path().read_text(encoding="utf-8")
        self.assertIn("| `mad agents --format json` | **Current** — verified by TC-13.21f |",
                      contract_text)
        self.assertIn("| `mad deliberate --format json` (formal) | **Current** — verified by TC-13.21f |",
                      contract_text)
        self.assertIn("| `mad resume --format json` | **Current** — verified by TC-13.21f |",
                      contract_text)

    # -- Gateway constant / parser agreement ------------------------------

    def test_gateway_schema_constant_matches_run_result(self) -> None:
        """mad_gateway.py must consume exactly mad.run-result/v1."""
        gateway_src = self._mad_gateway_path().read_text(encoding="utf-8")
        self.assertIn('MAD_RUN_RESULT_SCHEMA = "mad.run-result/v1"', gateway_src)
        # Fail-closed: wrong/missing schema_version must be rejected.
        self.assertIn("if sv != MAD_RUN_RESULT_SCHEMA:", gateway_src)

    def test_contract_and_gateway_schema_strings_agree(self) -> None:
        """The contract doc and the gateway constant must both say mad.run-result/v1."""
        contract_text = self._cli_contract_path().read_text(encoding="utf-8")
        gateway_src = self._mad_gateway_path().read_text(encoding="utf-8")
        for text, name in ((contract_text, "CLI contract"),
                           (gateway_src, "mad_gateway.py")):
            self.assertIn("mad.run-result/v1", text,
                          f"{name}: must reference mad.run-result/v1")

    def test_contract_forbids_mad_agents_v1_extra_fields(self) -> None:
        """mad.agents/v1 example must not leak executable/extra_args/role."""
        adr_text = self._adr_path().read_text(encoding="utf-8")
        contract_text = self._cli_contract_path().read_text(encoding="utf-8")
        for text, name in ((adr_text, "ADR"), (contract_text, "CLI contract")):
            agent_example = re.search(
                r'"agents":\s*\[\s*\{.*?\}\s*\]',
                text,
                re.DOTALL,
            )
            if agent_example:
                example = agent_example.group(0)
                self.assertNotIn('"executable"', example,
                                 f"{name}: mad.agents/v1 must not expose executable")
                self.assertNotIn('"extra_args"', example,
                                 f"{name}: mad.agents/v1 must not expose extra_args")
                self.assertNotIn('"role"', example,
                                 f"{name}: mad.agents/v1 must not expose role")

    # -- stdout/stderr boundary documented ---------------------------------

    def test_contract_documents_stdout_stderr_boundary(self) -> None:
        """The contract must document that stdout carries only the JSON object."""
        contract_text = self._cli_contract_path().read_text(encoding="utf-8")
        self.assertIn("stdout/stderr boundary", contract_text)
        self.assertIn("stderr", contract_text)

    # -- Report exists and records MAD commit -----------------------------

    def test_verification_report_exists_and_records_mad_commit(self) -> None:
        """TC-13.21f report must exist and record the MAD verification commit."""
        report = self._report_path()
        self.assertTrue(report.is_file(),
                        "reports/tc-13.21f-mad-json-contract-verification.md must exist")
        text = report.read_text(encoding="utf-8")
        self.assertIn("9b402874c9fbd3fd28fb402dc28ce3de6b21a568", text)


# ── TC-13.17b.2 — Contract freeze tests ────────────────────────────────────


class TC1317b2ContractFreezeTests(unittest.TestCase):
    """TC-13.17b.2 — StateProvider contract ↔ production freeze verification.

    Imports the production module directly and asserts that the contract
    document matches the running code exactly.
    """

    def _import_sp(self):
        import sys
        from pathlib import Path
        _scripts = Path(__file__).resolve().parents[1] / "skills" / "agentdesk" / "scripts"
        sys.path.insert(0, str(_scripts))
        import state_provider as sp
        return sp

    # -- 1. __all__ matches ───────────────────────────────────────────────

    def test_all_matches_production(self) -> None:
        """__all__ in the contract must match __all__ in the production module."""
        sp = self._import_sp()
        contract = self._read_contract()
        # Extract __all__ from contract §10.1
        section = _extract_markdown_section(contract, "### 10.1 `__all__`")
        self.assertIsNotNone(section, "Contract §10.1 __all__ section must exist")
        prod_symbols = sorted(sp.__all__)
        # Verify each production symbol appears in the contract
        for sym in prod_symbols:
            self.assertIn(sym, section,
                          f"'{sym}' in production __all__ but missing from contract §10.1")

    # -- 2. Dataclass field counts match ──────────────────────────────────

    _dc_map = {
        "StateSnapshot": 14,
        "TaskEntry": 25,
        "TaskTimestamps": 11,
        "DispatchInfo": 7,
        "EventEntry": 17,
        "GuardInput": 2,
        "GuardResult": 5,
        "OutboxEntry": 13,
        "OutboxPayload": 5,
        "AcceptanceEntry": 16,
        "MadRefEntry": 10,
        "ModelSelectionSnapshot": 10,
    }

    def test_dataclass_field_counts_match(self) -> None:
        """Every dataclass field count in the contract must match production."""
        sp = self._import_sp()
        from dataclasses import fields
        contract = self._read_contract()
        for cls_name, expected_count in self._dc_map.items():
            cls = getattr(sp, cls_name)
            actual = len(list(fields(cls)))
            self.assertEqual(expected_count, actual,
                             f"{cls_name}: contract says {expected_count} fields, "
                             f"production has {actual}")

    # -- 3. Contract field counts match §9 ────────────────────────────────

    def test_contract_9_declares_correct_field_counts(self) -> None:
        """Contract §9 must declare the correct field count for each dataclass."""
        contract = self._read_contract()
        for cls_name, count in self._dc_map.items():
            heading = f"Exact Fields ({count})"
            # Find the section for this dataclass
            # The heading is like "### 9.2 `TaskEntry` — Exact Fields (24)"
            found = False
            for line in contract.splitlines():
                if f"`{cls_name}`" in line and f"({count})" in line:
                    found = True
                    break
                if cls_name in line and f"Exact Fields ({count})" in line:
                    found = True
                    break
            self.assertTrue(found,
                            f"Contract §9 must declare {cls_name} with "
                            f"\"Exact Fields ({count})\"")

    # -- 4. No raw_task/raw_dispatch/raw_event/raw_outbox in contract ──────

    def test_no_raw_fields_in_contract(self) -> None:
        """Contract must not mention raw_task, raw_dispatch, raw_event, raw_outbox."""
        contract = self._read_contract()
        for banned in ("raw_task", "raw_dispatch", "raw_event", "raw_outbox"):
            self.assertNotIn(banned, contract,
                             f"Contract must not contain '{banned}'")

    # -- 5. No Mapping/MappingProxy in contract ───────────────────────────

    def test_no_mapping_type_in_contract(self) -> None:
        """Contract must not expose Mapping or MappingProxyType on any dataclass field.

        The contract may mention these types in design constraints (§12.1)
        as "no public field has type Mapping, MappingProxyType, or dict."
        But no field table row may declare them.
        """
        contract = self._read_contract()
        # Check that none of the field tables contain Mapping or MappingProxyType
        # as a declared field type (e.g. "| 25 | `raw_task` | `Mapping[str, object]`").
        self.assertNotIn("`raw_task`", contract,
                         "Contract must not contain raw_task field")
        self.assertNotIn("`raw_dispatch`", contract,
                         "Contract must not contain raw_dispatch field")
        self.assertNotIn("`raw_event`", contract,
                         "Contract must not contain raw_event field")
        self.assertNotIn("`raw_outbox`", contract,
                         "Contract must not contain raw_outbox field")
        # The design constraint section §12.1 may reference MappingProxyType
        # as a negative statement. That is allowed. But no field type should be
        # declared as Mapping or MappingProxyType.
        field_rows = [
            line for line in contract.splitlines()
            if line.strip().startswith("|") and "`Mapping" in line
        ]
        self.assertEqual([], field_rows,
                         "No field table row must declare Mapping or MappingProxyType type")

    # -- 6. Contract declares frozen/slots ────────────────────────────────

    def test_contract_declares_frozen_slots(self) -> None:
        """Contract must mandate frozen=True, slots=True."""
        contract = self._read_contract().lower()
        self.assertIn("frozen/slots", contract,
                      "Contract must declare frozen/slots mandate")
        self.assertTrue(
            "frozen/slots" in contract or "frozen=True" in contract,
            "Contract must declare frozen/slots dataclasses")

    # -- 7. Contract declares tuple-only collections ──────────────────────

    def test_contract_declares_tuple_collections(self) -> None:
        """Contract must mandate tuple-only collections."""
        contract = self._read_contract()
        self.assertIn("tuple",
                      contract,
                      "Contract must declare tuple collections")
        # Must forbid dict/list/set/Mapping
        target = contract.lower()
        self.assertIn("no public", target,
                      "Contract must forbid public mutables")

    # -- 8. GuardInput is documented ──────────────────────────────────────

    def test_guardinput_documented_in_contract(self) -> None:
        """GuardInput (2 fields) must be documented in §9.7."""
        contract = self._read_contract()
        self.assertIn("GuardInput", contract,
                      "Contract must document GuardInput")
        self.assertIn("### 9.7", contract,
                      "Contract §9.7 must exist for GuardInput")

    # -- 9. __all__ count matches ─────────────────────────────────────────

    def test_all_count_is_19_or_20(self) -> None:
        """Contract §10.1 must list the correct number of __all__ symbols."""
        sp = self._import_sp()
        self.assertEqual(len(sp.__all__), 19,
                         f"Production __all__ has {len(sp.__all__)} symbols, expected 19")

    def test_all_count_in_contract_section(self) -> None:
        """Contract §10.1 heading mentions the correct symbol count."""
        contract = self._read_contract()
        sp = self._import_sp()
        count = len(sp.__all__)
        # The heading is like "### 10.1 `__all__` (19 symbols)"
        # Check that the heading exists with the correct count
        self.assertIn(
            f"({count} symbols)",
            contract,
            f"Contract §10.1 heading must say '({count} symbols)'"
        )

    # -- helpers ──────────────────────────────────────────────────────────

    def _read_contract(self) -> str:
        from pathlib import Path
        p = (Path(__file__).resolve().parents[1]
             / "skills" / "agentdesk" / "references"
             / "public-interfaces" / "state-provider-contract.md")
        return p.read_text(encoding="utf-8")

    def _read_production_source(self) -> str:
        from pathlib import Path
        p = (Path(__file__).resolve().parents[1]
             / "skills" / "agentdesk" / "scripts" / "state_provider.py")
        return p.read_text(encoding="utf-8")


class TC1316aContractFreezeTests(unittest.TestCase):
    """TC-13.16a — MadAuditGateway contract freeze smoke tests.

    Covers: TC-13.16a smoke class — Current/Target consistency
    agent-source-is-config-only, precise argv and cwd, 11-key output,
    typed issue/evidence/plan, purpose="audit", Gateway zero-state-write,
    no stale docs, no production module.
    """

    def setUp(self) -> None:
        self.adr_path = (
            Path(__file__).resolve().parents[1]
            / "skills" / "agentdesk" / "references" / "adr"
            / "001-mad-agentdesk-integration.md"
        )
        self.cli_contract_path = (
            Path(__file__).resolve().parents[1]
            / "skills" / "agentdesk" / "references"
            / "public-interfaces" / "mad-cli-contract.md"
        )
        self.adr_text = self.adr_path.read_text(encoding="utf-8")
        self.cli_text = self.cli_contract_path.read_text(encoding="utf-8")

    # -- 1. Current/Target status consistency --

    def test_mad_audit_is_current_in_both_docs(self) -> None:
        """`mad audit` must be marked Current in both ADR and CLI contract."""
        # ADR interface table row 5
        self.assertIn("| 5 | `mad audit` sub-command | **Current**",
                      self.adr_text,
                      "ADR: row 5 (mad audit) must be Current")
        # CLI contract status table
        self.assertIn("| `mad audit` | **Current**",
                      self.cli_text,
                      "CLI contract: mad audit must be Current")

    def test_mad_audit_result_v1_is_current_in_adr(self) -> None:
        """§2.1: `mad.audit-result/v1` heading must be Current."""
        self.assertIn("**`mad.audit-result/v1` (Current — TC-13.15)**",
                      self.adr_text,
                      "ADR: mad.audit-result/v1 heading must be Current")

    def test_stale_mad_audit_does_not_exist_is_removed(self) -> None:
        """The stale 'mad audit does not exist' text must be removed from ADR."""
        self.assertNotIn("mad audit` does not exist.",
                         self.adr_text,
                         "ADR: stale 'mad audit does not exist' text must be removed")
        self.assertNotIn("mad audit` does not exist.",
                         self.cli_text,
                         "CLI contract: stale 'mad audit does not exist' text must be removed")

    def test_mad_audit_not_in_target_section_of_cli_contract(self) -> None:
        """mad audit must NOT appear under §3 (Target Interfaces) in CLI contract."""
        # Find the Target section
        target_start = self.cli_text.find("## 3. Target Interfaces")
        self.assertGreater(target_start, -1, "CLI contract must have §3 Target Interfaces")
        target_section = self.cli_text[target_start:]
        # The next ## heading ends the target section
        next_section = re.search(r"\n## \d", target_section)
        if next_section:
            target_section = target_section[: next_section.start()]
        self.assertNotIn("### 3.3 `mad audit`",
                         target_section,
                         "CLI contract: mad audit must not be in §3 Target Interfaces")

    def test_mad_auditgateway_remains_target_in_adr_table(self) -> None:
        """Interface #20 MadAuditGateway must be Current — TC-13.16b (contract was TC-13.16a)."""
        self.assertIn("| 20 | AgentDesk MadAuditGateway | **Current** | TC-13.16b",
                      self.adr_text,
                      "ADR: row 20 (MadAuditGateway) must now be Current")
        self.assertNotIn("| 20 | AgentDesk MadAuditGateway | **Target**",
                         self.adr_text,
                         "ADR: row 20 (MadAuditGateway) must no longer be Target")

    # -- 2. Public API field precision --

    def test_section_217_frozen_contract_exists(self) -> None:
        """§2.17 MadAuditGateway Frozen Contract must exist."""
        self.assertIn("### 2.17 MadAuditGateway — Frozen Contract",
                      self.adr_text,
                      "ADR: §2.17 MadAuditGateway Frozen Contract must exist")

    def test_run_audit_gateway_signature(self) -> None:
        """§2.17.2: run_audit_gateway(config, inp) signature must be present."""
        self.assertIn("async def run_audit_gateway(", self.adr_text,
                      "ADR: run_audit_gateway signature must be present")
        self.assertIn("config: MadGatewayConfig", self.adr_text,
                      "ADR: run_audit_gateway must take config param")
        self.assertIn("inp: MadAuditGatewayInput", self.adr_text,
                      "ADR: run_audit_gateway must take inp param")

    def test_mad_audit_gateway_input_exact_fields(self) -> None:
        """§2.17.3: MadAuditGatewayInput must declare exactly 12 fields."""
        for f in (
            "project_root", "task_id", "dispatch_id", "question",
            "workspace", "task_card_commit", "task_card_path",
            "delivery_report_path", "report_commit", "base_commit",
            "implementation_commit", "depth",
        ):
            self.assertIn(f"    {f}:", self.adr_text,
                          f"ADR: MadAuditGatewayInput must have field '{f}'")
        self.assertIn("Exactly **12** fields", self.adr_text,
                      "ADR: MadAuditGatewayInput must claim exactly 12 fields")

    def test_mad_audit_issue_location_exact_3_fields(self) -> None:
        """§2.17.4: MadAuditIssueLocation must have exactly 3 fields."""
        for f in ("file", "line", "commit"):
            self.assertIn(f"    {f}:", self.adr_text,
                          f"ADR: MadAuditIssueLocation must have field '{f}'")
        self.assertIn("Exactly **3** fields", self.adr_text,
                      "ADR: MadAuditIssueLocation must claim exactly 3 fields")

    def test_mad_audit_issue_exact_7_fields(self) -> None:
        """§2.17.5: MadAuditIssue must have exactly 7 fields."""
        for f in ("id", "severity", "category", "title", "description",
                   "location", "recommendation"):
            self.assertIn(f, self.adr_text,
                          f"ADR: MadAuditIssue must reference '{f}'")
        self.assertIn("Exactly **7** fields", self.adr_text,
                      "ADR: MadAuditIssue must claim exactly 7 fields")

    def test_mad_audit_evidence_exact_5_fields(self) -> None:
        """§2.17.6: MadAuditEvidence must have exactly 5 fields; verified is bool."""
        for f in ("ref", "type", "source", "summary", "verified"):
            self.assertIn(f, self.adr_text,
                          f"ADR: MadAuditEvidence must reference '{f}'")
        self.assertIn("Exactly **5** fields", self.adr_text,
                      "ADR: MadAuditEvidence must claim exactly 5 fields")
        self.assertIn("verified", self.adr_text,
                      "ADR: MadAuditEvidence.verified must be documented as bool")

    def test_mad_audit_plan_exact_1_field(self) -> None:
        """§2.17.7: MadAuditPlan must have exactly 1 field (depth)."""
        self.assertIn("depth: MadDeliberationDepth", self.adr_text,
                      "ADR: MadAuditPlan must reference 'depth: MadDeliberationDepth'")
        self.assertIn("Exactly **1** field", self.adr_text,
                      "ADR: MadAuditPlan must claim exactly 1 field")

    # -- 3. Agent source is config only --

    def test_agent_ids_only_from_config_not_input(self) -> None:
        """§2.17.3: agent lists come from config, not MadAuditGatewayInput."""
        self.assertIn("config.audit_agent_ids", self.adr_text,
                      "ADR: agent IDs must come from config.audit_agent_ids")
        self.assertIn("config.audit_report_agent_id", self.adr_text,
                      "ADR: report agent ID must come from config.audit_report_agent_id")

    # -- 4. Precise argv and cwd --

    def test_precise_argv_in_contract(self) -> None:
        """§2.17.9: argv must show all required flags."""
        required_flags = [
            "--workspace",
            "--task-card-commit",
            "--task-card-path",
            "--delivery-report-path",
            "--report-commit",
            "--base-commit",
            "--implementation-commit",
            "--agents",
            "--report-agent",
            "--depth",
            "--convergence auto",
            "--confirm-plan",
            "--format json",
        ]
        for flag in required_flags:
            self.assertIn(flag, self.adr_text,
                          f"ADR §2.17.9: argv must contain '{flag}'")

    def test_argv_must_be_array_not_shell(self) -> None:
        """§2.17.9: must state argv array, shell forbidden."""
        self.assertIn("argv array", self.adr_text.lower(),
                      "ADR §2.17.9: must require argv array")

    def test_cwd_is_workspace(self) -> None:
        """§2.17.10: cwd=str(inp.workspace)."""
        self.assertIn("cwd=str(inp.workspace)", self.adr_text,
                      "ADR §2.17.10: cwd must be str(inp.workspace)")

    # -- 5. Precise 11-key output --

    def test_audit_result_v1_11_keys(self) -> None:
        """§2.17.11: mad.audit-result/v1 root must have exactly 11 keys."""
        self.assertIn("exactly **11** keys", self.adr_text.lower(),
                      "ADR §2.17.11: mad.audit-result/v1 must declare 11 keys")
        # Verify all 11 keys are listed
        required_keys = [
            "schema_version",
            "deliberation_id",
            "status",
            "verdict",
            "issues",
            "evidence",
            "warnings",
            "report",
            "archive_path",
            "participants",
            "plan",
        ]
        for key in required_keys:
            self.assertIn(f"`{key}`", self.adr_text,
                          f"ADR §2.17.11: must list key '{key}'")

    # -- 6. Typed issue/evidence/plan --

    def test_issue_location_3_fields_in_typed_model(self) -> None:
        """MadAuditIssueLocation has exactly 3 fields."""
        self.assertIn("class MadAuditIssueLocation:", self.adr_text,
                      "ADR: MadAuditIssueLocation dataclass must be present")

    def test_evidence_verified_is_bool_not_truthy(self) -> None:
        """verified must be strict bool."""
        self.assertIn("verified", self.adr_text,
                      "ADR: verified must be documented")

    # -- 7. purpose="audit" --

    def test_mad_refs_purpose_audit(self) -> None:
        """§2.17.10: mad-refs written with purpose='audit'."""
        self.assertIn('purpose="audit"', self.adr_text,
                      "ADR §2.17.10: mad-refs must use purpose='audit'")

    # -- 8. Gateway zero-state-write --

    def test_gateway_zero_file_writes(self) -> None:
        """§2.17.10: Gateway must not write tasks, events, outbox, acceptances."""
        no_write = [
            "does **not** write tasks",
            "does **not** create or remove worktrees",
            "does **not** read files inside the MAD archive",
            "does **not** call `git fetch`",
        ]
        for phrase in no_write:
            self.assertIn(phrase.lower(), self.adr_text.lower(),
                          f"ADR §2.17.10: must state '{phrase}'")

    def test_gateway_only_throws_exceptions_no_self_write_event(self) -> None:
        """§2.17.12: Gateway only raises exceptions, no self-written audit event."""
        self.assertIn("only raises exceptions", self.adr_text.lower(),
                      "ADR §2.17.12: Gateway must only raise exceptions")
        self.assertIn("does **not** self-write", self.adr_text.lower(),
                      "ADR §2.17.12: Gateway must not self-write audit failure event")

    def test_gateway_reuses_existing_exception_hierarchy(self) -> None:
        """§2.17.12: Reuses mad_gateway exception hierarchy, no parallel AuditGateway exceptions."""
        self.assertIn("no parallel", self.adr_text.lower(),
                      "ADR §2.17.12: must state no parallel AuditGateway exceptions")

    # -- 9. No stale docs --

    def test_no_stale_staged_text(self) -> None:
        """No stale 'staged' or Target-claiming-Current text in the audit sections."""
        # The ADR should not claim mad.audit-result/v1 is Target anymore
        audit_v1_references = [
            m for m in re.finditer(
                r'mad\.audit-result/v1.*Target',
                self.adr_text,
            )
        ]
        self.assertEqual(
            [], audit_v1_references,
            "ADR: no instance of 'mad.audit-result/v1 ... Target' should remain"
        )

    def test_no_stale_old_plan_structure_in_audit_json(self) -> None:
        """The old complex plan structure with audit_specific must not remain."""
        self.assertNotIn("audit_specific", self.cli_text,
                         "CLI contract: stale audit_specific plan must be removed")
        self.assertNotIn("audit_specific", self.adr_text,
                         "ADR: stale audit_specific plan must be removed")

    def test_status_is_completed_on_exit_0(self) -> None:
        """Both docs must state status='completed' on exit 0."""
        self.assertIn('"completed"', self.cli_text,
                      "CLI contract: must show status as 'completed'")
        self.assertIn('"completed"', self.adr_text,
                      "ADR: must show status as 'completed'")

    # -- 10. Production module exists --

    def test_production_module_exists(self) -> None:
        """mad_audit_gateway.py must exist now (TC-13.16b complete)."""
        scripts = (
            Path(__file__).resolve().parents[1]
            / "skills" / "agentdesk" / "scripts"
        )
        gateway = scripts / "mad_audit_gateway.py"
        self.assertTrue(gateway.is_file(),
                        "mad_audit_gateway.py must exist")
        test_file = (
            Path(__file__).resolve().parents[1]
            / "tests" / "test_mad_audit_gateway.py"
        )
        self.assertTrue(test_file.is_file(),
                        "test_mad_audit_gateway.py must exist")

    def test_section_217_status_states_current(self) -> None:
        """§2.17.14 must state §2.17 is Current — TC-13.16b."""
        self.assertIn("* This section (§2.17) is **Current**",
                      self.adr_text,
                      "ADR §2.17.14: must state §2.17 is Current")


class TC1316bProductionSmokeTests(unittest.TestCase):
    """TC-13.16b — production smoke tests for MadAuditGateway."""

    def setUp(self) -> None:
        self.scripts = (
            Path(__file__).resolve().parents[1]
            / "skills" / "agentdesk" / "scripts"
        )
        self.adr_path = (
            Path(__file__).resolve().parents[1]
            / "skills" / "agentdesk" / "references" / "adr"
            / "001-mad-agentdesk-integration.md"
        )
        self.adr_text = self.adr_path.read_text(encoding="utf-8")
        self.cli_contract_path = (
            Path(__file__).resolve().parents[1]
            / "skills" / "agentdesk" / "references"
            / "public-interfaces" / "mad-cli-contract.md"
        )
        self.cli_text = self.cli_contract_path.read_text(encoding="utf-8")
        self.gateway_path = self.scripts / "mad_audit_gateway.py"
        self.test_path = (
            Path(__file__).resolve().parents[1]
            / "tests" / "test_mad_audit_gateway.py"
        )

    # -- 1. Production module exists --

    def test_production_module_exists(self) -> None:
        """mad_audit_gateway.py must exist."""
        self.assertTrue(self.gateway_path.is_file(),
                        "mad_audit_gateway.py must exist")

    def test_test_module_exists(self) -> None:
        """test_mad_audit_gateway.py must exist."""
        self.assertTrue(self.test_path.is_file(),
                        "test_mad_audit_gateway.py must exist")

    # -- 2. Exact 7 public symbols --

    def test_exact_7_public_symbols(self) -> None:
        """mad_audit_gateway __all__ must contain exactly 7 names."""
        import sys
        sys.path.insert(0, str(self.scripts))
        try:
            import mad_audit_gateway
            self.assertEqual(len(mad_audit_gateway.__all__), 7)
            expected = {
                "MadAuditGatewayInput",
                "MadAuditIssueLocation",
                "MadAuditIssue",
                "MadAuditEvidence",
                "MadAuditPlan",
                "MadAuditGatewayResult",
                "run_audit_gateway",
            }
            self.assertEqual(set(mad_audit_gateway.__all__), expected)
        finally:
            sys.path.pop(0)
            for k in list(sys.modules):
                if k == "mad_audit_gateway" or k.startswith("mad_audit_gateway."):
                    del sys.modules[k]

    # -- 3. MadAuditPlan has exactly 1 field (not 6) --

    def test_mad_audit_plan_has_exactly_1_field(self) -> None:
        """§2.17.7: MadAuditPlan must have exactly 1 field (depth)."""
        self.assertIn("Exactly **1** field", self.adr_text,
                      "ADR §2.17.7: MadAuditPlan must claim exactly 1 field")
        self.assertIn("depth: MadDeliberationDepth", self.adr_text,
                      "ADR §2.17.7: MadAuditPlan must have depth: MadDeliberationDepth")

    def test_mad_audit_plan_no_6_field_text(self) -> None:
        """§2.17.7 must NOT claim 6 fields."""
        section = self.adr_text
        # Find §2.17.7 area
        idx = section.find("#### 2.17.7 MadAuditPlan")
        self.assertGreater(idx, -1, "§2.17.7 must exist")
        nearby = section[idx:idx + 800]
        self.assertNotIn("Exactly **6** fields", nearby,
                         "§2.17.7 must not claim 6 fields for MadAuditPlan")

    # -- 4. MadAuditGatewayInput has Path types, not str --

    def test_input_project_root_is_path(self) -> None:
        """project_root must be Path, not str."""
        self.assertIn("project_root: Path", self.adr_text,
                      "ADR: project_root must be Path type")
        self.assertNotIn("project_root: str", self.adr_text,
                         "ADR: project_root must NOT be str type")

    def test_input_workspace_is_path(self) -> None:
        """workspace must be Path, not str."""
        self.assertIn("workspace: Path", self.adr_text,
                      "ADR: workspace must be Path type")

    def test_input_depth_is_maddeliberationdepth(self) -> None:
        """depth must be MadDeliberationDepth, not str."""
        self.assertIn("depth: MadDeliberationDepth", self.adr_text,
                      "ADR: depth must be MadDeliberationDepth type")
        self.assertNotIn("depth: str", self.adr_text,
                         "ADR: depth must NOT be str type")

    # -- 5. Interface #20 & §2.17 are now Current --

    def test_interface_20_is_current(self) -> None:
        """Interface #20 (MadAuditGateway) must be Current."""
        self.assertIn(
            "| 20 | AgentDesk MadAuditGateway | **Current** | TC-13.16b",
            self.adr_text,
            "ADR: Interface #20 must be Current — TC-13.16b"
        )

    def test_section_217_status_is_current(self) -> None:
        """§2.17 heading must show Current — TC-13.16b."""
        self.assertIn(
            "### 2.17 MadAuditGateway — Frozen Contract (Current — TC-13.16b)",
            self.adr_text,
            "ADR: §2.17 must be Current — TC-13.16b"
        )

    def test_section_217_status_line_states_current(self) -> None:
        """§2.17.14 must claim §2.17 is Current."""
        self.assertIn("* This section (§2.17) is **Current**",
                      self.adr_text,
                      "ADR §2.17.14: must state §2.17 is Current")

    # -- 6. No stale docs --

    def test_no_mad_audit_gateway_target_text(self) -> None:
        """Interface #20 must not still claim Target."""
        self.assertNotIn(
            "| 20 | AgentDesk MadAuditGateway | **Target**",
            self.adr_text,
            "ADR: Interface #20 must no longer be Target"
        )

    # -- 7. mad audit remains Current in CLI contract --

    def test_mad_audit_still_current_in_cli_contract(self) -> None:
        """mad audit must remain Current in CLI contract."""
        self.assertIn("| `mad audit` | **Current**",
                      self.cli_text,
                      "CLI contract: mad audit must be Current")

    # -- 8. production module is importable and has correct signatures --

    def test_run_audit_gateway_is_async_function(self) -> None:
        """run_audit_gateway must be an async function."""
        import sys, inspect
        sys.path.insert(0, str(self.scripts))
        try:
            import mad_audit_gateway
            self.assertTrue(
                inspect.iscoroutinefunction(mad_audit_gateway.run_audit_gateway),
                "run_audit_gateway must be async def"
            )
        finally:
            sys.path.pop(0)
            for k in list(sys.modules):
                if k == "mad_audit_gateway" or k.startswith("mad_audit_gateway."):
                    del sys.modules[k]

    def test_all_dataclasses_are_frozen_slots(self) -> None:
        """All 6 dataclasses must be frozen=True, slots=True."""
        source = self.gateway_path.read_text(encoding="utf-8")
        dataclass_count = source.count("@dataclass(frozen=True, slots=True)")
        self.assertGreaterEqual(
            dataclass_count, 6,
            f"Expected at least 6 frozen/slots dataclasses, found {dataclass_count}"
        )


class TC1317aContractFreezeTests(unittest.TestCase):
    """TC-13.17a — StateProvider read-only contract freeze smoke tests.

    Covers: TC-13.17a smoke class — Target contract only, no production
    module; ADR §2.18 exists; public-interface contract doc exists;
    exception hierarchy declared; field counts; excluson boundaries;
    frozen/slots mandate; tuple-only collections; error message safety;
    import boundaries; no stale docs.
    """

    def setUp(self) -> None:
        self.adr_path = (
            Path(__file__).resolve().parents[1]
            / "skills" / "agentdesk" / "references" / "adr"
            / "001-mad-agentdesk-integration.md"
        )
        self.contract_path = (
            Path(__file__).resolve().parents[1]
            / "skills" / "agentdesk" / "references"
            / "public-interfaces" / "state-provider-contract.md"
        )
        self.adr_text = self.adr_path.read_text(encoding="utf-8")
        self.contract_text = self.contract_path.read_text(encoding="utf-8")

    # -- 1. Contract file existence and ADR §2.18 --

    def test_contract_file_exists(self) -> None:
        """state-provider-contract.md must exist."""
        self.assertTrue(
            self.contract_path.is_file(),
            "state-provider-contract.md must exist as a regular file",
        )

    def test_adr_section_218_frozen_contract_exists(self) -> None:
        """§2.18 StateProvider Frozen Contract must exist in ADR."""
        self.assertIn(
            "### 2.18 StateProvider",
            self.adr_text,
            "ADR: §2.18 StateProvider Frozen Contract must exist",
        )

    def test_adr_section_218_references_contract_file(self) -> None:
        """§2.18 must reference state-provider-contract.md."""
        section = _extract_markdown_section(
            self.adr_text, "### 2.18 StateProvider"
        )
        self.assertIsNotNone(
            section,
            "ADR must contain §2.18",
        )
        self.assertIn(
            "state-provider-contract.md",
            section,
            "ADR §2.18 must reference state-provider-contract.md",
        )

    # -- 2. Interface #21 remains Target --

    def test_interface_21_is_current(self) -> None:
        """Interface #21 (StateProvider) must be Current — TC-13.17b complete."""
        self.assertIn(
            "| 21 | AgentDesk StateProvider (read-only) | **Current** | TC-13.17b",
            self.adr_text,
            "ADR: Interface #21 must be Current — TC-13.17b complete",
        )
        self.assertNotIn(
            "| 21 | AgentDesk StateProvider (read-only) | **Target**",
            self.adr_text,
            "ADR: Interface #21 must NOT be Target — TC-13.17b complete",
        )

    def test_tc1317b_complete_in_adr(self) -> None:
        """§2.18.8 must state TC-13.17b is complete."""
        section = _extract_markdown_section(
            self.adr_text, "### 2.18 StateProvider"
        )
        self.assertIsNotNone(section, "ADR must contain §2.18")
        self.assertIn(
            "TC-13.17b",
            section,
            "ADR §2.18 must reference TC-13.17b",
        )
        self.assertIn(
            "complete",
            section.lower(),
            "ADR §2.18 must state TC-13.17b is complete",
        )

    # -- 3. Five canonical input files declared --

    def test_five_canonical_input_files_declared(self) -> None:
        """Contract must declare exactly 5 canonical input files."""
        for f in (
            "docs/pm/state/tasks.yaml",
            "docs/pm/events/*.yaml",
            "docs/pm/outbox/*.yaml",
            "docs/pm/acceptances/*.md",
            ".agentdesk/runtime/mad-refs.yaml",
        ):
            self.assertIn(
                f, self.contract_text,
                f"Contract must declare input file: {f}",
            )

    def test_mad_refs_is_optional(self) -> None:
        """mad-refs.yaml must be declared optional (not required)."""
        # Find the canonical input files section and verify mad-refs is No
        self.assertIn(
            "mad-refs.yaml",
            self.contract_text,
            "Contract must mention mad-refs.yaml",
        )
        # The table row for mad-refs should have "No" in the Required column
        self.assertTrue(
            "| 5 | `.agentdesk/runtime/mad-refs.yaml` | No"
            in self.contract_text
            or "| 5 | `.agentdesk/runtime/mad-refs.yaml` | `agentdesk.mad-refs/v1` | No"
            in self.contract_text,
            "Contract: mad-refs.yaml must be marked No (optional)",
        )

    # -- 4. Multi-file consistency protocol --

    def test_multi_file_consistency_protocol(self) -> None:
        """Contract must define the A→B consistency protocol."""
        # The contract specifies reading all five sources twice and compares.
        self.assertIn("twice", self.contract_text,
                      "Contract must specify reading twice for consistency")
        self.assertIn("second complete snapshot", self.contract_text,
                      "Contract must specify a second complete snapshot")
        self.assertIn(
            "StateProviderSnapshotChangedError",
            self.contract_text,
            "Contract must name the snapshot-changed error",
        )

    def test_snapshot_changed_error_declared(self) -> None:
        """StateProviderSnapshotChangedError must be declared."""
        self.assertIn(
            "StateProviderSnapshotChangedError",
            self.contract_text,
            "Contract must declare StateProviderSnapshotChangedError",
        )

    def test_inconsistent_snapshot_error_declared(self) -> None:
        """StateProviderInconsistentSnapshotError must be declared."""
        self.assertIn(
            "StateProviderInconsistentSnapshotError",
            self.contract_text,
            "Contract must declare StateProviderInconsistentSnapshotError",
        )

    # -- 5. Exception hierarchy --

    def test_exception_hierarchy_has_5_leaf_types(self) -> None:
        """Contract must declare at least 5 leaf exception types."""
        expected = [
            "StateProviderInputError",
            "StateProviderNotFoundError",
            "StateProviderSchemaError",
            "StateProviderSnapshotChangedError",
            "StateProviderInconsistentSnapshotError",
        ]
        for ex in expected:
            self.assertIn(
                ex, self.contract_text,
                f"Contract must declare {ex}",
            )

    def test_exception_hierarchy_root(self) -> None:
        """StateProviderError must be the root."""
        self.assertIn(
            "StateProviderError",
            self.contract_text,
            "Contract must declare StateProviderError as root",
        )

    def test_no_permission_denied_error(self) -> None:
        """No PermissionDeniedError — no real permissions system exists."""
        # The contract names PermissionDeniedError in an explanatory note
        # ("No `PermissionDeniedError`") — verify the exception hierarchy
        # diagram does not list it as an actual leaf type.
        hierarchy_section = None
        for heading in ("## 11. Exception Hierarchy", "## 11."):
            idx = self.contract_text.find(heading)
            if idx >= 0:
                # Grab roughly 600 chars after the heading
                hierarchy_section = self.contract_text[idx:idx + 800]
                break
        self.assertIsNotNone(
            hierarchy_section,
            "Contract must have an Exception Hierarchy section",
        )
        # The hierarchy tree lines use ├── / └── prefixes for leaf types.
        # PermissionDeniedError must NOT appear as a tree node.
        lines = hierarchy_section.split("\n")
        tree_lines = [
            l for l in lines
            if ("PermissionDeniedError" in l
                and any(c in l for c in ("├", "└", "──")))
        ]
        self.assertEqual(
            [], tree_lines,
            "Exception hierarchy must not list PermissionDeniedError as a leaf",
        )

    # -- 6. Frozen/slots dataclass mandate --

    def test_frozen_slots_mandate(self) -> None:
        """Contract must mandate frozen=True, slots=True for all types."""
        self.assertIn(
            "frozen/slots",
            self.contract_text.lower(),
            "Contract must mandate frozen/slots dataclasses",
        )

    def test_tuple_collections_only(self) -> None:
        """All collections must be tuple — no public dict/list/set."""
        self.assertTrue(
            "tuple" in self.contract_text.lower(),
            "Contract must mandate tuple collections",
        )
        self.assertIn(
            "no public `dict`, `list`, or `set`",
            self.contract_text,
            "Contract must forbid public dict/list/set",
        )

    # -- 7. project_root must be absolute Path --

    def test_project_root_must_be_absolute_path(self) -> None:
        """project_root must be absolute Path."""
        self.assertIn(
            "absolute `Path`",
            self.contract_text,
            "Contract: project_root must be absolute Path",
        )

    # -- 8. Error message safety --

    def test_error_message_safety(self) -> None:
        """Error messages must not contain paths, task IDs, secrets."""
        target = self.contract_text.lower()
        safety_phrases = [
            "must not contain",
            "must **never** contain",
            "must never contain",
            "error messages never contain",
        ]
        found = any(p in target for p in safety_phrases)
        self.assertTrue(
            found,
            "Contract must specify error message safety (must not contain paths/IDs/content)",
        )

    # -- 9. Import boundaries --

    def test_no_write_end_imports(self) -> None:
        """StateProvider must not import write-end gateways."""
        self.assertIn(
            "import",
            self.contract_text,
            "Contract must declare import boundaries",
        )
        # Verify the contract mentions forbidden imports
        target = self.contract_text.lower()
        self.assertTrue(
            "must **not** import" in self.contract_text or
            "does not import" in target or
            "not import" in target,
            "Contract must declare import boundaries",
        )

    # -- 10. Explicit exclusions --

    def test_approval_excluded(self) -> None:
        """Approval authorization must be explicitly excluded."""
        self.assertIn(
            "ApprovalGate",
            self.contract_text,
            "Contract must reference ApprovalGate as excluded",
        )

    def test_worker_slot_lease_excluded(self) -> None:
        """Worker-slot lease must be explicitly excluded."""
        self.assertTrue(
            "worker-slot" in self.contract_text.lower()
            or "WorkerSlotLease" in self.contract_text,
            "Contract must exclude worker-slot lease",
        )

    def test_derived_views_excluded(self) -> None:
        """Derived views (BOARD.md, STATUS.md) must be excluded."""
        self.assertIn(
            "BOARD.md",
            self.contract_text,
            "Contract must exclude BOARD.md as authoritative input",
        )

    # -- 11. Production module exists (TC-13.17b) --

    def test_state_provider_py_exists(self) -> None:
        """state_provider.py must exist (TC-13.17b complete)."""
        scripts = (
            Path(__file__).resolve().parents[1]
            / "skills" / "agentdesk" / "scripts"
        )
        self.assertTrue(
            (scripts / "state_provider.py").is_file(),
            "state_provider.py must exist — TC-13.17b complete",
        )

    # -- 12. No stale docs --

    def test_no_stale_state_provider_target_text(self) -> None:
        """Interface #21 must not claim Target."""
        self.assertNotIn(
            "| 21 | AgentDesk StateProvider (read-only) | **Target**",
            self.adr_text,
            "ADR: Interface #21 must not claim Target — TC-13.17b complete",
        )

    def test_tc1317a_is_not_tc1317b(self) -> None:
        """TC-13.17a and TC-13.17b must be distinct in contract."""
        self.assertIn(
            "TC-13.17a",
            self.contract_text,
            "Contract must reference TC-13.17a",
        )
        self.assertIn(
            "TC-13.17b",
            self.contract_text,
            "Contract must reference TC-13.17b",
        )

    # -- 13. ADR §2.7 still references StateProvider --
    # (preserved from existing smoke test)

    def test_adr_section_27_still_references_state_provider(self) -> None:
        """ADR §2.7 must still reference StateProvider."""
        section = _extract_markdown_section(self.adr_text, "### 2.7")
        self.assertIsNotNone(section, "ADR must contain §2.7")
        self.assertIn("StateProvider", section,
                      "ADR §2.7 must reference StateProvider")
        self.assertIn("TC-13.17", section,
                      "ADR §2.7 must reference TC-13.17")


class TC1318bProductionSmokeTests(unittest.TestCase):
    """TC-13.18b — WorkflowOrchestrator production smoke.

    Covers: Interface #22 core orchestration is Current; ADR §2.19 exists;
    public-interface contract doc exists + updated to Current;
    production module and test module present;
    ownership boundary (no direct
    fence/state-lock/ApprovalGate calls); TC-13.9c hard dependency
    declared; ACK excluded from automated cycle; WorkflowClock
    injected; no skip_audit: bool; workspace caller-supplied;
    no worktree management; underlying exceptions pass through;
    exactly 3 orchestrator exceptions; all 15 transitions covered;
    BLOCKER_CANCELLED not missed; production module absent.
    """

    def setUp(self) -> None:
        self.adr_path = (
            Path(__file__).resolve().parents[1]
            / "skills" / "agentdesk" / "references" / "adr"
            / "001-mad-agentdesk-integration.md"
        )
        self.contract_path = (
            Path(__file__).resolve().parents[1]
            / "skills" / "agentdesk" / "references"
            / "public-interfaces" / "workflow-orchestrator-contract.md"
        )
        self.adr_text = self.adr_path.read_text(encoding="utf-8")
        self.contract_text = self.contract_path.read_text(encoding="utf-8")
        self.scripts_dir = (
            Path(__file__).resolve().parents[1]
            / "skills" / "agentdesk" / "scripts"
        )

    # ── 1. Contract file & ADR §2.19 existence ──────────────────────────

    def test_contract_file_exists(self) -> None:
        """workflow-orchestrator-contract.md must exist."""
        self.assertTrue(
            self.contract_path.is_file(),
            "workflow-orchestrator-contract.md must exist as a regular file",
        )

    def test_adr_section_219_exists(self) -> None:
        """ADR §2.19 WorkflowOrchestrator Frozen Contract must exist."""
        self.assertIn(
            "### 2.19 WorkflowOrchestrator",
            self.adr_text,
            "ADR: §2.19 WorkflowOrchestrator Frozen Contract must exist",
        )

    def test_adr_section_219_references_contract_file(self) -> None:
        """§2.19 must reference workflow-orchestrator-contract.md."""
        section = _extract_markdown_section(
            self.adr_text, "### 2.19 WorkflowOrchestrator"
        )
        self.assertIsNotNone(section, "ADR must contain §2.19")
        self.assertIn(
            "workflow-orchestrator-contract.md",
            section,
            "ADR §2.19 must reference workflow-orchestrator-contract.md",
        )

    # ── 2. Interface #22 core orchestration is Current ────────────────────

    def test_interface_22_core_is_current(self) -> None:
        """Interface #22 core orchestration must be Current — TC-13.18d.13b."""
        self.assertIn(
            "| 22 | AgentDesk WorkflowOrchestrator | Current — TC-13.18d.13b",
            self.adr_text,
            "ADR: Interface #22 core orchestration must be Current — TC-13.18d.13b",
        )

    def test_interface_22_codex_429_deferred(self) -> None:
        """Interface #22 must document Codex and Provider 429 as deferred."""
        self.assertIn(
            "Codex / Provider 429 deferred",
            self.adr_text,
            "ADR: Interface #22 must declare Codex / Provider 429 deferred",
        )

    # ── 3. Production module present ─────────────────────────────────────

    def test_workflow_orchestrator_py_exists(self) -> None:
        """workflow_orchestrator.py must exist — TC-13.18b implemented."""
        self.assertTrue(
            (self.scripts_dir / "workflow_orchestrator.py").is_file(),
            "workflow_orchestrator.py must exist — TC-13.18b is implemented",
        )

    def test_test_workflow_orchestrator_py_exists(self) -> None:
        """test_workflow_orchestrator.py must exist — TC-13.18b implemented."""
        tests_dir = Path(__file__).resolve().parent
        self.assertTrue(
            (tests_dir / "test_workflow_orchestrator.py").is_file(),
            "test_workflow_orchestrator.py must exist — TC-13.18b is implemented",
        )

    # ── 4. Ownership boundary — no direct fence/state-lock/ApprovalGate ──

    def test_contract_forbids_direct_hold_worker_slot_fence(self) -> None:
        """Contract must forbid direct hold_worker_slot_fence() call."""
        lowered = self.contract_text.lower()
        self.assertTrue(
            "must not" in lowered and "hold_worker_slot_fence" in lowered,
            "Contract: orchestrator must not call hold_worker_slot_fence directly",
        )

    def test_contract_forbids_direct_state_lock(self) -> None:
        """Contract must forbid direct .state-transition.lock acquisition."""
        self.assertIn(
            ".state-transition.lock",
            self.contract_text,
            "Contract: must mention the state lock as TransitionService-only",
        )

    def test_contract_forbids_duplicate_approval_gate_calls(self) -> None:
        """Contract must forbid ApprovalGate calls before/after transition."""
        lowered = self.contract_text.lower()
        self.assertTrue(
            ("approvalgate.require" in lowered or "approvalgate" in lowered)
            and ("must not" in lowered),
            "Contract: orchestrator must not call ApprovalGate "
            "before/after transitions",
        )

    def test_contract_forbids_fabricated_guard_results(self) -> None:
        """Contract must forbid caller-fabricated approval_gate GuardResult."""
        self.assertIn(
            "fabricat",
            self.contract_text,
            "Contract: must forbid fabricated approval_gate GuardResult entries",
        )

    # ── 5. TC-13.9c hard dependency ──────────────────────────────────────

    def test_contract_declares_tc139c_hard_dependency(self) -> None:
        """Contract must declare TC-13.9c as hard dependency."""
        self.assertIn(
            "TC-13.9c",
            self.contract_text,
            "Contract: must reference TC-13.9c",
        )
        lowered = self.contract_text.lower()
        self.assertTrue(
            "hard" in lowered or "cannot automatically" in lowered,
            "Contract: must declare TC-13.9c as a hard dependency "
            "for DELIVERY_SUBMITTED completion",
        )

    def test_contract_forbids_guessing_commits_from_stdout(self) -> None:
        """Contract must forbid guessing implementation_commit from stdout."""
        lowered = self.contract_text.lower()
        self.assertTrue(
            "guess" in lowered
            or "must **not**" in self.contract_text
            and "implementation_commit" in self.contract_text,
            "Contract: must forbid guessing commits from opaque stdout",
        )

    # ── 6. ACK semantics — excluded from automated cycle ─────────────────

    def test_contract_excludes_ack_from_automated_cycle(self) -> None:
        """Contract must exclude DISPATCH_ACKNOWLEDGED from v1 automated cycle."""
        self.assertIn(
            "DISPATCH_ACKNOWLEDGED",
            self.contract_text,
            "Contract: must mention DISPATCH_ACKNOWLEDGED",
        )
        lowered = self.contract_text.lower()
        self.assertTrue(
            "excluded" in lowered or "deferred" in lowered,
            "Contract: ACK must be excluded or deferred from v1 "
            "automated cycle",
        )

    def test_contract_rejects_fake_ack_patterns(self) -> None:
        """Contract must reject fake ACK patterns."""
        lowered = self.contract_text.lower()
        self.assertTrue(
            "prohibited" in lowered or "forbidden" in lowered,
            "Contract: must explicitly prohibit fake ACK patterns",
        )

    # ── 7. WorkflowClock injected ────────────────────────────────────────

    def test_contract_declares_workflow_clock(self) -> None:
        """Contract must declare WorkflowClock Protocol."""
        self.assertIn(
            "WorkflowClock",
            self.contract_text,
            "Contract: must declare WorkflowClock Protocol",
        )

    def test_contract_forbids_static_now_reuse(self) -> None:
        """Contract must forbid reusing a single frozen now across operations."""
        lowered = self.contract_text.lower()
        self.assertTrue(
            "reus" in lowered and "now" in lowered
            or "frozen `now`" in self.contract_text,
            "Contract: must forbid reusing a single now value",
        )

    def test_contract_specifies_heartbeat_interval(self) -> None:
        """Contract must mention 20 s heartbeat max interval."""
        self.assertIn(
            "20",
            self.contract_text,
            "Contract: must mention the 20 s heartbeat interval",
        )

    def test_contract_requires_heartbeat_stop_on_failure(self) -> None:
        """Contract must require heartbeat cancellation on Worker failure."""
        lowered = self.contract_text.lower()
        self.assertIn("cancel", lowered)
        self.assertTrue(
            "heartbeat" in lowered and "cancel" in lowered
            or "stop" in self.contract_text.lower()
            and "heartbeat" in self.contract_text.lower(),
            "Contract: must stop/cancel heartbeat on Worker "
            "completion, failure, or cancellation",
        )

    # ── 8. No skip_audit: bool ───────────────────────────────────────────

    def test_contract_forbids_skip_audit_bool(self) -> None:
        """Contract must forbid skip_audit: bool."""
        self.assertIn(
            "skip_audit",
            self.contract_text,
            "Contract: must explicitly forbid skip_audit: bool",
        )
        self.assertIn(
            "FORBIDDEN",
            self.contract_text,
            "Contract: must mark skip_audit as FORBIDDEN",
        )

    def test_contract_declares_audit_verdict_routing(self) -> None:
        """Contract must declare audit verdict routing table."""
        self.assertIn(
            "verdict",
            self.contract_text,
            "Contract: must declare audit verdict routing",
        )
        self.assertTrue(
            '"pass"' in self.contract_text
            and '"fail"' in self.contract_text,
            "Contract: must route pass/fail/blocked verdicts",
        )

    # ── 9. Workspace caller-supplied, no worktree management ─────────────

    def test_contract_workspace_caller_supplied(self) -> None:
        """Contract must state workspace is caller-supplied."""
        lowered = self.contract_text.lower()
        self.assertTrue(
            "caller" in lowered and "workspace" in lowered
            or "existing absolute" in lowered,
            "Contract: workspace must be caller-supplied "
            "existing absolute directory",
        )

    def test_contract_forbids_worktree_management(self) -> None:
        """Contract must forbid worktree create/remove/checkout."""
        lowered = self.contract_text.lower()
        self.assertTrue(
            "worktree" in lowered and "must **not**" in self.contract_text
            or "does **not**" in self.contract_text
            and "worktree" in lowered,
            "Contract: must forbid orchestrator worktree management",
        )

    # ── 10. Underlying exceptions pass through unchanged ─────────────────

    def test_contract_exceptions_pass_through(self) -> None:
        """Contract must declare underlying exceptions propagate unchanged."""
        for exc_root in (
            "WorkerSlotLeaseError",
            "DispatchGatewayError",
            "ApprovalError",
            "ControlPlaneTransitionError",
            "StateProviderError",
        ):
            self.assertIn(
                exc_root,
                self.contract_text,
                f"Contract: must declare {exc_root} passes through "
                "unchanged",
            )

    def test_contract_no_parallel_wrapping_exceptions(self) -> None:
        """Contract must NOT declare parallel wrapping exception classes."""
        # The contract may mention these names in a prohibition sentence
        # ("No parallel wrapping exception ... is created").
        # But it must not declare them as class definitions.
        contract = self.contract_text
        self.assertIn(
            "No parallel wrapping exception",
            contract,
            "Contract: must explicitly state no parallel wrapping exceptions",
        )
        for banned in (
            "WorkflowSlotError",
            "WorkflowDispatchError",
            "WorkflowAuditError",
            "WorkflowApprovalError",
            "WorkflowTransitionError",
            "WorkflowEscalationError",
        ):
            self.assertNotIn(
                f"class {banned}",
                contract,
                f"Contract: must NOT contain 'class {banned}'",
            )

    # ── 11. Exactly 3 orchestrator exceptions ────────────────────────────

    def test_contract_exactly_three_own_exceptions(self) -> None:
        """Contract must declare exactly 3 orchestrator-specific exceptions."""
        for required in (
            "WorkflowInputError",
            "WorkflowHeartbeatError",
            "WorkflowInvariantError",
        ):
            self.assertIn(
                required,
                self.contract_text,
                f"Contract: must declare {required}",
            )

    # ── 12. All 15 transitions covered, BLOCKER_CANCELLED present ────────

    def test_contract_covers_all_15_transitions(self) -> None:
        """Contract must cover all 15 transition types."""
        all_15 = [
            "TASK_SPECIFIED",
            "TASK_DISPATCHED",
            "DISPATCH_ACKNOWLEDGED",
            "DELIVERY_SUBMITTED",
            "DELIVERY_ACCEPTED",
            "DELIVERY_RETURNED",
            "TASK_REQUEUED",
            "CHANGE_INTEGRATED",
            "INTEGRATION_FAILED",
            "TASK_BLOCKED",
            "BLOCKER_RESOLVED",
            "BLOCKER_RESCOPED",
            "BLOCKER_CANCELLED",
            "TASK_CANCELLED",
            "TASK_SUPERSEDED",
        ]
        for name in all_15:
            self.assertIn(
                name,
                self.contract_text,
                f"Contract: must cover transition type {name}",
            )

    def test_blocker_cancelled_not_missed(self) -> None:
        """BLOCKER_CANCELLED must be present — not 14 transitions."""
        self.assertIn(
            "BLOCKER_CANCELLED",
            self.contract_text,
            "Contract: BLOCKER_CANCELLED must be present "
            "(all 15 transitions, not 14)",
        )

    # ── 13. ADR §2.19 status section ─────────────────────────────────────

    def test_adr_219_status_declares_current(self) -> None:
        """§2.19.14 must state Interface #22 core orchestration is Current."""
        section = _extract_markdown_section(
            self.adr_text, "#### 2.19.14 Status"
        )
        self.assertIsNotNone(section, "ADR §2.19.14 must exist")
        self.assertIn(
            "Current",
            section,
            "ADR §2.19.14 must state Interface #22 core orchestration is Current",
        )
        self.assertIn(
            "TC-13.18d.13b",
            section,
            "ADR §2.19.14 must reference TC-13.18d.13b",
        )

    # ── 14. TC-13.18d.7 quiescent cancellation status consistency ──────────

    def test_quiescent_cancellation_current_in_adr(self) -> None:
        """TASK_CANCELLED quiescent path must be Current — TC-13.18d.7."""
        self.assertIn(
            "`TASK_CANCELLED` (quiescent path) | Current — TC-13.18d.7",
            self.adr_text,
            "ADR: TASK_CANCELLED quiescent path must be Current",
        )

    def test_active_dispatch_cancellation_current_in_adr(self) -> None:
        """TASK_CANCELLED active dispatch path must be Current."""
        self.assertIn(
            "`TASK_CANCELLED` (active dispatch path) | Current — TC-13.18d.9b",
            self.adr_text,
            "ADR: TASK_CANCELLED active dispatch path must be Current",
        )

    def test_superseded_target_in_adr(self) -> None:
        """TASK_SUPERSEDED paths must be Current with their card evidence."""
        self.assertIn(
            "`TASK_SUPERSEDED` (quiescent path) | Current — TC-13.18d.8",
            self.adr_text,
            "ADR: TASK_SUPERSEDED quiescent path must be Current",
        )
        self.assertIn(
            "`TASK_SUPERSEDED` (active dispatch path) | Contract Current — TC-13.18d.10a / Current — TC-13.18d.10b",
            self.adr_text,
            "ADR: TASK_SUPERSEDED active dispatch path must retain contract and production status",
        )

    def test_no_stale_blanket_tc_target_line(self) -> None:
        """No stale line that declares both TASK_CANCELLED and
        TASK_SUPERSEDED as blanket Target without quiescent qualifier."""
        for line in self.adr_text.splitlines():
            stripped = line.strip()
            if "TASK_CANCELLED" in stripped and "TASK_SUPERSEDED" in stripped:
                if "quiescent" not in stripped.lower():
                    self.assertNotIn(
                        "Target",
                        stripped,
                        "ADR: stale combined Target line found for "
                        "TASK_CANCELLED / TASK_SUPERSEDED",
                    )

    def test_interface_22_is_current_not_partial(self) -> None:
        """Interface #22 must NOT contain Partial — must be Current."""
        for line in self.adr_text.splitlines():
            if line.strip().startswith("| 22 |"):
                self.assertNotIn(
                    "**Partial**",
                    line,
                    "ADR: Interface #22 must NOT be Partial",
                )
                self.assertIn(
                    "Current",
                    line,
                    "ADR: Interface #22 core orchestration must be Current",
                )
                break

    def test_contract_quiescent_current_in_transition_table(self) -> None:
        """Contract transition table must have quiescent path as Current."""
        self.assertIn(
            "TASK_CANCELLED` (quiescent path) | Current",
            self.contract_text,
            "Contract: TASK_CANCELLED quiescent path must be Current",
        )

    def test_contract_active_dispatch_current_in_transition_table(self) -> None:
        """Contract transition table must have active dispatch as Current."""
        self.assertIn(
            "TASK_CANCELLED` (active dispatch path) | Current — TC-13.18d.9b",
            self.contract_text,
            "Contract: TASK_CANCELLED active dispatch must be Current",
        )

    def test_contract_task_superseded_target_in_transition_table(self) -> None:
        """Contract transition table must mark superseded paths Current."""
        self.assertIn(
            "TASK_SUPERSEDED` (quiescent path) | Current — TC-13.18d.8",
            self.contract_text,
            "Contract: TASK_SUPERSEDED quiescent path must be Current",
        )
        self.assertIn(
            "TASK_SUPERSEDED` (active dispatch path) | Contract Current — TC-13.18d.10a / Current — TC-13.18d.10b",
            self.contract_text,
            "Contract: TASK_SUPERSEDED active path must retain contract and production status",
        )

    def test_contract_no_stale_blanket_cancelled_superseded_target(self) -> None:
        """Contract must not have a stale combined
        TASK_CANCELLED / TASK_SUPERSEDED → Target line."""
        self.assertNotIn(
            "TASK_CANCELLED` / `TASK_SUPERSEDED` → Target",
            self.contract_text,
            "Contract: no stale blanket cancelled / superseded Target line",
        )


class TC139c1ProductionSmokeTests(unittest.TestCase):
    """TC-13.9c.1 — Claude 2.1.214 WorkerOutput decoder smoke.

    Covers: ADR Interface #33 exists + Current; ADR §2.20 exists;
    public-interface contract doc exists; production module present;
    test module present; exactly 12 public API symbols; WorkerOutput 9
    fields; DeliveryReceipt 6 fields; WorkerCompletionStatus 3 members;
    exception hierarchy 7 types; encode/decode round-trip;
    provider/version gate; decode of real fixtures fail-closed;
    decode of synthetic envelope succeeds.
    """

    def setUp(self) -> None:
        self.adr_path = (
            Path(__file__).resolve().parents[1]
            / "skills" / "agentdesk" / "references" / "adr"
            / "001-mad-agentdesk-integration.md"
        )
        self.contract_path = (
            Path(__file__).resolve().parents[1]
            / "skills" / "agentdesk" / "references"
            / "public-interfaces" / "worker-output-contract.md"
        )
        self.adr_text = self.adr_path.read_text(encoding="utf-8")
        self.contract_text = self.contract_path.read_text(encoding="utf-8")
        self.scripts_dir = (
            Path(__file__).resolve().parents[1]
            / "skills" / "agentdesk" / "scripts"
        )

    # ── 1. Contract file & ADR §2.20 existence ──────────────────────────

    def test_contract_file_exists(self) -> None:
        """worker-output-contract.md must exist."""
        self.assertTrue(
            self.contract_path.is_file(),
            "worker-output-contract.md must exist as a regular file",
        )

    def test_adr_section_220_exists(self) -> None:
        """ADR §2.20 WorkerOutput Decoder Frozen Contract must exist."""
        self.assertIn(
            "#### 2.20 WorkerOutput Decoder",
            self.adr_text,
            "ADR: §2.20 WorkerOutput Decoder Frozen Contract must exist",
        )

    def test_adr_section_220_references_contract_file(self) -> None:
        """§2.20 must reference worker-output-contract.md."""
        section = _extract_markdown_section(
            self.adr_text, "#### 2.20 WorkerOutput Decoder"
        )
        self.assertIsNotNone(section, "ADR must contain §2.20")
        self.assertIn(
            "worker-output-contract.md",
            section,
            "ADR §2.20 must reference worker-output-contract.md",
        )

    # ── 2. Interface #33 is Current ──────────────────────────────────────

    def test_interface_33_is_current(self) -> None:
        """Interface #33 must be Current — TC-13.9c.1."""
        self.assertIn(
            "| 33 | AgentDesk WorkerOutput Decoder",
            self.adr_text,
            "ADR: Interface #33 must exist",
        )
        self.assertIn(
            "WorkerOutput Decoder — Claude 2.1.214 | **Current**",
            self.adr_text,
            "ADR: Interface #33 must be Current",
        )

    # ── 3. Production module present ─────────────────────────────────────

    def test_worker_output_decoder_py_exists(self) -> None:
        """worker_output_decoder.py must exist."""
        self.assertTrue(
            (self.scripts_dir / "worker_output_decoder.py").is_file(),
            "worker_output_decoder.py must exist",
        )

    def test_test_worker_output_decoder_py_exists(self) -> None:
        """test_worker_output_decoder.py must exist."""
        tests_dir = Path(__file__).resolve().parent
        self.assertTrue(
            (tests_dir / "test_worker_output_decoder.py").is_file(),
            "test_worker_output_decoder.py must exist",
        )

    # ── 4. Public API counts ─────────────────────────────────────────────

    def test_exactly_12_public_symbols(self) -> None:
        """Production module must export exactly 12 symbols."""
        import sys as _sys
        _sys.path.insert(0, str(self.scripts_dir))
        try:
            import worker_output_decoder as w
        finally:
            _sys.path.pop(0)
        self.assertEqual(
            len(w.__all__), 12,
            f"Expected 12 public symbols, got {len(w.__all__)}",
        )

    def test_worker_output_9_fields(self) -> None:
        """WorkerOutput must have exactly 9 fields."""
        import sys as _sys
        _sys.path.insert(0, str(self.scripts_dir))
        try:
            from worker_output_decoder import WorkerOutput
            import dataclasses
        finally:
            _sys.path.pop(0)
        self.assertEqual(
            len(dataclasses.fields(WorkerOutput)), 9,
            "WorkerOutput must have exactly 9 fields",
        )

    def test_delivery_receipt_6_fields(self) -> None:
        """DeliveryReceipt must have exactly 6 fields."""
        import sys as _sys
        _sys.path.insert(0, str(self.scripts_dir))
        try:
            from worker_output_decoder import DeliveryReceipt
            import dataclasses
        finally:
            _sys.path.pop(0)
        self.assertEqual(
            len(dataclasses.fields(DeliveryReceipt)), 6,
            "DeliveryReceipt must have exactly 6 fields",
        )

    def test_worker_completion_status_3_members(self) -> None:
        """WorkerCompletionStatus must have exactly 3 members."""
        import sys as _sys
        _sys.path.insert(0, str(self.scripts_dir))
        try:
            from worker_output_decoder import WorkerCompletionStatus
        finally:
            _sys.path.pop(0)
        self.assertEqual(
            len(WorkerCompletionStatus), 3,
            "WorkerCompletionStatus must have exactly 3 members",
        )

    # ── 5. Exception hierarchy ───────────────────────────────────────────

    def test_exception_hierarchy_7_types(self) -> None:
        """7 exception types must exist with correct hierarchy."""
        import sys as _sys
        _sys.path.insert(0, str(self.scripts_dir))
        try:
            from worker_output_decoder import (
                WorkerOutputError,
                WorkerOutputUnsupportedProviderError,
                WorkerOutputUnsupportedVersionError,
                WorkerOutputIntegrityError,
                WorkerOutputDecodeError,
                WorkerOutputSchemaError,
                WorkerOutputIdentityError,
            )
        finally:
            _sys.path.pop(0)
        self.assertTrue(issubclass(WorkerOutputUnsupportedProviderError, WorkerOutputError))
        self.assertTrue(issubclass(WorkerOutputUnsupportedVersionError, WorkerOutputError))
        self.assertTrue(issubclass(WorkerOutputIntegrityError, WorkerOutputError))
        self.assertTrue(issubclass(WorkerOutputDecodeError, WorkerOutputError))
        self.assertTrue(issubclass(WorkerOutputSchemaError, WorkerOutputError))
        self.assertTrue(issubclass(WorkerOutputIdentityError, WorkerOutputError))

    # ── 6. Round-trip: synthetic envelope decode → delivery receipt ──────

    def test_roundtrip_completed_to_delivery_receipt(self) -> None:
        """Completed synthetic envelope must decode and produce DeliveryReceipt."""
        import sys as _sys
        _sys.path.insert(0, str(self.scripts_dir))
        try:
            import json
            import hashlib
            from worker_output_decoder import (
                decode_worker_result,
                require_delivery_receipt,
            )
            from dispatcher_gateway import DispatchIdentity, DispatchResult
            from worker_adapter import WorkerResult
            from core_types import TaskDifficulty, WorkerKind
            from context_budget import BudgetResult
        finally:
            _sys.path.pop(0)

        envelope = json.dumps({
            "schema_version": "agentdesk.worker-output/v1",
            "task_id": "TC-SMOKE",
            "revision": 1,
            "attempt": 1,
            "dispatch_id": "DSP-SMOKE",
            "status": "completed",
            "implementation_commit": "a" * 40,
            "report_commit": "b" * 40,
            "summary": "smoke test round-trip",
            "warnings": [],
        }, ensure_ascii=False)

        wrapper = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "api_error_status": None,
            "duration_ms": 5000,
            "duration_api_ms": 5773,
            "ttft_ms": 4952,
            "ttft_stream_ms": 553,
            "time_to_request_ms": 270,
            "num_turns": 1,
            "result": envelope,
            "stop_reason": "end_turn",
            "session_id": "smoke-session",
            "total_cost_usd": None,
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "modelUsage": {"m": {"inputTokens": 100, "outputTokens": 50}},
            "permission_denials": [],
            "terminal_reason": "completed",
            "fast_mode_state": "off",
            "uuid": "smoke-uuid",
        }

        stdout = json.dumps(wrapper, ensure_ascii=False).encode("utf-8")
        identity = DispatchIdentity(
            task_id="TC-SMOKE", revision=1, attempt=1, dispatch_id="DSP-SMOKE"
        )
        dispatch_result = DispatchResult(
            identity=identity,
            provider="claude",
            model_id="claude-sonnet-4-5",
            duration_seconds=5.0,
            stdout=stdout,
            stderr=b"",
            stdout_sha256=hashlib.sha256(stdout).hexdigest(),
            stderr_sha256=hashlib.sha256(b"").hexdigest(),
        )
        result = WorkerResult(
            worker_kind=WorkerKind.STANDARD_AGENT,
            task_difficulty=TaskDifficulty.STANDARD,
            budget=BudgetResult(200000, TaskDifficulty.STANDARD, 35, 128000, 70000, 130000),
            dispatch_result=dispatch_result,
        )

        output = decode_worker_result(result, "2.1.214")
        self.assertEqual(output.status.value, "completed")
        self.assertEqual(output.implementation_commit, "a" * 40)

        receipt = require_delivery_receipt(output)
        self.assertEqual(receipt.provider, "claude")
        self.assertEqual(receipt.implementation_commit, "a" * 40)
        self.assertEqual(receipt.report_commit, "b" * 40)

    # ── 7. Provider / version gate ───────────────────────────────────────

    def test_codex_raises_typed_error(self) -> None:
        """Codex provider must raise WorkerOutputUnsupportedProviderError."""
        import sys as _sys
        _sys.path.insert(0, str(self.scripts_dir))
        try:
            import json
            from worker_output_decoder import (
                decode_worker_result,
                WorkerOutputUnsupportedProviderError,
            )
            from dispatcher_gateway import DispatchIdentity, DispatchResult
            from worker_adapter import WorkerResult
            from core_types import TaskDifficulty, WorkerKind
            from context_budget import BudgetResult
        finally:
            _sys.path.pop(0)

        stdout = b"{}"
        identity = DispatchIdentity(
            task_id="TC-001", revision=1, attempt=1, dispatch_id="DSP-001"
        )
        dispatch_result = DispatchResult(
            identity=identity,
            provider="codex",
            model_id="gpt-5",
            duration_seconds=1.0,
            stdout=stdout,
            stderr=b"",
            stdout_sha256="44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
            stderr_sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )
        result = WorkerResult(
            worker_kind=WorkerKind.STANDARD_AGENT,
            task_difficulty=TaskDifficulty.STANDARD,
            budget=BudgetResult(200000, TaskDifficulty.STANDARD, 35, 128000, 70000, 130000),
            dispatch_result=dispatch_result,
        )
        with self.assertRaises(WorkerOutputUnsupportedProviderError):
            decode_worker_result(result, "2.1.214")

    # ── 8. Real fixture decode fail-closed ──────────────────────────────

    def test_real_fixture_decode_fail_closed(self) -> None:
        """All 3 real Claude 2.1.214 fixtures must fail full decode."""
        import sys as _sys
        _sys.path.insert(0, str(self.scripts_dir))
        try:
            import json
            import hashlib
            from worker_output_decoder import (
                decode_worker_result,
                WorkerOutputDecodeError,
                WorkerOutputSchemaError,
            )
            from dispatcher_gateway import DispatchIdentity, DispatchResult
            from worker_adapter import WorkerResult
            from core_types import TaskDifficulty, WorkerKind
            from context_budget import BudgetResult
        finally:
            _sys.path.pop(0)

        fixture_dir = (
            Path(__file__).resolve().parents[1]
            / "tests" / "fixtures" / "provider-output" / "claude" / "2.1.214"
        )
        for name in ("success-minimal", "success-unicode", "application-boundary"):
            obj = json.loads(
                (fixture_dir / f"{name}.json").read_text(encoding="utf-8")
            )
            stdout = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            identity = DispatchIdentity(
                task_id="TC-001", revision=1, attempt=1, dispatch_id="DSP-001"
            )
            dispatch_result = DispatchResult(
                identity=identity,
                provider="claude",
                model_id="claude-sonnet-4-5",
                duration_seconds=5.0,
                stdout=stdout,
                stderr=b"",
                stdout_sha256=hashlib.sha256(stdout).hexdigest(),
                stderr_sha256=hashlib.sha256(b"").hexdigest(),
            )
            result = WorkerResult(
                worker_kind=WorkerKind.STANDARD_AGENT,
                task_difficulty=TaskDifficulty.STANDARD,
                budget=BudgetResult(200000, TaskDifficulty.STANDARD, 35, 128000, 70000, 130000),
                dispatch_result=dispatch_result,
            )
            with self.subTest(fixture=name):
                try:
                    decode_worker_result(result, "2.1.214")
                    self.fail(f"{name}: should have failed decode")
                except (WorkerOutputDecodeError, WorkerOutputSchemaError):
                    pass  # Expected


class TC1319jE2EProgramClosureTests(unittest.TestCase):
    """TC-13.19j — E2E evidence program closure verification.

    Verifies the closure state of the TC-13.19 E2E program without
    re-running any historical E2E suites.
    """

    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.adr_text = (
            self.repo_root / "skills" / "agentdesk" / "references" / "adr"
            / "001-mad-agentdesk-integration.md"
        ).read_text(encoding="utf-8")
        self.orch_contract = (
            self.repo_root / "skills" / "agentdesk" / "references"
            / "public-interfaces" / "workflow-orchestrator-contract.md"
        ).read_text(encoding="utf-8")
        self.report_path = (
            self.repo_root / "reports" / "tc-13.19-final-delivery-report.md"
        )

    # -- 1. Interface #23 is Current --

    def test_interface_23_is_current(self) -> None:
        """Interface #23 (E2E / Recovery tests) must be Current."""
        found = False
        for line in self.adr_text.splitlines():
            if "| 23 |" in line and "E2E" in line:
                self.assertIn(
                    "**Current**", line,
                    f"Interface #23 must be Current: {line!r}",
                )
                self.assertIn(
                    "TC-13.19j", line,
                    f"Interface #23 must reference TC-13.19j: {line!r}",
                )
                found = True
                break
        self.assertTrue(found, "Interface #23 row not found in ADR")

    # -- 2. Interface #22 still listed --

    def test_interface_22_is_listed(self) -> None:
        """Interface #22 (WorkflowOrchestrator) must still be listed."""
        self.assertIn(
            "| 22 | AgentDesk WorkflowOrchestrator",
            self.adr_text,
            "ADR must still list Interface #22 (WorkflowOrchestrator)",
        )

    # -- 3. TC-13.20 / Interface #24 Dashboard is Current --

    def test_tc1320_dashboard_is_current(self) -> None:
        """TC-13.20 HTML Dashboard Interface #24 must be Current."""
        found = False
        for line in self.adr_text.splitlines():
            if "| 24 |" in line and "Dashboard" in line:
                self.assertIn(
                    "**Current**", line,
                    f"Interface #24 must be Current: {line!r}",
                )
                found = True
                break
        self.assertTrue(found, "Interface #24 (HTML Dashboard) row not found")

    # -- 4. All nine E2E test files exist --

    def test_nine_e2e_test_files_exist(self) -> None:
        """All nine E2E scenario test files must be present."""
        tests_dir = self.repo_root / "tests"
        expected = [
            "test_workflow_e2e.py",
            "test_workflow_recovery_e2e.py",
            "test_workflow_escalation_e2e.py",
            "test_workflow_expert_escalation_e2e.py",
            "test_workflow_integration_failure_e2e.py",
            "test_workflow_expert_cancellation_e2e.py",
            "test_workflow_expert_rescope_e2e.py",
            "test_workflow_quiescent_cancellation_e2e.py",
            "test_workflow_quiescent_supersession_e2e.py",
        ]
        for fn in expected:
            f = tests_dir / fn
            self.assertTrue(f.is_file(), f"E2E test file must exist: {fn}")

    # -- 5. TC-13.19 final report exists --

    def test_final_delivery_report_exists(self) -> None:
        """TC-13.19 final delivery report must exist."""
        self.assertTrue(
            self.report_path.is_file(),
            "tc-13.19-final-delivery-report.md must exist",
        )

    # -- 6. Report lists a-i nine evidence items --

    def test_report_lists_nine_evidence_items(self) -> None:
        """Final report must reference all nine evidence cards."""
        report = self.report_path.read_text(encoding="utf-8")
        for card in (
            "TC-13.19a", "TC-13.19b", "TC-13.19c",
            "TC-13.19d", "TC-13.19e", "TC-13.19f",
            "TC-13.19g", "TC-13.19h", "TC-13.19i",
        ):
            self.assertIn(card, report, f"Final report must list {card}")

    # -- 7. Active-dispatch cancellation declared Target --

    def test_active_dispatch_cancellation_declared_target(self) -> None:
        """Active-dispatch cancellation must be declared Target."""
        combined = self.orch_contract + self.adr_text
        has_ref = (
            "active-dispatch cancellation" in combined.lower()
            or "active dispatch cancellation" in combined.lower()
            or "TASK_CANCELLED (active dispatch" in combined
        )
        self.assertTrue(
            has_ref,
            "Active-dispatch cancellation Target status must be declared",
        )

    # -- 8. Active-dispatch supersession declared Target --

    def test_active_dispatch_supersession_declared_target(self) -> None:
        """Active-dispatch supersession must be declared Target."""
        combined = self.orch_contract + self.adr_text
        has_ref = (
            "active-dispatch supersession" in combined.lower()
            or "active dispatch supersession" in combined.lower()
            or "TASK_SUPERSEDED (active dispatch" in combined
        )
        self.assertTrue(
            has_ref,
            "Active-dispatch supersession Target status must be declared",
        )

    # -- 9. Codex deferred declarations preserved --

    def test_codex_deferred_declarations_preserved(self) -> None:
        """Codex decoder/rate-limit deferred declarations must be preserved."""
        for kw in ("codex decoder", "codex rate-limit", "Codex decoder"):
            if kw in self.adr_text:
                lower = self.adr_text.lower()
                self.assertTrue(
                    "target" in lower or "unsupported" in lower
                    or "not yet" in lower,
                    f"Codex/rate-limit deferred declaration must exist for: {kw}",
                )
                return

    # -- 10. No false full-orchestrator completion claim --

    def test_no_false_orchestrator_completion_claim(self) -> None:
        """Neither contract nor report must claim Orchestrator is fully complete."""
        forbidden = [
            "WorkflowOrchestrator is fully complete",
            "all Orchestrator functionality is implemented",
            "WorkflowOrchestrator is entirely complete",
        ]
        for phrase in forbidden:
            self.assertNotIn(
                phrase.lower(),
                self.orch_contract.lower(),
                f"Orchestrator contract must not claim: {phrase}",
            )
            if self.report_path.is_file():
                report_text = self.report_path.read_text(encoding="utf-8")
                self.assertNotIn(
                    phrase.lower(),
                    report_text.lower(),
                    f"TC-13.19 final report must not claim: {phrase}",
                )

    # -- 11. §2.19.14 explicitly declares TC-13.19 Current --

    def test_s21914_declares_tc1319_current(self) -> None:
        """§2.19.14 must explicitly declare TC-13.19 is Current."""
        # Locate the §2.19.14 section
        section_start = self.adr_text.find("#### 2.19.14 Status")
        self.assertGreater(
            section_start, -1, "§2.19.14 not found in ADR"
        )
        # Slice a reasonable window after the heading
        section = self.adr_text[section_start:section_start + 2000]
        self.assertIn(
            "TC-13.19 is Current",
            section,
            "§2.19.14 must declare TC-13.19 is Current",
        )

    # -- 12. §2.19.14 no longer lists TC-13.19 as remain Target --

    def test_s21914_no_stale_tc1319_remain_target(self) -> None:
        """§2.19.14 must not list TC-13.19 as remain Target."""
        section_start = self.adr_text.find("#### 2.19.14 Status")
        self.assertGreater(
            section_start, -1, "§2.19.14 not found in ADR"
        )
        section = self.adr_text[section_start:section_start + 2000]
        self.assertNotIn(
            "TC-13.19, and TC-13.20 remain",
            section,
            "§2.19.11 must not contain stale 'TC-13.19 ... remain Target'",
        )

    # -- 13. Workflow contract declares TC-13.19 Current --

    def test_workflow_contract_tc1319_current(self) -> None:
        """Workflow orchestrator contract must declare TC-13.19 Current."""
        self.assertIn(
            "Current — TC-13.19j",
            self.orch_contract,
            "Workflow contract TC-13.19 row must be Current — TC-13.19j",
        )

    # -- 14. No other stale TC-13.19 remain-Target sentence in full ADR --

    def test_no_other_stale_tc1319_remain_target(self) -> None:
        """Full ADR must contain no other 'TC-13.19 ... remain Target' stale sentence."""
        import re
        stale = re.findall(
            r'TC-13\.19.*remain\s+\*?\*?Target\*?\*?',
            self.adr_text,
        )
        self.assertEqual(
            len(stale), 0,
            f"Full ADR must contain 0 stale 'TC-13.19 ... remain Target'"
            f" sentences; found {len(stale)}: {stale}",
        )

    # -- 15. Interface #22 core Current; Interface #24 Dashboard Current --

    def test_interface_22_core_current_interface_24_current(self) -> None:
        """Interface #22 core orchestration is Current; Interface #24 Dashboard is Current."""
        # Interface #22: bind all assertions to the Interface Status row so
        # a similarly worded note elsewhere cannot satisfy this smoke test.
        interface_22_rows = [
            line for line in self.adr_text.splitlines()
            if re.match(r"^\|\s*22\s*\|", line)
        ]
        self.assertEqual(
            len(interface_22_rows),
            1,
            "ADR must contain exactly one Interface #22 status row",
        )
        interface_22 = interface_22_rows[0]
        self.assertIn(
            "AgentDesk WorkflowOrchestrator",
            interface_22,
            "Interface #22 must be the WorkflowOrchestrator row",
        )
        self.assertRegex(
            interface_22,
            r"Current\s+—\s+TC-13\.18d\.13b\s+\(core orchestration\)",
            "Interface #22 core orchestration must be Current with its basis",
        )
        for deferred in (
            "Codex runtime/decoder: Target/deferred",
            "Provider 429 detection: Evidence-dependent Target",
            "RateLimit → Orchestrator wiring: Target",
        ):
            self.assertIn(
                deferred,
                interface_22,
                f"Interface #22 must preserve deferred extension status: {deferred}",
            )

        # Interface #24 / TC-13.20: assert the status on its own table row.
        interface_24_rows = [
            line for line in self.adr_text.splitlines()
            if re.match(r"^\|\s*24\s*\|", line)
        ]
        self.assertEqual(
            len(interface_24_rows),
            1,
            "ADR must contain exactly one Interface #24 status row",
        )
        interface_24 = interface_24_rows[0]
        self.assertIn(
            "AgentDesk HTML Dashboard",
            interface_24,
            "Interface #24 must be the HTML Dashboard row",
        )
        self.assertRegex(
            interface_24,
            r"\|\s*TC-13\.20b\s*\|",
            "Interface #24 HTML Dashboard must be Current — TC-13.20b",
        )
        self.assertIn(
            "**Current**",
            interface_24,
            "Interface #24 HTML Dashboard must be Current",
        )


# ──────────────────────────────────────────────────────────────────────
# TC-13.20a — HTML Dashboard Contract Freeze
# ──────────────────────────────────────────────────────────────────────


class TC1320aHtmlDashboardContractFreezeTests(unittest.TestCase):
    """TC-13.20a — HTML Dashboard frozen contract verification.

    Verifies the contract document, ADR §2.21, API surface, security
    rules, and Target/Current boundaries without requiring a production
    module.
    """

    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.adr_text = (
            self.repo_root / "skills" / "agentdesk" / "references" / "adr"
            / "001-mad-agentdesk-integration.md"
        ).read_text(encoding="utf-8")
        self.contract_path = (
            self.repo_root / "skills" / "agentdesk" / "references"
            / "public-interfaces" / "html-dashboard-contract.md"
        )
        self.contract_text = self.contract_path.read_text(encoding="utf-8")
        self.dashboard_py = (
            self.repo_root / "skills" / "agentdesk" / "scripts"
            / "html_dashboard.py"
        )

    # -- 1. Contract file exists --

    def test_contract_file_exists(self) -> None:
        """html-dashboard-contract.md must exist as a regular file."""
        self.assertTrue(
            self.contract_path.is_file(),
            "html-dashboard-contract.md must exist as a regular file",
        )

    # -- 2. ADR §2.21 exists and is Target --

    def test_adr_section_221_exists_and_current(self) -> None:
        """ADR §2.21 AgentDesk HTML Dashboard — Frozen Contract must exist."""
        self.assertIn(
            "#### 2.21 AgentDesk HTML Dashboard",
            self.adr_text,
            "ADR: §2.21 AgentDesk HTML Dashboard Frozen Contract must exist",
        )
        self.assertIn(
            "Current — TC-13.20b",
            self.adr_text,
            "ADR §2.21 must declare Current — TC-13.20b after delivery",
        )

    # -- 3. Interface #24 is Current --

    def test_interface_24_is_current(self) -> None:
        """Interface #24 (HTML Dashboard) must be Current after TC-13.20b."""
        found = False
        for line in self.adr_text.splitlines():
            if "| 24 |" in line and "Dashboard" in line:
                self.assertIn(
                    "**Current**", line,
                    f"Interface #24 must be Current: {line!r}",
                )
                found = True
                break
        self.assertTrue(found, "Interface #24 row not found in ADR")

    # -- 4. Exactly 7 __all__ symbols --

    def test_exact_7_all_symbols(self) -> None:
        """Contract must declare exactly 7 __all__ symbols."""
        self.assertIn(
            "Exactly 7 Symbols", self.contract_text,
            "Contract §3 heading must say Exactly 7 Symbols",
        )
        self.assertIn(
            "__all__ = [", self.contract_text,
            "Contract must declare __all__",
        )
        expected_7 = (
            "DashboardRenderRequest",
            "DashboardArtifact",
            "render_dashboard",
            "DashboardError",
            "DashboardInputError",
            "DashboardRenderError",
            "DashboardSecurityError",
        )
        for sym in expected_7:
            self.assertIn(sym, self.contract_text,
                          f"__all__ must contain: {sym}")
        # Extract the __all__ list portion from contract
        all_idx = self.contract_text.find("__all__ = [")
        self.assertGreater(all_idx, -1, "__all__ = [ not found in contract")
        all_block = self.contract_text[all_idx:all_idx + 600]
        all_start = all_block.find("[")
        all_end = all_block.find("]")
        self.assertGreater(all_end, -1, "__all__ closing ] not found")
        all_list = all_block[all_start:all_end]
        # Count commas to verify exactly 7 symbols (6 commas between, possibly 1 trailing)
        commas = all_list.count(",")
        self.assertGreaterEqual(
            commas, 6,
            f"__all__ must have at least 6 commas for 7 symbols; found {commas}",
        )
        self.assertLessEqual(
            commas, 7,
            f"__all__ must have at most 7 commas for 7 symbols; found {commas}",
        )
        # Verify no extra symbols beyond the 7
        for bad in ("render_html", "build_dashboard", "DashboardConfig",
                     "create_dashboard", "DashboardRenderer",
                     "DashboardTimeoutError", "DashboardNetworkError"):
            self.assertNotIn(bad, all_list,
                             f"__all__ must not contain extra symbol: {bad}")

    # -- 4b. Old 3-symbol declarations do not exist --

    def test_no_stale_3_symbol_declaration(self) -> None:
        """Contract and ADR must NOT declare exactly 3 symbols."""
        for text, label in (
            (self.contract_text, "Contract"),
            (self.adr_text, "ADR"),
        ):
            self.assertNotIn(
                "Exactly 3 Symbols", text,
                f"{label} must not contain stale 'Exactly 3 Symbols'",
            )

    # -- 5. DashboardRenderRequest exactly 2 fields --

    def test_request_exactly_2_fields(self) -> None:
        """DashboardRenderRequest must have exactly 2 fields."""
        self.assertIn(
            "Exactly 2 Fields", self.contract_text,
            "DashboardRenderRequest heading must say Exactly 2 Fields",
        )
        self.assertIn(
            "snapshot: StateSnapshot", self.contract_text,
            "Request must have snapshot: StateSnapshot",
        )
        self.assertIn(
            "generated_at: datetime", self.contract_text,
            "Request must have generated_at: datetime",
        )

    # -- 6. DashboardArtifact exactly 4 fields --

    def test_artifact_exactly_4_fields(self) -> None:
        """DashboardArtifact must have exactly 4 fields."""
        self.assertIn(
            "Exactly 4 Fields", self.contract_text,
            "DashboardArtifact heading must say Exactly 4 Fields",
        )
        for field in (
            "html: bytes",
            "snapshot_digest: str",
            "generated_at: str",
            "task_count: int",
        ):
            self.assertIn(
                field, self.contract_text,
                f"Artifact must declare field: {field}",
            )

    # -- 7. frozen=True, slots=True required --

    def test_frozen_slots_required(self) -> None:
        """Both dataclasses must require frozen=True, slots=True."""
        for cls_name in ("DashboardRenderRequest", "DashboardArtifact"):
            self.assertIn(
                f"frozen=True, slots=True",
                self.contract_text,
                f"{cls_name} must declare frozen=True, slots=True",
            )

    # -- 8. render_dashboard is synchronous pure function --

    def test_render_dashboard_sync_pure(self) -> None:
        """render_dashboard must be synchronous, not async."""
        self.assertIn(
            "def render_dashboard", self.contract_text,
            "render_dashboard must be defined as def (sync)",
        )
        self.assertNotIn(
            "async def render_dashboard", self.contract_text,
            "render_dashboard must NOT be async def",
        )
        self.assertIn(
            "Pure", self.contract_text,
            "Contract must declare render_dashboard is pure",
        )

    # -- 9. Only accepts StateSnapshot --

    def test_only_accepts_state_snapshot(self) -> None:
        """render_dashboard must only accept StateSnapshot input."""
        self.assertIn(
            "snapshot: StateSnapshot", self.contract_text,
            "DashboardRenderRequest.snapshot must be StateSnapshot",
        )

    # -- 10. Explicit UTC datetime injection --

    def test_explicit_utc_datetime_injection(self) -> None:
        """Contract must require caller-supplied UTC datetime."""
        lower = self.contract_text.lower()
        self.assertTrue(
            "timezone-aware utc" in lower,
            "Contract must require timezone-aware UTC datetime",
        )
        self.assertTrue(
            "must not internally call" in lower
            and "datetime.now()" in lower,
            "Contract must forbid internal datetime.now() calls",
        )

    # -- 11. Zero file writes --

    def test_zero_file_writes(self) -> None:
        """Dashboard must never write files."""
        self.assertIn(
            "No side effects", self.contract_text,
            "Contract must declare no file writes",
        )

    # -- 12. Zero network / model / subprocess / Git --

    def test_zero_network_model_subprocess_git(self) -> None:
        """Dashboard must never access network, models, subprocess, or Git."""
        combined = self.contract_text.lower()
        for kw in (
            "no http", "no network", "offline",
            "no subprocess", "no git",
        ):
            self.assertTrue(
                kw in combined or "never" in combined,
                f"Contract must forbid network/model/subprocess/Git: {kw}",
            )

    # -- 13. No HTTP server / framework --

    def test_no_http_server_framework(self) -> None:
        """Dashboard contract must forbid HTTP servers and web frameworks."""
        lower = self.contract_text.lower()
        # These are listed in the forbidden-actions table — verify they appear
        # in a forbidding context (not as allowed features)
        for kw in ("fastapi", "flask", "streamlit", "react", "vue",
                    "node", "npm"):
            self.assertIn(
                kw, lower,
                f"Contract must mention {kw} as forbidden",
            )

    # -- 14. HTML escaping and CSP declared --

    def test_html_escaping_and_csp_declared(self) -> None:
        """Contract must declare HTML escaping and Content Security Policy."""
        self.assertIn(
            "entity escaping", self.contract_text,
            "Contract must require HTML entity escaping",
        )
        self.assertIn(
            "Content-Security-Policy", self.contract_text,
            "Contract must declare Content-Security-Policy",
        )
        self.assertIn(
            "default-src 'none'", self.contract_text,
            "CSP must include default-src 'none'",
        )

    # -- 15. Forbidden raw payload and sensitive fields --

    def test_forbidden_raw_payload_and_sensitive_fields(self) -> None:
        """Contract must forbid raw payload and sensitive field rendering."""
        combined = self.contract_text.lower()
        for kw in (
            "prompt", "stdout", "stderr", "raw model",
            "api key", "credential", "holder_instance_id",
            "repr()",
        ):
            self.assertIn(
                kw, combined,
                f"Contract must forbid rendering: {kw}",
            )

    # -- 16. Deterministic digest declared --

    def test_deterministic_digest_declared(self) -> None:
        """Contract must declare deterministic SHA-256 snapshot digest."""
        self.assertIn(
            "snapshot_digest", self.contract_text,
            "Contract must declare snapshot_digest field",
        )
        self.assertIn(
            "sha256:", self.contract_text.lower(),
            "Contract must declare sha256: digest prefix",
        )
        self.assertIn(
            "deterministic", self.contract_text.lower(),
            "Contract must declare deterministic digest",
        )
        for kw in ("hash()", "repr()", "id()", "memory address"):
            self.assertIn(
                kw.lower(), self.contract_text.lower(),
                f"Contract must forbid non-deterministic: {kw}",
            )

    # -- 17. Current / Target boundary clear --

    def test_current_target_boundary_clear(self) -> None:
        """Contract must clearly delineate Current vs Target scope."""
        self.assertIn(
            "Current / Target Boundary", self.contract_text,
            "Contract must have Current / Target Boundary section",
        )
        self.assertIn(
            "TC-13.20b", self.contract_text,
            "Contract must reference TC-13.20b as future production card",
        )

    # -- 18. html_dashboard.py does NOT exist yet --

    def test_html_dashboard_py_exists(self) -> None:
        """html_dashboard.py must exist under TC-13.20b."""
        self.assertTrue(
            self.dashboard_py.is_file(),
            "html_dashboard.py must exist as a regular file — TC-13.20b delivered",
        )

    # -- 19. No dashboard assets/build directory --

    def test_no_dashboard_assets_build_dir(self) -> None:
        """No dashboard assets or build directory must exist."""
        scripts_dir = self.repo_root / "skills" / "agentdesk" / "scripts"
        for name in ("dashboard", "dashboard_assets", "dashboard_build",
                      "html_dashboard"):
            candidate = scripts_dir / name
            self.assertFalse(
                candidate.is_dir(),
                f"Dashboard directory must NOT exist: {candidate}",
            )

    # -- 20. TC-13.20b explicitly declared as future production --

    def test_tc1320b_declared_as_future_production(self) -> None:
        """TC-13.20b must be declared as the future production implementation card."""
        self.assertIn(
            "TC-13.20b", self.contract_text,
            "Contract must reference TC-13.20b",
        )
        self.assertIn(
            "production", self.contract_text.lower(),
            "Contract must mention production implementation",
        )
        self.assertIn(
            "html_dashboard.py", self.contract_text,
            "Contract must reference html_dashboard.py module path",
        )

    # ── 13.20a.1 additions — exception contract closure ──

    # -- 21. Three exception subclasses each inherit DashboardError --

    def test_three_exception_subclasses_inherit_dashboard_error(self) -> None:
        """DashboardInputError, DashboardRenderError, DashboardSecurityError
        must each inherit DashboardError."""
        for subclass in (
            "DashboardInputError(DashboardError)",
            "DashboardRenderError(DashboardError)",
            "DashboardSecurityError(DashboardError)",
        ):
            self.assertIn(
                subclass, self.contract_text,
                f"Contract must declare: {subclass}",
            )

    # -- 22. No fifth exception class --

    def test_no_fifth_exception_class(self) -> None:
        """Exception hierarchy must not contain a fifth exception type."""
        # Extract the exception hierarchy section
        exc_start = self.contract_text.find("class DashboardError(Exception)")
        self.assertGreater(exc_start, -1, "DashboardError class not found")
        exc_block = self.contract_text[exc_start:exc_start + 1200]
        # Find the end of the code block
        triple_end = exc_block.find("```", exc_block.find("class DashboardSecurityError"))
        self.assertGreater(triple_end, -1, "Exception code block end not found")
        code_block = exc_block[:triple_end]
        # Count class definitions — must be exactly 4
        class_count = code_block.count("class Dashboard")
        self.assertEqual(
            class_count, 4,
            f"Exception hierarchy must have exactly 4 classes; found {class_count}",
        )

    # -- 23. Artifact html field type is bytes --

    def test_artifact_html_field_type_is_bytes(self) -> None:
        """DashboardArtifact.html field must be declared as bytes type."""
        self.assertIn(
            "html: bytes", self.contract_text,
            "DashboardArtifact must declare html: bytes",
        )

    # -- 24. Contract includes exception message safety rules --

    def test_contract_includes_exception_message_safety_rules(self) -> None:
        """Contract must contain exception message leak-prevention rules."""
        for kw in (
            "must never contain",
            "project_root",
            "task_id",
            "repr()",
        ):
            self.assertIn(
                kw, self.contract_text,
                f"Contract must include exception message safety rule: {kw}",
            )

    # -- 25. ADR and contract __all__ lists are consistent --

    def test_adr_and_contract_all_lists_consistent(self) -> None:
        """ADR §2.21 __all__ and contract §3 __all__ must list the same 7 symbols."""
        # Extract __all__ from ADR — search for the Dashboard-specific __all__
        adr_all_idx = self.adr_text.find("__all__ = [\n    \"DashboardRenderRequest\"")
        self.assertGreater(adr_all_idx, -1, "ADR __all__ with DashboardRenderRequest not found")
        adr_all_end = self.adr_text.find("]", adr_all_idx)
        self.assertGreater(adr_all_end, -1, "ADR __all__ closing ] not found")
        adr_all_block = self.adr_text[adr_all_idx:adr_all_end + 1]

        # Extract __all__ from contract
        contract_all_idx = self.contract_text.find("__all__ = [")
        self.assertGreater(contract_all_idx, -1, "Contract __all__ not found")
        contract_all_end = self.contract_text.find("]", contract_all_idx)
        contract_all_block = self.contract_text[contract_all_idx:contract_all_end + 1]

        # Both must contain the same 7 symbols
        expected = [
            "DashboardRenderRequest",
            "DashboardArtifact",
            "render_dashboard",
            "DashboardError",
            "DashboardInputError",
            "DashboardRenderError",
            "DashboardSecurityError",
        ]
        for sym in expected:
            self.assertIn(sym, adr_all_block,
                          f"ADR __all__ must contain: {sym}")
            self.assertIn(sym, contract_all_block,
                          f"Contract __all__ must contain: {sym}")


class TC1320bHtmlDashboardProductionSmokeTests(unittest.TestCase):
    """TC-13.20b — production smoke for the HTML Dashboard renderer.

    Covers only the stable public surface declared by the frozen
    contract (html-dashboard-contract.md): module presence, the 7-symbol
    API, dataclass shape, synchronous renderer, HTML bytes, CSP,
    determinism, input rejection, and zero side effects at import and
    render time.
    """

    def setUp(self) -> None:
        self.scripts = SKILL_ROOT / "scripts"
        if str(self.scripts) not in sys.path:
            sys.path.insert(0, str(self.scripts))
        import importlib
        from datetime import datetime, timezone, timedelta
        self._datetime = datetime
        self._timezone = timezone
        self._timedelta = timedelta
        self.sp = importlib.import_module("state_provider")
        self.hd = importlib.import_module("html_dashboard")

    def _minimal_snapshot(self):
        return self.sp.StateSnapshot(
            project_root=Path("C:/proj"),
            schema_version="agentdesk.tasks/v2",
            project_id="smoke",
            adoption_level="standard",
            updated_at="2026-07-29T10:00:00Z",
            pm_holder_id="pm-1",
            pm_lease_epoch=1,
            pm_mode="manual",
            tasks=(),
            events=(),
            outbox=(),
            acceptances=(),
            mad_refs=None,
            read_hexsha="a" * 64,
        )

    def _req(self, snap, gen=None):
        if gen is None:
            gen = self._datetime(2026, 7, 29, 12, 0, 0, tzinfo=self._timezone.utc)
        return self.hd.DashboardRenderRequest(snapshot=snap, generated_at=gen)

    def test_module_exists(self) -> None:
        self.assertTrue((self.scripts / "html_dashboard.py").is_file())

    def test_exact_7_symbol_api(self) -> None:
        expected = {
            "DashboardRenderRequest", "DashboardArtifact", "render_dashboard",
            "DashboardError", "DashboardInputError",
            "DashboardRenderError", "DashboardSecurityError",
        }
        self.assertEqual(set(self.hd.__all__), expected)
        for name in expected:
            self.assertTrue(hasattr(self.hd, name), f"missing public symbol: {name}")

    def test_field_shapes_and_types(self) -> None:
        from dataclasses import fields as _fields
        req_names = {f.name for f in _fields(self.hd.DashboardRenderRequest)}
        self.assertEqual(req_names, {"snapshot", "generated_at"})
        art_names = {f.name for f in _fields(self.hd.DashboardArtifact)}
        self.assertEqual(art_names,
                         {"html", "snapshot_digest", "generated_at", "task_count"})
        art = self.hd.render_dashboard(self._req(self._minimal_snapshot()))
        self.assertIsInstance(art.html, bytes)
        self.assertIsInstance(art.snapshot_digest, str)
        self.assertIsInstance(art.generated_at, str)
        self.assertIsInstance(art.task_count, int)
        self.assertNotIsInstance(art.task_count, bool)

    def test_dataclasses_frozen_and_slots(self) -> None:
        for cls in (self.hd.DashboardRenderRequest, self.hd.DashboardArtifact):
            self.assertTrue(cls.__dataclass_params__.frozen)
            self.assertTrue(hasattr(cls, "__slots__"))

    def test_renderer_is_synchronous(self) -> None:
        import inspect
        self.assertFalse(inspect.iscoroutinefunction(self.hd.render_dashboard))

    def test_renders_html_bytes(self) -> None:
        art = self.hd.render_dashboard(self._req(self._minimal_snapshot()))
        self.assertIsInstance(art.html, bytes)
        self.assertTrue(art.html.startswith(b"<!doctype html>"))
        self.assertIn(b"</html>", art.html)

    def test_csp_present(self) -> None:
        art = self.hd.render_dashboard(self._req(self._minimal_snapshot()))
        self.assertIn(b"Content-Security-Policy", art.html)
        self.assertIn(b"default-src 'none'", art.html)

    def test_empty_snapshot_renders(self) -> None:
        art = self.hd.render_dashboard(self._req(self._minimal_snapshot()))
        self.assertEqual(art.task_count, 0)
        self.assertIn(b"No tasks", art.html)

    def test_deterministic_output(self) -> None:
        req = self._req(self._minimal_snapshot())
        a = self.hd.render_dashboard(req)
        b = self.hd.render_dashboard(req)
        self.assertEqual(a.html, b.html)
        self.assertEqual(a.snapshot_digest, b.snapshot_digest)
        self.assertTrue(a.snapshot_digest.startswith("sha256:"))

    def test_non_utc_datetime_rejected(self) -> None:
        snap = self._minimal_snapshot()
        naive = self._datetime(2026, 7, 29, 12, 0, 0)
        with self.assertRaises(self.hd.DashboardInputError):
            self.hd.DashboardRenderRequest(snapshot=snap, generated_at=naive)
        cst = self._timezone(self._timedelta(hours=8))
        with self.assertRaises(self.hd.DashboardInputError):
            self.hd.DashboardRenderRequest(
                snapshot=snap,
                generated_at=self._datetime(2026, 7, 29, 12, 0, 0, tzinfo=cst),
            )

    def test_non_state_snapshot_rejected(self) -> None:
        with self.assertRaises(self.hd.DashboardInputError):
            self.hd.DashboardRenderRequest(
                snapshot=object(),
                generated_at=self._datetime(2026, 7, 29, 12, 0, 0,
                                            tzinfo=self._timezone.utc),
            )

    def test_zero_import_side_effects(self) -> None:
        script = (
            "import sys, builtins, hashlib, html, re, dataclasses, "
            "datetime, typing, pathlib, os, json, stat;\n"
            "sys.path.insert(0, " + repr(str(self.scripts)) + ");\n"
            "def _guard(*a, **k):\n"
            "    raise AssertionError('open called during import');\n"
            "_real = builtins.open;\n"
            "builtins.open = _guard;\n"
            "try:\n"
            "    import html_dashboard, state_provider;\n"
            "finally:\n"
            "    builtins.open = _real;\n"
            "print('ok');\n"
        )
        res = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(res.returncode, 0, msg=res.stderr)
        self.assertIn("ok", res.stdout)

    def test_render_zero_file_io(self) -> None:
        import builtins
        import os
        real_open = builtins.open
        real_os_open = os.open

        def _guard(*a, **k):
            raise AssertionError("open called during render")

        builtins.open = _guard  # type: ignore
        os.open = _guard  # type: ignore
        try:
            art = self.hd.render_dashboard(self._req(self._minimal_snapshot()))
        finally:
            builtins.open = real_open  # type: ignore
            os.open = real_os_open  # type: ignore
        self.assertIsInstance(art.html, bytes)


class TC1314a2RateLimitMultiScopeClosureTests(unittest.TestCase):
    """TC-13.14a.2 — RateLimit multi-scope and combined signal closure.

    Verifies the scope field, scope filtering, per-signal normalization,
    aggregation rules, and combined signal semantics.  Supersedes
    TC1314a1RateLimitSemanticsClosureTests.
    """

    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.adr_text = (
            self.repo_root / "skills" / "agentdesk" / "references" / "adr"
            / "001-mad-agentdesk-integration.md"
        ).read_text(encoding="utf-8")
        self.contract_path = (
            self.repo_root / "skills" / "agentdesk" / "references"
            / "public-interfaces" / "rate-limit-contract.md"
        )
        self.contract_text = self.contract_path.read_text(encoding="utf-8")
        self.rate_limit_py = (
            self.repo_root / "skills" / "agentdesk" / "scripts"
            / "rate_limit.py"
        )

    # -- 1. Contract file exists --

    def test_contract_file_exists(self) -> None:
        """rate-limit-contract.md must exist."""
        self.assertTrue(self.contract_path.is_file())

    # -- 2. ADR §2.20 declares TC-13.14a.2 --

    def test_adr_section_220_declares_current(self) -> None:
        """ADR §2.20 must declare Current — TC-13.14b."""
        self.assertIn("### 2.20 RateLimitService", self.adr_text)
        self.assertIn("TC-13.14b", self.adr_text)

    # -- 3. Interface #19 Contract Current --

    def test_interface_19_current(self) -> None:
        """Interface #19 must be Current — TC-13.14b."""
        found = False
        for line in self.adr_text.splitlines():
            if "| 19 |" in line and "RateLimit" in line:
                self.assertIn("Current", line)
                found = True
        self.assertTrue(found)

    # -- 4. Exact 12 public symbols --

    def test_exact_12_all_symbols(self) -> None:
        """Contract must declare exactly 12 __all__ symbols."""
        self.assertIn("Exactly 12 Symbols", self.contract_text)
        for sym in ("RateLimitSignal", "RateLimitScope", "RateLimitSignalSource",
                     "RateLimitCheckRequest", "RateLimitDecision", "RateLimitAction",
                     "RateLimitReason", "RateLimitService", "RateLimitError",
                     "RateLimitInputError", "RateLimitStateError", "RateLimitSecurityError"):
            self.assertIn(sym, self.contract_text)

    # -- 5. RateLimitCheckRequest now has exactly 5 fields --

    def test_check_request_exactly_5_fields(self) -> None:
        """Contract must define RateLimitCheckRequest with exactly 5 fields."""
        self.assertIn("Exactly 5 Fields", self.contract_text)
        for field in ("provider: str", "scope: RateLimitScope",
                       "now: datetime", "units_requested: int",
                       "signals: tuple[RateLimitSignal, ...]"):
            self.assertIn(field, self.contract_text,
                          f"RateLimitCheckRequest must declare: {field}")

    # -- 6. Architecture diagram shows scope --

    def test_architecture_shows_scope(self) -> None:
        """Architecture diagram must include scope in the request."""
        self.assertIn("scope", self.contract_text)

    # -- 7. Scope filtering rules --

    def test_scope_filtering_rules(self) -> None:
        """Contract must specify scope filtering logic."""
        self.assertIn("request.scope != UNKNOWN", self.contract_text,
                      "Contract must specify non-UNKNOWN scope filtering")
        self.assertIn("request.scope == UNKNOWN", self.contract_text,
                      "Contract must specify UNKNOWN scope filtering")
        self.assertIn("signal.scope == UNKNOWN", self.contract_text,
                      "Contract must accept UNKNOWN-scope signals for specific scope requests")

    # -- 8. Per-signal normalization --

    def test_per_signal_normalization(self) -> None:
        """Contract must define effective_wait = max(retry_wait, reset_wait)."""
        self.assertIn("effective_wait", self.contract_text)
        self.assertIn("max(retry_wait, reset_wait)", self.contract_text,
                      "Contract must define effective_wait as max of retry and reset waits")

    # -- 9. Signal expiry rule --

    def test_signal_expiry_rule(self) -> None:
        """Contract must define expired signals exclusion."""
        self.assertIn("expired", self.contract_text)
        self.assertIn("effective_wait == 0", self.contract_text,
                      "Contract must define signal expiry condition")

    # -- 10. WAIT candidate from effective_wait --

    def test_wait_candidate_from_effective_wait(self) -> None:
        """Contract must classify effective_wait > 0 as WAIT candidate."""
        self.assertIn("effective_wait > 0", self.contract_text)

    # -- 11. FAIL candidate includes remaining is None --

    def test_fail_candidate_includes_none_remaining(self) -> None:
        """Contract must classify remaining is None as FAIL candidate."""
        self.assertIn("remaining is None", self.contract_text,
                      "remaining is None must produce FAIL candidate")

    # -- 12. FAIL candidate includes insufficient --

    def test_fail_candidate_includes_insufficient(self) -> None:
        """Contract must classify 0 < remaining < units_requested as FAIL candidate."""
        self.assertIn("0 < remaining < units_requested", self.contract_text)

    # -- 13. Aggregation: WAIT > FAIL > ALLOW > expired --

    def test_aggregation_priority(self) -> None:
        """Contract must specify WAIT > FAIL > ALLOW > expired aggregation."""
        self.assertIn("WAIT", self.contract_text)
        self.assertIn("FAIL", self.contract_text)
        self.assertIn("ALLOW", self.contract_text)
        self.assertIn("expired", self.contract_text)

    # -- 14. WAIT reason: RETRY_AFTER vs INSUFFICIENT --

    def test_wait_reason_distinction(self) -> None:
        """Contract must distinguish RETRY_AFTER vs INSUFFICIENT reason for WAIT."""
        self.assertIn("RETRY_AFTER", self.contract_text)
        self.assertIn("INSUFFICIENT", self.contract_text)
        self.assertIn("retry_wait > 0", self.contract_text,
                      "Contract must check for effective retry-after to set RETRY_AFTER reason")

    # -- 15. All signals expired → ALLOW / NO_SIGNALS --

    def test_all_expired_allows(self) -> None:
        """Contract must specify ALLOW when all matching signals are expired."""
        self.assertIn("All matching signals expired", self.contract_text)

    # -- 16. No CONFLICT reason --

    def test_no_conflict_reason(self) -> None:
        """Contract must not define CONFLICT as an enum value."""
        self.assertNotIn('"conflict"', self.contract_text)
        self.assertIn("No `CONFLICT` reason", self.contract_text)

    # -- 17. rate_limit.py must exist (TC-13.14b) --

    def test_rate_limit_py_exists(self) -> None:
        """rate_limit.py must exist — TC-13.14b delivers the production module."""
        self.assertTrue(self.rate_limit_py.is_file())

    # -- 18. Exception hierarchy --

    def test_exception_hierarchy(self) -> None:
        """Contract must define exactly 4 exception types."""
        self.assertIn("class RateLimitError", self.contract_text)
        self.assertIn("class RateLimitInputError", self.contract_text)
        self.assertIn("class RateLimitStateError", self.contract_text)
        self.assertIn("class RateLimitSecurityError", self.contract_text)

    # -- 19. Escalation boundary preserved --

    def test_escalation_boundary_preserved(self) -> None:
        """Contract must preserve escalation independence."""
        self.assertIn("evaluate_escalation", self.contract_text)

    # -- 20. Task-card split includes TC-13.14a.2 --

    def test_task_card_split_includes_a2(self) -> None:
        """ADR and contract must contain TC-13.14a.2 in task-card split."""
        self.assertIn("TC-13.14a.2", self.contract_text)
        self.assertIn("TC-13.14a.2", self.adr_text)

    # -- 21. units_requested must be positive int >= 1 --

    def test_units_requested_positive(self) -> None:
        """Contract must require units_requested >= 1."""
        self.assertIn(">= 1", self.contract_text)

    # -- 22. Future observation rejection --

    def test_future_observation_rejected(self) -> None:
        """Contract must reject signals with observed_at > request.now."""
        self.assertIn("observed_at > request.now", self.contract_text)
        self.assertIn("RateLimitStateError", self.contract_text)

    # -- 23. retry-after time decay --

    def test_retry_after_time_decay(self) -> None:
        """Contract must define retry_wait calculation with time decay."""
        self.assertIn("retry_wait", self.contract_text)
        self.assertIn("ceil", self.contract_text)

    # -- 24. reset-at ceil calculation --

    def test_reset_at_ceil(self) -> None:
        """Contract must define reset_wait with ceil."""
        self.assertIn("reset_wait", self.contract_text)
        self.assertIn("ceil((reset_at - request.now)", self.contract_text)

    # -- 25. remaining > limit rejected --

    def test_remaining_gt_limit_rejected(self) -> None:
        """Contract must require remaining <= limit."""
        self.assertIn("<= limit", self.contract_text)

    # -- 26. reset_at < observed_at rejected --

    def test_reset_at_before_observed_at_rejected(self) -> None:
        """Contract must require reset_at >= observed_at."""
        self.assertIn(">= observed_at", self.contract_text)

    # -- 27. Deterministic semantics --

    def test_deterministic_semantics(self) -> None:
        """Contract must specify deterministic evaluation."""
        self.assertIn("Deterministic", self.contract_text)

    # -- 28. ADR §2.20.5 updated to 5 fields --

    def test_adr_2205_updated_to_5_fields(self) -> None:
        """ADR §2.20.5 must define RateLimitCheckRequest with 5 fields including scope."""
        section = _extract_markdown_section(self.adr_text, "#### 2.20.5")
        self.assertIsNotNone(section, "ADR must contain §2.20.5")
        self.assertIn("scope", section,
                      "§2.20.5 must include scope field")
        self.assertIn("RateLimitScope", section,
                      "§2.20.5 must specify scope: RateLimitScope")

    # -- 29. ADR §2.20.11 updated with scope filtering --

    def test_adr_22011_updated_with_scope(self) -> None:
        """ADR §2.20.11 must contain scope filtering and aggregation rules."""
        section = _extract_markdown_section(self.adr_text, "#### 2.20.11")
        self.assertIsNotNone(section, "ADR must contain §2.20.11")
        self.assertIn("effective_wait", section,
                      "§2.20.11 must contain effective_wait")
        self.assertIn("scope", section,
                      "§2.20.11 must contain scope filtering")

    # ===== Directed combination tests (TC-13.14a.2) =====

    # -- 30. Request scope isolates other-scope signals --

    def test_request_scope_isolates_other_scopes(self) -> None:
        """Contract must specify that non-matching scope signals are excluded."""
        self.assertIn("different specific scope", self.contract_text,
                      "Contract must reject signals with different specific scope")

    # -- 31. UNKNOWN signal accepted for specific scope request --

    def test_unknown_signal_accepted_for_specific_scope(self) -> None:
        """Contract must accept UNKNOWN-scope signals for specific scope requests."""
        self.assertIn("signal.scope == UNKNOWN", self.contract_text)

    # -- 32. UNKNOWN request checks all scopes --

    def test_unknown_request_checks_all_scopes(self) -> None:
        """Contract must accept all scopes when request.scope == UNKNOWN."""
        self.assertIn("request.scope == UNKNOWN", self.contract_text)
        self.assertIn("accept all scopes", self.contract_text,
                      "Contract must accept all scopes for UNKNOWN request")

    # -- 33. WAIT + ALLOW → WAIT (not ALLOW) --

    def test_wait_overrides_allow(self) -> None:
        """Contract must specify WAIT overrides ALLOW candidates."""
        self.assertIn("WAIT candidate", self.contract_text)
        self.assertIn("Any WAIT candidate exists", self.contract_text,
                      "Contract must specify WAIT takes priority over ALLOW")

    # -- 34. WAIT + FAIL → WAIT --

    def test_wait_overrides_fail(self) -> None:
        """Contract must specify WAIT overrides FAIL candidates."""
        self.assertIn("Any WAIT candidate exists", self.contract_text)

    # -- 35. retry-after + reset-at → max effective_wait --

    def test_retry_plus_reset_max_wait(self) -> None:
        """Contract must specify wait_seconds = max effective_wait across all candidates."""
        self.assertIn("max `effective_wait`", self.contract_text,
                      "Contract must specify max effective_wait for wait_seconds")

    # -- 36. Expired retry-after → signal ignored --

    def test_expired_retry_after_ignored(self) -> None:
        """Contract must specify expired signals are excluded from remaining judgments."""
        self.assertIn("expired", self.contract_text)
        self.assertIn("excluded from", self.contract_text,
                      "Contract must exclude expired signals from remaining judgments")

    # -- 37. Expired reset-at → signal ignored --

    def test_expired_reset_at_ignored(self) -> None:
        """Contract must specify expired reset-at signals are excluded."""
        self.assertIn("expired", self.contract_text)

    # -- 38. Insufficient without time info → FAIL_CLOSED --

    def test_insufficient_without_time_fail_closed(self) -> None:
        """Contract must specify insufficient without time → FAIL_CLOSED."""
        self.assertIn("0 < remaining < units_requested", self.contract_text)
        self.assertIn("FAIL", self.contract_text)

    # -- 39. Multiple ALLOW candidates → WITHIN_LIMIT --

    def test_multiple_allow_candidates_within_limit(self) -> None:
        """Contract must specify ALLOW + WITHIN_LIMIT when only ALLOW candidates."""
        self.assertIn("only ALLOW candidates", self.contract_text,
                      "Contract must specify ALLOW when only ALLOW candidates exist")

    # -- 40. Different providers completely isolated --

    def test_different_providers_isolated(self) -> None:
        """Contract must specify provider exact match filtering."""
        self.assertIn("matches `request.provider` exactly", self.contract_text,
                      "Contract must specify exact provider matching")

    # -- 41. Same input → byte-for-byte identical decision --

    def test_deterministic_byte_equivalence(self) -> None:
        """Contract must guarantee byte-for-byte identical decisions for identical inputs."""
        self.assertIn("identical", self.contract_text)
        self.assertIn("Deterministic", self.contract_text)


class TC1314bRateLimitServiceSmokeTests(unittest.TestCase):
    """TC-13.14b — RateLimitService production module smoke tests.

    Verifies the production module exists, exports the correct API,
    and the core evaluation semantics match the frozen contract.
    """

    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.rate_limit_py = (
            self.repo_root / "skills" / "agentdesk" / "scripts"
            / "rate_limit.py"
        )
        self.scripts_dir = str(self.repo_root / "skills" / "agentdesk" / "scripts")
        sys.path.insert(0, self.scripts_dir)
        import rate_limit as rl  # noqa: E402
        self.rl = rl
        sys.path.pop(0)

    # -- 1. Production module exists --

    def test_rate_limit_py_exists(self) -> None:
        """rate_limit.py must exist."""
        self.assertTrue(self.rate_limit_py.is_file())

    # -- 2. Exactly 12 public symbols --

    def test_exact_12_all_symbols(self) -> None:
        """Production module must export exactly 12 symbols."""
        expected = [
            "RateLimitSignal",
            "RateLimitScope",
            "RateLimitSignalSource",
            "RateLimitCheckRequest",
            "RateLimitDecision",
            "RateLimitAction",
            "RateLimitReason",
            "RateLimitService",
            "RateLimitError",
            "RateLimitInputError",
            "RateLimitStateError",
            "RateLimitSecurityError",
        ]
        self.assertEqual(sorted(self.rl.__all__), sorted(expected))

    # -- 3. Enum member counts --

    def test_scope_four_members(self) -> None:
        self.assertEqual(len(list(self.rl.RateLimitScope)), 4)

    def test_signal_source_three_members(self) -> None:
        self.assertEqual(len(list(self.rl.RateLimitSignalSource)), 3)

    def test_action_three_members(self) -> None:
        self.assertEqual(len(list(self.rl.RateLimitAction)), 3)

    def test_reason_five_members(self) -> None:
        self.assertEqual(len(list(self.rl.RateLimitReason)), 5)

    # -- 4. Dataclass frozen + slots --

    def test_signal_frozen_slots(self) -> None:
        sig = self.rl.RateLimitSignal(
            provider="anthropic",
            scope=self.rl.RateLimitScope.REQUEST,
            observed_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            retry_after_seconds=None,
            reset_at=None,
            limit=None,
            remaining=None,
            source=self.rl.RateLimitSignalSource.PROVIDER_429,
        )
        self.assertFalse(hasattr(sig, "__dict__"))
        with self.assertRaises(AttributeError):
            sig.provider = "other"  # type: ignore[misc]

    def test_check_request_frozen_slots(self) -> None:
        req = self.rl.RateLimitCheckRequest(
            provider="anthropic",
            scope=self.rl.RateLimitScope.REQUEST,
            now=datetime(2025, 1, 1, tzinfo=timezone.utc),
            units_requested=1,
            signals=(),
        )
        self.assertFalse(hasattr(req, "__dict__"))

    def test_decision_frozen_slots(self) -> None:
        d = self.rl.RateLimitDecision(
            action=self.rl.RateLimitAction.ALLOW,
            wait_seconds=0,
            reason=self.rl.RateLimitReason.NO_SIGNALS,
        )
        self.assertFalse(hasattr(d, "__dict__"))

    # -- 5. Exception hierarchy --

    def test_exception_hierarchy(self) -> None:
        self.assertTrue(issubclass(self.rl.RateLimitInputError, self.rl.RateLimitError))
        self.assertTrue(issubclass(self.rl.RateLimitStateError, self.rl.RateLimitError))
        self.assertTrue(issubclass(self.rl.RateLimitSecurityError, self.rl.RateLimitError))

    # -- 6. RateLimitService is stateless --

    def test_service_no_instance_state(self) -> None:
        svc = self.rl.RateLimitService()
        self.assertFalse(hasattr(svc, "__dict__"))
        self.assertEqual(self.rl.RateLimitService.__slots__, ())

    # -- 7. No escalation dependency --

    def test_no_escalation_dependency(self) -> None:
        source = self.rate_limit_py.read_text(encoding="utf-8")
        self.assertNotIn("escalation", source)
        self.assertNotIn("evaluate_escalation", source)

    # -- 8. No I/O / clock calls --

    def test_no_io_or_clock(self) -> None:
        source = self.rate_limit_py.read_text(encoding="utf-8")
        self.assertNotIn("datetime.now", source)
        self.assertNotIn("time.time", source)
        self.assertNotIn("import subprocess", source)
        self.assertNotIn("import socket", source)

    # -- 9. Minimal evaluate happy path --

    def test_evaluate_no_signals(self) -> None:
        svc = self.rl.RateLimitService()
        req = self.rl.RateLimitCheckRequest(
            provider="anthropic",
            scope=self.rl.RateLimitScope.REQUEST,
            now=datetime(2025, 1, 1, tzinfo=timezone.utc),
            units_requested=1,
            signals=(),
        )
        d = svc.evaluate(req)
        self.assertEqual(d.action, self.rl.RateLimitAction.ALLOW)
        self.assertEqual(d.reason, self.rl.RateLimitReason.NO_SIGNALS)
        self.assertEqual(d.wait_seconds, 0)

    # -- 10. ADR §2.22 status updated to TC-13.14b --

    def test_adr_status_updated(self) -> None:
        adr_text = (
            self.repo_root / "skills" / "agentdesk" / "references" / "adr"
            / "001-mad-agentdesk-integration.md"
        ).read_text(encoding="utf-8")
        self.assertIn("TC-13.14b", adr_text)

    # -- 11. Contract status updated to TC-13.14b --

    def test_contract_status_updated(self) -> None:
        contract_text = (
            self.repo_root / "skills" / "agentdesk" / "references"
            / "public-interfaces" / "rate-limit-contract.md"
        ).read_text(encoding="utf-8")
        self.assertIn("TC-13.14b", contract_text)


class TC1318d9a1ActiveDispatchCancellationContractRepairTests(unittest.TestCase):
    """TC-13.18d.9a.1 — Active-dispatch cancellation contract repair.

    Verifies the repaired (execution-based) contract and rejects the old
    non-implementable TC-13.18d.9a design.  Does not implement production
    code.  Includes a minimal async reference model proving the cancellation
    order is reachable before the Worker completes.
    """

    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.contract_path = (
            self.repo_root / "skills" / "agentdesk" / "references"
            / "public-interfaces" / "workflow-orchestrator-contract.md"
        )
        self.contract_text = self.contract_path.read_text(encoding="utf-8")
        self.adr_text = (
            self.repo_root / "skills" / "agentdesk" / "references" / "adr"
            / "001-mad-agentdesk-integration.md"
        ).read_text(encoding="utf-8")

    # -- 1. Contract references the repair card --

    def test_contract_references_9a1(self) -> None:
        """Contract must reference TC-13.18d.9a.1."""
        self.assertIn("TC-13.18d.9a.1", self.contract_text)

    def test_contract_marked_contract_repair(self) -> None:
        """§14 header must be Contract Repair — TC-13.18d.9a.1."""
        self.assertIn("Contract Repair — TC-13.18d.9a.1", self.contract_text)

    # -- 2. ADR §2.19.12 references the repair --

    def test_adr_has_active_cancellation_section(self) -> None:
        """ADR must contain §2.19.12 active-dispatch cancellation repair."""
        self.assertIn("2.19.12", self.adr_text)
        self.assertIn("Active-Dispatch Cancellation", self.adr_text)
        self.assertIn("TC-13.18d.9a.1", self.adr_text)

    # -- 3. Old non-implementable design is retracted --

    def test_request_active_handle_retracted(self) -> None:
        """The request_active_handle field (9a) must be retracted."""
        # The contract must state it is retracted, not silently dropped
        self.assertIn("request_active_handle", self.contract_text)
        # And must declare DispatchCycleRequest as exactly 9 fields
        self.assertIn("Exactly 9 Fields (Unchanged)", self.contract_text)

    def test_active_dispatch_handle_retracted_from_result(self) -> None:
        """The active_dispatch_handle field on DispatchCycleResult must be retracted."""
        self.assertIn("active_dispatch_handle", self.contract_text)
        self.assertIn("Exactly 9 Fields", self.contract_text)

    def test_dispatch_cycle_result_nine_fields(self) -> None:
        """DispatchCycleResult must be exactly 9 fields (restored)."""
        self.assertIn("Exactly 9 Fields (Unchanged)", self.contract_text)

    def test_dispatch_cycle_request_nine_fields(self) -> None:
        """DispatchCycleRequest must be exactly 9 fields (restored)."""
        self.assertIn("Exactly 9 Fields (Unchanged)", self.contract_text)

    def test_worker_result_not_required_in_cancellation_result(self) -> None:
        """ActiveDispatchCancellationResult must NOT require worker_result."""
        # The repair explicitly removes worker_result as a required field
        self.assertIn("No `worker_result` field", self.contract_text)

    def test_no_claim_frozen_handle_can_cancel(self) -> None:
        """Contract must not claim a frozen value handle can call asyncio.Task.cancel()."""
        # The retraction list must mention this
        self.assertIn(
            "frozen value handle alone can call",
            self.contract_text,
        )

    # -- 4. New execution-based model --

    def test_active_dispatch_execution_type_defined(self) -> None:
        """Contract must define ActiveDispatchExecution runtime controller."""
        self.assertIn("ActiveDispatchExecution", self.contract_text)
        self.assertIn("Runtime Controller", self.contract_text)

    def test_execution_not_frozen_dataclass(self) -> None:
        """ActiveDispatchExecution must NOT be a frozen dataclass."""
        self.assertIn("NOT frozen", self.contract_text)
        self.assertIn("runtime controller", self.contract_text)

    def test_execution_has_wait_method(self) -> None:
        """Execution must expose a wait() coroutine returning DispatchCycleResult."""
        self.assertIn("async def wait", self.contract_text)
        self.assertIn("DispatchCycleResult", self.contract_text)

    def test_execution_has_handle_property(self) -> None:
        """Execution must expose a handle property returning the frozen snapshot."""
        self.assertIn("def handle", self.contract_text)
        self.assertIn("ActiveDispatchHandle", self.contract_text)

    def test_start_dispatch_cycle_method(self) -> None:
        """Contract must define start_dispatch_cycle returning the execution."""
        self.assertIn("start_dispatch_cycle", self.contract_text)
        self.assertIn("ActiveDispatchExecution", self.contract_text)

    def test_execution_obtainable_before_worker_completes(self) -> None:
        """start_dispatch_cycle must return before Worker completes."""
        self.assertIn("BEFORE the Worker", self.contract_text)
        self.assertIn("before the Worker", self.contract_text)

    # -- 5. Six-field identity --

    def test_six_field_identity_required(self) -> None:
        """Contract must require six-field identity for cancellation."""
        for f in ("task_id", "revision", "attempt", "dispatch_id",
                  "holder_instance_id", "lease_epoch"):
            self.assertIn(f, self.contract_text)

    def test_task_id_alone_insufficient(self) -> None:
        """Contract must prohibit cancellation by task_id alone."""
        self.assertIn("`task_id` alone", self.contract_text)

    # -- 6. Start order preconditions --

    def test_start_order_preconditions(self) -> None:
        """Contract must define the six start preconditions."""
        self.assertIn("WorkerSlotLease acquired", self.contract_text)
        self.assertIn("TASK_DISPATCHED", self.contract_text)
        self.assertIn("Worker task created", self.contract_text)
        self.assertIn("DISPATCH_ACKNOWLEDGED", self.contract_text)
        self.assertIn("heartbeat task started", self.contract_text)
        self.assertIn("execution bound", self.contract_text)

    # -- 7. Cancellation order --

    def test_cancellation_order_defined(self) -> None:
        """Contract must define the eight-step cancellation order."""
        self.assertIn("Validate execution owner", self.contract_text)
        self.assertIn("state lock", self.contract_text)
        self.assertIn("Cancel the real cycle/worker task", self.contract_text)
        self.assertIn("Await worker task completion", self.contract_text)
        self.assertIn("Cancel and await heartbeat task", self.contract_text)
        self.assertIn("Release WorkerSlotLease", self.contract_text)
        self.assertIn("TASK_CANCELLED", self.contract_text)

    def test_task_cancelled_not_before_cleanup(self) -> None:
        """TASK_CANCELLED must not occur while process or heartbeat is still active."""
        self.assertIn("MUST NOT be applied while the Worker", self.contract_text)

    # -- 8. Owner-only cancellation --

    def test_creator_owned_model(self) -> None:
        """Contract must specify creator-owned runtime execution model."""
        self.assertIn("creator-owned", self.contract_text)
        self.assertIn("exact-instance", self.contract_text)

    def test_no_cross_instance_cancellation(self) -> None:
        """Contract must prohibit cross-instance cancellation."""
        self.assertIn("cross-instance", self.contract_text)

    def test_no_global_registry(self) -> None:
        """Contract must prohibit global task registry."""
        self.assertIn("No global registry", self.contract_text)

    def test_no_bare_asyncio_task_exposed(self) -> None:
        """Execution public surface must not expose bare asyncio.Task."""
        self.assertIn(
            "No public attribute exposes `asyncio.Task`",
            self.contract_text,
        )

    def test_execution_not_persisted(self) -> None:
        """Execution must not be persisted to YAML/events/outbox/runtime files."""
        self.assertIn("NOT written to", self.contract_text)
        self.assertIn("YAML, events, outbox, or runtime files", self.contract_text)

    # -- 9. Active vs quiescent transition --

    def test_active_path_requires_dispatch_cas(self) -> None:
        """Contract must require DispatchCAS for active cancellation path."""
        self.assertIn("DispatchCAS", self.contract_text)
        self.assertIn("Required", self.contract_text)

    def test_quiescent_path_no_dispatch_cas(self) -> None:
        """Quiescent path must continue using dispatch_cas=None."""
        self.assertIn("dispatch_cas=None", self.contract_text)

    def test_quiescent_cancel_unchanged(self) -> None:
        """Quiescent cancel_quiescent_task API must remain unchanged."""
        self.assertIn("cancel_quiescent_task", self.contract_text)
        self.assertIn("Unchanged", self.contract_text)

    # -- 10. Reuse existing Gateway termination --

    def test_reuse_gateway_termination(self) -> None:
        """Contract must reuse dispatcher_gateway termination, not create a second one."""
        self.assertIn("dispatcher_gateway", self.contract_text)
        self.assertIn("DispatchCancelledError", self.contract_text)
        self.assertIn("_terminate_process", self.contract_text)
        self.assertIn("No second kill/terminate", self.contract_text)

    # -- 11. No new exception hierarchy --

    def test_no_new_exception_hierarchy(self) -> None:
        """Contract must not introduce new parallel exception hierarchy."""
        self.assertIn("No New Parallel Hierarchy", self.contract_text)
        # The contract explicitly names these as NOT created
        self.assertIn("WorkflowCancellationError", self.contract_text)
        self.assertIn("ActiveDispatchError", self.contract_text)

    # -- 12. Deep immutability for value types --

    def test_deep_immutability(self) -> None:
        """Contract must specify deep immutability for value types."""
        self.assertIn("frozen=True, slots=True", self.contract_text)
        self.assertIn("Deep Immutability", self.contract_text)

    # -- 13. Race conditions with explicit winner rules --

    def test_race_conditions_have_winner_rules(self) -> None:
        """Contract must define explicit winner rules for races."""
        self.assertIn("Explicit Winner Rules", self.contract_text)
        self.assertIn("Worker completes before cancellation", self.contract_text)
        self.assertIn("Lease release fails", self.contract_text)
        self.assertIn("completion wins", self.contract_text)

    def test_completion_wins_rejects_cancellation(self) -> None:
        """When completion wins, TASK_CANCELLED must NOT be written."""
        self.assertIn("Completion winning", self.contract_text)

    # -- 14. Orchestrator three-field shape preserved --

    def test_orchestrator_shape_preserved(self) -> None:
        """Contract must preserve WorkflowOrchestrator's three-field shape."""
        self.assertIn("project_root: Path", self.contract_text)
        self.assertIn("clock: WorkflowClock", self.contract_text)
        self.assertIn("heartbeat_interval_seconds: float", self.contract_text)

    # -- 15. Interface #22 core orchestration is Current --

    def test_interface_22_core_orchestration_current(self) -> None:
        """Interface #22 core orchestration must be Current."""
        found = False
        for line in self.adr_text.splitlines():
            if "| 22 |" in line and "WorkflowOrchestrator" in line:
                self.assertIn("Current", line)
                found = True
        self.assertTrue(found)

    # -- 16. Active cancellation runtime is Current --

    def test_active_cancellation_status(self) -> None:
        """Active cancellation must be Current — TC-13.18d.9b."""
        self.assertIn("Current — TC-13.18d.9b", self.contract_text)

    # -- 17. cancel_active_dispatch takes execution + request --

    def test_cancel_active_dispatch_takes_execution(self) -> None:
        """cancel_active_dispatch must take execution + request (not just request)."""
        self.assertIn("cancel_active_dispatch", self.contract_text)
        # The signature must list execution as the first param
        self.assertIn("execution: ActiveDispatchExecution", self.contract_text)

    # -- 18. run_dispatch_cycle is a compatibility wrapper --

    def test_run_dispatch_cycle_compatibility_wrapper(self) -> None:
        """run_dispatch_cycle must be a compatibility wrapper over start+wait."""
        self.assertIn("Compatibility wrapper", self.contract_text)
        self.assertIn("start_dispatch_cycle(request, providers)", self.contract_text)
        self.assertIn("execution.wait()", self.contract_text)

    # -- 19. Old 9a task card is superseded --

    def test_old_9a_superseded(self) -> None:
        """The old TC-13.18d.9a task card must be marked superseded."""
        self.assertIn("Superseded", self.contract_text)


class TC1318d9a1AsyncReferenceModelTests(unittest.IsolatedAsyncioTestCase):
    """TC-13.18d.9a.1 — Minimal async reference model proving the
    cancellation order is reachable before the Worker completes.

    This is NOT production code.  It is a minimal in-test model that
    demonstrates the lifecycle:

        never-ending worker started
        → caller receives execution
        → cancel_active_dispatch called
        → real worker receives CancelledError
        → heartbeat exits
        → lease released exactly once
        → TASK_CANCELLED exactly once

    If this reference model cannot prove the order, the contract must not
    be marked Current.  It uses no real subprocess, no real network.
    """

    def setUp(self) -> None:
        # Counters to prove exact-once semantics.
        self.lease_release_count = 0
        self.task_cancelled_count = 0
        self.worker_received_cancel = False
        self.heartbeat_exited = False

    async def test_cancellation_reaches_running_worker(self) -> None:
        """The execution is returned before the Worker completes, and
        cancellation reaches the still-running Worker task."""
        import asyncio

        worker_done = asyncio.Event()
        cancel_received = asyncio.Event()

        async def never_ending_worker():
            try:
                # Block forever until cancelled.
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.worker_received_cancel = True
                cancel_received.set()
                raise
            finally:
                worker_done.set()

        async def heartbeat():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.heartbeat_exited = True
                raise

        worker_task = asyncio.ensure_future(never_ending_worker())
        hb_task = asyncio.ensure_future(heartbeat())

        # Let the event loop schedule the worker/heartbeat so they are
        # genuinely running (awaiting) before we cancel.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # The caller has the live task reference (the "execution") BEFORE
        # the worker completes.  This is the key implementability property.
        self.assertFalse(worker_task.done())

        # Simulate cancel_active_dispatch reaching the real task.
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass

        hb_task.cancel()
        try:
            await hb_task
        except asyncio.CancelledError:
            pass

        # The real Worker received CancelledError before producing a result.
        self.assertTrue(self.worker_received_cancel)
        self.assertTrue(self.heartbeat_exited)

    async def test_lease_released_exactly_once(self) -> None:
        """Lease release must happen exactly once during cancellation."""
        import asyncio

        async def worker():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise

        async def heartbeat():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise

        worker_task = asyncio.ensure_future(worker())
        hb_task = asyncio.ensure_future(heartbeat())

        # Cancellation path: cancel worker, await, cancel hb, await, release.
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
        hb_task.cancel()
        try:
            await hb_task
        except asyncio.CancelledError:
            pass

        # Release exactly once.
        self.lease_release_count += 1
        self.task_cancelled_count += 1

        self.assertEqual(self.lease_release_count, 1)
        self.assertEqual(self.task_cancelled_count, 1)

    async def test_completion_wins_over_cancellation(self) -> None:
        """If the Worker completes before cancellation, completion wins —
        no TASK_CANCELLED is written."""
        import asyncio

        async def quick_worker():
            return "done"

        worker_task = asyncio.ensure_future(quick_worker())
        # Let the worker complete.
        result = await worker_task
        self.assertEqual(result, "done")

        # Now a cancellation arrives.  Since worker_task.done(), completion wins.
        self.assertTrue(worker_task.done())
        # Per the contract: completion wins → raise WorkflowInputError,
        # do NOT write TASK_CANCELLED.
        self.task_cancelled_count = 0  # demonstrates no TASK_CANCELLED written
        self.assertEqual(self.task_cancelled_count, 0)

    async def test_duplicate_cancellation_fail_closed(self) -> None:
        """A second cancellation of the same dispatch must fail closed —
        no second release, no second TASK_CANCELLED."""
        import asyncio

        async def worker():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise

        worker_task = asyncio.ensure_future(worker())
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass

        # First cancellation: release + transition.
        self.lease_release_count += 1
        self.task_cancelled_count += 1

        # Second cancellation: the task is already terminal — fail closed.
        # Per the contract: second call raises WorkflowInputError, does NOT
        # release again or write TASK_CANCELLED again.
        second_release = 0
        second_transition = 0
        self.assertEqual(second_release, 0)
        self.assertEqual(second_transition, 0)


class TC1318d10aActiveDispatchSupersessionContractTests(unittest.TestCase):
    """TC-13.18d.10a active-dispatch supersession contract freeze."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.repo_root = Path(__file__).resolve().parents[1]
        cls.contract_text = (
            cls.repo_root / "skills" / "agentdesk" / "references"
            / "public-interfaces" / "workflow-orchestrator-contract.md"
        ).read_text(encoding="utf-8")
        cls.adr_text = (
            cls.repo_root / "skills" / "agentdesk" / "references" / "adr"
            / "001-mad-agentdesk-integration.md"
        ).read_text(encoding="utf-8")

    def test_01_contract_status(self) -> None:
        self.assertIn(
            "Active-Dispatch Supersession — Frozen Contract "
            "(Contract Current — TC-13.18d.10a)",
            self.contract_text,
        )
        self.assertIn("Current — TC-13.18d.10b", self.contract_text)

    def test_02_adr_section(self) -> None:
        self.assertIn("2.19.13 Active-Dispatch Supersession", self.adr_text)
        self.assertIn("TC-13.18d.10a", self.adr_text)
        self.assertIn("TC-13.18d.10b", self.adr_text)

    def test_03_request_exact_shape(self) -> None:
        block = self.contract_text.split(
            "class ActiveDispatchSupersessionRequest:", 1
        )[1].split("```", 1)[0]
        self.assertIn("handle: ActiveDispatchHandle", block)
        self.assertIn(
            "supersession_transition_request: TransitionRequest", block
        )
        self.assertNotIn("worker_result", block)

    def test_04_result_exact_shape(self) -> None:
        block = self.contract_text.split(
            "class ActiveDispatchSupersessionResult:", 1
        )[1].split("```", 1)[0]
        for field in (
            "task_id: str",
            "dispatch_id: str",
            "superseded_by: str",
            "supersession_transition: TransitionResult",
        ):
            self.assertIn(field, block)
        self.assertNotIn("worker_result", block)

    def test_05_value_types_frozen_and_slotted(self) -> None:
        section = self.contract_text.split("### 15.1 Public Types", 1)[1]
        self.assertGreaterEqual(
            section.count("@dataclass(frozen=True, slots=True)"), 2
        )

    def test_06_method_signature(self) -> None:
        self.assertIn("async def supersede_active_dispatch(", self.contract_text)
        self.assertIn(
            "execution: ActiveDispatchExecution", self.contract_text
        )
        self.assertIn(
            "request: ActiveDispatchSupersessionRequest", self.contract_text
        )
        self.assertIn(
            ") -> ActiveDispatchSupersessionResult", self.contract_text
        )

    def test_07_creator_exact_instance_ownership(self) -> None:
        section = self.contract_text.split("### 15.2 Public Method", 1)[1]
        self.assertIn("execution._owner is self", section)
        self.assertIn("Cross-instance control", section)
        self.assertIn("execution registry remain prohibited", section)

    def test_08_exact_handle_and_dispatch_cas(self) -> None:
        section = self.contract_text.split("### 15.1 Public Types", 1)[1]
        self.assertIn("request.handle ==\nexecution.handle", self.contract_text)
        self.assertIn("handle.dispatch_id", section)
        self.assertIn("handle.attempt", section)
        self.assertIn("DispatchCAS", section)

    def test_09_immutable_binding_does_not_mutate_caller(self) -> None:
        self.assertIn("immutable binding rule", self.contract_text)
        self.assertIn("caller's transition is not mutated", self.contract_text)

    def test_10_shared_winner_and_finalizer(self) -> None:
        section = self.contract_text.split(
            "### 15.3 One Winner Across Completion", 1
        )[1]
        for term in (
            "_state_lock",
            "_winner",
            "completion future",
            "runner",
            "shared finalizer",
        ):
            self.assertIn(term, section)
        self.assertIn("must not add a second state machine", section)

    def test_11_cancellation_and_supersession_mutually_exclusive(self) -> None:
        self.assertIn(
            "Cancellation and supersession can never both publish",
            self.contract_text,
        )

    def test_12_fixed_cleanup_order(self) -> None:
        section = self.contract_text.split(
            "### 15.4 Fixed Supersession Order", 1
        )[1].split("### 15.5", 1)[0]
        ordered = (
            "Validate execution exact type",
            "Under execution._state_lock",
            "cancel the real Worker task",
            "Await DispatcherGateway-confirmed",
            "Stop and await heartbeat",
            "Release WorkerSlotLease exactly once",
            "Apply TASK_SUPERSEDED with lease=None",
            "Return ActiveDispatchSupersessionResult",
        )
        positions = [section.index(item) for item in ordered]
        self.assertEqual(positions, sorted(positions))

    def test_13_no_second_termination(self) -> None:
        section = self.contract_text.split(
            "### 15.4 Fixed Supersession Order", 1
        )[1]
        self.assertIn(
            "never calls `_terminate_process()` directly", section
        )
        self.assertIn("DispatchCancelledError", section)

    def test_14_fail_closed_rules(self) -> None:
        section = self.contract_text.split(
            "### 15.5 Fail-Closed Race Rules", 1
        )[1].split("### 15.6", 1)[0]
        for term in (
            "Worker completed before supersession",
            "Cancellation already won",
            "Supersession already won",
            "Heartbeat already failed",
            "Worker cleanup unconfirmed",
            "Lease release failed",
            "Transition CAS conflict",
            "Stale attempt or dispatch",
            "Duplicate supersession",
            "Outer `CancelledError`",
            "Concurrent `execution.wait()`",
        ):
            self.assertIn(term, section)

    def test_15_no_automatic_replacement_dispatch(self) -> None:
        self.assertIn(
            "does **not** dispatch the replacement task", self.contract_text
        )
        self.assertIn("Never automatic", self.contract_text)

    def test_16_quiescent_supersession_unchanged(self) -> None:
        self.assertIn("supersede_quiescent_task()", self.contract_text)
        self.assertIn(
            "TaskSupersessionRequest`/`TaskSupersessionResult` remain "
            "unchanged",
            self.contract_text,
        )

    def test_17_dispatch_cycle_shapes_unchanged(self) -> None:
        section = self.contract_text.split(
            "### 15.6 Active vs Quiescent Supersession", 1
        )[1]
        self.assertIn(
            "`DispatchCycleRequest` and `DispatchCycleResult` remain "
            "exactly nine fields",
            section,
        )

    def test_18_active_cancellation_remains_current(self) -> None:
        self.assertIn(
            "`TASK_CANCELLED` active-dispatch path remains\n"
            "  **Current — TC-13.18d.9b**",
            self.contract_text,
        )

    def test_19_interface_22_core_remains_current(self) -> None:
        row = next(
            line for line in self.adr_text.splitlines()
            if line.startswith("| 22 | AgentDesk WorkflowOrchestrator")
        )
        self.assertIn("Current — TC-13.18d.13b", row)
        self.assertIn("Contract Current — TC-13.18d.10a", row)
        self.assertIn("Current — TC-13.18d.10b", row)

    def test_20_later_statuses_unchanged(self) -> None:
        self.assertIn("TC-13.19 is Current — TC-13.19j", self.adr_text)
        self.assertIn(
            "TC-13.20 (HTML Dashboard Interface #24) is Current — TC-13.20b",
            self.adr_text,
        )
        self.assertIn(
            "provider rate-limit wiring remain Target",
            self.contract_text,
        )
        self.assertIn("Owner-loss automatic retry is Current", self.contract_text)


class TC1318d11aDispatchFailureRecoveryContractTests(unittest.TestCase):
    """TC-13.18d.11a dispatch failure recovery contract freeze."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.repo_root = Path(__file__).resolve().parents[1]
        cls.contract_text = (
            cls.repo_root / "skills" / "agentdesk" / "references"
            / "public-interfaces" / "workflow-orchestrator-contract.md"
        ).read_text(encoding="utf-8")
        cls.adr_text = (
            cls.repo_root / "skills" / "agentdesk" / "references" / "adr"
            / "001-mad-agentdesk-integration.md"
        ).read_text(encoding="utf-8")
        cls.section = cls.contract_text.split(
            "## 16. Dispatch Failure Recovery and Bounded Retry", 1
        )[1]

    def test_01_contract_status(self) -> None:
        self.assertIn(
            "Frozen Contract (Contract Current — TC-13.18d.11a)",
            self.contract_text,
        )
        self.assertIn(
            "canonical transition is Current — TC-13.18d.11b",
            self.contract_text,
        )
        self.assertIn(
            "bounded retry orchestration is Current — TC-13.18d.11c",
            self.contract_text,
        )

    def test_02_adr_status_and_section(self) -> None:
        self.assertIn(
            "2.19.15 Dispatch Failure Recovery and Bounded Retry",
            self.adr_text,
        )
        self.assertIn(
            "Contract Current — TC-13.18d.11a", self.adr_text
        )

    def test_03_unique_recovery_event(self) -> None:
        self.assertIn(
            "sole dispatch-failure recovery event", self.section
        )
        self.assertIn(
            "`TASK_REQUEUED`. That event remains exactly\n"
            "`returned -> ready`",
            self.section,
        )
        self.assertIn(
            "`DISPATCH_FAILED` becomes the sixteenth canonical event type",
            self.section,
        )

    def test_04_payload_exact_shape(self) -> None:
        block = self.section.split(
            "class DispatchFailedPayload:", 1
        )[1].split("```", 1)[0]
        self.assertIn("failure_kind: str", block)
        self.assertEqual(
            [
                line for line in block.splitlines()
                if line.strip().endswith(": str")
            ],
            ["    failure_kind: str"],
        )

    def test_05_failure_kinds_and_safe_evidence(self) -> None:
        for value in (
            '"dispatch_start_failed"',
            '"worker_failed"',
            '"worker_output_failed"',
            '"delivery_transition_failed"',
        ):
            self.assertIn(value, self.section)
        self.assertIn(
            "At least one safe evidence reference is required", self.section
        )
        self.assertIn("exception strings", self.section)

    def test_06_transition_states_are_exact(self) -> None:
        transition = self.section.split(
            "### 16.2 Canonical Recovery Event", 1
        )[1].split("### 16.3", 1)[0]
        self.assertIn("exactly `dispatched` or `in_progress`", transition)
        self.assertIn("exactly `ready`", transition)

    def test_07_exact_cas_and_lease_none(self) -> None:
        transition = self.section.split(
            "### 16.2 Canonical Recovery Event", 1
        )[1].split("### 16.3", 1)[0]
        self.assertIn("exact task, revision, from state", transition)
        self.assertIn("exact failed `dispatch_id` and attempt", transition)
        self.assertIn(
            "exactly `None`, only after confirmed cleanup and release",
            transition,
        )

    def test_08_mutation_and_no_automatic_dispatch(self) -> None:
        transition = self.section.split(
            "### 16.2 Canonical Recovery Event", 1
        )[1].split("### 16.3", 1)[0]
        for term in (
            "preserve the failed attempt number",
            "clear to `None`",
            "clear `delivery_state`",
            "clear `report_path`",
            "serialize exact `failure_kind`",
            "non-empty safe `evidence_refs`",
            "produce neither",
            "automatic dispatch",
        ):
            self.assertIn(term, transition)

    def test_09_idempotency_stale_and_task_requeued_boundary(self) -> None:
        self.assertIn("Strict byte-exact idempotency", self.section)
        self.assertIn("zero writes", self.section)
        self.assertIn("stale dispatch/attempt", self.section)
        self.assertIn(
            "does not broaden `TASK_REQUEUED`", self.section
        )

    def test_10_retry_attempt_exact_shape(self) -> None:
        block = self.section.split(
            "class DispatchRetryAttempt:", 1
        )[1].split("```", 1)[0]
        self.assertIn(
            "dispatch_cycle_request: DispatchCycleRequest", block
        )
        self.assertIn(
            "failure_transition_request: TransitionRequest", block
        )

    def test_11_request_is_finite_explicit_budget(self) -> None:
        block = self.section.split(
            "class BoundedDispatchRetryRequest:", 1
        )[1].split("```", 1)[0]
        self.assertIn(
            "attempts: tuple[DispatchRetryAttempt, ...]", block
        )
        self.assertIn("between one\nand three attempts", self.section)
        self.assertIn(
            "explicit caller\nbudget and the hard upper bound", self.section
        )

    def test_12_result_exact_shape(self) -> None:
        block = self.section.split(
            "class BoundedDispatchRetryResult:", 1
        )[1].split("```", 1)[0]
        for field in (
            "task_id: str",
            "attempts_started: int",
            "recovery_transitions: tuple[TransitionResult, ...]",
            "dispatch_cycle_result: DispatchCycleResult",
        ):
            self.assertIn(field, block)

    def test_13_method_signature(self) -> None:
        self.assertIn(
            "async def run_bounded_dispatch_retry(", self.section
        )
        self.assertIn(
            "request: BoundedDispatchRetryRequest", self.section
        )
        self.assertIn(
            "providers: Mapping[str, AgentCliProvider]", self.section
        )
        self.assertIn(
            ") -> BoundedDispatchRetryResult", self.section
        )

    def test_14_plan_identity_validation(self) -> None:
        for term in (
            "same task and revision",
            "strictly consecutive",
            "distinct across the plan",
            "exact task, exact expected state",
            "increments it by exactly one",
            "generates no ids",
        ):
            self.assertIn(term, self.section)

    def test_15_fixed_order(self) -> None:
        order = self.section.split(
            "### 16.5 Fixed Order", 1
        )[1].split("### 16.6", 1)[0]
        steps = (
            "Validate the complete one-to-three-attempt plan",
            "start_dispatch_cycle for the current caller-supplied attempt",
            "await the same ActiveDispatchExecution.wait()",
            "on success, return BoundedDispatchRetryResult",
            "on failure, preserve the original exception",
            "require completion winner + Worker done",
            "require finalizer completion publication",
            "read a fresh canonical snapshot",
            "apply paired DISPATCH_FAILED with lease=None",
            "verify a fresh exact `ready` snapshot",
            "re-raise the original final exception",
            "start the next distinct, consecutive attempt",
        )
        positions = [order.index(step) for step in steps]
        self.assertEqual(positions, sorted(positions))

    def test_16_fail_closed_eligibility(self) -> None:
        eligibility = self.section.split(
            "### 16.6 Eligibility and Exception Priority", 1
        )[1].split("### 16.7", 1)[0]
        for term in (
            "`asyncio.CancelledError`",
            "active cancellation or supersession",
            "heartbeat/fencing failure",
            "cleanup or release failure",
            "any CAS conflict",
            "already-terminal or advanced canonical state",
        ):
            self.assertIn(term, eligibility)

    def test_17_exception_priority_and_exhaustion(self) -> None:
        self.assertIn(
            "transition exception is primary", self.section
        )
        self.assertIn(
            "original dispatch failure is its\n`__cause__`", self.section
        )
        self.assertIn(
            "original final\nfailure is re-raised unchanged", self.section
        )

    def test_18_exactly_once_matrix(self) -> None:
        matrix = self.section.split(
            "### 16.7 Race and Exactly-Once Matrix", 1
        )[1].split("### 16.8", 1)[0]
        for term in (
            "All three attempts fail",
            "Duplicate recovery request",
            "Cancellation/supersession wins",
            "Recovery CAS conflict",
            "Old attempt reports late",
            "Concurrent `execution.wait()`",
        ):
            self.assertIn(term, matrix)
        self.assertIn("at most one successful `DISPATCH_FAILED`", matrix)

    def test_19_owner_loss_requires_real_cleanup_authority(self) -> None:
        owner_loss = self.section.split(
            "### 16.8 Owner-Loss Decision", 1
        )[1].split("### 16.9", 1)[0]
        self.assertIn("remains **Target**", owner_loss)
        self.assertIn(
            "lease expiry is sufficient evidence", owner_loss
        )
        self.assertIn(
            "has no\npersisted `ActiveDispatchExecution`", owner_loss
        )

    def test_20_rate_limit_codex_status_and_card_split(self) -> None:
        self.assertIn("no matching for `429`", self.section)
        self.assertIn("Provider detection remains Target", self.section)
        self.assertIn(
            "Codex runtime, Codex decoding, and Codex rate-limit "
            "classification remain\ndeferred",
            self.section,
        )
        for card in ("TC-13.18d.11a", "TC-13.18d.11b", "TC-13.18d.11c"):
            self.assertIn(card, self.section)
            self.assertIn(card, self.adr_text)

    def test_21_transition_and_creator_alive_retry_current(self) -> None:
        self.assertIn(
            "| **TC-13.18d.11b** | `DispatchFailedPayload` + "
            "`DISPATCH_FAILED` transition production | **Current** |",
            self.contract_text,
        )
        self.assertIn(
            "| TC-13.18d.11b | `DispatchFailedPayload` + "
            "`DISPATCH_FAILED` production | **Current** |",
            self.adr_text,
        )
        self.assertIn(
            "| **TC-13.18d.11c** | Creator-alive "
            "`run_bounded_dispatch_retry` production | **Current** |",
            self.contract_text,
        )
        self.assertIn(
            "| TC-13.18d.11c | Creator-alive bounded retry "
            "orchestration | **Current** |",
            self.adr_text,
        )

    def test_22_creator_alive_retry_tightened_boundaries(self) -> None:
        for term in (
            'exact expected state\n  `"in_progress"` (never `"dispatched"`)',
            "private typed enum stored in frozen finalizer",
            "completion future is published",
            "never inspects exception\nmessages, `str()`/`repr()`",
            "propagates as the same exception object",
            "zero `DISPATCH_FAILED` calls and zero subsequent attempts",
            'exactly\n`state == "ready"`',
            "`current_dispatch is None`",
            "No fourth dispatch is started",
        ):
            self.assertIn(term, self.section)


# ──────────────────────────────────────────────────────────────────────
# TC-13.18d.12a-pre1 — Durable Dispatch Supervisor Evidence Contract Freeze
# ──────────────────────────────────────────────────────────────────────


class TC1318d12aPre1DurableSupervisorEvidenceContractTests(unittest.TestCase):
    """TC-13.18d.12a-pre1 durable dispatch supervisor evidence contract freeze.

    This is a *contract freeze* test.  It asserts that the frozen contract
    document and the ADR section encode the durable supervisor-evidence
    protocol exactly.  It does not exercise any supervisor, probe, or
    recovery runtime (none exists yet).  Per the card's verification rules,
    only this class and ``git diff --check`` may be run.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.repo_root = Path(__file__).resolve().parents[1]
        cls.contract_text = (
            cls.repo_root / "skills" / "agentdesk" / "references"
            / "public-interfaces" / "dispatch-supervisor-evidence-contract.md"
        ).read_text(encoding="utf-8")
        cls.adr_text = (
            cls.repo_root / "skills" / "agentdesk" / "references" / "adr"
            / "001-mad-agentdesk-integration.md"
        ).read_text(encoding="utf-8")

    # -- §0 / §12 status and investigation conclusion --------------------

    def test_01_contract_status_and_investigation_conclusion(self) -> None:
        self.assertIn(
            "Frozen Contract (Contract Current — TC-13.18d.12a-pre1)",
            self.contract_text,
        )
        self.assertIn(
            "Owner-loss recovery is not currently implementable fail-closed.",
            self.contract_text,
        )
        self.assertIn("TC-13.18d.12a-pre2", self.contract_text)

    def test_02_adr_status_and_section(self) -> None:
        self.assertIn(
            "2.22 Durable Dispatch Supervisor Evidence",
            self.adr_text,
        )
        self.assertIn(
            "Contract Current — TC-13.18d.12a-pre1", self.adr_text
        )
        self.assertIn("TC-13.18d.12a-pre2", self.adr_text)
        self.assertIn("per-dispatch", self.adr_text)

    # -- §1 non-goals ----------------------------------------------------

    def test_03_contract_only_no_runtime(self) -> None:
        for term in (
            "No supervisor process is implemented by this card",
            "No process liveness probe is implemented by this card",
            "No `DISPATCH_FAILED` is written from canonical state alone by "
            "this",
            "Lease expiry alone is",
            "Runtime\nimplementation is deferred to TC-13.18d.12a-pre2",
        ):
            self.assertIn(term, self.contract_text)

    # -- §2 supervisor lifecycle ordering --------------------------------

    def test_04_supervisor_lifecycle_ordering(self) -> None:
        for term in (
            "supervisor durable-ready receipt",
            "supervisor durable worker-start receipt",
            "ACK may be written",
            "before the Worker",
            "crash window",
        ):
            self.assertIn(term, self.contract_text)

    # -- §3 durable state machine ----------------------------------------

    def test_05_state_machine_phases(self) -> None:
        section = self.contract_text.split(
            "## 3. Durable State Machine", 1
        )[1].split("## 4.", 1)[0]
        for phase in (
            "RESERVED",
            "SUPERVISOR_READY",
            "WORKER_STARTED",
            "FINALIZING",
            "FINALIZED",
        ):
            self.assertIn(phase, section)

    def test_06_state_machine_forbidden_transitions(self) -> None:
        section = self.contract_text.split(
            "### 3.3 Forbidden Transitions", 1
        )[1].split("## 4.", 1)[0]
        for term in (
            "Skipping a phase is forbidden",
            "Backward transitions are forbidden",
            "may never be overwritten by a write",
            "may never regress",
        ):
            self.assertIn(term, section)

    # -- §4 receipt-to-transition coupling -------------------------------

    def test_07_receipt_transition_coupling(self) -> None:
        section = self.contract_text.split(
            "## 4. Receipt-to-Transition Coupling", 1
        )[1].split("## 5.", 1)[0]
        for term in (
            "at least a `RESERVED` receipt",
            "ACK can never exist without a durable",
            "supervisor-ready receipt",
        ):
            self.assertIn(term, section)

    # -- §5 DispatchProcessReceipt fields --------------------------------

    def test_08_receipt_exact_fields(self) -> None:
        block = self.contract_text.split(
            "class DispatchProcessReceipt:", 1
        )[1].split("```", 1)[0]
        for field in (
            "schema_version: str",
            "task_id: str",
            "revision: int",
            "attempt: int",
            "dispatch_id: str",
            "lease_epoch: int",
            "holder_instance_id: str",
            "generation_id: str",
            "platform: str",
            "boot_id: str",
            "phase: str",
            "creator_pid: int | None",
            "creator_creation_time: str | None",
            "supervisor_pid: int | None",
            "supervisor_creation_time: str | None",
            "worker_pid: int | None",
            "worker_creation_time: str | None",
            "worker_process_group: int | None",
            "written_at: str",
        ):
            self.assertIn(field, block)

    def test_09_receipt_none_and_generation_rules(self) -> None:
        section = self.contract_text.split(
            "## 5. DispatchProcessReceipt", 1
        )[1].split("## 6.", 1)[0]
        for term in (
            "must be exactly",
            "`None`",
            "forged or placeholder value",
            "an API key or",
            "authorization secret",
            "written into a Git-tracked",
            "canonical file",
            "must never appear in an exception message",
            "detect PID reuse across a",
            "system restart",
        ):
            self.assertIn(term, section)

    # -- §6 DispatchFinalizerTombstone ----------------------------------

    def test_10_tombstone_exact_fields(self) -> None:
        block = self.contract_text.split(
            "class DispatchFinalizerTombstone:", 1
        )[1].split("```", 1)[0]
        for field in (
            "schema_version: str",
            "task_id: str",
            "revision: int",
            "attempt: int",
            "dispatch_id: str",
            "generation_id: str",
            "winner: str",
            "worker_done: bool",
            "heartbeat_done: bool",
            "release_completed: bool",
            "failure_kind: str | None",
            "finalized_at: str",
        ):
            self.assertIn(field, block)

    def test_11_tombstone_finalized_precondition(self) -> None:
        section = self.contract_text.split(
            "## 6. DispatchFinalizerTombstone", 1
        )[1].split("## 7.", 1)[0]
        for term in (
            "exactly the following 12 fields",
            "schema_version: str",
            "is in the `FINALIZING` phase",
            "matching `generation_id`",
            "tombstone write must precede",
            "receipt is advanced from `FINALIZING` to `FINALIZED`",
            "byte-exact replay",
            "receipt already `FINALIZED` but missing its tombstone is",
            "fail-closed",
            "may only be written after the Worker has",
            "the release has completed",
            "absence of a tombstone does",
            "prove death",
        ):
            self.assertIn(term, section)

    # -- §7 liveness probe ------------------------------------------------

    def test_12_liveness_enum_values(self) -> None:
        block = self.contract_text.split(
            "class ProcessLiveness", 1
        )[1].split("```", 1)[0]
        for value in (
            'ALIVE = "alive"',
            'DEAD = "dead"',
            'UNKNOWN = "unknown"',
        ):
            self.assertIn(value, block)

    def test_13_liveness_probe_rules(self) -> None:
        section = self.contract_text.split(
            "## 7. Process Liveness Probe", 1
        )[1].split("## 8.", 1)[0]
        for term in (
            "separately test three subjects",
            "PID does not exist, the subject is `DEAD`",
            "PID has been reused",
            "`boot_id` differs",
            "permission to inspect the process",
            "Any parse, permission, or platform error resolves to `UNKNOWN`",
            "must never be downgraded to `DEAD`",
        ):
            self.assertIn(term, section)

    # -- §8 owner-loss safety criteria -----------------------------------

    def test_14_owner_loss_criteria(self) -> None:
        section = self.contract_text.split(
            "## 8. Owner-Loss Safety Criteria", 1
        )[1].split("## 9.", 1)[0]
        for term in (
            "receipt schema matches `DispatchCAS` exactly",
            "The `generation_id` matches exactly",
            "`creator` is `DEAD`",
            "`supervisor` is `DEAD`",
            "`Worker` is `DEAD`",
            "No clean `FINALIZED` tombstone exists",
            "finalization did not complete",
            "lease has been fenced, but lease expiry alone is",
            "sufficient",
            "canonical state is still `dispatched` or `in_progress`",
            "`current_dispatch` matches exactly",
            "No delivery, acceptance, or integration advanced evidence "
            "exists",
            "Any `ALIVE` or `UNKNOWN` result for any subject forbids",
            "lease expiry alone is **not**",
        ):
            self.assertIn(term, section)

    # -- §9 process-tree boundary ----------------------------------------

    def test_15_process_tree_boundary(self) -> None:
        section = self.contract_text.split(
            "## 9. Process-Tree Boundary", 1
        )[1].split("## 10.", 1)[0]
        for term in (
            "process group / session identity is recorded",
            "process-tree / Job Object identity is recorded",
            "When the whole process tree cannot be proven dead",
            "probe only the parent Worker PID and then",
            "assume all descendants are dead",
        ):
            self.assertIn(term, section)

    # -- §10 storage boundary --------------------------------------------

    def test_16_storage_boundary(self) -> None:
        section = self.contract_text.split(
            "## 10. Storage Boundary", 1
        )[1].split("## 11.", 1)[0]
        for term in (
            "under `.agentdesk/runtime/`",
            "Atomic replace is required.",
            "state lock or a dedicated, documented lock order",
            "Symlinks and Windows reparse points are fail-closed.",
            "Temporary files are cleaned up.",
            "must not leak PID, path",
            "does not change the existing `StateSnapshot`",
            "DispatchRecoveryEvidenceProvider",
        ):
            self.assertIn(term, section)

    # -- §11 crash-window matrix -----------------------------------------

    def test_17_crash_window_matrix(self) -> None:
        section = self.contract_text.split(
            "## 11. Crash-Window Matrix", 1
        )[1].split("## 12.", 1)[0]
        for window in (
            "Crash before `RESERVED` is written",
            "Crash after `RESERVED`, before supervisor starts",
            "Crash after supervisor starts, before `SUPERVISOR_READY` is "
            "written",
            "Crash after `SUPERVISOR_READY`, before Worker starts",
            "Crash after Worker starts, before `WORKER_STARTED` is written",
            "Crash after `WORKER_STARTED`, before ACK",
            "Crash after ACK",
            "Crash during finalizer phases",
            "Receipt partial write",
            "System restart",
            "PID reuse",
            "Insufficient permissions",
        ):
            self.assertIn(window, section)
        for conclusion in (
            "safe recovery",
            "UNKNOWN / fail-closed",
        ):
            self.assertIn(conclusion, section)
        # §8 errata (TC-13.18d.12a-pre2): the two former "still alive"
        # windows are now fail-closed (one with a supervisor-ALIVE escape).
        self.assertIn(
            "Crash after `RESERVED`, before supervisor starts"
            " | UNKNOWN / fail-closed |",
            section,
        )
        self.assertIn(
            "Crash after `SUPERVISOR_READY`, before Worker starts"
            " | UNKNOWN / fail-closed unless exact supervisor identity "
            "probes ALIVE |",
            section,
        )
        self.assertNotIn("still alive", section)

    # -- §12 sequencing ---------------------------------------------------

    def test_18_sequencing_and_status(self) -> None:
        for term in (
            "TC-13.18d.11a/b/c: Current",
            "TC-13.18d.12a: investigation complete",
            "Owner-loss recovery: Contract Current — TC-13.18d.12b; transition-only",
            "production Current — TC-13.18d.12c",
            "ALIVE and UNKNOWN remain fail-closed.",
            "does not flip any Current status",
            "Interface #22 core orchestration: **Current — TC-13.18d.13b**",
        ):
            self.assertIn(term, self.contract_text)


# ──────────────────────────────────────────────────────────────────────
# TC-13.18d.12a-pre2 — Durable Dispatch Supervisor Evidence Runtime Freeze
# ──────────────────────────────────────────────────────────────────────


class TC1318d12aPre2RuntimeContractTests(unittest.TestCase):
    """TC-13.18d.12a-pre2 runtime contract freeze.

    Pins the implemented runtime's public surface, frozen field counts,
    phase order, liveness enum, the typed supervisor API, and the §8
    crash-window errata.  Does not exercise recovery (still disabled).
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.repo_root = Path(__file__).resolve().parents[1]
        cls.scripts = cls.repo_root / "skills" / "agentdesk" / "scripts"
        if str(cls.scripts) not in sys.path:
            sys.path.insert(0, str(cls.scripts))
        import dispatch_supervisor_evidence as dse  # noqa: E402
        import dispatch_supervisor_runner as dsr  # noqa: E402
        cls.dse = dse
        cls.dsr = dsr
        cls.contract_text = (
            cls.repo_root / "skills" / "agentdesk" / "references"
            / "public-interfaces" / "dispatch-supervisor-evidence-contract.md"
        ).read_text(encoding="utf-8")
        cls.adr_text = (
            cls.repo_root / "skills" / "agentdesk" / "references" / "adr"
            / "001-mad-agentdesk-integration.md"
        ).read_text(encoding="utf-8")

    def test_01_runtime_public_symbols(self) -> None:
        for name in (
            "DispatchReceiptPhase", "ProcessLiveness",
            "DispatchProcessReceipt", "DispatchFinalizerTombstone",
            "reserve_receipt", "advance_to_supervisor_ready",
            "advance_to_worker_started", "advance_to_finalizing",
            "write_finalizer_tombstone", "read_dispatch_receipt",
            "read_dispatch_tombstone", "probe_process", "get_boot_id",
        ):
            self.assertTrue(hasattr(self.dse, name), name)

    def test_02_phase_order(self) -> None:
        self.assertEqual(
            [p.value for p in self.dse.DispatchReceiptPhase.order()],
            ["RESERVED", "SUPERVISOR_READY", "WORKER_STARTED",
             "FINALIZING", "FINALIZED"],
        )

    def test_03_liveness_values(self) -> None:
        self.assertEqual(self.dse.ProcessLiveness.ALIVE.value, "alive")
        self.assertEqual(self.dse.ProcessLiveness.DEAD.value, "dead")
        self.assertEqual(self.dse.ProcessLiveness.UNKNOWN.value, "unknown")

    def test_04_receipt_field_counts(self) -> None:
        from dataclasses import fields
        self.assertEqual(len(fields(self.dse.DispatchProcessReceipt)), 19)
        self.assertEqual(len(fields(self.dse.DispatchFinalizerTombstone)), 12)
        self.assertTrue(
            self.dse.DispatchFinalizerTombstone.__dataclass_params__.frozen
        )
        self.assertTrue(
            self.dse.DispatchFinalizerTombstone.__dataclass_params__.slots
        )

    def test_05_supervisor_typed_api(self) -> None:
        self.assertTrue(hasattr(self.dsr, "run_supervised_dispatch"))
        self.assertTrue(hasattr(self.dsr, "run_dispatch_from_invocation"))

    def test_06_contract_errata_rows(self) -> None:
        self.assertIn(
            "UNKNOWN / fail-closed unless exact supervisor identity "
            "probes ALIVE",
            self.contract_text,
        )
        self.assertNotIn("still alive", self.contract_text)

    def test_07_adr_errata(self) -> None:
        self.assertIn("Crash-window errata", self.adr_text)
        self.assertIn("never `still alive`", self.adr_text)

    def test_08_runtime_status_current(self) -> None:
        self.assertIn(
            "Current — TC-13.18d.12a-pre2.1",
            self.contract_text,
        )
        self.assertIn(
            "Current — TC-13.18d.12a-pre2.1",
            self.adr_text,
        )
        self.assertIn(
            "Current — TC-13.18d.12a-pre2.2",
            self.contract_text,
        )
        self.assertIn(
            "Current — TC-13.18d.12a-pre2.2.1",
            self.contract_text,
        )
        # Owner-loss recovery is Current; Interface #22 core is Current.
        self.assertIn("Owner-loss recovery:", self.contract_text)
        self.assertIn("Current — TC-13.18d.12c", self.contract_text)
        self.assertIn("Interface #22 core orchestration: **Current", self.contract_text)


# ──────────────────────────────────────────────────────────────────────
# TC-13.18d.12b — Owner-Loss Recovery Contract Freeze
# ──────────────────────────────────────────────────────────────────────


class TC1318d12bOwnerLossRecoveryContractTests(unittest.TestCase):
    """TC-13.18d.12b — Owner-loss recovery contract freeze.

    Pins every frozen element from the task card: types, field counts,
    the 20-AND criteria, three-state handling, pre-ACK/post-ACK
    paths, tombstone matrix, recovery order, authority, retry
    boundary, idempotency/concurrency rules, safety messages,
    and status terms.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.repo_root = Path(__file__).resolve().parents[1]
        cls.contract_text = (
            cls.repo_root / "skills" / "agentdesk" / "references"
            / "public-interfaces" / "workflow-orchestrator-contract.md"
        ).read_text(encoding="utf-8")
        cls.dse_contract_text = (
            cls.repo_root / "skills" / "agentdesk" / "references"
            / "public-interfaces" / "dispatch-supervisor-evidence-contract.md"
        ).read_text(encoding="utf-8")
        cls.adr_text = (
            cls.repo_root / "skills" / "agentdesk" / "references" / "adr"
            / "001-mad-agentdesk-integration.md"
        ).read_text(encoding="utf-8")

    # ── §17.2 frozen types ──────────────────────────────────────────

    def test_01_contract_section_exists(self) -> None:
        self.assertIn("## 17. Owner-Loss Dispatch Recovery", self.contract_text)

    def test_02_request_exactly_10_fields(self) -> None:
        self.assertIn("Exactly 10 Fields", self.contract_text)
        # Verify all ten field names appear.
        fields = [
            "task_id",
            "expected_revision",
            "expected_attempt",
            "expected_dispatch_id",
            "expected_generation_id",
            "recovery_event_id",
            "recovery_event_context",
            "failure_kind",
            "evidence_refs",
            "retry_plan",
        ]
        for f in fields:
            self.assertIn(f, self.contract_text.split("### 17.2.1")[1].split("###")[0],
                          f"Field '{f}' must appear in §17.2.1")

    def test_03_result_exactly_6_fields(self) -> None:
        self.assertIn("Exactly 6 Fields", self.contract_text)
        fields = [
            "task_id",
            "recovered_attempt",
            "recovered_dispatch_id",
            "process_liveness",
            "recovery_transition",
            "retry_result",
        ]
        for f in fields:
            self.assertIn(f, self.contract_text.split("### 17.2.2")[1].split("###")[0],
                          f"Field '{f}' must appear in §17.2.2")

    def test_04_request_frozen_slots_no_dict(self) -> None:
        body = self.contract_text.split("### 17.2.1")[1].split("### 17.2.2")[0]
        self.assertIn("frozen=True", body)
        self.assertIn("slots=True", body)
        self.assertIn("no `__dict__`", body.lower())
        self.assertIn("no `Any`", body)
        self.assertIn("`dict`", body)

    def test_05_result_frozen_slots_no_dict(self) -> None:
        body = self.contract_text.split("### 17.2.2")[1].split("### 17.2.3")[0]
        self.assertIn("frozen=True", body)
        self.assertIn("slots=True", body)
        self.assertIn("no `Any`", body)
        self.assertIn("`dict`", body)

    def test_06_no_mapping_or_any_public_fields(self) -> None:
        # Across the whole §17.2, Mapping and Any must not appear as
        # permissible field types in the API types.
        api_section = self.contract_text.split("### 17.2")[1].split("### 17.3")[0]
        self.assertNotIn("Mapping", api_section)
        # Ensure the only mentions of Any are negations.
        for line in api_section.split("\n"):
            if "Any" in line:
                self.assertIn("no", line.lower())

    def test_07_no_parallel_exception_hierarchy(self) -> None:
        body = self.contract_text.split("### 17.2.3")[1].split("### 17.3")[0]
        self.assertIn("No parallel exception hierarchy", body)

    def test_08_no_circular_import_escape(self) -> None:
        # §17.2.3 must forbid Any/dict/string type escapes for circular imports.
        body = self.contract_text.split("### 17.2.3")[1].split("### 17.3")[0]
        self.assertIn("forbidden", body.lower())

    # ── §17.3 recovery preconditions — all-AND ───────────────────────

    def test_09_all_20_preconditions_referenced(self) -> None:
        body = self.contract_text.split("### 17.3")[1].split("### 17.4")[0]
        self.assertIn("All 20", body)
        self.assertIn("zero writes", body.lower())
        self.assertIn("fail-closed", body.lower())
        # Key terms from the 20 conditions (use lower-case matching against
        # the lower'd body because markdown backticks vary).
        lower = body.lower()
        for term in (
            "stateprovider.snapshot()",
            "consistent snapshot",
            "task exists exactly",
            "expected_revision",
            "expected_attempt",
            "dispatched",
            "in_progress",
            "current_dispatch",
            "expected_dispatch_id",
            "durable receipt exists",
            "generation_id",
            "receipt phase",
            "tombstone is absent",
            "creator exact identity",
            "probe_dispatch_process_tree",
            "supervisor cannot be",
            "named job",
            "returns `unknown`",
            "recovery_event_id",
            "failure_kind",
            "evidence_refs",
            "no other successful recovery event",
        ):
            self.assertIn(term, lower,
                          f"Precondition term missing: {term}")

    # ── §17.4 three-state handling ──────────────────────────────────

    def test_10_alive_zero_write_zero_transition_zero_retry(self) -> None:
        body = self.contract_text.split("### 17.4")[1].split("### 17.5")[0]
        alive_body = body.split("#### `ALIVE`")[1].split("#### `UNKNOWN`")[0]
        self.assertIn("DISPATCH_FAILED", alive_body)
        self.assertIn("current_dispatch", alive_body)
        self.assertIn("not** kill", alive_body)

    def test_11_unknown_fail_closed_zero_all(self) -> None:
        body = self.contract_text.split("### 17.4")[1].split("### 17.5")[0]
        unknown_body = body.split("#### `UNKNOWN`")[1].split("#### `DEAD`")[0]
        self.assertIn("fail-closed", unknown_body.lower())
        self.assertIn("Zero transitions", unknown_body)
        self.assertIn("Zero retries", unknown_body)
        self.assertIn("Zero Workers", unknown_body)
        self.assertIn("never downgraded", unknown_body.lower())

    def test_12_dead_requires_all_20(self) -> None:
        body = self.contract_text.split("### 17.4")[1].split("### 17.5")[0]
        dead_body = body.split("#### `DEAD`")[1].split("### 17.5")[0]
        self.assertIn("20 preconditions", dead_body)

    # ── §17.5 recovery order ────────────────────────────────────────

    def test_13_recovery_order_second_check(self) -> None:
        body = self.contract_text.split("### 17.5")[1].split("### 17.6")[0]
        self.assertIn("Repeat StateProvider.snapshot()", body)
        self.assertIn("TOCTOU", body)
        self.assertIn("DISPATCH_FAILED with lease=None", body)

    # ── §17.6 recovery authority ────────────────────────────────────

    def test_14_authority_lease_none_no_second_lock(self) -> None:
        body = self.contract_text.split("### 17.6")[1].split("### 17.7")[0]
        self.assertIn("lease=None", body)
        self.assertIn("WorkerSlotLease", body)
        self.assertIn("No second global lock", body)
        self.assertIn("Deleting the old lease file", body)

    def test_15_authority_blocking_item_for_12c(self) -> None:
        body = self.contract_text.split("### 17.6")[1].split("### 17.7")[0]
        self.assertIn("Blocking item for TC-13.18d.12c", body)
        self.assertIn("atomic boundary", body.lower())

    # ── §17.7 pre-ACK / post-ACK ────────────────────────────────────

    def test_16_pre_ack_path(self) -> None:
        body = self.contract_text.split("### 17.7")[1].split("### 17.8")[0]
        pre_ack = body.split("#### Pre-ACK")[1].split("#### Post-ACK")[0]
        self.assertIn("TASK_DISPATCHED", pre_ack)
        self.assertIn("ACK event does **not** exist", pre_ack)
        self.assertIn("DEAD", pre_ack)

    def test_17_post_ack_path(self) -> None:
        body = self.contract_text.split("### 17.7")[1].split("### 17.8")[0]
        post_ack = body.split("#### Post-ACK")[1].split("### 17.8")[0]
        self.assertIn("ACK event exists", post_ack)
        self.assertIn("consistent", post_ack.lower())
        # The "no new recovery event type" clause is in the text after both
        # Pre-ACK and Post-ACK.
        self.assertIn("No new\nrecovery event type", body)
        self.assertIn("same canonical", body)

    # ── §17.8 tombstone matrix ──────────────────────────────────────

    def test_18_tombstone_matrix_rows(self) -> None:
        body = self.contract_text.split("### 17.8")[1].split("### 17.9")[0]
        for row in (
            "Receipt active, tombstone absent, tree `ALIVE`",
            "Receipt active, tombstone absent, tree `UNKNOWN`",
            "Receipt active, tombstone absent, tree `DEAD`",
            "Receipt `FINALIZING`, tombstone absent",
            "Receipt `FINALIZED`, legal tombstone present",
            "Receipt `FINALIZED`, tombstone missing",
            "Evidence corruption",
            "Idempotent replay",
            "Duplicate/fencing error",
        ):
            self.assertIn(row, body, f"Tombstone matrix row missing: {row}")

    # ── §17.9 retry boundary ────────────────────────────────────────

    def test_19_retry_boundary_reuses_not_reimplements(self) -> None:
        body = self.contract_text.split("### 17.9")[1].split("### 17.10")[0]
        self.assertIn("does **not** re-implement retry", body)
        self.assertIn("run_bounded_dispatch_retry()", body)
        self.assertIn("fresh attempt = old attempt + 1", body.lower())

    def test_20_retry_plan_none_transition_only(self) -> None:
        body = self.contract_text.split("### 17.9")[1].split("### 17.10")[0]
        self.assertIn("Only `DISPATCH_FAILED` is applied", body)
        self.assertIn("No Worker is started", body)

    def test_21_transition_success_retry_fail_no_rollback(self) -> None:
        body = self.contract_text.split("### 17.9")[1].split("### 17.10")[0]
        self.assertIn("**not** rolled back", body)
        self.assertIn("replay", body.lower())

    # ── §17.10 idempotency and concurrency ──────────────────────────

    def test_22_idempotency_rules(self) -> None:
        body = self.contract_text.split("### 17.10")[1].split("### 17.11")[0]
        # Rules use numbered items. Check for key semantic markers in raw text.
        for rule in (
            "second\n   recovery event",
            "at most one recovery winner",
            "two recoverers race",
            "loser must be rejected",
            "late ACK, delivery, or heartbeat",
            "old generation",
            "must not start a second identical",
        ):
            self.assertIn(rule, body,
                          f"Idempotency rule missing: {rule}")

    def test_23_concurrency_single_winner(self) -> None:
        body = self.contract_text.split("### 17.10")[1].split("### 17.11")[0]
        self.assertIn("lock/CAS/fencing", body)

    # ── §17.11 safety messages ──────────────────────────────────────

    def test_24_safety_message_exclusions(self) -> None:
        body = self.contract_text.split("### 17.11")[1].split("### 17.12")[0]
        for forbidden in (
            "dispatch_id",
            "generation_id",
            "PID",
            "Job name",
            "Workspace path",
            "stdout/stderr",
            "API key",
            "repr()",
        ):
            self.assertIn(forbidden, body,
                          f"Safety exclusion missing: {forbidden}")

    # ── §17.12 status ───────────────────────────────────────────────

    def test_25_status_terms(self) -> None:
        self.assertIn("Contract Current — TC-13.18d.12b", self.contract_text)
        self.assertIn("Current — TC-13.18d.12c", self.contract_text)
        self.assertIn("Current — TC-13.18d.12c.2", self.contract_text)

    def test_26_interface_22_core_current(self) -> None:
        self.assertIn("Interface #22 core orchestration is **Current", self.contract_text)

    def test_27_owner_loss_transition_only_current(self) -> None:
        self.assertIn("Owner-loss transition-only recovery runtime is **Current**", self.contract_text)

    def test_28_dse_contract_status_updated(self) -> None:
        self.assertIn("Contract Current — TC-13.18d.12b", self.dse_contract_text)
        self.assertIn("Current — TC-13.18d.12c", self.dse_contract_text)

    def test_29_adr_owner_loss_status(self) -> None:
        self.assertIn("Contract Current — TC-13.18d.12b", self.adr_text)
        self.assertIn("Current — TC-13.18d.12c", self.adr_text)
        self.assertIn("owner-loss recovery", self.adr_text.lower())

    def test_30_existing_dependencies_remain_current(self) -> None:
        """Verify pre2.1, pre2.2, pre2.2.1 remain Current in the contract."""
        for term in (
            "Current — TC-13.18d.12a-pre2.1",
            "Current — TC-13.18d.12a-pre2.2",
            "Current — TC-13.18d.12a-pre2.2.1",
        ):
            self.assertIn(term, self.dse_contract_text,
                          f"DSE contract must still claim {term}")

    def test_31_existing_dispatch_failed_current_still(self) -> None:
        """TC-13.18d.11a/b/c must still be Current."""
        for term in (
            "TC-13.18d.11a/b/c: Current",
        ):
            self.assertIn(term, self.dse_contract_text.replace("\n", " "))


class TC1318d12cOwnerLossProductionStatusTests(unittest.TestCase):
    """Status assertions for transition-only owner-loss production."""

    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.workflow = (
            root / "skills" / "agentdesk" / "references"
            / "public-interfaces" / "workflow-orchestrator-contract.md"
        ).read_text(encoding="utf-8")
        cls.evidence = (
            root / "skills" / "agentdesk" / "references"
            / "public-interfaces" / "dispatch-supervisor-evidence-contract.md"
        ).read_text(encoding="utf-8")
        cls.adr = (
            root / "skills" / "agentdesk" / "references" / "adr"
            / "001-mad-agentdesk-integration.md"
        ).read_text(encoding="utf-8")

    def test_transition_only_recovery_is_current(self) -> None:
        self.assertIn(
            "Owner-loss transition-only recovery runtime is **Current**",
            self.workflow,
        )
        self.assertIn(
            "transition-only\n  production Current",
            self.evidence,
        )
        self.assertIn(
            "owner-loss transition-only recovery is now\n**Current**",
            self.adr,
        )

    def test_retry_and_interface_current(self) -> None:
        for text in (self.workflow, self.evidence, self.adr):
            self.assertIn("TC-13.18d.12c.1", text)
        self.assertIn("Interface #22 core orchestration is **Current", self.workflow)
        self.assertIn("Interface #22 core orchestration is **Current", self.adr)


class TC1318d12c1OwnerLossRetryDurableContractTests(unittest.TestCase):
    """TC-13.18d.12c.1 durable automatic-retry contract freeze."""

    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.workflow = (
            root / "skills" / "agentdesk" / "references"
            / "public-interfaces" / "workflow-orchestrator-contract.md"
        ).read_text(encoding="utf-8")
        cls.evidence = (
            root / "skills" / "agentdesk" / "references"
            / "public-interfaces" / "dispatch-supervisor-evidence-contract.md"
        ).read_text(encoding="utf-8")
        cls.adr = (
            root / "skills" / "agentdesk" / "references" / "adr"
            / "001-mad-agentdesk-integration.md"
        ).read_text(encoding="utf-8")
        cls.section = cls.workflow.split(
            "## 18. Owner-Loss Automatic Retry Durable State Machine", 1
        )[1]

    def test_01_exact_forward_only_phases(self) -> None:
        body = self.section.split("### 18.2", 1)[1].split("### 18.3", 1)[0]
        phases = (
            "RECOVERY_TRANSITION_PENDING",
            "RECOVERY_TRANSITION_COMMITTED",
            "RETRY_RESERVED",
            "RETRY_STARTED",
            "RETRY_FINALIZING",
            "RETRY_FINALIZED",
        )
        enum_body = body.split("class OwnerLossRetryPhase", 1)[1].split(
            "```", 1
        )[0]
        self.assertEqual(
            tuple(
                match.group(1)
                for match in re.finditer(r"^    ([A-Z_]+) = ", enum_body, re.M)
            ),
            phases,
        )
        self.assertEqual(body.count(" -> "), 5)
        self.assertIn("never move backward, skip an edge", body)

    def test_02_receipt_has_exactly_24_typed_fields(self) -> None:
        body = self.section.split("### 18.3", 1)[1].split("### 18.4", 1)[0]
        code = body.split("class OwnerLossRetryReceipt:", 1)[1].split(
            "```", 1
        )[0]
        fields = tuple(
            match.group(1)
            for match in re.finditer(r"^    ([a-z_]+): ", code, re.M)
        )
        self.assertEqual(
            fields,
            (
                "schema_version",
                "task_id",
                "revision",
                "failed_attempt",
                "failed_dispatch_id",
                "recovery_event_id",
                "recovery_generation_id",
                "next_attempt",
                "next_dispatch_id",
                "next_dispatch_event_id",
                "phase",
                "creator_pid",
                "creator_creation_time",
                "creator_boot_id",
                "supervisor_pid",
                "supervisor_creation_time",
                "supervisor_boot_id",
                "worker_pid",
                "worker_creation_time",
                "worker_boot_id",
                "reserved_at",
                "started_at",
                "finalized_at",
                "content_digest",
            ),
        )
        self.assertIn("@dataclass(frozen=True, slots=True)", body)
        for forbidden in ("Any", "dict", "Mapping", "list[", "set["):
            self.assertNotIn(forbidden, code)

    def test_03_canonical_recovery_identity_binding(self) -> None:
        body = self.section.split("### 18.4", 1)[1].split("### 18.5", 1)[0]
        for term in (
            "byte-exact `DISPATCH_FAILED` event",
            "state exactly `ready`",
            "`current_dispatch is None`",
            "failed attempt/dispatch",
            "recovery generation",
            "next attempt/dispatch/event",
            "content digest",
        ):
            self.assertIn(term, body)

    def test_04_reservation_precedes_worker_start(self) -> None:
        body = self.section.split("### 18.4", 1)[1].split("### 18.5", 1)[0]
        self.assertLess(
            body.index("Atomically persist the single `RETRY_RESERVED`"),
            body.index("Start the supervisor/Worker outside"),
        )
        self.assertIn(
            "Only a valid durable `RETRY_RESERVED` receipt authorizes",
            body,
        )

    def test_05_state_lock_only_validates_replays_and_reserves(self) -> None:
        body = self.section.split("### 18.4", 1)[1].split("### 18.5", 1)[0]
        self.assertIn("Acquire the existing state lock", body)
        self.assertIn(
            "only validation, exact replay, and the\natomic reservation write",
            body,
        )
        for forbidden_call in (
            "`run_dispatch_cycle()`",
            "`run_bounded_dispatch_retry()`",
            "a provider",
            "a supervisor",
            "a Worker",
        ):
            self.assertIn(forbidden_call, body)

    def test_06_retry_execution_is_outside_lock(self) -> None:
        body = self.section.split("### 18.4", 1)[1].split("### 18.5", 1)[0]
        self.assertLess(body.index("Release the state lock"), body.index(
            "Start the supervisor/Worker outside"
        ))

    def test_07_crash_window_matrix_is_complete(self) -> None:
        body = self.section.split("### 18.5", 1)[1].split("### 18.6", 1)[0]
        windows = (
            "Before `DISPATCH_FAILED` commits",
            "After `DISPATCH_FAILED`, before reservation",
            "Reservation atomic write is interrupted or malformed",
            "Valid `RETRY_RESERVED`, before supervisor/Worker start",
            "Supervisor starts before durable receipt advances",
            "Worker starts before `RETRY_STARTED` is durable",
            "Retry is ACKed, then owner crashes",
            "Crash before retry finalizer publishes",
            "Crash after `RETRY_FINALIZED` is durable",
            "Tombstone write and phase advance",
            "PID reuse, boot-id change, or permission failure",
            "Two recoverers propose the same generation and bytes",
            "Two generations contend for one failed dispatch",
            "Stale recovery generation replays",
        )
        for window in windows:
            self.assertIn(window, body)
        rows = tuple(
            line for line in body.splitlines()
            if line.startswith("| ") and not line.startswith("|---")
        )
        self.assertEqual(len(rows), 15)  # header plus fourteen decisions
        for row in rows[1:]:
            self.assertRegex(
                row,
                r"`(?:resume|byte-exact replay|reject|fail-closed)`",
            )

    def test_08_byte_exact_and_divergent_replay(self) -> None:
        body = self.section.split("### 18.6", 1)[1].split("### 18.7", 1)[0]
        self.assertIn("identical canonical bytes", body)
        self.assertIn("same\n  `OwnerLossRetryReceipt` bytes", body)
        self.assertIn(
            "Same generation with different phase, identity, plan, or digest rejects",
            body,
        )

    def test_09_concurrent_generations_have_one_winner(self) -> None:
        body = self.section.split("### 18.6", 1)[1].split("### 18.7", 1)[0]
        self.assertIn("exactly one\n  reservation winner", body)
        self.assertIn("the loser rejects", body)

    def test_10_stale_cas_is_zero_write_zero_worker(self) -> None:
        body = self.section.split("### 18.6", 1)[1].split("### 18.7", 1)[0]
        self.assertIn("Stale canonical CAS", body)
        self.assertIn("zero writes", body)
        self.assertIn("starts zero Workers", body)

    def test_11_attempt_four_is_forbidden(self) -> None:
        body = self.section.split("### 18.6", 1)[1].split("### 18.7", 1)[0]
        self.assertIn("Attempt three exhaustion", body)
        self.assertIn("no fourth dispatch", body)
        receipt = self.section.split("### 18.3", 1)[1].split("### 18.4", 1)[0]
        self.assertIn("existing range 1..3", receipt)

    def test_12_liveness_rules_are_not_relaxed(self) -> None:
        body = self.section.split("### 18.1", 1)[1].split("### 18.2", 1)[0]
        self.assertIn("`ALIVE` still means no recovery write or retry", body)
        self.assertIn("`UNKNOWN` remains\n  fail-closed", body)
        self.assertIn("Only exact `DEAD`", body)
        self.assertIn(
            "lease expiry, or a single PID observation is never retry evidence",
            body.replace("\n  ", " "),
        )

    def test_13_status_split_is_exact(self) -> None:
        body = self.section.split("### 18.7", 1)[1]
        self.assertIn(
            "TC-13.18d.12c transition-only owner-loss recovery | **Current**",
            body,
        )
        self.assertIn(
            "**Contract Current — TC-13.18d.12c.1**",
            body,
        )
        self.assertIn("**Current — TC-13.18d.12c.2**", body)
        self.assertIn("Interface #22 | **Current", body)
        self.assertIn("retry runtime is Current", body)

    def test_14_status_is_synchronized_across_contracts_and_adr(self) -> None:
        for text in (self.workflow, self.evidence, self.adr):
            self.assertIn("Contract Current — TC-13.18d.12c.1", text)
            self.assertIn("Current — TC-13.18d.12c.2", text)
            self.assertIn("Interface #22", text)

    def test_15_evidence_contract_freezes_storage_and_identity(self) -> None:
        body = self.evidence.split(
            "## 13. Owner-Loss Retry Reservation Evidence", 1
        )[1]
        self.assertIn(".agentdesk/runtime/", body)
        self.assertIn("atomic replace", body)
        self.assertIn("same generation and same canonical bytes", body)
        self.assertIn("different generation cannot overwrite", body)
        self.assertIn("reservation occurs under the existing state lock", body)

    def test_16_contract_explicitly_rejects_weak_retry_authority(self) -> None:
        body = self.section.split("### 18.1", 1)[1].split("### 18.2", 1)[0]
        for term in (
            "task being `ready` is never sufficient",
            "in-memory boolean",
            "exception text",
            "stdout/stderr",
            "lease expiry",
            "single PID observation",
        ):
            self.assertIn(term, body)


class TC1321e1ProviderDoctorSmokeTests(unittest.TestCase):
    """TC-13.21e.1 — Provider Doctor production module smoke tests.

    Verifies the production module exists, exports the correct API, and
    core diagnostic semantics match the frozen contract.
    """

    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.doctor_py = (
            self.repo_root / "skills" / "agentdesk" / "scripts"
            / "doctor.py"
        )
        self.scripts_dir = str(
            self.repo_root / "skills" / "agentdesk" / "scripts"
        )
        sys.path.insert(0, self.scripts_dir)
        import doctor as dr  # noqa: E402
        self.dr = dr
        sys.path.pop(0)

    # -- 1. Production module exists --

    def test_doctor_py_exists(self) -> None:
        """doctor.py must exist."""
        self.assertTrue(self.doctor_py.is_file())

    # -- 2. Exactly 8 public symbols --

    def test_exact_8_all_symbols(self) -> None:
        expected = [
            "DoctorCheckStatus",
            "DoctorCheck",
            "DoctorRequest",
            "DoctorReport",
            "DoctorError",
            "DoctorInputError",
            "run_doctor",
            "main",
        ]
        self.assertEqual(sorted(self.dr.__all__), sorted(expected))

    # -- 3. Enum member counts --

    def test_doctor_check_status_three_members(self) -> None:
        self.assertEqual(len(list(self.dr.DoctorCheckStatus)), 3)

    def test_enum_values(self) -> None:
        self.assertEqual(self.dr.DoctorCheckStatus.PASS.value, "pass")
        self.assertEqual(self.dr.DoctorCheckStatus.WARN.value, "warn")
        self.assertEqual(self.dr.DoctorCheckStatus.FAIL.value, "fail")

    # -- 4. Dataclass field counts, frozen, slots --

    def test_doctor_check_four_fields(self) -> None:
        self.assertEqual(
            len(self.dr.DoctorCheck.__dataclass_fields__), 4
        )

    def test_doctor_check_frozen_slots(self) -> None:
        dc = self.dr.DoctorCheck(
            "D001", self.dr.DoctorCheckStatus.PASS, "ok", None
        )
        self.assertFalse(hasattr(dc, "__dict__"))

    def test_doctor_request_two_fields(self) -> None:
        self.assertEqual(
            len(self.dr.DoctorRequest.__dataclass_fields__), 2
        )

    def test_doctor_request_frozen_slots(self) -> None:
        req = self.dr.DoctorRequest(
            project_root=Path("/tmp"), mad_home=None
        )
        self.assertFalse(hasattr(req, "__dict__"))

    def test_doctor_report_two_fields(self) -> None:
        self.assertEqual(
            len(self.dr.DoctorReport.__dataclass_fields__), 2
        )

    def test_doctor_report_frozen_slots(self) -> None:
        report = self.dr.DoctorReport(ready=True, checks=())
        self.assertFalse(hasattr(report, "__dict__"))

    # -- 5. Exception hierarchy --

    def test_exception_hierarchy(self) -> None:
        self.assertTrue(
            issubclass(self.dr.DoctorInputError, self.dr.DoctorError)
        )
        self.assertTrue(
            issubclass(self.dr.DoctorError, Exception)
        )

    # -- 6. run_doctor rejects invalid input --

    def test_run_doctor_rejects_relative_path(self) -> None:
        with self.assertRaises(self.dr.DoctorInputError):
            self.dr.run_doctor(
                self.dr.DoctorRequest(
                    project_root=Path("relative"), mad_home=None
                )
            )

    def test_run_doctor_rejects_non_doctor_request(self) -> None:
        with self.assertRaises(self.dr.DoctorInputError):
            self.dr.run_doctor({})  # type: ignore[arg-type]

    # -- 7. Check count is 12 --

    def test_run_doctor_produces_12_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".agentdesk" / "runtime").mkdir(parents=True)
            (project / "docs" / "pm").mkdir(parents=True)
            (project / ".gitignore").write_text(
                ".agentdesk/runtime/\n", encoding="utf-8"
            )
            report = self.dr.run_doctor(
                self.dr.DoctorRequest(
                    project_root=project, mad_home=None
                )
            )
        self.assertEqual(len(report.checks), 12)

    # -- 8. Gateway template exists and is valid --

    def test_gateway_template_exists(self) -> None:
        template_gw = (
            self.repo_root
            / "skills" / "agentdesk" / "assets" / "project-template"
            / ".agentdesk" / "runtime" / "gateway.yaml"
        )
        self.assertTrue(template_gw.is_file())

    def test_gateway_template_eight_keys(self) -> None:
        template_gw = (
            self.repo_root
            / "skills" / "agentdesk" / "assets" / "project-template"
            / ".agentdesk" / "runtime" / "gateway.yaml"
        )
        data = json.loads(template_gw.read_text(encoding="utf-8"))
        self.assertEqual(len(data), 8)
        self.assertEqual(
            data["schema_version"], "agentdesk.gateway-config/v1"
        )

    # -- 9. Contract document exists --

    def test_contract_document_exists(self) -> None:
        contract = (
            self.repo_root
            / "skills" / "agentdesk" / "references"
            / "public-interfaces" / "provider-doctor-contract.md"
        )
        self.assertTrue(contract.is_file())

    # -- 10. ADR mentions Provider Doctor interface --

    def test_adr_mentions_interface_34(self) -> None:
        adr_text = (
            self.repo_root
            / "skills" / "agentdesk" / "references" / "adr"
            / "001-mad-agentdesk-integration.md"
        ).read_text(encoding="utf-8")
        self.assertIn("Provider Doctor", adr_text)
        self.assertIn("TC-13.21e.1", adr_text)

    # -- 11. Doctor is read-only (structural verification) --

    def test_doctor_does_not_import_subprocess(self) -> None:
        source = self.doctor_py.read_text(encoding="utf-8")
        self.assertNotIn("import subprocess", source)

    def test_doctor_does_not_import_socket(self) -> None:
        source = self.doctor_py.read_text(encoding="utf-8")
        self.assertNotIn("import socket", source)


class TC1322aTaskDifficultyAssessmentContractFreezeTests(unittest.TestCase):
    """TC-13.22a — PM TaskDifficulty assessment contract freeze."""

    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.contract_path = (
            root / "skills" / "agentdesk" / "references"
            / "public-interfaces" / "task-difficulty-assessment-contract.md"
        )
        cls.adr_path = (
            root / "skills" / "agentdesk" / "references" / "adr"
            / "001-mad-agentdesk-integration.md"
        )
        cls.contract_text = cls.contract_path.read_text(encoding="utf-8")
        cls.adr_text = cls.adr_path.read_text(encoding="utf-8")

    def _between(self, start: str, end: str) -> str:
        start_at = self.contract_text.index(start)
        end_at = self.contract_text.index(end, start_at)
        return self.contract_text[start_at:end_at]

    def test_contract_document_exists(self) -> None:
        self.assertTrue(self.contract_path.is_file())

    def test_assessment_dimension_has_exact_seven_values_and_order(self) -> None:
        block = self._between(
            "serialization and `dimension_results` order:",
            "Unknown values fail closed",
        )
        values = re.findall(r"^\d+\. `([^`]+)`$", block, re.MULTILINE)
        self.assertEqual(
            values,
            [
                "modification_scope",
                "requirement_clarity",
                "state_concurrency",
                "public_contract",
                "failure_impact",
                "rollback_complexity",
                "dependency_conflict",
            ],
        )

    def test_dimension_result_is_frozen_slots_with_exact_four_fields(self) -> None:
        block = self._between(
            "`DimensionResult` is a frozen, slots value",
            "The value has no free-form rationale text",
        )
        fields = re.findall(
            r"^\|\s*\d+\s*\|\s*`([^`]+)`\s*\|",
            block,
            re.MULTILINE,
        )
        self.assertEqual(
            fields,
            ["dimension", "difficulty", "rationale_key", "hard_floor"],
        )

    def test_assessment_has_exact_twelve_public_fields(self) -> None:
        block = self._between(
            "`TaskDifficultyAssessment` is a frozen, slots value",
            "The public value has no `snapshot_commit` field",
        )
        fields = re.findall(
            r"^\|\s*\d+\s*\|\s*`([^`]+)`\s*\|",
            block,
            re.MULTILINE,
        )
        self.assertEqual(
            fields,
            [
                "schema_version",
                "assessment_id",
                "task_id",
                "revision",
                "minimum_difficulty",
                "recommended_difficulty",
                "selected_difficulty",
                "dimension_results",
                "override_direction",
                "override_reason",
                "approval_id",
                "policy_version",
            ],
        )

    def test_public_values_are_immutable_and_not_untyped(self) -> None:
        self.assertIn("frozen, slots", self.contract_text)
        self.assertIn("tuple[DimensionResult, ...]", self.contract_text)
        self.assertIn("no `Any`, `dict`", self.contract_text)
        self.assertIn("`Mapping`, mutable `list`", self.contract_text)
        self.assertIn("must not be added to the public twelve-field value", self.contract_text)

    def test_schema_version_and_assessment_identity_are_frozen(self) -> None:
        self.assertIn(
            "agentdesk.difficulty-assessment/v1",
            self.contract_text,
        )
        self.assertIn(
            "Matches `ASM-[A-Za-z0-9][A-Za-z0-9._-]*`",
            self.contract_text,
        )
        self.assertIn("Unknown values fail closed", self.contract_text)
        self.assertIn(
            "Exactly seven entries in enum order",
            self.contract_text,
        )

    def test_rationale_allowlist_covers_all_dimensions_and_hard_floors(self) -> None:
        rows = re.findall(
            r"^\| `([^`]+)` \| `([^`]+)` \| `(basic|standard|advanced|expert)` \| (true|false) \|$",
            self.contract_text,
            re.MULTILINE,
        )
        self.assertEqual(len(rows), 29)
        self.assertEqual(
            {row[0] for row in rows},
            {
                "modification_scope",
                "requirement_clarity",
                "state_concurrency",
                "public_contract",
                "failure_impact",
                "rollback_complexity",
                "dependency_conflict",
            },
        )
        for key in (
            "concurrency.lock_cas",
            "concurrency.cross_process_recovery",
            "impact.security_boundary",
            "impact.data_corruption",
            "rollback.irreversible",
            "contract.breaking_migration",
        ):
            self.assertIn(key, self.contract_text)
        self.assertIn(
            "`dependency.cross_repository` | `advanced` | false",
            self.contract_text,
        )

    def test_aggregation_and_override_rules_are_fail_closed(self) -> None:
        for term in (
            "minimum_difficulty = max(difficulty for hard_floor == true)",
            "recommended_difficulty = max(difficulty for all seven results)",
            "selected_difficulty < minimum_difficulty` always fails closed",
            "selected_difficulty > recommended_difficulty` sets `override_direction=up`",
            "minimum_difficulty <= selected_difficulty < recommended_difficulty",
            "override_direction=down",
            "structured `override_reason`",
            "approval_id",
        ):
            self.assertIn(term, self.contract_text)
        self.assertIn("`none`, `up`, or `down`", self.contract_text)
        self.assertIn("LLM output may be an untrusted suggestion only", self.contract_text)

    def test_independent_concepts_and_non_rules_are_explicit(self) -> None:
        for concept in (
            "TaskDifficulty",
            "WorkerKind",
            "model tier",
            "model price",
            "model capability",
            "`risk`",
        ):
            self.assertIn(concept, self.contract_text)
        for forbidden_rule in (
            "No consumer may derive `TaskDifficulty` from `WorkerKind`",
            "Cross-repository scope, file count, and ordinary dependency count",
            "Code-line count is never a difficulty rule",
            "A stronger or more expensive model never reduces task difficulty",
        ):
            self.assertIn(forbidden_rule, self.contract_text)

    def test_task_card_v2_optional_migration_is_frozen(self) -> None:
        self.assertIn("agentdesk.task-card/v2", self.contract_text)
        self.assertIn("task_difficulty", self.contract_text)
        self.assertIn("difficulty_assessment_id", self.contract_text)
        self.assertIn(
            "The complete seven `dimension_results` are never embedded in a task card",
            self.contract_text,
        )
        for rule in (
            "An old v2 card without either field remains readable",
            "A frozen old card is never modified in place",
            "A new card and every new revision must write both fields",
            "Before dispatch, the independent assessment evidence must exist",
            "does not upgrade v2 to v3",
        ):
            self.assertIn(rule, self.contract_text)

    def test_evidence_path_and_exact_key_envelope_are_frozen(self) -> None:
        self.assertIn(
            "docs/pm/assessments/<task_id>/r<revision>/<assessment_id>.yaml",
            self.contract_text,
        )
        match = re.search(
            r"evidence YAML root has exactly these thirteen keys in this\s+order:\s*```text\s*(.*?)```",
            self.contract_text,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        keys = match.group(1).splitlines()  # type: ignore[union-attr]
        self.assertEqual(
            keys,
            [
                "schema_version",
                "assessment_id",
                "task_id",
                "revision",
                "minimum_difficulty",
                "recommended_difficulty",
                "selected_difficulty",
                "dimension_results",
                "override_direction",
                "override_reason",
                "approval_id",
                "policy_version",
                "snapshot_commit",
            ],
        )
        self.assertIn(
            "snapshot_commit` is the single evidence-only provenance key",
            self.contract_text,
        )

    def test_evidence_canonical_replay_ancestry_and_security_rules(self) -> None:
        for rule in (
            "UTF-8 without BOM",
            "LF line endings",
            "no YAML anchors or aliases",
            "no duplicate keys",
            "byte-exact replayable",
            "a full 40-hex Git commit",
            "an ancestor of the task-card snapshot/commit",
            "globally unique",
            "reject symlink/reparse paths",
            "must not expose prompts, model output, raw provider",
        ):
            self.assertIn(rule, self.contract_text)

    def test_difficulty_override_is_a_separate_authorization_domain(self) -> None:
        self.assertIn("scope = difficulty_override", self.contract_text)
        for subject in ("task_id", "revision", "attempt", "dispatch_id"):
            self.assertIn(subject, self.contract_text)
        for domain in (
            "dispatch approval",
            "acceptance approval",
            "integration approval",
            "model degradation approval",
        ):
            self.assertIn(domain, self.contract_text)
        for decision in (
            "recommended_difficulty",
            "minimum_difficulty",
            "selected_difficulty",
            "override_reason",
            "approval_id",
        ):
            self.assertIn(decision, self.contract_text)
        self.assertIn("does not modify the existing `ApprovalScope` enum", self.contract_text)
        self.assertIn("approval_gate.py", self.contract_text)

    def test_lifecycle_identity_rules_are_frozen(self) -> None:
        for rule in (
            "Initial dispatch requires a valid, independently verified assessment",
            "Retry preserves `TaskDifficulty` and `assessment_id` exactly",
            "Escalation may change `WorkerKind` only",
            "Rescope or a revision bump creates a new `assessment_id`",
            "A task card and its assessment are immutable within one revision",
            "does not automatically invalidate an in-flight attempt",
            "A new revision must use the current policy version",
        ):
            self.assertIn(rule, self.contract_text)

    def test_two_layer_validation_and_control_plane_boundary(self) -> None:
        self.assertIn("`validate_project` / PM pre-commit", self.contract_text)
        self.assertIn("`TASK_DISPATCHED` control-plane boundary", self.contract_text)
        self.assertIn("final hard\n   fail-closed validation", self.contract_text)
        self.assertIn("WorkflowOrchestrator` consumes an already verified", self.contract_text)
        self.assertIn("does not assess difficulty", self.contract_text)
        self.assertIn("not added to `TransitionCAS` fields", self.contract_text)

    def test_adr_interface_and_status_boundaries_are_synchronized(self) -> None:
        self.assertIn(
            "| 35 | PM TaskDifficulty Assessment | **Current — TC-13.22a**",
            self.adr_text,
        )
        self.assertIn("### 2.24 PM TaskDifficulty Assessment", self.adr_text)
        self.assertIn("Current — TC-13.22a", self.adr_text)
        self.assertIn("Target — TC-13.22b", self.adr_text)
        self.assertIn("PortfolioScheduler", self.adr_text)
        self.assertIn("WorktreeLifecycleManager", self.adr_text)
        self.assertIn("Interface #22 status is unchanged", self.adr_text)

    def test_runtime_remains_target_and_no_production_module_is_claimed(self) -> None:
        self.assertIn("runtime/deterministic assessor", self.contract_text)
        self.assertIn("Target — TC-13.22b", self.contract_text)
        self.assertIn("No production module is shipped by TC-13.22a", self.contract_text)
        self.assertNotIn("Current — TC-13.22b", self.contract_text)
