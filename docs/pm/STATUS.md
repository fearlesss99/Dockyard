# Project Status

<!-- GENERATED FILE: source=docs/pm/state/tasks.yaml -->
> Generated view — do not edit by hand. The only current-state authority is `docs/pm/state/tasks.yaml`.
> 生成时间（取状态源 `updated_at`，确保结果可复现）：`2026-08-02T10:35:00Z`

## Project control

| Field | Value |
| --- | --- |
| Project | AgentDeskSkill-0.1.0-beta-standard-validation |
| Schema | `agentdesk.tasks/v2` |
| Adoption level | `standard` |
| State updated | `2026-08-02T10:35:00Z` |
| PM holder | pm-session-bootstrap |
| PM lease epoch | 1 |
| PM control mode | `timed` |

## State summary

| State | Count |
| --- | ---: |
| `draft` | 0 |
| `ready` | 0 |
| `dispatched` | 1 |
| `in_progress` | 0 |
| `review_ready` | 0 |
| `returned` | 0 |
| `blocked` | 0 |
| `accepted` | 0 |
| `integrated` | 0 |
| `cancelled` | 0 |
| `superseded` | 0 |

## Queues by state

| State | Tasks |
| --- | --- |
| `draft` | — |
| `ready` | — |
| `dispatched` | TC-001 |
| `in_progress` | — |
| `review_ready` | — |
| `returned` | — |
| `blocked` | — |
| `accepted` | — |
| `integrated` | — |
| `cancelled` | — |
| `superseded` | — |

## Current dispatches

| Task | State | Dispatch | Role | Required model | Selected model | Binding | Branch | Base commit |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| TC-001 | dispatched | DSP-TC001-R1-A1-0001 | DEV | standard | expert: claude/claude-opus-4-8[1M] | deepseek-v4-pro-via-claude-opus | feat/tc-001-r1-standard-dispatch-probe | c60444e37656197bbc23d8f9d3072c4186f5a0d9 |
