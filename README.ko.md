<p align="center">
  <img src="docs/assets/readme-img.png" alt="CONTINUUM 배너" width="100%" />
</p>

<p align="center">
  <strong>CONTINUUM: 장시간 실행되는 AI 에이전트를 위한 검증 가능한 의미론적 복구.</strong>
  시맨틱 체크포인트(대화 덤프가 아님), 중복된 사이드 이펙트를 거부하는 멱등한 액션 원장,
  그리고 해시 체인 기반의 변조 증거 로그를, 기본적으로 거부하는 MCP 서버로 노출한다. 프레임워크에 구애받지 않으며, Python 3.11+를 지원한다.
</p>

<p align="center">
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.11+" /></a>
  <a href="https://pypi.org/project/continuum-agent/"><img src="https://img.shields.io/pypi/v/continuum-agent?style=flat-square&label=PyPI" alt="PyPI" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache_2.0-blue?style=flat-square" alt="License" /></a>
  <a href="https://pydantic.dev"><img src="https://img.shields.io/badge/pydantic-v2-E92063?style=flat-square&logo=pydantic&logoColor=white" alt="Pydantic v2" /></a>
  <a href="https://continuum-nu-six.vercel.app/"><img src="https://img.shields.io/badge/website-live_demo-E06D53?style=flat-square" alt="Website Demo" /></a>
  <a href="https://github.com/Cyrax321/CONTINUUM/actions/workflows/ci.yml"><img src="https://github.com/Cyrax321/CONTINUUM/actions/workflows/ci.yml/badge.svg" alt="CI 상태" /></a>
  <a href="https://app.codecov.io/gh/Cyrax321/CONTINUUM"><img src="https://img.shields.io/codecov/c/github/Cyrax321/CONTINUUM?style=flat-square&logo=codecov" alt="Coverage" /></a>
</p>

<p align="center" style="margin-bottom: 6px;">
  <a href="https://continuum-nu-six.vercel.app/"><strong>CONTINUUM 웹사이트 방문</strong></a>
</p>

<p align="center" style="margin-top: 6px;">
  <a href="https://app.ona.com/#https://github.com/Cyrax321/CONTINUUM"><img src="https://ona.com/build-with-ona.svg" alt="Build with Ona" /></a>
</p>

<p align="center">
  <sub>CONTINUUM이 에이전트의 복구에 도움이 되었다면, 리포지토리에 스타를 눌러주세요. 더 많은 사람들이 발견하고 좋은 first issue가 계속 제공되는 데 도움이 됩니다.</sub>
</p>

<p align="center">
  <sub><a href="README.md">English</a> | <a href="README.zh-CN.md">简体中文</a> | <a href="README.es.md">Español</a> | <a href="README.ja.md">日本語</a> | <a href="README.pt-BR.md">Português</a> | <strong>한국어</strong></sub>
</p>

---

## 목차

[왜](#왜) · [빠른 시작](#빠른-시작) · [작동 방식](#작동-방식) · [CONTINUUM의 위치](#continuum의-위치) · [기능](#기능) · [보안 확장](#보안-확장) · [실증적 검증](#실증적-검증) · [MCP 통합](#mcp-통합) · [프레임워크 통합](#프레임워크-통합) · [핵심 개념](#핵심-개념) · [아키텍처](#아키텍처) · [API와 CLI](#api와-cli) · [로드맵](#로드맵) · [CONTINUUM이 아닌 것](#continuum이-아닌-것) · [관련 연구](#관련-연구) · [상태와 제한](#상태와-제한) · [기여](#기여) · [라이선스](#라이선스)

---

## 왜

현대 AI 에이전트는 긴 작업을 실행한다. 수백 번의 LLM 호출, 도구 호출, 파일 및 데이터베이스 쓰기가 포함된다. 충돌이 발생하면 일반적인 대응은 모든 것을 처음부터 다시 재생하는 것이며, 이는 작업을 중복시키고, 사이드 이펙트를 중복시키며, 토큰을 낭비하고, 결정을 잃게 만든다.

CONTINUUM은 더 좁고 더 어려운 질문을 던진다. 에이전트가 작업 상태의 컴팩트한 의미론적 표현으로부터 재개하면서, 그 상태가 현재 환경에서 여전히 유효한지 독립적으로 검증할 수 있는가. 그 차별화는 세 부분으로 이루어진다.

- **시맨틱 체크포인트**: 에이전트가 계속하는 데 필요한 컴팩트하고 버전 관리된 표현이며, 대화 덤프가 아니다.
- **독립적인 환경 재검증**: 각 체크포인트 구성 요소는 재개 전에 현재 환경에 대해 검증되며, 오래됨은 의존성 그래프를 통해 전파된다.
- **출처를 인식하는 상태**: 모든 사실은 그 기원을 추적하므로, 에이전트가 보고한 진행 상황이 스스로 인증되는 일은 결코 없다.

## 빠른 시작

PyPI에 `continuum-agent` 0.1.0으로 게시됨. `pip install continuum-agent` 실행 (`pip install continuum-agent==0.1.0`으로 고정). 릴리스 태그는 빌드된 wheel을 [GitHub Releases](https://github.com/Cyrax321/CONTINUUM/releases)에 첨부한다.

제로 설정 경로 (클론도, 설치도, 게시도 필요 없음):

| 경로 | 방법 |
|:--|:--|
| PyPI에서 설치 | `pip install continuum-agent==0.1.0` 후 `continuum --help` |
| 크래시 복구를 끝에서 끝까지 보기 | `docker run --rm ghcr.io/cyrax321/continuum` |
| Docker를 통해 CLI 사용 | `docker run --rm ghcr.io/cyrax321/continuum continuum --help` |
| 클론 없이 CLI 실행 | `uvx --from git+https://github.com/Cyrax321/CONTINUUM.git continuum --help` |
| Windows PowerShell (클론 내부) | `powershell -ExecutionPolicy Bypass -File .\try-it.ps1` 또는 `powershell -ExecutionPolicy Bypass -File .\try-it.ps1 cli --help` |
| 브라우저에서 완전한 개발 환경 | [![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/Cyrax321/CONTINUUM?quickstart=1) |

Docker 이미지는 CI가 `main`에 대한 각 push와 각 릴리스 태그마다 GHCR에 게시한다 (`.github/workflows/docker-publish.yml`). Codespace는 `.devcontainer/`에 정의되어 있다.

```bash
git clone https://github.com/Cyrax321/CONTINUUM.git
cd CONTINUUM

uv venv && source .venv/bin/activate     # macOS / Linux; Windows: .venv\Scripts\activate

# 기여자 (권장): 라이브러리 + CLI + 모든 테스트 도구 + 모든 어댑터
uv pip install -e ".[dev]"

# 또는 필요한 것만 선택: . (최소), [mcp], [otel], [langgraph],
# [openai], [langchain], [attest], [postgres]

# 또는 클론을 완전히 건너뛰기:
uv pip install git+https://github.com/Cyrax321/CONTINUUM.git
uv pip install "continuum-agent[mcp] @ git+https://github.com/Cyrax321/CONTINUUM.git"
```

> **pip 폴백:** 위의 모든 명령에서 `uv pip install`을 `pip install`로 교체하십시오.

검증:

```bash
continuum --help                 # CLI 진입점
continuum-mcp --help             # MCP 서버 진입점 ([mcp] 또는 [dev] 필요)
pytest -q                        # 약 1,380개 테스트 수집 (정확한 수와 스킵 수는 환경에 따라 다름)
ruff check src/ tests/ examples/ && ruff format --check src/ tests/ examples/
mypy src/continuum               # CI가 강제하는 세 가지 게이트
```

코어 라이브러리는 하나의 런타임 의존성(`pydantic>=2.7`)만 가지며, 나머지는 모두 선택 사항이다. 전체 패키지 맵, extras 행렬, Postgres 테스트 설정, 명령별 검증은 [references/install.md](references/install.md)에 있다.

### 코딩 에이전트를 2분 안에 연결

Claude Code, Gemini CLI 또는 Codex의 경우 Python을 작성할 필요도 없고 프롬프트 파일도 필요하지 않다.

```bash
continuum start my-task --goal "에이전트가 해야 할 일"
continuum hooks install claude-code --with-gate   # 동일하게: gemini, codex
```

그 이후 에이전트가 작성하는 모든 파일은 해시 체인 증거로 캡처되고, 세션 시작 시 자동으로 상태 브리핑이 제공되며, `.continuum/gate.json`에 등록된 청구되지 않은 사이드 이펙트는 실행 전에 거부되고, 어떤 충돌 후의 새로운 세션도 실행 가능한 다음 단계로 재개된다. CLAUDE.md는 필요하지 않다.

최소 라이브러리 예제, 기록과 복구:

```python
from continuum import EventType, Run, SQLiteStorage, project

store = SQLiteStorage("agent.db")
store.create_run(Run(run_id="run_4821", goal="10,000개 문서 분석"))
store.append_event("run_4821", EventType.RUN_STARTED, {"goal": "10,000개 문서 분석", "total": 10_000})

for i, doc in enumerate(documents):
    analyze(doc)
    store.append_event("run_4821", EventType.WORK_COMPLETED, {"doc": i})

# 충돌 후, 새로운 프로세스는 중단된 지점에서 정확히 재개한다:
state = project("run_4821", store.read_events("run_4821"))
print(state.progress.completed)            # 이미 완료, 반복하지 않음
print(store.verify_events("run_4821").ok)  # True, 충돌 후에도 체인은 온전함
```

**직접 증명을 실행:**

```bash
python examples/crash_recovery_agent.py   # 실제 프로세스 킬, 실제 사이드 이펙트
python examples/context_compaction.py     # 트랜스크립트 손실, 체크포인트는 생존
python examples/model_switch.py           # 모델 A 사망, 모델 B가 안전하게 인계
python scripts/mcp_smoke.py               # 실제 서브프로세스, 실제 JSON-RPC 트래픽
```

`e2e-autonomy-test/` 키트는 실제 인보이스 배치 작업, 실행 중 하드킬, 그리고 새로운 재개 세션을 스크립트화한 뒤, outbox, 원장, 이벤트 체인을 대역 외에서 채점한다. 실행 1은 실제 Claude Code 세션에서 **7/7 메커니즘**을 획득했다. 전체 워크스루는 [references/e2e.md](references/e2e.md)에 있다.

## 작동 방식

CONTINUUM은 **LLM 컨텍스트**(일시적)와 **지속적인 작업 상태**(영구적)를 분리한다. 대화 기록을 저장하는 대신, 계속하는 데 필요한 최소한의 검증된 정보인 시맨틱 체크포인트를 구축한다.

![CONTINUUM 작동 방식](docs/assets/architecture.svg)

자세한 설명, 프로젝션 모델, 복구 컨텍스트는 [references/architecture.md](references/architecture.md)에 있다.

## CONTINUUM의 위치

네 가지 관심사가 모든 장시간 실행 에이전트에서 겹친다. CONTINUUM은 마지막 하나만 소유하고, 다른 세 가지는 명시적인 심을 통해 건드린다. 경쟁자를 지명하지 않으며, 제공된 모듈이나 게시된 스위트가 이미 출력하지 않은 주장을 하지 않는다.

| 레이어 | 질문에 답함 | 연결 방법 (제공된 모듈 또는 게시된 출력) |
|:--|:--|:--|
| Harness | 에이전트는 도구를 어떻게 호출하고 목표를 향해 나아가는가 | CONTINUUM 외부. 연결 지점은 `src/continuum/adapters/generic.py`(`GenericAgentAdapter`), `src/continuum/adapters/thin.py`(CrewAI, AutoGen, Pydantic AI 훅), `src/continuum/mcp/server.py`(MCP stdio), `src/continuum/hooks.py`와 `src/continuum/clienthooks.py`(코딩 CLI 수명 주기 훅), `src/continuum/gateway.py`(모든 언어용 강제 HTTP 프록시), `src/continuum/otel.py`(OpenTelemetry 브리지)에서 제공된다. 레시피는 `docs/recipes/`와 `references/adapters.md`에 있다. |
| 내구성 있는 실행 | 충돌 전에 무슨 일이 일어났고, 무엇을 잃지 않고 재생할 수 있는가 | 해시 체인 이벤트 로그 `src/continuum/events.py`와 `verify()`와 `trusted_through`, 영속 저장소 `src/continuum/storage/sqlite.py`(WAL, `synchronous=FULL`, schema v6)와 `src/continuum/storage/postgres.py` plus `src/continuum/storage/migrations.py`, 정책 기반 체크포인트 `src/continuum/checkpoint/manager.py`와 `src/continuum/checkpoint/policy.py`가 `restore()`에서 간격을 재생한다. 워크스루는 `docs/recovery_walkthrough.md`(`examples/recovery_walkthrough.py`의 출력)에 있다. |
| 제어 평면 | 어떤 실행이 활성 상태이며, 누가 그것에 대해 행동할 수 있고, 출력은 어디로 가는가 | 실행 레지스트리와 부모-자식 계층 `src/continuum/storage/`와 `src/continuum/recovery/family.py`(`continuum tree`), allowlist 인가 `src/continuum/mcp/authz.py`(`CONTINUUM_MCP_MUTATING_CLIENTS` / `CONTINUUM_MCP_TOKEN`), 표현 표면 `src/continuum/dashboard/app.py`와 `src/continuum/serve/server.py`, CLI `src/continuum/cli/main.py`(`continuum runs`, `continuum tree`, `continuum health`). |
| 검증 기판 | 시간 T의 체크포인트와 지금의 세계가 주어졌을 때, 계속하는 것이 여전히 안전하고 정확한가 | `src/continuum/state/validator.py`(오래됨 `dependency -> evidence -> finding -> decision` plus `PlanStep.depends_on`), `src/continuum/provenance_map.py`(`Origin`에서 `REQUIRES_REVIEW`까지 `REVIEW_CONFIRMED`까지), `src/continuum/actions/ledger.py`와 `src/continuum/actions/idempotency.py` 및 `src/continuum/gate.py` / `src/continuum/gateway.py`(실행 전 청구, 중복 거부, 조정을 위해 `UnknownSideEffect` 발생), `src/continuum/replayguard.py`(휴대용 가드), `src/continuum/pinning.py`와 `src/continuum/replay_similarity.py`(재생 정확성), `src/continuum/budgets.py`(재시도 상한), `src/continuum/recovery/engine.py` + `src/continuum/recovery/contract.py` + `src/continuum/recovery/planner.py` + `src/continuum/recovery/observations.py`(최대 심각도 `RESUME < ... < ABORT`, `evidence` / `reason` / `next_allowed_action` / `human_steps`가 있는 봉인된 계약), `src/continuum/checkpoint/rewind.py`(원자적 이중 상태 되감기), `src/continuum/analysis/prefix_trust.py`(조언적 신뢰). 게시된 검사: `docs/recovery_walkthrough.md`, `benchmarks/fault_injection/`(`detection_rate` / `unsafe_resume_rate`를 출력하는 스위트), `src/continuum/benchmark/phase6/`(복구 정확성 스위트), `docs/RESULTS.md` 그리고 아래의 재생성 가능한 시각화. |

위의 각 행은 태그가 지정된 커밋 시점에 `main`에 존재하는 경로로 추적 가능하다. 이 표에서는 벤치마크 수치를 다시 게시하지 않는다. 벤치마크는 이미 출력한 스위트 출력에만 존재한다. 게시된 스위트와 설계 문서의 전체 목록은 `docs/research.md`에 있다.

### 크래시 복구, 실제로

아래 이미지는 목업이 아니다. `python demo-run/generate_crash_visual.py`의 출력이며, `demo-run/worker.py`를 문서 399에서 `os._exit(9)`까지 실행하고, `continuum resume --env dataset=v4`를 호출하여 거부 경로(`REQUEST_HUMAN`, `safe:false`, exit 20)를 보여주고, 불확실한 사이드 이펙트를 프로브로 조정하며, 동일한 데이터베이스에서 재개하여 중복 작업 없이 완료한다. 트랜스크립트는 감사를 위해 `docs/assets/crash-recovery.txt`로도 저장된다.

재생성:

```bash
python demo-run/generate_crash_visual.py
# 또는: python scripts/generate_crash_visual.py
```

![크래시 복구: 배치 중 하드 킬, 거부, 조정, 재개](docs/assets/crash-recovery.svg)

코드가 포함된 전체 워크스루는 `docs/recovery_walkthrough.md`(`examples/recovery_walkthrough.py`)에 있다. 최소 bench harness는 `references/bench.md`(`continuum benchmark`)에 있다.

## 기능

| 기능 | 얻게 되는 것 |
|:--|:--|
| 시맨틱 체크포인트 | 컴팩트하고 버전 관리되며 검사 가능한 상태, 트랜스크립트 덤프가 아님 |
| 멱등한 액션 원장 | 중복된 외부 사이드 이펙트를 거부하고, 불확실한 것은 조정을 위해 드러냄 |
| 환경 재검증 | 각 체크포인트 구성 요소는 재개 전에 현재 세계에 대해 검증됨 |
| 출처를 인식하는 상태 | 에이전트가 보고한 진행 상황은 `REQUIRES_REVIEW`로 표시되며 스스로 인증되지 않음 |
| 복구 엔진 | 결정적이고 봉인된 다음 액션 계약을 가진 일곱 가지 복구 모드 |
| 기본적으로 거부하는 MCP 서버 | 열한 개의 도구, 읽기/변경 분리, 호출자 allowlist |
| 프레임워크 어댑터 | 범용 Python, OpenAI Agents SDK, LangGraph, LangChain 통합 |
| 안전한 계획 루프 | 이중 신호 관측 검증이 고위험 분기를 REQUIRES_REVIEW로 승격 |
| 주기적 재검증 | 환경이 일정에 따라 다시 검사되어 실행 중 드리프트를 한 주기 내에 포착 |
| 변조 증거 로그 | 해시 체인 이벤트 로그(36가지 이벤트 타입)와 무결성 검증 |
| 강제 게이트 | 청구되지 않은 사이드 이펙트 호출은 실행 전에 거부되며, 거부 메시지가 청구 프로토콜을 가르침 |
| 관측 훅 | 코딩 CLI가 작성하는 모든 파일은 다이제스트 검증된 증거가 되며 모델 제어 밖에 있음 |
| 세션 브리핑 | 새로운 세션은 시작 시 실행 상태를 결정적으로 학습하며, 이전 세션의 추론 요약을 포함 |
| 조정 프로브 | 등록된 명령이 불확실한 사이드 이펙트를 자동으로 처리하고, 사람은 나머지ements만 봄 |
| 실행 가능한 가이던스 | Resume과 validate는 다음 단계를 실행 가능한 명령으로 렌더링하며 상태로 렌더링하지 않음 |
| 강제 HTTP 게이트웨이 | 모든 언어에서의 아웃바운드 호출은 청구를 요구하며, 응답은 실제 상태 코드로부터 정산됨 |
| OpenTelemetry 브리지 | 프로덕션 트레이싱의 도구 호출 스팬이 코드 변경 없이 증거가 됨 |
| 액션 인덱스 | 실행을 넘나드는 멱등성 조회는 인덱싱된 읽기이며 전체 로그 스캔이 아님 |
| 버전 고정 | 호출자가 주장한 prompt, 도구, 모델 해시가 청구마다 저장되며 드리프트는 재개 시 드러남 |
| 재시도 예산 | 액션 타입별 시도 상한이 청구 시 강제되며 에이전트는 남은 시도 횟수를 볼 수 있음 |
| 다중 에이전트 부모-자식 | 부모 재개는 가족의 최악 상태를 합성하며, 불확실한 자식이 부모를 차단함 |
| 정보가 있는 재시도 | 엔진이 작성한 실패 요약이 복구 후 재개에 주입됨 |
| 포크 의미론 | 발산하는 연속은 신선한 권한을 가진 자식 실행으로 분기됨 |
| 로그 압축 | 앵커 이전 접두사는 verbatim으로 아카이브되며 라이브 로그는 수개월 실행에도 제한됨 |
| 소모된 부여 추적 | 일회성 권한 참조는 종료 상태에서 소모된 것으로 표시되며 복원 후 재사용은 거부됨(`GRANT_DENIED`), 체크포인트 복원 경로에서 권한 부활을 방어 |
| 체인 증명 | `continuum attest`는 Ed25519로 실행의 체인 헤드에 서명하여 외부 검증자가 알려진 키로 기록이 변조되지 않았음을 증명할 수 있게 함 |
| HITL 대시보드 | 감사 패리티가 CLI와 동일한 확인, 조정, 완료 버튼 |

## 보안 확장

두 개의 가산적인 보안 확장이 복구와 체크포인트 기반 위에 자리한다. 그것들은 재개, 재생, 또는 기존의 크래시 시 재검증 경로를 변경하지 않는다.

- **안전한 계획 루프**: 관측은 출처를 가지고 두 개의 독립적인 신호로 검증된다(`verified` / `unverified` / `contested`). 검증되지 않거나 논쟁되는 관측에 의해 보호되는 계획 분기는 `REQUIRES_REVIEW`로 승격된다. 결정은 원장에 `PERCEPTION_OBSERVED`와 `BRANCH_RESOLVED` 이벤트로 추가된다.
- **주기적 재검증**: 복구 엔진을 단계 간격(기본값 25)과 앱 전환 시 재사용하므로, 실행 중 환경 드리프트가 다음 크래시까지 기다리지 않고 한 주기 내에 포착된다.

[docs/PROBLEM.md](docs/PROBLEM.md), [docs/RESULTS.md](docs/RESULTS.md) 및 [STATUS.md](STATUS.md)를 참조하라.

## 실증적 검증

CONTINUUM은 목업 단위 테스트뿐만 아니라 실제 LLM 에이전트, 라이브 프로토콜 경계, 하드 프로세스 크래시에 대해 검증된다.

- **실제 에이전트**: 실행 중 `SIGKILL`된 Claude Code에 의한 다중 세션 인보이스 배치. 메커니즘에서 7/7을 획득. 재개 세션은 `continuum_resume`을 조회하고, 2단계 원장으로 사이드 이펙트를 라우팅하며, 검증된 쓰기의 중복을 거부하고, `request_human`을 존중했다. 라이브 테스트는 프롬프트 드리프트 중복排除 갭을 드러냈고, `ActionLedger.claim()`에서 정규 경로 정규화와 토큰 기반 폴백으로 닫혔다.
- **서드파티 클라이언트**: Gemini CLI와 Kilo Code가 stdio JSON-RPC로 라이브 SQLite 저장소에 연결되어 다중 에이전트 공존과 인가 분리를 검증.
- **프로토콜 준수**: `@modelcontextprotocol/inspector --cli`로 프로세스 죽음을 가로질러 엔드투엔드로 구동. 변경 도구는 기본적으로 `CONTINUUM_MCP_MUTATING_CLIENTS` 뒤에서 거부되며, 외부 클레임은 `REQUIRES_REVIEW`(`safe: false`)로 강등된다.
- **자기 치유**: 하드킬된 서버는 시작 시 한 번의 재시도로 고립된 SQLite `-wal`/`-shm` 사이드카를 정리하여 복구한다.
- **규모**: 약 1,380개 테스트가 수집됨(약 1,360개 통과, 나머지는 선택적 서비스 없이 스킵), Python 3.11, 3.12, 3.13에서 실행(unit, `hypothesis` 기반 속성 테스트, 동시성, 적대적). CONTINUUM-Bench는 다섯 개의 크래시 시나리오에 전용 argument-drift 시나리오를 더해 실행하며, CONTINUUM에 대해 0 중복 작업과 0 중복 사이드 이펙트를, 단순 재생에 대해 완전한 중복을 측정한다. 추가로 12 시나리오 복구 정확성 스위트(`continuum.benchmark.phase6`)가 내구성 실행 서베이의 크래시 지점을 실행 가능한 어설션으로 인코딩한다.
- **적대적 감사**: 전체 MCP 표면이 라이브 프로토콜 위에서 감사되었고, 세 가지 결함이 발견되어 수정되었다. 방법과 재현 단계는 [test.md](test.md)에 있다.

## MCP 통합

CONTINUUM은 MCP 서버를 제공한다. 에이전트는 라이브러리를 임베드하지 않고도 진행 상황을 기록하고, 체크포인트를 만들고, 사이드 이펙트를 원장을 통해 라우팅할 수 있다.

```bash
uv pip install -e ".[mcp]"
CONTINUUM_MCP_MUTATING_CLIENTS=your-client-name continuum-mcp
```

stdio를 통한 열한 개의 도구. 세 개는 읽기 전용(`continuum_validate`, `continuum_resume`, `continuum_list_actions`), 여덟 개는 변경한다. 사이드 이펙트는 2단계(청구, 실행, 완료)이며, 변경 도구는 기본적으로 allowlist 뒤에서 거부된다. 에이전트가 보고한 상태는 출처 `Origin.EXTERNAL_AGENT`로 기록되고 `REQUIRES_REVIEW`로 표시된다.

검증 세부사항(시작 시 크래시 복구와 Claude Code를 통한 엔드투엔드 테스트 포함)은 [references/mcp.md](references/mcp.md)에 있다. 등록된 서버가 `CONNECTION_CLOSED`를 보고하면, 원인은 거의 항상 `PATH` 해결이며 서버 자체가 아니다. [docs/api/mcp.md](docs/api/mcp.md#troubleshooting)에 진단과 두 가지 수정책이 있다.

## 프레임워크 통합

아홉 개의 어댑터가 `src/continuum/adapters/`에 제공된다(하나의 인프로세스 퍼사드 plus 여덟 개의 통합). 모두 선택적 설치이므로 코어는 표준 라이브러리만 유지한다.

| 어댑터 | 클래스 | 비고 |
|:--|:--|:--|
| 범용 Python 에이전트 | `GenericAgentAdapter` | 인프로세스 퍼사드. 신뢰할 수 있는(`Origin.DETERMINISTIC`) 상태를 쓴다. |
| 파일시스템 샌드박스 | `FilesystemSandboxAdapter` | 로컬 디렉터리 샌드박스. 외부 서비스 없음. 문서와 CI의 기본값. |
| Python 인프로세스 | `PythonInProcAdapter` | 임시 작업 디렉터리에서 Python을 실행하고 원장을 통해 기록한다. |
| 컨테이너 | `ContainerAdapter` | Docker 기반. `docker`가 없을 때 보호된 스킵. |
| 브라우저 | `BrowserAdapter` | Playwright 기반. 설치되지 않았을 때 보호된 스킵. |
| Kubernetes | `KubernetesAdapter` | `kubectl` 기반. 구성되지 않았을 때 보호된 스킵. |
| OpenAI Agents SDK | `OpenAIAgentAdapter` | 실험적. `ToolContext` / `RunHooks`에 후크. 선택적 `openai-agents`. |
| LangGraph | `LangGraphAgentAdapter` | 실험적. `StateGraph`를 래핑. 선택적 `langgraph`. |
| LangChain | `LangChainAgentAdapter` | 실험적. LCEL `Runnable` 파이프라인과 `create_agent` 도구 호출 루프에 `checkpoint_node`를 드롭. 선택적 `langchain`. |

각 어댑터는 원장을 통해 진행 상황을 기록하고 2단계 인터셉트와 완료 프로토콜로 외부 효과를 라우팅한다. 세 개의 프레임워크 어댑터는 모두 엔드투엔드 통합 테스트를 가지고 있으며, **라이브 OpenRouter 모델**에 대해 구동되었고, 그 실행에서 LLM 인자 드리프트 중복排除 갭과 두 개의 OpenAI 어댑터 결함(어댑터별 라이브 하드 크래시(사이드 이펙트 중 `os._exit(137)`) 증명 포함)이 드러나고 닫혔다. 전체 사용법, 라이브 모델 결과, 실행 가능한 예제는 [references/adapters.md](references/adapters.md)에 있다.

프로덕션 LangGraph 앱은 네이티브 영속성 API를 유지할 수도 있다. `make_continuum_checkpointer(storage)`는 LangGraph의 `BaseCheckpointSaver`를 CONTINUUM 저장소 위에서 구현하므로, 각 put은 동일한 해시 체인 출처 태그된 이벤트 로그에 착륙한다([references/adapters.md](references/adapters.md) 참조).

추가로 세 개의 프로덕션 프레임워크가 [`adapters/thin.py`](src/continuum/adapters/thin.py)의 SDK가 필요 없는 얇은 훅 표면으로 커버된다.

| 프레임워크 | 인터셉트 표면 | 진입점 |
|:--|:--|:--|
| CrewAI | 전역 도구 호출 전후 훅 | `install_crewai_hooks(storage, run_id)` |
| AutoGen core | `FunctionTool.run_json`을 현장에서 래핑 | `wrap_autogen_tool(tool, storage, run_id)` |
| Pydantic AI | 비동기 Hooks 역량 | `Agent(capabilities=[wrap_pydantic_ai_hooks(storage, run_id)])` |

이들 중 어느 것에도 닿지 않는 스택의 경우: `continuum gateway`는 모든 언어로부터의 아웃바운드 HTTP에 청구를 강제하고, `continuum.otel.make_span_processor(storage)`는 기존 OpenTelemetry 도구 스팬을 증거로 바꾸며, `continuum serve`는 MCP 도구와 동일한 작업을 언어에 구애받지 않는 JSON 와이어 프로토콜로 노출한다(stdio, 또는 `--transport http`를 통한 HTTP와 `CONTINUUM_SERVE_TOKEN` 인증).

### 에이전트 또는 MCP가 보고한 실행 재개

MCP를 통해, 또는 OpenAI 어댑터를 통해 보고된 상태는 출처 `Origin.EXTERNAL_AGENT`를 가지며 확인될 때까지 `request_human`으로 해결된다. LangGraph와 LangChain 실행은 `Origin.DETERMINISTIC`을 사용하고 직접 재개한다. 검토를 지우고 재개하려면:

```bash
continuum confirm <run_id>   # REVIEW_CONFIRMED를 기록하고 다시 평가
continuum resume <run_id>    # 이제 RESUME을 보고함
```

MCP에서는 동등한 것이 `continuum_confirm` 도구 뒤의 `continuum_resume`이다. 확인은 일회성의 인간 증명 이벤트이며, 자체 인증 안전성의 탈출구이므로 외부에서 구동된 실행이 영구적으로 막히는 일은 없다.

## 핵심 개념

각 개념의 깊은 레퍼런스는 [references/concepts.md](references/concepts.md)에 있다.

- **시맨틱 체크포인트**, 에이전트가 계속하는 데 필요한 컴팩트하고 버전 관리된 표현.
- **상태 검증**, 각 구성 요소가 독립적으로 검증되며 오래됨은 의존성 그래프를 통해 전파된다.
- **멱등한 액션 원장**, 외부 사이드 이펙트가 추적되고 중복排除되며, 불확실한 결과는 조용히 재시도되는 대신 발생한다.
- **복구 모드**, `RESUME`, `REPAIR_AND_RESUME`, `ROLLBACK`, `WAIT`, `REQUEST_HUMAN`, `ABORT`(plus `REPLAN`).
- **복구 계약**, 결정적이고 무결성이 봉인되며 게이트된 다음 액션.

## 아키텍처

CONTINUUM은 하나의 불변식을 중심으로 구성된다. **모든 사실은 그 기원을 가지고 있으며, 신뢰는 얻어지는 것이지 결코 가정되지 않는다.** 이것이 스타트업에게 중요한 이유: 수 주간 실행되는 에이전트는 컨텍스트가 손실되었을 때 작업을 잃어서는 안 되며, 토큰이나 비용을 낭비하거나 동일한 도구를 두 번 실행해서는 안 된다.

### 시스템을 한눈에, 범용 어댑터, 단일 로그, 모든 하네스

모든 하네스가 동일한 해시 체인 로그에 연결된다. 동일한 실행을 Claude Code가 작성하고, LangGraph가 재개하며, CLI가 검사하고, 대시보드에서 승인할 수 있다. 프레임워크 협력이 필요하지 않다.

```text
  Claude Code ─┐
  Gemini CLI ──┤
  Codex ───────┤
  LangGraph ───┼── 5개 심 ──►  단일 내구성 로그  ──►  복구 + 대시보드 + CLI
  LangChain ───┤                (해시 체인,        (봉인된 계약,
  OpenAI SDK ──┤                 출처 태그,          검증, 상태,
  CrewAI ──────┤                 엄격히 한 번)        가족)
  모든 HTTP ──┤
  모든 OTel 앱┘

  심: 1 인프로세스  2 MCP  3 CLI 훅  4 게이트웨이  5 OTel
```

### 세 가지 보장 (데모가 각각을 증명한다)

1. **자기 인증 없음.** 에이전트가 보고한 상태는 `EXTERNAL_AGENT`이며 인간의 `REVIEW_CONFIRMED`까지 `REQUIRES_REVIEW`로 강등된다. 신뢰할 수 있는 작성자만이 `DETERMINISTIC` 상태를 생성한다.
2. **사이드 이펙트는 청구를 요구한다.** 모든 외부 효과는 실행 전에 멱등한 원장에서 청구된다. 청구되지 않은 효과는 경계에서 차단되고, 중복은 거부되며, 불확실한 결과는 조정을 위해 발생한다.
3. **복구는 현실에 대해 검증한다.** 재개는 안전하다고 말하기 전에 파일 다이제스트, 의존성 버전, 모델 동일성을 확인한다. 오래됨은 `dependency -> evidence -> finding -> decision` plus `PlanStep.depends_on`으로 전파되므로 영향받은 단계만 수리된다.

### 다섯 가지 통합 심

| 심 | 연결 방법 | 얻게 되는 것 |
|:--|:--|:--|
| 1 인프로세스 | `GenericAgentAdapter.intercept_action(...)`와 `wrap_tool(key_fn=...)`(LangChain, LangGraph, OpenAI Agents SDK용) | Python 프레임워크, 신뢰할 수 있는 쓰기 |
| 2 MCP 서버 | `continuum-mcp` 12개 도구를 stdio를 통해(`continuum_record_progress`, `continuum_intercept_action`, `continuum_complete_action` 등) | 모든 MCP 대응 클라이언트, 3 읽기 전용 + 8 변경, allowlist `CONTINUUM_MCP_MUTATING_CLIENTS` |
| 3 CLI 수명 주기 훅 | `continuum hooks install claude-code --with-gate` (`gemini`와 `codex`도) | 코딩 CLI: `SessionStart briefing`, `PostToolUse observe`, `PreToolUse gate`, CLAUDE.md 불필요 |
| 4 강제 HTTP 게이트웨이 | `continuum gateway --port 8765`와 `.continuum/gateway.json` | 모든 언어, 모든 아웃바운드 HTTP는 청구를 요구하며 게이트웨이는 실제 상태 코드로부터 정산 |
| 5 OpenTelemetry 브리지 | `make_span_processor(storage)` | 모든 트레이싱된 앱, 스팬이 `TOOL_COMPLETED` 증거가 됨 |

CrewAI, AutoGen, Pydantic AI용 얇은 훅 표면은 SDK 없이 `adapters/thin.py`에 존재한다.

### 강제 파이프라인, 왜 중복도 없고 잘못된 호출도 없는가

게이트에서 관측으로의 파이프라인이 하네스 경계의 틈을 닫는다. 이것이 토큰과 비용을 절약하고 잘못된 도구 호출을 차단하는 것이다.

```text
PreToolUse 훅                    PostToolUse 훅
    |                                    |
    v                                    v
continuum gate                    continuum observe
    |                                    |
    |-- 청구 없음? 거부 (exit 2)          |-- TOOL_COMPLETED 이벤트:
    |   + 청구 지침                      |     경로, 바이트, 현재 디스크의 sha256
    |                                    |
    |-- 유효한 청구 있음? 허용                |-- 디스크 검증 상태:
    |                                    |     검증됨 / 변경됨 / 누락
    v
에이전트가 효과 실행
    |
    v
continuum_complete_action  (현실에서 정산, 보고에서가 아님)
    |
    v
원장은 COMPLETED로 표시되고, 다음 재생은 두 번째 발화가 아닌 캐시된 결과를 반환
```

알 수 없는 호스트는 실패 폐쇄로 거부되며, 개방된 릴레이가 아니다. Shell `Bash/curl`은 문서화된 v1의 사각지대이다.

### 복구 결정 트리, 수주가 끝날 때까지, 정확하고 엄격하게

엔진은 가장 신중한 신호를 채택하므로 안전이 편의에 지는 일은 결코 없다.

```text
RESUME < REPAIR_AND_RESUME < REPLAN < WAIT < REQUEST_HUMAN < ROLLBACK < ABORT
```

각 `continuum resume`은 봉인된 계약을 반환한다. 내용은 복구 상태와 `safe`, 검증된 것과 무효화된 구성 요소, 실행 가능한 `human_steps`(실행해야 할 정확한 shell), 체크포인트 이후 관측의 디스크 검증, 핀 고정 드리프트, 그리고 다중 에이전트 `continuum tree`를 위한 가족 집계이다. 브리핑 `continuum briefing`은 신선한 `claude` SessionStart마다 그 계약을 주입한다. 따라서 터미널을 kill한 뒤 `hi`라고 말해도 마지막 좋은 프리픽스부터 재개한다.

### 왜 이것이 토큰, 비용, 잘못된 호출을 절약하는가

* **토큰:** 시맨틱 체크포인트는 `Goal + Plan + Progress`를 저장하며 트랜스크립트 덤프가 아니다. 브리핑은 검증된 상태에 더해 상한 4096의 추론 요약만을 제공하며, 다음 세션을 저하시키는 것으로 나타난 오류 꼬리를 전달하지 않는다. 정보가 있는 재시도 `recovery/summary.py`는 원시 기록이 아닌 엔진이 작성한 요약을 주입한다.
* **비용:** 원장 `action_index`는 상대 경로와 절대 경로 같은 인자 드리프트가 있어도 중복된 사이드 이펙트를 거부한다(`invoice:INV-001` 안정적인 키). 따라서 동일한 API가 재개 후 두 번 결제되는 일이 없다. 예산 `budgets.py`는 청구 시 재시도 폭풍을 상한한다. `continuum benchmark`는 continuum에 대해 `0 중복`, 단순한 것에 대해 `50`으로 출력한다.
* **잘못된 호출:** 게이트, 게이트웨이, `replayguard`의 `langgraph_protected_node`는 청구되지 않거나 재생된 도구 호출을 실행 전에 차단한다. 고정 `pinning.py`는 재개 시 prompt나 도구 드리프트를 드러낸다.

### 저장소 아키텍처

스키마 v6. SQLite가 기본, Postgres는 CI에서 검증됨. 하나의 로그, 많은 투영.

| 테이블 | 목적 |
|:--|:--|
| `events` | 해시 체인 추적 전용 로그(v0.2에서 44가지 이벤트 타입) |
| `runs` | 부모-자식을 위한 `parent_run_id`를 가진 실행 메타데이터 |
| `versions` | 체크포인트별 SemanticState 스냅샷 |
| `checkpoints` | `RECOVERY` 앵커를 가진 봉인된 체크포인트 기록 |
| `action_index` | 실행을 넘나드는 멱등성 투영(schema v3+), 인덱싱된 읽기이며 전체 스캔이 아님 |
| `events_archive` | 압축된 접두사 저장소(schema v5+), `continuum compact`가 수개월 실행을 위해 라이브 로그를 제한 |
| `lg_checkpoints` / `lg_writes` | LangGraph 네이티브 영속성(schema v4+), `make_continuum_checkpointer(storage)` |

### 모듈 맵, 하나의 라이브러리, 많은 표면

CONTINUUM은 하나의 라이브러리(`src/continuum`, 104 모듈) plus 대규모 테스트 스위트(98 테스트 파일, 약 1,380 테스트)이다. 모든 모듈은 하나의 해시 체인 이벤트 로그에 추가하고 재생한다.

| 모듈 | 역할 |
|:--|:--|
| `events.py` | 추적 전용이며 해시 체인인 이벤트 로그와 `verify() trusted_through` |
| `state/` | 투영 `project()`, 검증, 추출, 오래됨 전파 |
| `storage/` | `SQLiteStorage` v6, `postgres.py`, `migrations.py`, `actionindex.py` |
| `actions/` | 멱등한 원장 `claim/complete/reconcile`, `idempotency.py` 키와 정규화와 토큰 폴백, 소모된 부여 추적 `GRANT_DENIED` |
| `checkpoint/` | 정책 기반 체크포인트 `manager.py` `policy.py`와 `RECOVERY` 앵커와 `prune` |
| `recovery/` | 엔진, 플래너, 봉인된 계약 `contract.py`, `guidance` `human_steps`, `observations` 디스크 검증, `family` 롤업, `fork` 의미론, `summary` 정보가 있는 재시도 |
| `gate.py` | 도구 전 강제: 원장 청구에 대한 허용 또는 거부 |
| `gateway.py` | 강제 HTTP 프록시: 아웃바운드 요청을 위해 실행 전 청구 |
| `replayguard.py` | 휴대용 가드: `evaluate, protected_call, langgraph_protected_node`, ACRFence 재생 위험을 닫음 |
| `hooks.py` `clienthooks.py` | 공유 체크포인트 훅과 인스톨러 프로필 `claude-code gemini codex` |
| `budgets.py` | 액션 타입별 재시도 예산 레지스트리와 평가 |
| `pinning.py` | 재개 시 버전 고정 정규화와 드리프트 감지 |
| `replay_similarity.py` | 재생과 포크를 위한 의미적 유사성 백엔드 exact/fuzzy/embedding |
| `reconcilers.py` | 자동 정산을 위한 프로브 레지스트리 `.continuum/reconcilers.json` |
| `adapters/` | 9개 클래스 어댑터 + 얇은 훅 `thin.py` CrewAI AutoGen Pydantic AI + LangGraph 저장소 |
| `mcp/` | 12개 stdio 도구 plus 인가 `authz.py` 토큰 인증, allowlist, 확인 토큰 |
| `serve/` | Sidecar stdio JSON 와이어 + HTTP `CONTINUUM_SERVE_TOKEN` |
| `dashboard/` | 웹 대시보드 `app.py` `hitl.py`와 HITL 버튼 확인, 조정, 완료, 접두사 신뢰 조언, 고정 |
| `cli/` | 38개 argparse 명령, 종료 코드가 평결, `runs, start, inspect, resume, verify, health, tree, benchmark, attest, dashboard` |
| `otel.py` | OpenTelemetry 스팬 프로세서 브리지 |
| `benchmark/` | CONTINUUM-Bench 하네스, 5개 크래시 시나리오 + 인자 드리프트 + 12 시나리오 복구 스위트 |

### 정직한 제한

- 게이트는 셸 명령의 내부를 볼 수 없다(Bash나 curl은 구조화된 도구 청구를 우회한다)
- Postgres 백엔드는 CI에서 테스트되지만 프로덕션에서 단련되지 않았다
- `request_human` 알림을 위한 웹훅 외부는 아직 없다(#305)
- v1에서는 한 계층의 다중 에이전트 계층만
- 큰 페이로드 오프로딩(#254)은 아직 미구현
- 수 주 규모의 벤치마크와 토큰 비용 표는 보드 #550(#568에서 #570)에 착륙한다

완전한 레퍼런스는 [references/architecture.md](references/architecture.md)에 있다. 그리고 이 위에 구축되는 수개월 평면, 출처 인과 그래프, 권한 부활, 허용 가능성, 활성성은 보드 #550와 그 20개 하위 이슈 #551부터 #570으로 고정되어 있다.

## API와 CLI

Python 표면(`EventType`, `Run`, `SQLiteStorage`, `diff_states`, `project`)과 어댑터 API는 실행 가능한 예제와 함께 [references/api.md](references/api.md)에 문서화되어 있다. CLI는 동일한 표면을 셸 형태로 제공한다.

```bash
continuum runs                                   # 실행 목록
continuum inspect <run_id>                       # 의미 상태
continuum validate <run_id> --env dataset=v4     # 검증, 읽기 전용
continuum resume <run_id> --env dataset=v4       # 복구 결정 + 계약 + 다음 단계
continuum checkpoint <run_id>                    # 체크포인트 강제, 변경함
continuum actions <run_id>                       # 외부 사이드 이펙트
continuum reconcile <run_id>                     # 프로브로 불확실한 효과 정산
continuum complete <run_id>                      # 키보드에서 실행을 완료로 닫기
continuum verify <run_id>                        # 이벤트 해시 체인 재감사
continuum budget <run_id>                        # 액션 타입별 재시도 예산 사용량
continuum compact <run_id>                       # 앵커 이전 로그 접두사 아카이브
continuum tree <parent_run_id>                   # 부모 + 자식과 복구 상태 표시
continuum attest <run_id> --key signer.pem       # 외부 검증자를 위해 체인 헤드에 서명
```

모든 배선은 호스트 측이며, 모델의 협력은 선택 사항이다.

```bash
continuum hooks install claude-code --with-gate   # 코딩 CLI: 증거, 브리핑, 게이트
continuum gateway --port 8765                     # 다른 모든 것을 위한 강제 HTTP 프록시
provider.add_span_processor(continuum.otel.make_span_processor(storage))  # OTel을 증거로
continuum-mcp                                     # MCP가 가능한 모든 것: 열한 개 도구 서버
continuum briefing                                # 세션 시작 컨텍스트 주입
continuum budget <run_id>                        # 재시도 예산 사용량 보고서
continuum tree <parent_run_id>                   # 다중 에이전트 계층 보기
```

선택적 레지스트리는 코드 옆에 존재하며 데이터이지 코드가 아니다. `.continuum/gate.json`(사이드 이펙트 도구 + 안정적인 키 템플릿), `.continuum/reconcilers.json`(외부 시스템을 확인하는 프로브), `.continuum/gateway.json`(업스트림 라우트).

각 명령은 `--json`을 받으며, 읽기 전용 명령은 절대 쓰지 않는다. 따라서 에이전트가 실행 중에도 라이브 데이터베이스에 대해 안전하다. 종료 코드는 안전성 계약이다(검증되어 안전한 실행만 0으로 종료한다). 전체 명령 목록, 종료 코드 표, 상태 차이 출력은 [references/cli.md](references/cli.md)에 있다.

## 로드맵

| 단계 | 컴포넌트 | 상태 |
|:--:|:--|:--|
| 1-11 | 데이터 모델, 의미 상태, 영속성, 체크포인팅, 검증, 액션 원장, 복구 엔진, CLI, 크래시 복구 예제, 환경 스냅샷과 차이, 프레임워크 어댑터 | 완료 |
| 12 | 벤치마크 스위트(CONTINUUM-Bench) | 완료(최소 하네스) |
| 13 | 클라우드 API(FastAPI + PostgreSQL) | 부분적: PostgreSQL 저장소 백엔드와 HTTP sidecar 전송(`continuum serve --transport http`)은 제공되고 CI 테스트됨, 호스팅된 멀티 테넌트 서비스는 미시작 |
| 14 | 대시보드 | 완료(`continuum dashboard`) |
| 15+ | 강제된 내구성: 관측 훅, 게이트, 세션 브리핑, 조정 프로브, 강제 게이트웨이, OTel 브리지, 액션 인덱스, 실행 가능한 가이던스, 멀티 클라이언트 인스톨러, 의미적 재생 감지, 버전 고정, 재시도 예산, 로그 압축, HITL 표면, 포크 의미론, 정보가 있는 재시도, 다중 에이전트 집계 | 완료(issue #213 참조) |
| 다음 | 수개월 규모의 내구성 평면: 마일스톤에 고정된 계획(#312), 구조화된 시도 기억(#313), 원자적 이중 상태 되감기(#292), 공개된 복구 정확성 벤치마크(#293), webhook 외부 알림(#305) | 계획 중(초안 사양은 [docs/UPGRADE_SPEC.md](docs/UPGRADE_SPEC.md)에 있음) |

원래 계획 beyond: MCP 서버, MCP 인가와 호출자 인증 레이어, 출처와 반자기 인증, 커뮤니티 파일, 포워드 마이그레이션을 동반한 스키마 버저닝, 제한된 복구 컨텍스트, 소모된 부여 추적, Ed25519 이벤트 체인 증명, 네이티브 LangGraph 체크포인터, 그리고 `main`에 대한 각 push마다의 wheel 아티팩트가 제공된다. [STATUS.md](STATUS.md)에서 검증된 것과 믿어지는 것의 내역과 열린 정확성 버그를 참조하라.

## CONTINUUM이 아닌 것

| 이것이 아니다 | 오히려 이것이다 |
|:--|:--|
| LLM | LLM을 사용하는 에이전트를 위한 신뢰성 레이어 |
| 에이전트 프레임워크 | 모든 프레임워크에 꽂을 수 있는 복구 레이어 |
| 벡터 데이터베이스 | 임베딩이 아닌 구조화된 의미 상태 |
| RAG 시스템 | 검증된 체크포인트이지, 검색 증강 메모리가 아니다 |
| 워크플로우 엔진 | 복구 레이어이지, 오케스트레이터가 아니다 |

핵심 추상화: `의미 상태 + 환경 검증 + 액션 조정 = 안전한 복구`.

## 관련 연구

CONTINUUM은 내구성 있는 실행, 멱등한 사이드 이펙트 추적, LLM 에이전트를 위한 크래시 복구의 교차점에 위치한다. 가장 가까운 이웃은 기계 검증된 재개 계약(Khan 2026), 제약으로 보호된 입장을 동반한 에이전트 트랜잭션 처리(Mnemosyne 2026), 체크포인트 롤백 공격 분석(ACRFence 2026)과 설계 수준의 프롬프트 인젝션 방어(CaMeL 2025)이다. 완전한 주석이 달린 목록, 기초, 인용 감사는 [references/related-work.md](references/related-work.md)에 있다.

## 상태와 제한

- **테스트됨**: 이 트리의 2026-08-24 감사에서 완전한 실행으로 1,360 통과 + 23 스킵. CI는 Python 3.11, 3.12, 3.13에서 스위트를 강제하며, 카운트는 플랫폼과 Postgres 같은 선택적 서비스에 따라 다르다([STATUS.md](STATUS.md) 참조). MCP 표면도 라이브 프로토콜 위에서 적대적으로 감사되었다. [test.md](test.md) 참조.
- **PyPI에서 `continuum-agent` 0.1.0**(`pip install continuum-agent`, 클론은 `pip install .`로 여전히 동작. 빠른 시작 참조).
- **MCP 호출자 인증은 배포별로 선택 사항.** `CONTINUUM_MCP_TOKEN`이 설정되면, 서버는 호출자가 `initialize` 핸드셰이크의 `_meta.authToken`에서 그 공유 비밀을 제시하지 않는 한 모든 변경 도구를 거부한다. 호출자별 비밀은 `CONTINUUM_MCP_CLIENT_TOKENS`(`name:secret` 쌍)를 통해 이용 가능하다. 토큰이 아무것도 설정되지 않으면, 인가는 선언된 아이덴티티のみ에 의한다(역사적 기본값, 로컬 단일 사용자 사용을 위해 유지).
- **MCP를 통해 자체 보고된 상태를 확인하려면 별도의 비밀이 필요하다.** `continuum_confirm`은 운영자가 `CONTINUUM_MCP_CONFIRM_TOKEN`을 설정할 때까지 모든 호출자를 거부한다. 진행 상황을 기록하도록 허용된 에이전트가 그것을 확인하는 것도 허용되어서는 안 되기 때문이다. 기본 경로는 인간이 이끄는 채로 유지된다. 호스트에서 `continuum confirm <run_id>`를 실행하라.
- **구축되지 않은 컴포넌트**: 클라우드 API(단계 13).
- **셸 명령 강제 간격**: 게이트는 구조화된 도구 호출에 대해 클레임을 강제하지만 Bash나 curl 명령 내부를 볼 수 없다. v1 범위의 거부로 문서화됨.
- **프레임워크 어댑터는 여전히 실험적.** 세 개의 프레임워크 어댑터는 모두 라이브 모델에서의 소프트 재개와 하드 크래시 증명(OpenRouter, `gpt-4o-mini`)을 가지고 있으며, 불확실한 사이드 이펙트 위에서의 재개를 차단하는 크래시 계약을 포함하고, 범용 퍼사드와 동등한 크래시와 재개 검증 테스트를 가지게 되었다(Refs #285). 프로덕션 복구에는 `GenericAgentAdapter`를 우선하라.
- **에이전트와 MCP 실행은 자동 재개 전에 명시적 확인을 필요로 한다.** 외부에서 보고된 상태는 `REQUIRES_REVIEW`이므로, `continuum resume`은 인간이 확인할 때까지 `request_human`을 반환한다. 설계에 의한 것이며 결함이 아니다. [프레임워크 통합](#프레임워크-통합) 참조.
- **e2e 자율 테스트 시리즈**(issue [#6](https://github.com/Cyrax321/CONTINUUM/issues/6)): 세 번의 완전한 Claude Code 실행이 메커니즘에서 7/7을 획득했으며, 프롬프트 없는 복구 동작이 관찰되었다. 다양한 프롬프트 스타일에 걸친 추가 반복은 여전히 열려 있다.

## 에 대하여

2026년 초, 장시간 실행되는 에이전트가 추론이 아니라 복구에서 실패하는 것을 보았다. 체크포인트는 검증해야 할 증거가 아니라 계속하기 위한 증명으로 취급되고 있었다. Temporal, LangGraph, ACRFence 2603.20625, self conditioning 2509.09677을 조사한 결과, 간극이 이식 가능한 검증 기판임을 발견했다. 그것은, 시간 T의 상태와 지금의 세계가 주어졌을 때, 계속하는 것이 여전히 안전한지를 묻는 것이다.

3주 만에 나는 하나의 불변식으로부터 CONTINUUM을 구축했다. 모든 사실은 그 기원을 가진다. 결과는 `verify()`를 가진 해시 체인 로그, 안정적인 키 중복排除를 가진 원장, 청구되지 않은 효과를 차단하는 게이트와 게이트웨이, 그리고 계약을 봉인하는 복구 엔진이다. 다섯 개의 심이 동일한 로그를 Claude Code, LangGraph, LangChain, OpenAI, HTTP, OpenTelemetry에 노출한다. 실제 킬과 1380개의 테스트로 검증되었으며, 단순한 재생이 `50`으로 출력하는 곳에서 `0 중복`으로 출력한다.

CONTINUUM은 **Anandhu P Shaji**([@Cyrax321](https://github.com/Cyrax321) · [LinkedIn](https://www.linkedin.com/in/anandhupshaji/))에 의해 생성되었고 원저자에 의해 유지된다. 오픈 소스이며 [Apache-2.0](LICENSE) 하에 있다. 커뮤니티 기여는 [CONTRIBUTING.md](CONTRIBUTING.md)를 통해 환영되며, [AUTHORS.md](AUTHORS.md)와 [graphs/contributors](https://github.com/Cyrax321/CONTINUUM/graphs/contributors)에서 크레딧을 받는다.

## 기여

이 프로젝트는 Apache 2.0 하에 오픈 소스이며 의도적으로 확장 가능하게 구축되었다. 복구 시맨틱을 검증하는 연구자, 원장이나 MCP 서버를 다른 프레임워크나 언어로 포팅하는 엔지니어, 계획된 로드맵을 현실로 만드는 모든 사람을 위해 확장 가능하다. 좋은 출발점은 [issue 트래커](https://github.com/Cyrax321/CONTINUUM/issues)의 `good first issue` 레이블, 또는 STATUS.md에 나열된 열린 정확성 버그이다.

큰 PR을 보내기 전에 이슈를 열어라. 완전한 기여 가이드는 [CONTRIBUTING.md](CONTRIBUTING.md)를 참조하라. [Code of Conduct](CODE_OF_CONDUCT.md)를 포함한다.

### 기여자

<a href="https://github.com/Cyrax321/CONTINUUM/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=Cyrax321/CONTINUUM" />
</a>

## 스폰서

CONTINUUM이 에이전트의 신뢰할 수 있는 복구에 도움이 된다면, 장기적인 유지를 지원하기 위해 스폰서를 고려하라.

<p align="center">
  <a href="https://github.com/sponsors/Cyrax321"><img src="https://img.shields.io/badge/Sponsor-❤-ff69b4?style=for-the-badge&logo=githubsponsors" alt="Sponsor Cyrax321" /></a>
</p>

<p align="center">
  <a href="https://github.com/sponsors/Cyrax321">스폰서가 되기</a>, GitHub Sponsors, 또는 FUNDING.yml에 커스텀 링크를 추가하라(다른 플랫폼을 선호하는 경우).
</p>

## 라이선스

Apache 2.0, [LICENSE](LICENSE) 참조.

---

깊은 레퍼런스 자료:

- [references/install.md](references/install.md) - 전제 조건, 설치 수준, 패키지 맵, 검증
- [references/concepts.md](references/concepts.md) - 시맨틱 체크포인트, 검증, 원장, 복구 모드, 계약
- [references/architecture.md](references/architecture.md) - 데이터 모델, 이벤트 로그, 프로젝션, 저장소, 체크포인팅, 복구 엔진, 보안
- [references/adapters.md](references/adapters.md) - 프레임워크 어댑터 사용법과 라이브 모델 검증 결과
- [references/api.md](references/api.md) - Python과 어댑터 API
- [references/cli.md](references/cli.md) - 완전한 CLI 명령 목록, 종료 코드, 상태 차이
- [references/mcp.md](references/mcp.md) - MCP 서버 상태, 검증, 열린 질문
- [references/bench.md](references/bench.md) - CONTINUUM-Bench 설계
- [references/quickstart.md](references/quickstart.md) - 설치, 예제, 증명 스크립트
- [references/e2e.md](references/e2e.md) - 엔드투엔드 자율 테스트 워크스루
- [references/testing.md](references/testing.md) - 테스트 스위트 배치와 관례
- [references/related-work.md](references/related-work.md) - 주석이 달린 관련 연구와 인용 감사
