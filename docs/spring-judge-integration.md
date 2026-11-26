# Spring → 검증 초안 → Agent Judge · 2026-11-23

기준은 `codex/rag-safe-share`의 `23e54a9`다. 기존 Graph JSON·룰·RAG 초안 검증·단일 Judge v2와 평가 기준은 유지한다. Spring이 이미 보관한 초안 자체를 코드로 재검사한 뒤 RAG의 기존 `openai_judge`에 전달한다. 새 초안을 생성해 기존 초안의 판정으로 표시하지 않는다.

## API와 호출 순서

```text
GET  /api/graph?txId=TX-000619283&hops=2&windowDays=7
POST /api/review/prepare   {"snapshotId":"<reviewSnapshotId>","findingIndex":0}
POST /api/review/judge     {"reviewId":"<준비한 초안 ID>"}
GET  /api/review/judge?reviewId=<같은 ID>
POST /api/history/save     {"snapshotId":"<같은 snapshotId>","reviewId":"<같은 reviewId>"}
GET  /api/history/open?historyId=<저장 ID>
```

Judge POST는 정확히 `reviewId` 한 필드만 받는다. 그래프·수치·초안·모델·키·임의 URL은 받지 않는다. 기존 동일 Origin·JSON·2KB·중복/미정의 필드 거부를 적용한다. GET은 `judgeState=not_requested|running|finished`와 현재 `judge` 또는 `null`을 반환한다. 실행 중 같은 초안의 중복 POST는 429다. 끝난 같은 초안의 재전송은 `cached=true`이며 모델을 다시 호출하지 않는다. 재시도가 필요하면 같은 그래프에서 새 초안을 준비해 새 `reviewId`를 사용한다.

Spring은 기존 `/v1/rag/reports/verify`를 먼저 실행한다. 수치·인용·원문·렌더링 검증이 실패하면 409이며 Judge를 호출하지 않는다. 내부 `POST /v1/rag/reports/judge`에는 서버가 보관한 `graph`, `ruleResult`, `report`만 보낸다. RAG도 전달받은 초안을 다시 검증하고 같은 코퍼스 버전의 사실·거래·인용 절을 수집한 뒤 기존 단일 Judge를 최대 한 번 호출한다. 이 경로는 로컬 호스트와 명시한 내부 호스트만 허용하고, 동일 Origin·JSON·2MB·동시 한 요청으로 제한한다. RAG OpenAPI는 0.8.0에 추가했다.

일반 RAG 호출의 20초 제한은 유지하며 Judge 호출만 Spring에서 75초를 허용한다. 기존 모델 어댑터의 네트워크 대기는 60초다. Spring의 시간 초과·통신 실패는 무판정으로 기록하고 자동 재시도하지 않는다. 이미 시작한 원격 모델 호출의 취소나 비용 회수를 보장하지 않는다.

## 상태·사유·근거

클라이언트는 기존 `judge.status` 대신 **`judge.decisionStatus`**를 판정 표시 기준으로 사용한다. 기존 Judge의 `status=held`는 호환을 위해 보존한다.

| 상황 | `decisionStatus` | `outcomeCategory` | `evaluationCompleted` | 점수 |
|---|---|---|---|---:|
| 모든 항목의 근거가 뒷받침함 | `passed` | `semantic_decision` | true | 100 |
| 근거 부족을 항목별로 판단함 | `held` | `insufficient_evidence` | true | 50 |
| 원본과 모순된 주장이 있음 | `rejected` | `semantic_decision` | true | 0 |
| 모델 응답 JSON·항목·근거 ID 오류, 미완료·거부 | `not_judged` | `model_response` | false | null |
| 모델 설정 없음·제공자 오류·통신 실패·입력 한도 | `not_judged` | `configuration|provider_error|transport|input_limit` | false | null |

`reasonCode`와, 제공되는 경우 허용된 `responseErrorCode`·`providerHttpStatus`·`providerErrorCode`만 기록한다. 모델의 원본 오류 메시지나 인증 헤더는 보관하지 않는다. 사유 문장·항목 판정은 기존 모델 응답 검사 후 저장한다. `judge_evidence_missing`처럼 모델 호출 전 필요한 근거가 없는 경우에는 `held / insufficient_evidence / evaluationCompleted=false / 점수 null`이다.

각 항목에 기존 `itemId`, `criterion`, `verdict`, `reason`, `verificationScore`, `factIds`, `chunkIds`를 보존하고, 사실이 가리키는 원본 `evidenceTxIds`를 서버에서 도출한다. 최상위에도 사용한 세 ID 집합을 모은다. Spring은 항목 누락/중복, 허용된 사실·청크, 항목 점수·최종 분기, 거래 ID의 원본 연결을 다시 검사한다.

`judge.identity`는 그래프·룰·초안의 SHA-256, 대상 거래, report/context ID와 버전·코퍼스를 묶는다. 모델·전략/루브릭 버전·입력 SHA-256·판정 ID·시각·호출 시도·지연도 기록한다. Spring–RAG 통신 실패로 실제 모델 호출 여부를 모르면 `modelCallAttempts=null`, `modelCallAttemptsKnown=false`로 남기며 0회라고 추정하지 않는다.

Judge의 점수는 초안 검토 점수이며 거래의 룰·ML 점수, 확률 또는 사람의 승인과 별개다. `humanReviewRequired=true`, `readyForSubmission=false`를 유지한다.

## 그래프 변경과 저장

판정은 서버의 원래 `reviewId → snapshotId → graph/rule/report`에만 귀속한다. 계좌 추가 조회가 성공하면 기존 스냅샷의 Judge 조회·실행·저장은 409다. 새 스냅샷에서 준비한 초안은 `judge=null`, `judgeState=not_requested`로 시작한다. 모델 응답을 기다리는 동안 그래프가 바뀌어도 반환 직전에 다시 확인해 늦은 판정을 폐기한다.

Judge 결과는 현재 초안의 서버 상태에 보관한다. 영속 보관은 기존 `history/save`에 같은 스냅샷과 초안 ID를 보내면 수행한다. `reviewId:null`은 기존처럼 그래프·룰만 저장한다. 판정 실행 중 초안을 함께 저장하면 409이며, 원문을 모으는 사이 Judge 상태가 바뀌면 저장 전체를 거부하고 재요청을 요구한다. 부분 결과를 저장하거나 과거 저장본을 덮어쓰지 않는다.

SQLite 기존 스키마 1의 `capture.review.judge`에 판정·사유·ID·버전을 포함한다. Judge 없는 기존 저장본도 계속 열린다. 다시 열기·저장본 검사·JSON 다운로드는 과거 Judge와 식별자 연결만 확인하며 현재 모델·Neo4j·RAG를 호출하지 않는다. 현재 자료 재검증으로 표시하지 않는다.

## 키 설정과 실행

키는 **RAG 서비스를 실행하는 셸의 `OPENAI_API_KEY`**, 모델은 같은 프로세스의 `OPENAI_MODEL`에서 기존 `judge_configuration()`으로 읽는다. `.env`를 자동 로드하지 않는다. Spring·브라우저 요청으로 전달하거나 코드·SQLite·로그에 쓰지 않는다. 설정을 마친 로컬 PowerShell에서 저장소 루트 기준으로 실행한다.

Compose에서는 Spring이 `rag:8092`로 호출하므로 RAG의 시작 명령에 `--judge-host rag`를 지정한다. RAG 포트는 Compose 내부에 유지한다. 로컬 직접 실행은 기존 루프백 수신 주소와 호스트 제한을 그대로 사용한다. 제공자 응답 본문이 읽는 도중 잘리면 Judge는 `not_judged / transport`로 기록한다.

```powershell
# 두 변수의 값은 로컬 환경에 설정하고 채팅에 붙여 넣지 않는다.
python -B rag-service/rag_server.py --port 8092
```

8092를 이미 사용 중이면 본인이 시작한 이전 RAG를 확인해 종료하고 새 프로세스를 시작한다. 다른 포트로 실행하면 Spring의 `RAG_URL` 또는 `--traceon.rag-url`을 같은 주소로 지정한다. 새 RAG의 `/health`에는 `judgeModes.single`, `judgeModes.strategyVersion`, 설정 유무만 나타내는 `judgeModes.configured`가 있다. 키와 모델 값은 헬스체크에 반환하지 않는다.

## 재현 검사

```powershell
cd backend
.\gradlew.bat test bootJar
cd ..
python -B -m unittest discover -s rag-service/tests -q
# 실제 Neo4j·인용 자료 + 기존 Judge 어댑터 + 모의 제공자 응답, 유료 호출 0회
python -B scripts/verify-spring-judge.py --output artifacts/spring-judge/new-mock-run.json
# 설정한 로컬 RAG를 통해 실제 Judge 최대 한 번
python -B scripts/verify-spring-judge.py --live-rag-url http://127.0.0.1:8092 --output artifacts/spring-judge/new-live-run.json
```

통합 도구는 기본 18081 포트에서 자신이 만든 Spring만 시작/종료한다. 실제 모델 모드는 이미 로컬 환경이 설정된 RAG에 접속하며 Spring 자식 환경에서는 `OPENAI_API_KEY`, `OPENAI_MODEL`을 제거한다. 이전 결과·DB·로그는 덮어쓰지 않는다.

Spring 자동 검사 45개, RAG 회귀 검사 274개가 통과했다. 모의 통합에서는 실제 69→72거래와 원문을 사용해 `passed`, 근거 부족 `held`, `rejected`, 잘못된 근거 ID 응답의 `not_judged`를 확인했다. 다섯 저장본을 Spring 재시작 후 그대로 열었으며 실시간 의존성 호출은 0회였다. 판정 실행 중 저장 차단과 그래프 변경 후 늦은 응답 폐기도 확인했다. 이는 연결·귀속·저장 정확성 검사이며 기존 Judge 독립 검수·탐지 평가를 재평가하거나 품질 수치를 바꾸지 않는다.

2026-11-26 백엔드 Compose 설정과 합쳐 Spring 45개·RAG 276개를 재실행했다. 모의 제공자를 사용한 HTTP 검사에서 명시한 `rag` 호스트의 판정 성공, 미등록 호스트와 교차 사이트 요청의 거부, 잘린 제공자 응답의 무판정을 확인했다. 실제 Compose에서의 그래프 → 초안 → Judge → 저장·복원은 Docker 엔진 실행 후 확인한다.
