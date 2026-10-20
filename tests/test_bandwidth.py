import io
import threading
import time
import unittest
from dataclasses import replace
from unittest.mock import patch

import test_backup as fixtures
from onedrive_backup.db import encode
from onedrive_backup.engine import Engine
from onedrive_backup.graph import GraphClient, GraphError
from onedrive_backup.scheduling import DownloadQueue


class BandwidthTest(unittest.TestCase):
    setUp = fixtures.BackupTest.setUp

    def queue(self, item_id, size=5, drive="drive1"):
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO items(drive_id,item_id,payload) VALUES (?,?,?)",
                         (drive, item_id, encode({**fixtures.file_item(), "id": item_id, "size": size})))
            self.db.queue(drive, item_id)

    def test_mixed_queue_reaches_large_files_beyond_fifo_page(self):
        for i in range(300):
            self.queue("small" + str(i))
        self.queue("large", 128 * 1024 ** 2)
        horizon = self.db.one("SELECT MAX(job_id) FROM jobs")[0]
        self.engine._download_totals((time.time(), horizon))
        queue = DownloadQueue(self.db)
        self.assertEqual(queue.pop([])["item_id"], "large")
        seen = []
        while True:
            job = queue.pop([])
            if job is None:
                break
            seen.append(job["job_id"])
        self.assertEqual(len(seen), 300)
        self.assertEqual(len(set(seen)), 300)

    def test_mixed_queue_balances_sizes_and_drives(self):
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO drives(drive_id,user_id,status) VALUES ('drive2','user2','active')")
        for i in range(16):
            for name, size in (("small", 5), ("medium", 2 * 1024 ** 2), ("large", 128 * 1024 ** 2)):
                self.queue(name + str(i), size, "drive1" if i < 8 else "drive2")
        self.engine._download_totals((time.time(), 10000))
        queue = DownloadQueue(self.db)
        active = []
        for _ in range(8):
            active.append(queue.pop(active))
        self.assertEqual([sum(job["category"] == i for job in active) for i in range(3)], [2, 2, 4])
        self.assertEqual(sum(job["drive_id"] == "drive1" for job in active), 4)

    def test_readback_does_not_occupy_download_slots(self):
        third = threading.Event()
        original = self.storage.verify

        class Graph(fixtures.FakeGraph):
            def metadata(self, drive, item):
                return {**fixtures.file_item(), "id": item}

            def download(self, item):
                if item["id"] == "f2":
                    third.set()
                return io.BytesIO(b"hello")

        def verify(*args):
            if not third.wait(5):
                raise AssertionError("verification blocked network refilling")
            return original(*args)

        for i in range(3):
            self.queue("f" + str(i))
        with patch.object(self.storage, "verify", side_effect=verify):
            Engine(replace(self.cfg, download_concurrency=2), self.db, Graph(), self.storage).process_jobs()
        self.assertTrue(third.is_set())
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM objects")[0], 3)

    def test_failed_readback_never_commits_backup(self):
        from onedrive_backup.storage import StorageError
        self.queue("f0")
        with patch.object(self.storage, "verify", side_effect=StorageError("readback_checksum_mismatch")):
            with self.assertRaises(StorageError):
                self.engine.process_jobs()
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM objects")[0], 0)
        self.assertEqual(len(list((self.mount / "objects").rglob("*.bin"))), 0)

    def test_metadata_prefetch_overlaps_busy_downloads(self):
        prefetched = threading.Event()

        class Graph(fixtures.FakeGraph):
            def metadata_batch(self, identities):
                if any(item == "f2" for drive, item in identities):
                    prefetched.set()
                return [{**fixtures.file_item(), "id": item} for drive, item in identities]

            def download(self, item):
                if not prefetched.wait(5):
                    raise TimeoutError("metadata was not prefetched while streams were busy")
                return io.BytesIO(b"hello")

        for i in range(3):
            self.queue("f" + str(i))
        Engine(replace(self.cfg, download_concurrency=2), self.db, Graph(), self.storage).process_jobs()
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM objects")[0], 3)

    def test_verification_backpressure_stops_admission(self):
        verification_started, release = threading.Event(), threading.Event()
        errors = []
        graph = fixtures.FakeGraph()
        original = self.storage.verify

        def verify(*args):
            verification_started.set()
            if not release.wait(5):
                raise AssertionError("verification was not released")
            original(*args)

        def observe():
            try:
                if not verification_started.wait(5):
                    raise AssertionError("verification did not start")
                # No new download may start until this first verification drains.
                time.sleep(0.1)
                if graph.download_count != 1:
                    raise AssertionError("backpressure admitted another stream")
            except BaseException as exc:
                errors.append(exc)
            finally:
                release.set()

        for i in range(5):
            self.queue("f" + str(i))
        observer = threading.Thread(target=observe)
        observer.start()
        try:
            with patch.object(self.storage, "verify", side_effect=verify):
                cfg = replace(self.cfg, download_concurrency=1, verification_backlog_bytes=1)
                Engine(cfg, self.db, graph, self.storage).process_jobs()
        finally:
            release.set()
            observer.join(5)
        self.assertFalse(errors, errors)
        self.assertEqual(self.db.one("SELECT COUNT(*) FROM objects")[0], 5)


class Response(io.BytesIO):
    def __init__(self, body, status=200, headers=None):
        super().__init__(body)
        self.status, self.headers = status, headers or {}

    def getcode(self):
        return self.status


class ResumeTest(unittest.TestCase):
    def client(self):
        # Reuse the normal configuration fixture without real network requests.
        cfg = type("Config", (), {"download_concurrency": 4, "download_concurrency_max": 8,
                                 "request_units_per_second": 0, "max_http_attempts": 2,
                                 "retry_base_seconds": 1})()
        client = GraphClient(cfg)
        self.addCleanup(client.close)
        return client

    def test_truncated_stream_resumes_at_delivered_offset(self):
        client = self.client()
        first = Response(b"he", headers={"ETag": '"v1"'})
        second = Response(b"llo", 206, {"Content-Range": "bytes 2-4/5"})
        with patch.object(client, "_open", side_effect=[first, second]) as opened, patch("onedrive_backup.graph.time.sleep"):
            with client.download({"size": 5, "@microsoft.graph.downloadUrl": "https://files.test/data"}) as stream:
                self.assertEqual(stream.read(8), b"he")
                self.assertEqual(stream.read(8), b"llo")
                self.assertEqual(stream.read(8), b"")
            request = opened.call_args_list[1].args[0]
            self.assertEqual(request.get_header("Range"), "bytes=2-")
            self.assertEqual(request.get_header("If-range"), '"v1"')
            self.assertIsNone(request.get_header("Authorization"))

    def test_ignored_or_wrong_range_never_appends_full_file(self):
        for status, content_range in ((200, ""), (206, "bytes 0-4/5"), (206, "bytes 2-4/6")):
            with self.subTest(status=status, content_range=content_range):
                client = self.client()
                responses = [Response(b"he"), Response(b"hello", status, {"Content-Range": content_range})]
                with patch.object(client, "_open", side_effect=responses), patch("onedrive_backup.graph.time.sleep"):
                    with client.download({"size": 5, "@microsoft.graph.downloadUrl": "https://files.test/data"}) as stream:
                        self.assertEqual(stream.read(8), b"he")
                        with self.assertRaises(GraphError):
                            stream.read(8)
