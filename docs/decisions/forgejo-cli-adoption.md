# Evaluate Forgejo CLI adoption for one-shot Forgejo operations

**Status:** Decided; do not adopt yet

## Context

ExoMonad owns a custom `ForgejoClient` for Forgejo API operations. The watcher uses several high-frequency polling calls, while `file_pr`, `merge_pr`, and reviewer submission use lower-frequency one-shot calls that could theoretically be delegated to a CLI.

The candidate tools are:

- `forgejo` / `forgejo forgejo-cli`: the server-side Forgejo binary. The current Forgejo CLI reference is primarily administrative and server-local, with commands for web serving, admin user management, runner registration, migrations, diagnostics, and related maintenance. It is not a user-facing pull-request workflow client.
- `tea`: the official Gitea CLI. Gitea describes it as covering issues, pull requests, releases, repositories, and server administration, with examples for `tea login add --url ... --token $TOKEN`, `tea pulls review`, and `tea pulls merge`. Codeberg documents that `tea` can be used with Forgejo instances because Codeberg runs Forgejo.
- `fj`: a Forgejo community CLI. Codeberg documents it as usable with Forgejo and younger than `tea`, with Forgejo-specific scope.
- `gh`: the GitHub CLI. It remains GitHub-specific and is not a fit for Forgejo Gitea-derived API automation.

Local check on this workstation: `gh` is installed, but `tea` and `fj` are not installed. That means this issue cannot honestly claim a working local prototype against the docker-compose Forgejo setup without first adding another tool to the development environment.

## Decision

Do not replace `ForgejoClient` with a CLI subprocess path now. Keep all existing Forgejo API operations in the typed Rust client until a Forgejo/Gitea CLI is installed through the project environment and proven against the local Forgejo fixture.

If this is revisited, `tea` is the first candidate to prototype, not the server-side `forgejo` binary and not `gh`. `tea` has the strongest user-workflow fit today: named logins, token-based auth, pull request review, and pull request merge commands. `fj` should remain a secondary candidate until it demonstrates equivalent command coverage, JSON output, and packaging stability.

## Method Mapping

Keep these in `ForgejoClient` regardless of CLI availability because they are used by polling or dashboards and subprocess overhead would be a regression:

- `list_open_pull_requests`
- `list_pull_request_reviews`
- `list_commit_statuses`
- `commit_status_for_head`
- `actions_status_for_head`
- `latest_actions_status_for_branch`
- `list_workflow_runs_for_branch`
- `list_global_runners`
- `find_open_pull_request`

Potential future CLI prototypes, in priority order:

- `submit_pull_request_review`: likely maps to `tea pulls review <number> --approve` or a reject/request-changes equivalent. This is attractive because it is one-shot and human-workflow-shaped.
- `merge_pull_request`: likely maps to `tea pulls merge <number>`, but merge method, squash behavior, delete-branch behavior, and error parsing must be verified.
- `create_pull_request` and `update_pull_request`: only adopt if `tea` supports stable JSON output for created/updated PRs and can avoid hidden global config state.
- `get_pull_request`: keep in HTTP unless a one-shot caller specifically benefits from CLI behavior; watcher and merge gates need typed fields.

## Auth

Current ExoMonad Forgejo auth uses explicit Forgejo token configuration in server config/environment. A CLI path must not rely on pre-existing user-global login state. The only acceptable shape is deterministic setup from environment, for example:

```bash
tea login add --name exomonad --url "$FORGEJO_URL" --token "$FORGEJO_TOKEN"
tea --login exomonad pulls review 17 --approve --comment "LGTM"
```

Before adoption, verify whether `tea` can avoid writing persistent credentials or can write them to a project-scoped temporary config directory. Global CLI auth state is not acceptable for daemon behavior.

## NixOS And Availability

Neither `tea` nor `fj` is currently available in this workspace shell. Adoption requires adding the chosen CLI to the Nix development environment and CI image first. Without that, replacing Rust API calls would make ExoMonad less portable than the current reqwest client.

## Consequences

- No implementation follow-up is created from this decision because adoption is not currently viable.
- The existing typed `ForgejoClient` remains the source of truth for Forgejo automation.
- Future work should start with an environment/tooling issue: add `tea`, verify commands against the local Forgejo fixture, and record exact JSON/error behavior. Only after that should one-shot methods be migrated individually.

## References

- Forgejo CLI reference: https://forgejo.org/docs/latest/admin/command-line/
- Codeberg CLI client guidance for `tea` and `fj`: https://docs.codeberg.org/git/clone-commit-via-cli/
- Gitea Tea CLI overview and PR examples: https://about.gitea.com/products/tea/
- GitHub CLI manual: https://cli.github.com/manual/
