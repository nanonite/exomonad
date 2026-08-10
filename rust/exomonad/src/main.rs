//! exomonad: Rust host with embedded Haskell WASM plugin.
//!
//! This binary runs as a sidecar in each agent container, handling:
//! - Claude Code hooks via HTTP forwarding to the server
//! - MCP tools via WASM plugin (server-side)
//!
//! WASM plugins are loaded from file (server-side only).

mod app_state;
mod dashboard;
#[cfg(debug_assertions)]
mod experiment_analysis;
#[cfg(debug_assertions)]
mod experiment_harness;
mod init;
mod logging;
mod logs;
mod mcp_stdio;
mod models;
mod new;
mod revert;
mod serve;
mod uds_client;

use exomonad::config;
use urlencoding::encode;

use anyhow::{Context, Result};
use clap::{Parser, Subcommand};
use exomonad_core::protocol::{Runtime as HookRuntime, ServiceRequest};
use std::path::PathBuf;
use std::time::{Duration, Instant};

use exomonad_core::{
    codex_noop_envelope, format_codex_hook_response, normalize_codex_hook_payload, HookEnvelope,
    HookEventType,
};
use tokio::io::AsyncWriteExt;
use tokio::net::UnixStream;
use tracing::warn;

// ============================================================================
// CLI Types
// ============================================================================

#[derive(Parser)]
#[command(name = "exomonad")]
#[command(about = "ExoMonad: Rust host with embedded Haskell WASM plugin for agent orchestration")]
#[command(version)]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Handle a Claude Code hook event (thin HTTP client → server)
    Hook {
        /// The hook event type to handle
        #[arg(value_enum)]
        event: HookEventType,

        /// The runtime environment (Claude or Gemini)
        #[arg(long, default_value = "claude")]
        runtime: HookRuntime,
    },

    /// Initialize tmux session for this project.
    ///
    /// Creates a new session if none exists, or attaches to existing.
    /// Session name is read from .exo/config.toml tmux_session field.
    Init {
        /// Optionally override session name (default: from config)
        #[arg(long)]
        session: Option<String>,
        /// Delete existing session and create fresh
        #[arg(long)]
        recreate: bool,
        /// Use OpenCode as the root TL agent (default: Claude)
        #[arg(long)]
        opencode_as_tl: bool,
        /// Enable OpenRouter for LLM routing
        #[arg(long)]
        openrouter: bool,
        /// Set root agent type (valid: claude|gemini|opencode|codex|shoal;
        /// overrides --opencode-as-tl and the configured root_agent_type)
        #[arg(long)]
        tl: Option<String>,
        /// Set spawn agent type for workers/teammates. The worker effort level is
        /// inherited by forked TLs, leaves, ephemeral workers, and companions.
        #[arg(long)]
        worker: Option<String>,
        /// Model for the root TL agent.
        /// With --tl=opencode, stores [opencode].tl_model and validates via
        /// opencode models.
        /// With other TL agents, stores the root agent model.
        #[arg(long)]
        tl_model: Option<String>,
        /// Model for spawned workers with --worker=opencode.
        #[arg(long)]
        worker_model: Option<String>,
        /// Effort for the root TL: low, medium, high, xhigh, or max. CLI effort
        /// flags override config.toml; omitted effort defaults to medium.
        #[arg(long, value_enum)]
        tl_effort_level: Option<config::EffortLevel>,
        /// Effort inherited by forked TLs, leaves, ephemeral workers, and companions.
        /// For OpenCode this becomes the model's `--variant` when supported.
        #[arg(long, value_enum)]
        worker_effort_level: Option<config::EffortLevel>,
        /// Effort for automatically spawned reviewers: low, medium, high, xhigh, or max.
        /// CLI effort flags override config.toml.
        #[arg(long, value_enum)]
        reviewer_effort_level: Option<config::EffortLevel>,
        /// Set reviewer agent type (valid: claude|gemini|opencode|codex|shoal).
        /// Overrides [reviewer] in config.toml.
        #[arg(long)]
        reviewer: Option<String>,
        /// Model for the reviewer agent. Validated against the agent type.
        #[arg(long)]
        reviewer_model: Option<String>,
        /// Maximum reviewer rounds before a PR is escalated to Stuck.
        /// Overrides `.exo/review-policy.toml` for this initialized session.
        #[arg(long, value_parser = config::parse_positive_u32)]
        reviewer_max_rounds: Option<u32>,
        /// Enable additional human-readable tracing. Required structured
        /// telemetry is emitted regardless of this flag.
        #[arg(long)]
        verbose: bool,
        /// Pin which git remote exomonad's PR/CI operations (file_pr, merge_pr,
        /// the Forgejo watcher, and push) use, for repos with more than one
        /// remote configured (e.g. a GitHub `origin` alongside a Forgejo
        /// remote). Must already exist (`git remote add <name> <url>` first).
        /// Persisted via `git config --local exomonad.remote <name>`, so it
        /// applies to this repo and every worktree spawned from it. Omit to
        /// keep today's behavior: auto-detect, preferring a remote named
        /// `origin`.
        #[arg(long)]
        set_git_remote: Option<String>,
        /// Clear all persisted inbox messages and metadata before starting.
        #[arg(long)]
        reset_inbox: bool,
        /// Explicitly import legacy sources before starting. Repeat as needed.
        #[arg(long = "import-legacy", value_name = "PATH")]
        import_legacy: Vec<PathBuf>,
        /// Inspect explicit legacy sources without writing atlas.db.
        #[arg(long)]
        import_legacy_dry_run: bool,
    },

    /// Initialize a new exomonad project in the current directory.
    /// Creates .exo/config.toml, .gitignore entries, copies WASM, and rules template.
    New {
        /// Project name (unused, reserved for future)
        #[arg(long)]
        name: Option<String>,
        /// Maximum review rounds before a PR is escalated to Stuck (default: 5)
        #[arg(long)]
        reviewer_max_rounds: Option<u32>,
    },

    /// Recompile WASM plugin from Haskell source
    Recompile {
        /// WASM package to build (default: from config wasm_name, usually "devswarm")
        #[arg(long)]
        role: Option<String>,
    },

    /// Import legacy or immutable session logs into the rebuildable analysis store
    Logs {
        #[command(subcommand)]
        command: LogsCommands,
    },

    /// Run MCP server on Unix domain socket (.exo/server.sock)
    ///
    /// Loads WASM from file path (not embedded) with hot reload on change.
    Serve {
        /// Run the deterministic TL autonomy benchmark without Forgejo.
        #[cfg(debug_assertions)]
        #[arg(long)]
        mock_watcher: bool,
    },

    /// Run stdio MCP proxy (stdin/stdout ↔ UDS server)
    McpStdio {
        /// Agent role (e.g., "tl", "dev", "worker")
        #[arg(long)]
        role: String,
        /// Agent name (e.g., "root", "feature-impl")
        #[arg(long)]
        name: String,
    },

    /// Show the live Forgejo and agent watcher dashboard
    Watch {
        /// Refresh interval in seconds
        #[arg(long, default_value_t = 5)]
        interval: u64,
    },

    /// Reply to a UI request
    Reply {
        /// Request ID
        #[arg(long)]
        id: String,

        /// JSON payload
        #[arg(long)]
        payload: Option<String>,

        /// Cancel the request
        #[arg(long)]
        cancel: bool,
    },

    /// List available models per agent harness.
    Models {
        /// Harness: opencode, gemini, claude, or codex. Omit for all.
        #[arg(value_name = "HARNESS")]
        harness: Option<String>,
        /// Provider filter (opencode only). E.g. "anthropic", "openai".
        #[arg(value_name = "PROVIDER")]
        provider: Option<String>,
    },

    /// Undo workspace files created by exomonad init
    Revert {
        /// Also kill the configured tmux session
        #[arg(long)]
        kill_session: bool,
    },

    /// Reload WASM plugins (clears plugin cache, next call loads fresh from disk)
    Reload,

    /// Gracefully shut down the running server
    Shutdown,
}

#[derive(Subcommand)]
enum LogsCommands {
    /// Import explicit source paths without modifying the source files
    Import {
        /// Source file or directory. May be repeated.
        #[arg(long, value_name = "PATH", required = true)]
        source: Vec<PathBuf>,
        /// Input format: auto, jsonl, json, sqlite, or text.
        #[arg(long, default_value = "auto")]
        format: String,
        /// Inspect sources and report counts without writing atlas.db.
        #[arg(long)]
        dry_run: bool,
        /// Rebuild all derived rows from the selected sources.
        #[arg(long)]
        rebuild: bool,
    },
    /// Drop closed immutable ledger segments according to local retention.
    DropSegments {
        /// Drop segments older than this many seconds.
        #[arg(long, default_value_t = 2_592_000)]
        older_than_seconds: u64,
        /// Report fingerprints without deleting any segment.
        #[arg(long)]
        dry_run: bool,
    },
    /// Compile a shareable aggregate/sample artifact from the local L2 store.
    Export {
        /// Shareable export mode. Only aggregate is permitted.
        #[arg(long, default_value = "aggregate")]
        mode: String,
        /// Output directory for analysis.json, manifest.json, and privacy-report.json.
        #[arg(long, default_value = ".exo/analysis/export")]
        output: PathBuf,
    },
    /// Run local detectors, incidents, adjudication, and the measurement gate.
    Measure {
        /// Explicit preregistration manifest for a controlled contrast.
        #[arg(long)]
        preregistration: Option<PathBuf>,
        /// Output directory for local measurement artifacts.
        #[arg(long, default_value = ".exo/analysis/measurement")]
        output: PathBuf,
        /// Refuse success unless the measurement-ready gate passes.
        #[arg(long)]
        require_ready: bool,
        /// Local judge model/coder; repeat for independent judges.
        #[arg(long = "judge-model")]
        judge_models: Vec<String>,
        /// JSON file containing independent per-judge labels for the sampled signals.
        #[arg(long)]
        labels: Option<PathBuf>,
    },
}

// Main
// ============================================================================

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();

    let config = config::Config::discover().unwrap_or_else(|e| {
        eprintln!("[exomonad] No config found, using defaults: {e}");
        config::Config::default()
    });

    let agent_id = std::env::var("EXOMONAD_AGENT_ID").unwrap_or_else(|_| "root".to_string());
    let service_name = format!("exomonad/{}", agent_id);
    let _guard = match &cli.command {
        Commands::McpStdio { role, name } => {
            logging::init_mcp_stdio(config.otlp_endpoint.as_deref(), &service_name, role, name)
        }
        _ => logging::init(config.otlp_endpoint.as_deref(), &service_name),
    };

    match cli.command {
        Commands::McpStdio { ref role, ref name } => {
            return mcp_stdio::run(role, name).await;
        }

        Commands::Recompile { ref role } => {
            let role_str = role.as_deref().unwrap_or(&config.wasm_name);
            let project_dir = if config.project_dir.is_absolute() {
                config.project_dir.clone()
            } else {
                std::env::current_dir()?.join(&config.project_dir)
            };
            return exomonad::recompile::run_recompile(
                role_str,
                &project_dir,
                config.flake_ref.as_deref(),
            )
            .await;
        }

        Commands::Logs {
            command:
                LogsCommands::Import {
                    source,
                    format,
                    dry_run,
                    rebuild,
                },
        } => {
            let project_dir = if config.project_dir.is_absolute() {
                config.project_dir.clone()
            } else {
                std::env::current_dir()?.join(&config.project_dir)
            };
            return logs::run(&project_dir, source, format, dry_run, rebuild);
        }

        Commands::Logs {
            command:
                LogsCommands::DropSegments {
                    older_than_seconds,
                    dry_run,
                },
        } => {
            let project_dir = if config.project_dir.is_absolute() {
                config.project_dir.clone()
            } else {
                std::env::current_dir()?.join(&config.project_dir)
            };
            return logs::drop_segments(&project_dir, older_than_seconds, dry_run);
        }

        Commands::Logs {
            command: LogsCommands::Export { mode, output },
        } => {
            let project_dir = if config.project_dir.is_absolute() {
                config.project_dir.clone()
            } else {
                std::env::current_dir()?.join(&config.project_dir)
            };
            return logs::export(&project_dir, mode, output);
        }

        Commands::Logs {
            command:
                LogsCommands::Measure {
                    preregistration,
                    output,
                    require_ready,
                    judge_models,
                    labels,
                },
        } => {
            let project_dir = if config.project_dir.is_absolute() {
                config.project_dir.clone()
            } else {
                std::env::current_dir()?.join(&config.project_dir)
            };
            return logs::measure(
                &project_dir,
                preregistration,
                output,
                require_ready,
                judge_models,
                labels,
            );
        }

        Commands::Serve {
            #[cfg(debug_assertions)]
            mock_watcher,
        } => {
            #[cfg(debug_assertions)]
            if mock_watcher {
                return experiment_harness::run(&config).await;
            }
            return serve::run(&config).await;
        }

        Commands::Watch { interval } => {
            return dashboard::run(&config, Duration::from_secs(interval.max(1))).await;
        }

        Commands::Hook { event, runtime } => {
            let fail_open_stdout = || match runtime {
                HookRuntime::Codex => codex_noop_envelope().stdout,
                _ => r#"{"continue":true}"#.to_string(),
            };

            let mut path = format!("/hook?event={}&runtime={}", event, runtime);
            if let Ok(agent_id) = std::env::var("EXOMONAD_AGENT_ID") {
                path.push_str(&format!("&agent_id={}", encode(&agent_id)));
            }
            if let Ok(session_id) = std::env::var("EXOMONAD_SESSION_ID") {
                path.push_str(&format!("&session_id={}", encode(&session_id)));
            }
            if let Ok(role) = std::env::var("EXOMONAD_ROLE") {
                path.push_str(&format!("&role={}", encode(&role)));
            }
            if let Ok(chainlink_db) = std::env::var("CHAINLINK_DB") {
                path.push_str(&format!("&chainlink_db={}", encode(&chainlink_db)));
            }

            let mut body = String::new();
            use std::io::Read;
            std::io::stdin().read_to_string(&mut body)?;

            let is_root_session_start =
                event == HookEventType::SessionStart && std::env::var("EXOMONAD_AGENT_ID").is_err();

            let socket = if is_root_session_start {
                let start = Instant::now();
                let timeout_dur = Duration::from_secs(5);
                let mut found = None;
                while start.elapsed() < timeout_dur {
                    if let Ok(s) = uds_client::find_server_socket() {
                        found = Some(s);
                        break;
                    }
                    tokio::time::sleep(Duration::from_millis(500)).await;
                }
                match found {
                    Some(s) => s,
                    None => {
                        println!("{}", fail_open_stdout());
                        return Ok(());
                    }
                }
            } else {
                match uds_client::find_server_socket() {
                    Ok(s) => s,
                    Err(_) => {
                        println!("{}", fail_open_stdout());
                        return Ok(());
                    }
                }
            };

            let client = uds_client::ServerClient::new(socket);
            let json_body: serde_json::Value = match serde_json::from_str(&body) {
                Ok(v) => v,
                Err(e) => {
                    warn!(error = %e, body_len = body.len(), "Hook body is not valid JSON, using empty object");
                    serde_json::json!({})
                }
            };
            let json_body = match runtime {
                HookRuntime::Codex => normalize_codex_hook_payload(event, json_body),
                _ => json_body,
            };

            match client
                .post_json::<serde_json::Value, HookEnvelope>(&path, &json_body)
                .await
            {
                Ok(mut resp) => {
                    if runtime == HookRuntime::Codex {
                        resp = format_codex_hook_response(event, resp);
                    }
                    print!("{}", resp.stdout);
                    if resp.exit_code != 0 {
                        std::process::exit(resp.exit_code);
                    }
                }
                Err(_) => println!("{}", fail_open_stdout()),
            }
        }

        Commands::Init {
            session,
            recreate,
            opencode_as_tl,
            openrouter,
            tl,
            worker,
            tl_model,
            worker_model,
            tl_effort_level,
            worker_effort_level,
            reviewer_effort_level,
            reviewer,
            reviewer_model,
            reviewer_max_rounds,
            verbose,
            set_git_remote,
            reset_inbox,
            import_legacy,
            import_legacy_dry_run,
        } => {
            if let Err(e) = init::run(
                session,
                recreate,
                opencode_as_tl,
                openrouter,
                tl,
                worker,
                tl_model,
                worker_model,
                tl_effort_level,
                worker_effort_level,
                reviewer_effort_level,
                reviewer,
                reviewer_model,
                reviewer_max_rounds,
                verbose,
                set_git_remote,
                reset_inbox,
                import_legacy,
                import_legacy_dry_run,
            )
            .await
            {
                tracing::error!(error = %e, "exomonad init failed: {:#}", e);
                return Err(e);
            }
        }

        Commands::New {
            name,
            reviewer_max_rounds,
        } => {
            new::run(name, reviewer_max_rounds).await?;
        }

        Commands::Reply {
            id,
            payload,
            cancel,
        } => {
            let socket_path = std::env::var("EXOMONAD_CONTROL_SOCKET")
                .unwrap_or_else(|_| ".exo/sockets/control.sock".to_string());
            let mut stream = UnixStream::connect(&socket_path).await?;

            let parsed_payload = match payload {
                Some(p) => Some(serde_json::from_str(&p).context("Invalid JSON in --payload")?),
                None => None,
            };
            let request = ServiceRequest::UserInteraction {
                request_id: id,
                payload: parsed_payload,
                cancel,
            };

            let mut json = serde_json::to_vec(&request)?;
            json.push(b'\n');
            stream.write_all(&json).await?;
        }

        Commands::Revert { kill_session } => {
            return revert::run(&config, kill_session).await;
        }

        Commands::Reload => {
            let socket = uds_client::find_server_socket().context("Cannot find server socket.")?;
            let client = uds_client::ServerClient::new(socket);
            let resp: serde_json::Value =
                client.post_json("/reload", &serde_json::json!({})).await?;
            println!("{}", serde_json::to_string_pretty(&resp)?);
        }

        Commands::Models { harness, provider } => {
            return models::run(harness, provider).await;
        }

        Commands::Shutdown => {
            let socket = uds_client::find_server_socket().context("Cannot find server socket.")?;
            println!("Socket: {}", socket.display());

            // Read and validate PID file
            let pid_path = socket.parent().unwrap().join("server.pid");
            match std::fs::read_to_string(&pid_path) {
                Ok(content) => {
                    if let Ok(parsed) = serde_json::from_str::<serde_json::Value>(&content) {
                        if let Some(pid) = parsed.get("pid").and_then(|v| v.as_u64()) {
                            use nix::sys::signal;
                            use nix::unistd::Pid;
                            let alive = signal::kill(Pid::from_raw(pid as i32), None).is_ok();
                            println!(
                                "PID: {} ({})",
                                pid,
                                if alive { "running" } else { "not running" }
                            );
                            if !alive {
                                eprintln!(
                                    "Warning: server process {} is not running. Stale socket?",
                                    pid
                                );
                            }
                        }
                    }
                }
                Err(_) => {
                    eprintln!("Warning: no server.pid found at {}", pid_path.display());
                }
            }

            let client = uds_client::ServerClient::new(socket);
            println!("Connecting...");
            match client
                .post_json::<serde_json::Value, serde_json::Value>(
                    "/shutdown",
                    &serde_json::json!({}),
                )
                .await
            {
                Ok(resp) => println!("Server acknowledged shutdown: {}", resp),
                Err(e) => eprintln!("Shutdown request failed: {}", e),
            }
        }
    }

    Ok(())
}
