use serde::{Deserialize, Serialize};
use std::fs::{self, File, OpenOptions};
use std::io;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tracing::{debug, warn};

/// Metadata written into lock files for stale detection.
#[derive(Debug, Serialize, Deserialize)]
struct LockMetadata {
    pid: u32,
    created_at: String,
    ttl_seconds: u64,
}

/// Advisory file lock using complete temporary metadata and atomic publication.
///
/// Provides mutual exclusion for read-modify-write operations on shared JSON files.
/// Stale locks (mtime > TTL or dead PID) are automatically broken.
/// Implements `Drop` for best-effort cleanup.
pub struct FileLock {
    path: PathBuf,
    released: bool,
}

impl FileLock {
    /// Acquire a file lock for the given path.
    ///
    /// Lock file is created at `{path}.lock`. Retries with jitter on contention,
    /// times out after ~5 seconds.
    pub fn acquire(path: &Path, ttl: Duration) -> io::Result<Self> {
        let lock_path = lock_path_for(path);
        let timeout = Duration::from_secs(5);
        let start = Instant::now();
        let mut attempt = 0u32;

        reclaim_orphaned_lock_temps(&lock_path)?;

        loop {
            match try_create_lock(&lock_path, ttl) {
                Ok(()) => {
                    debug!(lock = %lock_path.display(), "File lock acquired");
                    return Ok(FileLock {
                        path: lock_path,
                        released: false,
                    });
                }
                Err(e) if e.kind() == io::ErrorKind::AlreadyExists => {
                    if try_break_stale(&lock_path, ttl)? {
                        debug!(lock = %lock_path.display(), "Broke stale lock, retrying");
                        continue;
                    }

                    if start.elapsed() >= timeout {
                        return Err(io::Error::new(
                            io::ErrorKind::TimedOut,
                            format!(
                                "Failed to acquire lock {} after {:?}",
                                lock_path.display(),
                                timeout
                            ),
                        ));
                    }

                    // Backoff with jitter: base 10ms * 2^attempt, capped at 200ms, plus jitter
                    let base_ms = 10u64.saturating_mul(1u64 << attempt.min(4));
                    let jitter_ms =
                        (std::process::id() as u64 + attempt as u64 * 7) % (base_ms / 2 + 1);
                    let sleep_ms = base_ms + jitter_ms;
                    std::thread::sleep(Duration::from_millis(sleep_ms.min(200)));
                    attempt += 1;
                }
                Err(e) => return Err(e),
            }
        }
    }

    /// Explicitly release the lock.
    pub fn release(mut self) -> io::Result<()> {
        fs::remove_file(&self.path)?;
        self.released = true;
        Ok(())
    }
}

impl Drop for FileLock {
    fn drop(&mut self) {
        if !self.released {
            if let Err(e) = fs::remove_file(&self.path) {
                if e.kind() != io::ErrorKind::NotFound {
                    warn!(lock = %self.path.display(), error = %e, "Failed to clean up lock file in Drop");
                }
            }
        }
    }
}

/// Compute lock path: `{target}.lock`
pub fn lock_path_for(path: &Path) -> PathBuf {
    let mut lock = path.as_os_str().to_owned();
    lock.push(".lock");
    PathBuf::from(lock)
}

/// `fsync` on a directory to ensure metadata (renames) are durable.
pub fn fsync_dir(dir: &Path) -> io::Result<()> {
    let d = File::open(dir)?;
    d.sync_all()
}

/// Publish complete lock metadata through an atomic hard-link claim.
fn try_create_lock(lock_path: &Path, ttl: Duration) -> io::Result<()> {
    let metadata = LockMetadata {
        pid: std::process::id(),
        created_at: chrono::Utc::now().to_rfc3339(),
        ttl_seconds: ttl.as_secs(),
    };
    let suffix = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    let temporary_lock_path = lock_path.with_file_name(format!(
        ".{}.{}.{}.tmp",
        lock_path
            .file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("lock"),
        std::process::id(),
        suffix
    ));

    let file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&temporary_lock_path)?;

    let result = (|| {
        serde_json::to_writer(&file, &metadata).map_err(io::Error::other)?;
        file.sync_all()?;

        // Claim the canonical path only after the metadata is complete. A
        // contender therefore sees either no lock or a readable lock, never
        // the empty intermediate file that would otherwise be mistaken for a
        // stale lock and deleted while its owner is writing.
        fs::hard_link(&temporary_lock_path, lock_path)?;
        debug!(lock = %lock_path.display(), "Lock published atomically");
        #[cfg(test)]
        wait_for_test_publication(lock_path);
        Ok(())
    })();

    let _ = fs::remove_file(&temporary_lock_path);
    result
}

/// Reclaim temporary lock publications left by processes that are no longer alive.
fn reclaim_orphaned_lock_temps(lock_path: &Path) -> io::Result<()> {
    let Some(parent) = lock_path.parent() else {
        return Ok(());
    };
    let Some(lock_name) = lock_path.file_name().and_then(|name| name.to_str()) else {
        return Ok(());
    };
    let prefix = format!(".{}.", lock_name);
    let entries = match fs::read_dir(parent) {
        Ok(entries) => entries,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error),
    };

    for entry in entries {
        let entry = entry?;
        if !entry.file_type()?.is_file() {
            continue;
        }
        let Some(name) = entry.file_name().to_str().map(str::to_owned) else {
            continue;
        };
        let Some(pid_text) = name
            .strip_prefix(&prefix)
            .and_then(|suffix| suffix.strip_suffix(".tmp"))
            .and_then(|suffix| suffix.split('.').next())
        else {
            continue;
        };
        let Ok(pid) = pid_text.parse::<u32>() else {
            continue;
        };
        if is_pid_alive(pid) {
            continue;
        }

        match fs::remove_file(entry.path()) {
            Ok(()) => debug!(path = %entry.path().display(), pid, "Reclaimed orphaned lock temp"),
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(error),
        }
    }
    Ok(())
}

/// Check if an existing lock is stale (mtime > TTL or PID dead). Break if so.
fn try_break_stale(lock_path: &Path, ttl: Duration) -> io::Result<bool> {
    let meta = match fs::metadata(lock_path) {
        Ok(m) => m,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(true),
        Err(e) => return Err(e),
    };

    // Check mtime-based staleness
    let mtime_stale = meta
        .modified()
        .ok()
        .and_then(|mtime| mtime.elapsed().ok())
        .map(|age| age > ttl)
        .unwrap_or(false);

    // Check PID-based staleness
    let (owner_pid, pid_stale) = match fs::read_to_string(lock_path) {
        Ok(content) => {
            if let Ok(lock_meta) = serde_json::from_str::<LockMetadata>(&content) {
                let owner_pid = lock_meta.pid;
                (Some(owner_pid), !is_pid_alive(owner_pid))
            } else {
                // Corrupt lock file — treat as stale
                (None, true)
            }
        }
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(true),
        Err(_) => (None, false),
    };

    if mtime_stale || pid_stale {
        warn!(
            lock = %lock_path.display(),
            owner_pid = ?owner_pid,
            mtime_stale,
            pid_stale,
            "Breaking stale lock"
        );
        match fs::remove_file(lock_path) {
            Ok(()) => Ok(true),
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(true),
            Err(e) => Err(e),
        }
    } else {
        Ok(false)
    }
}

#[cfg(test)]
struct TestPublicationGate {
    published: std::sync::Barrier,
    release: std::sync::Barrier,
}

#[cfg(test)]
type TestPublicationGateState = Option<(PathBuf, std::sync::Arc<TestPublicationGate>)>;

#[cfg(test)]
fn test_publication_gate() -> &'static std::sync::Mutex<TestPublicationGateState> {
    static GATE: std::sync::OnceLock<std::sync::Mutex<TestPublicationGateState>> =
        std::sync::OnceLock::new();
    GATE.get_or_init(|| std::sync::Mutex::new(None))
}

#[cfg(test)]
fn install_test_publication_gate(lock_path: &Path, gate: std::sync::Arc<TestPublicationGate>) {
    *test_publication_gate().lock().unwrap() = Some((lock_path.to_path_buf(), gate));
}

#[cfg(test)]
fn clear_test_publication_gate() {
    *test_publication_gate().lock().unwrap() = None;
}

#[cfg(test)]
fn wait_for_test_publication(lock_path: &Path) {
    let gate = test_publication_gate()
        .lock()
        .unwrap()
        .as_ref()
        .filter(|(gated_path, _)| gated_path == lock_path)
        .map(|(_, gate)| std::sync::Arc::clone(gate));
    if let Some(gate) = gate {
        gate.published.wait();
        gate.release.wait();
    }
}

/// Check if a process is alive via `/proc/{pid}` (Linux) or `kill(pid, 0)` fallback.
fn is_pid_alive(pid: u32) -> bool {
    // Fast path: check /proc/{pid} exists (Linux)
    let proc_path = format!("/proc/{}", pid);
    if Path::new(&proc_path).exists() {
        return true;
    }

    // Fallback: signal 0 check
    #[cfg(unix)]
    {
        let self_pid = std::process::id();
        // SAFETY: getppid is a simple libc call returning the parent PID
        let parent_pid = unsafe { libc::getppid() as u32 };
        if pid == self_pid || pid == parent_pid {
            return true;
        }
        // SAFETY: signal 0 doesn't actually send a signal, just checks existence.
        // ESRCH = no such process (dead), EPERM = process exists but no permission (alive).
        let res = unsafe { libc::kill(pid as i32, 0) };
        if res == 0 {
            return true;
        }
        // Check errno for ESRCH (dead) vs EPERM (alive but no permission)
        let err = io::Error::last_os_error();
        if let Some(code) = err.raw_os_error() {
            if code == libc::ESRCH {
                return false;
            }
        }
        // EPERM or any other error: conservatively treat as alive
        true
    }

    #[cfg(not(unix))]
    false
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    #[test]
    fn test_acquire_and_release() {
        let dir = tempdir().unwrap();
        let target = dir.path().join("data.json");
        fs::write(&target, "[]").unwrap();

        let lock = FileLock::acquire(&target, Duration::from_secs(30)).unwrap();
        let lock_file = lock_path_for(&target);
        assert!(lock_file.exists());

        lock.release().unwrap();
        assert!(!lock_file.exists());
        let temporary_files = fs::read_dir(dir.path())
            .unwrap()
            .filter_map(Result::ok)
            .filter(|entry| {
                entry
                    .file_name()
                    .to_string_lossy()
                    .starts_with(".data.json.lock.")
            })
            .collect::<Vec<_>>();
        assert!(
            temporary_files.is_empty(),
            "temporary locks remain: {temporary_files:?}"
        );
    }

    #[test]
    fn test_lock_publication_never_exposes_breakable_canonical_path() {
        let dir = tempdir().unwrap();
        let target = dir.path().join("data.json");
        fs::write(&target, "[]").unwrap();
        let lock_path = lock_path_for(&target);
        let gate = std::sync::Arc::new(TestPublicationGate {
            published: std::sync::Barrier::new(2),
            release: std::sync::Barrier::new(2),
        });
        install_test_publication_gate(&lock_path, std::sync::Arc::clone(&gate));

        let holder_path = lock_path.clone();
        let holder =
            std::thread::spawn(move || try_create_lock(&holder_path, Duration::from_secs(30)));
        gate.published.wait();
        let observed_breakable = try_break_stale(&lock_path, Duration::from_secs(30));
        gate.release.wait();
        let holder_result = holder.join().unwrap();
        clear_test_publication_gate();

        assert!(!observed_breakable.unwrap());
        assert!(holder_result.is_ok());
        fs::remove_file(lock_path).unwrap();
    }

    #[test]
    fn test_reclaims_dead_lock_temp_but_preserves_live_owner() {
        let dir = tempdir().unwrap();
        let target = dir.path().join("data.json");
        fs::write(&target, "[]").unwrap();
        let lock_path = lock_path_for(&target);
        let lock_name = lock_path.file_name().unwrap().to_string_lossy();
        let dead_temp = dir.path().join(format!(".{lock_name}.999999999.1.tmp"));
        let live_temp = dir
            .path()
            .join(format!(".{lock_name}.{}.2.tmp", std::process::id()));
        fs::write(&dead_temp, "orphaned").unwrap();
        fs::write(&live_temp, "owned").unwrap();

        let lock = FileLock::acquire(&target, Duration::from_secs(30)).unwrap();
        assert!(!dead_temp.exists());
        assert!(live_temp.exists());
        lock.release().unwrap();
        assert!(live_temp.exists());
    }

    #[test]
    fn test_drop_cleans_up() {
        let dir = tempdir().unwrap();
        let target = dir.path().join("data.json");
        fs::write(&target, "[]").unwrap();

        let lock_file = lock_path_for(&target);
        {
            let _lock = FileLock::acquire(&target, Duration::from_secs(30)).unwrap();
            assert!(lock_file.exists());
        }
        // Drop should have cleaned up
        assert!(!lock_file.exists());
    }

    #[test]
    fn test_stale_lock_broken_by_mtime() {
        let dir = tempdir().unwrap();
        let target = dir.path().join("data.json");
        fs::write(&target, "[]").unwrap();

        // Create a lock file with TTL of 0 (immediately stale by mtime)
        let lock_file = lock_path_for(&target);
        let meta = LockMetadata {
            pid: std::process::id(),
            created_at: "2020-01-01T00:00:00Z".to_string(),
            ttl_seconds: 0,
        };
        fs::write(&lock_file, serde_json::to_string(&meta).unwrap()).unwrap();

        // Should be able to acquire despite existing lock (stale by TTL=0)
        // Give it a moment so mtime is in the past
        std::thread::sleep(Duration::from_millis(10));
        let lock = FileLock::acquire(&target, Duration::from_millis(1)).unwrap();
        lock.release().unwrap();
    }

    #[test]
    fn test_stale_lock_broken_by_dead_pid() {
        let dir = tempdir().unwrap();
        let target = dir.path().join("data.json");
        fs::write(&target, "[]").unwrap();

        // Create a lock file with a PID that doesn't exist
        let lock_file = lock_path_for(&target);
        let meta = LockMetadata {
            pid: 999_999_999, // Almost certainly not a real PID
            created_at: chrono::Utc::now().to_rfc3339(),
            ttl_seconds: 3600,
        };
        fs::write(&lock_file, serde_json::to_string(&meta).unwrap()).unwrap();

        let lock = FileLock::acquire(&target, Duration::from_secs(30)).unwrap();
        lock.release().unwrap();
    }

    #[test]
    fn test_concurrent_acquire_both_succeed_sequentially() {
        use std::sync::{Arc, Barrier};

        let dir = tempdir().unwrap();
        let target = dir.path().join("data.json");
        fs::write(&target, "[]").unwrap();

        let barrier = Arc::new(Barrier::new(2));
        let target_clone = target.clone();
        let barrier_clone = barrier.clone();

        let handle = std::thread::spawn(move || {
            barrier_clone.wait();
            let lock = FileLock::acquire(&target_clone, Duration::from_secs(30))?;
            // Hold lock briefly then drop (auto-release)
            std::thread::sleep(Duration::from_millis(50));
            drop(lock);
            Ok::<(), io::Error>(())
        });

        barrier.wait();
        let lock = FileLock::acquire(&target, Duration::from_secs(30)).unwrap();
        std::thread::sleep(Duration::from_millis(50));
        drop(lock);

        // Both threads should have acquired and released successfully
        handle.join().unwrap().unwrap();
    }

    #[test]
    fn test_lock_path_convention() {
        let target = Path::new("/home/user/.claude/teams/t/inboxes/lead.json");
        let expected = Path::new("/home/user/.claude/teams/t/inboxes/lead.json.lock");
        assert_eq!(lock_path_for(target), expected);
    }

    #[test]
    fn test_fsync_dir() {
        let dir = tempdir().unwrap();
        // Should succeed on a valid directory
        assert!(fsync_dir(dir.path()).is_ok());
    }

    #[test]
    fn test_corrupt_lock_file_treated_as_stale() {
        let dir = tempdir().unwrap();
        let target = dir.path().join("data.json");
        fs::write(&target, "[]").unwrap();

        // Write garbage to lock file
        let lock_file = lock_path_for(&target);
        fs::write(&lock_file, "not json").unwrap();

        // Should break the corrupt lock and acquire
        let lock = FileLock::acquire(&target, Duration::from_secs(30)).unwrap();
        lock.release().unwrap();
    }
}
