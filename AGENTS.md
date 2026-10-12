# Agent instructions

Режим агентов: main

Read [the workflow](docs/agent-workflow.md) before implementing or resuming a task,
or reviewing and delivering changes. It defines checks, resource ownership and delivery scope.
Read [README.md](README.md) for product behaviour. For collection, scoring or delivery changes,
also read [the audit](docs/audit-2026-10-07.md) and the affected tests.

Use the project-local [orchestrate](.agents/skills/orchestrate/SKILL.md) when the owner explicitly
requests execution of a specification or task set. Preparation and status requests do not dispatch work.
