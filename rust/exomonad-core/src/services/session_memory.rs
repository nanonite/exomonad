use anyhow::{Context, Result};
use rusqlite::{params_from_iter, types::Value, Connection, OptionalExtension};
use std::fmt;
use std::path::{Path, PathBuf};
use std::str::FromStr;
use std::sync::{Mutex, MutexGuard};
use std::time::{SystemTime, UNIX_EPOCH};

const DEFAULT_LIST_LIMIT: usize = 100;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MemoryKind {
    Unspecified,
    OriginalPlan,
    WavePlan,
    SpawnedChild,
    ChildHandoff,
    Blocker,
    Decision,
    ReviewFeedback,
    FixDirection,
    MergeResult,
    CiResult,
    NextAction,
    HumanClarification,
    SessionSummary,
}

impl Default for MemoryKind {
    fn default() -> Self {
        Self::Unspecified
    }
}

impl MemoryKind {
    const ALL: [Self; 14] = [
        Self::Unspecified,
        Self::OriginalPlan,
        Self::WavePlan,
        Self::SpawnedChild,
        Self::ChildHandoff,
        Self::Blocker,
        Self::Decision,
        Self::ReviewFeedback,
        Self::FixDirection,
        Self::MergeResult,
        Self::CiResult,
        Self::NextAction,
        Self::HumanClarification,
        Self::SessionSummary,
    ];

    fn as_str(self) -> &'static str {
        match self {
            Self::Unspecified => "unspecified",
            Self::OriginalPlan => "original_plan",
            Self::WavePlan => "wave_plan",
            Self::SpawnedChild => "spawned_child",
            Self::ChildHandoff => "child_handoff",
            Self::Blocker => "blocker",
            Self::Decision => "decision",
            Self::ReviewFeedback => "review_feedback",
            Self::FixDirection => "fix_direction",
            Self::MergeResult => "merge_result",
            Self::CiResult => "ci_result",
            Self::NextAction => "next_action",
            Self::HumanClarification => "human_clarification",
            Self::SessionSummary => "session_summary",
        }
    }
}

impl fmt::Display for MemoryKind {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.as_str())
    }
}

impl FromStr for MemoryKind {
    type Err = String;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        Self::ALL
            .into_iter()
            .find(|kind| kind.as_str() == value)
            .ok_or_else(|| format!("unrecognized session memory kind: {value}"))
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NewMemoryRecord {
    pub run_id: String,
    pub agent_id: String,
    pub birth_branch: String,
    pub issue_id: Option<i64>,
    pub kind: MemoryKind,
    pub importance: i32,
    pub summary: String,
    pub detail: Option<String>,
    pub supersedes_id: Option<i64>,
    pub metadata_json: Option<String>,
}

impl Default for NewMemoryRecord {
    fn default() -> Self {
        Self {
            run_id: String::new(),
            agent_id: String::new(),
            birth_branch: String::new(),
            issue_id: None,
            kind: MemoryKind::Unspecified,
            importance: 50,
            summary: String::new(),
            detail: None,
            supersedes_id: None,
            metadata_json: None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MemoryRecordRow {
    pub id: i64,
    pub run_id: String,
    pub agent_id: String,
    pub birth_branch: String,
    pub issue_id: Option<i64>,
    pub kind: MemoryKind,
    pub importance: i32,
    pub summary: String,
    pub detail: Option<String>,
    pub created_at: i64,
    pub supersedes_id: Option<i64>,
    pub metadata_json: Option<String>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct MemoryFilter {
    pub run_id: Option<String>,
    pub agent_id: Option<String>,
    pub issue_id: Option<i64>,
    pub kind: Option<MemoryKind>,
    pub min_importance: Option<i32>,
    pub limit: Option<usize>,
}

pub struct SessionMemoryService {
    db_path: PathBuf,
    conn: Mutex<Connection>,
}

impl SessionMemoryService {
    pub fn open(project_dir: impl AsRef<Path>) -> Result<Self> {
        let exo_dir = project_dir.as_ref().join(".exo");
        std::fs::create_dir_all(&exo_dir).with_context(|| {
            format!(
                "failed to create session memory directory {}",
                exo_dir.display()
            )
        })?;
        Self::open_path(exo_dir.join("memory.db"))
    }

    pub fn open_in_memory() -> Result<Self> {
        let conn = Connection::open_in_memory().map_err(|error| {
            tracing::error!(error = %error, "Failed to open in-memory session memory database");
            anyhow::Error::new(error).context("failed to open in-memory session memory database")
        })?;
        let service = Self {
            db_path: PathBuf::from(":memory:"),
            conn: Mutex::new(conn),
        };
        service.migrate()?;
        tracing::info!(path = %service.db_path.display(), "Opened session memory ledger");
        Ok(service)
    }

    pub fn open_path(db_path: impl AsRef<Path>) -> Result<Self> {
        let db_path = db_path.as_ref().to_path_buf();
        if let Some(parent) = db_path.parent() {
            std::fs::create_dir_all(parent).with_context(|| {
                format!(
                    "failed to create session memory database directory {}",
                    parent.display()
                )
            })?;
        }
        let conn = Connection::open(&db_path).map_err(|error| {
            tracing::error!(path = %db_path.display(), error = %error, "Failed to open session memory database");
            anyhow::Error::new(error).context(format!(
                "failed to open session memory database {}",
                db_path.display()
            ))
        })?;
        let service = Self {
            db_path,
            conn: Mutex::new(conn),
        };
        service.migrate()?;
        tracing::info!(path = %service.db_path.display(), "Opened session memory ledger");
        Ok(service)
    }

    pub fn db_path(&self) -> &Path {
        &self.db_path
    }

    pub fn append(&self, record: NewMemoryRecord) -> Result<i64> {
        validate_record(&record)?;
        let created_at = now_epoch_secs();
        let kind = record.kind.to_string();
        let conn = self.connection()?;
        if let Some(supersedes_id) = record.supersedes_id {
            let predecessor_exists: Option<i64> = conn
                .query_row(
                    "SELECT id FROM session_memory WHERE id = ?1",
                    [supersedes_id],
                    |row| row.get(0),
                )
                .optional()
                .map_err(|error| database_error("validate session memory predecessor", error))?;
            if predecessor_exists.is_none() {
                anyhow::bail!(
                    "supersedes_id {supersedes_id} does not reference an existing session memory record"
                );
            }
        }
        let inserted = conn
            .execute(
                "INSERT INTO session_memory (
                    run_id, agent_id, birth_branch, issue_id, kind, importance,
                    summary, detail, created_at, supersedes_id, metadata_json
                 ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)",
                rusqlite::params![
                    &record.run_id,
                    &record.agent_id,
                    &record.birth_branch,
                    record.issue_id,
                    &kind,
                    record.importance,
                    &record.summary,
                    record.detail.as_deref(),
                    created_at,
                    record.supersedes_id,
                    record.metadata_json.as_deref(),
                ],
            )
            .map_err(|error| database_error("append session memory record", error))?;
        let id = conn.last_insert_rowid();
        tracing::info!(
            kind = %kind,
            agent_id = %record.agent_id,
            issue_id = ?record.issue_id,
            record_id = id,
            "Appended session memory record"
        );
        debug_assert_eq!(inserted, 1);
        Ok(id)
    }

    pub fn list(&self, filter: MemoryFilter) -> Result<Vec<MemoryRecordRow>> {
        let mut query = String::from(
            "SELECT id, run_id, agent_id, birth_branch, issue_id, kind, importance,
                    summary, detail, created_at, supersedes_id, metadata_json
             FROM session_memory WHERE 1 = 1",
        );
        let mut values = Vec::new();
        if let Some(run_id) = filter.run_id {
            query.push_str(" AND run_id = ?");
            values.push(Value::Text(run_id));
        }
        if let Some(agent_id) = filter.agent_id {
            query.push_str(" AND agent_id = ?");
            values.push(Value::Text(agent_id));
        }
        if let Some(issue_id) = filter.issue_id {
            query.push_str(" AND issue_id = ?");
            values.push(Value::Integer(issue_id));
        }
        if let Some(kind) = filter.kind {
            query.push_str(" AND kind = ?");
            values.push(Value::Text(kind.to_string()));
        }
        if let Some(min_importance) = filter.min_importance {
            query.push_str(" AND importance >= ?");
            values.push(Value::Integer(i64::from(min_importance)));
        }
        query.push_str(" ORDER BY created_at ASC, id ASC LIMIT ?");
        let limit = filter.limit.unwrap_or(DEFAULT_LIST_LIMIT);
        values.push(Value::Integer(i64::try_from(limit).unwrap_or(i64::MAX)));

        let conn = self.connection()?;
        let mut statement = conn
            .prepare(&query)
            .map_err(|error| database_error("prepare session memory list", error))?;
        let rows = statement
            .query_map(params_from_iter(values), decode_record)
            .map_err(|error| database_error("list session memory records", error))?;
        rows.map(|row| row.map_err(|error| database_error("decode session memory record", error)))
            .collect()
    }

    pub fn latest_by_kind(
        &self,
        run_id: &str,
        kind: MemoryKind,
    ) -> Result<Option<MemoryRecordRow>> {
        let conn = self.connection()?;
        conn.query_row(
            "SELECT id, run_id, agent_id, birth_branch, issue_id, kind, importance,
                    summary, detail, created_at, supersedes_id, metadata_json
             FROM session_memory
             WHERE run_id = ?1 AND kind = ?2
             ORDER BY created_at DESC, id DESC
             LIMIT 1",
            rusqlite::params![run_id, kind.to_string()],
            decode_record,
        )
        .optional()
        .map_err(|error| database_error("find latest session memory record", error))
    }

    #[cfg(test)]
    pub fn clear_all(&self) -> Result<()> {
        let conn = self.connection()?;
        conn.execute("DELETE FROM session_memory", [])
            .map_err(|error| database_error("clear session memory ledger", error))?;
        Ok(())
    }

    fn migrate(&self) -> Result<()> {
        let conn = self.connection()?;
        conn.execute_batch(
            "PRAGMA journal_mode = WAL;
             CREATE TABLE IF NOT EXISTS session_memory (
               id            INTEGER PRIMARY KEY AUTOINCREMENT,
               run_id        TEXT    NOT NULL,
               agent_id      TEXT    NOT NULL,
               birth_branch  TEXT    NOT NULL,
               issue_id      INTEGER,
               kind          TEXT    NOT NULL,
               importance    INTEGER NOT NULL DEFAULT 50,
               summary       TEXT    NOT NULL,
               detail        TEXT,
               created_at    INTEGER NOT NULL,
               supersedes_id INTEGER REFERENCES session_memory(id),
               metadata_json TEXT
             );
             CREATE INDEX IF NOT EXISTS idx_session_memory_run
               ON session_memory(run_id);
             CREATE INDEX IF NOT EXISTS idx_session_memory_agent
               ON session_memory(agent_id);
             CREATE INDEX IF NOT EXISTS idx_session_memory_issue
               ON session_memory(issue_id);
             CREATE INDEX IF NOT EXISTS idx_session_memory_kind
               ON session_memory(kind);",
        )
        .map_err(|error| database_error("migrate session memory database", error))?;
        Ok(())
    }

    fn connection(&self) -> Result<MutexGuard<'_, Connection>> {
        self.conn
            .lock()
            .map_err(|_| anyhow::anyhow!("session memory database mutex poisoned"))
    }
}

fn validate_record(record: &NewMemoryRecord) -> Result<()> {
    if record.summary.trim().is_empty() {
        anyhow::bail!("session memory summary must not be empty or whitespace");
    }
    if record.summary.chars().count() > 200 {
        anyhow::bail!("session memory summary must be at most 200 characters");
    }
    if record
        .detail
        .as_deref()
        .is_some_and(|detail| detail.len() > 4096)
    {
        anyhow::bail!("session memory detail must be at most 4096 bytes");
    }
    if !(0..=100).contains(&record.importance) {
        anyhow::bail!("session memory importance must be between 0 and 100");
    }
    Ok(())
}

fn database_error(operation: &str, error: rusqlite::Error) -> anyhow::Error {
    tracing::error!(operation, error = %error, "Session memory database operation failed");
    anyhow::Error::new(error).context(operation.to_string())
}

fn decode_record(row: &rusqlite::Row<'_>) -> rusqlite::Result<MemoryRecordRow> {
    let kind_text: String = row.get(5)?;
    let kind = MemoryKind::from_str(&kind_text).map_err(|error| {
        rusqlite::Error::FromSqlConversionFailure(
            5,
            rusqlite::types::Type::Text,
            Box::new(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                error.to_string(),
            )),
        )
    })?;
    Ok(MemoryRecordRow {
        id: row.get(0)?,
        run_id: row.get(1)?,
        agent_id: row.get(2)?,
        birth_branch: row.get(3)?,
        issue_id: row.get(4)?,
        kind,
        importance: row.get(6)?,
        summary: row.get(7)?,
        detail: row.get(8)?,
        created_at: row.get(9)?,
        supersedes_id: row.get(10)?,
        metadata_json: row.get(11)?,
    })
}

fn now_epoch_secs() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
        .try_into()
        .unwrap_or(i64::MAX)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn record(summary: &str, kind: MemoryKind) -> NewMemoryRecord {
        NewMemoryRecord {
            run_id: "run-1".to_string(),
            agent_id: "agent-1".to_string(),
            birth_branch: "main.agent-1".to_string(),
            issue_id: Some(621),
            kind,
            importance: 75,
            summary: summary.to_string(),
            detail: Some("full detail".to_string()),
            supersedes_id: None,
            metadata_json: Some(r#"{"source":"test"}"#.to_string()),
        }
    }

    #[test]
    fn append_and_list_round_trip_every_field() {
        let service = SessionMemoryService::open_in_memory().unwrap();
        let first_id = service
            .append(record("predecessor", MemoryKind::OriginalPlan))
            .unwrap();
        let id = service
            .append(NewMemoryRecord {
                run_id: "run-1".to_string(),
                agent_id: "agent-1".to_string(),
                birth_branch: "main.agent-1".to_string(),
                issue_id: None,
                kind: MemoryKind::Decision,
                importance: 80,
                summary: "choose append-only storage".to_string(),
                detail: None,
                supersedes_id: Some(first_id),
                metadata_json: None,
            })
            .unwrap();
        let rows = service.list(MemoryFilter::default()).unwrap();
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[1].id, id);
        assert_eq!(rows[1].run_id, "run-1");
        assert_eq!(rows[1].agent_id, "agent-1");
        assert_eq!(rows[1].birth_branch, "main.agent-1");
        assert_eq!(rows[1].issue_id, None);
        assert_eq!(rows[1].kind, MemoryKind::Decision);
        assert_eq!(rows[1].importance, 80);
        assert_eq!(rows[1].summary, "choose append-only storage");
        assert_eq!(rows[1].detail, None);
        assert!(rows[1].created_at > 0);
        assert_eq!(rows[1].supersedes_id, Some(first_id));
        assert_eq!(rows[1].metadata_json, None);

        let full = service
            .list(MemoryFilter {
                kind: Some(MemoryKind::OriginalPlan),
                ..Default::default()
            })
            .unwrap();
        assert_eq!(full[0].detail.as_deref(), Some("full detail"));
        assert_eq!(
            full[0].metadata_json.as_deref(),
            Some(r#"{"source":"test"}"#)
        );
    }

    #[test]
    fn unknown_kind_string_fails_to_parse() {
        assert!("not_a_kind".parse::<MemoryKind>().is_err());
    }

    #[test]
    fn open_creates_memory_database_under_project_exo_directory() {
        let directory = tempfile::tempdir().unwrap();
        let service = SessionMemoryService::open(directory.path()).unwrap();
        assert_eq!(service.db_path(), directory.path().join(".exo/memory.db"));
        assert!(service.db_path().exists());
    }

    #[test]
    fn append_rejects_invalid_values() {
        let service = SessionMemoryService::open_in_memory().unwrap();
        assert!(service.append(record("   ", MemoryKind::Blocker)).is_err());
        assert!(service
            .append(record(&"x".repeat(201), MemoryKind::Blocker))
            .is_err());
        assert!(service
            .append(NewMemoryRecord {
                detail: Some("x".repeat(4097)),
                ..record("valid", MemoryKind::Blocker)
            })
            .is_err());
        assert!(service
            .append(NewMemoryRecord {
                importance: 101,
                ..record("valid", MemoryKind::Blocker)
            })
            .is_err());
    }

    #[test]
    fn supersedes_id_must_exist() {
        let service = SessionMemoryService::open_in_memory().unwrap();
        let error = service
            .append(NewMemoryRecord {
                supersedes_id: Some(999),
                ..record("invalid predecessor", MemoryKind::FixDirection)
            })
            .unwrap_err();
        assert!(error.to_string().contains("supersedes_id"));
    }

    #[test]
    fn list_filters_and_limits_records() {
        let service = SessionMemoryService::open_in_memory().unwrap();
        service
            .append(record("one", MemoryKind::OriginalPlan))
            .unwrap();
        service
            .append(NewMemoryRecord {
                agent_id: "agent-2".to_string(),
                issue_id: Some(622),
                kind: MemoryKind::Blocker,
                importance: 90,
                summary: "two".to_string(),
                ..record("two", MemoryKind::Blocker)
            })
            .unwrap();
        assert_eq!(
            service
                .list(MemoryFilter {
                    run_id: Some("run-1".to_string()),
                    ..Default::default()
                })
                .unwrap()
                .len(),
            2
        );
        assert_eq!(
            service
                .list(MemoryFilter {
                    agent_id: Some("agent-2".to_string()),
                    ..Default::default()
                })
                .unwrap()
                .len(),
            1
        );
        assert_eq!(
            service
                .list(MemoryFilter {
                    issue_id: Some(622),
                    ..Default::default()
                })
                .unwrap()
                .len(),
            1
        );
        assert_eq!(
            service
                .list(MemoryFilter {
                    kind: Some(MemoryKind::Blocker),
                    ..Default::default()
                })
                .unwrap()
                .len(),
            1
        );
        assert_eq!(
            service
                .list(MemoryFilter {
                    min_importance: Some(80),
                    ..Default::default()
                })
                .unwrap()
                .len(),
            1
        );
        assert_eq!(
            service
                .list(MemoryFilter {
                    limit: Some(1),
                    ..Default::default()
                })
                .unwrap()
                .len(),
            1
        );
    }

    #[test]
    fn latest_by_kind_returns_newest_or_none() {
        let service = SessionMemoryService::open_in_memory().unwrap();
        service
            .append(record("first", MemoryKind::Decision))
            .unwrap();
        service
            .append(record("second", MemoryKind::Decision))
            .unwrap();
        let latest = service
            .latest_by_kind("run-1", MemoryKind::Decision)
            .unwrap()
            .unwrap();
        assert_eq!(latest.summary, "second");
        assert!(service
            .latest_by_kind("run-1", MemoryKind::Blocker)
            .unwrap()
            .is_none());
    }

    #[test]
    fn memory_kind_display_round_trips() {
        for kind in MemoryKind::ALL {
            assert_eq!(kind.to_string().parse::<MemoryKind>().unwrap(), kind);
        }
    }
}
