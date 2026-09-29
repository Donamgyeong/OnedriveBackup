import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path


class StorageError(RuntimeError):
    pass


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


class Storage:
    def __init__(self, config):
        self.config = config
        self.root = Path(config.backup_root).resolve()

    def check(self, expected_size=0, *, check_space=True):
        marker = self.root / self.config.mount_marker_name
        if not self.root.is_dir() or not marker.is_file():
            raise StorageError("mount_marker_missing")
        if marker.read_text(encoding="utf-8").strip() != self.config.mount_marker_value:
            raise StorageError("mount_identity_mismatch")
        if check_space and shutil.disk_usage(self.root).free < expected_size + self.config.minimum_free_bytes:
            raise StorageError("insufficient_free_space")

    def path(self, relative):
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root) or path == self.root:
            raise StorageError("path_outside_backup_root")
        return path

    def exists(self, relative, size):
        path = self.path(relative)
        return path.is_file() and path.stat().st_size == size

    def remove(self, relative):
        self.check(check_space=False)
        # Resolve and enforce containment before every individual unlink. Never recurse.
        path = self.path(relative)
        path.unlink(missing_ok=True)

    def put(self, stream, size):
        self.check(size)
        object_id = uuid.uuid4().hex
        relative = f"objects/{object_id[:2]}/{object_id}.bin"
        path = self.path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        checksum, received = hashlib.sha256(), 0
        # Unique immutable object. Only the committed DB/manifest makes it visible.
        # No atomic rename or replacement support is required from the mount.
        with path.open("xb") as handle:
            while block := stream.read(1024 * 1024):
                received += len(block)
                if received > size:
                    raise StorageError("download_larger_than_metadata")
                self.check(len(block))
                handle.write(block)
                checksum.update(block)
            handle.flush()
            os.fsync(handle.fileno())
        if received != size:
            raise StorageError("download_size_mismatch")
        self.check()
        if digest(path) != checksum.hexdigest():
            raise StorageError("readback_checksum_mismatch")
        return object_id, relative, checksum.hexdigest()

    def manifest(self, object_id, payload):
        path = self.path(f"manifests/{object_id}.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.check(len(encoded))
        with path.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if path.read_bytes() != encoded:
            raise StorageError("manifest_readback_mismatch")


class ProcessLock:
    """OS-held lock, automatically released on process death."""
    def __init__(self, path):
        self.path = Path(path)

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0, 2)
        if self.handle.tell() == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise StorageError("another_process_is_running") from None
        return self

    def __exit__(self, *args):
        self.handle.close()
