import hashlib
import json
import logging
import os
import shutil
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .compat import is_path_within

log = logging.getLogger(__name__)


class StorageError(RuntimeError):
    pass


def storage_error_detail(exc):
    messages = {
        "mount_access_denied": "백업 경로 또는 식별 파일 접근이 거부되었습니다. Cloudium의 실행 프로그램 허용 정책과 실행 계정 권한을 확인하세요.",
        "mount_marker_missing": "백업 폴더 또는 식별 파일을 찾을 수 없습니다. backup_root와 mount_marker_name 및 Cloudium 로그인 상태를 확인하세요.",
        "mount_identity_mismatch": "식별 파일 내용이 mount_marker_value와 다릅니다. 올바른 백업 대상인지 확인하세요.",
        "insufficient_free_space": "백업 대상의 여유 공간이 minimum_free_bytes 조건을 충족하지 못합니다.",
        "another_process_is_running": "로컬 잠금 파일을 잠글 수 없습니다. 중복 실행과 sqlite_path의 접근 권한을 확인하세요.",
    }
    code = str(exc)
    # Only expose known diagnostic codes, never arbitrary exception payloads.
    if code in messages:
        return f"StorageError [{code}]: {messages[code]}"
    return "StorageError"


DEFAULT_IO_CHUNK_BYTES = 8 * 1024 * 1024


def digest(path, chunk_bytes=DEFAULT_IO_CHUNK_BYTES):
    result = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(chunk_bytes), b""):
            result.update(block)
    return result.hexdigest()


class Storage:
    def __init__(self, config):
        self.config = config
        self.root = Path(config.backup_root).resolve()
        self._reservation_lock = threading.Lock()
        self._reserved_bytes = 0
        self._directory_lock = threading.Lock()
        self._created_directories = set()

    def check(self, expected_size=0, *, check_space=True):
        marker = self.root / self.config.mount_marker_name
        try:
            if not stat.S_ISDIR(self.root.stat().st_mode) or not stat.S_ISREG(marker.stat().st_mode):
                raise StorageError("mount_marker_missing")
            if marker.read_text(encoding="utf-8").strip() != self.config.mount_marker_value:
                raise StorageError("mount_identity_mismatch")
            if check_space and shutil.disk_usage(self.root).free < expected_size + self.config.minimum_free_bytes:
                raise StorageError("insufficient_free_space")
        except PermissionError:
            raise StorageError("mount_access_denied") from None
        except FileNotFoundError:
            raise StorageError("mount_marker_missing") from None

    def path(self, relative):
        path = (self.root / relative).resolve()
        if not is_path_within(path, self.root) or path == self.root:
            raise StorageError("path_outside_backup_root")
        return path

    def exists(self, relative, size):
        path = self.path(relative)
        return path.is_file() and path.stat().st_size == size

    def _write_path(self, relative):
        # Serialize first creation only. Parallel mkdir/resolve on a new shared
        # parent can race on virtual mounts; repeated mkdir is expensive on NAS.
        parent = relative.rsplit("/", 1)[0]
        with self._directory_lock:
            if parent not in self._created_directories:
                self.path(relative).parent.mkdir(parents=True, exist_ok=True)
                self._created_directories.add(parent)
        return self.path(relative)

    def remove(self, relative):
        self.check(check_space=False)
        # Resolve and enforce containment before every individual unlink. Never recurse.
        path = self.path(relative)
        path.unlink(missing_ok=True)

    @contextmanager
    def reserve(self, size):
        with self._reservation_lock:
            self.check(size + self._reserved_bytes)
            self._reserved_bytes += size
        try:
            yield
        finally:
            with self._reservation_lock:
                self._reserved_bytes -= size

    def put(self, stream, size, progress=None, timings=None, verify=True):
        timings = timings if timings is not None else {}
        timings.update({"network_read": 0.0, "disk_write": 0.0, "readback": 0.0})
        with self.reserve(size):
            object_id = uuid.uuid4().hex
            relative = f"objects/{object_id[:2]}/{object_id}.bin"
            path = self._write_path(relative)
            checksum, received = hashlib.sha256(), 0
            # Unique immutable object. Only the committed DB/manifest makes it visible.
            # No atomic rename or replacement support is required from the mount.
            owned = False
            try:
                with path.open("xb") as handle:
                    owned = True
                    while True:
                        operation_started = time.perf_counter()
                        block = stream.read(self.config.download_chunk_bytes)
                        timings["network_read"] += time.perf_counter() - operation_started
                        if not block:
                            break
                        received += len(block)
                        if received > size:
                            raise StorageError("download_larger_than_metadata")
                        operation_started = time.perf_counter()
                        handle.write(block)
                        timings["disk_write"] += time.perf_counter() - operation_started
                        checksum.update(block)
                        if progress:
                            progress(len(block))
                    operation_started = time.perf_counter()
                    handle.flush()
                    os.fsync(handle.fileno())
                    timings["disk_write"] += time.perf_counter() - operation_started
                if received != size:
                    raise StorageError("download_size_mismatch")
                if verify:
                    operation_started = time.perf_counter()
                    self.verify(relative, size, checksum.hexdigest())
                    timings["readback"] = time.perf_counter() - operation_started
            except Exception:
                # Only this newly created, uncommitted object may be removed.
                # If the mount is unavailable, keep the original failure and report
                # that a partial object remains instead of masking the cause.
                if owned:
                    try:
                        self.remove(relative)
                    except (OSError, StorageError):
                        log.warning("미완료 객체 정리 보류 object_id=%s", object_id)
                raise
            return object_id, relative, checksum.hexdigest()

    def verify(self, relative, size, sha256):
        self.check(check_space=False)
        path = self.path(relative)
        if path.stat().st_size != size or digest(path, self.config.download_chunk_bytes) != sha256:
            raise StorageError("readback_checksum_mismatch")

    def manifest(self, object_id, payload):
        path = self._write_path(f"manifests/{object_id}.json")
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
