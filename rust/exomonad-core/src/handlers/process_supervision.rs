//! Bounded process execution with process-group cleanup on timeout and cancellation.

#[cfg(target_os = "linux")]
use crate::services::{try_record_managed_native_lock_proof_for_identity, ManagedProcessIdentity};
use crate::services::{EventLog, GitMutationLease};
use sha2::{Digest, Sha256};
use std::io;
#[cfg(target_os = "linux")]
use std::path::Path;
use std::process::ExitStatus;
use std::time::Duration;
use thiserror::Error;
use tokio::io::AsyncReadExt;
use tokio::process::{Child, Command};

const TERM_GRACE: Duration = Duration::from_millis(200);
const KILL_WAIT: Duration = Duration::from_secs(2);
const GROUP_POLL: Duration = Duration::from_millis(10);

#[derive(Debug, Error)]
pub(crate) enum ProcessRunError {
    #[error("could not start managed process ({0})")]
    Spawn(io::ErrorKind),
    #[error("managed process wait failed ({0})")]
    Wait(io::ErrorKind),
    #[error("managed process output read failed ({0})")]
    Output(io::ErrorKind),
    #[error("managed process exceeded its timeout")]
    Timeout,
    #[error("managed process group cleanup is unresolved: {0}")]
    Cleanup(&'static str),
    #[error("managed Git process audit could not be persisted")]
    Audit,
}

#[derive(Debug)]
pub(crate) struct ProcessRunOutput {
    pub status: ExitStatus,
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
    pub pid: u32,
    pub start_identity: Option<u64>,
    pub process_group_id: Option<u32>,
}

pub(crate) struct GitProcessAudit<'a> {
    pub lease: &'a GitMutationLease,
    pub event_log: &'a EventLog,
    pub operation_id: &'a str,
    pub agent_id: &'a str,
    pub birth_branch: &'a str,
}

impl GitProcessAudit<'_> {
    fn record_started(
        &self,
        pid: u32,
        start_identity: Option<u64>,
        process_group_id: Option<u32>,
    ) -> Result<(), ProcessRunError> {
        let paths = &self.lease.paths;
        let data = serde_json::json!({
            "schema_version": 1,
            "operation_id_sha256": digest(self.operation_id),
            "agent_id": self.agent_id,
            "birth_branch": self.birth_branch,
            "worktree_root": paths.worktree_root,
            "git_dir": paths.git_dir,
            "common_git_dir": paths.common_git_dir,
            "index_file": paths.index_file,
            "index_lock_file": paths.index_lock_file,
            "process_id": pid,
            "process_start_identity": start_identity,
            "process_group_id": process_group_id,
        });
        self.event_log
            .append("git.process.started", "git-process", &data)
            .map_err(|_| ProcessRunError::Audit)?;
        Ok(())
    }

    fn observe_lock(&self, pid: u32, start_identity: Option<u64>) -> Result<bool, ProcessRunError> {
        #[cfg(target_os = "linux")]
        {
            let lock_path = &self.lease.paths.index_lock_file;
            if !path_exists(lock_path)? {
                return Ok(false);
            }
            let Some(start_time_ticks) = start_identity else {
                return Err(ProcessRunError::Cleanup(
                    "managed Git process start identity is unavailable while its index lock exists",
                ));
            };
            let owner = ManagedProcessIdentity {
                pid,
                start_time_ticks,
            };
            try_record_managed_native_lock_proof_for_identity(self.lease, &owner, self.event_log)
                .map(|proof| proof.is_some())
                .map_err(|_| {
                    ProcessRunError::Cleanup(
                        "managed Git lock ownership could not be proved from process and inode evidence",
                    )
                })
        }
        #[cfg(not(target_os = "linux"))]
        {
            // Without procfs descriptor identity, keep Git's native lock protocol in control.
            // Automatic quarantine will refuse a lock for which no durable owner proof exists.
            let _ = (pid, start_identity);
            Ok(false)
        }
    }
}

pub(crate) async fn run_supervised(
    command: &mut Command,
    timeout: Option<Duration>,
    operation_id: &str,
    git_audit: Option<GitProcessAudit<'_>>,
) -> Result<ProcessRunOutput, ProcessRunError> {
    configure_command(command);
    let mut child = command
        .spawn()
        .map_err(|error| ProcessRunError::Spawn(error.kind()))?;
    let pid = child.id().ok_or(ProcessRunError::Cleanup(
        "spawned process has no process identity",
    ))?;
    let process_group_id = process_group_id(pid);
    let mut group_guard = ProcessGroupGuard::new(process_group_id);
    let start_identity = process_start_identity(pid);
    tracing::info!(
        process_operation_id = operation_id,
        child_pid = pid,
        child_start_identity = ?start_identity,
        process_group_id = ?group_id_for_record(pid),
        "Managed process started"
    );
    let stdout = spawn_reader(child.stdout.take())?;
    let stderr = spawn_reader(child.stderr.take())?;
    if let Some(audit) = &git_audit {
        if audit
            .record_started(pid, start_identity, group_id_for_record(pid))
            .is_err()
        {
            cancel_process_group(process_group_id, &mut child).await?;
            group_guard.disarm();
            let _ = tokio::join!(join_reader(stdout), join_reader(stderr));
            return Err(ProcessRunError::Audit);
        }
    }

    let status = match wait_for_process(
        &mut child,
        timeout,
        pid,
        start_identity,
        git_audit.as_ref(),
    )
    .await
    {
        Ok(status) => status,
        Err(error) => {
            cancel_process_group(process_group_id, &mut child).await?;
            group_guard.disarm();
            let _ = tokio::join!(join_reader(stdout), join_reader(stderr));
            return Err(error);
        }
    };

    cleanup_process_group(process_group_id, &mut child).await?;
    group_guard.disarm();
    Ok(ProcessRunOutput {
        status,
        stdout: join_reader(stdout).await?,
        stderr: join_reader(stderr).await?,
        pid,
        start_identity,
        process_group_id: group_id_for_record(pid),
    })
}

async fn wait_for_process(
    child: &mut Child,
    timeout: Option<Duration>,
    pid: u32,
    start_identity: Option<u64>,
    git_audit: Option<&GitProcessAudit<'_>>,
) -> Result<ExitStatus, ProcessRunError> {
    let started_at = tokio::time::Instant::now();
    let mut lock_proof_recorded = false;
    loop {
        if let Some(status) = child
            .try_wait()
            .map_err(|error| ProcessRunError::Wait(error.kind()))?
        {
            return Ok(status);
        }
        if !lock_proof_recorded {
            if let Some(audit) = git_audit {
                lock_proof_recorded = audit.observe_lock(pid, start_identity)?;
            }
        }
        if timeout.is_some_and(|budget| started_at.elapsed() >= budget) {
            return Err(ProcessRunError::Timeout);
        }
        tokio::time::sleep(GROUP_POLL).await;
    }
}

fn configure_command(command: &mut Command) {
    command
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .kill_on_drop(true);
    #[cfg(unix)]
    command.process_group(0);
}

fn spawn_reader(
    stream: Option<impl tokio::io::AsyncRead + Unpin + Send + 'static>,
) -> Result<tokio::task::JoinHandle<Result<Vec<u8>, io::ErrorKind>>, ProcessRunError> {
    let stream = stream.ok_or(ProcessRunError::Cleanup(
        "process output pipe is unavailable",
    ))?;
    Ok(tokio::spawn(async move {
        let mut stream = stream;
        let mut output = Vec::new();
        stream
            .read_to_end(&mut output)
            .await
            .map_err(|error| error.kind())
            .map(|_| output)
    }))
}

async fn join_reader(
    mut reader: tokio::task::JoinHandle<Result<Vec<u8>, io::ErrorKind>>,
) -> Result<Vec<u8>, ProcessRunError> {
    tokio::time::timeout(KILL_WAIT, &mut reader)
        .await
        .map_err(|_| {
            reader.abort();
            ProcessRunError::Cleanup("process output pipe remained open after group cleanup")
        })?
        .map_err(|_| ProcessRunError::Cleanup("output reader did not finish"))?
        .map_err(ProcessRunError::Output)
}

#[cfg(unix)]
type ProcessGroupId = nix::unistd::Pid;
#[cfg(not(unix))]
type ProcessGroupId = Option<u32>;

#[cfg(unix)]
fn process_group_id(pid: u32) -> ProcessGroupId {
    nix::unistd::Pid::from_raw(pid as i32)
}

#[cfg(unix)]
fn group_id_for_record(pid: u32) -> Option<u32> {
    Some(pid)
}

#[cfg(not(unix))]
fn process_group_id(_pid: u32) -> ProcessGroupId {
    None
}

#[cfg(not(unix))]
fn group_id_for_record(_pid: u32) -> Option<u32> {
    None
}

#[cfg(unix)]
struct ProcessGroupGuard(Option<ProcessGroupId>);

#[cfg(unix)]
impl ProcessGroupGuard {
    fn new(group: ProcessGroupId) -> Self {
        Self(Some(group))
    }

    fn disarm(&mut self) {
        self.0 = None;
    }
}

#[cfg(unix)]
impl Drop for ProcessGroupGuard {
    fn drop(&mut self) {
        if let Some(group) = self.0 {
            let _ = nix::sys::signal::killpg(group, Some(nix::sys::signal::Signal::SIGKILL));
        }
    }
}

#[cfg(not(unix))]
struct ProcessGroupGuard;

#[cfg(not(unix))]
impl ProcessGroupGuard {
    fn new(_group: ProcessGroupId) -> Self {
        Self
    }

    fn disarm(&mut self) {}
}

#[cfg(unix)]
async fn cancel_process_group(
    group: ProcessGroupId,
    child: &mut Child,
) -> Result<(), ProcessRunError> {
    stop_process_group(group, child).await
}

#[cfg(unix)]
async fn cleanup_process_group(
    group: ProcessGroupId,
    child: &mut Child,
) -> Result<(), ProcessRunError> {
    stop_process_group(group, child).await
}

#[cfg(unix)]
async fn stop_process_group(
    group: ProcessGroupId,
    child: &mut Child,
) -> Result<(), ProcessRunError> {
    signal_group(group, nix::sys::signal::Signal::SIGTERM)?;
    if wait_for_group_exit(group, child, TERM_GRACE).await? {
        return Ok(());
    }

    signal_group(group, nix::sys::signal::Signal::SIGKILL)?;
    if !wait_for_group_exit(group, child, KILL_WAIT).await? {
        return Err(ProcessRunError::Cleanup(
            "process group survived forced termination",
        ));
    }
    reap_child(child).await
}

#[cfg(not(unix))]
async fn cancel_process_group(
    _group: ProcessGroupId,
    child: &mut Child,
) -> Result<(), ProcessRunError> {
    child
        .start_kill()
        .map_err(|error| ProcessRunError::Wait(error.kind()))?;
    tokio::time::timeout(KILL_WAIT, child.wait())
        .await
        .map_err(|_| ProcessRunError::Cleanup("direct child could not be reaped"))?
        .map_err(|error| ProcessRunError::Wait(error.kind()))?;
    Err(ProcessRunError::Cleanup(
        "platform fallback terminated the direct child but cannot verify helper processes",
    ))
}

#[cfg(not(unix))]
async fn cleanup_process_group(
    _group: ProcessGroupId,
    _child: &mut Child,
) -> Result<(), ProcessRunError> {
    Ok(())
}

#[cfg(unix)]
fn signal_group(
    group: ProcessGroupId,
    signal: nix::sys::signal::Signal,
) -> Result<(), ProcessRunError> {
    match nix::sys::signal::killpg(group, Some(signal)) {
        Ok(()) => Ok(()),
        Err(nix::errno::Errno::ESRCH) => Ok(()),
        Err(nix::errno::Errno::EPERM) => Err(ProcessRunError::Cleanup(
            "permission denied while signaling process group",
        )),
        Err(_) => Err(ProcessRunError::Cleanup("could not signal process group")),
    }
}

#[cfg(unix)]
async fn wait_for_group_exit(
    group: ProcessGroupId,
    child: &mut Child,
    budget: Duration,
) -> Result<bool, ProcessRunError> {
    let deadline = tokio::time::Instant::now() + budget;
    loop {
        child
            .try_wait()
            .map_err(|error| ProcessRunError::Wait(error.kind()))?;
        if !process_group_exists(group)? {
            return Ok(true);
        }
        if tokio::time::Instant::now() >= deadline {
            return Ok(false);
        }
        tokio::time::sleep(GROUP_POLL).await;
    }
}

#[cfg(unix)]
fn process_group_exists(group: ProcessGroupId) -> Result<bool, ProcessRunError> {
    match nix::sys::signal::killpg(group, None::<nix::sys::signal::Signal>) {
        Ok(()) | Err(nix::errno::Errno::EPERM) => Ok(true),
        Err(nix::errno::Errno::ESRCH) => Ok(false),
        Err(_) => Err(ProcessRunError::Cleanup("could not inspect process group")),
    }
}

#[cfg(unix)]
async fn reap_child(child: &mut Child) -> Result<(), ProcessRunError> {
    tokio::time::timeout(KILL_WAIT, child.wait())
        .await
        .map_err(|_| ProcessRunError::Cleanup("managed child could not be reaped"))?
        .map(|_| ())
        .map_err(|error| ProcessRunError::Wait(error.kind()))
}

fn process_start_identity(pid: u32) -> Option<u64> {
    #[cfg(target_os = "linux")]
    {
        let stat = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
        let mut fields = stat.rsplit_once(')')?.1.split_whitespace();
        fields.nth(19)?.parse().ok()
    }
    #[cfg(not(target_os = "linux"))]
    {
        let _ = pid;
        None
    }
}

fn digest(value: &str) -> String {
    format!("{:x}", Sha256::digest(value.as_bytes()))
}

#[cfg(target_os = "linux")]
fn path_exists(path: &Path) -> Result<bool, ProcessRunError> {
    match std::fs::symlink_metadata(path) {
        Ok(_) => Ok(true),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(false),
        Err(_) => Err(ProcessRunError::Cleanup(
            "managed Git index lock could not be inspected",
        )),
    }
}
