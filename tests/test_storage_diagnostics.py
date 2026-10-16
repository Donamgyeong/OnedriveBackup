import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from onedrive_backup.cli import main
from onedrive_backup.storage import Storage, StorageError, storage_error_detail


class StorageDiagnosticsTest(unittest.TestCase):
    def test_cli_reports_known_cause_without_arbitrary_payloads(self):
        for code, expected in (("mount_access_denied", "mount_access_denied"),
                               ("https://secret.example/token", "실행할 수 없습니다: StorageError\n")):
            output = io.StringIO()
            with patch("onedrive_backup.cli.Config.load", side_effect=StorageError(code)), redirect_stderr(output):
                self.assertEqual(main(["run"]), 1)
            self.assertIn(expected, output.getvalue())
            self.assertNotIn("secret.example", output.getvalue())

    def test_missing_and_denied_marker_are_distinct(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage(SimpleNamespace(backup_root=folder, mount_marker_name="marker",
                                              mount_marker_value="expected", minimum_free_bytes=0))
            with self.assertRaisesRegex(StorageError, "mount_marker_missing"):
                storage.check(check_space=False)
            with patch.object(Path, "stat", side_effect=PermissionError("private payload")):
                with self.assertRaisesRegex(StorageError, "mount_access_denied"):
                    storage.check(check_space=False)
            (Path(folder) / "marker").write_text("wrong", encoding="utf-8")
            with self.assertRaisesRegex(StorageError, "mount_identity_mismatch"):
                storage.check(check_space=False)

    def test_lock_message_does_not_assume_duplicate_process(self):
        self.assertIn("접근 권한", storage_error_detail(StorageError("another_process_is_running")))
