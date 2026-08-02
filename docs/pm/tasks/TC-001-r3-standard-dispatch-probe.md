---
schema_version: agentdesk.task-card/v2
task_id: TC-001
revision: 3
type: spike
role_id: DEV
priority: P2
risk: L0
min_model_tier: inherit
required_model_capabilities: [coding, testing]
depends_on: []
base_commit: "c60444e37656197bbc23d8f9d3072c4186f5a0d9"
allowed_paths: [docs/pm/reports/TC-001-r3-a1.md]
blocked_paths: [docs/pm/state/**, docs/pm/events/**, docs/pm/outbox/**, docs/pm/tasks/**, docs/pm/acceptances/**]
conflict_surfaces:
  paths: [docs/pm/reports/TC-001-r3-a1.md]
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
created_at: "2026-08-02T10:40:00Z"
---

# TC-001 · Standard dispatch and callback probe

## Goal

Run the three frozen non-network checks and produce the exact attempt report. This task validates the R1 execution and PM callback path; it does not change production code or control-plane state.

## Context Budget

Required:

- `AGENTS.md`
- This task card
- `docs/pm/CHECKS.yaml`
- `skills/agentdesk/assets/delivery-report-template.md`

## Scope

Allowed:

- Execute only the required check IDs from `CHECKS.yaml`.
- Create the exact attempt report path.

Forbidden:

- Production code changes, PM control-plane mutations, network/provider/model/API calls, PM acceptance, or integration.

## Acceptance Criteria

- [ ] The report records each required check ID, argv, result, and timestamp.
- [ ] The report records no production-code or control-plane changes.
- [ ] The worker actively sends a callback containing the immutable dispatch quartet.

## Delivery

Use the dispatch-provided branch, attempt, IDs, base commit, and exact report path. Commit the report as the task evidence, then actively send a completion/blocked callback to logical role `PM` through its verified runtime route. Do not place runtime IDs, absolute paths, receipts, credentials, or provider data in the report.
