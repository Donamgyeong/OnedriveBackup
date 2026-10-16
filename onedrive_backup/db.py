import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS accounts (
 user_id TEXT PRIMARY KEY, profile TEXT NOT NULL, status TEXT NOT NULL, seen REAL NOT NULL);
CREATE TABLE IF NOT EXISTS drives (
 drive_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, delta_link TEXT,
 last_scan REAL, last_backup REAL, status TEXT NOT NULL DEFAULT 'new');
CREATE TABLE IF NOT EXISTS scans (
 drive_id TEXT PRIMARY KEY, next_link TEXT NOT NULL, full_scan INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS scan_items (
 drive_id TEXT NOT NULL, item_id TEXT NOT NULL, payload TEXT NOT NULL,
 PRIMARY KEY(drive_id,item_id));
CREATE TABLE IF NOT EXISTS items (
 drive_id TEXT NOT NULL, item_id TEXT NOT NULL, payload TEXT NOT NULL,
 deleted INTEGER NOT NULL DEFAULT 0, object_id TEXT,
 PRIMARY KEY(drive_id,item_id));
CREATE TABLE IF NOT EXISTS jobs (
 job_id INTEGER PRIMARY KEY AUTOINCREMENT, drive_id TEXT NOT NULL, item_id TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 retry_at REAL NOT NULL DEFAULT 0, error TEXT,
 UNIQUE(drive_id,item_id));
CREATE TABLE IF NOT EXISTS objects (
 object_id TEXT PRIMARY KEY, drive_id TEXT NOT NULL, item_id TEXT NOT NULL,
 relative_path TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
 payload TEXT NOT NULL, created REAL NOT NULL, verification TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
 run_id INTEGER PRIMARY KEY AUTOINCREMENT, started REAL NOT NULL, finished REAL,
 status TEXT NOT NULL, summary TEXT);
CREATE INDEX IF NOT EXISTS jobs_due ON jobs(state,retry_at);
CREATE TABLE IF NOT EXISTS expired_items (
 drive_id TEXT NOT NULL, item_id TEXT NOT NULL, content_tag TEXT,
 PRIMARY KEY(drive_id,item_id));
CREATE TABLE IF NOT EXISTS purge_jobs (
 resource_id TEXT PRIMARY KEY, kind TEXT NOT NULL, relative_path TEXT NOT NULL,
 manifest_path TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
 requested REAL NOT NULL, completed REAL, error TEXT);
CREATE TABLE IF NOT EXISTS browse_files (
 drive_id TEXT NOT NULL, item_id TEXT NOT NULL, relative_path TEXT,
 object_id TEXT, sha256 TEXT, pending TEXT, error TEXT, size INTEGER, mtime_ns INTEGER,
 PRIMARY KEY(drive_id,item_id));
CREATE TABLE IF NOT EXISTS browse_cleanup (
 relative_path TEXT PRIMARY KEY, object_id TEXT NOT NULL, sha256 TEXT NOT NULL);
"""


class Database:
    def __init__(self, path, tenant_id, backup_root):
        if sqlite3.sqlite_version_info < (3, 24, 0):
            raise sqlite3.NotSupportedError("SQLite 3.24.0 or newer is required")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        browse_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(browse_files)")}
        if "size" not in browse_columns:
            self.conn.execute("ALTER TABLE browse_files ADD COLUMN size INTEGER")
        if "mtime_ns" not in browse_columns:
            self.conn.execute("ALTER TABLE browse_files ADD COLUMN mtime_ns INTEGER")
        for key, value in (("tenant_id", tenant_id), ("backup_root", str(Path(backup_root).resolve())), ("schema_version", "1")):
            row = self.one("SELECT value FROM settings WHERE key=?", (key,))
            if row and row["value"] != value:
                self.conn.close()
                raise ValueError(f"DB의 {key}가 설정과 다릅니다")
            self.conn.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", (key, value))
        self.conn.execute("UPDATE jobs SET state='pending' WHERE state='running'")
        self.conn.execute("UPDATE runs SET status='interrupted',finished=? WHERE status='running'", (time.time(),))
        self.conn.commit()

    def one(self, sql, args=()):
        return self.conn.execute(sql, args).fetchone()

    def all(self, sql, args=()):
        return self.conn.execute(sql, args).fetchall()

    @contextmanager
    def transaction(self):
        with self.conn:
            yield self.conn

    def close(self):
        self.conn.close()

    def queue(self, drive_id, item_id, reset=True):
        conflict = ("DO UPDATE SET state='pending',attempts=0,retry_at=0,error=NULL"
                    if reset else "DO NOTHING")
        self.conn.execute("INSERT INTO jobs(drive_id,item_id) VALUES (?,?) "
                          "ON CONFLICT(drive_id,item_id) " + conflict, (drive_id, item_id))

    def snapshot(self, path):
        destination = sqlite3.connect(path)
        try:
            self.conn.backup(destination)
            # Make the catalog self-contained, without persistent WAL sidecars.
            destination.execute("PRAGMA journal_mode=DELETE")
        finally:
            destination.close()


def clean_metadata(value):
    """Do not persist temporary pre-authenticated download links."""
    if isinstance(value, dict):
        return {k: clean_metadata(v) for k, v in value.items() if k != "@microsoft.graph.downloadUrl"}
    if isinstance(value, list):
        return [clean_metadata(v) for v in value]
    return value


def encode(value):
    return json.dumps(clean_metadata(value), ensure_ascii=False, sort_keys=True)
