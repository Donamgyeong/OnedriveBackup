import io
import json
import tempfile
import unittest
import argparse
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from onedrive_backup.config import Config, ConfigError
from onedrive_backup.cli import execute
from onedrive_backup.db import Database, encode
from onedrive_backup.engine import Engine
from onedrive_backup.graph import GraphClient, GraphError
from onedrive_backup.storage import ProcessLock, Storage, StorageError
from onedrive_backup.retention import Retention
from onedrive_backup.browse import BrowsePublisher, safe_name


def file_item(tag="v1", name="document.txt"):
    return {"id": "file1", "name": name, "file": {}, "size": 5, "cTag": tag,
            "eTag": "metadata-" + tag, "parentReference": {"id": "root1"}}


ROOT = {"id": "root1", "name": "root", "folder": {}, "root": {}}


class FakeGraph:
    def __init__(self):
        self.responses = []
        self.current = file_item()
        self.download_count = 0
        self.on_download = None
        self.requests = []

    def get(self, url):
        self.requests.append(url)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def metadata(self, *args):
        return dict(self.current)

    def download(self, item):
        self.download_count += 1
        if self.on_download:
            self.on_download()
        return io.BytesIO(b"hello")


class BackupTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.mount = root / "mount"
        self.mount.mkdir()
        (self.mount / "marker").write_text("test-storage", encoding="utf-8")
        self.cfg = Config("tenant", "client", "TEST_SECRET", "https://login.microsoftonline.com",
                          "https://graph.microsoft.com/v1.0", str(root / "state.sqlite"), str(self.mount),
                          "marker", "test-storage", 60, 0, 3, 1, 30, 2, "keep_all", "report", "include")
        self.db = Database(self.cfg.sqlite_path, "tenant", self.mount)
        self.addCleanup(self.db.close)
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO drives(drive_id,user_id,status) VALUES ('drive1','user1','active')")
        self.graph = FakeGraph()
        self.storage = Storage(self.cfg)
        self.engine = Engine(self.cfg, self.db, self.graph, self.storage)

    def scan(self, items, token="delta1"):
        self.graph.responses.append({"value": items, "@odata.deltaLink": token})
        self.engine.scan("drive1")

    def first_backup(self):
        self.scan([ROOT, file_item()])
        self.engine.process_jobs()

    def test_initial_then_unchanged_and_rename_do_not_download(self):
        self.first_backup()
        self.scan([file_item(name="renamed.txt")], "delta2")
        self.engine.process_jobs()
        self.assertEqual(self.graph.download_count, 1)
        self.assertEqual(self.engine.original_parts("drive1", "file1"), ["renamed.txt"])
        self.assertEqual(self.db.one("SELECT delta_link FROM drives")[0], "delta2")

    def test_content_change_keeps_previous_version(self):
        self.first_backup()
        self.graph.current = file_item("v2")
        self.scan([self.graph.current], "delta2")
        self.engine.process_jobs()
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM objects")[0], 2)

    def test_page_resume_keeps_old_cursor_until_jobs_commit(self):
        self.graph.responses = [{"value": [ROOT], "@odata.nextLink": "page2"}, GraphError(503)]
        with self.assertRaises(GraphError):
            self.engine.scan("drive1")
        self.assertIsNone(self.db.one("SELECT delta_link FROM drives")[0])
        self.assertEqual(self.db.one("SELECT next_link FROM scans")[0], "page2")
        self.scan([file_item()])
        self.assertEqual(self.graph.requests[-1], "page2")
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM jobs")[0], 1)

    def test_failed_final_transaction_does_not_advance_cursor(self):
        self.graph.responses = [{"value": [file_item()], "@odata.deltaLink": "new"}]
        with patch.object(self.db, "queue", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.engine.scan("drive1")
        self.assertIsNone(self.db.one("SELECT delta_link FROM drives")[0])
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM items")[0], 0)

    def test_duplicate_item_last_state_wins(self):
        self.graph.responses = [{"value": [file_item("v1")], "@odata.nextLink": "page2"},
                                {"value": [file_item("v2")], "@odata.deltaLink": "delta"}]
        self.engine.scan("drive1")
        self.assertEqual(json.loads(self.db.one("SELECT payload FROM items")[0])["cTag"], "v2")
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM jobs")[0], 1)

    def test_expired_token_full_scan_does_not_download_same_content(self):
        self.first_backup()
        self.graph.responses = [GraphError(410), {"value": [ROOT, file_item()], "@odata.deltaLink": "fresh"}]
        self.engine.scan("drive1")
        self.engine.process_jobs()
        self.assertEqual(self.graph.download_count, 1)

    def test_deleted_source_keeps_backup(self):
        self.first_backup()
        relative = self.db.one("SELECT relative_path FROM objects")[0]
        self.scan([{"id": "file1", "deleted": {}}])
        self.assertEqual(self.db.one("SELECT deleted FROM items WHERE item_id='file1'")[0], 1)
        self.assertTrue(self.storage.path(relative).exists())

    def test_deletion_before_download_is_reported_as_gap(self):
        self.scan([ROOT, file_item()])
        self.scan([{"id": "file1", "deleted": {}}])
        self.assertEqual(self.db.one("SELECT error FROM jobs")[0], "source_deleted_before_backup")

    def test_deleted_folder_marks_descendants(self):
        child = file_item()
        child["parentReference"] = {"id": "folder1"}
        self.scan([ROOT, {"id": "folder1", "name": "folder", "folder": {}, "parentReference": {"id": "root1"}}, child])
        self.scan([{"id": "folder1", "deleted": {}}])
        self.assertEqual(self.db.one("SELECT deleted FROM items WHERE item_id='file1'")[0], 1)

    def test_source_changes_during_download_never_commits(self):
        self.scan([ROOT, file_item()])
        self.graph.on_download = lambda: setattr(self.graph, "current", file_item("v2"))
        self.engine.process_jobs()
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM objects")[0], 0)
        self.assertEqual(self.db.one("SELECT state FROM jobs")[0], "pending")

    def test_missing_and_corrupt_files_are_requeued(self):
        self.first_backup()
        path = self.storage.path(self.db.one("SELECT relative_path FROM objects")[0])
        path.write_bytes(b"wrong")
        self.assertEqual(self.engine.repair_missing(verify_hash=True), 1)
        self.engine.process_jobs()
        latest = self.db.one("SELECT o.relative_path FROM items i JOIN objects o ON i.object_id=o.object_id")[0]
        self.assertEqual(self.storage.path(latest).read_bytes(), b"hello")

    def test_mount_identity_failure_blocks_writes(self):
        (self.mount / "marker").write_text("different", encoding="utf-8")
        with self.assertRaises(StorageError):
            self.storage.put(io.BytesIO(b"hello"), 5)
        self.assertFalse((self.mount / "objects").exists())

    def test_short_download_not_registered(self):
        self.scan([ROOT, file_item()])
        with patch.object(self.graph, "download", return_value=io.BytesIO(b"hi")):
            with self.assertRaises(StorageError):
                self.engine.process_jobs()
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM objects")[0], 0)

    def test_signed_url_is_not_persisted(self):
        self.assertNotIn("secret", encode({"@microsoft.graph.downloadUrl": "secret", "id": "1"}))

    def test_config_placeholders_fail_before_execution(self):
        path = Path(self.temp.name) / "config.json"
        path.write_text(json.dumps({key: None for key in asdict(self.cfg)}), encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "미설정"):
            Config.load(path)

    def test_untrusted_delta_link_cannot_receive_credentials(self):
        with self.assertRaises(GraphError):
            GraphClient(self.cfg).get("https://attacker.invalid/v1.0/users")

    def test_download_has_no_authorization_header(self):
        client = GraphClient(self.cfg)
        with patch.object(client, "_open", return_value=io.BytesIO()) as opened:
            client.download({"@microsoft.graph.downloadUrl": "https://files.example.test/document"})
            self.assertIsNone(opened.call_args.args[0].get_header("Authorization"))

    def test_lock_prevents_duplicate_process(self):
        with ProcessLock(self.cfg.sqlite_path + ".lock"):
            with self.assertRaises(StorageError):
                with ProcessLock(self.cfg.sqlite_path + ".lock"):
                    pass

    def test_failed_download_survives_new_delta_cursor(self):
        self.scan([ROOT, file_item()])
        with patch.object(self.graph, "metadata", side_effect=GraphError(503)):
            self.engine.process_jobs()
        self.scan([], "delta2")
        self.assertEqual(self.db.one("SELECT delta_link FROM drives")[0], "delta2")
        self.assertEqual(self.db.one("SELECT state FROM jobs")[0], "pending")

    def test_repeat_delta_does_not_reset_failed_attempts(self):
        self.scan([ROOT, file_item()])
        with self.db.transaction() as conn:
            conn.execute("UPDATE jobs SET state='failed',attempts=3")
        self.scan([file_item()], "delta2")
        self.assertEqual(self.db.one("SELECT attempts FROM jobs")[0], 3)
        self.assertEqual(self.db.one("SELECT state FROM jobs")[0], "failed")

    def test_missing_file_check_preserves_retry_limit(self):
        self.first_backup()
        self.storage.path(self.db.one("SELECT relative_path FROM objects")[0]).unlink()
        self.engine.repair_missing()
        with self.db.transaction() as conn:
            conn.execute("UPDATE jobs SET state='failed',attempts=3")
        self.engine.repair_missing()
        self.scan([file_item()], "delta2")
        self.assertEqual(self.db.one("SELECT state FROM jobs")[0], "failed")

    def test_restore_verifies_bytes_and_refuses_overwrite(self):
        self.first_backup()
        output = str(Path(self.temp.name) / "restored.txt")
        object_id = self.db.one("SELECT object_id FROM objects")[0]
        args = argparse.Namespace(command="restore", object_id=object_id, output=output)
        with patch("builtins.print"):
            self.assertEqual(execute(args, self.cfg, self.db, self.storage), 0)
        self.assertEqual(Path(output).read_bytes(), b"hello")
        with self.assertRaises(FileExistsError):
            execute(args, self.cfg, self.db, self.storage)

    def test_full_run_creates_verified_catalog(self):
        pages = lambda url: iter([{"value": [{"id": "user1"}]}] if url.startswith("/users?") else
                                 [{"value": [{"id": "drive1", "driveType": "business"}]}])
        self.graph.responses = [{"value": [ROOT, file_item()], "@odata.deltaLink": "delta1"}]
        with patch.object(self.graph, "pages", side_effect=pages, create=True):
            report = self.engine.run()
        self.assertEqual(report["status"], "success")
        self.assertEqual(report["browse"]["published"], 1)
        self.assertEqual(self.view_path().read_bytes(), b"hello")
        self.assertEqual(len(list((self.mount / "catalogs").glob("*.sqlite"))), 1)

    def test_restart_recovers_running_jobs(self):
        self.scan([ROOT, file_item()])
        with self.db.transaction() as conn:
            conn.execute("UPDATE jobs SET state='running'")
        self.db.close()
        resumed = Database(self.cfg.sqlite_path, "tenant", self.mount)
        try:
            self.assertEqual(resumed.one("SELECT state FROM jobs")[0], "pending")
            Engine(self.cfg, resumed, self.graph, self.storage).process_jobs()
            self.assertEqual(resumed.one("SELECT COUNT(*) FROM objects")[0], 1)
        finally:
            resumed.close()

    def retention(self):
        return Retention(replace(self.cfg, retention_policy="expire_after_days", retention_days=1), self.db, self.storage)

    def second_backup(self):
        self.graph.current = file_item("v2")
        self.scan([self.graph.current], "delta2")
        self.engine.process_jobs()

    def age_objects(self, created=100):
        with self.db.transaction() as conn:
            conn.execute("UPDATE objects SET created=?", (created,))

    def test_retention_exact_boundary_and_dry_run(self):
        self.first_backup()
        self.second_backup()
        self.age_objects()
        self.assertEqual(self.retention().prune(dry_run=True, now=86499)["count"], 0)
        self.assertEqual(self.retention().prune(dry_run=True, now=86500)["count"], 1)
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM purge_jobs")[0], 0)
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM objects")[0], 2)

    def test_retention_keeps_latest_even_after_expiry(self):
        self.first_backup()
        self.age_objects()
        obj = self.db.one("SELECT * FROM objects")
        self.assertEqual(self.retention().prune(now=86500)["completed"], 0)
        self.assertTrue(self.storage.path(obj["relative_path"]).exists())
        self.assertTrue(self.storage.path(f"manifests/{obj['object_id']}.json").exists())
        self.scan([file_item()], "delta2")
        self.engine.repair_missing(verify_hash=True)
        self.engine.process_jobs()
        self.assertEqual(self.graph.download_count, 1)

    def test_retention_keeps_unexpired_new_version(self):
        self.first_backup()
        self.age_objects()
        self.graph.current = file_item("v2")
        self.scan([self.graph.current])
        self.engine.process_jobs()
        self.retention().prune(now=86500)
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM objects")[0], 1)
        self.assertIsNotNone(self.db.one("SELECT object_id FROM items WHERE item_id='file1'")[0])

    def test_retention_keep_all_does_not_dispose(self):
        self.first_backup()
        self.age_objects()
        report = Retention(self.cfg, self.db, self.storage).prune(now=86500)
        self.assertEqual(report["completed"], 0)

    def test_retention_resumes_after_manifest_delete_failure(self):
        self.first_backup()
        self.second_backup()
        self.age_objects()
        original = self.storage.remove
        def fail_manifest(relative):
            if relative.startswith("manifests/"):
                raise PermissionError("locked")
            return original(relative)
        with patch.object(self.storage, "remove", side_effect=fail_manifest):
            self.assertEqual(self.retention().prune(now=86500)["failed"], 1)
        self.assertEqual(self.engine.repair_missing(), 0)
        self.assertEqual(self.db.one("SELECT state FROM purge_jobs")[0], "pending")
        # Already committed disposal finishes even if policy is changed to keep_all.
        self.assertEqual(Retention(self.cfg, self.db, self.storage).prune(now=86501)["completed"], 1)
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM objects")[0], 1)

    def test_retention_preserves_newer_pending_source_job(self):
        self.first_backup()
        self.age_objects()
        self.scan([file_item("v2")])
        self.retention().prune(now=86500)
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM jobs")[0], 1)

    def test_retention_removes_catalog_and_manifest(self):
        relative = "catalogs/1-" + "a" * 32 + ".sqlite"
        resource = "catalog-" + "b" * 32
        path = self.storage.path(relative)
        path.parent.mkdir()
        self.db.snapshot(path)
        self.storage.manifest(resource, {"kind": "catalog", "relative_path": relative, "created": 100})
        self.assertEqual(self.retention().prune(now=86500)["completed"], 1)
        self.assertFalse(path.exists())
        self.assertFalse(self.storage.path(f"manifests/{resource}.json").exists())

    def test_retention_refuses_outside_path(self):
        self.first_backup()
        self.age_objects()
        outside = Path(self.temp.name) / "important.txt"
        outside.write_text("keep")
        with self.db.transaction() as conn:
            conn.execute("UPDATE objects SET relative_path=?", (str(outside),))
        with self.assertRaises(StorageError):
            self.retention().prune(now=86500)
        self.assertEqual(outside.read_text(), "keep")

    def test_retention_mount_identity_blocks_deletion(self):
        self.first_backup()
        self.age_objects()
        (self.mount / "marker").write_text("wrong")
        with self.assertRaises(StorageError):
            self.retention().prune(now=86500)
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM objects")[0], 1)

    def test_retention_works_even_when_storage_below_free_threshold(self):
        self.first_backup()
        self.second_backup()
        self.age_objects()
        with patch("onedrive_backup.storage.shutil.disk_usage", side_effect=AssertionError("must not need free space")):
            self.assertEqual(self.retention().prune(now=86500)["completed"], 1)

    def test_retention_config_requires_positive_days_only_for_expiration(self):
        config_path = Path(self.temp.name) / "config.json"
        values = asdict(self.cfg)
        config_path.write_text(json.dumps(values))
        self.assertIsNone(Config.load(config_path).retention_days)
        values["retention_policy"] = "expire_after_days"
        for invalid in (None, 0, -1, True, "30"):
            values["retention_days"] = invalid
            config_path.write_text(json.dumps(values))
            with self.assertRaises(ConfigError):
                Config.load(config_path)
        values["retention_days"] = 30
        config_path.write_text(json.dumps(values))
        self.assertEqual(Config.load(config_path).retention_days, 30)

    def test_run_automatically_prunes_expired_backups(self):
        self.first_backup()
        self.second_backup()
        self.age_objects()
        self.engine.config = replace(self.cfg, retention_policy="expire_after_days", retention_days=1)
        pages = lambda url: iter([{"value": [{"id": "user1"}]}] if url.startswith("/users?") else
                                 [{"value": [{"id": "drive1", "driveType": "business"}]}])
        self.graph.responses = [{"value": [], "@odata.deltaLink": "delta2"}]
        with patch.object(self.graph, "pages", side_effect=pages, create=True):
            report = self.engine.run()
        self.assertEqual(report["retention"]["completed"], 1)
        self.assertEqual(report["status"], "success")
        self.assertEqual(self.graph.download_count, 2)

    def test_pending_purge_blocks_restore_and_redownload(self):
        self.first_backup()
        self.second_backup()
        self.age_objects()
        object_id = self.db.one("SELECT object_id FROM objects WHERE object_id NOT IN (SELECT object_id FROM items WHERE object_id IS NOT NULL)")[0]
        with patch.object(self.storage, "remove", side_effect=PermissionError("locked")):
            self.retention().prune(now=86500)
        args = argparse.Namespace(command="restore", object_id=object_id,
                                  output=str(Path(self.temp.name) / "restore.txt"))
        with self.assertRaises(ValueError):
            execute(args, self.cfg, self.db, self.storage)
        self.engine.process_jobs()
        self.assertEqual(self.graph.download_count, 2)

    def test_deleted_source_still_keeps_last_backup(self):
        self.first_backup()
        self.age_objects()
        self.scan([{"id": "file1", "deleted": {}}])
        self.assertEqual(self.retention().prune(now=86500)["completed"], 0)
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM objects")[0], 1)

    def test_corrupt_latest_does_not_discard_healthy_previous(self):
        self.first_backup()
        self.second_backup()
        self.age_objects()
        latest = self.db.one("SELECT o.* FROM objects o JOIN items i ON i.object_id=o.object_id")
        self.storage.path(latest["relative_path"]).write_bytes(b"wrong")
        self.assertEqual(self.retention().prune(now=86500)["completed"], 0)
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM objects")[0], 2)

    def legacy_purge(self):
        obj = self.db.one("SELECT * FROM objects")
        with self.db.transaction() as conn:
            conn.execute("""INSERT INTO purge_jobs(resource_id,kind,relative_path,manifest_path,requested)
                VALUES (?,'object',?,?,100)""", (obj["object_id"], obj["relative_path"], f"manifests/{obj['object_id']}.json"))
        return obj

    def test_legacy_pending_last_version_purge_is_cancelled(self):
        self.first_backup()
        obj = self.legacy_purge()
        self.assertEqual(self.retention().prune(dry_run=True, now=86500)["count"], 0)
        self.retention().prune(now=86500)
        self.assertEqual(self.db.one("SELECT state FROM purge_jobs")[0], "cancelled")
        self.assertTrue(self.storage.path(obj["relative_path"]).exists())

    def test_partially_deleted_last_version_is_recovered(self):
        self.first_backup()
        obj = self.legacy_purge()
        self.storage.path(obj["relative_path"]).unlink()
        self.retention().prune(now=86500)
        self.assertEqual(self.engine.repair_missing(), 1)
        self.engine.process_jobs()
        latest = self.db.one("SELECT o.* FROM objects o JOIN items i ON i.object_id=o.object_id")
        self.assertEqual(self.storage.path(latest["relative_path"]).read_bytes(), b"hello")

    def test_formerly_expired_unchanged_file_is_downloaded_again(self):
        self.first_backup()
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM objects")
            conn.execute("UPDATE items SET object_id=NULL")
            conn.execute("INSERT INTO expired_items VALUES ('drive1','file1','c:v1')")
        self.scan([], "delta2")
        self.assertEqual(self.engine.repair_missing(), 1)
        self.engine.process_jobs()
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM objects")[0], 1)
        self.assertEqual(self.graph.download_count, 2)

    def test_already_deleted_source_without_backup_reports_gap(self):
        self.first_backup()
        self.scan([{"id": "file1", "deleted": {}}])
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM objects")
            conn.execute("UPDATE items SET object_id=NULL")
        self.engine.repair_missing()
        self.assertEqual(self.db.one("SELECT error FROM jobs")[0], "source_deleted_no_backup")

    def browse(self):
        return BrowsePublisher(self.cfg, self.db, self.storage)

    def view_path(self, item_id="file1"):
        return self.storage.path(self.db.one("SELECT relative_path FROM browse_files WHERE item_id=?", (item_id,))[0])

    def test_browse_existing_backup_is_readable_without_graph(self):
        self.first_backup()
        before = self.graph.download_count
        result = self.browse().sync()
        self.assertEqual(result["failed"], 0)
        self.assertEqual(self.view_path().name, "document.txt")
        self.assertEqual(self.view_path().read_bytes(), b"hello")
        self.assertEqual(self.graph.download_count, before)
        self.assertEqual(self.browse().sync()["published"], 0)

    def test_browse_update_keeps_version_objects(self):
        self.first_backup()
        self.browse().sync()
        self.graph.current = file_item("v2")
        self.scan([self.graph.current])
        with patch.object(self.graph, "download", return_value=io.BytesIO(b"world")):
            self.engine.process_jobs()
        self.assertEqual(self.browse().sync()["failed"], 0)
        self.assertEqual(self.view_path().read_bytes(), b"world")
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM objects")[0], 2)

    def test_browse_rename_and_deleted_keep_readable_copy(self):
        self.first_backup()
        self.browse().sync()
        previous = self.view_path()
        self.scan([file_item(name="보고서.txt")])
        self.assertEqual(self.browse().sync()["failed"], 0)
        self.assertEqual(self.view_path().name, "보고서.txt")
        self.assertFalse(previous.exists())
        renamed = self.view_path()
        self.scan([{"id": "file1", "deleted": {}}])
        self.assertEqual(self.browse().sync()["failed"], 0)
        self.assertIn("deleted", self.view_path().parts)
        self.assertFalse(renamed.exists())
        self.assertEqual(self.view_path().read_bytes(), b"hello")
        self.assertEqual(self.graph.download_count, 1)

    def test_browse_parent_rename_refreshes_descendant_path(self):
        folder = {"id": "folder", "name": "계약", "folder": {}, "parentReference": {"id": "root1"}}
        self.graph.current = file_item()
        self.graph.current["parentReference"] = {"id": "folder"}
        self.scan([ROOT, folder, self.graph.current])
        self.engine.process_jobs()
        self.browse().sync()
        previous = self.view_path()
        self.scan([{**folder, "name": "계약서"}])
        self.browse().sync()
        self.assertTrue(self.view_path().parent.name.startswith("계약서__"))
        self.assertFalse(previous.exists())
        self.assertEqual(self.graph.download_count, 1)

    def test_browse_user_edit_is_preserved(self):
        self.first_backup()
        self.browse().sync()
        self.view_path().write_bytes(b"my edits")
        self.second_backup()
        self.assertEqual(self.browse().sync()["failed"], 1)
        self.assertEqual(self.view_path().read_bytes(), b"my edits")
        original = self.db.one("SELECT o.* FROM items i JOIN objects o ON i.object_id=o.object_id")
        self.assertEqual(self.storage.path(original["relative_path"]).read_bytes(), b"hello")

    def test_browse_failed_publish_can_resume_without_downloading(self):
        self.first_backup()
        with patch("onedrive_backup.browse.os.replace", side_effect=PermissionError("locked")):
            self.assertEqual(self.browse().sync()["failed"], 1)
        self.assertIsNotNone(self.db.one("SELECT pending FROM browse_files")[0])
        self.assertEqual(self.browse().sync()["failed"], 0)
        self.assertEqual(self.view_path().read_bytes(), b"hello")
        self.assertEqual(self.graph.download_count, 1)
        self.assertFalse(list((self.mount / "browse").rglob("*.partial")))

    def test_browse_no_partial_file_at_original_name(self):
        self.first_backup()
        with patch("onedrive_backup.browse.shutil.copyfileobj", side_effect=OSError("interrupted")):
            self.assertEqual(self.browse().sync()["failed"], 1)
        pending = json.loads(self.db.one("SELECT pending FROM browse_files")[0])
        self.assertFalse(self.storage.path(pending["path"]).exists())
        self.assertEqual(self.browse().sync()["failed"], 0)

    def test_browse_case_collision_preserves_both_files(self):
        self.first_backup()
        other = {**file_item(), "id": "file2", "name": "DOCUMENT.txt"}
        self.graph.current = other
        self.scan([other])
        self.engine.process_jobs()
        self.assertEqual(self.browse().sync()["failed"], 0)
        self.assertNotEqual(str(self.view_path()).casefold(), str(self.view_path("file2")).casefold())
        self.assertEqual(self.view_path("file2").suffix, ".txt")

    def test_browse_invalid_names_and_long_paths(self):
        self.assertTrue(safe_name("CON.txt", "id").startswith("_CON"))
        self.assertNotIn("/", safe_name("../secret.docx", "id"))
        self.assertTrue(safe_name("a" * 200 + ".docx", "id").endswith(".docx"))
        self.first_backup()
        parent = "root1"
        folders = []
        for index in range(8):
            folder = {"id": f"folder{index}", "name": "긴폴더이름" * 6, "folder": {}, "parentReference": {"id": parent}}
            folders.append(folder)
            parent = folder["id"]
        self.scan(folders + [{**file_item(), "parentReference": {"id": parent}}])
        self.assertEqual(self.browse().sync()["failed"], 0)
        self.assertIn("_long_paths", self.view_path().parts)

    def test_browse_blocks_unknown_destination_and_outside_path(self):
        self.first_backup()
        row = self.db.one("SELECT o.*,i.payload AS source_payload,i.deleted FROM objects o JOIN items i ON i.object_id=o.object_id")
        target = self.storage.path(self.browse().destination(row))
        target.parent.mkdir(parents=True)
        target.write_bytes(b"unmanaged")
        self.assertEqual(self.browse().sync()["failed"], 1)
        self.assertEqual(target.read_bytes(), b"unmanaged")
        with self.assertRaises(StorageError):
            self.browse().path("browse/../objects/important.bin")

    def test_browse_failed_update_protects_visible_old_version_from_prune(self):
        self.first_backup()
        self.browse().sync()
        self.age_objects()
        self.second_backup()
        self.assertEqual(self.retention().prune(now=86500)["completed"], 0)
        self.browse().sync()
        self.assertEqual(self.retention().prune(now=86500)["completed"], 1)


if __name__ == "__main__":
    unittest.main()
