//! Liveness probes must distinguish a live tmux target from a stale one.
//!
//! These tests drive a real tmux server. Each test creates its own uniquely
//! named session and kills it on the way out, so they are safe to run inside an
//! existing tmux session. When the `tmux` binary is unavailable the test is
//! skipped rather than failed.

use exomonad_core::domain::RoutingInfo;
use exomonad_core::services::tmux_ipc::{routing_target_alive, PaneId, TmuxIpc, WindowId};
use std::process::Command;

/// A tmux session created for one test, torn down on drop.
struct TestSession {
    name: String,
}

impl TestSession {
    fn create(name: &str) -> Self {
        let status = Command::new("tmux")
            .args([
                "new-session",
                "-d",
                "-s",
                name,
                "-n",
                "probe",
                "sh",
                "-c",
                "sleep 300",
            ])
            .status()
            .expect("tmux new-session");
        assert!(status.success(), "failed to create tmux session {name}");
        Self {
            name: name.to_string(),
        }
    }

    /// First window id (`@N`) of this session.
    fn window_id(&self) -> WindowId {
        let id = self.query("#{window_id}");
        WindowId::parse(&id).expect("tmux reported a valid window id")
    }

    /// First pane id (`%N`) of this session.
    fn pane_id(&self) -> PaneId {
        let id = self.query("#{pane_id}");
        PaneId::parse(&id).expect("tmux reported a valid pane id")
    }

    fn query(&self, format: &str) -> String {
        let output = Command::new("tmux")
            .args(["list-panes", "-t", &self.name, "-F", format])
            .output()
            .expect("tmux list-panes");
        assert!(output.status.success(), "tmux list-panes failed");
        String::from_utf8_lossy(&output.stdout)
            .lines()
            .next()
            .expect("session has at least one pane")
            .trim()
            .to_string()
    }

    fn ipc(&self) -> TmuxIpc {
        TmuxIpc::new(&self.name)
    }
}

impl Drop for TestSession {
    fn drop(&mut self) {
        let _ = Command::new("tmux")
            .args(["kill-session", "-t", &self.name])
            .status();
    }
}

fn tmux_available() -> bool {
    Command::new("tmux")
        .arg("-V")
        .output()
        .map(|out| out.status.success())
        .unwrap_or(false)
}

#[tokio::test]
async fn window_exists_reports_true_for_a_live_window() {
    if !tmux_available() {
        return;
    }
    let session = TestSession::create("exo-liveness-live-window");
    let window = session.window_id();

    assert!(session
        .ipc()
        .window_exists(&window)
        .await
        .expect("probe succeeds"));
}

#[tokio::test]
async fn window_exists_reports_false_for_an_id_that_was_never_created() {
    if !tmux_available() {
        return;
    }
    let session = TestSession::create("exo-liveness-absent-window");
    let absent = WindowId::parse("@99999").expect("syntactically valid window id");

    assert!(
        !session
            .ipc()
            .window_exists(&absent)
            .await
            .expect("probe succeeds"),
        "a window id that exists nowhere on the server must not read as alive"
    );
}

#[tokio::test]
async fn window_exists_reports_false_after_the_window_is_killed() {
    if !tmux_available() {
        return;
    }
    let session = TestSession::create("exo-liveness-killed-window");
    let extra = Command::new("tmux")
        .args([
            "new-window",
            "-d",
            "-t",
            &session.name,
            "-P",
            "-F",
            "#{window_id}",
            "sh",
            "-c",
            "sleep 300",
        ])
        .output()
        .expect("tmux new-window");
    assert!(extra.status.success(), "tmux new-window failed");
    let extra_id = WindowId::parse(String::from_utf8_lossy(&extra.stdout).trim())
        .expect("tmux reported a valid window id");

    assert!(session
        .ipc()
        .window_exists(&extra_id)
        .await
        .expect("probe succeeds"));

    let status = Command::new("tmux")
        .args(["kill-window", "-t", extra_id.as_str()])
        .status()
        .expect("tmux kill-window");
    assert!(status.success(), "tmux kill-window failed");

    assert!(
        !session
            .ipc()
            .window_exists(&extra_id)
            .await
            .expect("probe succeeds"),
        "a killed window must not read as alive"
    );
}

#[tokio::test]
async fn window_exists_is_scoped_to_the_probing_session() {
    if !tmux_available() {
        return;
    }
    let own = TestSession::create("exo-liveness-scope-own");
    let other = TestSession::create("exo-liveness-scope-other");
    let foreign_window = other.window_id();

    assert!(
        !own.ipc()
            .window_exists(&foreign_window)
            .await
            .expect("probe succeeds"),
        "a window owned by a different session must not read as alive"
    );
}

#[tokio::test]
async fn pane_exists_reports_false_for_an_id_that_was_never_created() {
    if !tmux_available() {
        return;
    }
    let session = TestSession::create("exo-liveness-absent-pane");
    let live = session.pane_id();
    let absent = PaneId::parse("%99999").expect("syntactically valid pane id");

    assert!(session
        .ipc()
        .pane_exists(&live)
        .await
        .expect("probe succeeds"));
    assert!(
        !session
            .ipc()
            .pane_exists(&absent)
            .await
            .expect("probe succeeds"),
        "a pane id that exists nowhere on the server must not read as alive"
    );
}

#[tokio::test]
async fn routing_target_alive_reports_false_for_stale_routing() {
    if !tmux_available() {
        return;
    }
    let session = TestSession::create("exo-liveness-stale-routing");
    let stale = RoutingInfo::window(WindowId::parse("@99999").expect("valid window id"));

    assert!(
        !routing_target_alive(&stale, &session.ipc())
            .await
            .expect("probe succeeds"),
        "stale routing metadata must not keep an exited owner marked live"
    );
}

#[tokio::test]
async fn routing_target_alive_reports_true_for_current_routing() {
    if !tmux_available() {
        return;
    }
    let session = TestSession::create("exo-liveness-current-routing");
    let routing = RoutingInfo::window(session.window_id());

    assert!(routing_target_alive(&routing, &session.ipc())
        .await
        .expect("probe succeeds"));
}
