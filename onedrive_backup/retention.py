"""Resumable age-based disposal. No recursive deletion or source-side writes."""
import json
import logging
import re
import time

from .storage import StorageError, digest


log = logging.getLogger(__name__)


def tag(item):
    if item.get("cTag"):
        return "c:" + item["cTag"]
    if item.get("eTag"):
        return "e:" + item["eTag"]
    return None


class Retention:
    def __init__(self, config, db, storage):
        self.config, self.db, self.storage = config, db, storage

    def protected_objects(self, file_keys=None):
        """Keep a verified version for files that currently have a disposal candidate."""
        if file_keys is None:
            file_keys = {(row["drive_id"], row["item_id"]) for row in
                         self.db.all("SELECT DISTINCT drive_id,item_id FROM objects")}
        protected = set()
        relevant_ids = set()
        total = len(file_keys)
        last_progress = 0.0
        for index, (drive_id, item_id) in enumerate(file_keys, 1):
            rows = self.db.all("""SELECT rowid,* FROM objects WHERE drive_id=? AND item_id=?
                ORDER BY created DESC,rowid DESC""", (drive_id, item_id))
            if not rows:
                continue
            relevant_ids.update(row["object_id"] for row in rows)
            protected.add(rows[0]["object_id"])
            for row in rows:
                try:
                    if (self.storage.exists(row["relative_path"], row["size"])
                            and digest(self.storage.path(row["relative_path"])) == row["sha256"]
                            and self.storage.path(f"manifests/{row['object_id']}.json").is_file()):
                        protected.add(row["object_id"])
                        break
                except OSError:
                    continue
            else:
                # No verified replacement: do not discard any potentially recoverable copy.
                protected.update(row["object_id"] for row in rows)
            current = self.db.one("SELECT object_id FROM items WHERE drive_id=? AND item_id=?",
                                  (drive_id, item_id))
            if current and current["object_id"]:
                protected.add(current["object_id"])
            entry = self.db.one("SELECT object_id,pending FROM browse_files WHERE drive_id=? AND item_id=?",
                                (drive_id, item_id))
            if entry:
                if entry["object_id"]:
                    protected.add(entry["object_id"])
                if entry["pending"]:
                    protected.add(json.loads(entry["pending"])["object_id"])
            now = time.monotonic()
            if index == total or now - last_progress >= 1.0:
                log.info("보존본 검사 진행 files=%s/%s percent=%.1f%%",
                         index, total, index * 100.0 / total)
                last_progress = now
        # Cleanup rows are normally few; only references in candidate groups matter.
        protected.update(r["object_id"] for r in self.db.all("SELECT object_id FROM browse_cleanup")
                         if r["object_id"] in relevant_ids)
        return protected

    def candidate_file_keys(self, now):
        """Return only files for which an object could actually be deleted."""
        keys = {(r["drive_id"], r["item_id"]) for r in self.db.all("""SELECT DISTINCT o.drive_id,o.item_id
            FROM purge_jobs p JOIN objects o ON o.object_id=p.resource_id
            WHERE p.state='pending' AND p.kind='object'""")}
        if self.config.retention_policy == "keep_all":
            return keys
        cutoff = now - self.config.retention_days * 86400
        keys.update((r["drive_id"], r["item_id"]) for r in self.db.all("""SELECT DISTINCT o.drive_id,o.item_id
            FROM objects o WHERE o.created<=? AND EXISTS (
                SELECT 1 FROM objects newer WHERE newer.drive_id=o.drive_id AND newer.item_id=o.item_id
                AND (newer.created>o.created OR (newer.created=o.created AND newer.rowid>o.rowid)))""", (cutoff,)))
        return keys

    def validate_object_paths(self):
        row = self.db.one("""SELECT object_id,relative_path FROM objects
            WHERE length(object_id)<>32 OR object_id GLOB '*[^0-9a-f]*'
               OR relative_path<>( 'objects/' || substr(object_id,1,2) || '/' || object_id || '.bin')
            LIMIT 1""")
        if row:
            if not re.fullmatch(r"[0-9a-f]{32}", row["object_id"]):
                raise StorageError("invalid_retention_object_id")
            raise StorageError("invalid_retention_object_path")

    def candidates(self, now, protected=None):
        protected = self.protected_objects() if protected is None else protected
        pending = {r["resource_id"]: dict(r) for r in self.db.all("SELECT * FROM purge_jobs WHERE state='pending'")
                   if r["kind"] != "object" or r["resource_id"] not in protected}
        if self.config.retention_policy == "keep_all":
            return list(pending.values())
        cutoff = now - self.config.retention_days * 86400
        for row in self.db.all("""SELECT o.* FROM objects o WHERE o.created<=? AND EXISTS (
            SELECT 1 FROM objects newer WHERE newer.drive_id=o.drive_id AND newer.item_id=o.item_id
            AND (newer.created>o.created OR (newer.created=o.created AND newer.rowid>o.rowid)))""", (cutoff,)):
            object_id = row["object_id"]
            if object_id in protected:
                continue
            if not re.fullmatch(r"[0-9a-f]{32}", object_id):
                raise StorageError("invalid_retention_object_id")
            expected_path = f"objects/{object_id[:2]}/{object_id}.bin"
            if row["relative_path"] != expected_path:
                raise StorageError("invalid_retention_object_path")
            pending.setdefault(object_id, {"resource_id": object_id, "kind": "object",
                                          "relative_path": expected_path,
                                          "manifest_path": f"manifests/{object_id}.json"})
        # Import both new and pre-retention catalog manifests; old manifests have no created field.
        manifest_dir = self.storage.path("manifests")
        for path in manifest_dir.glob("catalog-*.json"):
            if not re.fullmatch(r"catalog-[0-9a-f]{32}\.json", path.name):
                continue
            resolved = self.storage.path("manifests/" + path.name)
            payload = json.loads(resolved.read_text(encoding="utf-8"))
            if payload.get("kind") != "catalog":
                continue
            relative = payload.get("relative_path", "")
            if not re.fullmatch(r"catalogs/[0-9]+-[0-9a-f]{32}\.sqlite", relative):
                raise StorageError("invalid_retention_catalog_path")
            created = payload.get("created", resolved.stat().st_mtime)
            if not isinstance(created, (int, float)):
                raise StorageError("invalid_retention_catalog_timestamp")
            if created <= cutoff:
                pending.setdefault(path.stem, {"resource_id": path.stem, "kind": "catalog",
                                              "relative_path": relative, "manifest_path": "manifests/" + path.name})
        return list(pending.values())

    def prune(self, *, dry_run=False, now=None):
        self.storage.check(check_space=False)
        now = time.time() if now is None else now
        self.validate_object_paths()
        file_keys = self.candidate_file_keys(now)
        protected = self.protected_objects(file_keys)
        candidates = self.candidates(now, protected)
        log.info("보존 정책 검사 완료 candidate_files=%s protected=%s candidates=%s",
                 len(file_keys), len(protected), len(candidates))
        if dry_run:
            return {"dry_run": True, "count": len(candidates),
                    "candidates": [{k: r[k] for k in ("resource_id", "kind", "relative_path")} for r in candidates]}
        with self.db.transaction() as conn:
            for object_id in protected:
                conn.execute("UPDATE purge_jobs SET state='cancelled',error='minimum_one_version' WHERE resource_id=? AND state='pending'",
                             (object_id,))
            for row in candidates:
                conn.execute("""INSERT INTO purge_jobs
                    (resource_id,kind,relative_path,manifest_path,requested) VALUES (?,?,?,?,?)
                    ON CONFLICT(resource_id) DO UPDATE SET state='pending',requested=excluded.requested,error=NULL
                    WHERE purge_jobs.state='cancelled'""",
                             (row["resource_id"], row["kind"], row["relative_path"], row["manifest_path"], now))
        completed, failed = 0, 0
        pending_rows = self.db.all("SELECT * FROM purge_jobs WHERE state='pending'")
        total = len(pending_rows)
        last_progress = 0.0
        for index, row in enumerate(pending_rows, 1):
            try:
                # Both paths are validated before deleting either file, including on resume.
                resource = row["resource_id"]
                if row["kind"] == "object":
                    valid = re.fullmatch(r"[0-9a-f]{32}", resource) and row["relative_path"] == f"objects/{resource[:2]}/{resource}.bin"
                else:
                    valid = row["kind"] == "catalog" and re.fullmatch(r"catalog-[0-9a-f]{32}", resource) and re.fullmatch(r"catalogs/[0-9]+-[0-9a-f]{32}\.sqlite", row["relative_path"])
                if not valid or row["manifest_path"] != f"manifests/{resource}.json":
                    raise StorageError("invalid_purge_paths")
                self.storage.path(row["relative_path"])
                self.storage.path(row["manifest_path"])
                self.storage.remove(row["relative_path"])
                self.storage.remove(row["manifest_path"])
                with self.db.transaction() as conn:
                    obj = self.db.one("SELECT * FROM objects WHERE object_id=?", (resource,)) if row["kind"] == "object" else None
                    if obj:
                        current = self.db.one("SELECT * FROM items WHERE object_id=?", (resource,))
                        if current:
                            expired_tag = tag(json.loads(obj["payload"]))
                            conn.execute("INSERT OR REPLACE INTO expired_items VALUES (?,?,?)",
                                         (obj["drive_id"], obj["item_id"], expired_tag))
                            # Preserve pending work for a newer source version.
                            if expired_tag and expired_tag == tag(json.loads(current["payload"])):
                                conn.execute("DELETE FROM jobs WHERE drive_id=? AND item_id=?", (obj["drive_id"], obj["item_id"]))
                        conn.execute("UPDATE items SET object_id=NULL WHERE object_id=?", (resource,))
                        conn.execute("DELETE FROM objects WHERE object_id=?", (resource,))
                    conn.execute("UPDATE purge_jobs SET state='completed',completed=?,error=NULL WHERE resource_id=?", (now, resource))
                completed += 1
            except (OSError, StorageError) as exc:
                with self.db.transaction() as conn:
                    conn.execute("UPDATE purge_jobs SET error=? WHERE resource_id=?", (type(exc).__name__, row["resource_id"]))
                failed += 1
            progress_time = time.monotonic()
            if index == total or progress_time - last_progress >= 1.0:
                log.info("보존 기간 폐기 진행 items=%s/%s completed=%s failed=%s percent=%.1f%%",
                         index, total, completed, failed, index * 100.0 / total)
                last_progress = progress_time
        return {"dry_run": False, "completed": completed, "failed": failed}
