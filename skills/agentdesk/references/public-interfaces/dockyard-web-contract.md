# Dockyard Web Product Contract

Status: **Contract Current — TC-13.29b**
Visual shell direction: **Approved — Waypoint Light, 2026-08-09**
Loopback Web shell runtime: **Current — TC-13.29l.6**
Deterministic local requirement-to-plan composition: **Current — TC-13.29l.7**
Retry owner composition: **Current — TC-13.29l.13b**
Retry closed-loop E2E: **Verified — TC-13.29l.13c**
Returned-delivery redispatch: **Runtime + review handoff Current — TC-13.29l.14c / TC-13.29l.14d**
Owner-loss recovery and reconnect UX: **Current — TC-13.29l.15a / TC-13.29l.15b**
Adversarial verification: **Verified — TC-13.29m / extended matrix TC-13.29m.1b**
PM difficulty/Agent route core: **Current — local unified integration**
Adaptive model-based PM decomposition: **Target**

## 1. Product promise

Dockyard presents the existing AgentDesk closed loop as one Chinese visual
workflow:

```text
需求输入
→ 唯一 PM 拆解任务并生成计划草案
→ 用户编辑和批准
→ Scheduler 选择和派发
→ Worker 在独立 worktree 执行
→ Git 证据预检
→ MAD 审议
→ 用户验收或返修
→ integration
→ 全部任务完成
```

Dockyard does not create a second PM, bypass task cards, or replace canonical
state with browser state.

## 2. Information architecture

The Dockyard business route registry remains exact and ordered:

1. 工作台
2. 需求与方案
3. 任务
4. 运行
5. 审议与验收
6. Worker 与模型
7. 项目
8. 系统诊断
9. 设置

Desktop presents these routes in a top navigation bar. A history drawer exposes
recently viewed Dockyard routes without pretending to be chat or project
history; it may add safe projected objects only when an existing typed API owns
that evidence. Closing the drawer, refreshing the browser, or losing
browser-local history must not change canonical task, run, approval, or project
state.

The approved reference influences composition, scale, translucent surfaces,
video treatment, and motion. It does not replace the `Dockyard` brand or add the
reference product's names, navigation, account controls, credits, attachments,
voice input, prompt library, or other capabilities.

## 3. Visual system

- product name: `Dockyard`.
- Chinese UI only in the first runtime.
- light, high-contrast operational surfaces over a full-viewport video hero.
- neutral white/near-white surfaces, near-black text, and a Dockyard-owned
  forest-green accent palette.
- top navigation, central workspace, and a dismissible history drawer.
- dense operational presentation without hiding confirmation or error states.
- decorative motion must never imply progress not backed by runtime evidence.
- no dynamic task relationship graph in the first runtime.

Color tokens must distinguish neutral, selected, success, warning, failure,
blocked, unknown, and focus states without borrowing reference-product tokens.

The workbench video is decorative, muted, looping, `playsInline`, and excluded
from the accessibility tree. Its approved source is
`https://d8j0ntlcm91z4.cloudfront.net/user_38xzZboKViGWJOttwIXH07lWA1P/hf_20260329_050842_be71947f-f16e-4a14-810c-06e83d23ddb5.mp4`
or a locally packaged byte-identical copy. It is sized to 115% width and height,
horizontally centered, top-anchored, and rendered with an `object-top` focal
point. Failure to load or autoplay falls back to an intentional static
background and never blocks navigation or operational controls. A remote media
request must not contain project, device, task, session, or user data.

Video opacity is controlled in JavaScript, not by CSS transitions. Each fade
cancels any active animation frame and resumes from the current opacity. Load and
loop start fade in over 250 ms. Once 0.55 seconds remain, a `fadingOutRef` guard
allows one 250 ms fade-out. `ended` sets opacity to zero, waits 100 ms, seeks to
zero, resumes playback, and fades in. The video remains purely atmospheric; its
playback state is never presented as Runner, task, or integration state.

## 4. Workbench

The first viewport prioritizes `运行状态总览` inside the video hero. It exposes
honest Runner/SSE/snapshot health and active-run evidence before secondary
content, while preserving the requirement input as an existing Dockyard action.
It contains all five primary summaries without placing every feature on one
screen:

1. active Worker/Runner/provider health and current run phases;
2. current tasks by lifecycle state;
3. pending approvals and user decisions;
4. requirement input and current plan state;
5. recent delivery, MAD verdict, and integration result.

Each summary links to its dedicated page. The workbench is a projection, not a
state owner. Hero controls reuse only current Dockyard routes and frozen Control
API commands; visual similarity to the reference is not authority to add a fake
or disconnected command.

## 5. Requirement and plan experience

The default selection is `Standard + balanced`.

Execution mode choices are `Lite`, `Standard`, and `Automated`. Deliberation
depth choices are `fast`, `balanced`, and `deep`. Policy may raise a minimum
mode/tier/depth but never lower the user's or task card's requirement.
`Automated` must be disabled or marked not ready until its complete runtime is
verified; a label cannot grant a capability.

Before approval PM automatically splits explicit multi-step requirements,
checks a read-only Git inventory against the current snapshot, assesses
difficulty, and chooses an eligible configured Agent. The default
editor keeps route, execution, budget, and retry controls under an explicit
advanced-route toggle; the user may enable it for special per-task overrides.
No capable Agent is shown as a typed not-ready result rather than silently
downgrading. Editing an approved plan creates a new revision. Approval shows
the plan ID, revision, digest, and task count.

## 6. Task and run experience

Task views expose safe fields for state, revision, attempt, role, selected
provider/model, dependencies, delivery/integration status, commits, reports,
approval state, and typed errors. Run views expose safe phases for scheduling,
handoff, ACK, heartbeat, delivery, finalization, retry, and recovery.

The UI must not show or fetch raw prompt text, API keys, environment values,
full stdout/stderr, provider response bytes, runtime route/thread/session/host
IDs, absolute worktree paths, or unredacted process evidence.

Target capability is visibly labeled `未就绪`. A Target provider or command
has no enabled dispatch button. `UNKNOWN` is not displayed as healthy.

## 7. Commands and confirmations

The UI submits only the frozen Control API commands. It never edits local files
or invokes Git/shell/provider commands.

Plan approval, active manual retry, model/strategy escalation, dispatch
termination, and project removal require a second confirmation. Stale CAS or a
changed digest closes the dialog, refreshes evidence, and requires a new user
confirmation. Optimistic UI never declares canonical success before a command
receipt is received.

Task cancellation is a canonical `task.cancel` soft delete for exact
`draft`, `ready`, or `blocked` evidence only. It is desktop-only, requires the
typed phrase `删除 <task>`, preserves Git and historical evidence, and refreshes
the task/run projection after the command receipt. A task with active dispatch
evidence must use the exact-generation termination workflow first. Delivery
acceptance and return keep their existing confirmation and mobile policies.

Project removal says `不会删除 Git 仓库` and cannot offer a repository-delete
checkbox. Automatic recovery is displayed but does not create an extra user
confirmation beyond its existing policy.

## 8. Provider experience

Claude, Codex, and Reasonix are represented independently. The UI may edit safe
binding fields but never receives credentials. API keys remain exclusively in
the local Runner/provider user directory.

Provider state is `READY`, `DEGRADED`, `NOT_READY`, or `UNKNOWN`. Reasonix or
Codex remains `未就绪` until its exact decoder, permission, binding, and runtime
evidence gates are complete.

## 9. Responsive boundary

Desktop supports every approved operation. Mobile supports state viewing, plan
approval, manual retry, and dispatch termination. Complex task dependency
editing and advanced model configuration are desktop-primary and may render as
read-only on mobile.

The mobile layout is purpose-built; it is not a scaled desktop canvas. It must
support current mobile Safari for the four permitted operations and current
stable Chrome/Edge for the full desktop experience.

Mobile does not expose task deletion, project removal, returned-delivery
redispatch, or complex plan/dependency editing. The top navigation collapses to
a compact control and the history drawer becomes a bounded overlay without
relaxing these command restrictions.

## 10. Empty, loading, reconnect, and failure states

Every page defines Chinese empty, loading, stale, reconnecting, offline,
not-ready, unknown, forbidden, conflict, and failed states. SSE disconnect does
not erase the last validated snapshot. Reconnection must identify that data may
be stale until a new snapshot commit is received.

Errors display only typed safe messages and a correlation digest. UI code must
not parse exception text, stdout/stderr, provider names, or exit codes to infer
recovery, retryability, liveness, or success.

## 11. Explicit exclusions

The first runtime excludes dynamic task graphs, multi-user accounts,
organizations, RBAC, public registration or login, billing or credits, email,
Telegram, enterprise messaging, browser notifications, attachments, voice
capture, a prompt library, arbitrary plugins, arbitrary terminal access,
repository deletion, cloud relay, and deployment. The approved light visual
direction does not change these capability exclusions.

## 12. Status boundary

- Dockyard Web product contract: **Contract Current — TC-13.29b**.
- Web foundation/design system: **Current — TC-13.29h foundation; Waypoint
  Light visual revision approved 2026-08-09**.
- Read-only operational pages: **Current — TC-13.29i**.
- Command/mobile workflows: **Core Current — TC-13.29k**.
- Loopback Web shell runtime: **Current — TC-13.29l.6**.
- Deterministic local requirement-to-plan composition: **Current — TC-13.29l.7**.
- Adaptive model-based PM decomposition: **Target**.
- Active terminate capability: **Verified — TC-13.29l.12e**; only exact live
  ownership evidence can create the destructive action.
- Dockyard user retry command contract: **Contract Current — TC-13.29l.13a**;
  runtime/owner composition is **Current — TC-13.29l.13b**; closed-loop E2E is
  **Verified — TC-13.29l.13c** and adversarial verification is
  **Verified — TC-13.29m**.
- Returned-delivery redispatch UI/runtime is **Current — TC-13.29l.14c** and
  the completed attempt returns to the review owner through
  **TC-13.29l.14d**.
- Owner-loss recovery and honest recovery/reconnect presentation are
  **Current — TC-13.29l.15a / TC-13.29l.15b**. The page displays the validated
  ALIVE/UNKNOWN/DEAD state and retains the last verified snapshot during SSE
  reconnect; it never invents retry or terminate authority from status text.
- Local core closed-loop E2E: **Verified — TC-13.29l.10**.
- Supported Dockyard local closed loop: **E2E Verified — TC-13.29n**.
- Core and extended adversarial boundaries: **Verified — TC-13.29m / Extended
  TC-13.29m.1b**. Pairing, repository replacement, SSE cursor divergence,
  two-browser races, hostile output, secret isolation, and attempt four are
  included.
- Optional future matrices remain **Partial/Target**: adaptive model-based PM,
  Automated execution, and a versioned task-spec revision editor.
- GitHub publication preparation: **Target — TC-13.29o**.

The existing Interface #24 HTML Dashboard remains read-only and separate.

## 13. Returned-delivery redispatch UX

TC-13.29l.14b freezes a desktop-first `开始返修` action for an exact returned
delivery. It reuses the existing `task.retry` endpoint and is rendered only
from a server projection whose retry cause is `delivery_returned`. The dialog
shows task/revision, prior and next attempt, provider/model, and the frozen MAD
failure source. It states that the task specification is unchanged and that
the Worker will receive the original card plus durable MAD feedback.

The action is not created from a bare `ready` task, free-form error text, or a
client inference. Stale/divergent evidence closes the dialog and requires a new
projection. Attempt four is never rendered. This returned-delivery action is
desktop-only in the first runtime; mobile remains limited to the already
verified approval, failure retry, and active termination controls.

Returned-delivery redispatch UI is **Contract Current — TC-13.29l.14b**,
**Runtime Current — TC-13.29l.14c**, and **review handoff Current —
TC-13.29l.14d**. The unchanged-specification return/redispatch experience is
closed; a separate versioned task-spec revision editor remains **Target**.
