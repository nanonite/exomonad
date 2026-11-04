use super::{
    assert_tool_error, build_production_agent_handler, build_production_agent_runtime,
    build_wasm_runtime_with_agent_handler, call_tool, mount_open_pr, run_git, AgentHandler,
};
use async_trait::async_trait;
use exomonad_core::services::agent_control::AgentControlService;
use exomonad_core::services::event_log::EventLog;
use exomonad_core::services::forgejo::ForgejoClient;
use exomonad_core::services::immutable_ledger::LedgerWriter;
use exomonad_core::{EffectContext, EffectError, EffectHandler, EffectResult};
use exomonad_proto::effects::agent as agent_proto;
use prost::Message;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};
use std::process::Command;
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

const DIRTY_WORKTREE_CODE: &str = "agent.spawn_precondition.dirty_worktree";

struct OwnerFixture {
    name: String,
    branch: String,
    slice_id: String,
    worktree_relative: PathBuf,
    invocation_path: PathBuf,
    invocation_bytes: Vec<u8>,
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[serial_test::serial]
async fn compiled_resume_blocked_leaf_preserves_typed_dirty_refusal_without_intent() {
    let temp = tempfile::tempdir().expect("temporary blocked-resume fixture");
    let project = temp.path().join("checkout");
    let remote = temp.path().join("fixture.git");
    let owner_name = "blocked-owner-codex";
    let owner_branch = format!("main.{owner_name}");
    let worktree = initialize_resume_repository(&project, &remote, owner_name, &owner_branch);
    let issue_id = create_open_chainlink_issue(&project);
    let owner = seed_dormant_owner(
        &project,
        owner_name,
        &owner_branch,
        &worktree,
        "slice-blocked-resume",
        Some(issue_id),
    );
    append_parked_event(&project, &owner.slice_id);
    std::fs::write(
        project.join("SOURCE-DIRTY.txt"),
        "uncommitted source edit\n",
    )
    .expect("dirty source worktree for preflight");
    let fingerprint = worktree_fingerprint(&project.join(&owner.worktree_relative));
    let forgejo = MockServer::start().await;
    let client = ForgejoClient::new(&forgejo.uri(), "test-only-token")
        .expect("construct isolated Forgejo client");
    let runtime = build_production_agent_runtime(&project, client).await;

    let output = call_tool(
        &runtime,
        "tl",
        "resume_blocked_leaf",
        json!({
            "chainlink_issue_id": issue_id,
            "expected_invocation_id": "parked-invocation-1",
            "expected_branch": owner.branch,
            "expected_worktree_fingerprint": fingerprint,
            "task": "Continue the existing parked assignment",
            "human_approved": true
        }),
    )
    .await;

    assert_typed_dirty_refusal(&output, "resume_blocked_leaf");
    let events = read_project_events(&project);
    let failed = only_spawn_failed(&events);
    assert_eq!(failed["data"]["child_agent"], owner.name);
    assert!(failed["data"].get("intent_id").is_none());
    assert_eq!(failed["data"]["code"], DIRTY_WORKTREE_CODE);
    assert_eq!(
        std::fs::read(&owner.invocation_path).expect("read parked invocation after refusal"),
        owner.invocation_bytes,
        "preflight refusal must not create or replace the parked invocation"
    );
    assert_eq!(agent_directory_names(&project), [owner.name.as_str()]);
    assert_no_spawn_or_resume_event(&events);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[serial_test::serial]
async fn compiled_blocked_resume_uses_the_controller_selected_worker_route() {
    use std::os::unix::fs::PermissionsExt;

    let temp = tempfile::tempdir().expect("temporary blocked-resume fixture");
    let project = temp.path().join("checkout");
    let remote = temp.path().join("fixture.git");
    let owner_name = "issue-814-opencode";
    let owner_branch = format!("main.{owner_name}");
    let worktree = initialize_resume_repository(&project, &remote, owner_name, &owner_branch);
    let issue_id = create_open_chainlink_issue(&project);
    let owner = seed_terminal_opencode_owner(
        &project,
        owner_name,
        &owner_branch,
        &worktree,
        "issue-814",
        issue_id,
    );
    append_parked_event(&project, &owner.slice_id);

    let fake_bin = temp.path().join("fake-bin");
    std::fs::create_dir_all(&fake_bin).expect("create fake Codex bin");
    let captured_args = project.join("captured-codex-args.txt");
    let launched = project.join("codex-launched");
    let fake_codex = fake_bin.join("codex");
    std::fs::write(
        &fake_codex,
        format!(
            "#!/bin/sh\nprintf '%s\\n' \"$@\" > '{}'\nprintf started > '{}'\nsleep 30\n",
            captured_args.display(),
            launched.display()
        ),
    )
    .expect("write Codex executable shim");
    std::fs::set_permissions(&fake_codex, std::fs::Permissions::from_mode(0o755))
        .expect("make Codex shim executable");

    let prior_path = std::env::var_os("PATH").unwrap_or_default();
    let mut path_value = std::ffi::OsString::from(&fake_bin);
    path_value.push(":");
    path_value.push(prior_path);
    let _path_restore = EnvironmentRestore::set("PATH", path_value.clone());
    let socket = format!("exo-route-resume-{}", std::process::id());
    let session = format!("exo-route-resume-{}", std::process::id());
    let _socket_restore =
        EnvironmentRestore::set("EXOMONAD_TMUX_SOCKET", std::ffi::OsString::from(&socket));
    let created = Command::new("tmux")
        .args([
            "-L",
            &socket,
            "new-session",
            "-d",
            "-s",
            &session,
            "-n",
            "TL",
            "sleep",
            "300",
        ])
        .status()
        .expect("start isolated tmux session");
    assert!(created.success(), "isolated tmux session starts");
    let path_text = path_value.to_string_lossy().into_owned();
    let set_path = Command::new("tmux")
        .args(["-L", &socket, "set-environment", "-g", "PATH", &path_text])
        .status()
        .expect("set isolated tmux PATH");
    assert!(
        set_path.success(),
        "isolated tmux server receives shim PATH"
    );

    let forgejo = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/api/v1/repos/acme/repo/pulls"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!([])))
        .mount(&forgejo)
        .await;
    let mut services = super::Services::benchmark(project.clone())
        .expect("construct production AgentHandler services");
    services.forgejo_client = Some(
        ForgejoClient::new(&forgejo.uri(), "test-only-token")
            .expect("construct isolated Forgejo client"),
    );
    services.agent_resolver =
        std::sync::Arc::new(exomonad_core::services::AgentResolver::load(project.clone()).await);
    services.event_log = Some(std::sync::Arc::new(
        EventLog::open(project.join(".exo/logs")).expect("open event log"),
    ));
    let services = std::sync::Arc::new(services);
    let route = json!({
        "role": "worker",
        "harness": "codex/gpt-6-luna",
        "agent_type": "codex",
        "model": "gpt-6-luna",
        "effort": "xhigh",
        "configuration_fingerprint": "sha256:accepted-config",
        "policy_fingerprint": "sha256:accepted-policy",
        "manifest_digest": "sha256:accepted-manifest",
        "route_fingerprint": "sha256:accepted-route",
        "dispatch_intent_id": "dispatch-814-resume-2",
        "scope_id": "issue-814"
    });
    EventLog::open(project.join(".exo/logs"))
        .expect("open event log for accepted role route")
        .append(
            "tl.dispatch_intended",
            "root",
            &json!({
                "slice_id": "issue-814",
                "intent_id": "dispatch-814-resume-2",
                "boundary": "dispatch_intended",
                "started_at": "2026-11-03T12:00:00Z",
                "attempt": 2,
                "harness": "codex/gpt-6-luna",
                "agent_type": "codex",
                "model": "gpt-6-luna",
                "role": "worker",
                "effort": "xhigh",
                "route_configuration_fingerprint": "sha256:accepted-config",
                "route_policy_fingerprint": "sha256:accepted-policy",
                "route_manifest_digest": "sha256:accepted-manifest",
                "route_fingerprint": "sha256:accepted-route"
            }),
        )
        .expect("record accepted dispatch route before host resume");
    let service = std::sync::Arc::new(
        AgentControlService::new(services.clone())
            .with_tmux_session(session)
            .with_birth_branch(
                exomonad_core::BirthBranch::try_from_str("main").expect("main branch"),
            )
            .with_spawn_agent_type(exomonad_core::services::agent_control::AgentType::Codex)
            .with_spawn_agent_model(Some("gpt-6.1-sol".to_string()))
            .with_spawn_agent_effort(Some("medium".to_string()))
            .with_role_route_configuration_fingerprint("sha256:accepted-config".to_string()),
    );
    let handler = AgentHandler::new(service, services);
    let runtime = build_wasm_runtime_with_agent_handler(&project, handler).await;
    let fingerprint = worktree_fingerprint(&worktree);
    let mut stale_route = route.clone();
    stale_route["configuration_fingerprint"] = json!("sha256:stale-config");
    let missing = call_tool(
        &runtime,
        "tl",
        "resume_blocked_leaf",
        json!({
            "chainlink_issue_id": issue_id,
            "expected_invocation_id": "opencode-invocation-1",
            "expected_branch": owner_branch,
            "expected_worktree_fingerprint": fingerprint,
            "task": "Continue the accepted #814 assignment",
            "human_approved": true
        }),
    )
    .await;
    assert_eq!(
        missing["success"], false,
        "missing controller route is refused"
    );
    assert!(
        missing["error"]
            .as_str()
            .unwrap_or_default()
            .contains("accepted role route"),
        "missing-route refusal is actionable: {missing:#}"
    );
    assert!(
        !launched.exists(),
        "missing route must not start the harness"
    );

    let refused = call_tool(
        &runtime,
        "tl",
        "resume_blocked_leaf",
        json!({
            "chainlink_issue_id": issue_id,
            "expected_invocation_id": "opencode-invocation-1",
            "expected_branch": owner_branch,
            "expected_worktree_fingerprint": fingerprint,
            "task": "Continue the accepted #814 assignment",
            "human_approved": true,
            "role_route": stale_route
        }),
    )
    .await;
    assert_eq!(
        refused["success"], false,
        "stale route is rejected before launch"
    );
    assert!(!launched.exists(), "stale route must not start the harness");
    assert_eq!(
        std::fs::read(&owner.invocation_path).expect("prior invocation remains unchanged"),
        owner.invocation_bytes,
        "stale route refusal does not replace the historical invocation"
    );

    let output = call_tool(
        &runtime,
        "tl",
        "resume_blocked_leaf",
        json!({
            "chainlink_issue_id": issue_id,
            "expected_invocation_id": "opencode-invocation-1",
            "expected_branch": owner_branch,
            "expected_worktree_fingerprint": fingerprint,
            "task": "Continue the accepted #814 assignment",
            "human_approved": true,
            "role_route": route
        }),
    )
    .await;

    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
    while !launched.exists() && std::time::Instant::now() < deadline {
        tokio::time::sleep(std::time::Duration::from_millis(20)).await;
    }
    let actual_args = std::fs::read_to_string(&captured_args).unwrap_or_default();
    let invocation: serde_json::Value = serde_json::from_slice(
        &std::fs::read(&owner.invocation_path).expect("successor invocation is persisted"),
    )
    .expect("successor invocation is JSON");
    let identity: serde_json::Value = serde_json::from_slice(
        &std::fs::read(
            project
                .join(".exo/agents")
                .join(&owner_name)
                .join("identity.json"),
        )
        .expect("successor owner identity is persisted"),
    )
    .expect("successor identity is JSON");
    let _ = Command::new("tmux")
        .args(["-L", &socket, "kill-server"])
        .status();

    assert_eq!(
        output["success"], true,
        "blocked resume succeeds: {output:#}; invocation: {invocation:#}"
    );
    assert!(launched.exists(), "Codex shim is actually launched");
    assert!(
        actual_args.contains("gpt-6-luna"),
        "selected model reaches argv: {actual_args:?}"
    );
    assert!(
        actual_args.contains("xhigh"),
        "selected effort reaches argv: {actual_args:?}"
    );
    assert_eq!(invocation["runtime"], "codex");
    assert_eq!(invocation["model"], "gpt-6-luna");
    assert_eq!(invocation["effort"], "xhigh");
    assert_eq!(identity["agent_type"], "codex");
    assert_eq!(identity["model"], "gpt-6-luna");
    assert_eq!(identity["effort"], "xhigh");
    assert_eq!(
        identity["role_route"], invocation["role_route"],
        "owner identity and invocation retain identical accepted route provenance"
    );
    assert_eq!(invocation["role_route"]["role"], "worker");
    assert_eq!(
        invocation["role_route"]["dispatch_intent_id"],
        "dispatch-814-resume-2"
    );
    assert_eq!(invocation["role_route"]["scope_id"], "issue-814");
    assert_eq!(
        output["result"]["agent"]["role_route"], invocation["role_route"],
        "compiled tool response reports the exact persisted route provenance"
    );
    assert_eq!(
        output["result"]["invocation"]["role_route"], invocation["role_route"],
        "compiled invocation receipt retains the exact persisted route provenance"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[serial_test::serial]
async fn compiled_fresh_spawns_return_the_persisted_controller_route() {
    use std::os::unix::fs::PermissionsExt;

    let temp = tempfile::tempdir().expect("temporary route-bound spawn fixture");
    let project = temp.path().join("checkout");
    let remote = temp.path().join("fixture.git");
    initialize_repository(&project, &remote);

    let fake_bin = temp.path().join("fake-bin");
    std::fs::create_dir_all(&fake_bin).expect("create fake Codex bin");
    let args_log = project.join(".exo/codex-argv.log");
    let launch_log = project.join(".exo/codex-launches.log");
    let fake_codex = fake_bin.join("codex");
    std::fs::write(
        &fake_codex,
        format!(
            "#!/bin/sh\nprintf 'launch\\n' >> '{}'\nprintf '%s\\n' \"$*\" >> '{}'\nsleep 30\n",
            launch_log.display(),
            args_log.display(),
        ),
    )
    .expect("write Codex executable shim");
    std::fs::set_permissions(&fake_codex, std::fs::Permissions::from_mode(0o755))
        .expect("make Codex shim executable");
    let fake_shell = temp.path().join("fake-shell");
    std::fs::write(
        &fake_shell,
        "#!/bin/sh\nif [ \"$1\" = \"-l\" ]; then shift; fi\nif [ \"$1\" = \"-c\" ]; then exec /bin/sh -c \"$2\"; fi\nexec /bin/sh \"$@\"\n",
    )
    .expect("write stable test shell");
    std::fs::set_permissions(&fake_shell, std::fs::Permissions::from_mode(0o755))
        .expect("make test shell executable");

    let prior_path = std::env::var_os("PATH").unwrap_or_default();
    let mut path = std::ffi::OsString::from(&fake_bin);
    path.push(":");
    path.push(prior_path);
    let _path_restore = EnvironmentRestore::set("PATH", path.clone());
    let _shell_restore = EnvironmentRestore::set("SHELL", fake_shell.into_os_string());
    let socket = format!("exo-fresh-route-{}", std::process::id());
    let session = format!("exo-fresh-route-{}", std::process::id());
    let _socket_restore =
        EnvironmentRestore::set("EXOMONAD_TMUX_SOCKET", std::ffi::OsString::from(&socket));
    let created = Command::new("tmux")
        .args([
            "-L",
            &socket,
            "new-session",
            "-d",
            "-s",
            &session,
            "-n",
            "TL",
            "sleep",
            "300",
        ])
        .status()
        .expect("start isolated tmux session");
    assert!(created.success(), "isolated tmux session starts");
    let path_text = path.to_string_lossy().into_owned();
    let set_path = Command::new("tmux")
        .args(["-L", &socket, "set-environment", "-g", "PATH", &path_text])
        .status()
        .expect("set isolated tmux PATH");
    assert!(
        set_path.success(),
        "isolated tmux server receives shim PATH"
    );

    let worker_route = accepted_route_without_model_or_effort(
        "worker-no-default",
        "dispatch-worker-no-default",
        "worker",
    );
    let leaf_route = accepted_leaf_route("leaf-route", "dispatch-leaf-route");
    let default_leaf_route = accepted_route_without_model_or_effort(
        "leaf-no-default",
        "dispatch-leaf-no-default",
        "dev",
    );
    record_route_dispatch(&project, &worker_route);
    record_route_dispatch(&project, &leaf_route);
    record_route_dispatch(&project, &default_leaf_route);
    let forgejo = MockServer::start().await;
    let mut services = super::Services::benchmark(project.clone())
        .expect("construct production AgentHandler services");
    services.forgejo_client = Some(
        ForgejoClient::new(&forgejo.uri(), "test-only-token")
            .expect("construct isolated Forgejo client"),
    );
    services.agent_resolver =
        std::sync::Arc::new(exomonad_core::services::AgentResolver::load(project.clone()).await);
    services.event_log = Some(std::sync::Arc::new(
        EventLog::open(project.join(".exo/logs")).expect("open event log"),
    ));
    let services = std::sync::Arc::new(services);
    let service = std::sync::Arc::new(
        AgentControlService::new(services.clone())
            .with_tmux_session(session)
            .with_birth_branch(
                exomonad_core::BirthBranch::try_from_str("main").expect("main branch"),
            )
            .with_spawn_agent_type(exomonad_core::services::agent_control::AgentType::Codex)
            .with_spawn_agent_model(Some("gpt-6.1-sol".to_string()))
            .with_spawn_agent_effort(Some("medium".to_string()))
            .with_role_route_configuration_fingerprint("sha256:accepted-config".to_string()),
    );
    let handler = AgentHandler::new(service, services);
    let runtime = build_wasm_runtime_with_agent_handler(&project, handler).await;

    let worker = call_tool(
        &runtime,
        "tl",
        "spawn_worker",
        json!({
            "name": "worker-no-default",
            "task": "Do not inherit the host's unselected worker model or effort",
            "intent_id": "dispatch-worker-no-default",
            "agent_type": "codex",
            "role_route": worker_route
        }),
    )
    .await;
    assert_eq!(worker["success"], true, "worker spawn succeeds: {worker:#}");
    let leaf = call_tool(
        &runtime,
        "tl",
        "spawn_leaf",
        json!({
            "name": "leaf-route",
            "task": "Exercise the selected leaf route",
            "intent_id": "dispatch-leaf-route",
            "agent_type": "codex",
            "model": "gpt-6-luna",
            "role_route": leaf_route
        }),
    )
    .await;
    assert_eq!(leaf["success"], true, "leaf spawn succeeds: {leaf:#}");
    let leaf_without_selected_values = call_tool(
        &runtime,
        "tl",
        "spawn_leaf",
        json!({
            "name": "leaf-no-default",
            "task": "Do not inherit the host's unselected leaf model or effort",
            "intent_id": "dispatch-leaf-no-default",
            "agent_type": "codex",
            "role_route": default_leaf_route
        }),
    )
    .await;
    assert_eq!(
        leaf_without_selected_values["success"], true,
        "leaf with intentionally absent route values succeeds: {leaf_without_selected_values:#}"
    );

    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
    while std::fs::read_to_string(&launch_log)
        .map(|contents| contents.lines().count() < 4)
        .unwrap_or(true)
        && std::time::Instant::now() < deadline
    {
        tokio::time::sleep(std::time::Duration::from_millis(20)).await;
    }
    let _ = Command::new("tmux")
        .args(["-L", &socket, "kill-server"])
        .status();

    let leaf_receipt = &leaf["result"];
    for (receipt, route, owner) in [(leaf_receipt, &leaf_route, "leaf-route-codex")] {
        assert_eq!(receipt["role_route"], *route);
        assert_eq!(receipt["invocation_runtime"], "codex");
        assert_eq!(receipt["invocation_trigger"], "spawn");
        assert!(receipt["invocation_id"]
            .as_str()
            .is_some_and(|value| !value.is_empty()));
        assert_eq!(receipt["agent_id"], owner);
        let agent_dir = project.join(".exo/agents").join(owner);
        let invocation: Value = serde_json::from_slice(
            &std::fs::read(agent_dir.join("invocation.json")).expect("read invocation record"),
        )
        .expect("invocation is JSON");
        let identity: Value = serde_json::from_slice(
            &std::fs::read(agent_dir.join("identity.json")).expect("read owner identity"),
        )
        .expect("identity is JSON");
        assert_eq!(invocation["role_route"], *route);
        assert_eq!(identity["role_route"], *route);
        assert_eq!(invocation["invocation_id"], receipt["invocation_id"]);
        assert_eq!(invocation["model"], "gpt-6-luna");
        assert_eq!(invocation["effort"], "xhigh");
    }
    for (receipt, route, owner) in [
        (
            &worker["result"]["spawned"][0],
            &worker_route,
            "worker-no-default-codex",
        ),
        (
            &leaf_without_selected_values["result"],
            &default_leaf_route,
            "leaf-no-default-codex",
        ),
    ] {
        assert_eq!(receipt["role_route"], *route);
        assert_eq!(receipt["agent_id"], owner);
        let agent_dir = project.join(".exo/agents").join(owner);
        let invocation: Value = serde_json::from_slice(
            &std::fs::read(agent_dir.join("invocation.json")).expect("read no-default invocation"),
        )
        .expect("invocation is JSON");
        let identity: Value = serde_json::from_slice(
            &std::fs::read(agent_dir.join("identity.json")).expect("read no-default identity"),
        )
        .expect("identity is JSON");
        assert!(invocation["model"].is_null());
        assert!(invocation["effort"].is_null());
        assert!(identity["model"].is_null());
        assert!(identity["effort"].is_null());
        assert_eq!(invocation["role_route"], *route);
        assert_eq!(identity["role_route"], *route);
        assert_eq!(invocation["invocation_id"], receipt["invocation_id"]);
    }
    let args = std::fs::read_to_string(&args_log).expect("fake Codex recorded both launches");
    let launches = std::fs::read_to_string(&launch_log).expect("launch markers are retained");
    assert_eq!(
        launches.lines().count(),
        3,
        "one worker and two leaf invocations launched"
    );
    assert!(
        args.contains("gpt-6-luna"),
        "selected model reaches argv: {args}"
    );
    assert!(
        args.contains("xhigh"),
        "selected effort reaches argv: {args}"
    );
    assert!(
        !args.contains("gpt-6.1-sol"),
        "accepted routes without a model must not inherit the host model: {args}"
    );
    assert!(
        !args.contains("medium"),
        "accepted routes without effort must not inherit the host effort: {args}"
    );
    let worker_config = std::fs::read_to_string(
        project.join(".exo/agents/worker-no-default-codex/.codex/config.toml"),
    )
    .expect("read worker Codex config");
    assert!(!worker_config.contains("gpt-6.1-sol"));
    assert!(!worker_config.contains("medium"));
    let leaf_identity: Value = serde_json::from_slice(
        &std::fs::read(project.join(".exo/agents/leaf-no-default-codex/identity.json"))
            .expect("read leaf identity"),
    )
    .expect("identity is JSON");
    let leaf_worktree = PathBuf::from(
        leaf_identity["working_dir"]
            .as_str()
            .expect("leaf identity has working directory"),
    );
    let leaf_config = std::fs::read_to_string(leaf_worktree.join(".codex/config.toml"))
        .expect("read leaf Codex config");
    assert!(!leaf_config.contains("gpt-6.1-sol"));
    assert!(!leaf_config.contains("medium"));
}

fn accepted_leaf_route(scope_id: &str, intent_id: &str) -> Value {
    accepted_route_with_selected_values(scope_id, intent_id, "dev")
}

fn accepted_route_with_selected_values(scope_id: &str, intent_id: &str, role: &str) -> Value {
    json!({
        "role": role,
        "harness": "codex/gpt-6-luna",
        "agent_type": "codex",
        "model": "gpt-6-luna",
        "effort": "xhigh",
        "configuration_fingerprint": "sha256:accepted-config",
        "policy_fingerprint": "sha256:accepted-policy",
        "manifest_digest": "sha256:accepted-manifest",
        "route_fingerprint": format!("sha256:route-{scope_id}"),
        "dispatch_intent_id": intent_id,
        "scope_id": scope_id
    })
}

fn accepted_route_without_model_or_effort(scope_id: &str, intent_id: &str, role: &str) -> Value {
    json!({
        "role": role,
        "harness": "codex",
        "agent_type": "codex",
        "model": null,
        "effort": null,
        "configuration_fingerprint": "sha256:accepted-config",
        "policy_fingerprint": "sha256:accepted-policy",
        "manifest_digest": "sha256:accepted-manifest",
        "route_fingerprint": format!("sha256:route-{scope_id}"),
        "dispatch_intent_id": intent_id,
        "scope_id": scope_id
    })
}

fn record_route_dispatch(project: &Path, route: &Value) {
    let intent_id = route["dispatch_intent_id"]
        .as_str()
        .expect("route dispatch intent");
    let mut data = json!({
        "slice_id": route["scope_id"],
        "intent_id": intent_id,
        "boundary": "dispatch_intended",
        "started_at": "2026-11-03T12:00:00Z",
        "attempt": 1,
        "harness": route["harness"],
        "agent_type": route["agent_type"],
        "role": route["role"],
        "route_configuration_fingerprint": route["configuration_fingerprint"],
        "route_policy_fingerprint": route["policy_fingerprint"],
        "route_manifest_digest": route["manifest_digest"],
        "route_fingerprint": route["route_fingerprint"]
    });
    for field in ["model", "effort"] {
        if route[field].as_str().is_some_and(|value| !value.is_empty()) {
            data[field] = route[field].clone();
        }
    }
    EventLog::open(project.join(".exo/logs"))
        .expect("open event log for accepted route")
        .append("tl.dispatch_intended", "root", &data)
        .expect("record durable role route dispatch authorization");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[serial_test::serial]
async fn compiled_resume_pr_preserves_typed_dirty_refusal_after_owner_and_head_checks() {
    let temp = tempfile::tempdir().expect("temporary PR-resume fixture");
    let project = temp.path().join("checkout");
    let remote = temp.path().join("fixture.git");
    let owner_name = "pr-owner-codex";
    let owner_branch = format!("main.{owner_name}");
    let (worktree, head_sha) =
        initialize_resume_repository_with_pr(&project, &remote, owner_name, &owner_branch);
    let owner = seed_dormant_owner(
        &project,
        owner_name,
        &owner_branch,
        &worktree,
        "slice-pr-resume",
        None,
    );
    std::fs::write(
        project.join("SOURCE-DIRTY.txt"),
        "uncommitted source edit\n",
    )
    .expect("dirty source worktree for preflight");
    let forgejo = MockServer::start().await;
    mount_resume_pr_observations(&forgejo, 46, &owner.branch, &head_sha).await;
    let client = ForgejoClient::new(&forgejo.uri(), "test-only-token")
        .expect("construct isolated Forgejo client");
    let production = build_production_agent_handler(&project, client).await;
    let runtime = build_wasm_runtime_with_agent_handler(
        &project,
        ResumePrWatcher {
            production,
            pr_number: 46,
            branch: owner.branch.clone(),
            head_sha: head_sha.clone(),
        },
    )
    .await;

    let stale = call_tool(
        &runtime,
        "tl",
        "resume_pr",
        json!({
            "pr_number": 46,
            "expected_head_sha": "stale-head-sha",
            "task": "Resume the existing owner after review"
        }),
    )
    .await;
    assert_tool_error(&stale, "resume_pr");
    assert_eq!(stale["error_kind"], Value::Null);
    assert!(
        stale["error"]
            .as_str()
            .unwrap_or_default()
            .contains("head changed"),
        "stale PR head should be refused before host resume: {stale:#}"
    );
    assert!(read_project_events(&project).is_empty());

    let output = call_tool(
        &runtime,
        "tl",
        "resume_pr",
        json!({
            "pr_number": 46,
            "expected_head_sha": head_sha,
            "task": "Resume the existing owner after review"
        }),
    )
    .await;

    assert_typed_dirty_refusal(&output, "resume_pr");
    let events = read_project_events(&project);
    let failed = only_spawn_failed(&events);
    assert_eq!(failed["data"]["child_agent"], owner.name);
    assert_eq!(failed["data"]["code"], DIRTY_WORKTREE_CODE);
    assert!(failed["data"].get("intent_id").is_none());
    assert_eq!(
        std::fs::read(&owner.invocation_path).expect("read prior PR invocation after refusal"),
        owner.invocation_bytes,
        "typed refusal must preserve the existing owner invocation"
    );
    assert_eq!(agent_directory_names(&project), [owner.name.as_str()]);
    assert_no_spawn_or_resume_event(&events);
}

fn initialize_resume_repository(
    project: &Path,
    remote: &Path,
    owner_name: &str,
    owner_branch: &str,
) -> PathBuf {
    initialize_repository(project, remote);
    create_owner_worktree(project, owner_name, owner_branch)
}

fn initialize_resume_repository_with_pr(
    project: &Path,
    remote: &Path,
    owner_name: &str,
    owner_branch: &str,
) -> (PathBuf, String) {
    let worktree = initialize_resume_repository(project, remote, owner_name, owner_branch);
    std::fs::write(worktree.join("review-fix.txt"), "published owner change\n")
        .expect("write PR commit");
    run_git(&worktree, &["add", "review-fix.txt"]);
    run_git(&worktree, &["commit", "--quiet", "-m", "owner PR head"]);
    let head_sha = git_output(&worktree, &["rev-parse", "HEAD"]);
    run_git(project, &["push", "fixture", owner_branch]);
    (worktree, head_sha)
}

fn initialize_repository(project: &Path, remote: &Path) {
    std::fs::create_dir_all(project).expect("create fixture repository");
    run_git(project, &["init", "--quiet", "--initial-branch=main"]);
    run_git(project, &["config", "user.name", "WASM resume fixture"]);
    run_git(
        project,
        &["config", "user.email", "resume-fixture@example.invalid"],
    );
    std::fs::write(project.join(".gitignore"), ".exo/\n.chainlink/\n")
        .expect("write fixture ignores");
    std::fs::write(project.join("README.md"), "fixture baseline\n")
        .expect("write fixture baseline");
    run_git(project, &["add", ".gitignore", "README.md"]);
    run_git(project, &["commit", "--quiet", "-m", "fixture baseline"]);
    let remote_path = remote.to_str().expect("fixture remote path is UTF-8");
    run_git(project, &["init", "--bare", "--quiet", remote_path]);
    run_git(
        project,
        &[
            "remote",
            "add",
            "origin",
            "https://forgejo.local/acme/repo.git",
        ],
    );
    run_git(project, &["remote", "add", "fixture", remote_path]);
    run_git(project, &["push", "fixture", "main"]);
}

struct ResumePrWatcher {
    production: AgentHandler<super::Services>,
    pr_number: u64,
    branch: String,
    head_sha: String,
}

#[async_trait]
impl EffectHandler for ResumePrWatcher {
    fn namespace(&self) -> &str {
        "agent"
    }

    async fn handle(
        &self,
        effect_type: &str,
        payload: &[u8],
        ctx: &EffectContext,
    ) -> EffectResult<Vec<u8>> {
        if effect_type != "agent.watcher_pr_state" {
            return self.production.handle(effect_type, payload, ctx).await;
        }
        let request = agent_proto::WatcherPrStateRequest::decode(payload)
            .map_err(|error| EffectError::invalid_input(error.to_string()))?;
        if request.pr_number != self.pr_number {
            return Err(EffectError::invalid_input(format!(
                "unexpected watcher PR {}",
                request.pr_number
            )));
        }
        Ok(agent_proto::WatcherPrStateResponse {
            success: true,
            pr_number: self.pr_number,
            found: true,
            review_state: "pending".into(),
            ci_status: "success".into(),
            head_sha: self.head_sha.clone(),
            head_branch: self.branch.clone(),
            base_branch: "main".into(),
            pr_state: "open".into(),
            head_reachable: true,
            ..Default::default()
        }
        .encode_to_vec())
    }
}

fn create_owner_worktree(project: &Path, owner_name: &str, owner_branch: &str) -> PathBuf {
    let relative = PathBuf::from(".exo/worktrees").join(owner_name);
    let full = project.join(&relative);
    let full_path = full.to_str().expect("owner worktree path is UTF-8");
    run_git(
        project,
        &["worktree", "add", "--quiet", "-b", owner_branch, full_path],
    );
    full
}

fn seed_dormant_owner(
    project: &Path,
    owner_name: &str,
    owner_branch: &str,
    worktree: &Path,
    slice_id: &str,
    active_issue: Option<u64>,
) -> OwnerFixture {
    let relative_worktree = worktree
        .strip_prefix(project)
        .expect("owner worktree is inside project")
        .to_path_buf();
    let agent_dir = project.join(".exo/agents").join(owner_name);
    std::fs::create_dir_all(&agent_dir).expect("create durable owner checkpoint");
    std::fs::write(
        agent_dir.join("identity.json"),
        serde_json::to_vec(&json!({
            "agent_name": owner_name,
            "slug": owner_name.strip_suffix("-codex").unwrap_or(owner_name),
            "agent_type": "codex",
            "birth_branch": owner_branch,
            "parent_branch": "main",
            "working_dir": worktree,
            "display_name": format!("🤖 {owner_name}"),
            "topology": "worktree_per_agent",
            "ledger_owned": true,
            "slice_id": slice_id
        }))
        .expect("serialize durable owner identity"),
    )
    .expect("write durable owner identity");
    if let Some(issue_id) = active_issue {
        std::fs::write(agent_dir.join("active_issue"), issue_id.to_string())
            .expect("write active issue binding");
    }
    let invocation_path = agent_dir.join("invocation.json");
    let invocation_bytes = serde_json::to_vec(&json!({
        "invocation_id": "parked-invocation-1",
        "runtime": "codex",
        "trigger": "spawn",
        "mode": "one_shot",
        "routing": { "window_id": null, "pane_id": null, "parent_tab": null },
        "started_at": 1,
        "ended_at": 2,
        "status": "exited",
        "exit_code": 1,
        "generation": 1,
        "slice_id": slice_id,
        "branch": owner_branch,
        "worktree": worktree
    }))
    .expect("serialize prior invocation");
    std::fs::write(&invocation_path, &invocation_bytes).expect("write prior invocation");
    OwnerFixture {
        name: owner_name.to_string(),
        branch: owner_branch.to_string(),
        slice_id: slice_id.to_string(),
        worktree_relative: relative_worktree,
        invocation_path,
        invocation_bytes,
    }
}

fn seed_terminal_opencode_owner(
    project: &Path,
    owner_name: &str,
    owner_branch: &str,
    worktree: &Path,
    slice_id: &str,
    active_issue: u64,
) -> OwnerFixture {
    let mut owner = seed_dormant_owner(
        project,
        owner_name,
        owner_branch,
        worktree,
        slice_id,
        Some(active_issue),
    );
    let agent_dir = project.join(".exo/agents").join(owner_name);
    let identity_path = agent_dir.join("identity.json");
    let mut identity: Value = serde_json::from_slice(
        &std::fs::read(&identity_path).expect("read historical owner identity"),
    )
    .expect("historical owner identity is JSON");
    identity["agent_type"] = json!("opencode");
    identity["slug"] = json!(owner_name.strip_suffix("-opencode").unwrap_or(owner_name));
    std::fs::write(
        identity_path,
        serde_json::to_vec(&identity).expect("serialize OpenCode owner identity"),
    )
    .expect("write OpenCode owner identity");

    owner.invocation_bytes = serde_json::to_vec(&json!({
        "invocation_id": "opencode-invocation-1",
        "runtime": "opencode",
        "trigger": "spawn",
        "mode": "one_shot",
        "routing": { "window_id": null, "pane_id": null, "parent_tab": null },
        "started_at": 1,
        "ended_at": 2,
        "status": "failed",
        "exit_code": 1,
        "exit_reason": "Insufficient account funds",
        "generation": 1,
        "slice_id": slice_id,
        "branch": owner_branch,
        "worktree": worktree
    }))
    .expect("serialize terminal OpenCode invocation");
    std::fs::write(&owner.invocation_path, &owner.invocation_bytes)
        .expect("write terminal OpenCode invocation");
    owner
}

struct EnvironmentRestore {
    key: &'static str,
    previous: Option<std::ffi::OsString>,
}

impl EnvironmentRestore {
    fn set(key: &'static str, value: std::ffi::OsString) -> Self {
        let previous = std::env::var_os(key);
        std::env::set_var(key, value);
        Self { key, previous }
    }
}

impl Drop for EnvironmentRestore {
    fn drop(&mut self) {
        if let Some(previous) = self.previous.take() {
            std::env::set_var(self.key, previous);
        } else {
            std::env::remove_var(self.key);
        }
    }
}

fn create_open_chainlink_issue(project: &Path) -> u64 {
    let init = Command::new("chainlink")
        .args(["init", "--no-hooks", "--quiet"])
        .current_dir(project)
        .output()
        .expect("Chainlink CLI is available to create isolated issue fixture");
    assert!(
        init.status.success(),
        "initialize temporary Chainlink database: {}",
        String::from_utf8_lossy(&init.stderr)
    );
    let created = Command::new("chainlink")
        .args([
            "issue",
            "create",
            "Disposable blocked resume fixture",
            "--priority",
            "low",
            "--quiet",
        ])
        .current_dir(project)
        .output()
        .expect("create open temporary Chainlink issue");
    assert!(
        created.status.success(),
        "create temporary Chainlink issue: {}",
        String::from_utf8_lossy(&created.stderr)
    );
    String::from_utf8_lossy(&created.stdout)
        .trim()
        .parse()
        .expect("quiet issue creation returns its numeric ID")
}

fn append_parked_event(project: &Path, slice_id: &str) {
    EventLog::open(project.join(".exo/logs"))
        .expect("open temporary event log")
        .append(
            "tl.slice_parked",
            "root",
            &json!({ "slice_id": slice_id, "park_cause": "external_dependency" }),
        )
        .expect("record authoritative parked event");
}

fn worktree_fingerprint(worktree: &Path) -> String {
    let output = Command::new("git")
        .args(["status", "--porcelain=v1", "-z"])
        .current_dir(worktree)
        .output()
        .expect("inspect parked owner worktree");
    assert!(output.status.success(), "parked worktree status succeeds");
    format!("sha256:{:x}", Sha256::digest(output.stdout))
}

async fn mount_resume_pr_observations(
    server: &MockServer,
    number: u64,
    branch: &str,
    head_sha: &str,
) {
    mount_open_pr(server, number, branch, head_sha).await;
    Mock::given(method("GET"))
        .and(path(format!(
            "/api/v1/repos/acme/repo/pulls/{number}/reviews"
        )))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!([])))
        .mount(server)
        .await;
    Mock::given(method("GET"))
        .and(path(format!(
            "/api/v1/repos/acme/repo/commits/{head_sha}/status"
        )))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "state": "pending",
            "statuses": []
        })))
        .mount(server)
        .await;
}

fn git_output(project: &Path, arguments: &[&str]) -> String {
    let output = Command::new("git")
        .args(arguments)
        .current_dir(project)
        .output()
        .expect("run Git fixture command");
    assert!(
        output.status.success(),
        "Git command {arguments:?} failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8_lossy(&output.stdout).trim().to_string()
}

fn assert_typed_dirty_refusal(output: &Value, tool: &str) {
    assert_tool_error(output, tool);
    assert_eq!(
        output["error_kind"], DIRTY_WORKTREE_CODE,
        "compiled {tool} response must retain the production preflight classification: {output:#}"
    );
    assert!(
        output["error"]
            .as_str()
            .is_some_and(|message| message.starts_with("TL preflight failed; spawning is blocked")),
        "the production diagnostic must remain visible: {output:#}"
    );
}

fn read_project_events(project: &Path) -> Vec<Value> {
    LedgerWriter::open_project(project)
        .expect("open project event ledger")
        .read_resolved_events()
        .expect("read project events")
        .into_iter()
        .map(|record| {
            json!({
                "event_type": record.event.event_type,
                "agent_id": record.event.agent_id,
                "data": record.event.data
            })
        })
        .collect()
}

fn only_spawn_failed(events: &[Value]) -> &Value {
    let failures = events
        .iter()
        .filter(|event| event["event_type"] == "agent.spawn_failed")
        .collect::<Vec<_>>();
    assert_eq!(failures.len(), 1, "exactly one failure event is recorded");
    failures[0]
}

fn agent_directory_names(project: &Path) -> Vec<String> {
    let mut names = std::fs::read_dir(project.join(".exo/agents"))
        .expect("read durable owner directories")
        .map(|entry| {
            entry
                .expect("read owner entry")
                .file_name()
                .to_string_lossy()
                .into_owned()
        })
        .collect::<Vec<_>>();
    names.sort();
    names
}

fn assert_no_spawn_or_resume_event(events: &[Value]) {
    assert!(!events.iter().any(|event| {
        matches!(
            event["event_type"].as_str(),
            Some("agent.spawned" | "agent.resumed")
        )
    }));
}
