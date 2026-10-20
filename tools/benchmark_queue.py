"""Synthetic queue benchmark. No Graph connection or backup file writes.

Run: python -m tools.benchmark_queue --files 600000 --directory <local-temp-dir>
Peak memory is Python allocations during queue processing, not total process RSS.
"""
import argparse
import json
import tempfile
import time
import tracemalloc
from pathlib import Path

from onedrive_backup.db import Database, encode
from onedrive_backup.engine import Engine
from onedrive_backup.scheduling import DownloadQueue


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=600000)
    parser.add_argument("--directory", required=True)
    args = parser.parse_args()
    if args.files < 1:
        parser.error("--files must be positive")
    Path(args.directory).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=args.directory) as folder:
        db = Database(str(Path(folder) / "benchmark.sqlite"), "synthetic", Path(folder) / "backup")
        try:
            payload = encode({"file": {}, "size": 10 * 1024 * 1024})
            started = time.perf_counter()
            with db.transaction() as conn:
                conn.execute("INSERT INTO drives(drive_id,user_id,status) VALUES ('d','u','active')")
                conn.executemany("INSERT INTO items(drive_id,item_id,payload) VALUES ('d',?,?)",
                                 ((str(index), payload) for index in range(args.files)))
                conn.executemany("INSERT INTO jobs(drive_id,item_id) VALUES ('d',?)",
                                 ((str(index),) for index in range(args.files)))
            seed_seconds = time.perf_counter() - started
            engine = Engine(None, db, None, None)
            selection = (time.time(), db.one("SELECT MAX(job_id) FROM jobs")[0])
            tracemalloc.start()
            started = time.perf_counter()
            total_files, total_bytes = engine._download_totals(selection)
            totals_seconds = time.perf_counter() - started
            started = time.perf_counter()
            queue = DownloadQueue(db)
            queued = 0
            while queue.pop([]) is not None:
                queued += 1
            queue_seconds = time.perf_counter() - started
            _, peak_bytes = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            assert queued == total_files == args.files
            assert total_bytes == args.files * 10 * 1024 * 1024
            print(json.dumps({"files": total_files, "logical_bytes": total_bytes,
                              "seed_seconds": round(seed_seconds, 3),
                              "totals_seconds": round(totals_seconds, 3),
                              "queue_seconds": round(queue_seconds, 3),
                              "peak_python_mib": round(peak_bytes / (1024 * 1024), 3)}, indent=2))
        finally:
            db.close()


if __name__ == "__main__":
    main()
