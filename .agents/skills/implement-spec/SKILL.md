---
name: implement-spec
description: "Execute an assigned specification and its dependency graph under the target project's start, delegation and delivery rules. Use when the owner assigns a whole spec and the project's required start is satisfied."
---

Read root `AGENTS.md` and its requirements, worktree, testing, human-participation,
tracker and delivery pointers. Satisfy its start rule before writing. Confirm the
approved spec, tickets, criteria, blockers and human checkpoints. An authorized
spec run covers its agreed child tasks, not new requirements.

The goal is the assigned spec delivered in the project's selected mode: ticket
PRs or an approved integration branch. Record an immutable review base against
the intended target. This procedure does not choose another acceptance mode.

Tickets form a **task graph** with blocking relationships and a ready **frontier**.
Start each native subagent with a fresh context. Communicate through short context
pointers to the spec, ticket, research and commits, rather than copying their bodies.
Run independent workers in the background where the client and project permit it.
A native subagent is not an independently assigned application session.

## Steps

1. Read the spec and ticket graph. Resolve dependency and acceptance conditions.
2. If exploration is needed, a fresh exploration subagent can write scoped notes
   outside the repo, accessible to later workers. Read-only exploration does not
   start another implementation beyond the approved graph.
3. Prepare the owned execution context under project policy. In integration mode,
   prepare its branch/worktree and draft at the required point. In ticket-PR mode,
   each ticket follows its own delivery and dependency-merge gates. A main-checkout
   project keeps its prescribed context and serialized writers.
4. Use a fresh implementer for each ready ticket when native delegation is available.
   Use separate owned worktrees/branches when permitted; serialize writers where the
   project requires one main checkout. Each worker:
   - verifies ownership and required immutable base; reports mismatch without reset;
   - reads the project contract and task, uses `tdd` where required, and observes
     accepted testing seams and human checkpoints;
   - returns committed changes, checked revisions, evidence and blockers. Integration
     workers update from the integration tip only by the project's permitted procedure.
5. In integration mode, integrate child work with a fresh merger under project rules.
   In ticket-PR mode, deliver each ticket and wait for its required acceptance/merge
   before unblocking dependents. A worker's done status is not acceptance.
6. Recompute the frontier from proven dependency outcomes and start newly ready work.
   Keep independent authorized work moving while owner decisions are pending.
7. At required ticket/integration checkpoints, pass checks and follow the project's
   commit/review order. Supply immutable base/head, or an owned pre-commit tree
   snapshot when required, and requirements to `code-review`. Fix findings in an owned
   context, rerun affected checks, freeze the corrected revision and re-review with earlier reports. Preserve
   initial full-diff coverage. Invoke a named skill through the client's mechanism or
   read its `SKILL.md` and relevant references; a literal Skill tool is not required.
8. Follow root delivery pointers for the verified result, reusing existing drafts.
9. Clean only owned, integrated child resources with no active writer or unsaved work.
   Follow project rules for retained acceptance trees and unintegrated results.
