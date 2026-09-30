"""Resumable age-based disposal. No recursive deletion or source-side writes."""
import json
import re
import time

from .storage import StorageError, digest


def tag(item):
    if item.get("cTag"):
        return "c:" + item["cTag"]
    if item.get("eTag"):
        return "e:" + item["eTag"]
    return None


class Retention:
    def __init__(self, config, db, storage):
        self.config, self.db, self.storage = config, db, storage

    def protected_objects(self):
        """Keep latest metadata record plus a verified surviving version per file."""
        groups = {}
        for row in self.db.all("SELECT rowid,* FROM objects ORDER BY created DESC,rowid DESC"):
            groups.setdefault((row["drive_id"], row["item_id"]), []).append(row)
        protected = set()
        for rows in groups.values():
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
        # The current pointer is also protected if imported timestamps are out of order.
        protected.update(r["object_id"] for r in self.db.all("SELECT object_id FROM items WHERE object_id IS NOT NULL"))
        # Keep source versions while a visible copy is current or an update/cleanup is unfinished.
        for entry in self.db.all("SELECT object_id,pending FROM browse_files"):
            if entry["object_id"]:
                protected.add(entry["object_id"])
            if entry["pending"]:
                protected.add(json.loads(entry["pending"])["object_id"])
        protected.update(r["object_id"] for r in self.db.all("SELECT object_id FROM browse_cleanup"))
        return protected

    def candidates(self, now):
        protected = self.protected_objects()
        pending = {r["resource_id"]: dict(r) for r in self.db.all("SELECT * FROM purge_jobs WHERE state='pending'")
                   if r["kind"] != "object" or r["resource_id"] not in protected}
        if self.config.retention_policy == "keep_all":
            return list(pending.values())
        cutoff = now - self.config.retention_days * 86400
        for row in self.db.all("SELECT * FROM objects WHERE created<=?", (cutoff,)):
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
        candidates = self.candidates(now)
        if dry_run:
            return {"dry_run": True, "count": len(candidates),
                    "candidates": [{k: r[k] for k in ("resource_id", "kind", "relative_path")} for r in candidates]}
        with self.db.transaction() as conn:
            for object_id in self.protected_objects():
                conn.execute("UPDATE purge_jobs SET state='cancelled',error='minimum_one_version' WHERE resource_id=? AND state='pending'",
                             (object_id,))
            for row in candidates:
                conn.execute("""INSERT INTO purge_jobs
                    (resource_id,kind,relative_path,manifest_path,requested) VALUES (?,?,?,?,?)
                    ON CONFLICT(resource_id) DO UPDATE SET state='pending',requested=excluded.requested,error=NULL
                    WHERE purge_jobs.state='cancelled'""",
                             (row["resource_id"], row["kind"], row["relative_path"], row["manifest_path"], now))
        completed, failed = 0, 0
        for row in self.db.all("SELECT * FROM purge_jobs WHERE state='pending'"):
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
        return {"dry_run": False, "completed": completed, "failed": failed}
