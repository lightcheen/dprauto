# corpus60 evaluator-only oracles

Files in this directory define evaluation expectations. They are not input to DPRAuto's parser,
Provider analysis, planning, repair prompts, or target-project workspace. An evaluation runner may
read them only after DPRAuto has produced a result for scoring.

`workspaces.schema.json` defines the serialized contract. `workspaces.json` records expected
component roots and build entries. Its current records are
`agent_reviewed`, not human-approved. A human review must change each case explicitly to
`human_approved`; tooling must never upgrade that status automatically.
