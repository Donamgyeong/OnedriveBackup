import json
import logging
import time
import uuid
from http.client import HTTPException

from .db import encode
from .graph import GraphError, segment
from .storage import StorageError, digest
from .retention import Retention


log = logging.getLogger(__name__)


def content_tag(item):
    # Type prefix avoids accidental equivalence between eTag and cTag values.
    if item.get("cTag"):
        return "c:" + item["cTag"]
    if item.get("eTag"):
        return "e:" + item["eTag"]
    return None


def kind(item):
    if "deleted" in item:
        return "deleted"
    if "remoteItem" in item or "package" in item:
        return "special"
    if "folder" in item:
        return "folder"
    if "file" in item:
        return "file"
    return "special"


class Engine:
    def __init__(self, config, db, graph, storage):
        self.config, self.db, self.graph, self.storage = config, db, graph, storage

    def discover(self):
        # Only declare disappeared accounts after a complete successful enumeration.
        seen = set()
        errors = 0
        for page in self.graph.pages("/users?$select=id,displayName,userPrincipalName,userType,accountEnabled"):
            for user in page["value"]:
                user_id = user["id"]
                seen.add(user_id)
                excluded = user.get("userType") == "Guest" and self.config.guest_account_policy == "exclude"
                with self.db.transaction() as conn:
                    conn.execute("INSERT OR REPLACE INTO accounts VALUES (?,?,?,?)",
                                 (user_id, encode(user), "excluded" if excluded else "active", time.time()))
                    conn.execute("UPDATE drives SET status=? WHERE user_id=?",
                                 ("excluded" if excluded else "discovery_pending", user_id))
                if excluded:
                    continue
                try:
                    discovered = []
                    for drives_page in self.graph.pages(f"/users/{segment(user_id)}/drives"):
                        discovered.extend(drives_page["value"])
                    with self.db.transaction() as conn:
                        conn.execute("UPDATE drives SET status='unavailable' WHERE user_id=?", (user_id,))
                        for drive in discovered:
                            # This program backs up user-owned OneDrive business drives.
                            if drive.get("driveType") != "business":
                                continue
                            conn.execute("""INSERT INTO drives(drive_id,user_id,status) VALUES (?,?,'active')
                                ON CONFLICT(drive_id) DO UPDATE SET user_id=excluded.user_id,status='active'""",
                                         (drive["id"], user_id))
                        if not any(d.get("driveType") == "business" for d in discovered):
                            conn.execute("UPDATE accounts SET status='no_drive' WHERE user_id=?", (user_id,))
                except (GraphError, OSError, ValueError, KeyError) as exc:
                    errors += 1
                    with self.db.transaction() as conn:
                        conn.execute("UPDATE accounts SET status='error' WHERE user_id=?", (user_id,))
                        conn.execute("UPDATE drives SET status='discovery_error' WHERE user_id=?", (user_id,))
                    log.error("계정 드라이브 조회 실패 user=%s type=%s", user_id, type(exc).__name__)
        with self.db.transaction() as conn:
            for row in self.db.all("SELECT user_id FROM accounts"):
                if row["user_id"] not in seen:
                    conn.execute("UPDATE accounts SET status='not_seen' WHERE user_id=?", (row["user_id"],))
                    conn.execute("UPDATE drives SET status='account_not_seen' WHERE user_id=?", (row["user_id"],))
        return errors

    def scan(self, drive_id):
        drive = self.db.one("SELECT * FROM drives WHERE drive_id=?", (drive_id,))
        initial_url = f"/drives/{segment(drive_id)}/root/delta"
        with self.db.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO scans VALUES (?,?,?)",
                         (drive_id, drive["delta_link"] or initial_url, int(not drive["delta_link"])))
        reset = False
        while True:
            scan = self.db.one("SELECT * FROM scans WHERE drive_id=?", (drive_id,))
            try:
                page = self.graph.get(scan["next_link"])
            except GraphError as exc:
                if exc.status != 410 or reset:
                    raise
                # A fresh full enumeration is conservative; never delete backup objects.
                with self.db.transaction() as conn:
                    conn.execute("DELETE FROM scan_items WHERE drive_id=?", (drive_id,))
                    conn.execute("UPDATE scans SET next_link=?,full_scan=1 WHERE drive_id=?", (initial_url, drive_id))
                reset = True
                continue
            next_link, delta_link = page.get("@odata.nextLink"), page.get("@odata.deltaLink")
            if not next_link and not delta_link:
                raise ValueError("delta response missing continuation")
            with self.db.transaction() as conn:
                for item in page["value"]:
                    conn.execute("INSERT OR REPLACE INTO scan_items VALUES (?,?,?)", (drive_id, item["id"], encode(item)))
                if next_link:
                    conn.execute("UPDATE scans SET next_link=? WHERE drive_id=?", (next_link, drive_id))
                else:
                    self._finish_scan(drive_id, bool(scan["full_scan"]), delta_link)
            if not next_link:
                return

    def _finish_scan(self, drive_id, full_scan, delta_link):
        # Called inside one transaction: queued work and delta cursor commit together.
        conn = self.db.conn
        if full_scan:
            conn.execute("""UPDATE items SET deleted=1 WHERE drive_id=? AND item_id NOT IN
                (SELECT item_id FROM scan_items WHERE drive_id=?)""", (drive_id, drive_id))
        for row in conn.execute("SELECT * FROM scan_items WHERE drive_id=?", (drive_id,)).fetchall():
            item = json.loads(row["payload"])
            old = self.db.one("SELECT * FROM items WHERE drive_id=? AND item_id=?", (drive_id, item["id"]))
            if kind(item) == "deleted" and old:
                item = {**json.loads(old["payload"]), **item}
            conn.execute("""INSERT INTO items(drive_id,item_id,payload,deleted) VALUES (?,?,?,?)
                ON CONFLICT(drive_id,item_id) DO UPDATE SET payload=excluded.payload,deleted=excluded.deleted""",
                         (drive_id, item["id"], encode(item), int(kind(item) == "deleted")))
            if kind(item) == "file":
                stored = self.db.one("SELECT * FROM objects WHERE object_id=?", (old["object_id"],)) if old and old["object_id"] else None
                same = stored and content_tag(item) and content_tag(item) == content_tag(json.loads(stored["payload"]))
                if not same:
                    changed = not old or old["deleted"] or content_tag(item) != content_tag(json.loads(old["payload"]))
                    self.db.queue(drive_id, item["id"], reset=bool(changed))
                # An existing job may be repairing a missing/corrupt stored object.
                # Matching source tags must not cancel that repair or reset its limits.
            elif kind(item) == "special" and self.config.special_item_policy == "report":
                self.db.queue(drive_id, item["id"])
                conn.execute("UPDATE jobs SET state='failed',error='unsupported_item' WHERE drive_id=? AND item_id=?",
                             (drive_id, item["id"]))
            elif kind(item) != "deleted":
                conn.execute("DELETE FROM jobs WHERE drive_id=? AND item_id=?", (drive_id, item["id"]))
        # Include descendants of deleted folders, even if Graph returns only the folder.
        deleted = {r["item_id"] for r in self.db.all("SELECT item_id FROM items WHERE drive_id=? AND deleted=1", (drive_id,))}
        entries = self.db.all("SELECT item_id,payload FROM items WHERE drive_id=? AND deleted=0", (drive_id,))
        while True:
            descendants = {r["item_id"] for r in entries if json.loads(r["payload"]).get("parentReference", {}).get("id") in deleted}
            new = descendants - deleted
            if not new:
                break
            deleted.update(new)
        for item_id in deleted:
            conn.execute("UPDATE items SET deleted=1 WHERE drive_id=? AND item_id=?", (drive_id, item_id))
        # Retain an explicit gap if an unbacked file disappears before download.
        conn.execute("""UPDATE jobs SET state='failed',error='source_deleted_before_backup'
            WHERE drive_id=? AND EXISTS (SELECT 1 FROM items i WHERE
            i.drive_id=jobs.drive_id AND i.item_id=jobs.item_id AND i.deleted=1)""", (drive_id,))
        conn.execute("UPDATE drives SET delta_link=?,last_scan=? WHERE drive_id=?", (delta_link, time.time(), drive_id))
        conn.execute("DELETE FROM scans WHERE drive_id=?", (drive_id,))
        conn.execute("DELETE FROM scan_items WHERE drive_id=?", (drive_id,))

    def original_parts(self, drive_id, item_id):
        parts, seen = [], set()
        while item_id:
            if item_id in seen:
                raise ValueError("parent cycle")
            seen.add(item_id)
            row = self.db.one("SELECT payload FROM items WHERE drive_id=? AND item_id=?", (drive_id, item_id))
            if not row:
                break
            item = json.loads(row["payload"])
            if "root" in item:
                break
            parts.append(item.get("name", item_id))
            item_id = item.get("parentReference", {}).get("id")
        return list(reversed(parts))

    def repair_missing(self, verify_hash=False):
        self.storage.check()
        repaired = 0
        # Recover files expired by the former policy even when delta has no changes.
        for row in self.db.all("""SELECT i.* FROM items i LEFT JOIN objects o ON i.object_id=o.object_id
            WHERE o.object_id IS NULL"""):
            item = json.loads(row["payload"])
            if "file" not in item or "remoteItem" in item or "package" in item:
                continue
            with self.db.transaction():
                self.db.queue(row["drive_id"], row["item_id"], reset=False)
                if row["deleted"]:
                    self.db.conn.execute("UPDATE jobs SET state='failed',error='source_deleted_no_backup' WHERE drive_id=? AND item_id=?",
                                         (row["drive_id"], row["item_id"]))
            repaired += 1
        for row in self.db.all("""SELECT i.drive_id,i.item_id,i.deleted,o.* FROM items i JOIN objects o
            ON i.object_id=o.object_id WHERE NOT EXISTS
            (SELECT 1 FROM purge_jobs p WHERE p.resource_id=o.object_id AND p.state='pending')"""):
            exists = self.storage.exists(row["relative_path"], row["size"])
            valid = exists and (not verify_hash or digest(self.storage.path(row["relative_path"])) == row["sha256"])
            if not valid:
                with self.db.transaction():
                    self.db.queue(row["drive_id"], row["item_id"], reset=False)
                    if row["deleted"]:
                        self.db.conn.execute("UPDATE jobs SET state='failed',error='source_deleted_no_backup' WHERE drive_id=? AND item_id=?",
                                             (row["drive_id"], row["item_id"]))
                repaired += 1
        return repaired

    def backup_job(self, job):
        drive_id, item_id = job["drive_id"], job["item_id"]
        before = self.graph.metadata(drive_id, item_id)
        if kind(before) != "file":
            raise ValueError("source_not_regular_file")
        expected = content_tag(before)
        if not expected:
            raise ValueError("source_version_tag_missing")
        with self.graph.download(before) as stream:
            object_id, relative, sha256 = self.storage.put(stream, before["size"])
        after = self.graph.metadata(drive_id, item_id)
        if kind(after) != "file" or content_tag(after) != expected or after.get("size") != before["size"]:
            raise ValueError("source_changed_during_download")
        remote_hash = before.get("file", {}).get("hashes", {}).get("sha256Hash")
        if remote_hash and remote_hash.lower() != sha256:
            raise ValueError("source_checksum_mismatch")
        created = time.time()
        drive = self.db.one("SELECT user_id FROM drives WHERE drive_id=?", (drive_id,))
        manifest = {"object_id": object_id, "tenant_id": self.config.tenant_id,
                    "user_id": drive["user_id"], "drive_id": drive_id, "item_id": item_id,
                    "relative_path": relative, "sha256": sha256, "size": before["size"],
                    "metadata": json.loads(encode(after)), "original_parts": self.original_parts(drive_id, item_id),
                    "created": created, "verification": "mount_readback_only"}
        self.storage.manifest(object_id, manifest)
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO objects VALUES (?,?,?,?,?,?,?,?,?)",
                         (object_id, drive_id, item_id, relative, sha256, before["size"], encode(after), created, "mount_readback_only"))
            conn.execute("UPDATE items SET object_id=? WHERE drive_id=? AND item_id=?", (object_id, drive_id, item_id))
            conn.execute("DELETE FROM expired_items WHERE drive_id=? AND item_id=?", (drive_id, item_id))
            conn.execute("DELETE FROM jobs WHERE job_id=?", (job["job_id"],))

    def process_jobs(self):
        # Sequential workers intentionally avoid concurrent SQLite/mount mutations.
        jobs = self.db.all("""SELECT j.* FROM jobs j JOIN drives d ON j.drive_id=d.drive_id
            WHERE j.state='pending' AND j.retry_at<=? AND d.status='active'
            AND NOT EXISTS (SELECT 1 FROM items i JOIN purge_jobs p ON p.resource_id=i.object_id
                WHERE i.drive_id=j.drive_id AND i.item_id=j.item_id AND p.state='pending')
            ORDER BY j.job_id""", (time.time(),))
        for job in jobs:
            self.storage.check()
            with self.db.transaction() as conn:
                conn.execute("UPDATE jobs SET state='running' WHERE job_id=?", (job["job_id"],))
            try:
                self.backup_job(job)
            except (GraphError, StorageError, OSError, HTTPException, ValueError, KeyError) as exc:
                attempts = job["attempts"] + 1
                error = str(exc) if isinstance(exc, (GraphError, StorageError)) else type(exc).__name__
                with self.db.transaction() as conn:
                    conn.execute("UPDATE jobs SET state=?,attempts=?,retry_at=?,error=? WHERE job_id=?",
                                 ("failed" if attempts >= self.config.max_attempts else "pending", attempts,
                                  time.time() + min(3600, self.config.retry_base_seconds * 2 ** min(attempts - 1, 10)),
                                  error, job["job_id"]))
                log.error("파일 처리 실패 drive=%s item=%s error=%s", job["drive_id"], job["item_id"], error)
                if isinstance(exc, (StorageError, OSError)):
                    # Storage failure should not consume every queued job's attempts.
                    raise

    def run(self):
        self.storage.check(check_space=False)
        with self.db.transaction() as conn:
            run_id = conn.execute("INSERT INTO runs(started,status) VALUES (?,'running')", (time.time(),)).lastrowid
        errors, fatal = 0, False
        retention = {"completed": 0, "failed": 0}
        try:
            retention = Retention(self.config, self.db, self.storage).prune()
            errors += retention["failed"]
            self.storage.check()
            errors += self.discover()
            for drive in self.db.all("SELECT * FROM drives WHERE status='active'"):
                try:
                    self.scan(drive["drive_id"])
                except (GraphError, ValueError, KeyError) as exc:
                    errors += 1
                    log.error("변경 조회 실패 drive=%s type=%s", drive["drive_id"], type(exc).__name__)
            self.repair_missing()
            self.process_jobs()
        except (GraphError, StorageError, OSError, ValueError, KeyError) as exc:
            fatal = True
            log.error("실행 실패 type=%s", type(exc).__name__)
        remaining = self.db.one("SELECT COUNT(*) AS n FROM jobs")["n"]
        status = "failed" if fatal else ("partial" if errors or remaining else "success")
        with self.db.transaction() as conn:
            if not fatal:
                conn.execute("""UPDATE drives SET last_backup=last_scan WHERE status='active' AND last_scan IS NOT NULL
                    AND NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.drive_id=drives.drive_id)
                    AND NOT EXISTS (SELECT 1 FROM scans WHERE scans.drive_id=drives.drive_id)""")
            conn.execute("UPDATE runs SET finished=?,status=?,summary=? WHERE run_id=?",
                         (time.time(), status, json.dumps({"errors": errors, "remaining_jobs": remaining}), run_id))
        # Safe SQLite snapshot includes cursor, jobs, and latest metadata paths.
        if not fatal:
            try:
                self.storage.check()
                relative = f"catalogs/{run_id}-{uuid.uuid4().hex}.sqlite"
                target = self.storage.path(relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                self.db.snapshot(target)
                self.storage.manifest("catalog-" + uuid.uuid4().hex,
                                      {"kind": "catalog", "relative_path": relative, "sha256": digest(target),
                                       "run_id": run_id, "created": time.time(), "verification": "mount_readback_only"})
            except (StorageError, OSError) as exc:
                status = "partial"
                log.error("DB 백업 실패 type=%s", type(exc).__name__)
                with self.db.transaction() as conn:
                    conn.execute("UPDATE runs SET status='partial' WHERE run_id=?", (run_id,))
        return {"run_id": run_id, "status": status, "remaining_jobs": remaining,
                "retention": retention,
                "verification": "mount_readback_only"}
