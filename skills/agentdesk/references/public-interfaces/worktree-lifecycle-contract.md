# WorktreeLifecycleManager Durable Contract

Status: **Contract Current — TC-13.25a**.  Durable Store, reconciliation, and
create/release runtime are **Target — TC-13.25b/TC-13.25c**.

## 1. Scope and authority

`WorktreeLifecycleManager` is an independent AgentDesk component that safely
reserves, creates, reconciles, and releases one managed Git worktree for one
task revision, attempt, and dispatch.  It does not select tasks, write
canonical task/event/outbox state, acquire a WorkerSlotLease, start a Worker,
or call a model, API, network, or provider.

`WorkflowOrchestrator` never runs `git worktree` commands.  It may consume a
validated `WorktreeLifecycleResult`, while all worktree inventory and Git side
effects remain owned by `WorktreeLifecycleManager`.

The only authoritative Git worktree inventory is the NUL-delimited output of
`git worktree list --porcelain -z`.  Human-readable Git output, directory
scanning, globbing, and guessed paths are never inventory evidence.

## 2. Frozen public types

All public data values are `@dataclass(frozen=True, slots=True)`.  Public
fields contain no `Any`, bare `dict`, `Mapping`, `list`, `set`, callback,
factory, mutable collection, subprocess object, exception text, stdout,
stderr, argv, environment, credential, token, or provider object.

### 2.1 WorktreePhase

The exact forward-only values, in order, are:

```text
RESERVED
CREATING
READY
RELEASING
RELEASED
```

Only adjacent transitions are legal.  No phase may be skipped, overwritten,
or regressed.  A repeated phase is legal only as byte-exact replay.

### 2.2 WorktreeLifecycleAction and outcomes

`WorktreeLifecycleAction` has exactly `CREATE`, `RELEASE`, and `RECONCILE`.

`WorktreeLifecycleOutcome` has exactly `READY`, `RELEASED`, `REPLAYED`,
`REFUSED`, and `RECOVERY_REQUIRED`.

`WorktreeReconciliationAction` has exactly `NO_OP`, `RESUME_CREATE`,
`ADOPT_READY`, `RESUME_RELEASE`, `REJECT`, and `FAIL_CLOSED`.

### 2.3 WorktreeInventoryEntry — exactly 8 fields

| # | Field | Type |
|---:|---|---|
| 1 | `repository_common_dir` | `str` |
| 2 | `worktree_path` | `str` |
| 3 | `head_commit` | `str` |
| 4 | `branch` | `str \| None` |
| 5 | `is_bare` | `bool` |
| 6 | `is_detached` | `bool` |
| 7 | `is_locked` | `bool` |
| 8 | `is_prunable` | `bool` |

### 2.4 WorktreeReservation — exactly 15 fields

| # | Field | Type |
|---:|---|---|
| 1 | `schema_version` | `str` |
| 2 | `reservation_id` | `str` |
| 3 | `worktree_id` | `str` |
| 4 | `repository_common_dir` | `str` |
| 5 | `repository_root` | `str` |
| 6 | `worktree_path` | `str` |
| 7 | `branch` | `str` |
| 8 | `base_commit` | `str` |
| 9 | `task_id` | `str` |
| 10 | `revision` | `int` |
| 11 | `attempt` | `int` |
| 12 | `dispatch_id` | `str` |
| 13 | `phase` | `WorktreePhase` |
| 14 | `reserved_at` | `str` |
| 15 | `content_digest` | `str` |

### 2.5 WorktreeRecord — exactly 19 fields

| # | Field | Type |
|---:|---|---|
| 1 | `schema_version` | `str` |
| 2 | `worktree_id` | `str` |
| 3 | `reservation_id` | `str` |
| 4 | `repository_common_dir` | `str` |
| 5 | `repository_root` | `str` |
| 6 | `worktree_path` | `str` |
| 7 | `branch` | `str` |
| 8 | `base_commit` | `str` |
| 9 | `head_commit` | `str` |
| 10 | `task_id` | `str` |
| 11 | `revision` | `int` |
| 12 | `attempt` | `int` |
| 13 | `dispatch_id` | `str` |
| 14 | `phase` | `WorktreePhase` |
| 15 | `created_at` | `str \| None` |
| 16 | `release_requested_at` | `str \| None` |
| 17 | `released_at` | `str \| None` |
| 18 | `inventory_digest` | `str` |
| 19 | `content_digest` | `str` |

### 2.6 WorktreeLifecycleRequest — exactly 14 fields

| # | Field | Type |
|---:|---|---|
| 1 | `schema_version` | `str` |
| 2 | `operation_id` | `str` |
| 3 | `action` | `WorktreeLifecycleAction` |
| 4 | `repository_common_dir` | `str` |
| 5 | `repository_root` | `str` |
| 6 | `worktree_path` | `str` |
| 7 | `branch` | `str` |
| 8 | `base_commit` | `str` |
| 9 | `task_id` | `str` |
| 10 | `revision` | `int` |
| 11 | `attempt` | `int` |
| 12 | `dispatch_id` | `str` |
| 13 | `expected_worktree_id` | `str` |
| 14 | `requested_at` | `str` |

### 2.7 WorktreeLifecycleResult — exactly 9 fields

| # | Field | Type |
|---:|---|---|
| 1 | `schema_version` | `str` |
| 2 | `operation_id` | `str` |
| 3 | `action` | `WorktreeLifecycleAction` |
| 4 | `outcome` | `WorktreeLifecycleOutcome` |
| 5 | `worktree_id` | `str` |
| 6 | `phase` | `WorktreePhase` |
| 7 | `worktree_path` | `str` |
| 8 | `head_commit` | `str \| None` |
| 9 | `decision` | `WorktreeReconciliationAction` |

### 2.8 WorktreeReconciliationDecision — exactly 10 fields

| # | Field | Type |
|---:|---|---|
| 1 | `schema_version` | `str` |
| 2 | `worktree_id` | `str` |
| 3 | `action` | `WorktreeReconciliationAction` |
| 4 | `reason` | `str` |
| 5 | `expected_phase` | `WorktreePhase` |
| 6 | `observed_present` | `bool` |
| 7 | `observed_head` | `str \| None` |
| 8 | `observed_branch` | `str \| None` |
| 9 | `observed_clean` | `bool \| None` |
| 10 | `content_digest` | `str` |

## 3. Schema, identities, and canonical paths

The exact schema is `agentdesk.worktree-lifecycle/v1`.

- `worktree_id` is `WT-` plus a stable lowercase SHA-256 identity derived
  from the canonical common directory, repository root, worktree path,
  branch, base commit, task ID, revision, attempt, and dispatch ID.
- `reservation_id` is `WTR-` plus a stable lowercase SHA-256 identity over
  the same binding.
- `operation_id` is an `OP-` stable caller identity.
- revision is a non-bool positive integer; attempt is a non-bool integer in
  1..3.  Attempt 4 is rejected before reservation or any Git side effect.
- base/head commits are exact 40-character lowercase Git object IDs.
- branch is exactly
  `agentdesk/<task_id>/r<revision>/a<attempt>/<dispatch_id>`.
- worktree path is exactly
  `<repository_root.parent>/<repository_root.name>-agentdesk-worktrees/<worktree_id>`.

Durable paths are:

```text
docs/pm/worktree-lifecycle/reservations/<worktree_id>.yaml
docs/pm/worktree-lifecycle/records/<worktree_id>.yaml
```

Canonical evidence is UTF-8 without BOM, LF-only, one final LF, exact field
order, no duplicate/unknown keys, and SHA-256 over canonical bytes excluding
the value's own `content_digest`.  Writes use a same-directory temporary
regular file, flush/fsync, atomic replace, and parent-directory fsync.

## 4. Repository and path security

Before reservation, creation, reconciliation, or release, independently
verify:

1. absolute canonical repository root and `--git-common-dir` identity;
2. the common directory belongs to the same repository root;
3. base commit exists and is an ancestor of the requested HEAD;
4. exact managed-root containment after Windows case-fold normalization;
5. no relative path, `..`, alternate data stream, device/reserved name, UNC
   path, symlink, junction, mount point, or other reparse-point component;
6. every existing path component has the expected directory/regular-file
   type; and
7. branch, HEAD, task/revision/attempt/dispatch, record, reservation, and
   inventory identities agree byte exactly.

Permission failure, race, unsupported filesystem metadata, or an inability
to prove any identity is `UNKNOWN` and fails closed.  It is never converted
to "not present", "clean", or lock contention.

## 5. Inventory projection

The inventory parser consumes only exact bytes from
`git worktree list --porcelain -z`.  It parses NUL-delimited records and
rejects duplicate fields, unknown mandatory identity states, malformed object
IDs, relative paths, duplicate case-folded paths, and contradictory
bare/detached/branch states.  It does not parse localized or human-readable
Git text and never scans directories to invent an entry.

The inventory digest is SHA-256 over the canonical ordered tuple of verified
`WorktreeInventoryEntry` values.  Ordering is by normalized absolute path,
then branch, then HEAD.

## 6. Reservation, creation, and lock order

The exact create order is:

1. validate the typed request and current repository identity without a
   Store lock;
2. read and parse `git worktree list --porcelain -z`;
3. acquire the independent worktree-lifecycle Store lock;
4. re-read reservation/record evidence and CAS-write `RESERVED` evidence;
5. release the Store lock;
6. run non-interactive `git worktree add --no-checkout -b <branch> <path>
   <base_commit>` using an argv array, explicit cwd, timeout, and no shell;
7. verify inventory, branch, HEAD, common directory, and path;
8. acquire the Store lock, advance `CREATING -> READY`, and release it.

Durable reservation always precedes `git worktree add`.  No Store lock is
held while Git, a Worker, provider, model, API, network, heartbeat, or
WorkflowOrchestrator is called.  The Manager never runs `reset`, `clean`,
`checkout`, recursive deletion, shell-composed commands, or implicit `prune`.

## 7. Release ownership and refusal rules

Only an exact `READY` record created by this Manager may enter `RELEASING`.
Before `git worktree remove`, re-read inventory and verify the exact common
directory, repository root, path, branch, HEAD, task/revision/attempt/dispatch
identity, clean porcelain status, and absence of live process/lease ownership.

Release is refused for dirty or untracked content, detached or divergent
HEAD, wrong branch, missing/divergent reservation or record, locked/prunable
inventory ambiguity, live process/lease evidence, symlink/reparse evidence,
permission failure, or any `UNKNOWN` state.  `--force`, recursive filesystem
deletion, mtime cleanup, path globbing, and removal of unknown worktrees are
forbidden.  A successful release advances `RELEASING -> RELEASED`; immutable
reservation and history evidence are retained.

## 8. Replay, conflict, and concurrency

Identical canonical bytes are byte-exact replay.  The same worktree identity
with different binding bytes is a typed conflict.  Adjacent phase advancement
requires expected-phase CAS and exact old identity; it is not a divergent
overwrite.  Two concurrent creators for one identity have exactly one durable
reservation winner; the other receives byte-exact replay or typed conflict.
Different identities may not claim the same case-folded path or branch.

## 9. Pure reconciliation and crash matrix

Reconciliation consumes only a verified reservation/record tuple and a
verified inventory projection.  It returns a frozen
`WorktreeReconciliationDecision` and performs no Git or filesystem mutation.

| Crash/restart window | Required decision |
|---|---|
| Before durable reservation | `NO_OP` |
| Reservation durable, before `git worktree add` | `RESUME_CREATE` |
| Git entry present, record still `RESERVED/CREATING` | `ADOPT_READY` only after exact identity proof |
| Git add failed and path absent | `RESUME_CREATE` or typed `REJECT` according to frozen evidence |
| READY record but inventory missing/divergent | `FAIL_CLOSED` |
| Before release reservation | `NO_OP` |
| RELEASING record, exact clean entry still present | `RESUME_RELEASE` |
| Git entry absent after RELEASING | `NO_OP` only after exact ownership evidence |
| Dirty worktree, live process/lease, permission failure, or UNKNOWN | `FAIL_CLOSED` |
| Concurrent creator/releaser or divergent generation | byte-exact replay, typed conflict, or `FAIL_CLOSED` |

No decision is based on PID disappearance, lease expiry alone, exception
text, stdout/stderr, exit code, directory absence alone, or mtime.

## 10. Status boundaries

- WorktreeLifecycleManager Contract: **Contract Current — TC-13.25a**.
- Durable Store and pure reconciliation: **Target — TC-13.25b**.
- Create/Release Runtime: **Target — TC-13.25c**.
- Adversarial verification: **Target — TC-13.25d**.
- WorkflowOrchestrator Interface #22 remains unchanged and does not execute
  Git worktree commands.
- PortfolioScheduler Interface #36 remains unchanged and does not claim
  WorktreeLifecycleManager or complete Scheduler runtime Current.
