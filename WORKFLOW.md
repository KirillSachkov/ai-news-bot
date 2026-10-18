# Project workflow

Session mode: main
Classification: deferred

Read [the execution and delivery procedure](docs/agent-workflow.md) before implementing,
resuming, reviewing or delivering assigned work. It defines resource ownership, native workers,
editorial review, required checks and delivery gates.

Classification awaits the project owner's policy choice. Recommend enabling it: the bot consumes
untrusted external feeds and model output, spends paid-model tokens, stores persistent state and
sends Telegram messages under freshness, duplicate and budget controls. Changes can affect
editorial quality, cost or unintended delivery. Reassess when the owner reviews a concrete risk
policy grounded in [product behaviour](README.md) and [the audit](docs/audit-2026-10-07.md),
including verification and participation rules. Until that choice is accepted, existing checks,
review and action permissions continue to apply; the installed classifier grants no new authority.
