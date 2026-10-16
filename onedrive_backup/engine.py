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
                    previous = conn.execute("SELECT profile FROM accounts WHERE user_id=?", (user_id,)).fetchone()
                    profile_changed = bool(previous and previous["profile"] != encode(user))
                    conn.execute("INSERT OR REPLACE INTO accounts VALUES (?,?,?,?)",
                                 (user_id, encode(user), "excluded" if excluded else "active", time.time()))
                    if profile_changed:
                        conn.execute("""INSERT OR IGNORE INTO browse_jobs(drive_id,item_id)
                            SELECT i.drive_id,i.item_id FROM items i JOIN drives d ON d.drive_id=i.drive_id
                            WHERE d.user_id=? AND i.object_id IS NOT NULL""", (user_id,))
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

    def discover_sites(self):
        """Enumerate tenant site collections and subsites, then all libraries."""
        errors, libraries = 0, 0
        seen = set()
        pending = []
        log.info("SharePoint 사이트 검색 시작")
        try:
            for page in self.graph.pages("/sites/getAllSites"):
                pending.extend(page["value"])
                log.info("SharePoint 사이트 목록 조회 sites=%s percent=unknown", len(pending))
        except (GraphError, OSError, ValueError, KeyError) as exc:
            log.error("SharePoint 전체 사이트 검색 실패 type=%s", str(exc) if isinstance(exc, GraphError) else type(exc).__name__)
            return 1
        while pending:
            site = pending.pop()
            site_id = site["id"]
            if site_id in seen:
                continue
            seen.add(site_id)
            with self.db.transaction() as conn:
                previous = self.db.one("SELECT profile FROM sites WHERE site_id=?", (site_id,))
                changed = previous and previous["profile"] != encode(site)
                conn.execute("INSERT OR REPLACE INTO sites VALUES (?,?,'active',?)",
                             (site_id, encode(site), time.time()))
                if changed:
                    conn.execute("""INSERT OR IGNORE INTO browse_jobs SELECT i.drive_id,i.item_id
                        FROM items i JOIN site_drives s ON s.drive_id=i.drive_id
                        WHERE s.site_id=? AND i.object_id IS NOT NULL""", (site_id,))
            try:
                discovered = []
                for page in self.graph.pages("/sites/{}/drives".format(segment(site_id))):
                    discovered.extend(page["value"])
                with self.db.transaction() as conn:
                    conn.execute("""UPDATE drives SET status='unavailable' WHERE drive_id IN
                        (SELECT drive_id FROM site_drives WHERE site_id=?)""", (site_id,))
                    for drive in discovered:
                        if drive.get("driveType") != "documentLibrary":
                            continue
                        drive_id = drive["id"]
                        previous = self.db.one("SELECT profile FROM site_drives WHERE drive_id=?", (drive_id,))
                        if previous and previous["profile"] != encode(drive):
                            conn.execute("""INSERT OR IGNORE INTO browse_jobs SELECT drive_id,item_id
                                FROM items WHERE drive_id=? AND object_id IS NOT NULL""", (drive_id,))
                        conn.execute("INSERT OR REPLACE INTO site_drives VALUES (?,?,?)",
                                     (drive_id, site_id, encode(drive)))
                        conn.execute("""INSERT INTO drives(drive_id,user_id,status) VALUES (?,?,'active')
                            ON CONFLICT(drive_id) DO UPDATE SET status='active'""",
                                     (drive_id, "site:" + site_id))
                        libraries += 1
            except (GraphError, OSError, ValueError, KeyError) as exc:
                errors += 1
                with self.db.transaction() as conn:
                    conn.execute("UPDATE sites SET status='error' WHERE site_id=?", (site_id,))
                log.error("SharePoint 사이트 검색 실패 site=%s type=%s", site_id,
                          str(exc) if isinstance(exc, GraphError) else type(exc).__name__)
            # A failed library query must not prevent discovery of accessible subsites.
            try:
                for page in self.graph.pages("/sites/{}/sites".format(segment(site_id))):
                    pending.extend(page["value"])
            except (GraphError, OSError, ValueError, KeyError) as exc:
                errors += 1
                with self.db.transaction() as conn:
                    conn.execute("UPDATE sites SET status='error' WHERE site_id=?", (site_id,))
                log.error("SharePoint 하위 사이트 검색 실패 site=%s type=%s", site_id,
                          str(exc) if isinstance(exc, GraphError) else type(exc).__name__)
            log.info("SharePoint 검색 진행 sites=%s libraries=%s errors=%s percent=unknown",
                     len(seen), libraries, errors)
        # Partial enumeration must never mark unseen sites or libraries absent.
        if not errors:
            with self.db.transaction() as conn:
                for row in self.db.all("SELECT site_id FROM sites"):
                    if row["site_id"] not in seen:
                        conn.execute("UPDATE sites SET status='not_seen' WHERE site_id=?", (row["site_id"],))
                        conn.execute("""UPDATE drives SET status='site_not_seen' WHERE drive_id IN
                            (SELECT drive_id FROM site_drives WHERE site_id=?)""", (row["site_id"],))
        log.info("SharePoint 사이트 검색 완료 sites=%s libraries=%s errors=%s", len(seen), libraries, errors)
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
        hierarchy_changes = set()
        deletion_roots = set()
        if full_scan:
            missing_rows = conn.execute("""SELECT item_id,payload,object_id FROM items
                WHERE drive_id=? AND item_id NOT IN
                (SELECT item_id FROM scan_items WHERE drive_id=?)""", (drive_id, drive_id)).fetchall()
            deletion_roots.update(r["item_id"] for r in missing_rows)
            for missing in missing_rows:
                if missing["object_id"]:
                    conn.execute("INSERT OR IGNORE INTO browse_jobs VALUES (?,?)", (drive_id, missing["item_id"]))
            conn.execute("""UPDATE items SET deleted=1 WHERE drive_id=? AND item_id NOT IN
                (SELECT item_id FROM scan_items WHERE drive_id=?)""", (drive_id, drive_id))
        rows = conn.execute("SELECT * FROM scan_items WHERE drive_id=?", (drive_id,)).fetchall()
        existing = {row["item_id"]: row for row in conn.execute("""SELECT
            i.item_id,i.payload,i.deleted,i.object_id,o.payload AS object_payload
            FROM scan_items s JOIN items i ON i.drive_id=s.drive_id AND i.item_id=s.item_id
            LEFT JOIN objects o ON o.object_id=i.object_id
            WHERE s.drive_id=?""", (drive_id,)).fetchall()}
        total = len(rows)
        last_progress = 0.0
        log.info("드라이브 변경 확정 시작 drive=%s items=%s", drive_id, total)
        for index, row in enumerate(rows, 1):
            item = json.loads(row["payload"])
            old = existing.get(item["id"])
            if kind(item) == "deleted" and old:
                item = {**json.loads(old["payload"]), **item}
            encoded_item = encode(item)
            metadata_changed = not old or old["payload"] != encoded_item or bool(old["deleted"]) != (kind(item) == "deleted")
            if metadata_changed:
                old_item = json.loads(old["payload"]) if old else {}
                if "folder" in item or "folder" in old_item:
                    if old:
                        hierarchy_changes.add(item["id"])
                elif "file" in item or (old and old["object_id"]):
                    conn.execute("INSERT OR IGNORE INTO browse_jobs VALUES (?,?)", (drive_id, item["id"]))
            if kind(item) == "deleted":
                deletion_roots.add(item["id"])
            conn.execute("""INSERT INTO items(drive_id,item_id,payload,deleted) VALUES (?,?,?,?)
                ON CONFLICT(drive_id,item_id) DO UPDATE SET payload=excluded.payload,deleted=excluded.deleted""",
                         (drive_id, item["id"], encoded_item, int(kind(item) == "deleted")))
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
        children = {}
        if hierarchy_changes or deletion_roots:
            entries = self.db.all("SELECT item_id,payload FROM items WHERE drive_id=? AND deleted=0", (drive_id,))
            for entry in entries:
                parent_id = json.loads(entry["payload"]).get("parentReference", {}).get("id")
                children.setdefault(parent_id, []).append(entry["item_id"])
        # Include descendants when Graph reports only a deleted folder.
        deleted = set(deletion_roots)
        pending_parents = list(deletion_roots)
        while pending_parents:
            parent_id = pending_parents.pop()
            for item_id in children.get(parent_id, ()):
                if item_id not in deleted:
                    deleted.add(item_id)
                    pending_parents.append(item_id)
        affected = set(hierarchy_changes)
        pending_parents = list(hierarchy_changes)
        while pending_parents:
            parent_id = pending_parents.pop()
            for item_id in children.get(parent_id, ()):
                if item_id not in affected:
                    affected.add(item_id)
                    pending_parents.append(item_id)
        if affected:
            conn.executemany("""INSERT OR IGNORE INTO browse_jobs(drive_id,item_id)
                SELECT drive_id,item_id FROM items
                WHERE drive_id=? AND item_id=? AND object_id IS NOT NULL""",
                             ((drive_id, item_id) for item_id in affected))
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

    def repair_missing(self, verify_hash=False, full_scan=False):
        self.storage.check()
        repaired = 0
        batch_limit = self.config.repair_check_batch_size
        limit_clause = "" if full_scan else " LIMIT ?"
        limit_args = () if full_scan else (batch_limit,)
        missing_rows = self.db.all("""SELECT i.* FROM items i LEFT JOIN objects o ON i.object_id=o.object_id
            WHERE o.object_id IS NULL
            AND instr(i.payload, '"file":') > 0
            AND NOT EXISTS (SELECT 1 FROM jobs j
                WHERE j.drive_id=i.drive_id AND j.item_id=i.item_id)
            ORDER BY i.drive_id,i.item_id""" + limit_clause, limit_args)
        stored_limit = None if full_scan else max(0, batch_limit - len(missing_rows))
        cursor_row = self.db.one("SELECT value FROM settings WHERE key='repair_cursor_object_id'")
        cursor = cursor_row["value"] if cursor_row else ""
        stored_sql = """SELECT i.drive_id,i.item_id,i.deleted,o.* FROM items i JOIN objects o
            ON i.object_id=o.object_id WHERE NOT EXISTS
            (SELECT 1 FROM purge_jobs p WHERE p.resource_id=o.object_id AND p.state='pending')
            AND NOT EXISTS (SELECT 1 FROM jobs j
                WHERE j.drive_id=i.drive_id AND j.item_id=i.item_id)"""
        wrapped = False
        if full_scan:
            stored_rows = self.db.all(stored_sql + " ORDER BY i.object_id")
        elif not stored_limit:
            stored_rows = []
        else:
            stored_rows = self.db.all(stored_sql + " AND i.object_id>? ORDER BY i.object_id LIMIT ?",
                                      (cursor, stored_limit))
            if len(stored_rows) < stored_limit and cursor:
                wrapped = True
                stored_rows += self.db.all(stored_sql + " AND i.object_id<=? ORDER BY i.object_id LIMIT ?",
                                           (cursor, stored_limit - len(stored_rows)))
        total = len(missing_rows) + len(stored_rows)
        checked = 0
        last_progress = [0.0]
        log.info("백업 파일 검사 범위 mode=%s batch_limit=%s candidates=%s wrapped=%s",
                 "full" if full_scan else "incremental", "all" if full_scan else batch_limit,
                 total, wrapped)

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
        missing_repairs = []
        # Recover files expired by the former policy even when delta has no changes.
        for row in missing_rows:
            item = json.loads(row["payload"])
            if "file" in item and "remoteItem" not in item and "package" not in item:
                missing_repairs.append(row)
                repaired += 1
            checked += 1
            report_progress()

        def queue_repairs(rows):
            for repair_row in rows:
                self.db.queue(repair_row["drive_id"], repair_row["item_id"], reset=False)
                if repair_row["deleted"]:
                    self.db.conn.execute("UPDATE jobs SET state='failed',error='source_deleted_no_backup' WHERE drive_id=? AND item_id=?",
                                         (repair_row["drive_id"], repair_row["item_id"]))

        if missing_repairs:
            with self.db.transaction():
                queue_repairs(missing_repairs)

        checkpoint_repairs = []
        checkpoint_size = 250
        for stored_index, row in enumerate(stored_rows, 1):
            exists = self.storage.exists(row["relative_path"], row["size"])
            valid = exists and (not verify_hash or digest(self.storage.path(row["relative_path"])) == row["sha256"])
            if not valid:
                checkpoint_repairs.append(row)
                repaired += 1
            checked += 1
            report_progress()
            if stored_index % checkpoint_size == 0 or stored_index == len(stored_rows):
                with self.db.transaction() as conn:
                    queue_repairs(checkpoint_repairs)
                    if not full_scan:
                        conn.execute("""INSERT INTO settings(key,value) VALUES ('repair_cursor_object_id',?)
                            ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (row["object_id"],))
                checkpoint_repairs = []
        return repaired

    def _download_content(self, job, before, progress=None):
        started = time.perf_counter()
        if kind(before) != "file":
            raise ValueError("source_not_regular_file")
        expected = content_tag(before)
        if not expected:
            raise ValueError("source_version_tag_missing")
        with self.graph.download(before) as stream:
            object_id, relative, sha256 = self.storage.put(stream, before["size"], progress=progress)
        return {"object_id": object_id, "relative": relative, "sha256": sha256,
                "size": before["size"], "expected": expected,
                "elapsed": max(time.perf_counter() - started, 0.000001)}

    def _finalize_download(self, job, user_id, original_parts, before, result, after):
        drive_id, item_id = job["drive_id"], job["item_id"]
        object_id, relative, sha256 = result["object_id"], result["relative"], result["sha256"]
        expected = result["expected"]
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
        site_source = self.db.one("SELECT site_id,profile FROM site_drives WHERE drive_id=?", (drive_id,))
        if site_source:
            manifest.update({"source_kind": "sharepoint", "site_id": site_source["site_id"],
                             "library": json.loads(site_source["profile"]), "user_id": None})
        self.storage.manifest(object_id, manifest)
        result.update({"metadata": after, "created": created})
        return result

    def _download_job(self, job, user_id, original_parts, progress=None):
        before = self.graph.metadata(job["drive_id"], job["item_id"])
        result = self._download_content(job, before, progress)
        after = self.graph.metadata(job["drive_id"], job["item_id"])
        return self._finalize_download(job, user_id, original_parts, before, result, after)

    @staticmethod
    def _commit_backup_on(conn, job, result):
        conn.execute("INSERT INTO objects VALUES (?,?,?,?,?,?,?,?,?)",
                     (result["object_id"], job["drive_id"], job["item_id"], result["relative"],
                      result["sha256"], result["size"], encode(result["metadata"]), result["created"],
                      "mount_readback_only"))
        conn.execute("UPDATE items SET object_id=? WHERE drive_id=? AND item_id=?",
                     (result["object_id"], job["drive_id"], job["item_id"]))
        conn.execute("DELETE FROM expired_items WHERE drive_id=? AND item_id=?",
                     (job["drive_id"], job["item_id"]))
        conn.execute("DELETE FROM jobs WHERE job_id=?", (job["job_id"],))
        conn.execute("INSERT OR IGNORE INTO browse_jobs VALUES (?,?)",
                     (job["drive_id"], job["item_id"]))

    def _commit_backup(self, job, result):
        with self.db.transaction() as conn:
            self._commit_backup_on(conn, job, result)

    def _metadata_for_jobs(self, jobs):
        identities = [(job["drive_id"], job["item_id"]) for job in jobs]
        batch_method = getattr(self.graph, "metadata_batch", None)
        if batch_method:
            try:
                return batch_method(identities)
            except (GraphError, OSError, HTTPException, ValueError, KeyError) as exc:
                return [exc] * len(jobs)
        results = []
        for drive_id, item_id in identities:
            try:
                results.append(self.graph.metadata(drive_id, item_id))
            except (GraphError, OSError, HTTPException, ValueError, KeyError) as exc:
                results.append(exc)
        return results

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
                prepared = []
                fatal = None
                for job in batch:
                    try:
                        drive = self.db.one("SELECT user_id FROM drives WHERE drive_id=?", (job["drive_id"],))
                        original_parts = self.original_parts(job["drive_id"], job["item_id"])
                        prepared.append((job, drive["user_id"], original_parts))
                    except (GraphError, StorageError, OSError, HTTPException, ValueError, KeyError) as exc:
                        self._record_job_failure(job, exc)
                        progress.finish_file(False)
                        if isinstance(exc, (StorageError, OSError)):
                            fatal = exc
                            break
                if prepared:
                    with self.db.transaction() as conn:
                        conn.executemany("UPDATE jobs SET state='running' WHERE job_id=?",
                                         ((entry[0]["job_id"],) for entry in prepared))
                before_values = self._metadata_for_jobs([entry[0] for entry in prepared])
                submitted = []
                for context, before in zip(prepared, before_values):
                    job, user_id, original_parts = context
                    if isinstance(before, Exception):
                        self._record_job_failure(job, before)
                        progress.finish_file(False)
                        continue
                    try:
                        future = executor.submit(self._download_content, job, before, progress.advance)
                        submitted.append((context, before, future))
                    except (GraphError, StorageError, OSError, HTTPException, ValueError, KeyError) as exc:
                        self._record_job_failure(job, exc)
                        progress.finish_file(False)
                        if isinstance(exc, (StorageError, OSError)) and fatal is None:
                            fatal = exc
                downloaded = []
                for context, before, future in submitted:
                    job, user_id, original_parts = context
                    try:
                        downloaded.append((context, before, future.result()))
                    except (GraphError, StorageError, OSError, HTTPException, ValueError, KeyError) as exc:
                        self._record_job_failure(job, exc)
                        progress.finish_file(False)
                        if isinstance(exc, (StorageError, OSError)) and fatal is None:
                            fatal = exc
                after_values = self._metadata_for_jobs([entry[0][0] for entry in downloaded])
                completed = []
                for downloaded_entry, after in zip(downloaded, after_values):
                    context, before, result = downloaded_entry
                    job, user_id, original_parts = context
                    try:
                        if isinstance(after, Exception):
                            raise after
                        result = self._finalize_download(job, user_id, original_parts, before, result, after)
                        completed.append((job, result))
                    except (GraphError, StorageError, OSError, HTTPException, ValueError, KeyError) as exc:
                        self._record_job_failure(job, exc)
                        progress.finish_file(False)
                        if isinstance(exc, (StorageError, OSError)) and fatal is None:
                            fatal = exc
                if completed:
                    with self.db.transaction() as conn:
                        for job, result in completed:
                            self._commit_backup_on(conn, job, result)
                    for job, result in completed:
                        rate = result["size"] / result["elapsed"] / (1024 * 1024)
                        log.info("파일 백업 완료 drive=%s item=%s bytes=%s seconds=%.3f MiB/s=%.2f",
                                 job["drive_id"], job["item_id"], result["size"], result["elapsed"], rate)
                        progress.finish_file(True)
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
            log.info("작업 단계 시작 stage=site_discovery")
            errors += self.discover_sites()
            log.info("작업 단계 완료 stage=site_discovery")
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
