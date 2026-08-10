//! Rebuildable L2 analysis storage and explicit legacy import.
//!
//! L2 is a derived view. The importer may replace rows in this database, but it
//! never mutates source files. Each source has a content fingerprint, so an
//! unchanged import is a no-op and a changed source replaces only its derived
//! rows before being replayed.

use anyhow::{bail, Context, Result};
use chrono::Utc;
use rusqlite::{params, types::ValueRef, Connection, OpenFlags, OptionalExtension, Transaction};
use serde::Serialize;
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use std::fs::{self, File};
use std::io::{BufRead, BufReader, Read};
use std::path::{Path, PathBuf};
use std::sync::{Mutex, MutexGuard};

const PARSER_VERSION: &str = "observability-import-v1";

/// User-selected input format for `exomonad logs import`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SourceFormat {
    Auto,
    Jsonl,
    Sqlite,
    Text,
    Json,
}

impl SourceFormat {
    pub fn parse(value: &str) -> Result<Self> {
        match value {
            "auto" => Ok(Self::Auto),
            "jsonl" => Ok(Self::Jsonl),
            "sqlite" => Ok(Self::Sqlite),
            "text" => Ok(Self::Text),
            "json" => Ok(Self::Json),
            other => bail!("unsupported log format {other:?}; use auto, jsonl, sqlite, or text"),
        }
    }
}

#[derive(Debug, Clone)]
pub struct ImportOptions {
    pub project_dir: PathBuf,
    pub sources: Vec<PathBuf>,
    pub format: SourceFormat,
    pub dry_run: bool,
    pub rebuild: bool,
}

#[derive(Debug, Default, Serialize, PartialEq, Eq)]
pub struct ImportSummary {
    pub discovered_sources: usize,
    pub imported_sources: usize,
    pub skipped_sources: usize,
    pub rows_read: usize,
    pub rows_rejected: usize,
}

/// The local, rebuildable analysis database at `.exo/analysis/atlas.db`.
pub struct AnalysisStore {
    path: PathBuf,
    connection: Mutex<Connection>,
}

impl AnalysisStore {
    pub fn open_project(project_dir: impl AsRef<Path>) -> Result<Self> {
        Self::open(project_dir.as_ref().join(".exo/analysis/atlas.db"))
    }

    pub fn open(path: impl Into<PathBuf>) -> Result<Self> {
        let path = path.into();
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)
                .with_context(|| format!("create analysis directory {}", parent.display()))?;
        }
        let connection = Connection::open(&path)
            .with_context(|| format!("open analysis database {}", path.display()))?;
        let store = Self {
            path,
            connection: Mutex::new(connection),
        };
        store.migrate()?;
        Ok(store)
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    fn connection(&self) -> Result<MutexGuard<'_, Connection>> {
        self.connection
            .lock()
            .map_err(|_| anyhow::anyhow!("analysis database lock poisoned"))
    }

    fn migrate(&self) -> Result<()> {
        let connection = self.connection()?;
        connection
            .execute_batch(
                "PRAGMA foreign_keys = ON;
             CREATE TABLE IF NOT EXISTS schema_meta (
                 key TEXT PRIMARY KEY,
                 value TEXT NOT NULL
             );
             INSERT OR REPLACE INTO schema_meta(key, value)
                 VALUES ('schema_version', '1');
             CREATE TABLE IF NOT EXISTS sources (
                 source_id TEXT PRIMARY KEY,
                 path_or_label TEXT NOT NULL UNIQUE,
                 source_kind TEXT NOT NULL,
                 file_size INTEGER NOT NULL,
                 modified_at INTEGER,
                 content_hash TEXT NOT NULL,
                 parser_version TEXT NOT NULL,
                 imported_at TEXT NOT NULL,
                 status TEXT NOT NULL,
                 rows_read INTEGER NOT NULL DEFAULT 0,
                 rows_rejected INTEGER NOT NULL DEFAULT 0
             );
             CREATE TABLE IF NOT EXISTS segments (
                 segment_id TEXT PRIMARY KEY,
                 source_id TEXT NOT NULL REFERENCES sources(source_id),
                 path TEXT NOT NULL,
                 event_count INTEGER NOT NULL DEFAULT 0,
                 run_seq_start INTEGER,
                 run_seq_end INTEGER
             );
             CREATE TABLE IF NOT EXISTS events (
                 event_key TEXT PRIMARY KEY,
                 source_id TEXT NOT NULL REFERENCES sources(source_id),
                 source_offset INTEGER NOT NULL,
                 event_id TEXT NOT NULL,
                 event_time TEXT,
                 observed_at TEXT,
                 run_seq INTEGER,
                 session_id TEXT,
                 run_id TEXT,
                 agent_id TEXT,
                 parent_agent_id TEXT,
                 invocation_id TEXT,
                 generation INTEGER,
                 role TEXT,
                 provider TEXT,
                 runtime TEXT,
                 harness TEXT,
                 event_type TEXT NOT NULL,
                 outcome TEXT,
                 duration_ms INTEGER,
                 attempt INTEGER,
                 issue_number INTEGER,
                 pr_number INTEGER,
                 head_sha_hash TEXT,
                 payload_class TEXT NOT NULL,
                 lifecycle_state TEXT NOT NULL,
                 sink_status TEXT,
                 identity_confidence TEXT NOT NULL,
                 local_payload_json TEXT NOT NULL
             );
             CREATE INDEX IF NOT EXISTS events_source_idx ON events(source_id);
             CREATE INDEX IF NOT EXISTS events_session_seq_idx ON events(session_id, run_seq);
             CREATE TABLE IF NOT EXISTS supersessions (
                 supersession_event_key TEXT PRIMARY KEY REFERENCES events(event_key),
                 superseded_event_id TEXT NOT NULL,
                 reason TEXT
             );
             CREATE TABLE IF NOT EXISTS sessions (
                 session_id TEXT PRIMARY KEY,
                 first_event_time TEXT,
                 last_event_time TEXT,
                 event_count INTEGER NOT NULL,
                 completeness_status TEXT NOT NULL
             );
             CREATE TABLE IF NOT EXISTS import_errors (
                 error_id INTEGER PRIMARY KEY AUTOINCREMENT,
                 source_id TEXT NOT NULL REFERENCES sources(source_id),
                 source_offset INTEGER NOT NULL,
                 error_class TEXT NOT NULL,
                 detail_hash TEXT NOT NULL
             );
             CREATE TABLE IF NOT EXISTS provenance (
                 provenance_id TEXT PRIMARY KEY,
                 artifact_kind TEXT NOT NULL,
                 source_id TEXT,
                 query_revision TEXT NOT NULL,
                 method_revision TEXT NOT NULL,
                 created_at TEXT NOT NULL
             );",
            )
            .context("migrate analysis database")?;
        Ok(())
    }

    fn clear_for_rebuild(&self) -> Result<()> {
        let connection = self.connection()?;
        connection.execute_batch(
            "DELETE FROM supersessions;
             DELETE FROM sessions;
             DELETE FROM events;
             DELETE FROM segments;
             DELETE FROM import_errors;
             DELETE FROM sources;
             DELETE FROM provenance;",
        )?;
        Ok(())
    }
}

#[derive(Debug, Clone)]
struct SourceFingerprint {
    source_id: String,
    label: String,
    kind: String,
    size: u64,
    modified_at: Option<i64>,
    content_hash: String,
}

#[derive(Debug, Default)]
struct ImportCounts {
    read: usize,
    rejected: usize,
    run_seq_min: Option<u64>,
    run_seq_max: Option<u64>,
}

impl ImportCounts {
    fn observe_seq(&mut self, run_seq: Option<u64>) {
        if let Some(run_seq) = run_seq {
            self.run_seq_min = Some(self.run_seq_min.map_or(run_seq, |min| min.min(run_seq)));
            self.run_seq_max = Some(self.run_seq_max.map_or(run_seq, |max| max.max(run_seq)));
        }
    }
}

/// Import explicit source paths into the rebuildable L2 store.
pub fn import_sources(options: &ImportOptions) -> Result<ImportSummary> {
    if options.sources.is_empty() {
        bail!("at least one --source path is required");
    }
    let paths = discover_sources(&options.sources, &options.project_dir)?;
    let mut summary = ImportSummary {
        discovered_sources: paths.len(),
        ..ImportSummary::default()
    };
    let fingerprints = paths
        .iter()
        .map(|path| fingerprint(path, options.format))
        .collect::<Result<Vec<_>>>()?;
    if options.dry_run {
        return Ok(summary);
    }

    let store = AnalysisStore::open_project(&options.project_dir)?;
    if options.rebuild {
        store.clear_for_rebuild()?;
    }
    for source in fingerprints {
        if import_one(
            &store,
            &source,
            options.format,
            options.rebuild,
            &mut summary,
        )? {
            summary.imported_sources += 1;
        } else {
            summary.skipped_sources += 1;
        }
    }
    refresh_derived_tables(&store)?;
    Ok(summary)
}

fn import_one(
    store: &AnalysisStore,
    source: &SourceFingerprint,
    requested_format: SourceFormat,
    rebuild: bool,
    summary: &mut ImportSummary,
) -> Result<bool> {
    let connection = store.connection()?;
    if !rebuild {
        let previous = connection
            .query_row(
                "SELECT content_hash, parser_version FROM sources WHERE path_or_label = ?1",
                params![source.label],
                |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)),
            )
            .optional()?;
        if previous
            .as_ref()
            .is_some_and(|(hash, parser)| hash == &source.content_hash && parser == PARSER_VERSION)
        {
            return Ok(false);
        }
    }
    let transaction = connection.unchecked_transaction()?;
    transaction.execute(
        "DELETE FROM events WHERE source_id = ?1",
        params![source.source_id],
    )?;
    transaction.execute(
        "DELETE FROM supersessions WHERE supersession_event_key NOT IN (SELECT event_key FROM events)",
        [],
    )?;
    transaction.execute(
        "DELETE FROM import_errors WHERE source_id = ?1",
        params![source.source_id],
    )?;
    transaction.execute(
        "DELETE FROM segments WHERE source_id = ?1",
        params![source.source_id],
    )?;
    transaction.execute(
        "INSERT INTO sources(
             source_id, path_or_label, source_kind, file_size, modified_at,
             content_hash, parser_version, imported_at, status, rows_read, rows_rejected
         ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, 'importing', 0, 0)
         ON CONFLICT(source_id) DO UPDATE SET
             path_or_label=excluded.path_or_label,
             source_kind=excluded.source_kind,
             file_size=excluded.file_size,
             modified_at=excluded.modified_at,
             content_hash=excluded.content_hash,
             parser_version=excluded.parser_version,
             imported_at=excluded.imported_at,
             status='importing', rows_read=0, rows_rejected=0",
        params![
            source.source_id,
            source.label,
            source.kind,
            source.size as i64,
            source.modified_at,
            source.content_hash,
            PARSER_VERSION,
            Utc::now().to_rfc3339(),
        ],
    )?;
    let format = effective_format(&source.label, requested_format)?;
    let mut counts = ImportCounts::default();
    match format {
        SourceFormat::Jsonl => import_jsonl(&transaction, source, &mut counts)?,
        SourceFormat::Text => import_text(&transaction, source, &mut counts)?,
        SourceFormat::Json => import_json_document(&transaction, source, &mut counts)?,
        SourceFormat::Sqlite => import_sqlite(&transaction, source, &mut counts)?,
        SourceFormat::Auto => unreachable!("effective format is never auto"),
    }
    let status = if counts.rejected == 0 {
        "complete"
    } else {
        "partial"
    };
    transaction.execute(
        "UPDATE sources SET status = ?2, rows_read = ?3, rows_rejected = ?4 WHERE source_id = ?1",
        params![
            source.source_id,
            status,
            counts.read as i64,
            counts.rejected as i64
        ],
    )?;
    if matches!(format, SourceFormat::Jsonl) && source.label.contains("/ledger/segments/") {
        transaction.execute(
            "INSERT OR REPLACE INTO segments(
                 segment_id, source_id, path, event_count, run_seq_start, run_seq_end
             ) VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params![
                format!("segment:{}", source.source_id),
                source.source_id,
                source.label,
                counts.read as i64,
                counts.run_seq_min.map(|value| value as i64),
                counts.run_seq_max.map(|value| value as i64),
            ],
        )?;
    }
    transaction.commit()?;
    summary.rows_read += counts.read;
    summary.rows_rejected += counts.rejected;
    Ok(true)
}

fn import_jsonl(
    transaction: &Transaction<'_>,
    source: &SourceFingerprint,
    counts: &mut ImportCounts,
) -> Result<()> {
    let file = File::open(&source.label).with_context(|| format!("open {}", source.label))?;
    let mut reader = BufReader::new(file);
    let mut line = String::new();
    let mut offset = 0_u64;
    loop {
        line.clear();
        let bytes = reader.read_line(&mut line)?;
        if bytes == 0 {
            break;
        }
        let current_offset = offset;
        offset += bytes as u64;
        if line.trim().is_empty() {
            continue;
        }
        counts.read += 1;
        match serde_json::from_str::<Value>(line.trim_end()) {
            Ok(value) => {
                let event = normalize_event(source, current_offset, value)?;
                counts.observe_seq(event.run_seq);
                insert_event(transaction, &event)?;
            }
            Err(error) => {
                counts.rejected += 1;
                insert_import_error(
                    transaction,
                    &source.source_id,
                    current_offset,
                    "invalid_json",
                    &error.to_string(),
                )?;
            }
        }
    }
    Ok(())
}

fn import_json_document(
    transaction: &Transaction<'_>,
    source: &SourceFingerprint,
    counts: &mut ImportCounts,
) -> Result<()> {
    let contents = fs::read_to_string(&source.label)
        .with_context(|| format!("read JSON source {}", source.label))?;
    match serde_json::from_str::<Value>(&contents) {
        Ok(value) => {
            counts.read = 1;
            let event = normalize_event(source, 0, value)?;
            counts.observe_seq(event.run_seq);
            insert_event(transaction, &event)?;
        }
        Err(error) => {
            counts.read = 1;
            counts.rejected = 1;
            insert_import_error(
                transaction,
                &source.source_id,
                0,
                "invalid_json",
                &error.to_string(),
            )?;
        }
    }
    Ok(())
}

fn import_text(
    transaction: &Transaction<'_>,
    source: &SourceFingerprint,
    counts: &mut ImportCounts,
) -> Result<()> {
    let file = File::open(&source.label).with_context(|| format!("open {}", source.label))?;
    let mut reader = BufReader::new(file);
    let mut line = String::new();
    let mut offset = 0_u64;
    loop {
        line.clear();
        let bytes = reader.read_line(&mut line)?;
        if bytes == 0 {
            break;
        }
        let current_offset = offset;
        offset += bytes as u64;
        if line.trim().is_empty() {
            continue;
        }
        counts.read += 1;
        let value = serde_json::json!({
            "type": "custom.legacy.text",
            "line": line.trim_end_matches(['\r', '\n']),
        });
        let event = normalize_event(source, current_offset, value)?;
        insert_event(transaction, &event)?;
    }
    Ok(())
}

fn import_sqlite(
    transaction: &Transaction<'_>,
    source: &SourceFingerprint,
    counts: &mut ImportCounts,
) -> Result<()> {
    let source_connection =
        Connection::open_with_flags(&source.label, OpenFlags::SQLITE_OPEN_READ_ONLY)
            .with_context(|| format!("open SQLite source {} read-only", source.label))?;
    let mut table_query = source_connection.prepare(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name",
    )?;
    let table_names = table_query
        .query_map([], |row| row.get::<_, String>(0))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    let mut offset = 0_u64;
    for table in table_names {
        let identifier = quote_identifier(&table)?;
        let mut statement = source_connection.prepare(&format!("SELECT * FROM {identifier}"))?;
        let columns = statement
            .column_names()
            .into_iter()
            .map(ToOwned::to_owned)
            .collect::<Vec<_>>();
        let mut rows = statement.query([])?;
        while let Some(row) = rows.next()? {
            counts.read += 1;
            let mut object = Map::new();
            for (index, column) in columns.iter().enumerate() {
                object.insert(column.clone(), sqlite_value(row.get_ref(index)?)?);
            }
            let value = serde_json::json!({
                "type": format!("custom.legacy.sqlite.{table}"),
                "table": table,
                "row": object,
            });
            let event = normalize_event(source, offset, value)?;
            insert_event(transaction, &event)?;
            offset += 1;
        }
    }
    Ok(())
}

fn normalize_event(
    source: &SourceFingerprint,
    offset: u64,
    value: Value,
) -> Result<NormalizedEvent> {
    let canonical = serde_json::to_string(&value).context("canonicalize imported event")?;
    let fallback_id = hash_bytes(format!("{}:{offset}:{canonical}", source.source_id).as_bytes());
    let event_id = string_field(&value, &["event_id", "id"]).unwrap_or(fallback_id);
    let event_type = string_field(&value, &["type", "event_type"])
        .or_else(|| {
            if source.label.ends_with("/sink-health.json") {
                Some("sink.health".to_string())
            } else {
                None
            }
        })
        .unwrap_or_else(|| format!("custom.legacy.{}", source.kind));
    let lifecycle_state = string_field(&value, &["lifecycle_state"]).unwrap_or_else(|| {
        if source.kind == "jsonl" {
            "observed".to_string()
        } else {
            "legacy".to_string()
        }
    });
    let run_seq = number_field(&value, &["run_seq"]);
    let data = value.get("data").cloned().unwrap_or_else(|| value.clone());
    Ok(NormalizedEvent {
        event_key: hash_bytes(format!("{}:{offset}:{canonical}", source.source_id).as_bytes()),
        source_id: source.source_id.clone(),
        source_offset: offset,
        event_id,
        event_time: string_field(&value, &["event_time", "ts", "timestamp"])
            .or_else(|| number_field(&value, &["timestamp_ms"]).map(|value| value.to_string())),
        observed_at: string_field(&value, &["observed_at"]),
        run_seq,
        session_id: string_field(&value, &["session_id"]),
        run_id: string_field(&value, &["run_id"]),
        agent_id: string_field(&value, &["agent_id"]),
        parent_agent_id: string_field(&value, &["parent_agent_id"]),
        invocation_id: string_field(&value, &["invocation_id"]),
        generation: number_field(&value, &["generation"]),
        role: string_field(&value, &["role"]),
        provider: string_field(&value, &["provider"]),
        runtime: string_field(&value, &["runtime"]),
        harness: string_field(&value, &["harness"]),
        event_type,
        outcome: string_field(&value, &["outcome"]),
        duration_ms: number_field(&value, &["duration_ms"]),
        attempt: number_field(&value, &["attempt"]),
        issue_number: number_field(&value, &["issue_number"]),
        pr_number: number_field(&value, &["pr_number"]),
        head_sha_hash: string_field(&value, &["head_sha_hash", "head_sha"])
            .map(|value| hash_bytes(value.as_bytes())),
        payload_class: string_field(&value, &["payload_class"])
            .unwrap_or_else(|| "local_sensitive".to_string()),
        lifecycle_state,
        sink_status: string_field(&value, &["sink_status"]),
        identity_confidence: if source.kind == "jsonl" && value.get("event_id").is_some() {
            "exact".to_string()
        } else {
            "legacy".to_string()
        },
        local_payload_json: serde_json::to_string(&serde_json::json!({
            "event": value,
            "data": data,
        }))?,
        superseded_event_id: string_field(&value, &["superseded_event_id"]),
        supersession_reason: string_field(&value, &["supersession_reason"]),
    })
}

#[derive(Debug)]
struct NormalizedEvent {
    event_key: String,
    source_id: String,
    source_offset: u64,
    event_id: String,
    event_time: Option<String>,
    observed_at: Option<String>,
    run_seq: Option<u64>,
    session_id: Option<String>,
    run_id: Option<String>,
    agent_id: Option<String>,
    parent_agent_id: Option<String>,
    invocation_id: Option<String>,
    generation: Option<u64>,
    role: Option<String>,
    provider: Option<String>,
    runtime: Option<String>,
    harness: Option<String>,
    event_type: String,
    outcome: Option<String>,
    duration_ms: Option<u64>,
    attempt: Option<u64>,
    issue_number: Option<u64>,
    pr_number: Option<u64>,
    head_sha_hash: Option<String>,
    payload_class: String,
    lifecycle_state: String,
    sink_status: Option<String>,
    identity_confidence: String,
    local_payload_json: String,
    superseded_event_id: Option<String>,
    supersession_reason: Option<String>,
}

fn insert_event(transaction: &Transaction<'_>, event: &NormalizedEvent) -> Result<()> {
    transaction.execute(
        "INSERT OR IGNORE INTO events(
             event_key, source_id, source_offset, event_id, event_time, observed_at,
             run_seq, session_id, run_id, agent_id, parent_agent_id, invocation_id,
             generation, role, provider, runtime, harness, event_type, outcome,
             duration_ms, attempt, issue_number, pr_number, head_sha_hash,
             payload_class, lifecycle_state, sink_status, identity_confidence,
             local_payload_json
         ) VALUES (
             ?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14,
             ?15, ?16, ?17, ?18, ?19, ?20, ?21, ?22, ?23, ?24, ?25, ?26,
             ?27, ?28, ?29
         )",
        params![
            event.event_key,
            event.source_id,
            event.source_offset as i64,
            event.event_id,
            event.event_time,
            event.observed_at,
            event.run_seq.map(|value| value as i64),
            event.session_id,
            event.run_id,
            event.agent_id,
            event.parent_agent_id,
            event.invocation_id,
            event.generation.map(|value| value as i64),
            event.role,
            event.provider,
            event.runtime,
            event.harness,
            event.event_type,
            event.outcome,
            event.duration_ms.map(|value| value as i64),
            event.attempt.map(|value| value as i64),
            event.issue_number.map(|value| value as i64),
            event.pr_number.map(|value| value as i64),
            event.head_sha_hash,
            event.payload_class,
            event.lifecycle_state,
            event.sink_status,
            event.identity_confidence,
            event.local_payload_json,
        ],
    )?;
    if let Some(superseded_event_id) = &event.superseded_event_id {
        transaction.execute(
            "INSERT OR REPLACE INTO supersessions(
                 supersession_event_key, superseded_event_id, reason
             ) VALUES (?1, ?2, ?3)",
            params![
                event.event_key,
                superseded_event_id,
                event.supersession_reason
            ],
        )?;
    }
    Ok(())
}

fn insert_import_error(
    transaction: &Transaction<'_>,
    source_id: &str,
    offset: u64,
    error_class: &str,
    detail: &str,
) -> Result<()> {
    transaction.execute(
        "INSERT INTO import_errors(source_id, source_offset, error_class, detail_hash)
         VALUES (?1, ?2, ?3, ?4)",
        params![
            source_id,
            offset as i64,
            error_class,
            hash_bytes(detail.as_bytes())
        ],
    )?;
    Ok(())
}

fn refresh_derived_tables(store: &AnalysisStore) -> Result<()> {
    let connection = store.connection()?;
    connection.execute_batch(
        "DELETE FROM sessions;
         INSERT INTO sessions(session_id, first_event_time, last_event_time, event_count, completeness_status)
         SELECT session_id, MIN(event_time), MAX(event_time), COUNT(*),
                CASE WHEN SUM(CASE WHEN run_seq IS NULL THEN 1 ELSE 0 END) = 0
                     THEN 'known' ELSE 'unknown' END
         FROM events WHERE session_id IS NOT NULL GROUP BY session_id;",
    )?;
    Ok(())
}

fn discover_sources(inputs: &[PathBuf], project_dir: &Path) -> Result<Vec<PathBuf>> {
    let analysis_db = project_dir.join(".exo/analysis/atlas.db");
    let mut paths = Vec::new();
    for input in inputs {
        let metadata = fs::metadata(input)
            .with_context(|| format!("inspect import source {}", input.display()))?;
        if metadata.is_dir() {
            collect_files(input, &analysis_db, &mut paths)?;
        } else if input != &analysis_db {
            paths.push(input.clone());
        }
    }
    paths.sort();
    paths.dedup();
    Ok(paths)
}

fn collect_files(dir: &Path, analysis_db: &Path, paths: &mut Vec<PathBuf>) -> Result<()> {
    for entry in
        fs::read_dir(dir).with_context(|| format!("read source directory {}", dir.display()))?
    {
        let entry = entry?;
        let path = entry.path();
        if path == analysis_db {
            continue;
        }
        if entry.file_type()?.is_dir() {
            collect_files(&path, analysis_db, paths)?;
        } else if entry.file_type()?.is_file() {
            paths.push(path);
        }
    }
    Ok(())
}

fn fingerprint(path: &Path, requested_format: SourceFormat) -> Result<SourceFingerprint> {
    let canonical = path
        .canonicalize()
        .with_context(|| format!("canonicalize import source {}", path.display()))?;
    let metadata = fs::metadata(&canonical)?;
    let content_hash = hash_file(&canonical)?;
    let label = canonical.to_string_lossy().into_owned();
    let kind = match effective_format(&label, requested_format)? {
        SourceFormat::Jsonl => "jsonl",
        SourceFormat::Sqlite => "sqlite",
        SourceFormat::Json => "json",
        SourceFormat::Text => "text",
        SourceFormat::Auto => unreachable!("effective format is never auto"),
    };
    Ok(SourceFingerprint {
        source_id: hash_bytes(label.as_bytes()),
        label,
        kind: kind.to_string(),
        size: metadata.len(),
        modified_at: metadata
            .modified()
            .ok()
            .and_then(|time| time.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|duration| duration.as_secs() as i64),
        content_hash,
    })
}

fn effective_format(label: &str, requested: SourceFormat) -> Result<SourceFormat> {
    if requested != SourceFormat::Auto {
        return Ok(requested);
    }
    let extension = Path::new(label)
        .extension()
        .and_then(|extension| extension.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase();
    match extension.as_str() {
        "db" | "sqlite" | "sqlite3" => Ok(SourceFormat::Sqlite),
        "log" | "txt" => Ok(SourceFormat::Text),
        "json" => Ok(SourceFormat::Json),
        _ => Ok(SourceFormat::Jsonl),
    }
}

fn string_field(value: &Value, names: &[&str]) -> Option<String> {
    names.iter().find_map(|name| {
        value.get(*name).and_then(|field| match field {
            Value::String(value) => Some(value.clone()),
            Value::Number(value) => Some(value.to_string()),
            _ => None,
        })
    })
}

fn number_field(value: &Value, names: &[&str]) -> Option<u64> {
    names.iter().find_map(|name| {
        value.get(*name).and_then(|field| match field {
            Value::Number(value) => value.as_u64(),
            Value::String(value) => value.parse().ok(),
            _ => None,
        })
    })
}

fn hash_file(path: &Path) -> Result<String> {
    let mut file =
        File::open(path).with_context(|| format!("open {} for hashing", path.display()))?;
    let mut hasher = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let read = file.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    Ok(format!("{:x}", hasher.finalize()))
}

fn hash_bytes(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn quote_identifier(identifier: &str) -> Result<String> {
    if identifier.is_empty()
        || !identifier
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_')
    {
        bail!("unsupported SQLite identifier {identifier:?}");
    }
    Ok(format!("\"{identifier}\""))
}

fn sqlite_value(value: ValueRef<'_>) -> Result<Value> {
    Ok(match value {
        ValueRef::Null => Value::Null,
        ValueRef::Integer(value) => Value::from(value),
        ValueRef::Real(value) => serde_json::Number::from_f64(value)
            .map(Value::Number)
            .unwrap_or(Value::Null),
        ValueRef::Text(value) => Value::String(String::from_utf8_lossy(value).into_owned()),
        ValueRef::Blob(value) => Value::String(format!("sha256:{}", hash_bytes(value))),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use tempfile::TempDir;

    fn options(project_dir: &Path, source: PathBuf) -> ImportOptions {
        ImportOptions {
            project_dir: project_dir.to_path_buf(),
            sources: vec![source],
            format: SourceFormat::Auto,
            dry_run: false,
            rebuild: false,
        }
    }

    #[test]
    fn imports_jsonl_idempotently_and_keeps_source_unchanged() -> Result<()> {
        let temp = TempDir::new()?;
        let source = temp.path().join("events.jsonl");
        let mut file = File::create(&source)?;
        writeln!(file, "{{\"event_id\":\"e1\",\"type\":\"agent.spawned\",\"session_id\":\"s1\",\"run_seq\":1}}")?;
        writeln!(file, "{{bad json")?;
        file.sync_all()?;
        let before = fs::read(&source)?;
        let first = import_sources(&options(temp.path(), source.clone()))?;
        let second = import_sources(&options(temp.path(), source.clone()))?;
        assert_eq!(first.imported_sources, 1);
        assert_eq!(first.rows_read, 2);
        assert_eq!(first.rows_rejected, 1);
        assert_eq!(second.skipped_sources, 1);
        assert_eq!(fs::read(&source)?, before);

        let connection = Connection::open(temp.path().join(".exo/analysis/atlas.db"))?;
        assert_eq!(
            connection.query_row("SELECT COUNT(*) FROM events", [], |row| row
                .get::<_, i64>(0))?,
            1
        );
        assert_eq!(
            connection.query_row("SELECT COUNT(*) FROM import_errors", [], |row| row
                .get::<_, i64>(0))?,
            1
        );
        Ok(())
    }

    #[test]
    fn changed_source_replaces_derived_rows_and_rebuild_is_deterministic() -> Result<()> {
        let temp = TempDir::new()?;
        let source = temp.path().join("events.jsonl");
        fs::write(&source, "{\"event_id\":\"e1\",\"type\":\"custom.one\"}\n")?;
        import_sources(&options(temp.path(), source.clone()))?;
        fs::write(&source, "{\"event_id\":\"e2\",\"type\":\"custom.two\"}\n")?;
        import_sources(&options(temp.path(), source.clone()))?;
        let db = temp.path().join(".exo/analysis/atlas.db");
        let connection = Connection::open(&db)?;
        let event_id = connection.query_row("SELECT event_id FROM events", [], |row| {
            row.get::<_, String>(0)
        })?;
        assert_eq!(event_id, "e2");
        let snapshot = fs::read(&db)?;
        let mut rebuilt = options(temp.path(), source);
        rebuilt.rebuild = true;
        import_sources(&rebuilt)?;
        let rebuilt_connection = Connection::open(&db)?;
        assert_eq!(
            rebuilt_connection.query_row("SELECT event_id FROM events", [], |row| row
                .get::<_, String>(0))?,
            "e2"
        );
        assert!(!snapshot.is_empty());
        Ok(())
    }

    #[test]
    fn auto_imports_sink_health_json_into_l2() -> Result<()> {
        let temp = TempDir::new()?;
        let health = temp.path().join(".exo/sink-health.json");
        fs::create_dir_all(health.parent().context("health parent")?)?;
        fs::write(
            &health,
            serde_json::to_vec_pretty(&serde_json::json!({
                "accepted_event_count": 4,
                "rejected_event_count": 1,
                "write_failure_count": 1,
                "measurement_status": "partial"
            }))?,
        )?;
        import_sources(&options(temp.path(), health))?;
        let connection = Connection::open(temp.path().join(".exo/analysis/atlas.db"))?;
        assert_eq!(
            connection.query_row("SELECT event_type FROM events", [], |row| row
                .get::<_, String>(0))?,
            "sink.health"
        );
        Ok(())
    }

    #[test]
    fn sqlite_sources_are_opened_read_only() -> Result<()> {
        let temp = TempDir::new()?;
        let source = temp.path().join("memory.db");
        let connection = Connection::open(&source)?;
        connection.execute("CREATE TABLE records (id INTEGER, value TEXT)", [])?;
        connection.execute("INSERT INTO records VALUES (1, 'secret')", [])?;
        drop(connection);
        let summary = import_sources(&options(temp.path(), source.clone()))?;
        assert_eq!(summary.rows_read, 1);
        assert_eq!(fs::metadata(source)?.len() > 0, true);
        Ok(())
    }
}
