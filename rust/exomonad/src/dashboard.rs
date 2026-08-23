use crate::config::Config;
use anyhow::{Context, Result};
use crossterm::event::{self, Event, KeyCode, KeyEventKind, KeyModifiers};
use crossterm::execute;
use crossterm::terminal::{
    disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen,
};
use exomonad_core::domain::{BranchName, CIStatus};
use exomonad_core::services::forgejo::{
    ForgejoClient, ForgejoPullRequest, ForgejoPullRequestReview, ForgejoRunner, ForgejoWorkflowRun,
};
use exomonad_core::services::repo::{get_repo_info, RepoInfo};
use ratatui::backend::CrosstermBackend;
use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Cell, Paragraph, Row, Table, Wrap};
use ratatui::{Frame, Terminal};
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};
use std::io::{self, Stdout};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant};

const EVENT_LIMIT: usize = 12;
const PR_LIMIT: usize = 12;
const RUN_LIMIT: usize = 20;

pub async fn run(config: &Config, interval: Duration) -> Result<()> {
    let mut terminal = TerminalGuard::enter()?;
    let client = forgejo_client(config);
    let repo_info = get_repo_info(&config.project_dir).await.ok();
    let mut state = DashboardState::default();
    state
        .refresh(config, client.as_ref(), repo_info.as_ref())
        .await;
    let mut last_refresh = Instant::now();

    loop {
        terminal.draw(|frame| draw(frame, &state, config, repo_info.as_ref()))?;
        if should_quit(Duration::from_millis(200))? {
            break;
        }
        if last_refresh.elapsed() >= interval {
            state
                .refresh(config, client.as_ref(), repo_info.as_ref())
                .await;
            last_refresh = Instant::now();
        }
    }

    Ok(())
}

struct TerminalGuard {
    terminal: Terminal<CrosstermBackend<Stdout>>,
}

impl TerminalGuard {
    fn enter() -> Result<Self> {
        enable_raw_mode().context("failed to enable terminal raw mode")?;
        execute!(io::stdout(), EnterAlternateScreen)?;
        let backend = CrosstermBackend::new(io::stdout());
        Ok(Self {
            terminal: Terminal::new(backend)?,
        })
    }

    fn draw(&mut self, f: impl FnOnce(&mut Frame)) -> Result<()> {
        self.terminal.draw(f)?;
        Ok(())
    }
}

impl Drop for TerminalGuard {
    fn drop(&mut self) {
        let _ = disable_raw_mode();
        let _ = execute!(self.terminal.backend_mut(), LeaveAlternateScreen);
        let _ = self.terminal.show_cursor();
    }
}

#[derive(Default)]
struct DashboardState {
    agents: Vec<AgentRow>,
    ordered: Vec<OrderedStageRow>,
    ci_runs: Vec<CiRunRow>,
    runners: Vec<RunnerRow>,
    prs: Vec<PrRow>,
    events: Vec<EventRow>,
    error: Option<String>,
    updated_at: String,
}

impl DashboardState {
    async fn refresh(
        &mut self,
        config: &Config,
        client: Option<&Arc<ForgejoClient>>,
        repo_info: Option<&RepoInfo>,
    ) {
        self.updated_at = chrono::Local::now().format("%H:%M:%S").to_string();
        let controller_failure = controller_exit_reason(&config.project_dir);
        self.agents = scan_agents(&config.project_dir, controller_failure.is_some());
        self.ordered = read_ordered_stages(&config.project_dir);
        self.events = read_events(&config.project_dir, EVENT_LIMIT);
        self.error = controller_failure.map(|reason| format!("TL controller failed: {reason}"));

        if let (Some(client), Some(repo_info)) = (client, repo_info) {
            self.refresh_forgejo(client, repo_info).await;
        } else {
            self.ci_runs.clear();
            self.runners.clear();
            self.prs.clear();
        }
    }

    async fn refresh_forgejo(&mut self, client: &ForgejoClient, repo_info: &RepoInfo) {
        let prs = match client
            .list_open_pull_requests(&repo_info.owner, &repo_info.repo)
            .await
        {
            Ok(prs) => prs,
            Err(error) => {
                if self.error.is_none() {
                    self.error = Some(error.to_string());
                }
                Vec::new()
            }
        };
        self.ci_runs = collect_ci_runs(client, repo_info, &prs).await;
        self.prs = collect_pr_rows(client, repo_info, prs).await;
        self.runners = collect_runner_rows(client).await;
    }
}

#[derive(Clone, Default)]
struct AgentRow {
    name: String,
    role: String,
    branch: String,
    state: String,
}

#[derive(Clone, Default)]
struct OrderedStageRow {
    order: String,
    sub_tl: String,
    lifecycle: String,
    head: String,
    base: String,
    ci: String,
}

#[derive(Clone, Default)]
struct CiRunRow {
    pr: String,
    branch: String,
    status: String,
    name: String,
    started_at: String,
}

#[derive(Clone, Default)]
struct RunnerRow {
    name: String,
    status: String,
    heartbeat: String,
}

#[derive(Clone, Default)]
struct PrRow {
    agent: String,
    title: String,
    review: String,
    ci_gate: String,
}

#[derive(Clone, Default)]
struct EventRow {
    time: String,
    agent: String,
    summary: String,
}

fn forgejo_client(config: &Config) -> Option<Arc<ForgejoClient>> {
    match (
        config.forgejo_url.as_deref(),
        config.forgejo_token.as_deref(),
    ) {
        (Some(url), Some(token)) => ForgejoClient::new(url, token).ok(),
        (None, None) if ForgejoClient::fj_binary_in_path() => {
            Some(ForgejoClient::new_fj(config.project_dir.clone()))
        }
        _ => None,
    }
}

fn should_quit(timeout: Duration) -> Result<bool> {
    if !event::poll(timeout)? {
        return Ok(false);
    }
    let Event::Key(key) = event::read()? else {
        return Ok(false);
    };
    if key.kind != KeyEventKind::Press {
        return Ok(false);
    }
    Ok(matches!(key.code, KeyCode::Char('q') | KeyCode::Esc)
        || (key.code == KeyCode::Char('c') && key.modifiers.contains(KeyModifiers::CONTROL)))
}

fn draw(frame: &mut Frame, state: &DashboardState, config: &Config, repo_info: Option<&RepoInfo>) {
    let outer = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(3),
            Constraint::Min(0),
            Constraint::Length(7),
        ])
        .split(frame.area());
    draw_header(frame, outer[0], state, config, repo_info);

    let middle = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([Constraint::Percentage(44), Constraint::Percentage(56)])
        .split(outer[1]);
    let left = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Percentage(45),
            Constraint::Percentage(35),
            Constraint::Percentage(20),
        ])
        .split(middle[0]);
    let right = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Percentage(45), Constraint::Percentage(55)])
        .split(middle[1]);

    draw_agents(frame, left[0], &state.agents);
    draw_ordered(frame, left[1], &state.ordered);
    draw_runners(frame, left[2], &state.runners);
    draw_ci(frame, right[0], &state.ci_runs);
    draw_prs(frame, right[1], &state.prs);
    draw_events(frame, outer[2], state);
}

fn draw_header(
    frame: &mut Frame,
    area: Rect,
    state: &DashboardState,
    config: &Config,
    repo: Option<&RepoInfo>,
) {
    let repo_label = repo
        .map(|info| format!("{}/{}", info.owner.as_str(), info.repo.as_str()))
        .unwrap_or_else(|| "repo unknown".to_string());
    let forgejo = config
        .forgejo_url
        .as_deref()
        .unwrap_or("forgejo unconfigured");
    let status = state.error.as_deref().unwrap_or("ok");
    let title = Span::styled(
        "ExoMonad Watch",
        Style::default()
            .fg(Color::Cyan)
            .add_modifier(Modifier::BOLD),
    );
    let line = Line::from(vec![
        title,
        Span::raw(format!(
            "  {repo_label}  {forgejo}  refreshed {}  {status}",
            state.updated_at
        )),
    ]);
    frame.render_widget(
        Paragraph::new(line).block(Block::default().borders(Borders::ALL)),
        area,
    );
}

fn draw_agents(frame: &mut Frame, area: Rect, agents: &[AgentRow]) {
    let rows = agents.iter().map(|agent| {
        Row::new([
            Cell::from(agent.name.clone()),
            Cell::from(agent.role.clone()),
            Cell::from(agent.branch.clone()),
            Cell::from(agent.state.clone()).style(state_style(&agent.state)),
        ])
    });
    let table = Table::new(
        rows,
        [
            Constraint::Percentage(28),
            Constraint::Percentage(18),
            Constraint::Percentage(36),
            Constraint::Percentage(18),
        ],
    )
    .header(header(["agent", "role", "branch", "state"]))
    .block(Block::default().title("Agent Tree").borders(Borders::ALL));
    frame.render_widget(table, area);
}

fn draw_ordered(frame: &mut Frame, area: Rect, stages: &[OrderedStageRow]) {
    let rows = stages.iter().map(|stage| {
        Row::new([
            Cell::from(stage.order.clone()),
            Cell::from(stage.sub_tl.clone()),
            Cell::from(stage.lifecycle.clone()).style(state_style(&stage.lifecycle)),
            Cell::from(stage.head.clone()),
            Cell::from(stage.base.clone()),
            Cell::from(stage.ci.clone()).style(state_style(&stage.ci)),
        ])
    });
    let table = Table::new(
        rows,
        [
            Constraint::Percentage(8),
            Constraint::Percentage(22),
            Constraint::Percentage(22),
            Constraint::Percentage(18),
            Constraint::Percentage(18),
            Constraint::Percentage(12),
        ],
    )
    .header(header(["#", "sub-TL", "lifecycle", "head", "base", "CI"]))
    .block(
        Block::default()
            .title("Ordered Sub-TLs")
            .borders(Borders::ALL),
    );
    frame.render_widget(table, area);
}

fn draw_ci(frame: &mut Frame, area: Rect, runs: &[CiRunRow]) {
    let rows = runs.iter().map(|run| {
        Row::new([
            Cell::from(run.pr.clone()),
            Cell::from(run.branch.clone()),
            Cell::from(run.status.clone()).style(state_style(&run.status)),
            Cell::from(run.name.clone()),
            Cell::from(run.started_at.clone()),
        ])
    });
    let table = Table::new(
        rows,
        [
            Constraint::Percentage(10),
            Constraint::Percentage(28),
            Constraint::Percentage(14),
            Constraint::Percentage(34),
            Constraint::Percentage(14),
        ],
    )
    .header(header(["pr", "branch", "status", "run", "started"]))
    .block(Block::default().title("CI Status").borders(Borders::ALL));
    frame.render_widget(table, area);
}

fn draw_runners(frame: &mut Frame, area: Rect, runners: &[RunnerRow]) {
    let rows = runners.iter().map(|runner| {
        Row::new([
            Cell::from(runner.name.clone()),
            Cell::from(runner.status.clone()).style(state_style(&runner.status)),
            Cell::from(runner.heartbeat.clone()),
        ])
    });
    let table = Table::new(
        rows,
        [
            Constraint::Percentage(42),
            Constraint::Percentage(24),
            Constraint::Percentage(34),
        ],
    )
    .header(header(["runner", "status", "heartbeat"]))
    .block(
        Block::default()
            .title("Forgejo Runners")
            .borders(Borders::ALL),
    );
    frame.render_widget(table, area);
}

fn draw_prs(frame: &mut Frame, area: Rect, prs: &[PrRow]) {
    let rows = prs.iter().map(|pr| {
        Row::new([
            Cell::from(pr.agent.clone()),
            Cell::from(pr.title.clone()),
            Cell::from(pr.review.clone()).style(state_style(&pr.review)),
            Cell::from(pr.ci_gate.clone()).style(state_style(&pr.ci_gate)),
        ])
    });
    let table = Table::new(
        rows,
        [
            Constraint::Percentage(22),
            Constraint::Percentage(42),
            Constraint::Percentage(20),
            Constraint::Percentage(16),
        ],
    )
    .header(header(["agent", "title", "review", "ci"]))
    .block(Block::default().title("PR Summary").borders(Borders::ALL));
    frame.render_widget(table, area);
}

fn draw_events(frame: &mut Frame, area: Rect, state: &DashboardState) {
    let lines = state.events.iter().map(|event| {
        Line::from(vec![
            Span::styled(
                format!("{:<8}", event.time),
                Style::default().fg(Color::DarkGray),
            ),
            Span::styled(
                format!("{:<12}", event.agent),
                Style::default().fg(Color::Blue),
            ),
            Span::raw(event.summary.clone()),
        ])
    });
    let widget = Paragraph::new(lines.collect::<Vec<_>>())
        .wrap(Wrap { trim: true })
        .block(Block::default().title("Event Log").borders(Borders::ALL));
    frame.render_widget(widget, area);
}

fn header<const N: usize>(cells: [&'static str; N]) -> Row<'static> {
    Row::new(cells.map(Cell::from)).style(Style::default().fg(Color::Yellow))
}

fn state_style(value: &str) -> Style {
    match value.to_lowercase().as_str() {
        "success" | "approved" | "active" | "idle" | "online" => Style::default().fg(Color::Green),
        "failure" | "changes_requested" | "stuck" | "offline" => Style::default().fg(Color::Red),
        "running" | "pending" | "queued" | "busy" => Style::default().fg(Color::Yellow),
        _ => Style::default().fg(Color::Gray),
    }
}

async fn collect_pr_rows(
    client: &ForgejoClient,
    repo_info: &RepoInfo,
    prs: Vec<ForgejoPullRequest>,
) -> Vec<PrRow> {
    let mut rows = Vec::new();
    for pr in prs.into_iter().take(PR_LIMIT) {
        let reviews = client
            .list_pull_request_reviews(&repo_info.owner, &repo_info.repo, pr.number)
            .await
            .unwrap_or_default();
        let ci_gate = pr_ci_status(client, repo_info, &pr).await;
        rows.push(PrRow {
            agent: pr.head_ref.as_str().to_string(),
            title: pr.title,
            review: review_state(&reviews),
            ci_gate,
        });
    }
    rows
}

async fn collect_ci_runs(
    client: &ForgejoClient,
    repo_info: &RepoInfo,
    prs: &[ForgejoPullRequest],
) -> Vec<CiRunRow> {
    let pr_numbers = pr_numbers_by_branch(prs);
    let mut branches = pr_numbers.keys().cloned().collect::<BTreeSet<_>>();
    if branches.is_empty() {
        branches.insert(current_branch(repo_info.repo.as_str()));
    }
    let mut rows = Vec::new();
    for branch in branches {
        let Ok(branch_name) = BranchName::try_from_str(&branch) else {
            continue;
        };
        let runs = client
            .list_workflow_runs_for_branch(&repo_info.owner, &repo_info.repo, &branch_name, 4)
            .await
            .unwrap_or_default();
        rows.extend(
            runs.into_iter()
                .map(|run| ci_run_row(&branch, pr_numbers.get(&branch), run)),
        );
    }
    rows.truncate(RUN_LIMIT);
    rows
}

fn pr_numbers_by_branch(prs: &[ForgejoPullRequest]) -> BTreeMap<String, String> {
    prs.iter()
        .map(|pr| (pr.head_ref.as_str().to_string(), pr.number.to_string()))
        .collect()
}

async fn collect_runner_rows(client: &ForgejoClient) -> Vec<RunnerRow> {
    client
        .list_global_runners()
        .await
        .unwrap_or_default()
        .into_iter()
        .map(runner_row)
        .collect()
}

async fn pr_ci_status(
    client: &ForgejoClient,
    repo_info: &RepoInfo,
    pr: &ForgejoPullRequest,
) -> String {
    let Some(head_sha) = pr.head_sha.as_deref() else {
        return "unknown".to_string();
    };
    client
        .actions_status_for_head(&repo_info.owner, &repo_info.repo, &pr.head_ref, head_sha)
        .await
        .unwrap_or(CIStatus::Unknown)
        .as_str()
        .to_string()
}

fn review_state(reviews: &[ForgejoPullRequestReview]) -> String {
    reviews
        .iter()
        .rev()
        .find_map(|review| {
            let state = review.state.trim().to_lowercase();
            (!state.is_empty()).then_some(state)
        })
        .unwrap_or_else(|| "pending".to_string())
}

fn ci_run_row(branch: &str, pr: Option<&String>, run: ForgejoWorkflowRun) -> CiRunRow {
    let status = run.conclusion.unwrap_or(run.status);
    let name = if run.display_title.is_empty() {
        run.name
    } else {
        run.display_title
    };
    CiRunRow {
        pr: pr.map(String::as_str).unwrap_or("-").to_string(),
        branch: branch.to_string(),
        status,
        name,
        started_at: time_label(
            run.created_at
                .as_deref()
                .or(run.updated_at.as_deref())
                .unwrap_or("-"),
        ),
    }
}

fn runner_row(runner: ForgejoRunner) -> RunnerRow {
    let status = if runner.disabled {
        "offline"
    } else if runner.busy {
        "busy"
    } else if runner.status.is_empty() {
        "idle"
    } else {
        runner.status.as_str()
    };
    RunnerRow {
        name: runner.name,
        status: status.to_string(),
        heartbeat: runner.last_online.unwrap_or_else(|| "-".to_string()),
    }
}

fn scan_agents(project_dir: &Path, controller_failed: bool) -> Vec<AgentRow> {
    let mut agents = BTreeMap::new();
    scan_agent_dir(
        &project_dir.join(".exo/agents"),
        &mut agents,
        controller_failed,
    );
    scan_worktree_dir(&project_dir.join(".exo/worktrees"), &mut agents);
    agents.into_values().collect()
}

fn scan_agent_dir(path: &Path, agents: &mut BTreeMap<String, AgentRow>, controller_failed: bool) {
    let Ok(entries) = std::fs::read_dir(path) else {
        return;
    };
    for entry in entries.flatten().filter(|entry| entry.path().is_dir()) {
        let name = entry.file_name().to_string_lossy().to_string();
        let row = agent_row_from_dir(&name, &entry.path(), controller_failed);
        agents.insert(name, row);
    }
}

fn scan_worktree_dir(path: &Path, agents: &mut BTreeMap<String, AgentRow>) {
    let Ok(entries) = std::fs::read_dir(path) else {
        return;
    };
    for entry in entries.flatten().filter(|entry| entry.path().is_dir()) {
        let name = entry.file_name().to_string_lossy().to_string();
        agents.entry(name.clone()).or_insert_with(|| AgentRow {
            name,
            role: "worktree".to_string(),
            branch: git_branch(&entry.path()).unwrap_or_default(),
            state: "idle".to_string(),
        });
    }
}

fn agent_row_from_dir(name: &str, dir: &Path, controller_failed: bool) -> AgentRow {
    let branch = read_identity_branch(dir).or_else(|| read_trimmed(dir.join(".birth_branch")));
    let role = infer_role(name, dir, branch.as_deref().unwrap_or_default());
    AgentRow {
        name: name.to_string(),
        role,
        branch: branch.unwrap_or_default(),
        state: if name == "root" && controller_failed {
            "failure".to_string()
        } else {
            agent_state(dir)
        },
    }
}

fn read_ordered_stages(project_dir: &Path) -> Vec<OrderedStageRow> {
    let Some(document) = read_json(project_dir.join(".exo/tl-loop/root/run.json")) else {
        return Vec::new();
    };
    let Some(stages) = document.get("ordered_stages").and_then(Value::as_array) else {
        return Vec::new();
    };
    let integration = document.get("integration");
    let candidates = integration.and_then(|value| value.get("candidates"));
    let sub_tl_states = integration.and_then(|value| value.get("sub_tl_states"));
    let mut rows = Vec::new();
    for stage in stages {
        let order = stage
            .get("order")
            .and_then(Value::as_u64)
            .map(|value| value.to_string())
            .unwrap_or_else(|| "?".to_string());
        let Some(sub_tls) = stage.get("sub_tls").and_then(Value::as_array) else {
            continue;
        };
        for sub_tl in sub_tls.iter().filter_map(Value::as_str) {
            let candidate = candidates.and_then(|value| value.get(sub_tl));
            let lifecycle = candidate
                .and_then(|value| value.get("lifecycle"))
                .or_else(|| sub_tl_states.and_then(|value| value.get(sub_tl)))
                .or_else(|| integration.and_then(|value| value.get("lifecycle")));
            rows.push(OrderedStageRow {
                order: order.clone(),
                sub_tl: sub_tl.to_string(),
                lifecycle: value_text(lifecycle),
                head: candidate_or_integration_text(
                    candidate,
                    integration,
                    &["head_sha", "aggregate_head_sha"],
                ),
                base: candidate_or_integration_text(
                    candidate,
                    integration,
                    &["validated_base_sha"],
                ),
                ci: candidate_or_integration_text(candidate, integration, &["ci_status"]),
            });
        }
    }
    rows
}

fn candidate_or_integration_text(
    candidate: Option<&Value>,
    integration: Option<&Value>,
    keys: &[&str],
) -> String {
    keys.iter()
        .find_map(|key| {
            candidate
                .and_then(|value| value.get(*key))
                .or_else(|| integration.and_then(|value| value.get(*key)))
                .and_then(Value::as_str)
                .filter(|value| !value.is_empty())
        })
        .unwrap_or("-")
        .to_string()
}

fn value_text(value: Option<&Value>) -> String {
    value
        .and_then(Value::as_str)
        .filter(|text| !text.is_empty())
        .unwrap_or("-")
        .to_string()
}

fn agent_state(dir: &Path) -> String {
    let marker = read_trimmed(dir.join("state")).unwrap_or_default();
    if marker.to_lowercase().contains("stuck") {
        return "stuck".to_string();
    }
    if routing_target_exists(dir) {
        "active".to_string()
    } else {
        "idle".to_string()
    }
}

fn infer_role(name: &str, dir: &Path, branch: &str) -> String {
    let routing = read_json(dir.join("routing.json"));
    if name.starts_with("review-pr-") || branch.starts_with("review-pr-") {
        "reviewer".to_string()
    } else if routing.and_then(|v| v.get("pane_id").cloned()).is_some() {
        "worker".to_string()
    } else if name == "root" {
        "root".to_string()
    } else if name.contains("-tl-")
        || branch
            .rsplit('.')
            .next()
            .is_some_and(|leaf| leaf.contains("-tl-"))
    {
        "tl".to_string()
    } else {
        "dev".to_string()
    }
}

fn routing_target_exists(dir: &Path) -> bool {
    let Some(routing) = read_json(dir.join("routing.json")) else {
        return false;
    };
    routing
        .get("window_id")
        .or_else(|| routing.get("pane_id"))
        .and_then(Value::as_str)
        .is_some_and(tmux_target_exists)
}

fn tmux_target_exists(target: &str) -> bool {
    let session = std::env::var("EXOMONAD_TMUX_SESSION").unwrap_or_default();
    if session.is_empty() {
        return false;
    }
    std::process::Command::new("tmux")
        .args([
            "display-message",
            "-p",
            "-t",
            &format!("{session}:{target}"),
            "#{pane_id}",
        ])
        .output()
        .is_ok_and(|output| output.status.success())
}

fn read_identity_branch(dir: &Path) -> Option<String> {
    let json = read_json(dir.join("identity.json"))?;
    json.get("birth_branch")
        .or_else(|| json.get("branch"))
        .and_then(Value::as_str)
        .map(ToString::to_string)
}

fn controller_exit_reason(project_dir: &Path) -> Option<String> {
    let value = read_json(project_dir.join(".exo/tl-loop/root/controller-exit.json"))?;
    value
        .get("reason")
        .and_then(Value::as_str)
        .filter(|reason| !reason.is_empty())
        .map(ToString::to_string)
}

fn read_events(project_dir: &Path, limit: usize) -> Vec<EventRow> {
    let mut rows = Vec::new();
    let logs_dir = project_dir.join(".exo/logs");
    let Ok(entries) = std::fs::read_dir(logs_dir) else {
        return rows;
    };
    for path in entries
        .flatten()
        .map(|entry| entry.path())
        .filter(|path| is_jsonl(path))
    {
        rows.extend(read_event_file(&path));
    }
    rows.sort_by(|a, b| a.time.cmp(&b.time));
    rows.into_iter().rev().take(limit).collect()
}

fn read_event_file(path: &Path) -> Vec<EventRow> {
    let Ok(content) = std::fs::read_to_string(path) else {
        return Vec::new();
    };
    content
        .lines()
        .rev()
        .take(EVENT_LIMIT)
        .filter_map(parse_event_line)
        .collect()
}

fn parse_event_line(line: &str) -> Option<EventRow> {
    let value: Value = serde_json::from_str(line).ok()?;
    let time = value.get("ts").and_then(Value::as_str).unwrap_or("-");
    let agent = value.get("agent_id").and_then(Value::as_str).unwrap_or("-");
    let kind = value.get("type").and_then(Value::as_str).unwrap_or("event");
    Some(EventRow {
        time: time_label(time),
        agent: agent.to_string(),
        summary: event_summary(kind, value.get("data")),
    })
}

fn event_summary(kind: &str, data: Option<&Value>) -> String {
    let branch = data
        .and_then(|value| value.get("branch").or_else(|| value.get("head")))
        .and_then(Value::as_str)
        .unwrap_or("");
    if branch.is_empty() {
        kind.to_string()
    } else {
        format!("{kind} {branch}")
    }
}

fn git_branch(path: &Path) -> Option<String> {
    let output = std::process::Command::new("git")
        .arg("-C")
        .arg(path)
        .args(["branch", "--show-current"])
        .output()
        .ok()?;
    output
        .status
        .success()
        .then(|| String::from_utf8_lossy(&output.stdout).trim().to_string())
}

fn current_branch(fallback: &str) -> String {
    std::process::Command::new("git")
        .args(["branch", "--show-current"])
        .output()
        .ok()
        .filter(|output| output.status.success())
        .map(|output| String::from_utf8_lossy(&output.stdout).trim().to_string())
        .filter(|branch| !branch.is_empty())
        .unwrap_or_else(|| fallback.to_string())
}

fn read_json(path: PathBuf) -> Option<Value> {
    serde_json::from_str(&std::fs::read_to_string(path).ok()?).ok()
}

fn read_trimmed(path: PathBuf) -> Option<String> {
    std::fs::read_to_string(path)
        .ok()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
}

fn is_jsonl(path: &Path) -> bool {
    path.extension().is_some_and(|ext| ext == "jsonl")
}

fn time_label(value: &str) -> String {
    value.get(11..19).unwrap_or(value).to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn branch(value: &str) -> BranchName {
        BranchName::try_from_str(value).expect("literal branch is valid")
    }

    fn pull_request(number: u64, head_ref: &str) -> ForgejoPullRequest {
        ForgejoPullRequest {
            number: exomonad_core::domain::PRNumber::new(number),
            url: String::new(),
            title: String::new(),
            body: String::new(),
            head_ref: branch(head_ref),
            base_ref: branch("main"),
            state: "open".to_string(),
            merged: false,
            head_sha: None,
            base_sha: None,
        }
    }

    fn workflow_run() -> ForgejoWorkflowRun {
        ForgejoWorkflowRun {
            name: "test".to_string(),
            display_title: String::new(),
            head_branch: None,
            head_sha: None,
            status: "success".to_string(),
            conclusion: None,
            created_at: Some("2026-08-03T12:34:56Z".to_string()),
            updated_at: None,
        }
    }

    #[test]
    fn ci_run_pr_map_uses_pr_head_branch_and_number() {
        let prs = vec![pull_request(608, "feature/dashboard")];
        let map = pr_numbers_by_branch(&prs);

        assert_eq!(map.get("feature/dashboard"), Some(&"608".to_string()));
    }

    #[test]
    fn ci_run_row_uses_dash_when_branch_has_no_open_pr() {
        let row = ci_run_row("feature/no-pr", None, workflow_run());

        assert_eq!(row.pr, "-");
        assert_eq!(row.started_at, "12:34:56");
    }

    #[test]
    fn scan_agents_marks_root_failed_when_controller_exit_is_recorded() {
        let project = tempfile::tempdir().unwrap();
        let root = project.path().join(".exo/agents/root");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::create_dir_all(project.path().join(".exo/tl-loop/root")).unwrap();
        std::fs::write(
            project
                .path()
                .join(".exo/tl-loop/root/controller-exit.json"),
            r#"{"reason":"controller exited during startup"}"#,
        )
        .unwrap();

        let agents = scan_agents(
            project.path(),
            controller_exit_reason(project.path()).is_some(),
        );
        let root = agents
            .iter()
            .find(|agent| agent.name == "root")
            .expect("root agent should be visible");
        assert_eq!(root.state, "failure");
        assert_eq!(
            controller_exit_reason(project.path()).as_deref(),
            Some("controller exited during startup")
        );
    }

    #[test]
    fn ordered_stage_rows_use_candidate_evidence() {
        let project = tempfile::tempdir().unwrap();
        let run_dir = project.path().join(".exo/tl-loop/root");
        std::fs::create_dir_all(&run_dir).unwrap();
        std::fs::write(
            run_dir.join("run.json"),
            r#"{
                "ordered_stages":[{"order":1,"sub_tls":["alpha","beta"]}],
                "integration":{
                    "lifecycle":"RUNNING",
                    "sub_tl_states":{"alpha":"RUNNING","beta":"RUNNING"},
                    "candidates":{
                        "alpha":{"lifecycle":"MERGED","head_sha":"alpha-head","validated_base_sha":"base-a","ci_status":"success"},
                        "beta":{"lifecycle":"INTEGRATION_CONFLICT","head_sha":"beta-head","validated_base_sha":"base-b","ci_status":"failure"}
                    }
                }
            }"#,
        )
        .unwrap();

        let rows = read_ordered_stages(project.path());

        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0].sub_tl, "alpha");
        assert_eq!(rows[0].lifecycle, "MERGED");
        assert_eq!(rows[0].head, "alpha-head");
        assert_eq!(rows[0].base, "base-a");
        assert_eq!(rows[0].ci, "success");
        assert_eq!(rows[1].sub_tl, "beta");
        assert_eq!(rows[1].lifecycle, "INTEGRATION_CONFLICT");
        assert_eq!(rows[1].head, "beta-head");
        assert_eq!(rows[1].base, "base-b");
        assert_eq!(rows[1].ci, "failure");
    }
}
