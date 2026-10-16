import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from onedrive_backup.compat import is_path_within
from onedrive_backup.db import Database
from tools.check_py38_compat import scan


class Python38CompatibilityTest(unittest.TestCase):
    def test_project_sources_use_python38_compatible_features(self):
        root = Path(__file__).resolve().parent.parent
        self.assertEqual(scan(root), [])

    def test_path_containment_helper_replaces_pathlib_python39_api(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            self.assertTrue(is_path_within(root, root))
            self.assertTrue(is_path_within(root / "child", root))
            self.assertFalse(is_path_within(root.parent / "outside", root))

    def test_old_sqlite_gets_clear_compatibility_error(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch("onedrive_backup.db.sqlite3.sqlite_version_info", (3, 23, 0)):
                with self.assertRaises(sqlite3.NotSupportedError):
                    Database(str(Path(folder) / "state.sqlite"), "tenant", str(Path(folder) / "backup"))
