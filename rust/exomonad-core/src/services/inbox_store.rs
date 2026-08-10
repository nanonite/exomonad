use super::state_mirror::append_state_change;
use anyhow::{Context, Result};
use rusqlite::{params, Connection};
use serde_json::json;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, MutexGuard};
use std::time::{SystemTime, UNIX_EPOCH};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InboxMessageRecord {
    pub id: i64,
    pub from_agent: String,
    pub to_agent: String,
    pub content: String,
    pub summary: Option<String>,
    pub created_at: i64,
    pub notified_at: Option<i64>,
    pub read_at: Option<i64>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InboxPokeCandidate {
    pub agent_id: String,
    pub unread_count: usize,
    pub newest_message_id: i64,
}

pub struct InboxStore {
    db_path: PathBuf,
    conn: Mutex<Connection>,
}

impl InboxStore {
    pub fn open(project_dir: impl AsRef<Path>) -> Result<Self> {
        let exo_dir = project_dir.as_ref().join(".exo");
        std::fs::create_dir_all(&exo_dir)
            .with_context(|| format!("failed to create inbox directory {}", exo_dir.display()))?;
        Self::open_path(exo_dir.join("inbox.db"))
    }

    pub fn open_in_memory() -> Result<Self> {
        let conn =
            Connection::open_in_memory().context("failed to open in-memory inbox database")?;
        let store = Self {
            db_path: PathBuf::from(":memory:"),
            conn: Mutex::new(conn),
        };
        store.migrate()?;
        Ok(store)
    }

    pub fn open_path(db_path: impl AsRef<Path>) -> Result<Self> {
        let db_path = db_path.as_ref().to_path_buf();
        if let Some(parent) = db_path.parent() {
            std::fs::create_dir_all(parent).with_context(|| {
                format!(
                    "failed to create inbox database directory {}",
                    parent.display()
                )
            })?;
        }
        let conn = Connection::open(&db_path)
            .with_context(|| format!("failed to open inbox database {}", db_path.display()))?;
        let store = Self {
            db_path,
            conn: Mutex::new(conn),
        };
        store.migrate()?;
        Ok(store)
    }

    pub fn db_path(&self) -> &Path {
        &self.db_path
    }

    pub fn clear_all(&self) -> Result<()> {
        let conn = self.connection()?;
        let abandoned_count: i64 = conn
            .query_row("SELECT COUNT(*) FROM messages", [], |row| row.get(0))
            .context("count inbox messages before clear")?;
        conn.execute_batch(
            "DELETE FROM messages;
             DELETE FROM agent_inbox_meta;",
        )
        .context("failed to clear inbox")?;
        if abandoned_count > 0 {
            append_state_change(
                &self.db_path,
                "agent_inbox.messages_abandoned",
                json!({
                    "operation": "clear_all",
                    "count": abandoned_count,
                    "outcome": "abandoned"
                }),
            );
        }
        append_state_change(
            &self.db_path,
            "inbox.state_changed",
            json!({"operation": "clear_all"}),
        );
        Ok(())
    }

    pub fn write_message(
        &self,
        from_agent: &str,
        to_agent: &str,
        content: &str,
        summary: Option<&str>,
    ) -> Result<i64> {
        let created_at = now_epoch_secs();
        let normalized_to_agent = normalize_agent_id(to_agent);
        let conn = self.connection()?;
        conn.execute(
            "INSERT INTO messages (from_agent, to_agent, content, summary, created_at) VALUES (?1, ?2, ?3, ?4, ?5)",
            params![from_agent, normalized_to_agent, content, summary, created_at],
        )
        .context("failed to insert inbox message")?;
        let id = conn.last_insert_rowid();
        append_state_change(
            &self.db_path,
            "inbox.state_changed",
            json!({
                "operation": "write_message",
                "message_id": id,
                "from_agent": from_agent,
                "to_agent": normalized_to_agent,
                "content": content,
                "summary": summary,
                "created_at": created_at
            }),
        );
        append_state_change(
            &self.db_path,
            "message.delivery",
            json!({
                "message_id": id,
                "from_agent": from_agent,
                "to_agent": normalized_to_agent,
                "attempt": 1,
                "outcome": "accepted",
                "transport": "durable_inbox"
            }),
        );
        Ok(id)
    }

    pub fn peek_unnotified(&self, agent_id: &str) -> Result<Vec<InboxMessageRecord>> {
        let now = now_epoch_secs();
        let normalized_agent_id = normalize_agent_id(agent_id);
        let mut conn = self.connection()?;
        let tx = conn
            .transaction()
            .context("failed to start inbox peek transaction")?;
        let messages = select_messages(
            &tx,
            "SELECT id, from_agent, to_agent, content, summary, created_at, notified_at, read_at
             FROM messages
             WHERE to_agent = ?1 AND read_at IS NULL AND notified_at IS NULL
             ORDER BY created_at ASC, id ASC",
            normalized_agent_id.as_ref(),
        )?;
        for message in &messages {
            tx.execute(
                "UPDATE messages SET notified_at = ?1 WHERE id = ?2",
                params![now, message.id],
            )
            .context("failed to mark inbox message as notified")?;
        }
        tx.commit()
            .context("failed to commit inbox peek transaction")?;
        if !messages.is_empty() {
            append_state_change(
                &self.db_path,
                "inbox.state_changed",
                json!({
                    "operation": "mark_notified",
                    "agent_id": normalized_agent_id,
                    "message_ids": messages.iter().map(|message| message.id).collect::<Vec<_>>(),
                    "notified_at": now
                }),
            );
            for message in &messages {
                append_state_change(
                    &self.db_path,
                    "message.consumed",
                    json!({
                        "message_id": message.id,
                        "from_agent": message.from_agent,
                        "to_agent": message.to_agent,
                        "read_at": now
                    }),
                );
            }
        }
        Ok(messages)
    }

    pub fn drain_unread(&self, agent_id: &str) -> Result<Vec<InboxMessageRecord>> {
        let now = now_epoch_secs();
        let normalized_agent_id = normalize_agent_id(agent_id);
        let mut conn = self.connection()?;
        let tx = conn
            .transaction()
            .context("failed to start inbox drain transaction")?;
        let messages = select_messages(
            &tx,
            "SELECT id, from_agent, to_agent, content, summary, created_at, notified_at, read_at
             FROM messages
             WHERE to_agent = ?1 AND read_at IS NULL
             ORDER BY created_at ASC, id ASC",
            normalized_agent_id.as_ref(),
        )?;
        tx.execute(
            "UPDATE messages SET read_at = ?1 WHERE to_agent = ?2 AND read_at IS NULL",
            params![now, normalized_agent_id.as_ref()],
        )
        .context("failed to mark inbox messages as read")?;
        tx.execute(
            "INSERT INTO agent_inbox_meta (agent_id, last_check_inbox_at, last_poke_at, last_poke_message_id, poke_backoff_secs)
             VALUES (?1, ?2, NULL, NULL, NULL)
             ON CONFLICT(agent_id) DO UPDATE SET last_check_inbox_at = excluded.last_check_inbox_at",
            params![normalized_agent_id.as_ref(), now],
        )
        .context("failed to update inbox metadata")?;
        tx.commit()
            .context("failed to commit inbox drain transaction")?;
        if !messages.is_empty() {
            append_state_change(
                &self.db_path,
                "inbox.state_changed",
                json!({
                    "operation": "mark_read",
                    "agent_id": normalized_agent_id,
                    "message_ids": messages.iter().map(|message| message.id).collect::<Vec<_>>(),
                    "read_at": now
                }),
            );
        }
        Ok(messages)
    }

    pub fn agents_needing_poke(&self, base_interval_secs: u64) -> Result<Vec<InboxPokeCandidate>> {
        let base_interval_secs = i64::try_from(base_interval_secs).unwrap_or(i64::MAX);
        let now = now_epoch_secs();
        let conn = self.connection()?;
        let mut stmt = conn
            .prepare(
                "SELECT m.to_agent,
                        COUNT(*) AS unread_count,
                        MAX(m.id) AS newest_message_id,
                        MAX(m.created_at) AS newest_message_at,
                        meta.last_check_inbox_at,
                        meta.last_poke_at,
                        meta.last_poke_message_id,
                        meta.poke_backoff_secs
                 FROM messages m
                 LEFT JOIN agent_inbox_meta meta ON meta.agent_id = m.to_agent
                 WHERE m.read_at IS NULL
                 GROUP BY m.to_agent
                 ORDER BY m.to_agent ASC",
            )
            .context("failed to prepare inbox poke query")?;
        let rows = stmt
            .query_map([], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, i64>(1)?,
                    row.get::<_, i64>(2)?,
                    row.get::<_, i64>(3)?,
                    row.get::<_, Option<i64>>(4)?,
                    row.get::<_, Option<i64>>(5)?,
                    row.get::<_, Option<i64>>(6)?,
                    row.get::<_, Option<i64>>(7)?,
                ))
            })
            .context("failed to query inbox poke candidates")?;
        let mut candidates = Vec::new();
        for row in rows {
            let (
                agent_id,
                unread_count,
                newest_message_id,
                newest_message_at,
                last_check_inbox_at,
                last_poke_at,
                last_poke_message_id,
                poke_backoff_secs,
            ) = row.context("failed to decode inbox poke candidate")?;

            let has_new_mail_since_last_poke = last_poke_message_id != Some(newest_message_id);
            let due_at = if has_new_mail_since_last_poke {
                newest_message_at.saturating_add(base_interval_secs)
            } else {
                let backoff_secs = poke_backoff_secs.unwrap_or(base_interval_secs);
                last_poke_at
                    .unwrap_or(newest_message_at)
                    .saturating_add(backoff_secs)
            };
            let checked_after_due =
                last_check_inbox_at.is_some_and(|checked_at| checked_at >= due_at);
            if checked_after_due || now < due_at {
                continue;
            }

            candidates.push(InboxPokeCandidate {
                agent_id,
                unread_count: usize::try_from(unread_count).unwrap_or(usize::MAX),
                newest_message_id,
            });
        }
        Ok(candidates)
    }

    pub fn record_poke(
        &self,
        agent_id: &str,
        newest_message_id: i64,
        base_interval_secs: u64,
        max_interval_secs: u64,
    ) -> Result<()> {
        let normalized_agent_id = normalize_agent_id(agent_id);
        let now = now_epoch_secs();
        let base_interval_secs = i64::try_from(base_interval_secs).unwrap_or(i64::MAX);
        let max_interval_secs = i64::try_from(max_interval_secs).unwrap_or(i64::MAX);
        let conn = self.connection()?;
        let (last_poke_message_id, poke_backoff_secs): (Option<i64>, Option<i64>) = conn
            .query_row(
                "SELECT last_poke_message_id, poke_backoff_secs
                 FROM agent_inbox_meta
                 WHERE agent_id = ?1",
                params![normalized_agent_id.as_ref()],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap_or((None, None));
        let next_backoff_secs = if last_poke_message_id == Some(newest_message_id) {
            poke_backoff_secs
                .unwrap_or(base_interval_secs)
                .saturating_mul(2)
                .min(max_interval_secs)
        } else {
            base_interval_secs
        };
        conn.execute(
            "INSERT INTO agent_inbox_meta (
                 agent_id,
                 last_check_inbox_at,
                 last_poke_at,
                 last_poke_message_id,
                 poke_backoff_secs
             ) VALUES (?1, NULL, ?2, ?3, ?4)
             ON CONFLICT(agent_id) DO UPDATE SET
                 last_poke_at = excluded.last_poke_at,
                 last_poke_message_id = excluded.last_poke_message_id,
                 poke_backoff_secs = excluded.poke_backoff_secs",
            params![
                normalized_agent_id.as_ref(),
                now,
                newest_message_id,
                next_backoff_secs
            ],
        )
        .context("failed to record inbox poke metadata")?;
        append_state_change(
            &self.db_path,
            "inbox.state_changed",
            json!({
                "operation": "record_poke",
                "agent_id": normalized_agent_id,
                "newest_message_id": newest_message_id,
                "poke_backoff_secs": next_backoff_secs,
                "last_poke_at": now
            }),
        );
        Ok(())
    }

    pub fn unread_count(&self, agent_id: &str) -> Result<usize> {
        let normalized_agent_id = normalize_agent_id(agent_id);
        let conn = self.connection()?;
        let count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM messages WHERE to_agent = ?1 AND read_at IS NULL",
                params![normalized_agent_id.as_ref()],
                |row| row.get(0),
            )
            .context("failed to query unread inbox count")?;
        Ok(usize::try_from(count).unwrap_or(usize::MAX))
    }

    pub fn has_unread(&self, agent_id: &str) -> Result<bool> {
        Ok(self.unread_count(agent_id)? > 0)
    }

    pub fn last_check_inbox_at(&self, agent_id: &str) -> Result<Option<i64>> {
        let normalized_agent_id = normalize_agent_id(agent_id);
        let conn = self.connection()?;
        let mut stmt = conn
            .prepare("SELECT last_check_inbox_at FROM agent_inbox_meta WHERE agent_id = ?1")
            .context("failed to prepare inbox metadata query")?;
        let mut rows = stmt
            .query(params![normalized_agent_id.as_ref()])
            .context("failed to query inbox metadata")?;
        match rows.next().context("failed to read inbox metadata row")? {
            Some(row) => row
                .get(0)
                .context("failed to decode inbox metadata timestamp"),
            None => Ok(None),
        }
    }

    fn migrate(&self) -> Result<()> {
        let mut conn = self.connection()?;
        conn.execute_batch(
            "PRAGMA journal_mode = WAL;
             CREATE TABLE IF NOT EXISTS messages (
               id          INTEGER PRIMARY KEY,
               from_agent  TEXT    NOT NULL,
               to_agent    TEXT    NOT NULL,
               content     TEXT    NOT NULL,
               summary     TEXT,
               created_at  INTEGER NOT NULL,
               notified_at INTEGER,
               read_at     INTEGER
             );
             CREATE INDEX IF NOT EXISTS idx_messages_to_read_notify
               ON messages (to_agent, read_at, notified_at, created_at, id);
             CREATE TABLE IF NOT EXISTS agent_inbox_meta (
               agent_id            TEXT    PRIMARY KEY,
               last_check_inbox_at INTEGER,
               last_poke_at        INTEGER,
               last_poke_message_id INTEGER,
               poke_backoff_secs   INTEGER
             );",
        )
        .context("failed to migrate inbox database")?;
        ensure_column(&conn, "agent_inbox_meta", "last_poke_at", "INTEGER")?;
        ensure_column(&conn, "agent_inbox_meta", "last_poke_message_id", "INTEGER")?;
        ensure_column(&conn, "agent_inbox_meta", "poke_backoff_secs", "INTEGER")?;
        normalize_existing_agent_ids(&mut conn)?;
        remap_legacy_branch_recipients(&mut conn)?;
        Ok(())
    }

    fn connection(&self) -> Result<MutexGuard<'_, Connection>> {
        self.conn
            .lock()
            .map_err(|_| anyhow::anyhow!("inbox database mutex poisoned"))
    }
}

/// Inbox keys are the recipient's bare AgentName (the last dot-segment of a
/// branch). This is a pure structural strip: semantic recipient resolution
/// (e.g. a top-level branch like `main` belonging to the root agent) happens at
/// the notify_parent source via `delivery::canonical_parent_recipient`, not here.
fn normalize_agent_id(agent_id: &str) -> std::borrow::Cow<'_, str> {
    match agent_id.rsplit_once('.') {
        Some((_, bare)) if !bare.is_empty() => std::borrow::Cow::Borrowed(bare),
        _ => std::borrow::Cow::Borrowed(agent_id),
    }
}

fn ensure_column(conn: &Connection, table: &str, column: &str, sql_type: &str) -> Result<()> {
    let pragma = format!("PRAGMA table_info({table})");
    let mut stmt = conn
        .prepare(&pragma)
        .with_context(|| format!("failed to inspect schema for {table}"))?;
    let columns = stmt
        .query_map([], |row| row.get::<_, String>(1))
        .with_context(|| format!("failed to read schema for {table}"))?;
    for existing in columns {
        if existing.context("failed to decode schema column name")? == column {
            return Ok(());
        }
    }

    let alter = format!("ALTER TABLE {table} ADD COLUMN {column} {sql_type}");
    conn.execute(&alter, [])
        .with_context(|| format!("failed to add {table}.{column}"))?;
    Ok(())
}

fn normalize_existing_agent_ids(conn: &mut Connection) -> Result<()> {
    let tx = conn
        .transaction()
        .context("failed to start inbox normalization transaction")?;

    let mut message_stmt = tx
        .prepare("SELECT id, to_agent FROM messages")
        .context("failed to prepare inbox message normalization query")?;
    let message_rows = message_stmt
        .query_map([], |row| {
            Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?))
        })
        .context("failed to query inbox messages for normalization")?;
    let mut message_updates = Vec::new();
    for row in message_rows {
        let (id, to_agent) = row.context("failed to decode inbox message normalization row")?;
        let normalized = normalize_agent_id(&to_agent);
        if normalized.as_ref() != to_agent {
            message_updates.push((id, normalized.into_owned()));
        }
    }
    drop(message_stmt);
    for (id, normalized_to_agent) in message_updates {
        tx.execute(
            "UPDATE messages SET to_agent = ?1 WHERE id = ?2",
            params![normalized_to_agent, id],
        )
        .context("failed to normalize inbox message recipient")?;
    }

    #[derive(Default)]
    struct MetaAccumulator {
        last_check_inbox_at: Option<i64>,
        last_poke_at: Option<i64>,
        last_poke_message_id: Option<i64>,
        poke_backoff_secs: Option<i64>,
    }

    let mut meta_stmt = tx
        .prepare(
            "SELECT agent_id, last_check_inbox_at, last_poke_at, last_poke_message_id, poke_backoff_secs
             FROM agent_inbox_meta",
        )
        .context("failed to prepare inbox metadata normalization query")?;
    let meta_rows = meta_stmt
        .query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, Option<i64>>(1)?,
                row.get::<_, Option<i64>>(2)?,
                row.get::<_, Option<i64>>(3)?,
                row.get::<_, Option<i64>>(4)?,
            ))
        })
        .context("failed to query inbox metadata for normalization")?;
    let mut merged = std::collections::BTreeMap::<String, MetaAccumulator>::new();
    for row in meta_rows {
        let (agent_id, last_check_inbox_at, last_poke_at, last_poke_message_id, poke_backoff_secs) =
            row.context("failed to decode inbox metadata normalization row")?;
        let normalized_agent_id = normalize_agent_id(&agent_id).into_owned();
        let entry = merged.entry(normalized_agent_id).or_default();
        entry.last_check_inbox_at = max_option(entry.last_check_inbox_at, last_check_inbox_at);
        entry.last_poke_at = max_option(entry.last_poke_at, last_poke_at);
        entry.last_poke_message_id = max_option(entry.last_poke_message_id, last_poke_message_id);
        entry.poke_backoff_secs = max_option(entry.poke_backoff_secs, poke_backoff_secs);
    }
    drop(meta_stmt);

    tx.execute("DELETE FROM agent_inbox_meta", [])
        .context("failed to clear inbox metadata before normalization rewrite")?;
    for (agent_id, meta) in merged {
        tx.execute(
            "INSERT INTO agent_inbox_meta (
                 agent_id,
                 last_check_inbox_at,
                 last_poke_at,
                 last_poke_message_id,
                 poke_backoff_secs
             ) VALUES (?1, ?2, ?3, ?4, ?5)",
            params![
                agent_id,
                meta.last_check_inbox_at,
                meta.last_poke_at,
                meta.last_poke_message_id,
                meta.poke_backoff_secs
            ],
        )
        .context("failed to rewrite normalized inbox metadata")?;
    }

    tx.commit()
        .context("failed to commit inbox normalization transaction")?;
    Ok(())
}

fn max_option(left: Option<i64>, right: Option<i64>) -> Option<i64> {
    match (left, right) {
        (Some(left), Some(right)) => Some(left.max(right)),
        (Some(left), None) => Some(left),
        (None, Some(right)) => Some(right),
        (None, None) => None,
    }
}

/// Remap legacy bare-branch `to_agent` values to `root`.
///
/// Old messages may carry `to_agent = "main"` (or "master", "parent") if they
/// were written before `delivery::canonical_parent_recipient` existed at the
/// write site. `normalize_agent_id` is a pure structural strip (last dot-segment)
/// and does not apply semantic remapping, so those rows are never drained by
/// `drain_unread("root")` — but `resolve_tab_name_for_agent` routes them to the
/// "TL" window anyway, producing a poke that root cannot satisfy.
///
/// This migration is idempotent: after it runs there are no "main"/"master"/"parent"
/// rows left, so subsequent runs are no-ops.
fn remap_legacy_branch_recipients(conn: &mut Connection) -> Result<()> {
    conn.execute_batch(
        "DELETE FROM agent_inbox_meta WHERE agent_id IN ('main', 'master', 'parent');
         UPDATE messages SET to_agent = 'root' WHERE to_agent IN ('main', 'master', 'parent');",
    )
    .context("failed to remap legacy branch recipients to root")
}

fn select_messages(
    conn: &Connection,
    sql: &str,
    agent_id: &str,
) -> Result<Vec<InboxMessageRecord>> {
    let mut stmt = conn
        .prepare(sql)
        .context("failed to prepare inbox message query")?;
    let rows = stmt
        .query_map(params![agent_id], |row| {
            Ok(InboxMessageRecord {
                id: row.get(0)?,
                from_agent: row.get(1)?,
                to_agent: row.get(2)?,
                content: row.get(3)?,
                summary: row.get(4)?,
                created_at: row.get(5)?,
                notified_at: row.get(6)?,
                read_at: row.get(7)?,
            })
        })
        .context("failed to query inbox messages")?;
    collect_rows(rows)
}

fn collect_rows<T, F>(rows: rusqlite::MappedRows<'_, F>) -> Result<Vec<T>>
where
    F: FnMut(&rusqlite::Row<'_>) -> rusqlite::Result<T>,
{
    rows.collect::<rusqlite::Result<Vec<_>>>()
        .context("failed to decode inbox rows")
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

    #[test]
    fn write_peek_and_drain_track_message_lifecycle() {
        let dir = tempfile::tempdir().unwrap();
        let store = InboxStore::open(dir.path()).unwrap();

        let id = store
            .write_message("root", "worker-1", "please check this", Some("check this"))
            .unwrap();
        assert!(id > 0);

        assert_eq!(store.unread_count("worker-1").unwrap(), 1);

        let first_peek = store.peek_unnotified("worker-1").unwrap();
        assert_eq!(first_peek.len(), 1);
        assert_eq!(first_peek[0].from_agent, "root");
        assert_eq!(first_peek[0].summary.as_deref(), Some("check this"));

        let second_peek = store.peek_unnotified("worker-1").unwrap();
        assert!(second_peek.is_empty());

        let drained = store.drain_unread("worker-1").unwrap();
        assert_eq!(drained.len(), 1);
        assert_eq!(drained[0].id, id);
        assert_eq!(store.unread_count("worker-1").unwrap(), 0);
        assert!(!store.has_unread("worker-1").unwrap());
        assert!(store.last_check_inbox_at("worker-1").unwrap().is_some());
    }

    #[test]
    fn agents_needing_poke_returns_unread_agents_with_stale_checks() {
        let dir = tempfile::tempdir().unwrap();
        let store = InboxStore::open(dir.path()).unwrap();

        store
            .write_message("root", "worker-1", "one", None)
            .unwrap();
        store
            .write_message("root", "worker-1", "two", None)
            .unwrap();
        store
            .write_message("root", "worker-2", "three", None)
            .unwrap();
        store.drain_unread("worker-2").unwrap();
        {
            let conn = store.connection().unwrap();
            conn.execute(
                "UPDATE messages SET created_at = ?1 WHERE to_agent = 'worker-1'",
                params![now_epoch_secs() - 301],
            )
            .unwrap();
        }

        let candidates = store.agents_needing_poke(300).unwrap();
        assert_eq!(candidates.len(), 1);
        assert_eq!(candidates[0].agent_id, "worker-1");
        assert_eq!(candidates[0].unread_count, 2);
    }

    #[test]
    fn branch_qualified_recipients_drain_as_bare_agent_names() {
        let dir = tempfile::tempdir().unwrap();
        let store = InboxStore::open(dir.path()).unwrap();

        let id = store
            .write_message(
                "root",
                "main.worker-1",
                "please check this",
                Some("check this"),
            )
            .unwrap();

        let drained = store.drain_unread("worker-1").unwrap();
        assert_eq!(drained.len(), 1);
        assert_eq!(drained[0].id, id);
        assert_eq!(drained[0].to_agent, "worker-1");
        assert!(!store.has_unread("main.worker-1").unwrap());
    }

    #[test]
    fn normalize_is_a_pure_branch_prefix_strip() {
        // The inbox does not do semantic recipient remapping (e.g. main -> root);
        // that lives at the notify_parent source. A bare AgentName round-trips
        // unchanged here.
        let dir = tempfile::tempdir().unwrap();
        let store = InboxStore::open(dir.path()).unwrap();

        store
            .write_message("worker-1", "root", "done", None)
            .unwrap();

        let drained = store.drain_unread("root").unwrap();
        assert_eq!(drained.len(), 1);
        assert_eq!(drained[0].to_agent, "root");
    }

    #[test]
    fn legacy_bare_branch_recipients_remapped_to_root_on_migrate() {
        let store = InboxStore::open_in_memory().unwrap();
        {
            let conn = store.connection().unwrap();
            conn.execute(
                "INSERT INTO messages (from_agent, to_agent, content, summary, created_at)
                 VALUES ('leaf-opencode', 'main', 'pr ready', NULL, 1)",
                [],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO messages (from_agent, to_agent, content, summary, created_at)
                 VALUES ('leaf-opencode', 'parent', 'done', NULL, 2)",
                [],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO agent_inbox_meta (agent_id, last_poke_at)
                 VALUES ('main', 100)",
                [],
            )
            .unwrap();
        }

        store.migrate().unwrap();

        let drained = store.drain_unread("root").unwrap();
        assert_eq!(drained.len(), 2);
        assert!(drained.iter().all(|m| m.to_agent == "root"));
        assert!(!store.has_unread("main").unwrap());
        // poke metadata for the bare branch name is dropped
        assert!(store.last_check_inbox_at("main").unwrap().is_none());
    }

    #[test]
    fn legacy_branch_qualified_rows_are_normalized_on_migrate() {
        let store = InboxStore::open_in_memory().unwrap();
        {
            let conn = store.connection().unwrap();
            conn.execute(
                "INSERT INTO messages (from_agent, to_agent, content, summary, created_at)
                 VALUES ('root', 'main.worker-1', 'hello', NULL, 1)",
                [],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO agent_inbox_meta (agent_id, last_check_inbox_at)
                 VALUES ('main.worker-1', 5)",
                [],
            )
            .unwrap();
        }

        store.migrate().unwrap();

        let drained = store.drain_unread("worker-1").unwrap();
        assert_eq!(drained.len(), 1);
        assert_eq!(drained[0].to_agent, "worker-1");
        assert!(store
            .last_check_inbox_at("main.worker-1")
            .unwrap()
            .is_some());
    }

    #[test]
    fn inbox_poke_backoff_doubles_without_new_mail_and_resets_on_new_mail() {
        let dir = tempfile::tempdir().unwrap();
        let store = InboxStore::open(dir.path()).unwrap();

        let first_id = store
            .write_message("root", "worker-1", "one", None)
            .unwrap();
        {
            let conn = store.connection().unwrap();
            conn.execute(
                "UPDATE messages SET created_at = ?1 WHERE id = ?2",
                params![now_epoch_secs() - 31, first_id],
            )
            .unwrap();
        }

        let initial = store.agents_needing_poke(30).unwrap();
        assert_eq!(initial.len(), 1);
        assert_eq!(initial[0].newest_message_id, first_id);

        store.record_poke("worker-1", first_id, 30, 600).unwrap();
        let after_first_poke = store.agents_needing_poke(30).unwrap();
        assert!(after_first_poke.is_empty());

        {
            let conn = store.connection().unwrap();
            conn.execute(
                "UPDATE agent_inbox_meta SET last_poke_at = ?1 WHERE agent_id = 'worker-1'",
                params![now_epoch_secs() - 31],
            )
            .unwrap();
        }

        let same_mail = store.agents_needing_poke(30).unwrap();
        assert_eq!(same_mail.len(), 1);
        store.record_poke("worker-1", first_id, 30, 600).unwrap();

        {
            let conn = store.connection().unwrap();
            let backoff: Option<i64> = conn
                .query_row(
                    "SELECT poke_backoff_secs FROM agent_inbox_meta WHERE agent_id = 'worker-1'",
                    [],
                    |row| row.get(0),
                )
                .unwrap();
            assert_eq!(backoff, Some(60));
            conn.execute(
                "UPDATE agent_inbox_meta SET last_poke_at = ?1 WHERE agent_id = 'worker-1'",
                params![now_epoch_secs() - 61],
            )
            .unwrap();
        }

        let second_due = store.agents_needing_poke(30).unwrap();
        assert_eq!(second_due.len(), 1);

        let second_id = store
            .write_message("root", "worker-1", "two", None)
            .unwrap();
        {
            let conn = store.connection().unwrap();
            conn.execute(
                "UPDATE messages SET created_at = ?1 WHERE id = ?2",
                params![now_epoch_secs() - 31, second_id],
            )
            .unwrap();
        }

        let reset_due = store.agents_needing_poke(30).unwrap();
        assert_eq!(reset_due.len(), 1);
        assert_eq!(reset_due[0].newest_message_id, second_id);
        store.record_poke("worker-1", second_id, 30, 600).unwrap();

        let conn = store.connection().unwrap();
        let backoff: Option<i64> = conn
            .query_row(
                "SELECT poke_backoff_secs FROM agent_inbox_meta WHERE agent_id = 'worker-1'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(backoff, Some(30));
    }

    #[test]
    fn open_uses_project_exo_inbox_db_path() {
        let dir = tempfile::tempdir().unwrap();
        let store = InboxStore::open(dir.path()).unwrap();

        assert_eq!(store.db_path(), &dir.path().join(".exo/inbox.db"));
        assert!(store.db_path().exists());
    }

    #[test]
    fn clear_all_removes_messages_and_metadata() {
        let store = InboxStore::open_in_memory().unwrap();
        store
            .write_message("root", "worker-1", "please check this", None)
            .unwrap();
        store.drain_unread("worker-1").unwrap();

        store.clear_all().unwrap();

        let conn = store.connection().unwrap();
        let message_count: i64 = conn
            .query_row("SELECT COUNT(*) FROM messages", [], |row| row.get(0))
            .unwrap();
        let metadata_count: i64 = conn
            .query_row("SELECT COUNT(*) FROM agent_inbox_meta", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(message_count, 0);
        assert_eq!(metadata_count, 0);
        drop(conn);

        store
            .write_message("root", "worker-1", "schema still works", None)
            .unwrap();
        assert_eq!(store.drain_unread("worker-1").unwrap().len(), 1);
    }
}
