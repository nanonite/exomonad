//! Filesystem service for WASM host functions.
//!
//! Provides file read/write operations for hooks and MCP tools that need
//! to access file contents (e.g., reading transcript files, writing context).

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::path::{Component, Path, PathBuf};
use tokio::fs;

// ============================================================================
// Service
// ============================================================================

/// Input for reading a file.
#[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
pub struct ReadFileInput {
    /// Path to the file (absolute or relative to project_dir)
    pub path: String,
    /// Maximum bytes to read (0 = unlimited)
    #[serde(default)]
    pub max_bytes: usize,
}

/// Result of reading a file.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ReadFileOutput {
    /// File contents (UTF-8)
    pub content: String,
    /// Number of bytes read
    pub bytes_read: usize,
    /// Whether the file was truncated
    pub truncated: bool,
}

/// Input for writing a file.
#[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
pub struct WriteFileInput {
    /// Path to the file (absolute or relative to project_dir)
    pub path: String,
    /// Content to write
    pub content: String,
    /// Whether to create parent directories
    #[serde(default = "default_true")]
    pub create_parents: bool,
}

fn default_true() -> bool {
    true
}

/// Result of writing a file.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct WriteFileOutput {
    /// Number of bytes written
    pub bytes_written: usize,
    /// Absolute path of the written file
    pub path: String,
}

// ============================================================================
// Service
// ============================================================================

/// Filesystem service for file operations.
pub struct FileSystemService {
    /// Project root directory (for resolving relative paths)
    project_dir: PathBuf,
}

/// Resolve `.` and `..` components without touching the filesystem.
///
/// The containment check must not depend on whether the target exists, so
/// normalization is purely lexical. `..` at or above the root is clamped, matching
/// POSIX behavior where `/..` is `/`.
fn normalize_lexically(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for component in path.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                if !matches!(
                    out.components().next_back(),
                    None | Some(Component::RootDir)
                ) {
                    out.pop();
                }
            }
            other => out.push(other.as_os_str()),
        }
    }
    out
}

impl FileSystemService {
    /// Create a new filesystem service.
    pub fn new(project_dir: PathBuf) -> Self {
        Self { project_dir }
    }

    /// Resolve a caller-supplied path against the project root.
    ///
    /// Containment is decided lexically so it holds for paths that do not yet
    /// exist. Existing paths are additionally re-checked after symlink
    /// resolution. Both checks fail closed.
    fn resolve_path(&self, path: &str) -> Result<PathBuf> {
        let canonical_root = self.project_dir.canonicalize().with_context(|| {
            format!(
                "project root does not resolve: {}",
                self.project_dir.display()
            )
        })?;

        let requested = PathBuf::from(path);
        let joined = if requested.is_absolute() {
            requested
        } else {
            canonical_root.join(requested)
        };

        let normalized = normalize_lexically(&joined);
        if !normalized.starts_with(&canonical_root) {
            anyhow::bail!(
                "Path traversal denied: '{}' escapes project root '{}'",
                path,
                canonical_root.display()
            );
        }

        // Secondary check: an existing path (or existing parent) may be a symlink
        // pointing out of the root. Only applies when the target is materialized.
        let symlink_target = if normalized.exists() {
            Some(
                normalized
                    .canonicalize()
                    .with_context(|| format!("failed to resolve path: {}", normalized.display()))?,
            )
        } else {
            match normalized.parent() {
                Some(parent) if parent.exists() => {
                    let file_name = normalized.file_name().with_context(|| {
                        format!("failed to determine file name: {}", normalized.display())
                    })?;
                    Some(
                        parent
                            .canonicalize()
                            .with_context(|| {
                                format!("failed to resolve parent: {}", parent.display())
                            })?
                            .join(file_name),
                    )
                }
                _ => None,
            }
        };

        if let Some(resolved) = symlink_target {
            if !resolved.starts_with(&canonical_root) {
                anyhow::bail!(
                    "Path traversal denied: '{}' resolves through a symlink to '{}', outside project root '{}'",
                    path,
                    resolved.display(),
                    canonical_root.display()
                );
            }
        }

        Ok(normalized)
    }

    /// Read a file.
    #[tracing::instrument(skip(self))]
    pub async fn read_file(&self, input: &ReadFileInput) -> Result<ReadFileOutput> {
        let path = self.resolve_path(&input.path)?;

        let content = fs::read_to_string(&path)
            .await
            .with_context(|| format!("Failed to read file: {}", path.display()))?;

        let bytes_read = content.len();
        let (content, truncated) = if input.max_bytes > 0 && bytes_read > input.max_bytes {
            let truncated_content: String = content.chars().take(input.max_bytes).collect();
            (truncated_content, true)
        } else {
            (content, false)
        };

        Ok(ReadFileOutput {
            content,
            bytes_read,
            truncated,
        })
    }

    /// Write a file.
    #[tracing::instrument(skip(self))]
    pub async fn write_file(&self, input: &WriteFileInput) -> Result<WriteFileOutput> {
        let path = self.resolve_path(&input.path)?;

        if input.create_parents {
            if let Some(parent) = path.parent() {
                fs::create_dir_all(parent).await.with_context(|| {
                    format!(
                        "Failed to create parent directories for: {}",
                        path.display()
                    )
                })?;
            }
        }

        let bytes_written = input.content.len();
        fs::write(&path, &input.content)
            .await
            .with_context(|| format!("Failed to write file: {}", path.display()))?;

        Ok(WriteFileOutput {
            bytes_written,
            path: path.to_string_lossy().to_string(),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    #[tokio::test]
    async fn test_read_write_file() {
        let dir = tempdir().unwrap();
        let service = FileSystemService::new(dir.path().to_path_buf());

        // Write a file
        let write_input = WriteFileInput {
            path: "test.txt".to_string(),
            content: "Hello, World!".to_string(),
            create_parents: true,
        };
        let write_result = service.write_file(&write_input).await.unwrap();
        assert_eq!(write_result.bytes_written, 13);

        // Read it back
        let read_input = ReadFileInput {
            path: "test.txt".to_string(),
            max_bytes: 0,
        };
        let read_result = service.read_file(&read_input).await.unwrap();
        assert_eq!(read_result.content, "Hello, World!");
        assert!(!read_result.truncated);
    }

    #[tokio::test]
    async fn test_read_with_truncation() {
        let dir = tempdir().unwrap();
        let service = FileSystemService::new(dir.path().to_path_buf());

        // Write a file
        let write_input = WriteFileInput {
            path: "long.txt".to_string(),
            content: "Hello, World! This is a longer message.".to_string(),
            create_parents: true,
        };
        service.write_file(&write_input).await.unwrap();

        // Read with truncation
        let read_input = ReadFileInput {
            path: "long.txt".to_string(),
            max_bytes: 5,
        };
        let read_result = service.read_file(&read_input).await.unwrap();
        assert_eq!(read_result.content, "Hello");
        assert!(read_result.truncated);
    }

    #[tokio::test]
    async fn test_path_traversal_rejected() {
        let dir = tempdir().unwrap();
        let service = FileSystemService::new(dir.path().to_path_buf());

        let input = ReadFileInput {
            path: "../../../etc/passwd".to_string(),
            max_bytes: 0,
        };
        let result = service.read_file(&input).await;
        assert!(result.is_err());
        let err = result.unwrap_err().to_string();
        assert!(err.contains("Path traversal denied"), "got: {err}");
    }

    #[tokio::test]
    async fn test_path_traversal_absolute_rejected() {
        let dir = tempdir().unwrap();
        let service = FileSystemService::new(dir.path().to_path_buf());

        let input = ReadFileInput {
            path: "/etc/passwd".to_string(),
            max_bytes: 0,
        };
        let result = service.read_file(&input).await;
        assert!(result.is_err());
        let err = result.unwrap_err().to_string();
        assert!(err.contains("Path traversal denied"), "got: {err}");
    }

    #[tokio::test]
    async fn test_create_parent_directories() {
        let dir = tempdir().unwrap();
        let service = FileSystemService::new(dir.path().to_path_buf());

        // Write to nested path
        let write_input = WriteFileInput {
            path: "a/b/c/test.txt".to_string(),
            content: "nested".to_string(),
            create_parents: true,
        };
        let result = service.write_file(&write_input).await.unwrap();
        assert!(result.path.ends_with("a/b/c/test.txt"));

        // Verify file exists
        let read_input = ReadFileInput {
            path: "a/b/c/test.txt".to_string(),
            max_bytes: 0,
        };
        let read_result = service.read_file(&read_input).await.unwrap();
        assert_eq!(read_result.content, "nested");
    }

    /// Traversal is denied when the escape target does not exist.
    #[tokio::test]
    async fn test_path_traversal_rejected_when_target_absent() {
        let dir = tempdir().unwrap();
        let root = dir.path().join("root");
        std::fs::create_dir_all(&root).unwrap();
        let service = FileSystemService::new(root);

        let input = ReadFileInput {
            path: "../escape/secret.txt".to_string(),
            max_bytes: 0,
        };
        let err = service.read_file(&input).await.unwrap_err().to_string();
        assert!(err.contains("Path traversal denied"), "got: {err}");
    }

    /// Parent creation cannot materialize a directory outside the project root.
    #[tokio::test]
    async fn test_write_with_create_parents_cannot_escape_root() {
        let dir = tempdir().unwrap();
        let root = dir.path().join("root");
        std::fs::create_dir_all(&root).unwrap();
        let escape = dir.path().join("escape");
        let service = FileSystemService::new(root);

        let input = WriteFileInput {
            path: "../escape/deep/pwned.txt".to_string(),
            content: "pwned".to_string(),
            create_parents: true,
        };
        let err = service.write_file(&input).await.unwrap_err().to_string();
        assert!(err.contains("Path traversal denied"), "got: {err}");
        assert!(!escape.exists(), "escape directory must not be created");
    }

    /// A symlink inside the root pointing outside is denied.
    #[cfg(unix)]
    #[tokio::test]
    async fn test_symlink_escape_rejected() {
        let dir = tempdir().unwrap();
        let root = dir.path().join("root");
        let outside = dir.path().join("outside");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::create_dir_all(&outside).unwrap();
        std::fs::write(outside.join("secret.txt"), "secret").unwrap();
        std::os::unix::fs::symlink(&outside, root.join("link")).unwrap();

        let service = FileSystemService::new(root);
        let input = ReadFileInput {
            path: "link/secret.txt".to_string(),
            max_bytes: 0,
        };
        let err = service.read_file(&input).await.unwrap_err().to_string();
        assert!(err.contains("Path traversal denied"), "got: {err}");
    }

    /// Interior `..` components that stay inside the root remain valid.
    #[tokio::test]
    async fn test_interior_parent_components_allowed() {
        let dir = tempdir().unwrap();
        let service = FileSystemService::new(dir.path().to_path_buf());
        std::fs::create_dir_all(dir.path().join("a")).unwrap();
        std::fs::write(dir.path().join("b.txt"), "hello").unwrap();

        let input = ReadFileInput {
            path: "a/../b.txt".to_string(),
            max_bytes: 0,
        };
        let out = service.read_file(&input).await.unwrap();
        assert_eq!(out.content, "hello");
    }

    /// Parent creation still works for legitimate nested paths.
    #[tokio::test]
    async fn test_write_create_parents_inside_root_still_allowed() {
        let dir = tempdir().unwrap();
        let service = FileSystemService::new(dir.path().to_path_buf());

        let input = WriteFileInput {
            path: "nested/deep/file.txt".to_string(),
            content: "ok".to_string(),
            create_parents: true,
        };
        service.write_file(&input).await.unwrap();
        assert_eq!(
            std::fs::read_to_string(dir.path().join("nested/deep/file.txt")).unwrap(),
            "ok"
        );
    }
}
