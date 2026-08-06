# AgentDesk Git Integration Owner Contract

Status: **Contract Current — TC-13.29l.5h.1**
Runtime: **Current — TC-13.29l.5h.2**
Schema: `agentdesk.git-integration/v1`

## 1. Ownership

The Git Integration owner creates or adopts one real local integration commit
and advances one exact target ref with old-value CAS. It does not apply
`CHANGE_INTEGRATED`; Interface #22 remains the sole canonical lifecycle owner.
Dockyard never executes Git directly and consumes only the typed result.

The owner performs no MAD, Worker, provider, model, API, network, approval, or
task-state action. It never runs reset, restore, clean, stash, rebase, amend,
push, or a checkout that mutates the user's main worktree.

## 2. Frozen public types

All public types are `frozen=True, slots=True`. Public annotations contain no
`Any`, `object`, `dict`, `Mapping`, mutable collection, callback, process
handle, stdout/stderr, prompt, report body, credential, provider, or API key.

### 2.1 `GitIntegrationMethod`

```text
FAST_FORWARD
MERGE_TREE
```

### 2.2 `GitIntegrationPhase`

```text
RESERVED
VALIDATED
TREE_PREPARED
REF_UPDATED
FINALIZED
```

Phases advance by exactly one position. A finalized operation never reopens.

### 2.3 `GitIntegrationOutcome`

```text
RESERVED
FAST_FORWARDED
MERGED
ALREADY_INTEGRATED
CONFLICT
RECOVERY_REQUIRED
REJECTED
FINALIZED
```

### 2.4 `GitIntegrationRequest` — exactly 18 fields

| # | Field | Type |
|---:|---|---|
| 1 | `schema_version` | `str` |
| 2 | `operation_id` | `str` |
| 3 | `project_root` | `Path` |
| 4 | `task_id` | `str` |
| 5 | `revision` | `int` |
| 6 | `attempt` | `int` |
| 7 | `dispatch_id` | `str` |
| 8 | `source_worktree` | `Path` |
| 9 | `source_branch` | `str` |
| 10 | `base_commit` | `str` |
| 11 | `implementation_commit` | `str` |
| 12 | `report_commit` | `str` |
| 13 | `target_branch` | `str` |
| 14 | `expected_target_head` | `str` |
| 15 | `method` | `GitIntegrationMethod` |
| 16 | `commit_message` | `str` |
| 17 | `requested_at` | `str` |
| 18 | `content_digest` | `str` |

`attempt` is in `1..3`. Paths are absolute, existing, plain directories and
must resolve to the same Git common directory. Branch names pass
`git check-ref-format --branch`. All commits are lowercase 40-hex object IDs.
Implementation is an ancestor of report; base is an ancestor of report; source
branch resolves exactly to report. `expected_target_head` is the only legal
old value for the target-ref update.

### 2.5 `GitIntegrationReceipt` — exactly 22 fields

| # | Field | Type |
|---:|---|---|
| 1 | `schema_version` | `str` |
| 2 | `operation_id` | `str` |
| 3 | `request_content_digest` | `str` |
| 4 | `task_id` | `str` |
| 5 | `revision` | `int` |
| 6 | `attempt` | `int` |
| 7 | `dispatch_id` | `str` |
| 8 | `source_branch` | `str` |
| 9 | `report_commit` | `str` |
| 10 | `target_branch` | `str` |
| 11 | `target_head_before` | `str` |
| 12 | `phase` | `GitIntegrationPhase` |
| 13 | `method` | `GitIntegrationMethod` |
| 14 | `outcome` | `GitIntegrationOutcome` |
| 15 | `integrated_commit` | `str | None` |
| 16 | `integrated_tree` | `str | None` |
| 17 | `conflict_digest` | `str | None` |
| 18 | `reserved_at` | `str` |
| 19 | `ref_updated_at` | `str | None` |
| 20 | `finalized_at` | `str | None` |
| 21 | `updated_at` | `str` |
| 22 | `content_digest` | `str` |

## 3. Durable evidence

```text
.agentdesk/runtime/git-integration/requests/<operation_id>.yaml
.agentdesk/runtime/git-integration/receipts/<operation_id>.yaml
```

Files use canonical UTF-8 YAML, exact field order, LF and one final LF.
`sha256:` binds every field except `content_digest`. Writes use same-directory
atomic replace plus file and parent-directory durability. Symlink, junction,
reparse, extra sibling, non-regular, noncanonical, missing, or divergent files
fail closed. Same operation plus identical bytes is byte-exact replay; same
identity plus different bytes is rejected.

## 4. Integration algorithm

1. Reserve/replay request and `RESERVED` receipt under only the integration
   evidence lock; release it.
2. Validate repository identity, clean source worktree, commits, branches,
   ancestry, target HEAD and target-ref CAS without holding the evidence lock.
3. If target already contains report, adopt the exact target HEAD as
   `ALREADY_INTEGRATED` without a ref write.
4. If target equals an ancestor of report and method is `FAST_FORWARD`, prepare
   report as the integrated commit.
5. Otherwise method must be `MERGE_TREE`; use a tree-only Git merge operation.
   Conflict produces typed `CONFLICT`, a conflict digest and zero ref update.
6. Create a deterministic two-parent commit from caller-supplied message and
   timestamp; do not modify an index or checkout.
7. Update `refs/heads/<target_branch>` using exact old-value CAS.
8. Persist `REF_UPDATED`, verify the ref, then finalize. Interface #22 may
   record `CHANGE_INTEGRATED` only from a finalized typed receipt.

## 5. Crash and replay matrix

| Window | Required disposition |
|---|---|
| before request reservation | retry validation; zero Git write |
| request committed, receipt absent | byte-exact create `RESERVED` |
| receipt `RESERVED`, before validation | resume validation |
| source/target identity divergent | reject; zero ref update |
| target HEAD stale before tree preparation | reject stale CAS |
| fast-forward prepared, before ref update | resume exact report commit |
| merge tree prepared, before commit creation | recreate same tree/commit bytes |
| merge conflict | finalize typed `CONFLICT`; zero ref update |
| commit created, before ref update | resume old-value CAS |
| ref update succeeds, receipt lags | adopt only if ref equals exact integrated commit |
| ref update state unknown | `RECOVERY_REQUIRED`; never issue blind second update |
| competing identical operations | one ref winner; byte-exact replay |
| competing divergent operations | one CAS winner; loser rejects |
| finalized receipt | byte-exact replay/no-op |
| attempt 4 | reject before request write or Git command |

## 6. Status

- Contract: **Current — TC-13.29l.5h.1**
- Runtime: **Current — TC-13.29l.5h.2**
- Dockyard normal-path composition: **Current — TC-13.29l.5g.2c.2**
- Dockyard mid-flight crash resume: **Target**
- Dockyard local closed-loop E2E: **Blocked — TC-13.29l**

TC-13.29l.5h.2 implements the canonical request/receipt Store, validation,
fast-forward adoption, deterministic `merge-tree`/`commit-tree`, conflict
evidence, replay and old-value `update-ref` CAS. A target branch checked out in
any worktree is rejected before ref update so no user checkout or index is
made inconsistent.

TC-13.29l.5h.3 adds the owner-neutral finalized-receipt projection and the
durable Interface #22 completion entry point. The Git owner still performs no
canonical state transition. Only after its receipt is `FINALIZED` may
WorkflowOrchestrator validate the exact task/revision/attempt/dispatch/report,
integrated commit and receipt digest, then publish one lease-free
`CHANGE_INTEGRATED` without rerunning MAD or `DELIVERY_ACCEPTED`.

- Durable integration completion owner: **Current — TC-13.29l.5h.3**
- Dockyard composition: **Target — TC-13.29l.5g.2c.2**
- Dockyard local closed-loop E2E: **Blocked — TC-13.29l**
