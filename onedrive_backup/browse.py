"""Explorer-readable latest copies. Immutable backup objects remain authoritative."""
import hashlib
import json
import logging
import os
import re
import shutil
import unicodedata
import uuid
from pathlib import PurePosixPath

from .storage import StorageError, digest


log = logging.getLogger(__name__)


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

    def path(self, relative):
        parts = PurePosixPath(relative).parts
        if not parts or parts[0] != "browse" or any(p in (".", "..") for p in parts):
            raise StorageError("invalid_browse_path")
        result = self.storage.path(relative)
        if not result.is_relative_to(self.storage.root / "browse"):
            raise StorageError("browse_path_outside_view")
        return result

    def destination(self, row):
        drive = self.db.one("SELECT user_id FROM drives WHERE drive_id=?", (row["drive_id"],))
        user_id = drive["user_id"] if drive else row["drive_id"]
        account = self.db.one("SELECT profile FROM accounts WHERE user_id=?", (user_id,))
        profile = json.loads(account["profile"]) if account else {}
        label = profile.get("userPrincipalName") or profile.get("displayName") or user_id
        base = ["browse", safe_name(label, user_id, 45) + "__" + short_id(user_id),
                "drive_" + short_id(row["drive_id"]), "deleted" if row["deleted"] else "current"]
        item = json.loads(row["source_payload"])
        folders, seen = [], {row["item_id"]}
        parent = item.get("parentReference", {}).get("id")
        while parent:
            if parent in seen:
                raise StorageError("browse_parent_cycle")
            seen.add(parent)
            found = self.db.one("SELECT payload FROM items WHERE drive_id=? AND item_id=?", (row["drive_id"], parent))
            if not found:
                folders.append("_unknown_" + short_id(parent))
                break
            info = json.loads(found["payload"])
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
            return owner is not None and owner != identity
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
            if not self.storage.exists(source["relative_path"], source["size"]) or digest(original) != source["sha256"]:
                raise StorageError("browse_source_checksum_mismatch")
            target.parent.mkdir(parents=True, exist_ok=True)
            # This exact temp path was persisted before writing; retry can safely replace it.
            if temporary.exists():
                self.storage.remove(pending["temporary"])
            with original.open("rb") as src, temporary.open("xb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
                dst.flush()
                os.fsync(dst.fileno())
            if digest(temporary) != pending["sha256"]:
                raise StorageError("browse_copy_checksum_mismatch")
            self.storage.check(check_space=False)
            # Catch edits made while copying before replacing an owned destination.
            if target.exists() and digest(target) not in (pending["sha256"], pending.get("previous_hash")):
                raise StorageError("browse_file_modified_outside_backup")
            os.replace(temporary, target)
        if digest(target) != pending["sha256"]:
            raise StorageError("browse_readback_checksum_mismatch")
        if temporary.exists():
            self.storage.remove(pending["temporary"])
        with self.db.transaction() as conn:
            if entry["relative_path"] and entry["relative_path"] != pending["path"]:
                conn.execute("INSERT OR REPLACE INTO browse_cleanup VALUES (?,?,?)",
                             (entry["relative_path"], entry["object_id"], entry["sha256"]))
            conn.execute("""UPDATE browse_files SET relative_path=?,object_id=?,sha256=?,pending=NULL,error=NULL
                WHERE drive_id=? AND item_id=?""", (pending["path"], pending["object_id"], pending["sha256"],
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
        self.storage.check(check_space=False)
        failed, published = 0, 0
        reserved = {}
        for entry in self.db.all("SELECT * FROM browse_files"):
            identity = (entry["drive_id"], entry["item_id"])
            if entry["relative_path"]:
                reserved[entry["relative_path"].casefold()] = identity
            if entry["pending"]:
                reserved[json.loads(entry["pending"])["path"].casefold()] = identity
        # A stale copy is not available for another item until cleanup succeeds.
        for row in self.db.all("SELECT relative_path FROM browse_cleanup"):
            reserved.setdefault(row["relative_path"].casefold(), ("cleanup", "cleanup"))
        rows = self.db.all("""SELECT o.*,i.payload AS source_payload,i.deleted FROM items i JOIN objects o
            ON i.object_id=o.object_id WHERE NOT EXISTS
            (SELECT 1 FROM purge_jobs p WHERE p.resource_id=o.object_id AND p.state='pending')
            ORDER BY o.drive_id,o.item_id""")
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
                if same_path and entry["object_id"] == row["object_id"] and target.is_file() and digest(target) == row["sha256"]:
                    if entry["error"]:
                        with self.db.transaction() as conn:
                            conn.execute("UPDATE browse_files SET error=NULL WHERE drive_id=? AND item_id=?", identity)
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
                published += 1
            except (OSError, StorageError, ValueError) as exc:
                failed += 1
                with self.db.transaction() as conn:
                    conn.execute("INSERT OR IGNORE INTO browse_files(drive_id,item_id) VALUES (?,?)", identity)
                    conn.execute("UPDATE browse_files SET error=? WHERE drive_id=? AND item_id=?",
                                 (str(exc) if isinstance(exc, StorageError) else type(exc).__name__, *identity))
                log.error("탐색기 보기 갱신 실패 drive=%s item=%s type=%s", *identity, type(exc).__name__)
        failed += self.cleanup()
        return {"published": published, "failed": failed, "path": str(self.storage.root / "browse")}
