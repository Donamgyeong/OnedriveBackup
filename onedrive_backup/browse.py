"""Explorer-readable latest copies. Immutable backup objects remain authoritative."""
import hashlib
import json
import logging
import os
import re
import shutil
import time
import unicodedata
import uuid
from pathlib import PurePosixPath

from .compat import is_path_within
from .storage import StorageError, digest


log = logging.getLogger(__name__)


class HashingReader:
    def __init__(self, stream, checksum):
        self.stream = stream
        self.checksum = checksum

    def read(self, size=-1):
        block = self.stream.read(size)
        self.checksum.update(block)
        return block


def short_id(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def safe_name(name, identity, limit=100):
    """Preserve ordinary names/extensions; disambiguate lossy Windows conversions."""
    original = str(name)
    name = unicodedata.normalize("NFC", original)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).rstrip(" .") or "unnamed"
    if name.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        name = "_" + name
    if name != original or len(name) > limit:
        suffix = PurePosixPath(name).suffix[:20]
        stem = name[:-len(suffix)] if suffix else name
        name = stem[:max(1, limit - len(suffix) - 14)] + "__" + short_id(identity) + suffix
    return name


class BrowsePublisher:
    def __init__(self, config, db, storage):
        self.config, self.db, self.storage = config, db, storage
        self._drive_users = {}
        self._profiles = {}
        self._item_payloads = {}
        self._site_sources = {}

    def path(self, relative):
        parts = PurePosixPath(relative).parts
        if not parts or parts[0] != "browse" or any(p in (".", "..") for p in parts):
            raise StorageError("invalid_browse_path")
        result = self.storage.path(relative)
        if not is_path_within(result, (self.storage.root / "browse").resolve()):
            raise StorageError("browse_path_outside_view")
        return result

    def destination(self, row):
        drive_id = row["drive_id"]
        if drive_id not in self._drive_users:
            drive = self.db.one("SELECT user_id FROM drives WHERE drive_id=?", (drive_id,))
            self._drive_users[drive_id] = drive["user_id"] if drive else drive_id
        user_id = self._drive_users[drive_id]
        if user_id not in self._profiles:
            account = self.db.one("SELECT profile FROM accounts WHERE user_id=?", (user_id,))
            self._profiles[user_id] = json.loads(account["profile"]) if account else {}
        profile = self._profiles[user_id]
        # Explorer folders use the human-readable Microsoft 365 display name.
        # The stable user ID suffix still prevents collisions between namesakes.
        label = profile.get("displayName") or user_id
        base = ["browse", safe_name(label, user_id, 45) + "__" + short_id(user_id),
                "drive_" + short_id(row["drive_id"]), "deleted" if row["deleted"] else "current"]
        if drive_id not in self._site_sources:
            self._site_sources[drive_id] = self.db.one("""SELECT s.site_id,s.profile AS site_profile,d.profile
                FROM site_drives d JOIN sites s ON s.site_id=d.site_id WHERE d.drive_id=?""", (drive_id,))
        source = self._site_sources[drive_id]
        if source:
            site = json.loads(source["site_profile"])
            library = json.loads(source["profile"])
            user_id = source["site_id"]
            base = ["browse", "SharePoint",
                    safe_name(site.get("displayName") or site.get("name") or user_id, user_id, 40) + "__" + short_id(user_id),
                    safe_name(library.get("name") or drive_id, drive_id, 40) + "__" + short_id(drive_id),
                    "deleted" if row["deleted"] else "current"]
        item = json.loads(row["source_payload"])
        folders, seen = [], {row["item_id"]}
        parent = item.get("parentReference", {}).get("id")
        while parent:
            if parent in seen:
                raise StorageError("browse_parent_cycle")
            seen.add(parent)
            key = (drive_id, parent)
            if key not in self._item_payloads:
                found = self.db.one("SELECT payload FROM items WHERE drive_id=? AND item_id=?", key)
                self._item_payloads[key] = found["payload"] if found else None
            payload = self._item_payloads[key]
            if payload is None:
                folders.append("_unknown_" + short_id(parent))
                break
            info = json.loads(payload)
            if "root" in info:
                break
            folders.append(safe_name(info.get("name", parent), parent, 50) + "__" + short_id(parent))
            parent = info.get("parentReference", {}).get("id")
        name = safe_name(item.get("name") or json.loads(row["payload"]).get("name", row["item_id"]), row["item_id"])
        relative = "/".join(base + list(reversed(folders)) + [name])
        # Flatten unusually long paths; original hierarchy remains in the catalog.
        if len(str(self.storage.root / relative).encode("utf-16-le")) // 2 > 240:
            relative = "/".join(["browse", "_long_paths", short_id(user_id), short_id(row["drive_id"]),
                                 short_id(row["item_id"]), safe_name(name, row["item_id"], 60)])
        return relative

    def allocate(self, relative, row, reserved):
        identity = (row["drive_id"], row["item_id"])
        def occupied(candidate):
            owner = reserved.get(candidate.casefold())
            if owner is not None:
                return owner != identity
            entry = self.db.one("""SELECT drive_id,item_id FROM browse_files
                WHERE relative_path=? COLLATE NOCASE LIMIT 1""", (candidate,))
            return entry is not None and (entry["drive_id"], entry["item_id"]) != identity
        if occupied(relative):
            path = PurePosixPath(relative)
            suffix = path.suffix
            stem = path.name[:-len(suffix)] if suffix else path.name
            relative = str(path.with_name(stem + "__" + short_id("/".join(identity)) + suffix))
        if occupied(relative):
            raise StorageError("browse_name_collision")
        reserved[relative.casefold()] = identity
        return relative

    def resume(self, entry):
        pending = json.loads(entry["pending"])
        target, temporary = self.path(pending["path"]), self.path(pending["temporary"])
        source = self.db.one("SELECT * FROM objects WHERE object_id=?", (pending["object_id"],))
        if not source:
            raise StorageError("browse_source_missing")
        self.storage.check(check_space=False)
        if target.exists():
            actual = digest(target)
            if actual not in (pending["sha256"], pending.get("previous_hash")):
                raise StorageError("browse_file_modified_outside_backup")
        else:
            actual = None
        if actual != pending["sha256"]:
            self.storage.check(source["size"])
            original = self.storage.path(source["relative_path"])
            if not self.storage.exists(source["relative_path"], source["size"]):
                raise StorageError("browse_source_checksum_mismatch")
            target.parent.mkdir(parents=True, exist_ok=True)
            # This exact temp path was persisted before writing; retry can safely replace it.
            if temporary.exists():
                self.storage.remove(pending["temporary"])
            copied_hash = hashlib.sha256()
            with original.open("rb") as src, temporary.open("xb") as dst:
                shutil.copyfileobj(HashingReader(src, copied_hash), dst, self.config.download_chunk_bytes)
                dst.flush()
                os.fsync(dst.fileno())
            if copied_hash.hexdigest() != pending["sha256"]:
                raise StorageError("browse_copy_checksum_mismatch")
            self.storage.check(check_space=False)
            # Catch edits made while copying before replacing an owned destination.
            if target.exists() and digest(target) not in (pending["sha256"], pending.get("previous_hash")):
                raise StorageError("browse_file_modified_outside_backup")
            os.replace(temporary, target)
        if digest(target) != pending["sha256"]:
            raise StorageError("browse_readback_checksum_mismatch")
        target_stat = target.stat()
        if temporary.exists():
            self.storage.remove(pending["temporary"])
        with self.db.transaction() as conn:
            if entry["relative_path"] and entry["relative_path"] != pending["path"]:
                conn.execute("INSERT OR REPLACE INTO browse_cleanup VALUES (?,?,?)",
                             (entry["relative_path"], entry["object_id"], entry["sha256"]))
            conn.execute("""UPDATE browse_files SET relative_path=?,object_id=?,sha256=?,pending=NULL,error=NULL,
                size=?,mtime_ns=?
                WHERE drive_id=? AND item_id=?""", (pending["path"], pending["object_id"], pending["sha256"],
                                                     target_stat.st_size, target_stat.st_mtime_ns,
                                                     entry["drive_id"], entry["item_id"]))

    def cleanup(self):
        failed = 0
        for row in self.db.all("SELECT * FROM browse_cleanup"):
            try:
                path = self.path(row["relative_path"])
                if path.exists():
                    if digest(path) != row["sha256"]:
                        raise StorageError("browse_old_copy_modified")
                    self.storage.remove(row["relative_path"])
                with self.db.transaction() as conn:
                    conn.execute("DELETE FROM browse_cleanup WHERE relative_path=?", (row["relative_path"],))
            except (OSError, StorageError):
                failed += 1
        return failed

    def sync(self):
        published, failed, processed, cursor = 0, 0, 0, 0
        while True:
            self._item_payloads.clear()
            result = self._sync_batch(cursor)
            published += result["published"]
            failed += result["failed"]
            processed += result["processed"]
            if not result["processed"]:
                break
            cursor = result["cursor"]
            log.info("탐색기 게시 체크포인트 processed=%s published=%s failed=%s", processed, published, failed)
        self.storage.check(check_space=False)
        failed += self.cleanup()
        return {"published": published, "failed": failed, "path": str(self.storage.root / "browse")}

    def _sync_batch(self, cursor):
        self.storage.check(check_space=False)
        failed, published = 0, 0
        rows = self.db.all("""SELECT o.*,i.payload AS source_payload,i.deleted,j.rowid AS browse_job_id FROM browse_jobs j
            CROSS JOIN items i ON i.drive_id=j.drive_id AND i.item_id=j.item_id
            CROSS JOIN objects o ON i.object_id=o.object_id WHERE NOT EXISTS
            (SELECT 1 FROM purge_jobs p WHERE p.resource_id=o.object_id AND p.state='pending')
            AND j.rowid>? ORDER BY j.rowid LIMIT 256""", (cursor,))
        if not rows:
            return {"published": 0, "failed": 0, "processed": 0, "cursor": cursor}
        reserved = {}
        for entry in self.db.all("SELECT * FROM browse_files WHERE pending IS NOT NULL"):
            identity = (entry["drive_id"], entry["item_id"])
            if entry["relative_path"]:
                reserved[entry["relative_path"].casefold()] = identity
            if entry["pending"]:
                reserved[json.loads(entry["pending"])["path"].casefold()] = identity
        # A stale copy is not available for another item until cleanup succeeds.
        for row in self.db.all("SELECT relative_path FROM browse_cleanup"):
            reserved.setdefault(row["relative_path"].casefold(), ("cleanup", "cleanup"))
        total_rows = len(rows)
        processed_rows = 0
        last_progress = time.monotonic()
        if total_rows:
            log.info("탐색기 보기 동기화 시작 files=%s", total_rows)
        for row in rows:
            try:
                parts = PurePosixPath(self.destination(row)).parts
                for index in range(1, len(parts)):
                    reserved["/".join(parts[:index]).casefold()] = ("directory", "directory")
            except (ValueError, StorageError):
                pass  # The per-file pass below reports the error.
        for row in rows:
            identity = (row["drive_id"], row["item_id"])
            try:
                entry = self.db.one("SELECT * FROM browse_files WHERE drive_id=? AND item_id=?", identity)
                if entry and entry["pending"]:
                    self.resume(entry)
                    entry = self.db.one("SELECT * FROM browse_files WHERE drive_id=? AND item_id=?", identity)
                relative = self.allocate(self.destination(row), row, reserved)
                target = self.path(relative)
                same_path = entry and entry["relative_path"] == relative
                unchanged = same_path and entry["object_id"] == row["object_id"] and target.is_file()
                if unchanged:
                    target_stat = target.stat()
                    signature_matches = (entry["size"] == target_stat.st_size
                                         and entry["mtime_ns"] == target_stat.st_mtime_ns)
                    verified = signature_matches or digest(target) == row["sha256"]
                else:
                    verified = False
                if verified:
                    if entry["error"]:
                        with self.db.transaction() as conn:
                            conn.execute("UPDATE browse_files SET error=NULL WHERE drive_id=? AND item_id=?", identity)
                    if not signature_matches:
                        with self.db.transaction() as conn:
                            conn.execute("UPDATE browse_files SET size=?,mtime_ns=? WHERE drive_id=? AND item_id=?",
                                         (target_stat.st_size, target_stat.st_mtime_ns, *identity))
                    with self.db.transaction() as conn:
                        conn.execute("DELETE FROM browse_jobs WHERE drive_id=? AND item_id=?", identity)
                    continue
                if target.exists() and not same_path:
                    raise StorageError("browse_unmanaged_destination_exists")
                pending = {"path": relative, "object_id": row["object_id"], "sha256": row["sha256"],
                           "temporary": str(PurePosixPath(relative).with_name(".backup-" + uuid.uuid4().hex + ".partial")),
                           "previous_hash": entry["sha256"] if same_path else None}
                with self.db.transaction() as conn:
                    conn.execute("""INSERT INTO browse_files(drive_id,item_id,pending) VALUES (?,?,?)
                        ON CONFLICT(drive_id,item_id) DO UPDATE SET pending=excluded.pending,error=NULL""",
                                 (*identity, json.dumps(pending)))
                self.resume(self.db.one("SELECT * FROM browse_files WHERE drive_id=? AND item_id=?", identity))
                with self.db.transaction() as conn:
                    conn.execute("DELETE FROM browse_jobs WHERE drive_id=? AND item_id=?", identity)
                published += 1
            except (OSError, StorageError, ValueError) as exc:
                failed += 1
                with self.db.transaction() as conn:
                    conn.execute("INSERT OR IGNORE INTO browse_files(drive_id,item_id) VALUES (?,?)", identity)
                    conn.execute("UPDATE browse_files SET error=? WHERE drive_id=? AND item_id=?",
                                 (str(exc) if isinstance(exc, StorageError) else type(exc).__name__, *identity))
                log.error("탐색기 보기 갱신 실패 drive=%s item=%s type=%s", *identity, type(exc).__name__)
            finally:
                processed_rows += 1
                now = time.monotonic()
                if processed_rows == total_rows or now - last_progress >= 1.0:
                    log.info("탐색기 보기 동기화 진행 files=%s/%s published=%s failed=%s percent=%.1f%%",
                             processed_rows, total_rows, published, failed,
                             processed_rows * 100.0 / total_rows)
                    last_progress = now
        return {"published": published, "failed": failed, "processed": len(rows),
                "cursor": rows[-1]["browse_job_id"]}
