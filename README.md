# OneDrive 중앙화 증분 백업

Python 3.8 이상과 표준 라이브러리만 사용하는 초기 구현이다. SQLite에 변경 조회 커서와 영속 작업 큐를 기록하고, 회사 사용자별 OneDrive 파일을 중앙화 솔루션의 마운트 경로에 저장한다. 상세 배경은 [개념 설계](CONCEPT_DESIGN.md)를 참고한다.

사용자 PC에 Python을 설치하지 않는 Windows 배포도 지원한다. 개발 PC에서 `build.ps1`을 실행하면 버전이 포함된 Windows 배포 ZIP이 생성된다. 배포 및 설치 절차는 [Python 없는 Windows 배포 안내](DISTRIBUTION.md)를 참고한다. PyInstaller는 빌드할 때만 사용하며 배포 프로그램의 런타임 의존성은 아니다.

Synology NAS에서는 Python 3.8과 SQLite 3.24.0 이상을 확인한 뒤 소스로 실행한다. DSM 작업 스케줄러 등록과 마운트 점검 절차는 [Synology NAS 배포 안내](SYNOLOGY.md)를 참고한다. Python 3.8은 보안 지원이 종료된 버전이므로 NAS의 방화벽과 패키지 업데이트 정책으로 실행 환경을 제한해야 한다.

## 실행 준비

```powershell
Copy-Item config.example.json config.json
python -m onedrive_backup --config config.json validate
```

예제의 모든 운영 설정은 `null`이다. 값을 입력하기 전 검증은 실패하며 백업을 실행하지 않는다. 제공되지 않은 계정 정보, 경로, 주기, 인증 비밀값은 코드에 채워 넣지 않았다. Python 설치 외 필수 패키지 설치는 없다.

| 설정 | 입력할 내용 |
|---|---|
| tenant_id / client_id | Entra 테넌트 ID / 등록 앱 ID |
| client_secret | Entra 앱의 클라이언트 비밀값 |
| authority_url | 해당 클라우드의 인증 기본 URL; 공용 클라우드 예: `https://login.microsoftonline.com` |
| graph_url | Graph v1.0 기본 URL; 공용 클라우드 예: `https://graph.microsoft.com/v1.0` |
| sqlite_path | 실제 로컬 디스크의 SQLite 절대 **파일** 경로. 예: Windows `C:\\backup-state\\backup.sqlite`, Synology `/volume1/onedrive-backup/state/backup.sqlite` (폴더 경로는 사용할 수 없음) |
| backup_root | 미리 마운트되어 있는 대상의 절대 경로 |
| mount_marker_name / mount_marker_value | 대상 루트에 미리 준비한 UTF-8 식별 파일의 이름 / 내용 |
| backup_interval_seconds | 반복 실행 완료 후 다음 실행까지 대기할 초 |
| minimum_free_bytes | 다운로드 후 남겨 둘 최소 공간 바이트 |
| max_attempts | 파일 작업 최대 시도 횟수 |
| retry_base_seconds | 지수 백오프의 기본 초 |
| http_timeout_seconds / max_http_attempts | HTTP 타임아웃 초 / 요청 최대 시도 횟수 |
| download_concurrency | 적응형 다운로드의 시작 동시성. 기본 4, 허용 범위 1~16 |
| download_concurrency_max | 자동 증가 상한. 기본 8(기존 시작값이 8보다 크면 그 값), 시작 동시성 이상 16 이하. 시작값과 같게 하면 자동 증가 없음 |
| download_chunk_bytes | 다운로드·복사 버퍼 크기. 기본 8388608(8 MiB) |
| repair_check_batch_size | 자동 실행 한 회차에서 존재·크기를 검사할 기존 백업 파일 수. 기본 10000, 허용 범위 1~100000 |
| retention_policy | `keep_all`: 계속 보존 / `expire_after_days`: 최신 1개를 남기고 기간이 지난 이전 버전 폐기 |
| retention_days | 보존 일수(양의 정수). `expire_after_days` 선택 시 필수, `keep_all`에서는 null 가능 |
| special_item_policy | `report`: 특수 항목을 실패로 보고 / `exclude`: 명시적으로 제외 |
| guest_account_policy | `include` 또는 `exclude` |

앱 인증은 client credentials 방식이며 `client_secret`을 `config.json`에서 직접 읽는다. 앱에는 `User.Read.All`, `Files.Read.All`, `Sites.Read.All` application 권한과 관리자 동의가 필요하다. `Sites.Read.All`은 전체 SharePoint 사이트 검색에 필요하다. 인증서·관리 ID 인증은 아직 구현하지 않았다. `config.json`은 Git에서 제외되지만 평문 비밀값을 포함하므로 운영 서비스 계정과 관리자만 읽을 수 있도록 운영체제 파일 권한을 제한해야 한다. 로그와 SQLite에는 비밀값을 기록하지 않는다.

계정 드라이브 조회 로그는 응답 본문이나 URL을 남기지 않고 HTTP 상태와 안전한 Graph 오류 코드만 표시한다. OneDrive가 생성되지 않은 계정의 404 또는 알려진 MySite 미프로비저닝 응답은 `no_drive`로 기록하고 전체 실행 실패 수에는 포함하지 않는다. `status=403 code=accessDenied`가 표시되면 앱의 `Files.Read.All` application 권한과 관리자 동의를 확인한다.

식별 파일은 운영자가 실제 중앙화 마운트를 확인한 뒤 별도로 생성한다. 프로그램은 이 파일을 생성하지 않으며, 내용이 일치하지 않으면 쓰기를 차단한다. 서비스 계정에서 마운트를 볼 수 있어야 한다. `validate`는 설정 형식만 확인하고 인증·권한·마운트는 검사하지 않는다.

## 명령

```powershell
# 한 번 실행 / 반복 실행 (Ctrl+C로 종료)
python -m onedrive_backup --config config.json run
python -m onedrive_backup --config config.json serve

# 실행 및 작업 현황
python -m onedrive_backup --config config.json status
python -m onedrive_backup --config config.json retry-failed

# 현재 파일 해시 점검 및 복구 작업 등록; 실제 다운로드는 다음 run에서 수행
python -m onedrive_backup --config config.json verify

# 기존 백업을 탐색기에서 열 수 있도록 최신 파일 보기 생성/갱신
# OneDrive 재다운로드나 인증 없이 저장된 백업에서 복사한다.
python -m onedrive_backup --config config.json browse

# 기간 만료 대상 확인 / 폐기 실행 (Graph 인증 없이 실행 가능)
python -m onedrive_backup --config config.json prune --dry-run
python -m onedrive_backup --config config.json prune

# 저장 버전 검색
python -m onedrive_backup --config config.json list
python -m onedrive_backup --config config.json list --drive-id <DRIVE_ID> --item-id <ITEM_ID>

# 선택 버전을 백업 루트 외부의 새 파일로 복원
python -m onedrive_backup --config config.json restore --object-id <OBJECT_ID> --output <ABSOLUTE_FILE_PATH>
```

종료 코드는 0=정상, 1=실행 실패 또는 부분 실패, 2=설정/인자 오류, 130=사용자 중단이다. 반복 모드는 고정 시각 스케줄이 아니라 실행 완료 기준 간격이다. Synology에서는 DSM 작업 스케줄러로 `run`을 주기 실행할 수 있다. Windows 서비스 등록, 알림 전송과 로그 회전은 운영 환경이 정해진 뒤 연결한다.

## 실행 단계와 로그

`run`과 `serve`의 한 회차는 아래 순서로 실행된다. `작업 단계 시작`과 `작업 단계 완료` 사이에 해당 단계의 세부 진행 상황이 출력된다.

| 순서 | stage 또는 로그 | 수행 작업 | 진행률과 완료 기준 | 중단 후 처리 |
|---:|---|---|---|---|
| 1 | `retention` | 마운트 식별을 확인하고, 만료된 이전 버전 또는 미완료 폐기 작업이 있는 파일만 대상으로 최소 1개 정상 버전을 검사한다. 정책에 따라 이전 객체·manifest·catalog를 폐기한다. | `보존본 검사 진행 files=현재/전체`의 전체 수는 실제 폐기 가능성이 있는 파일 수다. `보존 기간 폐기 진행 items=현재/전체`와 완료 로그의 `completed`, `failed`를 표시한다. | 폐기 대상과 상태를 `purge_jobs`에 기록하므로 다음 실행에서 미완료 항목을 재개한다. |
| 2 | `user_discovery` | Microsoft 365 사용자 목록을 조회하고 각 사용자의 business OneDrive 드라이브를 확인한다. 게스트 정책과 OneDrive 미생성 계정을 반영한다. | Graph가 전체 사용자 수를 제공하지 않으므로 `accounts`, `drives`, `errors`, `current_user`와 `percent=unknown`을 표시한다. 모든 사용자 페이지 처리가 완료되면 끝난다. | 다음 실행에서 사용자 검색을 다시 수행한다. 이전에 정상 발견된 드라이브와 백업 데이터는 삭제하지 않는다. |
| 2-1 | `site_discovery` | `/sites/getAllSites`의 모든 페이지와 각 사이트의 하위 사이트를 조회하고, `/sites/{id}/drives`로 모든 문서 라이브러리를 등록한다. | `sites`, `libraries`, `errors`, `percent=unknown`을 표시한다. 사이트 및 라이브러리 접근 실패는 부분 실패다. | 검색은 다음 실행에서 다시 한다. 기존 드라이브의 delta cursor와 백업 내용은 유지한다. |
| 3 | `드라이브 동기화` | 활성 드라이브마다 저장된 delta link로 변경 항목을 가져온다. 최초 실행이나 만료된 cursor 복구 시에는 전체 스캔한다. | 드라이브별 `mode=full` 또는 `mode=incremental`을 표시한다. Graph 페이지를 받는 동안 `pages`, `items`, `percent=unknown`을 출력한다. 마지막 페이지를 받아야 전체 수가 확정된다. | 각 페이지의 `next_link`와 항목을 SQLite에 저장하므로 다음 실행에서 마지막으로 확정된 페이지부터 재개한다. |
| 3-1 | `드라이브 변경 확정` | 한 드라이브에서 받은 누적 항목을 기존 메타데이터와 비교하고, 다운로드 작업과 삭제 상태를 만든 뒤 새 delta link를 확정한다. | `items=현재/전체 percent=...`를 약 1초 간격으로 표시한다. `드라이브 변경 확정 완료` 뒤에 해당 드라이브의 조회 완료 로그가 나온다. | 작업 큐와 delta link를 한 SQLite 트랜잭션으로 저장한다. 중간에 종료되면 트랜잭션이 롤백되고 저장된 페이지에서 다시 처리한다. |
| 4 | `repair_check` | DB가 가리키는 기존 백업 객체를 `repair_check_batch_size`만큼 순환 검사한다. 이미 다운로드 큐에 있는 초기 파일은 제외하고, 별도 작업이 없는 누락·불완전 백업만 큐에 넣는다. | `백업 파일 검사 범위 mode=incremental batch_limit=... candidates=...` 다음에 현재 배치의 `files=현재/전체 repaired=... percent=...`를 표시한다. 이 백분율은 전체 저장소가 아닌 이번 배치 기준이다. | 객체 ID cursor와 복구 작업을 250개 단위로 함께 저장한다. 중단하면 마지막 checkpoint 다음부터 이어지며 최대 249개를 다시 검사할 수 있다. |
| 5 | `file_download` | 작업을 최대 256건씩 읽고 `download_concurrency` 범위에서 병렬 다운로드한다. 같은 묶음의 전후 메타데이터는 Graph batch로 조회하고 성공 결과는 한 트랜잭션으로 확정한다. | 약 1초 간격으로 `files`, `bytes`, `success`, `failed`, `percent`, 소수점 셋째 자리의 `file_percent`, `byte_percent`를 표시한다. 전송·읽기 검증 대기 중에는 5초마다 상태를 표시한다. 파일별 소요 시간과 MiB/s는 DEBUG 로그다. | HTTP 본문 timeout·연결 끊김·짧은 응답은 해당 파일만 재시도 큐에 남기고 계속한다. 디스크 쓰기·읽기 검증·마운트 장애는 실행을 중단한다. |
| 6 | `browse_publish` | 영속 `browse_jobs`에서 최대 256건씩 읽어 변경 파일을 게시한다. 파일·상위 폴더 이름 변경과 삭제 위치도 반영한다. | `files=현재/전체 percent=...`는 현재 최대 256건 배치 기준이며 `탐색기 게시 체크포인트 processed=...`는 누적 처리 수다. 변경이 없으면 전체 `browse` 트리를 순회하지 않는다. | 성공 항목은 큐에서 제거한다. 실패 항목은 이번 회차에서 반복하지 않고 다음 실행에 재개한다. 사용자가 수정한 파일은 덮어쓰지 않는다. |
| 7 | `catalog_snapshot` | 현재 SQLite 상태를 backup API로 복제해 `catalogs`에 저장하고 manifest와 해시를 만든다. | 별도 백분율은 없으며 완료 로그의 `path`가 생성된 catalog 경로다. | 실패하면 실행 상태를 `partial`로 기록한다. 다음 회차에서 새 snapshot 생성을 다시 시도한다. |

드라이브 동기화는 별도 `stage=` 문자열 대신 `드라이브 동기화 시작/진행/완료`로 표시되며, 그 안에서 드라이브 변경 조회와 확정이 반복된다. 여러 드라이브가 있으면 `drives=현재/전체 percent=...`가 전체 드라이브 진행률이다.

다음 메시지는 어느 단계에서 멈추거나 느려졌는지 판단할 때 사용한다.

| 로그 | 의미 |
|---|---|
| `Graph 요청 재시도 status=429 ... wait_seconds=N` | Graph 요청 제한이다. `Retry-After`에 따라 N초 기다린 뒤 재시도한다. |
| `Graph 요청 재시도 status=503 ...` | Microsoft 서비스가 일시적으로 요청을 처리하지 못해 대기 중이다. |
| `percent=unknown` | 멈춘 상태가 아니라 서버가 최종 전체 개수를 아직 알려주지 않은 페이지 조회 단계다. |
| `계정 OneDrive 없음` | 해당 계정에 OneDrive가 생성되지 않은 정상 제외 상태다. |
| `파일 처리 실패` | 해당 파일을 영구 폐기한 것이 아니라 재시도 큐에 기록했거나 최대 시도 횟수에 도달한 상태다. |
| `백업 실행 완료 ... status=success` | 오류와 남은 작업 없이 한 회차가 완료됐다. |
| `status=partial` | 일부 계정·드라이브·파일·게시·catalog 작업이 실패했거나 작업 큐가 남았다. |
| `status=failed` | 저장소 접근 실패처럼 회차 전체를 중단해야 하는 오류가 발생했다. |

로그는 Python 로깅의 기본 출력인 stderr에 기록되고 마지막 실행 결과 JSON은 stdout에 기록된다. 두 출력을 한 파일에 보관하려면 `2>&1`을 사용한다.

자동 `repair_check`는 50만 개 파일을 매 실행마다 모두 조회하지 않는다. 기본 배치 10000이면 변경이 없을 때 약 50회의 `run`에 걸쳐 전체 현재 백업을 한 바퀴 검사한다. 한 시간 주기라면 약 50시간이다. 값이 크면 유실 감지 주기가 짧아지는 대신 NAS와 마운트의 파일 조회 부하가 증가한다. `verify` 명령은 이 배치 제한과 cursor를 사용하지 않고 현재 백업 전체의 SHA-256을 검사하므로 운영 부하가 낮은 시간에 실행해야 한다.

## 저장과 증분 처리

- 모든 사용자 페이지와 사용자 드라이브 페이지를 조회한다. business 유형의 사용자 드라이브를 대상으로 한다. 비활성 사용자도 게스트 정책 외 별도 필터 없이 탐색한다. 삭제된 사용자 복구 API는 미구현이다.
- SharePoint는 전체 지역의 사이트를 `/sites/getAllSites`로 검색하고 하위 사이트도 순회한다. 각 사이트의 모든 `documentLibrary` 드라이브를 등록하여 동일한 delta·다운로드·무결성 검사·보존 정책을 적용한다. Teams 채널의 파일도 검색된 SharePoint 문서 라이브러리에 저장되어 있으면 포함한다. 사이트와 드라이브 ID로 중복을 제거하며, 권한 오류를 조용히 제외하지 않고 부분 실패로 보고한다.
- 초기 delta 전체 열거 후 변경분만 조회한다. 내용 태그가 같으면 이름·폴더 변경만 DB에 반영한다. 변경된 파일은 전체 내용을 내려받는다.
- delta 페이지를 DB에 보관하고 끝까지 받은 뒤 마지막 항목 상태로 정규화한다. 작업 큐와 새 delta 커서를 하나의 트랜잭션으로 확정한다.
- 다운로드는 크기별 혼합 큐(작은/중간/큰 파일 가중치 2:2:4)를 사용한다. 30초 성능 구간에서 동시성을 늘린 뒤 수신 속도가 5% 이상 증가하고 검증 완료 속도가 유지되는지 확인한다. 효과가 없으면 이전 동시성으로 복귀한다. 별도 메타데이터·다운로드·검증 작업자를 사용하며 SQLite는 메인 스레드만 접근한다. 검증 대기량이 크면 새 다운로드 투입을 멈춘다. 429·503은 전체 클라이언트가 Retry-After 동안 새 요청을 대기하고 동시성을 절반으로 줄인다. IETF RateLimit 헤더에는 의존하지 않는다. 모든 검증을 통과한 파일만 DB에 확정한다.
- 직접 HTTPS 연결은 검증된 TLS 연결을 재사용한다. 프록시가 설정된 목적지는 기존 urllib 프록시 처리를 사용한다. 네트워크·디스크·검증 시간을 구분하는 30초 간격 성능 로그와 운영 조정 방법은 [다운로드 성능 안내](PERFORMANCE.md)에 정리했다. 동적 서비스 한도 때문에 429가 전혀 없음을 보장하지는 않는다.
- 실행 중에는 보존 정책 검사, 사용자·OneDrive 검색, 드라이브 변경 조회, 누락 백업 검사, 파일 다운로드, 탐색기 보기 게시, DB 카탈로그 저장 등 현재 작업 단계를 콘솔에 표시한다. 총량을 아는 단계는 처리 수와 백분율을 함께 출력한다. Microsoft Graph가 총 사용자 수를 주지 않는 검색 구간은 `percent=unknown`으로 표시한다.
- 드라이브의 마지막 delta 페이지를 받은 뒤에는 누적 항목을 SQLite에 확정하는 진행률을 별도로 표시한다. 증분 실행은 delta에 포함된 항목의 기존 상태만 조회한다. 상위 폴더 이동·이름 변경·삭제가 있을 때만 전체 부모 계층을 한 번 구성하므로 일반적인 소수 파일 변경에서는 50만 개 전체를 다시 읽지 않는다.
- 다운로드 중에는 전체 대상 파일 수와 바이트, 성공·실패 수, 백분율을 약 1초 간격으로 표시한다. 실패 및 최종 완료는 즉시 표시한다. 전체 용량이 수 TB일 때도 초기 진행을 읽을 수 있도록 파일 수와 바이트 비율을 소수점 셋째 자리까지 따로 표시한다. 이어지는 `browse`는 현재 256건 배치의 진행률과 누적 처리 수를 표시한다. Graph 재시도 로그는 대기 시간도 표시한다.
- 로그는 기본적으로 표준 오류(stderr), 명령 결과 JSON은 표준 출력(stdout)에 기록된다. 하나의 파일로 저장하려면 `OneDriveBackup ... run >> backup.log 2>&1`처럼 두 스트림을 합친다.
- SQLite는 단일 프로세스 잠금, WAL, FULL 동기화를 사용한다. 실제 로컬 디스크 여부는 운영자가 확인해야 한다. 동일 저장소에 서로 다른 DB를 가진 여러 실행기를 동시에 배포하는 구성은 지원하지 않는다.
- 실행 중단 시 수집 페이지와 미완료 작업에서 재개한다. 성공 파일에 대한 DB 기록 직전 중단되면 재다운로드될 수 있으나 기존 정상 파일을 덮어쓰지 않는다.

```text
<backup_root>/
  <mount_marker_name>
  browse/<계정명__ID>/drive_<ID>/current/<원본폴더__ID>/<원본파일명.확장자>
  browse/<계정명__ID>/drive_<ID>/deleted/<원본폴더__ID>/<원본파일명.확장자>
  browse/SharePoint/<사이트명__ID>/<라이브러리명__ID>/current/<원본폴더__ID>/<원본파일명.확장자>
  browse/SharePoint/<사이트명__ID>/<라이브러리명__ID>/deleted/<원본폴더__ID>/<원본파일명.확장자>
  objects/<prefix>/<object_id>.bin   # 불변 파일 내용
  manifests/<object_id>.json        # 원래 이름, 경로, 태그, 해시, 식별자
  catalogs/<run-id>-<uuid>.sqlite    # 실행별 SQLite backup API 스냅샷
```

`browse`는 탐색기에서 원래 파일명과 확장자로 직접 열 수 있는 최신 백업 복사본이다. `run`/`serve`는 백업 처리 후 자동으로 갱신한다. 변경된 파일과 경로는 SQLite의 영속 큐에 기록되므로 중단 후에도 이어지고, 변경이 없는 회차에는 50만 개 보기 파일에 대한 경로 계산과 `stat`을 생략한다. 이미 저장된 `.bin` 백업도 DB 업그레이드 시 필요한 항목만 큐에 넣어 보여 주며, 기존 버전 저장소를 이동하거나 삭제하지 않는다. 과거 버전은 기존 `list`와 `restore`로 조회·복원한다.

계정 폴더는 Microsoft 365의 사용자 표시 이름(`displayName`)으로 만들고 원본 폴더 계층을 따른다. 사용자 이메일은 폴더명에 사용하지 않는다. 동명이인과 폴더명 충돌을 막기 위해 계정과 폴더에는 짧은 ID를 붙인다. 표시 이름이 없으면 사용자 ID를 사용한다. 일반 파일명과 확장자는 유지하며, 대소문자 충돌·금지 문자·예약어·너무 긴 이름에는 ID를 붙이거나 길이를 줄인다. 매우 긴 전체 경로는 `browse/_long_paths/<계정ID>/<드라이브ID>/<파일ID>/<파일명>`에 배치한다. 원래 경로는 DB와 manifest에 남는다.

SharePoint 파일은 `browse/SharePoint` 아래에서 사이트명과 라이브러리명으로 구분한다. 사이트와 라이브러리 이름 변경도 게시 큐에 반영한다. `status`에는 사이트 검색 상태와 라이브러리 연결도 표시한다. 백업 범위는 문서 라이브러리의 다운로드 가능한 파일 내용이며, 사이트 설정·일반 목록 데이터·페이지 구조·권한·휴지통·원본의 과거 버전 전체는 포함하지 않는다. 공유 바로가기 자체는 기존 특수 항목 정책을 적용하고, 연결된 사이트의 라이브러리는 사이트 검색을 통해 따로 백업한다.

OneDrive에서 이름·폴더가 바뀌면 다음 실행에서 새 위치에 복사본을 만든 후 이전 위치의 프로그램 소유 복사본을 정리한다. 원본에서 삭제된 파일은 `deleted` 아래로 이동하여 마지막 백업을 계속 열람할 수 있다. 새로운 내용 백업이 실패하면 이전에 성공한 백업 내용이 표시된다. 비어 있는 폴더와 이동 후 빈 폴더는 별도 정리하지 않는다.

복사 방식이므로 최신 파일 전체 용량만큼 추가 공간이 필요하며, 갱신 중에는 임시 파일 공간도 필요하다. 복사본은 열람용으로 사용한다. 사용자가 내용을 수정한 파일이나 프로그램이 관리하지 않는 기존 파일은 덮어쓰거나 삭제하지 않고 충돌로 기록한다. 수정본을 다른 경로로 옮긴 뒤 `browse`를 다시 실행하면 정상 복사본을 만들 수 있다. 문서 중앙화 솔루션의 접근 권한으로 읽기 전용 열람을 설정할 수 있다.

검증된 임시 복사본을 최종 파일명으로 바꾸므로 마운트가 같은 폴더 내 `os.replace`를 지원해야 한다. 교체 실패나 파일 잠금은 부분 실패로 보고하고 다음 실행에서 재개한다. 임시 파일은 `.backup-<ID>.partial` 형식이며 일반 문서 이름으로 게시하지 않는다. 보기 갱신·이전 경로 정리가 끝나기 전에는 연결된 백업 버전의 폐기를 보류한다.

`list`의 경로는 최신 관측 경로이며, 최초 저장 당시 경로는 버전별 manifest에 남는다. 이후 이름 변경은 DB 스냅샷에 보존된다.

현재 백업의 파일 유실 및 크기 차이는 `repair_check_batch_size` 단위로 순환 검사한다. 검사 위치는 SQLite에 저장하므로 여러 실행에 걸쳐 전체를 확인한다. 동일 크기 손상은 전체 SHA-256을 읽는 `verify`에서 감지한다. 이전 버전 전체에 대한 주기적 해시 점검은 별도 구현 대상이다.

## 완료 판정과 한계

성공은 **마운트에 쓴 데이터를 다시 읽어 검증했다**는 의미이며 `mount_readback_only`로 표시한다. 중앙화 서버의 영속 저장 완료를 확인하는 연동은 제공된 정보가 없어 미구현이다. `fsync`, 읽기 및 공간 조회를 지원하는 마운트가 필요하다. DB 스냅샷 실패는 부분 실패로 표시한다.

원본 삭제 자체로 백업을 삭제하지 않는다. `expire_after_days`에서도 파일마다 마지막 백업 1개는 기간과 관계없이 보존한다. 원본이 삭제된 파일에도 같은 규칙을 적용한다. 다운로드 전에 삭제된 항목의 대기 작업은 명시적인 실패로 남는다. 애초 조회되지 않은 중간 생성·삭제와 중간 편집 버전을 보존하지 못하며, 드라이브 전체의 동일 시점 스냅샷도 보장하지 않는다.

전송·검증 예외 발생 시 이번 `Storage.put`에서 새로 만든 미확정 객체만 정리를 시도한다. 마운트가 끊겼거나 프로세스가 강제 종료되면 부분 파일이 남을 수 있다. 이전 실행의 미등록 객체와 manifest 없는 잔여 파일 전체를 자동 정리하지는 않는다. 실패 작업은 횟수 제한 후 중지하며 운영자가 원인을 해결하고 재시도할 수 있다. 등록된 객체·manifest·DB 스냅샷은 아래 보존 정책으로 정리한다. 대규모 운영 검증 범위와 재현 방법은 [대규모 처리 안내](SCALABILITY.md)를 참고한다.

DB 유실 시 서비스를 중지하고 검증한 최신 `catalogs` 스냅샷을 실제 로컬 SQLite 경로에 복사한 뒤 재개한다. 기존 DB의 WAL/SHM 파일이 섞이지 않는 새 경로를 사용한다. 설정의 테넌트와 백업 루트는 스냅샷 값과 일치해야 한다. 스냅샷 이후의 객체는 manifest로 확인할 수 있지만 자동 DB 재구성 명령은 아직 없다.

미구현 연동·정책: 인증서/관리 ID, 다중 테넌트 실행 관리, 서버 영속 저장 확인, ACL·공유 링크 복원, OneNote/바로가기 내용, 삭제 계정 탐색, OneDrive 직접 복원, 알림 채널, 서비스 설치. 기존 설계 문서의 미정 정보는 계속 빈 placeholder로 유지한다.

## 기간 경과 폐기

`retention_policy`를 `expire_after_days`로 설정하고 `retention_days`에 원하는 일수를 입력하면 활성화된다. 예제 설정은 두 값 모두 `null`로 남겨 두었다. 기존 `keep_all` 설정 파일은 변경 없이 계속 사용할 수 있다.

- 기준: **각 버전의 백업 저장 완료 시각 + 보존 일수 × 24시간**. 원본 파일의 수정일이나 마지막 조회 시각이 아니다. 경계 시각에 도달하면 대상이 된다.
- 실행: `run`/`serve`의 각 회차 시작 시 자동 처리한다. 백업 주기에 따라 실제 폐기 시각은 만료 시각보다 늦을 수 있다. `prune`으로 따로 실행할 수도 있다.
- 범위: 파일별 최신 저장 버전을 제외한 만료 버전과 대응하는 manifest. 원본이 삭제되어도 마지막 저장 버전은 남긴다. 만료된 DB 스냅샷과 그 manifest 정리는 기존과 동일하다. OneDrive 원본에는 삭제 요청을 보내지 않는다.
- 파일별 최소 1개 보존은 필수 규칙이며 별도 설정이 필요 없다. 변경이 없는 단일 버전 파일은 매 실행마다 내용을 다시 해시하지 않고 최신본을 계속 유지한다. 실제 만료 이전 버전이나 미완료 폐기 작업이 있는 파일에서만 최신 객체와 정상 대체본을 확인한다. 최신본의 파일·해시·manifest 존재 확인이 실패하면 확인 가능한 이전 정상본도 보호하고, 정상본을 하나도 확인할 수 없으면 해당 파일의 남은 객체를 모두 보존한다.
- 예전 정책에서 최신본이 이미 폐기된 경우 변경 이벤트가 없어도 다음 백업 실행에서 원본 재다운로드 작업을 등록한다. 원본까지 삭제되어 재다운로드할 수 없으면 실패로 보고한다. 이미 삭제된 내용 자체를 되살리는 기능은 아니다.
- 폐기 작업을 SQLite에 먼저 기록한 다음 파일을 하나씩 지우고 DB 참조를 정리한다. 중단되거나 파일이 잠긴 경우 다음 실행에서 재개하며, 이미 없는 파일은 완료된 단계로 취급한다. 폐기 진행 중인 버전은 `list`/`restore`에서 제외한다.
- `keep_all`로 전환하면 새 폐기 대상을 만들지 않는다. 기존 폐기 큐도 최소 1개 보존 규칙으로 다시 검사하여 보호 대상은 취소한다. 보호되지 않는 이전 버전의 확정된 폐기는 재개한다. 일부 삭제된 보호 대상은 다음 백업에서 복구를 시도한다. `prune --dry-run`은 큐 등록이나 파일 삭제 없이 실제 폐기 대상을 출력한다.
- 오래된 버전의 catalog manifest에 생성 시각이 없으면 해당 manifest의 파일 수정 시각을 사용한다. 새 catalog에는 생성 시각을 명시한다. 임의 파일과 manifest 없는 고아 파일은 삭제 대상에 포함하지 않는다.
- 폐기는 마운트의 파일 삭제 기능을 호출하는 논리적 삭제다. 중앙화 솔루션 자체 휴지통·버전 보존·물리적 데이터 소거는 그 솔루션 정책에 따른다. 현재 DB에는 증분 추적 및 폐기 이력이 남으며, 미만료 DB 스냅샷에도 이전 메타데이터가 남을 수 있다.

오래된 DB 스냅샷으로 복구하면 이미 폐기된 파일의 참조가 들어 있을 수 있다. `prune`은 보호 대상의 폐기를 취소하고, 이후 백업 실행에서 누락 파일 복구를 시도한다. 파일별 별도 법적 보존 정책은 구현하지 않았다.

## 검증

```powershell
python tools/check_py38_compat.py
python -m unittest discover -s tests -v
python -m compileall -q onedrive_backup tests
```

테스트는 임시 디렉터리와 모의 Graph 응답만 사용한다. 실제 Microsoft 365·중앙화 솔루션 접속이나 회사 파일 다운로드는 수행하지 않는다.

API 참고: [사용자 드라이브 조회](https://learn.microsoft.com/en-us/graph/api/drive-list?view=graph-rest-1.0), [delta 변경 추적](https://learn.microsoft.com/en-us/graph/api/driveitem-delta?view=graph-rest-1.0), [앱 인증](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-client-creds-grant-flow).

0.13.0의 큐 정책, 새 선택 설정, 연결 중단 시 Range 재개 범위와 운영 한계는 [다운로드 성능 안내](PERFORMANCE.md)에 정리했다. 기존 config 및 DB를 그대로 사용할 수 있다.
