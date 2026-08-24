# Frozen evaluation baselines

`python-21-20260818.json` freezes the 21-project run used as the pre-change
baseline for transactional repair candidates and mutable verification plans.
It records the source revision, local source directory, evaluation limits,
observed validation command, and observed outcome for each case.

The `observed_test_command` values describe what DPRAuto actually selected in
the source run. They are not an assertion that the commands are correct. The
evaluation harness now binds cached records to an evaluation identity covering
the case, source tree, policy, model pool, and DPRAuto implementation. A
reviewed command manifest or implementation change therefore invalidates old
records instead of silently reusing or replacing their meaning.

Some upstream datasets contain abbreviated revisions. Those values are kept
verbatim rather than being presented as full commit identifiers. New
evaluation identities also include the copied source-tree SHA-256, so cache
validity does not rely on an abbreviated revision alone.

Expected pre-change metrics are:

- 21 projects
- 12 standard builds succeeded
- 2 final environments succeeded
- 16 projects entered Agent repair
- 0 Agent repairs succeeded
- 17 Installability checks passed
- 2 Testability checks passed
- 9 Runnability checks passed
- 4 repairs ended as regressions
