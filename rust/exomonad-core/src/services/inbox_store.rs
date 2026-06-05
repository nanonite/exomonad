use anyhow::{Context, Result};
use rusqlite::{params, Connection};
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

    pub fn write_message(
        &self,
        from_agent: &str,
        to_agent: &str,
        content: &str,
        summary: Option<&str>,
    ) -> Result<i64> {
        let created_at = now_epoch_secs();
        let conn = self.connection()?;
        conn.execute(
            "INSERT INTO messages (from_agent, to_agent, content, summary, created_at) VALUES (?1, ?2, ?3, ?4, ?5)",
            params![from_agent, to_agent, content, summary, created_at],
        )
        .context("failed to insert inbox message")?;
        Ok(conn.last_insert_rowid())
    }

    pub fn peek_unnotified(&self, agent_id: &str) -> Result<Vec<InboxMessageRecord>> {
        let now = now_epoch_secs();
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
            agent_id,
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
        Ok(messages)
    }

    pub fn drain_unread(&self, agent_id: &str) -> Result<Vec<InboxMessageRecord>> {
        let now = now_epoch_secs();
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
            agent_id,
        )?;
        tx.execute(
            "UPDATE messages SET read_at = ?1 WHERE to_agent = ?2 AND read_at IS NULL",
            params![now, agent_id],
        )
        .context("failed to mark inbox messages as read")?;
        tx.execute(
            "INSERT INTO agent_inbox_meta (agent_id, last_check_inbox_at)
             VALUES (?1, ?2)
             ON CONFLICT(agent_id) DO UPDATE SET last_check_inbox_at = excluded.last_check_inbox_at",
            params![agent_id, now],
        )
        .context("failed to update inbox metadata")?;
        tx.commit()
            .context("failed to commit inbox drain transaction")?;
        Ok(messages)
    }

    pub fn agents_needing_poke(&self, threshold_secs: u64) -> Result<Vec<InboxPokeCandidate>> {
        let stale_before =
            now_epoch_secs().saturating_sub(i64::try_from(threshold_secs).unwrap_or(i64::MAX));
        let conn = self.connection()?;
        let mut stmt = conn
            .prepare(
                "SELECT m.to_agent, COUNT(*) AS unread_count
                 FROM messages m
                 LEFT JOIN agent_inbox_meta meta ON meta.agent_id = m.to_agent
                 WHERE m.read_at IS NULL
                   AND (meta.last_check_inbox_at IS NULL OR meta.last_check_inbox_at <= ?1)
                 GROUP BY m.to_agent
                 ORDER BY m.to_agent ASC",
            )
            .context("failed to prepare inbox poke query")?;
        let rows = stmt
            .query_map(params![stale_before], |row| {
                let count: i64 = row.get(1)?;
                Ok(InboxPokeCandidate {
                    agent_id: row.get(0)?,
                    unread_count: usize::try_from(count).unwrap_or(usize::MAX),
                })
            })
            .context("failed to query inbox poke candidates")?;
        collect_rows(rows)
    }

    pub fn has_unread(&self, agent_id: &str) -> Result<bool> {
        let conn = self.connection()?;
        let count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM messages WHERE to_agent = ?1 AND read_at IS NULL",
                params![agent_id],
                |row| row.get(0),
            )
            .context("failed to query unread inbox count")?;
        Ok(count > 0)
    }

    pub fn last_check_inbox_at(&self, agent_id: &str) -> Result<Option<i64>> {
        let conn = self.connection()?;
        let mut stmt = conn
            .prepare("SELECT last_check_inbox_at FROM agent_inbox_meta WHERE agent_id = ?1")
            .context("failed to prepare inbox metadata query")?;
        let mut rows = stmt
            .query(params![agent_id])
            .context("failed to query inbox metadata")?;
        match rows.next().context("failed to read inbox metadata row")? {
            Some(row) => row
                .get(0)
                .context("failed to decode inbox metadata timestamp"),
            None => Ok(None),
        }
    }

    fn migrate(&self) -> Result<()> {
        let conn = self.connection()?;
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
               last_check_inbox_at INTEGER
             );",
        )
        .context("failed to migrate inbox database")?;
        Ok(())
    }

    fn connection(&self) -> Result<MutexGuard<'_, Connection>> {
        self.conn
            .lock()
            .map_err(|_| anyhow::anyhow!("inbox database mutex poisoned"))
    }
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

        let first_peek = store.peek_unnotified("worker-1").unwrap();
        assert_eq!(first_peek.len(), 1);
        assert_eq!(first_peek[0].from_agent, "root");
        assert_eq!(first_peek[0].summary.as_deref(), Some("check this"));

        let second_peek = store.peek_unnotified("worker-1").unwrap();
        assert!(second_peek.is_empty());

        let drained = store.drain_unread("worker-1").unwrap();
        assert_eq!(drained.len(), 1);
        assert_eq!(drained[0].id, id);
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

        let candidates = store.agents_needing_poke(300).unwrap();
        assert_eq!(candidates.len(), 1);
        assert_eq!(candidates[0].agent_id, "worker-1");
        assert_eq!(candidates[0].unread_count, 2);
    }

    #[test]
    fn open_uses_project_exo_inbox_db_path() {
        let dir = tempfile::tempdir().unwrap();
        let store = InboxStore::open(dir.path()).unwrap();

        assert_eq!(store.db_path(), &dir.path().join(".exo/inbox.db"));
        assert!(store.db_path().exists());
    }
}
