# OneDrive 중앙화 증분 백업

Python 3.11 이상과 표준 라이브러리만 사용하는 초기 구현이다. SQLite에 변경 조회 커서와 영속 작업 큐를 기록하고, 회사 사용자별 OneDrive 파일을 중앙화 솔루션의 마운트 경로에 저장한다. 상세 배경은 [개념 설계](CONCEPT_DESIGN.md)를 참고한다.

## 실행 준비

```powershell
Copy-Item config.example.json config.json
python -m onedrive_backup --config config.json validate
```

예제의 모든 운영 설정은 `null`이다. 값을 입력하기 전 검증은 실패하며 백업을 실행하지 않는다. 제공되지 않은 계정 정보, 경로, 주기, 인증 비밀값은 코드에 채워 넣지 않았다. Python 설치 외 필수 패키지 설치는 없다.

| 설정 | 입력할 내용 |
|---|---|
| tenant_id / client_id | Entra 테넌트 ID / 등록 앱 ID |
| client_secret_env | 클라이언트 비밀값이 들어 있는 **환경 변수 이름** |
| authority_url | 해당 클라우드의 인증 기본 URL; 공용 클라우드 예: `https://login.microsoftonline.com` |
| graph_url | Graph v1.0 기본 URL; 공용 클라우드 예: `https://graph.microsoft.com/v1.0` |
| sqlite_path | 실제 로컬 디스크의 SQLite 절대 **파일** 경로. 예: `C:\\backup-state\\backup.sqlite` (폴더 경로는 사용할 수 없음) |
| backup_root | 미리 마운트되어 있는 대상의 절대 경로 |
| mount_marker_name / mount_marker_value | 대상 루트에 미리 준비한 UTF-8 식별 파일의 이름 / 내용 |
| backup_interval_seconds | 반복 실행 완료 후 다음 실행까지 대기할 초 |
| minimum_free_bytes | 다운로드 후 남겨 둘 최소 공간 바이트 |
| max_attempts | 파일 작업 최대 시도 횟수 |
| retry_base_seconds | 지수 백오프의 기본 초 |
| http_timeout_seconds / max_http_attempts | HTTP 타임아웃 초 / 요청 최대 시도 횟수 |
| retention_policy | `keep_all`: 계속 보존 / `expire_after_days`: 최신 1개를 남기고 기간이 지난 이전 버전 폐기 |
| retention_days | 보존 일수(양의 정수). `expire_after_days` 선택 시 필수, `keep_all`에서는 null 가능 |
| special_item_policy | `report`: 특수 항목을 실패로 보고 / `exclude`: 명시적으로 제외 |
| guest_account_policy | `include` 또는 `exclude` |

앱 인증은 현재 client credentials + 환경 변수의 client secret을 구현했다. 앱에는 사용 API에 맞는 `User.Read.All`, `Files.Read.All` application 권한과 관리자 동의가 필요하다. 인증서·관리 ID 인증은 아직 구현하지 않았다. 비밀값을 JSON 파일에 직접 저장하지 않는다.

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

종료 코드는 0=정상, 1=실행 실패 또는 부분 실패, 2=설정/인자 오류, 130=사용자 중단이다. 반복 모드는 고정 시각 스케줄이 아니라 실행 완료 기준 간격이다. Windows 서비스 등록, 알림 전송과 로그 회전은 운영 환경이 정해진 뒤 연결한다.

## 저장과 증분 처리

- 모든 사용자 페이지와 사용자 드라이브 페이지를 조회한다. business 유형의 사용자 드라이브를 대상으로 한다. 비활성 사용자도 게스트 정책 외 별도 필터 없이 탐색한다. 삭제된 사용자 복구 API 및 SharePoint 팀 사이트 별도 탐색은 미구현이다.
- 초기 delta 전체 열거 후 변경분만 조회한다. 내용 태그가 같으면 이름·폴더 변경만 DB에 반영한다. 변경된 파일은 전체 내용을 내려받는다.
- delta 페이지를 DB에 보관하고 끝까지 받은 뒤 마지막 항목 상태로 정규화한다. 작업 큐와 새 delta 커서를 하나의 트랜잭션으로 확정한다.
- 다운로드 작업은 순차 실행한다. 다운로드 전후 원본 태그, 파일 크기, 대상 읽기 SHA-256을 검증한다. HTTP 제한 응답은 Retry-After를 존중하고 일시 오류를 재시도한다.
- SQLite는 단일 프로세스 잠금, WAL, FULL 동기화를 사용한다. 실제 로컬 디스크 여부는 운영자가 확인해야 한다. 동일 저장소에 서로 다른 DB를 가진 여러 실행기를 동시에 배포하는 구성은 지원하지 않는다.
- 실행 중단 시 수집 페이지와 미완료 작업에서 재개한다. 성공 파일에 대한 DB 기록 직전 중단되면 재다운로드될 수 있으나 기존 정상 파일을 덮어쓰지 않는다.

```text
<backup_root>/
  <mount_marker_name>
  browse/<계정명__ID>/drive_<ID>/current/<원본폴더__ID>/<원본파일명.확장자>
  browse/<계정명__ID>/drive_<ID>/deleted/<원본폴더__ID>/<원본파일명.확장자>
  objects/<prefix>/<object_id>.bin   # 불변 파일 내용
  manifests/<object_id>.json        # 원래 이름, 경로, 태그, 해시, 식별자
  catalogs/<run-id>-<uuid>.sqlite    # 실행별 SQLite backup API 스냅샷
```

`browse`는 탐색기에서 원래 파일명과 확장자로 직접 열 수 있는 최신 백업 복사본이다. `run`/`serve`는 백업 처리 후 자동으로 갱신한다. 이미 저장된 `.bin` 백업도 `browse` 명령 또는 다음 백업 실행에서 변환해 보여 주며, 기존 버전 저장소를 이동하거나 삭제하지 않는다. 과거 버전은 기존 `list`와 `restore`로 조회·복원한다.

계정별·드라이브별로 구분하고 원본 폴더 계층을 따른다. 계정과 폴더에는 충돌 방지를 위한 짧은 ID를 붙인다. 일반 파일명과 확장자는 유지하며, 대소문자 충돌·금지 문자·예약어·너무 긴 이름에는 ID를 붙이거나 길이를 줄인다. 매우 긴 전체 경로는 `browse/_long_paths/<계정ID>/<드라이브ID>/<파일ID>/<파일명>`에 배치한다. 원래 경로는 DB와 manifest에 남는다.

OneDrive에서 이름·폴더가 바뀌면 다음 실행에서 새 위치에 복사본을 만든 후 이전 위치의 프로그램 소유 복사본을 정리한다. 원본에서 삭제된 파일은 `deleted` 아래로 이동하여 마지막 백업을 계속 열람할 수 있다. 새로운 내용 백업이 실패하면 이전에 성공한 백업 내용이 표시된다. 비어 있는 폴더와 이동 후 빈 폴더는 별도 정리하지 않는다.

복사 방식이므로 최신 파일 전체 용량만큼 추가 공간이 필요하며, 갱신 중에는 임시 파일 공간도 필요하다. 복사본은 열람용으로 사용한다. 사용자가 내용을 수정한 파일이나 프로그램이 관리하지 않는 기존 파일은 덮어쓰거나 삭제하지 않고 충돌로 기록한다. 수정본을 다른 경로로 옮긴 뒤 `browse`를 다시 실행하면 정상 복사본을 만들 수 있다. 문서 중앙화 솔루션의 접근 권한으로 읽기 전용 열람을 설정할 수 있다.

검증된 임시 복사본을 최종 파일명으로 바꾸므로 마운트가 같은 폴더 내 `os.replace`를 지원해야 한다. 교체 실패나 파일 잠금은 부분 실패로 보고하고 다음 실행에서 재개한다. 임시 파일은 `.backup-<ID>.partial` 형식이며 일반 문서 이름으로 게시하지 않는다. 보기 갱신·이전 경로 정리가 끝나기 전에는 연결된 백업 버전의 폐기를 보류한다.

`list`의 경로는 최신 관측 경로이며, 최초 저장 당시 경로는 버전별 manifest에 남는다. 이후 이름 변경은 DB 스냅샷에 보존된다.

현재 백업의 파일 유실 및 크기 차이는 매 실행 시 검사한다. 동일 크기 손상은 `verify`에서 감지한다. 이전 버전 전체에 대한 주기적 해시 점검은 별도 구현 대상이다.

## 완료 판정과 한계

성공은 **마운트에 쓴 데이터를 다시 읽어 검증했다**는 의미이며 `mount_readback_only`로 표시한다. 중앙화 서버의 영속 저장 완료를 확인하는 연동은 제공된 정보가 없어 미구현이다. `fsync`, 읽기 및 공간 조회를 지원하는 마운트가 필요하다. DB 스냅샷 실패는 부분 실패로 표시한다.

원본 삭제 자체로 백업을 삭제하지 않는다. `expire_after_days`에서도 파일마다 마지막 백업 1개는 기간과 관계없이 보존한다. 원본이 삭제된 파일에도 같은 규칙을 적용한다. 다운로드 전에 삭제된 항목의 대기 작업은 명시적인 실패로 남는다. 애초 조회되지 않은 중간 생성·삭제와 중간 편집 버전을 보존하지 못하며, 드라이브 전체의 동일 시점 스냅샷도 보장하지 않는다.

중단·검증 실패로 생긴 미등록 객체와 부분 파일은 자동 삭제하지 않는다. 실패 작업은 횟수 제한 후 중지하며 운영자가 원인을 해결하고 재시도할 수 있다. 등록된 백업 객체·manifest·DB 스냅샷은 아래 보존 정책으로 정리하며, 미등록 실패 잔여 파일 정리는 별도 구현 대상이다. 매우 큰 조직에서는 최종 delta 정규화 트랜잭션, 사용자 목록 보관, 순차 다운로드의 처리량 측정이 필요하다.

DB 유실 시 서비스를 중지하고 검증한 최신 `catalogs` 스냅샷을 실제 로컬 SQLite 경로에 복사한 뒤 재개한다. 기존 DB의 WAL/SHM 파일이 섞이지 않는 새 경로를 사용한다. 설정의 테넌트와 백업 루트는 스냅샷 값과 일치해야 한다. 스냅샷 이후의 객체는 manifest로 확인할 수 있지만 자동 DB 재구성 명령은 아직 없다.

미구현 연동·정책: 인증서/관리 ID, 다중 테넌트 실행 관리, 서버 영속 저장 확인, ACL·공유 링크 복원, OneNote/바로가기 내용, 삭제 계정 탐색, OneDrive 직접 복원, 알림 채널, 서비스 설치. 기존 설계 문서의 미정 정보는 계속 빈 placeholder로 유지한다.

## 기간 경과 폐기

`retention_policy`를 `expire_after_days`로 설정하고 `retention_days`에 원하는 일수를 입력하면 활성화된다. 예제 설정은 두 값 모두 `null`로 남겨 두었다. 기존 `keep_all` 설정 파일은 변경 없이 계속 사용할 수 있다.

- 기준: **각 버전의 백업 저장 완료 시각 + 보존 일수 × 24시간**. 원본 파일의 수정일이나 마지막 조회 시각이 아니다. 경계 시각에 도달하면 대상이 된다.
- 실행: `run`/`serve`의 각 회차 시작 시 자동 처리한다. 백업 주기에 따라 실제 폐기 시각은 만료 시각보다 늦을 수 있다. `prune`으로 따로 실행할 수도 있다.
- 범위: 파일별 최신 저장 버전을 제외한 만료 버전과 대응하는 manifest. 원본이 삭제되어도 마지막 저장 버전은 남긴다. 만료된 DB 스냅샷과 그 manifest 정리는 기존과 동일하다. OneDrive 원본에는 삭제 요청을 보내지 않는다.
- 파일별 최소 1개 보존은 필수 규칙이며 별도 설정이 필요 없다. 변경이 없는 파일은 기존 최신본을 계속 유지한다. 새 버전 다운로드가 실패하면 이전 정상본을 유지한다. 최신 객체와 DB의 현재 참조를 보호하며, 최신본의 파일·해시·manifest 존재 확인이 실패하면 확인 가능한 이전 정상본도 보호한다. 정상본을 하나도 확인할 수 없으면 해당 파일의 남은 객체를 모두 보존한다.
- 예전 정책에서 최신본이 이미 폐기된 경우 변경 이벤트가 없어도 다음 백업 실행에서 원본 재다운로드 작업을 등록한다. 원본까지 삭제되어 재다운로드할 수 없으면 실패로 보고한다. 이미 삭제된 내용 자체를 되살리는 기능은 아니다.
- 폐기 작업을 SQLite에 먼저 기록한 다음 파일을 하나씩 지우고 DB 참조를 정리한다. 중단되거나 파일이 잠긴 경우 다음 실행에서 재개하며, 이미 없는 파일은 완료된 단계로 취급한다. 폐기 진행 중인 버전은 `list`/`restore`에서 제외한다.
- `keep_all`로 전환하면 새 폐기 대상을 만들지 않는다. 기존 폐기 큐도 최소 1개 보존 규칙으로 다시 검사하여 보호 대상은 취소한다. 보호되지 않는 이전 버전의 확정된 폐기는 재개한다. 일부 삭제된 보호 대상은 다음 백업에서 복구를 시도한다. `prune --dry-run`은 큐 등록이나 파일 삭제 없이 실제 폐기 대상을 출력한다.
- 오래된 버전의 catalog manifest에 생성 시각이 없으면 해당 manifest의 파일 수정 시각을 사용한다. 새 catalog에는 생성 시각을 명시한다. 임의 파일과 manifest 없는 고아 파일은 삭제 대상에 포함하지 않는다.
- 폐기는 마운트의 파일 삭제 기능을 호출하는 논리적 삭제다. 중앙화 솔루션 자체 휴지통·버전 보존·물리적 데이터 소거는 그 솔루션 정책에 따른다. 현재 DB에는 증분 추적 및 폐기 이력이 남으며, 미만료 DB 스냅샷에도 이전 메타데이터가 남을 수 있다.

오래된 DB 스냅샷으로 복구하면 이미 폐기된 파일의 참조가 들어 있을 수 있다. `prune`은 보호 대상의 폐기를 취소하고, 이후 백업 실행에서 누락 파일 복구를 시도한다. 파일별 별도 법적 보존 정책은 구현하지 않았다.

## 검증

```powershell
python -m unittest discover -s tests -v
python -m compileall -q onedrive_backup tests
```

테스트는 임시 디렉터리와 모의 Graph 응답만 사용한다. 실제 Microsoft 365·중앙화 솔루션 접속이나 회사 파일 다운로드는 수행하지 않는다.

API 참고: [사용자 드라이브 조회](https://learn.microsoft.com/en-us/graph/api/drive-list?view=graph-rest-1.0), [delta 변경 추적](https://learn.microsoft.com/en-us/graph/api/driveitem-delta?view=graph-rest-1.0), [앱 인증](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-client-creds-grant-flow).
