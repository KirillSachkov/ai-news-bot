# Agent workflow

## Authority and scope

This is a Python standard-library project. These instructions cover task coordination and
repository delivery, not permission to operate the bot.
[README](../README.md) describes the product; [the audit](audit-2026-10-07.md) records earlier checks
and operational limitations. Historical release instructions are evidence, not standing authorization.

Use GitHub Issues in this repository for durable task state. Read the whole assigned Issue and comments.
Keep private coordination, local paths, session data and credentials outside public tracked files,
Issues and PRs. A cross-project assignment can retain private provenance in its local handoff.

Before writing, inspect git status, branches, worktrees, open PRs and live project workers.
Preserve foreign changes and active workers. Read [the root workflow](../WORKFLOW.md) for session
configuration. Serialize project writing tasks: one writer at a time, with one owner per task.
Native subagents may review read-only.
Use the current runtime and inherit its agent/model unless the owner selected another.
Cloud tasks must verify their own required access before dependent work.

## Session handoff and coordination

A request to launch work in another session assigns execution to that destination only.
Deliver the assignment, verify receipt and report the session location; that completes launch,
including startup recovery. The resulting owner/project session is independent. Ongoing prompts,
answering its questions, result acceptance and closure require an owner coordination assignment.
Record the assigned tasks/sessions, lead and responsibilities in the task record and private handoff.
Monitor-only scope permits observation and reporting; direction and acceptance require their own scope.
Repository membership and session creation confer no management authority. Preserve foreign sessions,
results and resources; cleanup covers owned resources only after evidence and live-worker checks.

## Editorial and engineering routes

Ordinary writing, editing, research and material review use editorial judgement against
[the editorial profile](../editorial.md) and author acceptance, with existing publication gates.
The engineering workflow below applies to software/tooling changes or an explicitly assigned
implementation workflow. Discussing or selecting a content plan does not assign its production.

## Execution and review

For explicit whole-specification or task-set execution, use [implement-spec](../.agents/skills/implement-spec/SKILL.md)
and apply the project adaptations below. Natural-language assignments suffice; preparing or discussing
a specification or tasks does not dispatch work.
Restore valid results and actual active worker state on resumption. Keep decisions, dependencies,
resource ownership, checked commits and next actions in the assigned Issue or PR; keep private
worker identifiers in the local handoff.

Workers implement, diagnose, check, fix and clean up their owned resources.
The lead accepts results, resolves dependencies and integrates authorized work.
Run bounded implementation and review workers as native subagents of the active assignment;
inherit the current agent and model unless the owner selected otherwise. Report unavailable native
delegation and keep independent review pending instead of substituting standalone sessions.
Each implementation worker uses its own branch and worktree based on the temporary integration
branch; keep project writing tasks sequential. Read-only review can run in parallel. Workers use
[tdd](../.agents/skills/tdd/SKILL.md) for runtime behaviour changes, respecting agreed public test
boundaries. Instructions-only work uses document and integrity checks instead of contrived product tests.
For a software specification, use one temporary integration branch and final PR.
Use separate PRs for unrelated tasks. Commit results before independent per-ticket
[code-review](../.agents/skills/code-review/SKILL.md) and review final integration with the same skill. Pin immutable base/head commits for both review axes;
validate findings, commit fixes and repeat affected review before acceptance. Before reporting done,
each worker includes the current integration tip and rechecks evidence affected by the update.
For editorial changes, check meaning and consistency with the existing editorial profile.
Ask about business decisions and explicit gates; continue independent work while waiting.

## Verification

For software/tooling changes, including harness instructions, run from the repository root:

```bash
python3 -m unittest discover -s tests
git diff --check
```

Review semantics and relative links for documentation changes. Check portable skill discovery
from a fresh project context. Reuse evidence only while the checked inputs remain unchanged.
Repeat affected checks after fixes or changed inputs. Record commands, actual results, the checked
commit and independent review findings. Local tests are not CI; no CI workflow is currently installed.

For instructions-only changes, use offline unit tests and document review. Bot commands may contact
sources, call paid models, send Telegram messages or change state. Running the product, changing
config/editorial/state/credentials, operating services and deployment require scope authorization.
A repository merge alone does not authorize these actions.

## Delivery and reporting

Deliver software/tooling changes through a scoped branch and PR after required checks and independent review.
Complete commit, push, merge and task resolution only within the owner's authorized scope.
Respect any task-specific gate. Verify the merged tree and preserve evidence before cleaning up
owned branches or workers; confirm no live worker still needs them. Deployment is a separate stage
unless explicitly assigned.

After each verified stage, report the outcome, an accessible inspection link, checks and review,
the required owner decision or none, and remaining work. Distinguish a prepared diff, local checks,
CI, merge and deployment. A worker completion message or a closed Issue alone does not prove acceptance.

## Skill ownership

Canonical installed skills are regular project-owned files under `.agents/skills/`; `.claude/skills`
links to that directory for Claude discovery. Root `AGENTS.md` is the owner-maintained instruction entry.
The selected set is `implement`, `implement-spec`, `tdd`, `code-review`, `classify-change` and
`setup-matt-pocock-skills`, including their references and native metadata. Preserve approved invocation
metadata in `SKILL.md` and `agents/openai.yaml`. Installation does not invoke interactive setup.

Maintain all installed file hashes in [the skill lock](../.agents/skills/lock.json).
Keep private upstream provenance in the local handoff. Record local adaptations distinctly;
do not label modified bytes as an unchanged source copy. Project-specific rules belong in this
procedure and [WORKFLOW.md](../WORKFLOW.md), outside synchronized skills. Classification routing
uses the declaration in that root workflow; installing shared mechanics does not approve a policy.
This installation provides local discovery; cloud execution readiness still requires verification
in the cloud runtime.

The installed `implement-spec` delegates per-ticket execution to its required `implement` dependency.
Its support skills (`tdd`, `code-review`, and `classify-change`) are installed with it. Both implementation
entrypoints retain the approved model-invocable metadata; this dependency update preserves the project
routes and delivery gates above. All six directories are unchanged copies of the assigned approved source.
