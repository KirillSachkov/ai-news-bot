# Project domain documents

Read the target root `AGENTS.md` and keep its existing domain/glossary, context-map
and ADR pointers. `GLOSSARY.md`, `DOMAIN.md` and established context-specific layouts
are valid. A different filename is not evidence that migration is needed.

For a new project with no declared layout, propose one single-context glossary
and an ADR directory. Choose names with the owner under the project's requirements
rules, then record exactly those pointers in its authority file. Multiple contexts
need a map only when the domain actually has distinct vocabularies; do not infer
that from a package count alone.

## Reading rules

Before domain design, exploration, implementation, tests or review, read the glossary
for the relevant context and ADRs in the area. Use canonical terms rather than
synonyms the glossary rejects. Follow the map only into contexts relevant to the task.

## Writing rules

Use `domain-modeling` for agreed vocabulary and ADR changes. Keep definitions free
of code details and implementation plans. A spec contains behavior and acceptance;
ADRs record consequential trade-offs; the glossary defines the language used by both.
Create records when there is a resolved term or decision, rather than empty scaffolds.
Preserve an established layout until the owner explicitly accepts a migration and
its consumer updates.
