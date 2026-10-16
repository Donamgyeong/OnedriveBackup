# Synology NAS 배포 안내

이 프로젝트는 Python 3.8 문법과 표준 라이브러리 범위에서 실행되도록 구성되어 있다. 런타임용 외부 Python 패키지는 없다. NAS 모델과 DSM 버전에 따라 Python 및 SQLite 빌드가 다르므로 실제 장비에서 아래 사전 검사를 수행해야 한다.

## 1. 런타임 확인

SSH 또는 DSM 작업 스케줄러와 같은 계정으로 확인한다.

```sh
python3.8 --version
python3.8 -c 'import sqlite3; print(sqlite3.sqlite_version)'
```

Python은 3.8 이상이어야 하며, SQLite는 UPSERT 구문을 지원하는 3.24.0 이상이어야 한다. 프로그램도 DB를 열기 전에 SQLite 버전을 검사하고 조건을 만족하지 않으면 명확한 오류로 중단한다.

Python 3.8은 2024년 10월에 공식 보안 지원이 종료되었다. Synology에서 3.8만 사용할 수 있다면 전용 서비스 계정으로 실행하고 NAS 관리 화면과 SSH를 외부 인터넷에 직접 노출하지 않으며 DSM 및 Python 패키지를 NAS 공급 범위에서 최신 상태로 유지한다.

## 2. 파일 배치

아래 경로는 예시다. 실제 공유 폴더와 중앙화 솔루션 마운트 경로로 바꾼다.

```text
/volume1/onedrive-backup/          # 프로그램과 config.json
/volume1/onedrive-backup/state/    # 로컬 SQLite
/volume1/document-central/         # 중앙화 솔루션 마운트
```

`build-synology.ps1`로 만든 `dist/OneDriveBackup-0.10.0-synology-source.zip` 또는 프로젝트 파일을 `/volume1/onedrive-backup`에 복사하고 설정을 만든다. ZIP에는 실제 `config.json`, DB와 백업 데이터가 포함되지 않는다.

SharePoint 전체 사이트와 문서 라이브러리도 자동 검색한다. Entra 앱에 기존 권한 외 `Sites.Read.All` application 권한을 추가하고 관리자 동의를 부여해야 한다. 새 설정 항목은 없으며 SQLite 테이블은 자동 생성된다. 파일은 `browse/SharePoint/<사이트명__ID>/<라이브러리명__ID>/current` 아래에 게시된다. 사이트 설정이나 일반 목록 데이터는 백업 범위에 포함되지 않는다.

```sh
cd /volume1/onedrive-backup
cp config.example.json config.json
chmod 600 config.json
chmod +x synology/onedrive-backup.sh
```

`config.json`의 미정 값을 채운다. Synology에서는 다음과 같은 절대 경로를 사용한다.

```json
{
  "sqlite_path": "/volume1/onedrive-backup/state/backup.sqlite",
  "backup_root": "/volume1/document-central"
}
```

위 JSON은 경로 예시만 보여 준다. 실제 `config.json`에는 `config.example.json`의 모든 키가 있어야 한다. `client_secret`이 평문으로 저장되므로 파일을 전용 서비스 계정과 관리자만 읽도록 제한한다. SQLite 파일은 네트워크 마운트가 아닌 NAS의 로컬 볼륨에 둔다.

중앙화 마운트를 확인한 뒤 `mount_marker_name`으로 지정한 파일을 `backup_root` 바로 아래에 만들고 그 내용이 `mount_marker_value`와 정확히 일치하도록 한다. 프로그램은 식별 파일을 자동 생성하지 않는다.

## 3. 실행 전 확인

```sh
cd /volume1/onedrive-backup
PYTHON_BIN=/path/to/python3.8 ./synology/onedrive-backup.sh validate
PYTHON_BIN=/path/to/python3.8 ./synology/onedrive-backup.sh status
PYTHON_BIN=/path/to/python3.8 ./synology/onedrive-backup.sh run
```

Python 명령이 PATH에 `python3.8`로 등록되어 있으면 `PYTHON_BIN`은 생략할 수 있다. 처음에는 `run`을 수동 실행해 Graph 권한, 마운트 식별 파일, 쓰기·읽기 권한과 여유 공간을 확인한다.

`download_concurrency`는 기본 4다. 같은 동시성 묶음의 다운로드 전후 메타데이터는 Graph JSON batch로 조회되고 SQLite 결과도 한 트랜잭션으로 확정되므로 작은 파일이 많을 때 요청 왕복과 로컬 DB 동기화 횟수가 줄어든다. 파일 내용은 개별 스트림으로 내려받는다. NAS CPU, 메모리와 중앙화 마운트 처리량이 낮으면 2로 낮추고, 네트워크 대기 비중이 크면 Graph 429 응답과 NAS 부하를 확인하면서 8까지 시험한다. `download_chunk_bytes`의 기본값은 8 MiB다.

`repair_check_batch_size`는 자동 실행마다 존재와 크기를 검사할 기존 백업 파일 수이며 기본값은 10000이다. 파일이 50만 개라면 기본 설정에서 약 50회 실행에 걸쳐 전체를 순환 검사한다. 마운트의 파일 조회가 느리면 값을 낮추고, 유실 감지 주기를 줄이려면 NAS 부하를 확인하면서 높인다. 검사 cursor는 SQLite에 250개 단위로 저장되므로 중단 후 처음부터 다시 검사하지 않는다. 수동 `verify`는 배치 제한 없이 전체 파일을 읽고 해시를 계산한다.

일반 증분 실행에서는 Graph delta에 포함된 항목만 기존 DB 상태와 비교하며, `browse`도 변경 큐의 파일만 갱신한다. 상위 폴더 이동·이름 변경·삭제가 발생한 회차에는 하위 파일 경로를 다시 계산하기 위해 해당 드라이브의 계층 메타데이터를 한 번 읽는다. 최초 50만 개 수집과 최초 `browse` 복사는 전체 파일 처리가 필요하므로 충분한 실행 시간을 확보해야 한다.

실행 중에는 보존 정책 검사, 사용자·OneDrive 검색, 드라이브 변경 조회, 누락 백업 검사, 파일 다운로드, 탐색기 보기 게시, DB 카탈로그 저장 등 현재 작업 단계가 콘솔에 계속 기록된다. 총량을 알 수 있는 작업은 처리 수와 백분율을 함께 출력하고, 다운로드는 바이트 수·성공·실패 수·파일별 소요 시간과 MiB/s도 표시한다. Graph가 429 또는 일시적인 서버·네트워크 오류를 반환하면 재시도 횟수와 대기 시간도 기록한다. Microsoft Graph 사용자 검색처럼 서버가 총량을 주지 않는 구간은 `percent=unknown`으로 표시한다.

각 `stage`의 작업 내용, 완료 기준, 중단 시 재개 방법과 주요 오류 로그의 의미는 [README의 실행 단계와 로그](README.md#실행-단계와-로그)를 참고한다.

Python 로깅은 기본적으로 표준 오류(stderr)에 출력되고 명령 결과 JSON은 표준 출력(stdout)에 출력된다. DSM 작업 스케줄러에서는 아래 예시처럼 `2>&1`을 사용하면 둘 다 같은 로그 파일에 저장된다.

중앙화 마운트는 다음 파일 시스템 동작을 지원해야 한다.

- 같은 디렉터리 안에서 원자적인 파일 교체
- 파일과 디렉터리 `fsync`
- 파일 삭제와 이름 변경
- 여유 공간 조회
- POSIX `flock` 기반 단일 프로세스 잠금

마운트 구현에 따라 이 동작들이 다르므로 실제 파일을 사용한 사전 시험이 필요하다. 운영 서비스 계정에는 프로그램 경로와 SQLite 상태 경로, 중앙화 마운트에 필요한 최소 권한만 부여한다.

## 4. DSM 작업 스케줄러

DSM **제어판 → 작업 스케줄러 → 생성 → 예약된 작업 → 사용자 정의 스크립트**에서 전용 서비스 계정을 선택하고 실행 주기를 설정한다. 사용자 정의 스크립트 예시는 다음과 같다.

```sh
cd /volume1/onedrive-backup && PYTHON_BIN=/path/to/python3.8 ./synology/onedrive-backup.sh run >> /volume1/onedrive-backup/backup.log 2>&1
```

한 회차가 끝난 뒤 DSM이 다음 회차를 시작하는 방식이다. 프로그램은 잠금 파일을 사용해 같은 SQLite와 백업 루트에서 중복 실행되는 것을 차단한다. 로그 회전과 알림은 DSM 정책에 맞게 별도로 설정한다.

업데이트 전에는 작업을 비활성화하고 실행 중인 프로세스가 없는지 확인한다. `config.json`, `state/`, 백업 루트의 `objects`, `manifests`, `catalogs`, `browse`는 프로그램 소스 업데이트와 별도로 보존한다.
