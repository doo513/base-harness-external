# 외부 호출자 사용 안내

사용자에게 아래 운영 지침을 매번 붙이게 하지 않으려면
[Skill + Harness 설치 안내](INSTALL.md)를 사용하세요. 공통 Skill과 JSON 호출 도우미가
같은 저장소에 포함되어 있습니다. 아래는 직접 CLI를 호출할 때의 상세 참고입니다.

외부 호출자가 작업을 분석하고 파일을 편집하며, Harness를 CLI 도구로 호출합니다.
Harness는 Run·원문·개정 이력·제출 스냅샷·검증 결과를 보관합니다. 호출자는 모델
에이전트, 일반 프로그램, 플러그인 또는 사람이 될 수 있습니다. Harness 내부에
호출자의 모델이나 계정 정보를 등록할 필요는 없습니다.

현재 확인한 실행 환경은 WSL/Linux의 Python 3.11+와 Bun입니다. 호출 프로그램이
Windows에서 동작해도 Harness 명령만 WSL에서 실행할 수 있습니다. 호출자와 Harness
프로세스의 실행 위치는 독립적입니다. 아래 저장소 경로는 자신의 체크아웃 위치로 설정하세요.

## 1. 먼저 환경 확인

WSL에서:

```sh
cd /path/to/base-harness-external
HARNESS_REPO="$(pwd)"
bash "$HARNESS_REPO/scripts/harness-tool" doctor
bash "$HARNESS_REPO/scripts/harness-tool" doctor --sandbox
```

Windows PowerShell 또는 Windows 프로그램의 셸 도구에서는:

```powershell
$HarnessRepo = "/path/in/wsl/base-harness-external"
$Distro = "Ubuntu-24.04"
wsl.exe -d $Distro --exec bash "$HarnessRepo/scripts/harness-tool" doctor --sandbox
```

이후 명령도 같은 접두사 뒤에 붙이면 됩니다. 여러 WSL 배포판을 사용하면
`wsl.exe -d 배포판이름 --exec ...`로 같은 배포판을 계속 지정하세요.
`doctor --sandbox`의 `healthy`와 `command_execution_verified`가 모두 true여야
명령 실행 검증을 사용할 준비가 된 것입니다. `doctor`만 통과한 것은 파일 검사와
상태 저장의 준비 확인입니다. JSON의 `ok`는 API 처리 여부이지 테스트 통과가 아닙니다.

`sandbox_adapter`가 없으면 소스 체크아웃을 사용하세요. Bun을 찾지 못하면 해당
WSL의 PATH 또는 `BUN`에 실행 파일 경로를 설정하세요. 실행 스크립트는 PATH에
Bun이 없을 때 기본 설치 위치인 `~/.bun/bin/bun`도 확인합니다. `SANDBOX_SETUP_FAILED`면
현재 호스트 실행 제한이 namespace 사용을 허용하는지 확인해야 합니다.
이 경우 Harness는 격리 없는 명령 실행으로 대체하지 않습니다.

## 2. 호출자 통합 규칙

모델 에이전트에 작업을 맡긴다면 아래 내용을 작업 요청과 함께 전달할 수 있습니다.
셸 도구가 없는 호출 환경에는 먼저 호출자 측 CLI/MCP 어댑터가 필요합니다. 이
저장소는 MCP 서버를 띄우지 않습니다.

> 이 작업은 Base Harness External CLI로 진행 상태와 검증을 관리해 주세요.
> 도구 명령은 위에서 확인한 harness-tool 경로를 사용하세요.
> 시작 시 develop 도메인과 원래 요청을 전달하고, 기준이 불명확하면 exploratory로
> 시작하세요. 요구에 명시된 필수 검사는 시작 정책에 required-check로 고정하세요.
> 아직 정의하지 않은 필수 검사는 deferred-check로 명시하세요. 기대값과 명령에
> id를 지정하면 domain.ID로 안정적으로 참조할 수 있습니다.
> working directory의 파일은 기존 편집 도구로 수정하고, submit으로 복사본을 제출하세요.
> verify가 반환한 job_id를 status 또는 resume으로 조회하세요. 오류나 실패는 실제
> 관측으로 기록하고, 이를 근거로 다음 행동을 판단하세요. 해결 여부와 불확실성은
> assess에 별도로 남기세요. completed, 측정 통과, 모델의 satisfied를 구분하세요.
> Assessment의 citation_bindings와 citation_summary에서 인용 관측이 현재 Candidate,
> 해석 및 검사 집합에 속하는지 확인하세요. contextual 관측은 과거 이력이며 현재
> Candidate를 직접 측정한 사실로 취급하지 마세요.
> 종결 판단은 resolution의 display_status, measurement_status, assessment_status,
> gate_status를 함께 읽으세요. 정책 승인 주장은 unverified이며 사용자 인증이 아닙니다.
> 상태·검증기·스냅샷 파일을 직접 수정하지 마세요. 최종 보고에는 run_id,
> candidate_hash, 측정 결과, 게이트 결과, 모델 평가, 남은 불확실성을 적어 주세요.

명령에 넘기는 workspace와 JSON 파일 경로는 **WSL에서 접근 가능한 절대 경로**를
사용하세요. 예를 들어 `C:\Users\...`는 `/mnt/c/Users/...`로 표현합니다.
항상 같은 경로 표기를 사용하세요. 상태 저장 경로는 작업장과 분리해야 합니다.
기본 상태 위치는 doctor에 표시되며, 별도 저장소를 선택하면 모든 호출에 같은
`--state-dir /별도/상태/경로`를 명령 이름 앞에 지정하세요.

## 3. 최소 작업 순서

아래 예시는 WSL 셸 기준입니다. `RUN_ID`, `JOB_ID`는 실제 응답 값으로 바꾸세요.

```sh
bash scripts/harness-tool start --domain develop --mode exploratory --goal '사용자의 원래 요청' --workspace /absolute/project --request-id task-start-001
bash scripts/harness-tool resume --run-id RUN_ID
bash scripts/harness-tool revise --run-id RUN_ID --data /absolute/revision.json --request-id revision-001
bash scripts/harness-tool submit --run-id RUN_ID --request-id submit-001
bash scripts/harness-tool verify --run-id RUN_ID --request-id verify-001
bash scripts/harness-tool resume --run-id RUN_ID
bash scripts/harness-tool records --run-id RUN_ID --kind measurements --job-id JOB_ID --limit 5
bash scripts/harness-tool assess --run-id RUN_ID --data /absolute/assessment.json --request-id assessment-001
bash scripts/harness-tool finish --run-id RUN_ID --outcome completed --request-id finish-001
```

JSON 형식은 README의 revision/assessment 설명을 사용하세요. JSON 옵션은 파일 경로,
stdin을 뜻하는 `-`, 또는 `json:{...}` 접두사의 인라인 JSON을 받습니다. 한 호출에서
stdin은 한 옵션만 사용할 수 있습니다. 예를 들어 WSL 셸에서 다음처럼 호출합니다.

```sh
bash "$HARNESS_REPO/scripts/harness-tool" observe --run-id RUN_ID --data 'json:{"note":"측정 결과를 검토 중"}' --request-id note-001
```

입력 목록에는 실제
실행에 필요한 코드·테스트·fixture를 명시해야 합니다. 외부 의존성을 자동으로
설치하거나 전체 프로젝트를 자동 수집하지 않습니다. 명령 Sandbox는 네트워크가
격리되어 있으므로 최초 사용은 표준 라이브러리만 필요한 작은 프로젝트가 적합합니다.

동일 요청의 재시도에는 같은 request-id를 쓰고, 의도적으로 다시 실행하는 작업은
새 ID를 사용하세요. 원문·필수 정책·누적 예산은 같은 Run에서 유지됩니다.
한계 도달로 Run이 handoff 상태가 된 요청도 같은 request-id로 재시도하면 최초의
한계 오류가 다시 반환됩니다.
검증기 소스가 바뀌어 `VERIFIER_CHANGED`가 나오면 새 Run으로 시작해야 합니다.

## 4. 문맥 복원과 검사 변경

새 세션에서는 `context --run-id RUN_ID`로 첫 페이지를 받고 `next_page`가 없어질
때까지 `--page TOKEN`으로 이어 읽습니다. 마지막의 `next_cursor`를 다음
`context --run-id RUN_ID --after CURSOR`에 넘기면 변경분만 받습니다. 세션에서
맥락을 잃었다면 커서를 생략하고 다시 복원하세요. `critical`의 현재 실패·미검증·
불확실성 상태는 변경분이 비어 있어도 확인해야 합니다. 이력 원문은 삭제하지 않습니다.
텍스트 참조 및 복원 형식은 [호출 프로토콜](skills/harness-workflow/references/protocol.md)에
있으며, 기존 `resume` 및 `status --view full`도 계속 사용할 수 있습니다.

새 호출 도우미는 요청 생성 시 조회하지 않고, 실행할 때 `invoke` 한 번으로 Core의
문맥 확인과 작업을 처리합니다. 검사를 생략하는 것이 아니며, 예전 요청 봉투도 지원합니다.

```sh
bash scripts/harness-tool list-runs --limit 10
bash scripts/harness-tool resume --run-id RUN_ID
bash scripts/harness-tool records --run-id RUN_ID --kind interpretations --limit 5
bash scripts/harness-tool records --run-id RUN_ID --kind checks --limit 5
```

응답의 `next_offset`을 다음 호출의 `--offset`으로 사용합니다. resume은 복원용
요약을 반환하며 작업이나 worker를 자동으로 재실행하지 않습니다. API v2에서는 status도
기본 요약이며 상세 상태는 `status --view full`로 요청합니다. 측정 본문은 records의
measurements에 있고 Job 결과는 observation_refs만 담습니다. 큰 이력은 나누어 읽으세요.

입력 범위나 profile을 개정할 때 기존 보조 검사가 맞지 않으면
`CHECK_SCOPE_CONFLICT`를 반환하고 기존 Run을 그대로 유지합니다. 보조 검사의
비활성화 요청 JSON 예시는 다음과 같습니다.

```json
{"check_id":"probe","expected_revision":1,"interpretation_revision":2,"reason":"입력 범위 변경으로 더 이상 적용되지 않는 검사"}
```

```sh
bash scripts/harness-tool retire-check --run-id RUN_ID --data /absolute/retire.json --request-id retire-001
```

이후 revise를 다시 호출하세요. 재활성화는 check 명령에 비활성화된 검사의 최신
expected_revision을 전달합니다. 필수 게이트 검사는 비활성화할 수 없습니다.
`file-0` 같은 Domain 생성 검사는 원래 parameters를 revise해서 변경합니다.

## 5. 취소와 중단 복구

```sh
bash scripts/harness-tool cancel --run-id RUN_ID --job-id JOB_ID --request-id cancel-001
bash scripts/harness-tool resume --run-id RUN_ID
```

cancel은 해당 검증 Job만 취소하며 Run과 누적 예산은 유지합니다. 이미 끝난 Job에는
영향을 주지 않습니다. 실행 중이던 Job의 `cleanup=pending`은 정리가 아직
확인되지 않았다는 뜻입니다. 후속 resume/records 조회로 complete 여부를 확인하세요.
Run 자체를 닫으려면 finish의 partial 또는 abandoned를 사용합니다.

비정상 종료로 남은 오래된 제출 임시 폴더는 먼저 조회할 수 있습니다.

```sh
bash scripts/harness-tool cleanup --run-id RUN_ID
bash scripts/harness-tool cleanup --run-id RUN_ID --apply
```

기본 최소 나이는 1시간입니다. POSIX 파일 잠금으로 사용 중인 폴더를 건너뛰며,
apply는 소유권을 확인한 임시 폴더만 `.quarantine_...`로 옮깁니다. 응답의
recovery_path에서 내용을 확인할 수 있고, 작업 파일과 제출된 Candidate는 유지합니다.
소유권을 확인할 수 없는 옛 임시 폴더는 자동 처리하지 않습니다. 이 명령은 복구용
격리이며 디스크 공간을 자동으로 확보하는 삭제 명령은 아닙니다.

## 현재 보증 범위

이 경로는 local-advisory이며 `ready=false`입니다. 같은 사용자 권한에서 상태와
검증기를 악의적으로 수정하는 경우를 막는 보호된 인증은 별도 기능입니다.
외부 모델 에이전트의 작업 품질·속도 비교와 Windows 네이티브 Harness 실행 검증은
별도로 수행해야 합니다. 여기서 제공하는 것은 호출자에 독립적인 도구 호출 경로입니다.
