# Project Status

<!-- GENERATED FILE: source=docs/pm/state/tasks.yaml -->
> Generated view — do not edit by hand. The only current-state authority is `docs/pm/state/tasks.yaml`.
> 生成时间（取状态源 `updated_at`，确保结果可复现）：`2026-08-06T13:47:20Z`

## Project control

| Field | Value |
| --- | --- |
| Project | AgentDeskSkill-0.1.0-beta-standard-validation |
| Schema | `agentdesk.tasks/v2` |
| Adoption level | `standard` |
| State updated | `2026-08-06T13:47:20Z` |
| PM holder | pm-session-bootstrap |
| PM lease epoch | 1 |
| PM control mode | `timed` |

## State summary

| State | Count |
| --- | ---: |
| `draft` | 0 |
| `ready` | 0 |
| `dispatched` | 0 |
| `in_progress` | 1 |
| `review_ready` | 0 |
| `returned` | 0 |
| `blocked` | 0 |
| `accepted` | 0 |
| `integrated` | 0 |
| `cancelled` | 1 |
| `superseded` | 0 |

## Queues by state

| State | Tasks |
| --- | --- |
| `draft` | — |
| `ready` | — |
| `dispatched` | — |
| `in_progress` | TC-08827265041816624196 |
| `review_ready` | — |
| `returned` | — |
| `blocked` | — |
| `accepted` | — |
| `integrated` | — |
| `cancelled` | TC-001 |
| `superseded` | — |

## Current dispatches

| Task | State | Dispatch | Role | Required model | Selected model | Binding | Branch | Base commit |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| TC-08827265041816624196 | in_progress | DSP-74869b0e30a0dfb85f31 | R1 | advanced | expert: claude/claude-opus-4-8[1M] | deepseek-v4-pro-via-claude-opus | agentdesk/TC-08827265041816624196/r1/a1/DSP-74869b0e30a0dfb85f31 | 282c500eca057e4372c1072659e8f23012eb31ae |
