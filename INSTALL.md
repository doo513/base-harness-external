# Skill + Harness 설치와 운영

이 저장소는 **독립 Harness 본체 + 공통 `harness-workflow` Skill + 작은 CLI 호출 도우미**를
함께 배포합니다. 모델·계정·UI·자동 개발 루프는 포함하지 않습니다. 에이전트가 기존 도구로
작업하고 Harness를 호출합니다. Skill은 운영 지침이며 권한 강제나 보호된 인증이 아닙니다.

## 1. 실행 환경을 한 번 설정

현재 명령 검증 경로는 WSL/Linux의 Python 3.11+와 Bun, namespace Sandbox를 사용합니다.
네이티브 Windows Harness 실행을 검증했다는 뜻은 아닙니다. Windows에 있는 에이전트도
WSL의 도우미를 호출할 수 있으며, 모델을 WSL에 설치할 필요는 없습니다.

저장소를 원하는 영구 위치에 체크아웃하고, 그 안에서 다음을 실행하세요.

```sh
python3 skills/harness-workflow/scripts/harness_client.py configure \
  --harness-repo "$PWD" \
  --state-dir "$HOME/.local/state/base-harness-external"
python3 skills/harness-workflow/scripts/harness_client.py call \
  --request 'json:{"operation":"doctor","arguments":{"sandbox":true}}'
```

Python 패키지 설치 없이 기존 `scripts/harness-tool`을 호출합니다. Bun 또는 namespace
권한이 없다면 doctor 결과를 먼저 해결하세요. 격리 없는 실행으로 자동 대체하지 않습니다.

도우미의 기본 설정은 `$XDG_CONFIG_HOME/base-harness-external/client.json` 또는
`~/.config/base-harness-external/client.json`입니다. 다른 설정은 모든 호출에 같은
`--config /absolute/client.json`을 붙이거나 `BASE_HARNESS_CLIENT_CONFIG`로 지정하세요.
설정은 절대 경로를 저장하고 기존 파일을 덮어쓰지 않습니다. 기존 설정 변경은 내용을 확인한
뒤 명시적으로 편집하세요. 개인 설정이나 Run 데이터는 GitHub에 커밋하지 않습니다.

상태 디렉터리는 **모든 작업장 밖의 영구 위치**여야 합니다. 컨테이너라면 지속되는 볼륨에
두세요. 경로만으로 저장 수명을 완전히 판별할 수 없으므로 환경 전환 뒤에도 같은 저장소가
마운트되는지 확인해야 합니다. `/tmp` 같은 알려진 임시 위치는 기본 설정에서 거부합니다.
`configure --ephemeral`은 폐기 가능한 테스트 전용입니다.

## 2. 사용하는 에이전트에 Skill 설치

호스트가 지원하는 Skill 검색 디렉터리를 명시하세요. 다음 명령은 그 아래
`harness-workflow` 폴더만 생성하며 기존 Skill·Host 설정·Run을 변경하지 않습니다.

```sh
python3 scripts/install-skill.py --destination /absolute/host/skills
```

이미 같은 이름이 있으면 덮어쓰지 않고 중단합니다. 업데이트 시 기존 사용자 수정과 차이를
확인한 뒤 별도로 반영하세요. 설치를 시험할 때는 빈 임시 Host 디렉터리를 사용하면 됩니다.
Skill은 저장소 밖에 복사해도 작동합니다. 도우미 설정의 `harness_repo`가 본체 위치를 가리킵니다.

자동 발견을 지원하는 Host에서는 해당 작업공간의 지침에 다음처럼 한 번 연결할 수 있습니다.
설치만으로 모든 작업이 하네스 사용에 동의한 것으로 간주하지 않습니다.

> 이 작업공간에서 요청된 개발 작업은 설치한 harness-workflow Skill로 기록·검증한다.
> 구성된 영구 저장소를 사용하고, 기존 작업을 재개할 때는 원문·작업장·Domain을 확인해
> 해당 Run을 이어간다. 분석·구현 방법은 에이전트가 판단한다.

이후 사용자는 만들고 싶은 결과만 요청하면 됩니다. Skill 자동 선택은 Host의 기능에
의존합니다. Skill/셸 도구를 지원하지 않는 Host에는 별도 연동이 필요하며, 이 저장소가 MCP
서버를 제공한다고 가정하면 안 됩니다. MCP가 필요해도 동일 API의 전달 계층만 추가하는 것이
경계이며, 새 모델 루프·상태 저장소를 만드는 것이 아닙니다.

Windows Host가 WSL에 설치한 도우미를 호출하는 형식은 다음과 같습니다. 배포판과 경로는
설치한 환경으로 바꾸세요. `request.json` 안의 workspace도 WSL 절대 경로를 사용합니다.

```powershell
wsl.exe -d YOUR_DISTRO --exec python3 /absolute/host/skills/harness-workflow/scripts/harness_client.py --config /absolute/client.json call --request /absolute/request.json
```

## 3. 호출 규약과 책임

기계적 요청 형식과 실제 예제는 설치본에 포함된
[protocol.md](skills/harness-workflow/references/protocol.md)에 있습니다.
`SKILL.md`는 전체 API 설명을 반복하지 않고 필요한 시점에 이 문서를 읽도록 안내합니다.

| 구성 | 책임 |
|---|---|
| Skill / Host 에이전트 | 요청 해석, Domain 선택, 문제 해결·수정·종결 판단, 관측 검토 |
| 도우미 | 고정 설정, JSON/argv 전달, Run의 작업장·Domain 대조, 명시적 조회 |
| Domain | 계약·검사의 의미와 정규화, 작업별 검증 범위 |
| Core / Verifier | 상태·스냅샷·검사 참조·예산·수명주기, 격리 실행과 사실 기록 |

도우미는 성공 기준·정책을 생성하거나 사용자 승인을 추정하지 않습니다. `start`에는 mode와
provenance를 명시합니다. 모델이 구성한 정책이면 `declared_author: model`로 남기고, 실제
사용자 요구나 설정된 운영 정책과 구별하세요. 권한 허용은 계속 Host와 Harness의 경계를 따릅니다.

새 작업은 새 Run, 같은 작업의 재개는 명시적인 Run ID입니다. `find`는 후보를 열거할 뿐
선택하지 않습니다. 저장소가 사라졌거나 여러 Run이 모호하면 문제를 보고해야 합니다.
Host 세션 기억이나 로컬 메모는 연결을 돕는 힌트이며, 상태 원본은 Harness 한 곳입니다.

명령 전송은 한 번만 수행합니다. 재전송에는 **같은 request_id와 같은 입력**을 쓰고, 파일을
고친 뒤 새 제출에는 새 ID를 씁니다. `resume`은 조회일 뿐 작업을 자동 재개하지 않습니다.
`verify`는 비동기 job_id를 반환합니다. 선택적인 도우미 timeout은 응답 대기만 제한하며,
timeout을 검증 실패 또는 worker 종료로 해석하지 마세요.

## 4. 설치·운영 검증

```sh
python3 -m pip install -e '.[dev]'
python3 -m pytest -q tests/test_skill_workflow.py
BASE_HARNESS_EXTERNAL_LIVE=1 python3 -m pytest -q tests/test_skill_workflow.py -k live
python3 -m pytest -q tests
```

테스트는 설치본 이동, 중복 요청, 다른 Run 연결 거절, 새 프로세스에서 조회, 실패 후 수정·
재제출·재검증을 확인합니다. 실제 namespace 실행은 환경 권한이 필요한 별도 opt-in 검사입니다.
코드 테스트와 Skill을 읽은 모델의 실제 판단 평가는 구분합니다. 모델 품질·속도 비교 또는
같은 사용자 권한의 변조 방지 인증을 제공하지 않습니다.
