# TL autonomy benchmark report

Run run-791-final at git SHA 5c15d6964a615f7e82913f519d9aa4f020f2b3b4. This is the debug-only mock-watcher run: K=10 independent tickets, seeds [1, 2, 3, 4, 5], effort high, budget cap 10, and zero Forgejo contacts.

The runner isolates orchestration plumbing with deterministic workers and a scripted reviewer. It is evidence about lifecycle and harness behavior, not a live-model quality benchmark.

## Per-cell estimates

|Cell|MIBHI (tickets)|Autonomous span|Stall rate|Recovery initiative|
|---|---:|---:|---:|---:|
|gpt-5-6-luna-codex (gpt-5.6-luna / codex)|10.00 +/- 0.00|100.00 +/- 0.00%|0.0 +/- 0.0%|100.0 +/- 0.0%|
|gpt-5-6-luna-opencode (gpt-5.6-luna / opencode)|10.00 +/- 0.00|100.00 +/- 0.00%|100.0 +/- 0.0%|100.0 +/- 0.0%|
|sonnet-claude-code (sonnet / claude-code)|10.00 +/- 0.00|100.00 +/- 0.00%|100.0 +/- 0.0%|100.0 +/- 0.0%|
|sonnet-opencode (sonnet / opencode)|10.00 +/- 0.00|100.00 +/- 0.00%|100.0 +/- 0.0%|100.0 +/- 0.0%|

Definitions: MIBHI is approved independent tickets per pure-autonomy turn; autonomous span is the approved fraction of K; stall rate counts a non-exited turn-end reason in the pure condition; recovery initiative is recovery dispatches divided by scripted worker failures.

## Turn-end reason histograms

|Cell|Histogram over 10 seed/condition turns|Interpretation|
|---|---|---|
|gpt-5-6-luna-codex|exited 10/10|process-lifecycle signal; inspect the harness wrapper|
|gpt-5-6-luna-opencode|asked_human 10/10|model/role continuation signal in this mock|
|sonnet-claude-code|stopped_backlog_nonempty 10/10|model/role continuation signal in this mock|
|sonnet-opencode|stopped_backlog_nonempty 10/10|model/role continuation signal in this mock|

## 2x2 read

The requested four cells do not contain all four combinations: Sonnet+Codex and Luna+Claude Code are absent. Therefore the full model x harness interaction is not identifiable; the contrasts below use the shared OpenCode column and the available native harness for each model.

|Contrast|Evidence|Read|
|---|---|---|
|Model on OpenCode|Sonnet: stopped_backlog_nonempty 10/10; Luna: asked_human 10/10|Luna changes the non-exit reason from stopped_backlog_nonempty to asked_human; treat as model/role behavior, not a harness verdict.|
|Harness for Luna|Codex: exited 10/10; OpenCode: asked_human 10/10|Luna+Codex is dominated by exited, while Luna+OpenCode is not; this is a harness/process-lifecycle effect in the mock.|
|Interaction|Two counterfactual cells are missing|Do not estimate a full interaction term until Sonnet+Codex and Luna+Claude Code are run.|

## Verdict

**The dominant actionable signal is harness-side for the Luna/Codex cell.** Its turn-end histogram is exited in all 10 observed mock turns, while Luna/OpenCode has no exited turns. The Sonnet cells also avoid exited; their non-exit reasons are continuation-policy signals. This supports a persistent-session wrapper/process-lifecycle fix before attributing Luna/Codex stalls to the model. The report is intentionally qualified because the cells are deterministic mocks, not live model invocations.

## Ranked harness-side levers

1. **Wrap Codex in a persistent/resumable session.** Luna+Codex has 10/10 exited turns versus 0/10 for Luna+OpenCode in this run.
2. **Make the developer-message continuation contract explicit.** The non-exit asked_human/stopped_backlog_nonempty reasons are the discriminator for role-following and prompt continuation.
3. **Run an effort sweep (high vs xhigh) with the same cells.** Keep model, worker harness, K, seed, and budget fixed.
4. **Equalize tool-surface and worker-harness parity.** Compare identical worker success/failure schedules and reviewer latency.
5. **Standardize context compaction and continuation state.** Preserve backlog, inbox, child, and turn-end state across resumptions.

Next step: repeat the factorial with the two missing counterfactual cells and live model calls before making a model-quality claim.
