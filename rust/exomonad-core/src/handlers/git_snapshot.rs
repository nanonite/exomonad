//! Lossless parsing and read-only observation of Git worktree entries.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub(crate) struct GitEntrySnapshot {
    entries: BTreeMap<Vec<u8>, GitPathEntry>,
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub(crate) struct GitPathEntry {
    pub(crate) head: Option<HeadEntry>,
    pub(crate) index: Vec<IndexEntry>,
    pub(crate) untracked: bool,
    pub(crate) gitlink_observation: Option<GitlinkObservation>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct HeadEntry {
    pub(crate) mode: u32,
    pub(crate) object_type: Vec<u8>,
    pub(crate) object_id: Vec<u8>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct IndexEntry {
    pub(crate) mode: u32,
    pub(crate) object_id: Vec<u8>,
    pub(crate) stage: u8,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum GitlinkObservation {
    Missing,
    Uninitialized,
    InitializedDirectory,
    InitializedFile,
    UnsupportedContents,
}

impl GitEntrySnapshot {
    pub(crate) fn parse(
        index_records: &[u8],
        head_records: &[u8],
        untracked_records: &[u8],
    ) -> Result<Self, String> {
        let mut snapshot = Self::default();
        for (path, entry) in parse_index(index_records)? {
            snapshot.entries.entry(path).or_default().index.push(entry);
        }
        for (path, entry) in parse_head(head_records)? {
            let destination = snapshot.entries.entry(path).or_default();
            if destination.head.replace(entry).is_some() {
                return Err("Git returned duplicate HEAD path evidence".to_string());
            }
        }
        for path in parse_paths(untracked_records)? {
            snapshot.entries.entry(path).or_default().untracked = true;
        }
        for entry in snapshot.entries.values_mut() {
            entry.index.sort_by_key(|index| index.stage);
            if entry
                .index
                .windows(2)
                .any(|pair| pair[0].stage == pair[1].stage)
            {
                return Err("Git returned duplicate index stage evidence".to_string());
            }
        }
        Ok(snapshot)
    }

    pub(crate) fn entries(&self) -> &BTreeMap<Vec<u8>, GitPathEntry> {
        &self.entries
    }

    pub(crate) fn with_paths_from(&self, earlier: &Self) -> Self {
        let mut merged = self.clone();
        for path in earlier.entries.keys() {
            merged.entries.entry(path.clone()).or_default();
        }
        merged
    }

    pub(crate) fn observe_references(&mut self, root: &Path) -> Result<(), String> {
        for (path, entry) in &mut self.entries {
            if entry.is_gitlink() {
                entry.gitlink_observation = Some(observe_gitlink(root, path)?);
            }
        }
        Ok(())
    }
}

impl GitPathEntry {
    pub(crate) fn is_gitlink(&self) -> bool {
        self.head
            .as_ref()
            .is_some_and(|entry| entry.mode == 0o160000)
            || self.index.iter().any(|entry| entry.mode == 0o160000)
    }
}

pub(crate) fn observe_gitlink(root: &Path, path: &[u8]) -> Result<GitlinkObservation, String> {
    let relative = path_from_bytes(path)?;
    if relative.is_absolute()
        || relative
            .components()
            .any(|part| !matches!(part, std::path::Component::Normal(_)))
    {
        return Err("Git reported an unsafe reference path".to_string());
    }
    let full_path = root.join(relative);
    let mut current = root.to_path_buf();
    let mut components = full_path
        .strip_prefix(root)
        .map_err(|_| "reference path is outside the assigned worktree".to_string())?
        .components()
        .peekable();
    while let Some(component) = components.next() {
        let std::path::Component::Normal(name) = component else {
            return Err("Git reported an unsafe reference path".to_string());
        };
        if components.peek().is_none() {
            break;
        }
        current.push(name);
        match fs::symlink_metadata(&current) {
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => break,
            Err(_) => return Err("reference parent could not be inspected safely".to_string()),
            Ok(metadata) if metadata.file_type().is_symlink() => {
                return Err("reference path has a symlinked parent".to_string());
            }
            Ok(metadata) if metadata.is_dir() => {}
            Ok(_) => return Err("reference parent is not a directory".to_string()),
        }
    }
    let metadata = match fs::symlink_metadata(&full_path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Ok(GitlinkObservation::Missing);
        }
        Err(_) => return Err("reference path could not be inspected safely".to_string()),
    };
    if !metadata.is_dir() || metadata.file_type().is_symlink() {
        return Ok(GitlinkObservation::UnsupportedContents);
    }
    let marker = match fs::symlink_metadata(full_path.join(".git")) {
        Ok(marker) => marker,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return empty_directory(&full_path).map(|empty| {
                if empty {
                    GitlinkObservation::Uninitialized
                } else {
                    GitlinkObservation::UnsupportedContents
                }
            });
        }
        Err(_) => return Err("reference metadata could not be inspected safely".to_string()),
    };
    if marker.file_type().is_symlink() {
        return Ok(GitlinkObservation::UnsupportedContents);
    }
    if marker.is_dir() {
        Ok(GitlinkObservation::InitializedDirectory)
    } else if marker.is_file() {
        Ok(GitlinkObservation::InitializedFile)
    } else {
        Ok(GitlinkObservation::UnsupportedContents)
    }
}

fn empty_directory(path: &Path) -> Result<bool, String> {
    let mut entries = fs::read_dir(path)
        .map_err(|_| "reference directory could not be inspected safely".to_string())?;
    Ok(entries.next().is_none())
}

fn parse_index(bytes: &[u8]) -> Result<Vec<(Vec<u8>, IndexEntry)>, String> {
    nul_records(bytes)?
        .into_iter()
        .map(|record| {
            let (metadata, path) = split_record(record)?;
            let mut fields = metadata.split(|byte| byte.is_ascii_whitespace());
            let mode = parse_mode(fields.next())?;
            let object_id = parse_object_id(fields.next())?;
            let stage = parse_stage(fields.next())?;
            if fields.next().is_some() || path.is_empty() {
                return Err(malformed_entries());
            }
            Ok((
                path.to_vec(),
                IndexEntry {
                    mode,
                    object_id,
                    stage,
                },
            ))
        })
        .collect()
}

fn parse_head(bytes: &[u8]) -> Result<Vec<(Vec<u8>, HeadEntry)>, String> {
    nul_records(bytes)?
        .into_iter()
        .map(|record| {
            let (metadata, path) = split_record(record)?;
            let mut fields = metadata.split(|byte| byte.is_ascii_whitespace());
            let mode = parse_mode(fields.next())?;
            let object_type = fields
                .next()
                .filter(|field| !field.is_empty() && field.iter().all(u8::is_ascii_alphabetic))
                .ok_or_else(malformed_entries)?
                .to_vec();
            let object_id = parse_object_id(fields.next())?;
            if fields.next().is_some() || path.is_empty() {
                return Err(malformed_entries());
            }
            Ok((
                path.to_vec(),
                HeadEntry {
                    mode,
                    object_type,
                    object_id,
                },
            ))
        })
        .collect()
}

fn parse_paths(bytes: &[u8]) -> Result<Vec<Vec<u8>>, String> {
    nul_records(bytes).map(|paths| paths.into_iter().map(<[u8]>::to_vec).collect())
}

fn split_record(record: &[u8]) -> Result<(&[u8], &[u8]), String> {
    let separator = record
        .iter()
        .position(|byte| *byte == b'\t')
        .ok_or_else(malformed_entries)?;
    Ok((&record[..separator], &record[separator + 1..]))
}

fn nul_records(bytes: &[u8]) -> Result<Vec<&[u8]>, String> {
    if bytes.is_empty() {
        return Ok(Vec::new());
    }
    if bytes.last() != Some(&0) {
        return Err("Git returned malformed NUL-delimited entry evidence".to_string());
    }
    let records = bytes[..bytes.len() - 1]
        .split(|byte| *byte == 0)
        .collect::<Vec<_>>();
    if records.iter().any(|record| record.is_empty()) {
        return Err("Git returned malformed NUL-delimited entry evidence".to_string());
    }
    Ok(records)
}

fn parse_mode(field: Option<&[u8]>) -> Result<u32, String> {
    let field = field.filter(|field| field.len() == 6 && field.iter().all(u8::is_ascii_digit));
    let field = field.ok_or_else(malformed_entries)?;
    let mode = field.iter().try_fold(0u32, |value, digit| {
        if !(b'0'..=b'7').contains(digit) {
            return None;
        }
        value.checked_mul(8)?.checked_add(u32::from(digit - b'0'))
    });
    mode.ok_or_else(malformed_entries)
}

fn parse_object_id(field: Option<&[u8]>) -> Result<Vec<u8>, String> {
    let field = field.filter(|field| {
        field.len() >= 40 && field.len() % 2 == 0 && field.iter().all(u8::is_ascii_hexdigit)
    });
    field.map(<[u8]>::to_vec).ok_or_else(malformed_entries)
}

fn parse_stage(field: Option<&[u8]>) -> Result<u8, String> {
    let field = field.filter(|field| field.len() == 1 && (b'0'..=b'3').contains(&field[0]));
    field
        .map(|field| field[0] - b'0')
        .ok_or_else(malformed_entries)
}

fn path_from_bytes(path: &[u8]) -> Result<PathBuf, String> {
    #[cfg(unix)]
    {
        use std::os::unix::ffi::OsStringExt;
        Ok(PathBuf::from(std::ffi::OsString::from_vec(path.to_vec())))
    }
    #[cfg(not(unix))]
    {
        String::from_utf8(path.to_vec())
            .map(PathBuf::from)
            .map_err(|_| "reference path cannot be represented on this platform".to_string())
    }
}

fn malformed_entries() -> String {
    "Git returned malformed typed entry evidence".to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_head_index_conflict_stages_and_non_utf8_paths_losslessly() {
        let oid = b"0123456789abcdef0123456789abcdef01234567";
        let mut index = Vec::new();
        for stage in 1..=3 {
            index.extend_from_slice(b"100644 ");
            index.extend_from_slice(oid);
            index.extend_from_slice(format!(" {stage}\tbad-").as_bytes());
            index.push(0xff);
            index.extend_from_slice(b"-name\0");
        }
        let mut head = b"160000 commit ".to_vec();
        head.extend_from_slice(oid);
        head.extend_from_slice(b"\ttools/reference\0");
        let snapshot = GitEntrySnapshot::parse(&index, &head, b"new-\xff-file\0")
            .expect("valid Git output parses");

        let conflicted = snapshot.entries().get(b"bad-\xff-name".as_slice()).unwrap();
        assert_eq!(
            conflicted
                .index
                .iter()
                .map(|entry| entry.stage)
                .collect::<Vec<_>>(),
            [1, 2, 3]
        );
        assert_eq!(conflicted.index[0].object_id, oid);
        let reference = snapshot
            .entries()
            .get(b"tools/reference".as_slice())
            .unwrap();
        assert!(reference.is_gitlink());
        assert_eq!(reference.head.as_ref().unwrap().mode, 0o160000);
        assert_eq!(reference.head.as_ref().unwrap().object_id, oid);
        assert!(snapshot.entries().contains_key(b"new-\xff-file".as_slice()));
    }

    #[test]
    fn accepts_sha256_object_ids_without_sha1_length_assumptions() {
        let oid = b"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
        let mut head = b"100644 blob ".to_vec();
        head.extend_from_slice(oid);
        head.extend_from_slice(b"\tfile\0");
        let snapshot = GitEntrySnapshot::parse(&[], &head, &[]).unwrap();
        assert_eq!(
            snapshot.entries()[b"file".as_slice()]
                .head
                .as_ref()
                .unwrap()
                .object_id,
            oid
        );
    }

    #[test]
    fn rejects_malformed_or_duplicate_git_records() {
        assert!(GitEntrySnapshot::parse(b"100644 invalid 0\tfile\0", &[], &[]).is_err());
        assert!(GitEntrySnapshot::parse(&[], b"100644 blob abc\tfile\0", &[]).is_err());
        assert!(GitEntrySnapshot::parse(
            &[],
            b"100644 blob 0123456789abcdef0123456789abcdef01234567\tfile\0\
              100644 blob 0123456789abcdef0123456789abcdef01234567\tfile\0",
            &[],
        )
        .is_err());
        assert!(GitEntrySnapshot::parse(&[], &[], b"path\0\0").is_err());
    }

    #[test]
    fn observes_empty_and_initialized_reference_directories_without_mutating_them() {
        use std::process::Command;

        let root = tempfile::tempdir().unwrap();
        let reference = root.path().join("tools/reference");
        fs::create_dir_all(&reference).unwrap();
        assert_eq!(
            observe_gitlink(root.path(), b"tools/reference").unwrap(),
            GitlinkObservation::Uninitialized
        );
        let initialized = Command::new("git")
            .args(["init", "-q"])
            .current_dir(&reference)
            .status()
            .unwrap();
        assert!(initialized.success());
        assert_eq!(
            observe_gitlink(root.path(), b"tools/reference").unwrap(),
            GitlinkObservation::InitializedDirectory
        );
        assert_eq!(
            fs::read_dir(reference).unwrap().count(),
            1,
            "observation must not initialize or otherwise mutate a reference"
        );
    }
}
