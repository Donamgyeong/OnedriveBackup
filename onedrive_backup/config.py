import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    tenant_id: str
    client_id: str
    client_secret_env: str
    authority_url: str
    graph_url: str
    sqlite_path: str
    backup_root: str
    mount_marker_name: str
    mount_marker_value: str
    backup_interval_seconds: int
    minimum_free_bytes: int
    max_attempts: int
    retry_base_seconds: int
    http_timeout_seconds: int
    max_http_attempts: int
    retention_policy: str
    special_item_policy: str
    guest_account_policy: str
    retention_days: int | None = None

    @classmethod
    def load(cls, path):
        raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        missing = [key for key in cls.__annotations__ if key != "retention_days" and raw.get(key) in (None, "")]
        if missing:
            raise ConfigError("미설정 필수 항목: " + ", ".join(missing))
        unknown = set(raw) - set(cls.__annotations__)
        if unknown:
            raise ConfigError("알 수 없는 설정: " + ", ".join(sorted(unknown)))
        for key, typ in cls.__annotations__.items():
            if key == "retention_days":
                continue
            if type(raw[key]) is not typ:
                raise ConfigError(f"{key}: {typ.__name__} 형식이 필요합니다")
            if typ is str and not raw[key].strip():
                raise ConfigError(f"{key}: 빈 값입니다")
            if typ is int and raw[key] < (0 if key == "minimum_free_bytes" else 1):
                raise ConfigError(f"{key}: 범위를 벗어난 값입니다")
        for key in ("authority_url", "graph_url"):
            u = urlsplit(raw[key])
            if u.scheme != "https" or not u.hostname or u.username or u.password or u.query or u.fragment:
                raise ConfigError(f"{key}: HTTPS 기본 URL이 필요합니다")
            raw[key] = raw[key].rstrip("/")
        for key in ("sqlite_path", "backup_root"):
            if not Path(raw[key]).is_absolute():
                raise ConfigError(f"{key}: 절대 경로가 필요합니다")
        sqlite_path = Path(raw["sqlite_path"])
        if sqlite_path.exists() and sqlite_path.is_dir():
            raise ConfigError("sqlite_path: 폴더가 아니라 SQLite 파일 경로가 필요합니다 (예: C:\\backup-state\\backup.sqlite)")
        root = Path(raw["backup_root"]).resolve()
        if sqlite_path.resolve().is_relative_to(root):
            raise ConfigError("SQLite는 백업 마운트 외부의 실제 로컬 디스크에 두세요")
        marker = raw["mount_marker_name"]
        if marker in (".", "..") or any(c in marker for c in '/\\:'):
            raise ConfigError("mount_marker_name은 파일명이어야 합니다")
        if raw["retention_policy"] not in ("keep_all", "expire_after_days"):
            raise ConfigError("retention_policy: keep_all 또는 expire_after_days")
        days = raw.get("retention_days")
        if days is not None and (type(days) is not int or days <= 0):
            raise ConfigError("retention_days: 양의 정수 또는 null")
        if raw["retention_policy"] == "expire_after_days" and days is None:
            raise ConfigError("expire_after_days에는 retention_days가 필요합니다")
        if raw["special_item_policy"] not in ("report", "exclude"):
            raise ConfigError("special_item_policy: report 또는 exclude")
        if raw["guest_account_policy"] not in ("include", "exclude"):
            raise ConfigError("guest_account_policy: include 또는 exclude")
        return cls(**raw)

    def secret(self):
        value = os.environ.get(self.client_secret_env)
        if not value:
            raise ConfigError("지정한 client_secret_env 환경 변수에 인증 비밀값이 없습니다")
        return value
