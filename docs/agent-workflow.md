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

An authorized software/tooling assignment uses [implement](../.agents/skills/implement/SKILL.md).
Before edits, reuse its matching GitHub Issue and branch/PR or create a scoped Issue. Record the
observable result, decisions, scope/exclusions, acceptance criteria, checks and unresolved choices.
A supplied ticket worker keeps its coordinator's assigned scope and target. One coherent result uses
single-task execution; a graph of separately verifiable tasks uses the whole-specification route below.
Preserve approved graphs and ask before changing agreed deliverables or owner decisions.

Read an existing root `GLOSSARY.md`, or relevant entries reached through `GLOSSARY-MAP.md`, and
applicable `docs/adr/` decisions before exploring. Missing domain docs do not block work.
Use defined terms and surface ADR conflicts explicitly. Respect owner-agreed seams and critical
behaviours. Record each seam under test, what it catches and misses; the implementer chooses remaining
seams from acceptance criteria and public interfaces. Ask when intended behaviour or an agreed boundary
is unclear. Interface-design uncertainty uses the available `codebase-design` vocabulary reference;
its absence must be reported rather than assumed available through another checkout.

For explicit whole-specification or task-set execution, use [implement-spec](../.agents/skills/implement-spec/SKILL.md)
and apply the project adaptations below. Natural-language assignments suffice; preparing or discussing
a specification or tasks does not dispatch work.
Restore valid results and actual active worker state on resumption. Keep decisions, dependencies,
resource ownership, checked commits and next actions in the assigned Issue or PR; keep private
worker identifiers in the local handoff.

Workers implement, diagnose, check, fix and clean up their owned resources.
The lead accepts results, resolves dependencies and integrates authorized work.
Each implementation worker follows `implement` single-task execution for its ticket, including
committed-head review, required checks and any enabled classification checkpoints.
Run bounded implementation and review workers as native subagents of the active assignment;
inherit the current agent and model unless the owner selected otherwise. Report unavailable native
delegation and keep independent review pending instead of substituting standalone sessions.
Session mode in `WORKFLOW.md` owns isolation. In main mode, serialize implementation in the primary
checkout on the owned task/integration branch; do not create worker worktrees. In an explicitly
accepted worktree session, each implementation worker uses its own branch and worktree based on the
temporary integration branch; keep project writing tasks sequential. Read-only review can run in parallel. Workers use
[tdd](../.agents/skills/tdd/SKILL.md) for runtime behaviour changes, respecting agreed public test
boundaries. Instructions-only work uses document and integrity checks instead of contrived product tests.
For a software specification, use one temporary integration branch and final PR.
Use separate PRs for unrelated tasks. Commit results before independent per-ticket
[code-review](../.agents/skills/code-review/SKILL.md) and review final integration with the same skill. Pin immutable base/head commits for both review axes;
validate findings, commit fixes and repeat affected review before acceptance. Before reporting done,
each worker includes the current integration tip and rechecks evidence affected by the update.
Apply [the finding lifecycle](agents/review-findings.md): validate findings, retain IDs and dispositions,
review relevant unchanged callers and failure paths, and keep Standards and Spec independent.
When the originating spec is unavailable, retain behavioral Spec review against existing contracts
and report requirement coverage as unavailable; both axis reports remain required.
Suppress only findings whose semantics are proven covered by tooling. Track the complete assigned
base/head across fixes; affected re-review preserves the initial full-diff coverage. Trigger separate
security review when project policy or concrete uncertainty requires it, and simplification only for
evidenced complexity. After three unsuccessful cycles obtain fresh diagnosis; unresolved blockers
continue to block delivery.
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

Portable procedures are whole-folder copies of
[Agent Kit](https://github.com/KirillSachkov/agent-kit) pinned to
`b1608ac14d8f934a8686a0c583f1110466a18b18`: `implement`, `implement-spec`, `tdd`,
`code-review` and `setup-matt-pocock-skills`. The explicit selection is in
[the manifest](../.agents/skills-manifest.json); [the installer lock](../.agents/skills-lock.json)
records provider, revision and complete installed identities, including notices and native metadata.
The installer copies references, licenses and attribution with each folder. Keep these copies unchanged;
select a new full source commit and preview before installing an update with Agent Kit's `tools/skills.py`.
Use its `check` command after installation. Ignored installer backups retain previous folders and locks.
Runtime discovery needs only this repository, without a provider checkout or a Home installation.

The local `classify-change` folder has no counterpart in this Agent Kit version. It remains a
project-owned procedure, byte-for-byte preserved under [its legacy identity record](../.agents/skills/lock.json).
That record now covers only this retained folder; it is not an Agent Kit installer lock.
The old five-folder records were replaced only after exact tracked-folder adoption and canonical
installation. Classification remains deferred under [WORKFLOW.md](../WORKFLOW.md).
Read [classification setup](agents/classification-setup.md) when reassessing that choice;
installation never enables it or grants authority. An enabled project invokes the local classifier
before implementation, after initial implementation and before a protected boundary, using the
trusted target policy rather than candidate rules. Preserve both independent assessment roles.

Project policy lives in this procedure, root `AGENTS.md`, `WORKFLOW.md` and the linked project docs.
Portable skill updates do not alter starts, autonomy, accepted test boundaries, review/commit order,
tracker, session isolation or delivery authority. Installing setup does not invoke it. Keep the existing
root instruction entry and Claude directory alias; verify runtime loading before removing compatibility
entrypoints. Setup changes to language, tracker, domain layout or authority require their existing owner
choice; reuse approved choices. Keep glossary and ADR names until an agreed migration updates consumers.
The setup templates are examples, not new project policy.

Canonical installed skills are regular files under `.agents/skills/`; `.claude/skills` links there for
Claude discovery. Both implementation entrypoints retain model-invocable native metadata. This is
local discovery; cloud tasks still verify their own access. There is no `ask-matt` reference or installed
advisor in this project, so no advisor rename or additional installation is needed.
