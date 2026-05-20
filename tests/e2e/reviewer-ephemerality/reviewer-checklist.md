# Reviewer Ephemerality Fixture Checklist

This fixture PR is intentionally trivial.

When reviewing a PR for this E2E test:

1. Inspect the diff enough to confirm it only adds or updates `REVIEWER_EPHEMERALITY.md`.
2. If the file exists and mentions reviewer lifecycle testing, call `approve_pr` with a concise approval body.
3. Do not call `request_changes` unless the file is missing or unrelated.

The E2E validator may push an empty commit to the PR branch after the first verdict. Treat that as a fresh review round and approve again if the fixture file still exists.
