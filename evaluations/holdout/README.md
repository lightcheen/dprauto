# Sealed holdout

This suite measures generalization after development milestones. Its repository identities,
revisions, fetched sources, execution records, and ground truth are evaluator-only and must remain
under `private/` or in an external evaluator store. The whole directory is ignored by Git.

Only `commitment.json`, aggregate ecosystem counts, policy, and validation code are checked in.
This lets a reviewer verify that the private manifest has not changed without exposing cases to
implementation work. Do not run the holdout to guide parser, Provider, renderer, or repair changes.
Use corpus60 for development and the legacy 21-project suite for regression testing.

From the repository root, an evaluator with the private manifest runs:

```bash
python3 evaluations/holdout/validate_holdout.py --require-private
```

The validator checks the manifest commitment, exact case and ecosystem counts, full revisions,
overlap with every tracked development-evaluation JSON, leakage of holdout identities into tracked
files, and imports/references from production code to evaluator-only paths.
