---
schema_version: agentdesk.task-card/v2
task_id: TC-001
revision: 2
type: spike
role_id: DEV
priority: P2
risk: L0
min_model_tier: inherit
required_model_capabilities: [coding, testing]
depends_on: []
base_commit: "8a14c9cc61712a7474fe9cde8ceec416dc01334c"
allowed_paths:
  - docs/pm/reports/TC-001-r1-a1.md
blocked_paths:
  - docs/pm/state/**
  - docs/pm/events/**
  - docs/pm/outbox/**
  - docs/pm/tasks/**
  - docs/pm/acceptances/**
conflict_surfaces:
  paths: [docs/pm/reports/TC-001-r1-a1.md]
  symbols: []
  contracts: []
  migrations: []
required_checks: [agentdesk.build, agentdesk.typecheck, agentdesk.lint]
owner_approval:
  gate: none
  required_capabilities: []
  approver_role_id: Owner
callback_destination_role_id: PM
callback_transport: runtime_route
callback_required: true
created_at: "2026-08-02T10:37:00Z"
---

# TC-001 · Standard dispatch and callback probe

## Goal

Run the three frozen non-network checks and produce one attempt-specific report that records their exact results. This task validates the R1 execution and PM callback path; it does not change production code or control-plane state.

## Context Budget

Required:

- `AGENTS.md`
- This task card
- `docs/pm/CHECKS.yaml`
- `skills/agentdesk/assets/delivery-report-template.md`

Read if needed:

- `skills/agentdesk/references/codex-runtime-adapter.md`

Do not read by default:

- Unrelated historical reports
- Full project archive

## Scope

Allowed:

- Execute only the required check IDs from `CHECKS.yaml`.
- Create the exact attempt report path.

Forbidden:

- Production code changes.
- Any PM control-plane mutation.
- Network, provider, model, or external API calls.
- PM acceptance or integration.

## Acceptance Criteria

- [ ] The report records each required check ID, argv, result, and timestamp.
- [ ] The report records no production-code or control-plane changes.
- [ ] The report is committed and the worker actively sends a callback containing the immutable dispatch quartet.

## Delivery

Use the dispatch-provided branch, attempt, IDs, base commit, and exact report path. Commit the report as the task evidence. A completed report requests PM review; it is not a callback or acceptance.

After the report commit exists, actively send one completion/blocked callback to logical role `PM` through its verified runtime route. Reuse the same `callback_id` for every transport retry and require a successful runtime receipt before declaring the handoff complete. If callback delivery fails, preserve the report and callback ID, surface the transport failure, and leave the callback retryable.

Never place a host ID, session/thread ID, absolute worktree path, provider receipt, retry timer, credential, or other runtime routing value in this card or the report. Those values belong only in gitignored `.agentdesk/runtime/`.
