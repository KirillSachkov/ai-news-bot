# Agent workflow

## Authority and scope

This is a Python standard-library project. The repository previously had no agent workflow.
These instructions cover task coordination and repository delivery, not permission to operate the bot.
[README](../README.md) describes the product; [the audit](audit-2026-10-07.md) records earlier checks
and operational limitations. Historical release instructions are evidence, not standing authorization.

Use GitHub Issues in this repository for durable task state. Read the whole assigned Issue and comments.
Keep private coordination, local paths, session data and credentials outside public tracked files,
Issues and PRs. A cross-project assignment can retain private provenance in its local handoff.

Before writing, inspect git status, branches, worktrees, open PRs and live project workers.
Preserve foreign changes and active workers. The default checkout mode is main: serialize writes
in this checkout with one owner per writing task. Native subagents may review read-only.
Use the current runtime and inherit its agent/model unless the owner selected another.
Cloud tasks must verify their own required access before dependent work.

## Execution and review

The owner's explicit execution request starts [orchestrate](../.agents/skills/orchestrate/SKILL.md).
Preparing a specification or tasks does not dispatch work. Restore valid results and active workers
on resumption. Keep decisions, dependencies, resource ownership, checked commits and next actions
in the assigned Issue or PR; keep private worker identifiers in the local handoff.

Workers implement, diagnose, check, fix and clean up their owned resources.
The lead accepts results, resolves dependencies and integrates authorized work.
Delegate independent ready work when the runtime permits it; serialize main-checkout writes.
For a software specification, use one temporary integration branch and final PR.
Use separate PRs for unrelated tasks. Review each task independently and review final integration.
For editorial changes, check meaning and consistency with the existing editorial profile.
Ask about business decisions and explicit gates; continue independent work while waiting.

## Verification

Run from the repository root:

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

Deliver through a scoped branch and PR after required checks and independent review.
Complete commit, push, merge and task resolution only within the owner's authorized scope.
Respect any task-specific gate. Verify the merged tree and preserve evidence before cleaning up
owned branches or workers. Deployment is a separate stage unless explicitly assigned.

After each verified stage, report the outcome, an accessible inspection link, checks and review,
the required owner decision or none, and remaining work. Distinguish a prepared diff, local checks,
CI, merge and deployment. A worker completion message or a closed Issue alone does not prove acceptance.

## Skill ownership

The canonical portable entry is `.agents/skills/orchestrate/SKILL.md`, a regular project-owned file.
It has no executable helper or required companion skill. Optional skills named in its text apply
only when installed. No implement-spec, tdd or ask-matt copy was installed at adoption.
Maintain the installed checksum in [the skill lock](../.agents/skills/lock.json).
Keep private upstream provenance in the adoption handoff. Record local adaptations distinctly;
do not label modified bytes as an unchanged source copy. This installation provides local discovery;
cloud execution readiness still requires verification in the cloud runtime.
