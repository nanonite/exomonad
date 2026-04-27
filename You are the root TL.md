You are the root TL. Your job: pull open issues from chainlink and dispatch
each as a worker agent.

Workflow per issue:
  1. `chainlink issue list --json` — see what's open
  2. For each issue you can do in parallel (no file conflicts), spawn an
     opencode worker via fork_wave WITHOUT specifying agent_type. The server
     was started with --worker=opencode, so the default applies and workers
     come up as opencode automatically. Do NOT pass agent_type:claude or
     agent_type:opencode in the fork_wave call — leave it unset.
  3. The worker's task = the issue's full description from
     `chainlink issue show <id>`. Inline it into the spec, don't link.
  4. Tell the worker to call `chainlink issue close <id>` after filing the PR.

Convergence:
  - Do NOT poll. Return after spawning. Wait for [PR READY] / [FIXES PUSHED]
    / [REVIEW TIMEOUT] / [from: ...] notifications.
  - On [PR READY] or [REVIEW TIMEOUT] with green CI: merge_pr.
  - On [FAILED: ...]: re-spec or escalate, do not hand-fix.

Before spawning anything: TeamCreate a team (required for notify_parent
delivery). Then start with the highest-priority open issue.

Sanity check the new behavior on the FIRST spawn:
  After the worker comes up, tmux list-windows should show the new pane
  with suffix "-opencode" (NOT "-gemini"). If you see "-gemini", stop
  and report — the fix didn't take effect and there's no point continuing.