//! exomonad: Rust host with embedded Haskell WASM plugin.
//!
//! This binary runs as a sidecar in each agent container, handling:
//! - Claude Code hooks via HTTP forwarding to the server
//! - MCP tools via WASM plugin (server-side)
//!
//! WASM plugins are loaded from file (server-side only).

mod app_state;
mod dashboard;
mod init;
mod logging;
mod mcp_stdio;
mod models;
mod new;
mod serve;
mod uds_client;

use exomonad::config;
use urlencoding::encode;

use anyhow::{Context, Result};
use clap::{Parser, Subcommand};
use exomonad_core::protocol::{Runtime as HookRuntime, ServiceRequest};
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
        /// Set root agent type (overrides --opencode-as-tl)
        #[arg(long)]
        tl: Option<String>,
        /// Set spawn agent type for workers/teammates
        #[arg(long)]
        worker: Option<String>,
        /// Model for the root TL when --tl=opencode (e.g. anthropic/claude-sonnet-4-5).
        /// Default: opencode picks (uses its built-in default model).
        #[arg(long)]
        tl_model: Option<String>,
        /// Model for spawned workers when --worker=opencode.
        /// Default: opencode picks (uses its built-in default model).
        #[arg(long)]
        worker_model: Option<String>,
        /// Set reviewer agent type (claude|opencode). Overrides [reviewer] in config.toml.
        #[arg(long)]
        reviewer: Option<String>,
        /// Model for the reviewer agent. Validated against the agent type.
        #[arg(long)]
        reviewer_model: Option<String>,
        /// Enable verbose observability logging: hooks, Chainlink commands, decisions, reviewer spawns, Forgejo CI events.
        /// Sets RUST_LOG=info, EXOMONAD_HOOK_TRACE=1, and EXOMONAD_CHAINLINK_TRACE=1 on the server; EXOMONAD_VERBOSE=1 session-wide.
        #[arg(long)]
        verbose: bool,
    },

    /// Initialize a new exomonad project in the current directory.
    /// Creates .exo/config.toml, .gitignore entries, copies WASM, and rules template.
    New {
        /// Project name (unused, reserved for future)
        #[arg(long)]
        name: Option<String>,
    },

    /// Recompile WASM plugin from Haskell source
    Recompile {
        /// WASM package to build (default: from config wasm_name, usually "devswarm")
        #[arg(long)]
        role: Option<String>,
    },

    /// Run MCP server on Unix domain socket (.exo/server.sock)
    ///
    /// Loads WASM from file path (not embedded) with hot reload on change.
    Serve,

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

    /// Reload WASM plugins (clears plugin cache, next call loads fresh from disk)
    Reload,

    /// Gracefully shut down the running server
    Shutdown,
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

        Commands::Serve => {
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
            reviewer,
            reviewer_model,
            verbose,
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
                reviewer,
                reviewer_model,
                verbose,
            )
            .await
            {
                tracing::error!(error = %e, "exomonad init failed: {:#}", e);
                return Err(e);
            }
        }

        Commands::New { name } => {
            new::run(name).await?;
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
