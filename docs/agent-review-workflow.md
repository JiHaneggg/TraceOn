# Agent 식별 → 근거 수집 → 설명 초안 → 검증

`AgentTools.call('run_review_workflow', ...)`는 기존 근거 도구들을 순서대로 호출한다. 백엔드가 확정한 **같은 조회의 graph와 ruleResult**가 필수다. 기본 `template`은 네트워크·DB·실제 모델 호출 없이 기존 제어형 초안을 만든다. 새로운 탐지기나 점수 산출 모델을 추가하지 않는다.

## 실행 계약

1. `build_evidence_context`: Graph·Rule 일치 여부를 기존 룰 엔진으로 재검산하고 대상 거래·탐지·점수를 받는다. 불일치 입력은 `ValueError`로 거부한다.
2. `resolve_report_fact`, `get_citation`: 모든 사실의 값·근거 거래와 모든 인용의 버전 고정 원문·강조 위치·스냅샷 상태를 수집한다.
3. `generate_review_report`: 같은 입력과 처음 선택한 코퍼스로 설명 초안을 만든다. 원질문 근거가 없거나 원문 스냅샷이 없으면 이 단계를 실행하지 않는다. 보조 CDD 자료로 원질문의 보류를 해제하지 않는다.
4. `verify_review_report`: 같은 Graph·Rule·Query로 전체 보고서의 사실·인용·스냅샷·렌더링·해시를 검증한다. 변조 초안은 반려하고 검사 결과와 후보 초안을 함께 반환한다.

입력을 처음에 깊은 복사하고 각 도구에는 별도 복사본을 전달한다. `identity.snapshotId`는 Graph·Rule·Query를 묶고, `identity`에는 graphSha256·ruleResultSha256·contextId·corpusSha256·targetTxId도 남긴다. 이후 코퍼스 변경/누락을 현재 문서로 대체하지 않는다. 은행 원장의 진위나 전자서명 인증을 의미하지 않는다.

```python
from agent_tools import AgentTools
from retrieval import Index

tools = AgentTools(Index("data/rag/index.json"), raw_root="data/rag/raw")
result = tools.call("run_review_workflow", {
    "graph": backend_graph,
    "ruleResult": backend_rule_result,
    "mode": "template",
    "maxRetries": 0,
})
print(result["status"], result["execution"]["retryCount"],
      result["humanReviewRequired"])
```

추가 입력은 query·corpusSha256·mode·maxRetries만 받는다. 점수·사실·모델 응답을 별도 입력으로 받지 않는다. 이는 Python Agent 도구이며, 기존 HTTP API·Graph 화면 브리지는 기존 계약을 유지한다.

| 반환 항목 | 의미 |
|---|---|
| `status=passed` | 전체 사실·인용·원문·표시 검사 통과. 의미적 충분성·범죄·법률 적용 판단은 아님 |
| `status=held` | 요청 범위 근거·원문·코퍼스가 없거나 생성 실패. 사유는 reasonCodes |
| `status=rejected` | 근거 응답 불일치, 초안 값·인용·내용 변조, 잘못된 보고서 구조 |
| `verification.performed` | 전체 보고서 검사를 실행했는지 여부. 초안 이전 보류는 false |
| `context`, `identification`, `evidence` | 도구가 검산한 점수·사실·원문·근거 거래 |
| `report`, `verification` | 후보 보고서와 종합 검사. 보류 시 report는 null일 수 있음 |
| `execution` | 생성 시도 수, 재시도 횟수, 시도별 상태, 순서가 있는 도구 호출 기록 |
| `humanReviewRequired`, `readyForSubmission` | 항상 true, false. 자동 통과 후에도 사람 검토 필요 |

## 재시도와 실제 모델 연결

기본 `maxRetries=0`, 허용 범위는 정수 0~2다. `generationAttempts`는 최초 시도를 포함하고 `retryCount`는 추가 시도만 센다. 예: 최초 생성 실패 후 재시도 성공은 2회/1회, 근거 부족으로 생성 전 보류는 0회/0회다. 이 값은 도구 호출 횟수이며 외부 API 과금/호출 인증 수치가 아니다.

`ReportUnavailable` 생성 실패만 정해진 횟수까지 재시도한다. 점수·금액·인용·본문 검증 실패는 즉시 반려한다. 재시도 때 입력·문서 버전을 바꾸거나 template으로 자동 대체하지 않는다. 설정 누락도 생성 실패로 집계되므로 모델 설정을 준비한 후 재시도를 활성화한다.

실제 연결은 기존 `mode=openai`, `OPENAI_API_KEY`, `OPENAI_MODEL` 설정을 사용한다. 모델은 이미 도구에서 받은 observationId·citationId·reviewItemId 선택/정렬만 한다. 점수·원본 사실·숫자·원문·설명 문구는 기존 도구와 렌더러가 제공한다. 없는 ID나 임의 문장·금액을 모델 출력으로 받아들이지 않는다. 성공 시 `report.generation`에 생성 방식·모델 정보가 있으며, `verification.providerCallAuthenticated=false`는 유지된다.

## 고정 데이터 검증

```powershell
python rag-service/agent_workflow_demo.py --output artifacts/agent-workflow/new-verification.json
python -m unittest discover -s rag-service/tests -p test_agent_workflow.py -v
```

출력은 이미 존재하는 파일에 덮어쓰지 않는다. `examples/agent-workflow/`의 3거래 Graph·Rule·질문·합성 HTML·문서 정의를 사용한다. 코퍼스 수집 시각을 2024-01-01 UTC로 고정하므로 체크아웃 시각과 무관하게 동일한 코퍼스/Context/Report 식별자를 재현한다. 합성 문서는 실제 공식 자료가 아니며 URL에 접속하지 않는다. fixtureSha256과 전체 실행 결과를 JSON에 기록한다.

### 일반 사례

대상 금액 `1002.10`, 3거래 총 송금액 `3003.30` US Dollar, 룰 점수 90을 도구에서 받아 전체 사실·인용 검사를 통과한다. 최초 생성 1회, 재시도 0회다.

### 근거 없는 사례

질문 `화성 대기압 광합성`은 원질문 근거를 찾지 못해 보류한다. 보조 CDD 문서가 있어도 설명 초안을 만들지 않는다. 최초 생성·재시도 모두 0회다.

### 잘못된 초안

같은 조회에서 금액을 `999999.99`로 바꾼 초안과 원문에 없는 인용을 넣은 초안을 각각 반려한다. 변조 후 reportId를 다시 계산해도 금액/인용 및 렌더링 검사에서 실패한다. 반려 시 재생성하지 않는다.

추가 테스트는 동일 조회 복사·코퍼스 고정·원문 누락·잘못된 보고서 구조·재시도 상한·오류 정보 제한을 검사한다. 기존 OpenAI 어댑터에는 스텁 응답을 연결해 허용되지 않은 ID 출력 거부 → 1회 재시도 → 허용 ID 성공을 확인한다. 실제 모델 호출과 탐지 정확도·법률 판단 검증은 수행하지 않는다.

2026-11-23 실행: 새 Agent 테스트 19개를 포함한 RAG 230개와 룰 24개가 통과했다. 고정 시연 4건은 통과·보류·반려·반려였으며, 각 실행에서 사실 17개와 문서 원문 1~2개를 조회했다. 결과 파일은 `artifacts/agent-workflow/verification-20261123.json`이다. 실제 모델·DB 호출은 0회다.

## 단일 Agent Judge

`run_review_workflow`에 `judgeMode="openai"`를 추가하면 코드 검증 다음에 의미 검토를 실행한다. `mode="template"`과 조합하면 Judge만 모델을 호출한다. 기존 호출의 Judge 기본값은 `off`다.

기존 코드가 금액·시간·건수·인용·원문 스냅샷과 렌더링을 확인한다. 이 검증을 통과한 경우에만 Judge에게 같은 조회의 사실·근거 거래·초안에 인용된 RAG 원문 절을 보낸다. [DeepEval DAG](https://deepeval.com/docs/metrics-dag)의 항목별 판정과 명시적 분기 방식을 따라 단일 응답에서 세 기준을 평가한다.

| 항목 | 확인 기준 |
|---|---|
| `claim_support` | 초안의 각 핵심 관찰이 동일 조회의 사실·원본 경로 또는 원문에 근거하는지 확인한다. 순환 경로만으로 동일 자금이나 거래 목적을 입증할 수 없다. |
| `document_support` | 초안의 문서 해석이 원문의 전제·예외·관할·시점을 보존하는지 확인한다. 인용 문구가 일치해도 해석은 원문과 모순될 수 있다. |
| `normal_trade_consideration` | 초안이 가능한 정상 거래 가설과 확인 절차를 제시하는지, 또는 가설을 구분할 구체적인 미확인 정보를 짚는지 확인한다. |

항목별 판정에는 `factIds`, `chunkIds`, 사유와 검증 점수를 남긴다. 코드가 모델 응답의 항목 누락, 중복과 근거 ID를 확인한 뒤 다음 분기를 적용한다.

| 분기 | 흐름 상태 | Judge 검증 점수 |
|---|---|---|
| 모든 항목 `supported` | `passed` | 100 |
| `insufficient`가 있고 모순은 없음 | `held` | 50 |
| 하나 이상의 `contradicted` | `rejected` | 0 |
| 모델 설정 누락, 호출 실패, 불완전하거나 유효하지 않은 응답 | `held` | `null` |
| 코드 검증 전 보류 또는 코드 반려 | 기존 상태 유지, `judge.status=skipped` | `null` |

`verification`은 기존 코드 검증 결과이고 `judge`는 의미 검토 결과다. `judge.verificationScore`는 항목 점수의 최솟값이며 룰·ML 탐지 점수와 별개다. 단일 Judge는 최대 한 번 호출하며 시도 횟수와 지연을 기록한다. 최종 판단은 사람이 맡는다.

### 합성 데이터 실행

이미 설정한 PowerShell의 `OPENAI_API_KEY`와 `OPENAI_MODEL`을 읽는다. 키는 요청의 인증 헤더로만 사용한다. 다음 명령의 첫 번째 실행은 기존 합성 초안을 코드 검증한 뒤 Judge를 한 번 호출한다. 두 번째 실행은 평가용 합성 주장 3개를 각각 두 번 검토한다.

```powershell
python -B rag-service/agent_judge_demo.py --output artifacts/agent-judge/live-workflow.json
python -B rag-service/agent_judge_demo.py --suite benchmark --split evaluation --repeats 2 --output artifacts/agent-judge/live-evaluation.json
```

이미 존재하는 출력 파일은 새 경로를 지정해 실행한다. 모의 테스트 명령은 `python -B -m unittest discover -s rag-service/tests -p test_agent_judge.py -v`다.

`examples/agent-workflow/judge-cases.json`에 개발·평가용 주장과 기대 판정을 기록했다. 판정 라벨은 개발자가 지정했고 두 집합 모두 같은 합성 거래와 코퍼스를 사용한다. 전체 흐름 시연에는 실제 템플릿 초안을 그대로 쓰고 의미 평가에서는 검증에 사용할 주장만 교체한다. 평가 함수 `evaluate_cases`는 동일한 입력에 다른 Judge 함수를 연결할 수 있다. 입력·사례 파일의 해시, 코퍼스 및 근거 버전, 모델과 기준 버전, 혼동 행렬, 반려 Precision·Recall·F1, 반복 판정 일치율과 점수 범위를 남겨 이후 단일·토론 방식을 비교할 수 있다.

평가 출력 1.1은 점수 없이 보류된 응답을 혼동 행렬의 `unscored` 열에 기록한다. 반려 Recall의 분모는 반려가 기대된 모든 호출이며, 반복 판정 일치율은 양쪽에 점수가 있는 쌍에서 계산한다. `scoredRepeatPairs`와 `totalRepeatPairs`에서 비교 범위를 확인할 수 있다. 유효하지 않은 응답에는 원문 없이 `responseErrorCode`를 남긴다. 특정 사례만 확인할 때는 `--case-id`에 해당 ID를 지정한다.

## 토론 Judge와 단일 Judge 비교

`run_review_workflow`의 `judgeMode="debate"`는 코드 검증 후 최대 두 번 모델을 호출한다. 첫 검토자는 관찰된 의심 징후와 추정을 구분한다. 두 번째 검토자는 같은 그래프·원문에서 정상 거래 가능성과 반례를 확인하고 첫 검토에 응답한다. 두 응답은 기존 근거 ID 검사를 통과해야 한다.

항목 판정이 엇갈리거나 모순의 원본 근거가 일치하지 않으면 그 항목을 보류한다. 두 역할이 같은 원본으로 확인한 모순이 있으면 초안을 반려한다. 문서 해석의 모순에는 두 역할이 공통으로 인용한 원문 청크가 필요하다. 어느 역할의 응답이든 점수를 매길 수 없으면 전체 초안을 점수 없이 보류한다. 결과에는 두 역할의 판정·사유·ID, 합의 결과, 호출 횟수와 지연을 기록한다.

`traceon-single-v2`와 `traceon-debate-v2`는 같은 응답 스키마를 사용한다. 모든 검토에서 현재 입력의 판정 항목 ID·사실 ID·원문 청크 ID를 선택한다. [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)의 열거형 한도를 넘으면 모델을 호출하기 전에 보류한다. 응답은 기존의 ID, 중복, 누락 검사도 통과해야 한다. 출력 ID가 유효해도 그 ID가 주장을 뒷받침하는지는 별도로 검토한다.

첫 실제 비교의 토론 방식에는 모순 사례에서 두 번째 역할이 알 수 없는 ID를 반환한 실행이 한 번 있었다. 아래 명령은 그 개선을 개발 사례에서 확인하며 최대 12번 호출한다. `strategyVersion`이 바뀌므로 이전 실행과 새 실행은 별도 결과로 보관한다.

```powershell
python -B rag-service/agent_judge_demo.py --suite benchmark --strategy debate --split development --repeats 2 --output artifacts/agent-judge/live-debate-v2-development.json
```

### 기존 합성 사례 비교

```powershell
python -B rag-service/agent_judge_demo.py --suite compare --split evaluation --repeats 2 --output artifacts/agent-judge/live-single-vs-debate.json
```

위 명령은 기존 3개 사례에서 단일 Judge 6회와 토론 Judge 최대 12회, 총 최대 18번을 호출한다. 비교 파일은 고정한 입력 해시·모델·기대 판정·근거 ID 스키마를 대조하고, 같은 사례와 반복 번호의 두 결과를 짝지어 기록한다. 반려 Recall·Precision·F1, 통과 기대 사례의 오반려, 점수 없는 실행, 반복 일치율, 호출 횟수와 평균 지연을 함께 확인한다.

### 새 합성 사례 독립 검수

새 검수용 입력은 세 계좌에서 한 계좌로 모이는 집금 그래프와 여섯 가지 주장이다. 사람은 모델 출력을 보기 전에 라벨 패킷의 `cases`에서 사실·거래·원문을 읽고, 각 `labels` 항목에 기대 판정 `passed`, `held`, `rejected` 중 하나와 짧은 `reason`을 적는다. 원본 사실과 일치하고 검토 기준을 충족하면 통과, 필요한 근거가 없으면 보류, 원본과 양립할 수 없으면 반려로 라벨링한다.

```powershell
python -B rag-service/agent_judge_demo.py --suite label-packet --split holdout --output artifacts/agent-judge/holdout-label-packet.json
```

라벨 패킷은 모델 없이 생성된다. 원본을 복사해 `holdout-reviewed.json`으로 저장하고 `labels`만 작성한다. 실행기는 모든 라벨과 사유, 패킷의 입력 해시가 맞는지 모델을 호출하기 전에 확인한다. 결과에는 제출한 라벨 파일 해시도 남긴다.

```powershell
python -B rag-service/agent_judge_demo.py --suite compare --split holdout --labels artifacts/agent-judge/holdout-reviewed.json --repeats 2 --output artifacts/agent-judge/live-holdout-comparison.json
```

기존 합성 사례 비교에서 개선이 보일 때 독립 검수 결과를 비교한다. 모의 결과는 역할 연결과 결과 집계의 동작을 확인하는 데 사용한다. 실제 두 방식의 품질 차이는 독립 라벨과 실호출 비교 결과에서 판단한다.

### OpenAI Decisions 합성 실험

`traceon-decisions-v1`은 `gpt-6-luna`의 [Decisions API](https://developers.openai.com/api/docs/guides/decisions)에 같은 합성 그래프·초안·원문 입력을 보낸다. 항목별 `choice` 질문에서 `supported`, `insufficient`, `contradicted`를 선택한다. 응답의 선택지를 기존 점수와 분기로 변환하고, 모델이 반환한 신뢰도는 `decisionConfidence`에 따로 저장한다.

선택형 API가 반환하는 정보는 판정과 신뢰도다. 근거 ID 선택과 작성된 사유가 필요한 검토에는 단일 또는 토론 Judge를 사용한다. 합성 실험 파일에서 `evidenceAttributionProvided=false`로 표시한다.

```powershell
python -B rag-service/agent_judge_demo.py --suite benchmark --strategy decisions --split development --repeats 1 --output artifacts/agent-judge/live-decisions-development.json
```

위 명령은 개발 사례 3개를 한 번씩 실행한다. `--suite compare`에 `--include-decisions`를 추가하면 단일·토론과 같은 입력에서 판정 일치, 무점수 실행, 호출 횟수, 지연을 비교할 수 있다. 독립 검수 사례 여섯 개를 각 두 번 실행하면 세 방식 합산 최대 48번을 호출한다.

## 다중 그래프 독립 검수

순환 송금, 지급 방식이 섞인 집금, 단일 송금의 새 합성 그래프 3개에서 주장 9개를 준비한다. 거래 계좌·시각·거래 ID는 이전 Judge 평가와 분리한다. 각 검수 사례는 그래프·거래 사실·합성 원문·초안 주장을 포함한다. 원본이 뒷받침하면 `passed`, 근거나 검토가 부족하면 `held`, 원본과 명백하게 모순되면 `rejected`다.

```powershell
python -B rag-service/agent_judge_multigraph.py prepare --output artifacts/agent-judge/multigraph-label-packet-next.json
```

검수자는 생성된 파일을 복사한 뒤 `labels`의 각 ID에 `expected`와 짧은 `reason`을 채운다. 처음 생성한 패킷을 보관하고 검수 완료 파일은 `multigraph-reviewed.json`으로 저장한다. 평가 입력, 그래프 식별자나 패킷 해시가 바뀌거나 라벨이 비어 있으면 실행을 중단한다. 실제 모델에는 주장과 근거만 전달하고 검수 라벨은 판정이 돌아온 뒤 비교한다.

```powershell
python -B rag-service/agent_judge_multigraph.py evaluate --labels artifacts/agent-judge/multigraph-reviewed.json --output artifacts/agent-judge/multigraph-live.json
```

실행 창에 `OPENAI_API_KEY`와 `OPENAI_MODEL`을 설정한다. 단일 Judge를 사례별 1회 호출한다. 결과는 3분류 혼동 행렬, 결함 반려 Recall·F1, 유효 판정이 없는 호출 수, 소요 시간을 기록한다. 무점수 실행은 결과에 별도 열로 남기며, 반려가 기대된 사례에서 발생하면 반려 Recall의 분모에 포함한다. 9개 사례는 새 거래 구조에서 판정 분기를 확인하는 첫 검수 묶음이다.

### 첫 실제 평가 결과 · 2026-11-23

사용자가 확정한 3개 그래프의 통과·보류·반려 각 3건을 동결 패킷으로 대조했다. `gpt-6-luna`를 9회 호출해 8건이 기준 판정과 일치했다. 결함 반려는 3/3건으로 반려 Recall·F1이 각각 1.0이고, 평균 모델 응답 시간은 4.42초였다. 통과 기대 3건 중 2건이 통과했으며 `MG-04`는 근거 ID 누락으로 무점수 처리했다. 의미상 보류로 라벨링한 3건은 모두 보류 판정과 일치했다.

첫 기록은 `artifacts/agent-judge/multigraph-live-20261123.json`에 보존한다. `MG-04`만 재확인할 때도 이전과 동일한 전체 검수 패킷을 검사한다. 아래 명령은 1건만 호출하고 별도 결과를 저장한다.

```powershell
python -B rag-service/agent_judge_multigraph.py evaluate --labels artifacts/agent-judge/multigraph-reviewed.json --case-id MG-04 --output artifacts/agent-judge/multigraph-mg04-recheck-20261123.json
```

### MG-04 단건 재확인

같은 검수 파일, 거래 그래프, Judge 모델·기준 버전, 입력 SHA-256으로 `MG-04`를 1회 다시 호출했다. 응답은 송금인 수·거래 건수와 고객확인 원문 청크를 근거로 통과(100점)했다. 검토 사유는 공동 행사비 정산 가설과 구체적인 확인 항목을 뒷받침했다. 모델 응답 시간은 5.31초였다.

첫 실행은 8/9 일치와 무점수 1건으로 보존한다. 단건 재호출 결과는 `artifacts/agent-judge/multigraph-mg04-recheck-20261123.json`에 따로 기록했다. 단건 평가에는 반려 라벨이 없어 반려 Recall과 F1은 `null`이다.
