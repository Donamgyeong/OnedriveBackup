# Python 없는 Windows 배포 안내

## 사용자 설치

1. `OneDriveBackup-<버전>-win64.zip`을 로컬 디스크의 전용 폴더에 압축 해제한다. 네트워크 드라이브나 백업 대상 마운트 안에 프로그램과 SQLite DB를 두지 않는다.
2. `config.example.json`을 `config.json`으로 복사하고 환경에 맞게 입력한다. `sqlite_path`에는 폴더가 아닌 로컬 디스크의 파일 경로를 지정한다.
3. `client_secret`에 Entra 앱의 클라이언트 비밀값을 입력한다. `config.json`은 평문 비밀값을 포함하므로 운영 서비스 계정과 관리자만 읽을 수 있도록 Windows 파일 권한을 제한한다. 이 파일을 이메일·메신저·소스 저장소에 올리지 않는다.
4. 중앙화 마운트 루트에 `mount_marker_name` 파일을 만들고 UTF-8 내용이 `mount_marker_value`와 정확히 같게 한다.
5. 명령 프롬프트에서 먼저 검증한다.

```bat
OneDriveBackup.exe --config config.json validate
OneDriveBackup.exe --config config.json status
```

`RUN_ONCE.cmd`는 백업을 한 번 실행하고 결과를 화면에 남긴다. `STATUS.cmd`는 상태를 조회하고 `UPDATE_BROWSE.cmd`는 저장된 백업에서 탐색기용 최신 파일 보기를 갱신한다. 장기 운영은 `OneDriveBackup.exe --config config.json run`을 Windows 작업 스케줄러에서 원하는 주기로 실행하는 방식을 권장한다. 작업 스케줄러의 시작 위치는 압축을 푼 프로그램 폴더로 지정한다.

프로그램 폴더 안의 `OneDriveBackup.exe`와 `_internal` 폴더는 한 묶음이다. `_internal`을 삭제하거나 실행 파일만 따로 복사하면 실행되지 않는다. 대상 PC에 Python이나 pip를 설치할 필요는 없다.

배포본은 빌드한 Windows 아키텍처용이다. 이 빌드는 64비트 Windows용이며 Windows 10 이상에서 운영하는 것을 기준으로 한다. 실제 운영 전에 대상 PC에서 Microsoft Graph 인증, 마운트 쓰기·교체·삭제, 대용량 파일, 복원 동작을 검증한다.

## 개발자 빌드

빌드 PC에는 Python과 PyInstaller가 필요하다. 이는 사용자 PC에는 필요하지 않다.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\build.ps1
```

테스트를 통과하면 `dist\OneDriveBackup` 폴더, 버전이 포함된 배포 ZIP, 무결성 확인용 `.sha256` 파일을 만든다. `config.json`, SQLite 파일, 인증 비밀값은 배포본에 포함하지 않는다. Windows용 실행 파일은 Windows에서 빌드해야 한다.
