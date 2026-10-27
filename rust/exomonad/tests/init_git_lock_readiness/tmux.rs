use super::project::assert_success;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::time::{Duration, Instant};

pub struct TmuxSession {
    socket_dir: PathBuf,
    name: String,
}

impl Drop for TmuxSession {
    fn drop(&mut self) {
        let _ = tmux(&self.socket_dir, &["kill-session", "-t", &self.name]);
    }
}

pub async fn create_exited_tl_session(
    socket_dir: &Path,
    session: &str,
    project: &Path,
) -> TmuxSession {
    let config = socket_dir.join("tmux.conf");
    fs::write(&config, "set-window-option -g remain-on-exit on\n").unwrap();
    let output = Command::new("tmux")
        .env("TMUX_TMPDIR", socket_dir)
        .arg("-f")
        .arg(config)
        .args([
            "new-session",
            "-d",
            "-s",
            session,
            "-n",
            "TL",
            "sleep 0.5; exit 0",
        ])
        .output()
        .expect("create isolated existing tmux session");
    assert_success(&output, "create initial exited TL pane");
    let environment = tmux(
        socket_dir,
        &["set-environment", "-g", "EXOMONAD_TL_PREFLIGHT_ACK", "1"],
    );
    assert_success(
        &environment,
        "configure dirty-worktree preflight acknowledgement",
    );
    persist_exact_controller_owner(socket_dir, session, project).await;
    wait_for_exited_tl(socket_dir, session);
    TmuxSession {
        socket_dir: socket_dir.to_path_buf(),
        name: session.to_string(),
    }
}

async fn persist_exact_controller_owner(socket_dir: &Path, session: &str, project: &Path) {
    let pane_output = tmux(
        socket_dir,
        &[
            "list-panes",
            "-t",
            session,
            "-F",
            "#{window_id}\t#{pane_id}\t#{pane_pid}",
        ],
    );
    assert_success(&pane_output, "capture initial controller pane coordinates");
    let pane = String::from_utf8_lossy(&pane_output.stdout)
        .trim()
        .split('\t')
        .map(str::to_owned)
        .collect::<Vec<_>>();
    assert_eq!(pane.len(), 3);
    let server_output = tmux(socket_dir, &["display-message", "-p", "#{pid}"]);
    assert_success(&server_output, "capture initial tmux server identity");
    let server_pid = String::from_utf8_lossy(&server_output.stdout)
        .trim()
        .parse::<u32>()
        .unwrap();
    let pane_pid = pane[2].parse::<u32>().unwrap();
    let ownership = serde_json::json!({
        "schema_version": 1,
        "controller_epoch": "fixture-controller-epoch",
        "run_id": "root",
        "tmux_session": session,
        "window_id": pane[0],
        "pane_id": pane[1],
        "pane_pid": pane_pid,
        "pane_start_ticks": exomonad_core::services::tmux_ipc::process_start_ticks(pane_pid).await.unwrap(),
        "tmux_server_pid": server_pid,
        "tmux_server_start_ticks": exomonad_core::services::tmux_ipc::process_start_ticks(server_pid).await.unwrap(),
    });
    let root = project.join(".exo/tl-loop/root");
    fs::create_dir_all(&root).unwrap();
    fs::write(
        project.join(".exo/tl-loop/root.controller-epoch"),
        "fixture-controller-epoch\n",
    )
    .unwrap();
    fs::write(
        root.join("controller-target.json"),
        serde_json::to_vec_pretty(&ownership).unwrap(),
    )
    .unwrap();
}

pub fn wait_for_exited_tl(socket_dir: &Path, session: &str) {
    let started = Instant::now();
    while !pane_is_dead(socket_dir, session, "TL") {
        assert!(
            started.elapsed() < Duration::from_secs(5),
            "TL pane did not exit"
        );
        std::thread::sleep(Duration::from_millis(25));
    }
}

pub fn assert_exactly_one_live_role_window(socket_dir: &Path, session: &str, role: &str) {
    let windows = tmux(
        socket_dir,
        &[
            "list-windows",
            "-t",
            session,
            "-F",
            "#{window_name} #{pane_dead}",
        ],
    );
    assert_success(&windows, "list init windows");
    let matches = String::from_utf8_lossy(&windows.stdout)
        .lines()
        .filter(|line| line.split_whitespace().next() == Some(role))
        .map(str::to_string)
        .collect::<Vec<_>>();
    assert_eq!(
        matches.len(),
        1,
        "expected one {role} window, got {matches:?}"
    );
    assert!(
        matches[0].ends_with(" 0"),
        "{role} pane is not live: {matches:?}"
    );
}

pub fn window_id(socket_dir: &Path, session: &str, role: &str) -> String {
    let output = tmux(
        socket_dir,
        &[
            "list-windows",
            "-t",
            session,
            "-F",
            "#{window_name} #{window_id}",
        ],
    );
    assert_success(&output, "list tmux window identities");
    String::from_utf8_lossy(&output.stdout)
        .lines()
        .find_map(|line| {
            let mut fields = line.split_whitespace();
            (fields.next() == Some(role)).then(|| fields.next().unwrap().to_string())
        })
        .unwrap_or_else(|| panic!("missing {role} tmux window"))
}

pub fn window_count(socket_dir: &Path, session: &str, role: &str) -> usize {
    let output = tmux(
        socket_dir,
        &["list-windows", "-t", session, "-F", "#{window_name}"],
    );
    assert_success(&output, "count tmux role windows");
    String::from_utf8_lossy(&output.stdout)
        .lines()
        .filter(|line| *line == role)
        .count()
}

fn pane_is_dead(socket_dir: &Path, session: &str, role: &str) -> bool {
    let output = tmux(
        socket_dir,
        &[
            "list-windows",
            "-t",
            session,
            "-F",
            "#{window_name} #{pane_dead}",
        ],
    );
    assert_success(&output, "wait for existing TL pane exit");
    String::from_utf8_lossy(&output.stdout)
        .lines()
        .any(|line| line == format!("{role} 1"))
}

pub fn tmux(socket_dir: &Path, args: &[&str]) -> Output {
    Command::new("tmux")
        .env("TMUX_TMPDIR", socket_dir)
        .args(args)
        .output()
        .expect("run tmux fixture command")
}
