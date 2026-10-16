import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPException

from .db import encode
from .graph import GraphError, segment
from .storage import StorageError, digest, storage_error_detail
from .retention import Retention
from .browse import BrowsePublisher


log = logging.getLogger(__name__)


class DownloadProgress:
    """Thread-safe, log-friendly aggregate download progress."""

    def __init__(self, total_files, total_bytes, interval_seconds=1.0):
        self.total_files = total_files
        self.total_bytes = total_bytes
        self.interval_seconds = interval_seconds
        self.transferred_bytes = 0
        self.processed_files = 0
        self.successful_files = 0
        self.failed_files = 0
        self.last_report = time.monotonic()
        self.lock = threading.Lock()

    def start(self):
        log.info("파일 동기화 시작 files=%s bytes=%s", self.total_files, self.total_bytes)

    def advance(self, count):
        should_report = False
        with self.lock:
            self.transferred_bytes += count
            now = time.monotonic()
            if now - self.last_report >= self.interval_seconds:
                self.last_report = now
                should_report = True
                values = self._values()
        if should_report:
            self._report(values)

    def finish_file(self, successful):
        with self.lock:
            self.processed_files += 1
            if successful:
                self.successful_files += 1
            else:
                self.failed_files += 1
            self.last_report = time.monotonic()
            values = self._values()
        self._report(values)

    def _values(self):
        if self.total_bytes:
            percent = min(100.0, self.transferred_bytes * 100.0 / self.total_bytes)
        elif self.total_files:
            percent = self.processed_files * 100.0 / self.total_files
        else:
            percent = 100.0
        return (self.processed_files, self.total_files, self.successful_files, self.failed_files,
                self.transferred_bytes, self.total_bytes, percent)

    @staticmethod
    def _report(values):
        log.info("파일 동기화 진행 files=%s/%s success=%s failed=%s bytes=%s/%s percent=%.1f%%", *values)


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
        errors = processed = excluded_count = no_drive_count = drive_count = 0
        last_progress = 0.0
        log.info("사용자 및 OneDrive 검색 시작")
        for page in self.graph.pages("/users?$select=id,displayName,userPrincipalName,userType,accountEnabled"):
            for user in page["value"]:
                user_id = user["id"]
                seen.add(user_id)
                processed += 1
                now = time.monotonic()
                if processed == 1 or now - last_progress >= 1.0:
                    log.info("사용자 및 OneDrive 검색 진행 accounts=%s drives=%s errors=%s current_user=%s percent=unknown",
                             processed, drive_count, errors, user_id)
                    last_progress = now
                excluded = user.get("userType") == "Guest" and self.config.guest_account_policy == "exclude"
                with self.db.transaction() as conn:
                    conn.execute("INSERT OR REPLACE INTO accounts VALUES (?,?,?,?)",
                                 (user_id, encode(user), "excluded" if excluded else "active", time.time()))
                    conn.execute("UPDATE drives SET status=? WHERE user_id=?",
                                 ("excluded" if excluded else "discovery_pending", user_id))
                if excluded:
                    excluded_count += 1
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
                            drive_count += 1
                        if not any(d.get("driveType") == "business" for d in discovered):
                            conn.execute("UPDATE accounts SET status='no_drive' WHERE user_id=?", (user_id,))
                except GraphError as exc:
                    if exc.status == 404 or exc.code == "onedrive_not_provisioned":
                        with self.db.transaction() as conn:
                            conn.execute("UPDATE accounts SET status='no_drive' WHERE user_id=?", (user_id,))
                            conn.execute("UPDATE drives SET status='unavailable' WHERE user_id=?", (user_id,))
                        log.info("계정 OneDrive 없음 user=%s status=%s code=%s", user_id, exc.status, exc.code)
                        no_drive_count += 1
                        continue
                    errors += 1
                    with self.db.transaction() as conn:
                        conn.execute("UPDATE accounts SET status='error' WHERE user_id=?", (user_id,))
                        conn.execute("UPDATE drives SET status='discovery_error' WHERE user_id=?", (user_id,))
                    log.error("계정 드라이브 조회 실패 user=%s status=%s code=%s",
                              user_id, exc.status, exc.code)
                except (OSError, ValueError, KeyError) as exc:
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
        log.info("사용자 및 OneDrive 검색 완료 accounts=%s drives=%s excluded=%s no_drive=%s errors=%s",
                 processed, drive_count, excluded_count, no_drive_count, errors)
        return errors

    def scan(self, drive_id):
        drive = self.db.one("SELECT * FROM drives WHERE drive_id=?", (drive_id,))
        initial_url = f"/drives/{segment(drive_id)}/root/delta"
        pages = item_count = 0
        log.info("드라이브 변경 조회 시작 drive=%s mode=%s", drive_id,
                 "incremental" if drive["delta_link"] else "full")
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
                log.warning("드라이브 delta 재동기화 drive=%s reason=cursor_expired", drive_id)
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
            pages += 1
            item_count += len(page["value"])
            log.info("드라이브 변경 조회 진행 drive=%s pages=%s items=%s percent=%s",
                     drive_id, pages, item_count, "100.0" if not next_link else "unknown")
            if not next_link:
                log.info("드라이브 변경 조회 완료 drive=%s pages=%s items=%s", drive_id, pages, item_count)
                return

    def _finish_scan(self, drive_id, full_scan, delta_link):
        # Called inside one transaction: queued work and delta cursor commit together.
        conn = self.db.conn
        if full_scan:
            conn.execute("""UPDATE items SET deleted=1 WHERE drive_id=? AND item_id NOT IN
                (SELECT item_id FROM scan_items WHERE drive_id=?)""", (drive_id, drive_id))
        rows = conn.execute("SELECT * FROM scan_items WHERE drive_id=?", (drive_id,)).fetchall()
        existing = {row["item_id"]: row for row in conn.execute("""SELECT
            i.item_id,i.payload,i.deleted,i.object_id,o.payload AS object_payload
            FROM items i LEFT JOIN objects o ON o.object_id=i.object_id
            WHERE i.drive_id=?""", (drive_id,)).fetchall()}
        total = len(rows)
        last_progress = 0.0
        log.info("드라이브 변경 확정 시작 drive=%s items=%s", drive_id, total)
        for index, row in enumerate(rows, 1):
            item = json.loads(row["payload"])
            old = existing.get(item["id"])
            if kind(item) == "deleted" and old:
                item = {**json.loads(old["payload"]), **item}
            conn.execute("""INSERT INTO items(drive_id,item_id,payload,deleted) VALUES (?,?,?,?)
                ON CONFLICT(drive_id,item_id) DO UPDATE SET payload=excluded.payload,deleted=excluded.deleted""",
                         (drive_id, item["id"], encode(item), int(kind(item) == "deleted")))
            if kind(item) == "file":
                stored_payload = old["object_payload"] if old else None
                same = (stored_payload and content_tag(item)
                        and content_tag(item) == content_tag(json.loads(stored_payload)))
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
            now = time.monotonic()
            if index == total or now - last_progress >= 1.0:
                log.info("드라이브 변경 확정 진행 drive=%s items=%s/%s percent=%.1f%%",
                         drive_id, index, total, index * 100.0 / total)
                last_progress = now
        # Include descendants of deleted folders, even if Graph returns only the folder.
        deleted = {r["item_id"] for r in self.db.all("SELECT item_id FROM items WHERE drive_id=? AND deleted=1", (drive_id,))}
        entries = self.db.all("SELECT item_id,payload FROM items WHERE drive_id=? AND deleted=0", (drive_id,))
        children = {}
        for entry in entries:
            parent_id = json.loads(entry["payload"]).get("parentReference", {}).get("id")
            children.setdefault(parent_id, []).append(entry["item_id"])
        pending_parents = list(deleted)
        while pending_parents:
            parent_id = pending_parents.pop()
            for item_id in children.get(parent_id, ()):
                if item_id not in deleted:
                    deleted.add(item_id)
                    pending_parents.append(item_id)
        if deleted:
            log.info("삭제 항목 계층 반영 drive=%s items=%s", drive_id, len(deleted))
        for item_id in deleted:
            conn.execute("UPDATE items SET deleted=1 WHERE drive_id=? AND item_id=?", (drive_id, item_id))
        # Retain an explicit gap if an unbacked file disappears before download.
        conn.execute("""UPDATE jobs SET state='failed',error='source_deleted_before_backup'
            WHERE drive_id=? AND EXISTS (SELECT 1 FROM items i WHERE
            i.drive_id=jobs.drive_id AND i.item_id=jobs.item_id AND i.deleted=1)""", (drive_id,))
        conn.execute("UPDATE drives SET delta_link=?,last_scan=? WHERE drive_id=?", (delta_link, time.time(), drive_id))
        conn.execute("DELETE FROM scans WHERE drive_id=?", (drive_id,))
        conn.execute("DELETE FROM scan_items WHERE drive_id=?", (drive_id,))
        log.info("드라이브 변경 확정 완료 drive=%s items=%s", drive_id, total)

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
        missing_rows = self.db.all("""SELECT i.* FROM items i LEFT JOIN objects o ON i.object_id=o.object_id
            WHERE o.object_id IS NULL""")
        stored_rows = self.db.all("""SELECT i.drive_id,i.item_id,i.deleted,o.* FROM items i JOIN objects o
            ON i.object_id=o.object_id WHERE NOT EXISTS
            (SELECT 1 FROM purge_jobs p WHERE p.resource_id=o.object_id AND p.state='pending')""")
        total = len(missing_rows) + len(stored_rows)
        checked = 0
        last_progress = [0.0]

        def report_progress():
            now = time.monotonic()
            if checked not in (0, total) and now - last_progress[0] < 1.0:
                return
            percent = checked * 100.0 / total if total else 100.0
            log.info("백업 파일 검사 진행 files=%s/%s repaired=%s percent=%.1f%%",
                     checked, total, repaired, percent)
            last_progress[0] = now

        if not total:
            report_progress()
        # Recover files expired by the former policy even when delta has no changes.
        for row in missing_rows:
            item = json.loads(row["payload"])
            if "file" in item and "remoteItem" not in item and "package" not in item:
                with self.db.transaction():
                    self.db.queue(row["drive_id"], row["item_id"], reset=False)
                    if row["deleted"]:
                        self.db.conn.execute("UPDATE jobs SET state='failed',error='source_deleted_no_backup' WHERE drive_id=? AND item_id=?",
                                             (row["drive_id"], row["item_id"]))
                repaired += 1
            checked += 1
            report_progress()
        for row in stored_rows:
            exists = self.storage.exists(row["relative_path"], row["size"])
            valid = exists and (not verify_hash or digest(self.storage.path(row["relative_path"])) == row["sha256"])
            if not valid:
                with self.db.transaction():
                    self.db.queue(row["drive_id"], row["item_id"], reset=False)
                    if row["deleted"]:
                        self.db.conn.execute("UPDATE jobs SET state='failed',error='source_deleted_no_backup' WHERE drive_id=? AND item_id=?",
                                             (row["drive_id"], row["item_id"]))
                repaired += 1
            checked += 1
            report_progress()
        return repaired

    def _download_job(self, job, user_id, original_parts, progress=None):
        drive_id, item_id = job["drive_id"], job["item_id"]
        started = time.perf_counter()
        before = self.graph.metadata(drive_id, item_id)
        if kind(before) != "file":
            raise ValueError("source_not_regular_file")
        expected = content_tag(before)
        if not expected:
            raise ValueError("source_version_tag_missing")
        with self.graph.download(before) as stream:
            object_id, relative, sha256 = self.storage.put(stream, before["size"], progress=progress)
        after = self.graph.metadata(drive_id, item_id)
        if kind(after) != "file" or content_tag(after) != expected or after.get("size") != before["size"]:
            raise ValueError("source_changed_during_download")
        remote_hash = before.get("file", {}).get("hashes", {}).get("sha256Hash")
        if remote_hash and remote_hash.lower() != sha256:
            raise ValueError("source_checksum_mismatch")
        created = time.time()
        manifest = {"object_id": object_id, "tenant_id": self.config.tenant_id,
                    "user_id": user_id, "drive_id": drive_id, "item_id": item_id,
                    "relative_path": relative, "sha256": sha256, "size": before["size"],
                    "metadata": json.loads(encode(after)), "original_parts": original_parts,
                    "created": created, "verification": "mount_readback_only"}
        self.storage.manifest(object_id, manifest)
        return {"object_id": object_id, "relative": relative, "sha256": sha256,
                "size": before["size"], "metadata": after, "created": created,
                "elapsed": max(time.perf_counter() - started, 0.000001)}

    def _commit_backup(self, job, result):
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO objects VALUES (?,?,?,?,?,?,?,?,?)",
                         (result["object_id"], job["drive_id"], job["item_id"], result["relative"],
                          result["sha256"], result["size"], encode(result["metadata"]), result["created"],
                          "mount_readback_only"))
            conn.execute("UPDATE items SET object_id=? WHERE drive_id=? AND item_id=?",
                         (result["object_id"], job["drive_id"], job["item_id"]))
            conn.execute("DELETE FROM expired_items WHERE drive_id=? AND item_id=?",
                         (job["drive_id"], job["item_id"]))
            conn.execute("DELETE FROM jobs WHERE job_id=?", (job["job_id"],))

    def backup_job(self, job):
        drive = self.db.one("SELECT user_id FROM drives WHERE drive_id=?", (job["drive_id"],))
        result = self._download_job(dict(job), drive["user_id"],
                                    self.original_parts(job["drive_id"], job["item_id"]))
        self._commit_backup(job, result)
        return result

    def _record_job_failure(self, job, exc):
        attempts = job["attempts"] + 1
        error = str(exc) if isinstance(exc, (GraphError, StorageError)) else type(exc).__name__
        with self.db.transaction() as conn:
            conn.execute("UPDATE jobs SET state=?,attempts=?,retry_at=?,error=? WHERE job_id=?",
                         ("failed" if attempts >= self.config.max_attempts else "pending", attempts,
                          time.time() + min(3600, self.config.retry_base_seconds * 2 ** min(attempts - 1, 10)),
                          error, job["job_id"]))
        log.error("파일 처리 실패 drive=%s item=%s error=%s", job["drive_id"], job["item_id"], error)

    def process_jobs(self):
        jobs = [dict(row) for row in self.db.all("""SELECT j.* FROM jobs j JOIN drives d ON j.drive_id=d.drive_id
            WHERE j.state='pending' AND j.retry_at<=? AND d.status='active'
            AND NOT EXISTS (SELECT 1 FROM items i JOIN purge_jobs p ON p.resource_id=i.object_id
                WHERE i.drive_id=j.drive_id AND i.item_id=j.item_id AND p.state='pending')
            ORDER BY j.job_id""", (time.time(),))]
        workers = min(self.config.download_concurrency, len(jobs))
        if not workers:
            log.info("파일 다운로드 대상 없음")
            return
        total_bytes = 0
        for job in jobs:
            item = self.db.one("SELECT payload FROM items WHERE drive_id=? AND item_id=?",
                               (job["drive_id"], job["item_id"]))
            if item:
                size = json.loads(item["payload"]).get("size", 0)
                if type(size) is int and size > 0:
                    total_bytes += size
        progress = DownloadProgress(len(jobs), total_bytes)
        progress.start()
        self.storage.check()
        # Worker threads perform network and unique-file I/O only. SQLite remains on this thread.
        with ThreadPoolExecutor(max_workers=workers) as executor:
            for offset in range(0, len(jobs), workers):
                batch = jobs[offset:offset + workers]
                submitted = []
                fatal = None
                for job in batch:
                    try:
                        drive = self.db.one("SELECT user_id FROM drives WHERE drive_id=?", (job["drive_id"],))
                        original_parts = self.original_parts(job["drive_id"], job["item_id"])
                        with self.db.transaction() as conn:
                            conn.execute("UPDATE jobs SET state='running' WHERE job_id=?", (job["job_id"],))
                        future = executor.submit(self._download_job, job, drive["user_id"], original_parts,
                                                 progress.advance)
                        submitted.append((job, future))
                    except (GraphError, StorageError, OSError, HTTPException, ValueError, KeyError) as exc:
                        self._record_job_failure(job, exc)
                        progress.finish_file(False)
                        if isinstance(exc, (StorageError, OSError)):
                            fatal = exc
                            break
                for job, future in submitted:
                    try:
                        result = future.result()
                        self._commit_backup(job, result)
                        rate = result["size"] / result["elapsed"] / (1024 * 1024)
                        log.info("파일 백업 완료 drive=%s item=%s bytes=%s seconds=%.3f MiB/s=%.2f",
                                 job["drive_id"], job["item_id"], result["size"], result["elapsed"], rate)
                        progress.finish_file(True)
                    except (GraphError, StorageError, OSError, HTTPException, ValueError, KeyError) as exc:
                        self._record_job_failure(job, exc)
                        progress.finish_file(False)
                        if isinstance(exc, (StorageError, OSError)) and fatal is None:
                            fatal = exc
                if fatal is not None:
                    # Storage failures stop later batches without discarding completed work.
                    raise fatal

    def run(self):
        self.storage.check(check_space=False)
        with self.db.transaction() as conn:
            run_id = conn.execute("INSERT INTO runs(started,status) VALUES (?,'running')", (time.time(),)).lastrowid
        errors, fatal = 0, False
        retention = {"completed": 0, "failed": 0}
        browse = {"published": 0, "failed": 0}
        log.info("백업 실행 시작 run_id=%s", run_id)
        try:
            log.info("작업 단계 시작 stage=retention")
            retention = Retention(self.config, self.db, self.storage).prune()
            log.info("작업 단계 완료 stage=retention completed=%s failed=%s",
                     retention["completed"], retention["failed"])
            errors += retention["failed"]
            self.storage.check()
            log.info("작업 단계 시작 stage=user_discovery")
            errors += self.discover()
            log.info("작업 단계 완료 stage=user_discovery")
            drives = self.db.all("SELECT * FROM drives WHERE status='active'")
            log.info("드라이브 동기화 시작 drives=%s", len(drives))
            for drive_index, drive in enumerate(drives, 1):
                try:
                    self.scan(drive["drive_id"])
                except (GraphError, ValueError, KeyError) as exc:
                    errors += 1
                    if isinstance(exc, GraphError):
                        log.error("변경 조회 실패 drive=%s status=%s code=%s",
                                  drive["drive_id"], exc.status, exc.code)
                    else:
                        log.error("변경 조회 실패 drive=%s type=%s", drive["drive_id"], type(exc).__name__)
                log.info("드라이브 동기화 진행 drives=%s/%s percent=%.1f%%",
                         drive_index, len(drives), drive_index * 100.0 / len(drives))
            log.info("드라이브 동기화 완료 drives=%s errors=%s", len(drives), errors)
            log.info("작업 단계 시작 stage=repair_check")
            repaired = self.repair_missing()
            log.info("작업 단계 완료 stage=repair_check queued=%s", repaired)
            log.info("작업 단계 시작 stage=file_download")
            self.process_jobs()
            log.info("작업 단계 완료 stage=file_download")
            log.info("작업 단계 시작 stage=browse_publish")
            browse = BrowsePublisher(self.config, self.db, self.storage).sync()
            log.info("작업 단계 완료 stage=browse_publish published=%s failed=%s",
                     browse["published"], browse["failed"])
            errors += browse["failed"]
        except (GraphError, StorageError, OSError, ValueError, KeyError) as exc:
            fatal = True
            log.error("실행 실패 type=%s", storage_error_detail(exc) if isinstance(exc, StorageError) else type(exc).__name__)
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
                log.info("작업 단계 시작 stage=catalog_snapshot")
                self.storage.check()
                relative = f"catalogs/{run_id}-{uuid.uuid4().hex}.sqlite"
                target = self.storage.path(relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                self.db.snapshot(target)
                self.storage.manifest("catalog-" + uuid.uuid4().hex,
                                      {"kind": "catalog", "relative_path": relative, "sha256": digest(target),
                                       "run_id": run_id, "created": time.time(), "verification": "mount_readback_only"})
                log.info("작업 단계 완료 stage=catalog_snapshot path=%s", relative)
            except (StorageError, OSError) as exc:
                status = "partial"
                log.error("DB 백업 실패 type=%s", type(exc).__name__)
                with self.db.transaction() as conn:
                    conn.execute("UPDATE runs SET status='partial' WHERE run_id=?", (run_id,))
        log.info("백업 실행 완료 run_id=%s status=%s errors=%s remaining_jobs=%s",
                 run_id, status, errors, remaining)
        return {"run_id": run_id, "status": status, "remaining_jobs": remaining,
                "retention": retention,
                "browse": browse,
                "verification": "mount_readback_only"}
