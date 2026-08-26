//! Fail-open prompt composers for child and PR-resume continuations.

use super::adapters::{ChainlinkAdapter, ChainlinkSession};
use super::{renderer, BriefInputs, ChildSlice, SectionData};
use crate::domain::{AgentName, BirthBranch, PRNumber};
use crate::handlers::agent::resolve_agent_liveness;
use crate::services::agent_control::{AgentControlService, AgentIdentityRecord};
use crate::services::forgejo::ForgejoPullRequestReview;
use crate::services::repo;
use crate::services::{
    HasAgentResolver, HasForgejoClient, HasGitHubClient, HasGitWorktreeService, HasInboxStore,
    HasProjectDir, HasSessionMemory, HasTeamRegistry, MemoryFilter, MemoryKind, MemoryRecordRow,
};

/// Prefix a task without changing the caller's original bytes.
pub(crate) fn prefix_task(prefix: Option<&str>, task: &str) -> String {
    match prefix.filter(|value| !value.is_empty()) {
        Some(prefix) => format!("{prefix}\n\n{task}"),
        None => task.to_string(),
    }
}

type ContinuationResult<T> = Result<T, String>;

/// Compose the bounded parent context used by a GitHub issue child spawn.
pub async fn child_spawn_prefix<C>(
    ctx: &C,
    agent_control: &AgentControlService<C>,
    birth_branch: &BirthBranch,
    agent_name: &AgentName,
    issue_id: i64,
) -> Option<String>
where
    C: HasAgentResolver
        + HasForgejoClient
        + HasGitHubClient
        + HasGitWorktreeService
        + HasInboxStore
        + HasProjectDir
        + HasSessionMemory
        + HasTeamRegistry
        + 'static,
{
    compose_child(ctx, agent_control, birth_branch, agent_name, issue_id, None)
        .await
        .map_or_else(|error| fail_open("child spawn", error), Some)
}

/// Compose the PR-scoped continuation context used by `resume_pr`.
pub async fn resume_pr_prefix<C>(
    ctx: &C,
    agent_control: &AgentControlService<C>,
    birth_branch: &BirthBranch,
    pr_number: u64,
    head_sha: &str,
    owner: &AgentIdentityRecord,
) -> Option<String>
where
    C: HasAgentResolver
        + HasForgejoClient
        + HasGitHubClient
        + HasGitWorktreeService
        + HasInboxStore
        + HasProjectDir
        + HasSessionMemory
        + HasTeamRegistry
        + 'static,
{
    let chainlink = ChainlinkAdapter::new(ctx.project_dir());
    let sessions = chainlink.sessions().await;
    let issue_id = match active_issue_id(&sessions, owner) {
        Some(issue_id) => issue_id,
        None => {
            return fail_open(
                "PR resume",
                "no active Chainlink issue for the resolved PR owner".to_string(),
            )
        }
    };

    let repo_info = match repo::get_repo_info(ctx.project_dir()).await {
        Ok(repo_info) => repo_info,
        Err(error) => return fail_open("PR resume", error.to_string()),
    };
    let forgejo = match ctx.forgejo_client() {
        Some(forgejo) => forgejo,
        None => return fail_open("PR resume", "Forgejo is not configured".to_string()),
    };
    let pr = match forgejo
        .get_pull_request(&repo_info.owner, &repo_info.repo, PRNumber::new(pr_number))
        .await
    {
        Ok(pr) if pr.head_sha.as_deref() == Some(head_sha) => pr,
        Ok(pr) => {
            return fail_open(
                "PR resume",
                format!(
                    "current PR head SHA {:?} does not match requested {head_sha}",
                    pr.head_sha
                ),
            )
        }
        Err(error) => return fail_open("PR resume", error.to_string()),
    };
    let reviews = match forgejo
        .list_pull_request_reviews(&repo_info.owner, &repo_info.repo, PRNumber::new(pr_number))
        .await
    {
        Ok(reviews) => reviews,
        Err(error) => return fail_open("PR resume", error.to_string()),
    };
    let feedback = match current_review_feedback(
        forgejo,
        &repo_info.owner,
        &repo_info.repo,
        PRNumber::new(pr_number),
        head_sha,
        &reviews,
    )
    .await
    {
        Ok(feedback) => feedback,
        Err(error) => return fail_open("PR resume", error),
    };

    let run_id = root_run_id(birth_branch);
    let (inputs, ledger) = match gather_inputs(
        ctx,
        agent_control,
        &run_id,
        owner.agent_name.as_str(),
        owner.agent_type.suffix(),
        issue_id,
    )
    .await
    {
        Ok(value) => value,
        Err(error) => return fail_open("PR resume", error),
    };
    let slice = ChildSlice {
        agent_id: owner.agent_name.to_string(),
        issue_id,
        pr_number: Some(pr_number),
    };
    let child_context = renderer::render_child(&inputs, &ledger, &slice);
    let scoped_records = scoped_records(&ledger, owner.agent_name.as_str(), issue_id);
    let review_state = review_state_for_head(&reviews, head_sha);
    let ci_state = forgejo
        .commit_status_for_head(&repo_info.owner, &repo_info.repo, head_sha)
        .await
        .map(|status| status.as_str().to_string())
        .unwrap_or_else(|_| "unknown".to_string());

    let mut prefix = String::from("## Continuation context for this PR\n");
    prefix.push_str("## Original assignment\n");
    prefix.push_str(&child_context);
    prefix.push_str("\n\n## Prior PR state\n");
    prefix.push_str(&format!(
        "- PR #{pr_number}: {}\n- Head branch: {}\n- Head SHA: {head_sha}\n- Review state: {review_state}\n- CI state: {ci_state}",
        pr.state, pr.head_ref
    ));
    append_records(
        &mut prefix,
        "Prior attempts",
        &scoped_records,
        &[
            MemoryKind::ChildHandoff,
            MemoryKind::ReviewFeedback,
            MemoryKind::CiResult,
            MemoryKind::MergeResult,
        ],
    );
    append_records(
        &mut prefix,
        "Next fix direction",
        &scoped_records,
        &[MemoryKind::FixDirection],
    );
    prefix.push_str("\n\n## Latest review feedback\n");
    if feedback.is_empty() {
        prefix.push_str("_(empty)_");
    } else {
        prefix.push_str(&feedback.join("\n"));
    }
    Some(prefix)
}

async fn compose_child<C>(
    ctx: &C,
    agent_control: &AgentControlService<C>,
    birth_branch: &BirthBranch,
    agent_name: &AgentName,
    issue_id: i64,
    pr_number: Option<u64>,
) -> ContinuationResult<String>
where
    C: HasAgentResolver
        + HasForgejoClient
        + HasGitHubClient
        + HasGitWorktreeService
        + HasInboxStore
        + HasProjectDir
        + HasSessionMemory
        + HasTeamRegistry
        + 'static,
{
    let run_id = root_run_id(birth_branch);
    let (inputs, ledger) = gather_inputs(
        ctx,
        agent_control,
        &run_id,
        agent_name.as_str(),
        "tl",
        issue_id,
    )
    .await?;
    let slice = ChildSlice {
        agent_id: agent_name.to_string(),
        issue_id,
        pr_number,
    };
    Ok(renderer::render_child(&inputs, &ledger, &slice))
}

async fn gather_inputs<C>(
    ctx: &C,
    agent_control: &AgentControlService<C>,
    run_id: &str,
    agent_id: &str,
    role: &str,
    issue_id: i64,
) -> ContinuationResult<(BriefInputs, Vec<MemoryRecordRow>)>
where
    C: HasAgentResolver
        + HasForgejoClient
        + HasGitHubClient
        + HasGitWorktreeService
        + HasInboxStore
        + HasProjectDir
        + HasSessionMemory
        + HasTeamRegistry
        + 'static,
{
    let chainlink = ChainlinkAdapter::new(ctx.project_dir());
    let forgejo =
        super::adapters::ForgejoAdapter::new(ctx.project_dir(), ctx.forgejo_client().cloned());
    let inbox = super::adapters::InboxAdapter::new(ctx.inbox_store().clone());
    let (issues, issue_detail, sessions, open_prs, agents) = tokio::join!(
        chainlink.issues(),
        chainlink.issue_detail(issue_id),
        chainlink.sessions(),
        forgejo.open_prs(),
        list_agent_summaries(ctx, agent_control),
    );
    let agents = match agents {
        Ok(agents) => SectionData::Available(agents),
        Err(reason) => SectionData::Unavailable { reason },
    };
    let unread_summary = inbox.unread_summary();
    let ledger = ctx
        .session_memory()
        .list(MemoryFilter {
            run_id: Some(run_id.to_string()),
            ..Default::default()
        })
        .map_err(|error| format!("failed to load continuation ledger: {error}"))?;
    Ok((
        BriefInputs {
            run_id: run_id.to_string(),
            agent_id: agent_id.to_string(),
            role: role.to_string(),
            issues,
            issue_detail,
            sessions,
            unread_summary,
            agents,
            open_prs,
        },
        ledger,
    ))
}

async fn list_agent_summaries<C>(
    ctx: &C,
    agent_control: &AgentControlService<C>,
) -> ContinuationResult<Vec<super::adapters::AgentSummary>>
where
    C: HasAgentResolver
        + HasForgejoClient
        + HasGitHubClient
        + HasGitWorktreeService
        + HasProjectDir
        + HasTeamRegistry
        + 'static,
{
    let infos = agent_control
        .list_agents()
        .await
        .map_err(|error| error.to_string())?;
    let mut summaries = Vec::with_capacity(infos.len());
    for info in infos {
        let (alive, _) = resolve_agent_liveness(&info).await;
        let identity = ctx.agent_resolver().get(&info.internal_name).await;
        summaries.push(super::adapters::AgentSummary {
            agent_id: info.internal_name.to_string(),
            birth_branch: identity
                .as_ref()
                .map(|record| record.birth_branch.to_string()),
            role: info
                .agent_type
                .map(|agent_type| agent_type.suffix().to_string())
                .unwrap_or_else(|| "unknown".to_string()),
            alive,
        });
    }
    Ok(summaries)
}

fn active_issue_id(
    sessions: &SectionData<Vec<ChainlinkSession>>,
    owner: &AgentIdentityRecord,
) -> Option<i64> {
    let SectionData::Available(sessions) = sessions else {
        return None;
    };
    sessions
        .iter()
        .find(|session| {
            session.agent_id.as_deref() == Some(owner.agent_name.as_str())
                || session.agent_id.as_deref() == Some(owner.birth_branch.as_str())
        })
        .and_then(|session| session.active_issue_id)
}

async fn current_review_feedback(
    forgejo: &crate::services::forgejo::ForgejoClient,
    owner: &crate::domain::GithubOwner,
    repo: &crate::domain::GithubRepo,
    pr_number: PRNumber,
    head_sha: &str,
    reviews: &[ForgejoPullRequestReview],
) -> ContinuationResult<Vec<String>> {
    let mut feedback = Vec::new();
    for review in reviews
        .iter()
        .filter(|review| review.commit_id.as_deref() == Some(head_sha))
    {
        if !review.body.trim().is_empty() {
            feedback.push(format!("- {}", one_line(&review.body)));
        }
        let Some(review_id) = review.id else {
            continue;
        };
        let comments = forgejo
            .list_pull_request_review_comments(owner, repo, pr_number, review_id)
            .await
            .map_err(|error| error.to_string())?;
        feedback.extend(comments.into_iter().map(|comment| {
            let path = comment.path.unwrap_or_else(|| "unknown file".to_string());
            format!("- Review comment on {path}: {}", one_line(&comment.body))
        }));
    }
    Ok(feedback)
}

fn review_state_for_head(reviews: &[ForgejoPullRequestReview], head_sha: &str) -> String {
    let mut current = reviews
        .iter()
        .filter(|review| review.commit_id.as_deref() == Some(head_sha));
    if current
        .clone()
        .any(|review| review.state.eq_ignore_ascii_case("changes_requested"))
    {
        return "changes_requested".to_string();
    }
    if current
        .clone()
        .any(|review| review.state.eq_ignore_ascii_case("approved"))
    {
        return "approved".to_string();
    }
    if current.any(|review| review.state.eq_ignore_ascii_case("comment")) {
        return "commented".to_string();
    }
    "pending_review".to_string()
}

fn scoped_records<'a>(
    ledger: &'a [MemoryRecordRow],
    agent_id: &str,
    issue_id: i64,
) -> Vec<&'a MemoryRecordRow> {
    ledger
        .iter()
        .filter(|record| record.agent_id == agent_id && record.issue_id == Some(issue_id))
        .collect()
}

fn append_records(
    prefix: &mut String,
    heading: &str,
    records: &[&MemoryRecordRow],
    kinds: &[MemoryKind],
) {
    prefix.push_str(&format!("\n\n## {heading}\n"));
    let mut values = records
        .iter()
        .filter(|record| kinds.contains(&record.kind))
        .map(|record| {
            let detail = record
                .detail
                .as_deref()
                .filter(|detail| !detail.trim().is_empty())
                .map(|detail| format!(" — {}", one_line(detail)))
                .unwrap_or_default();
            format!("- {}: {}{}", record.kind, one_line(&record.summary), detail)
        })
        .collect::<Vec<_>>();
    values.sort();
    if values.is_empty() {
        prefix.push_str("_(empty)_");
    } else {
        prefix.push_str(&values.join("\n"));
    }
}

fn one_line(value: &str) -> String {
    value.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn root_run_id(branch: &BirthBranch) -> String {
    let mut root = branch.clone();
    while let Some(parent) = root.parent() {
        root = parent;
    }
    root.to_string()
}

fn fail_open<T>(scope: &str, error: String) -> Option<T> {
    tracing::warn!(scope, error = %error, "Continuation prefix unavailable; proceeding without it");
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::forgejo::ForgejoPullRequestReview;

    #[test]
    fn prefix_none_preserves_task_byte_for_byte() {
        let task = "  keep leading whitespace\nkeep trailing bytes  ";
        assert_eq!(prefix_task(None, task), task);
    }

    #[test]
    fn prefix_adds_one_blank_line_before_original_task() {
        assert_eq!(
            prefix_task(Some("context"), "original task"),
            "context\n\noriginal task"
        );
    }

    #[test]
    fn review_state_uses_only_the_current_head_sha() {
        let reviews = vec![
            ForgejoPullRequestReview {
                id: Some(1),
                state: "changes_requested".to_string(),
                body: "stale".to_string(),
                commit_id: Some("old".to_string()),
                author_login: None,
            },
            ForgejoPullRequestReview {
                id: Some(2),
                state: "approved".to_string(),
                body: "current".to_string(),
                commit_id: Some("head".to_string()),
                author_login: None,
            },
        ];
        assert_eq!(review_state_for_head(&reviews, "head"), "approved");
    }
}
