import argparse
import json
import logging
import shutil
import sqlite3
import sys
import time
from pathlib import Path

from .compat import is_path_within
from .config import Config, ConfigError
from .db import Database
from .engine import Engine
from .graph import GraphClient
from .storage import ProcessLock, Storage, StorageError, digest, storage_error_detail
from .retention import Retention
from .browse import BrowsePublisher


def parser():
    result = argparse.ArgumentParser(description="OneDrive → 중앙화 마운트 증분 백업")
    result.add_argument("--config", default="config.json")
    result.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate", help="설정 검증; API와 저장소 쓰기 없음")
    commands.add_parser("run", help="한 번 실행")
    commands.add_parser("serve", help="설정 간격으로 반복 실행")
    commands.add_parser("status", help="최근 실행과 작업 상태")
    commands.add_parser("retry-failed", help="실패 작업을 재시도 대기로 전환")
    commands.add_parser("verify", help="현재 백업 파일의 해시 검사 및 복구 작업 등록")
    commands.add_parser("browse", help="기존 백업으로 탐색기용 최신 파일 보기 생성·갱신")
    prune = commands.add_parser("prune", help="보존 기간이 지난 백업 폐기")
    prune.add_argument("--dry-run", action="store_true", help="대상만 조회하고 삭제하지 않음")
    listing = commands.add_parser("list", help="백업 버전 목록")
    listing.add_argument("--drive-id")
    listing.add_argument("--item-id")
    restore = commands.add_parser("restore", help="지정 백업 버전을 새 파일로 복원")
    restore.add_argument("--object-id", required=True)
    restore.add_argument("--output", required=True, help="복원할 전체 파일 경로; 기존 파일 덮어쓰기 금지")
    return result


def execute(args, cfg, db, storage):
    graph = GraphClient(cfg)
    try:
        return _execute(args, cfg, db, storage, Engine(cfg, db, graph, storage))
    finally:
        graph.close()


def _execute(args, cfg, db, storage, engine):
    if args.command in ("run", "serve"):
        cfg.secret()
        while True:
            report = engine.run()
            print(json.dumps(report, ensure_ascii=False), flush=True)
            if args.command == "run":
                return 0 if report["status"] == "success" else 1
            time.sleep(cfg.backup_interval_seconds)
    elif args.command == "status":
        print(json.dumps({"runs": [dict(r) for r in db.all("SELECT * FROM runs ORDER BY run_id DESC LIMIT 10")],
                          "jobs": [dict(r) for r in db.all("SELECT state,COUNT(*) AS count FROM jobs GROUP BY state")],
                          "accounts": [dict(r) for r in db.all("SELECT status,COUNT(*) AS count FROM accounts GROUP BY status")],
                          "sites": [dict(r) for r in db.all("SELECT site_id,status,seen FROM sites")],
                          "site_drives": [dict(r) for r in db.all("SELECT drive_id,site_id FROM site_drives")],
                          "retention": [dict(r) for r in db.all("SELECT state,COUNT(*) AS count FROM purge_jobs GROUP BY state")],
                          "expired_items": db.one("SELECT COUNT(*) AS count FROM expired_items")["count"],
                          "browse_errors": [dict(r) for r in db.all("SELECT drive_id,item_id,error FROM browse_files WHERE error IS NOT NULL")],
                          "browse_cleanup_pending": db.one("SELECT COUNT(*) AS count FROM browse_cleanup")["count"],
                          "drives": [dict(r) for r in db.all("SELECT drive_id,status,last_scan,last_backup FROM drives")]}, ensure_ascii=False, indent=2))
    elif args.command == "retry-failed":
        with db.transaction() as conn:
            count = conn.execute("""UPDATE jobs SET state='pending',attempts=0,retry_at=0,error=NULL
                WHERE state='failed' AND EXISTS (SELECT 1 FROM items i WHERE i.drive_id=jobs.drive_id
                AND i.item_id=jobs.item_id AND i.deleted=0)""").rowcount
        print(json.dumps({"queued": count}))
    elif args.command == "verify":
        print(json.dumps({"repair_jobs": engine.repair_missing(verify_hash=True, full_scan=True)}))
    elif args.command == "browse":
        report = BrowsePublisher(cfg, db, storage).sync()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report["failed"] else 0
    elif args.command == "prune":
        report = Retention(cfg, db, storage).prune(dry_run=args.dry_run)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report.get("failed") else 0
    elif args.command == "list":
        clauses, values = ["NOT EXISTS (SELECT 1 FROM purge_jobs p WHERE p.resource_id=objects.object_id AND p.state='pending')"], []
        for key in ("drive_id", "item_id"):
            if getattr(args, key):
                clauses.append(key + "=?")
                values.append(getattr(args, key))
        sql = "SELECT * FROM objects" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY created"
        for row in db.conn.execute(sql, values):
            item = json.loads(row["payload"])
            print(json.dumps({"object_id": row["object_id"], "drive_id": row["drive_id"], "item_id": row["item_id"],
                              "name": item.get("name"), "original_parts": engine.original_parts(row["drive_id"], row["item_id"]),
                              "size": row["size"], "created": row["created"]}, ensure_ascii=False))
    elif args.command == "restore":
        storage.check()
        row = db.one("""SELECT * FROM objects WHERE object_id=? AND NOT EXISTS
            (SELECT 1 FROM purge_jobs p WHERE p.resource_id=objects.object_id AND p.state='pending')""", (args.object_id,))
        if not row:
            raise ValueError("해당 object_id가 없습니다")
        source = storage.path(row["relative_path"])
        if digest(source) != row["sha256"]:
            raise StorageError("backup_checksum_mismatch")
        destination = Path(args.output).resolve()
        if is_path_within(destination, storage.root):
            raise ValueError("복원 출력은 백업 루트 밖으로 지정하세요")
        if destination == Path(cfg.sqlite_path).resolve():
            raise ValueError("DB 경로로 복원할 수 없습니다")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as src, destination.open("xb") as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
        if digest(destination) != row["sha256"]:
            raise StorageError("restore_checksum_mismatch")
        print(json.dumps({"restored": str(destination)}, ensure_ascii=False))
    return 0


def main(argv=None):
    args = parser().parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = Config.load(args.config)
        if args.command == "validate":
            print("설정 형식 검증 완료. 인증·API 권한·마운트 동작은 검증하지 않았습니다.")
            return 0
        with ProcessLock(cfg.sqlite_path + ".lock"):
            db = Database(cfg.sqlite_path, cfg.tenant_id, cfg.backup_root)
            try:
                return execute(args, cfg, db, Storage(cfg))
            finally:
                db.close()
    except KeyboardInterrupt:
        print("중지되었습니다. 다음 실행에서 미완료 작업을 재개합니다.", file=sys.stderr)
        return 130
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, ValueError, StorageError, sqlite3.Error) as exc:
        # Suppress URLs and arbitrary exception payloads in operational output.
        detail = storage_error_detail(exc) if isinstance(exc, StorageError) else type(exc).__name__
        print(f"실행할 수 없습니다: {detail}", file=sys.stderr)
        return 1
