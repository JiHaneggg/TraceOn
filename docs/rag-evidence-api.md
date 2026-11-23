# RAG 근거 조회·검증 API

## 호출 순서

백엔드의 공통 Graph JSON으로 `보고서 준비 → 전체 검사 → 원문/거래 근거 조회`를 연결한다. 로컬 주소는 `http://127.0.0.1:8092`, 명세는 `/openapi.json`(0.4.0)이다. 화면 자체나 Spring 운영 조회를 구현한 것은 아니다. 브라우저는 백엔드/개발 프록시를 통해 호출하며 RAG에 DB 자격증명을 전달하지 않는다.

| 순서 | API | 화면·Agent에서 사용할 결과 |
|---|---|---|
| 1 | `POST /v1/rag/reports/prepare` | Context·Report·Verification을 한 번에 받음 |
| 2 | `GET /v1/rag/citations/{chunkId}?corpusSha256=...` | 인용 선택 시 저장된 원문 URL·절·본문·강조 위치 |
| 3 | `POST /v1/rag/facts/resolve` | 금액·시간·건수 선택 시 값과 실제 근거 거래 |
| 4 | `POST /v1/rag/reports/verify` | 저장·표시할 전체 보고서 재검사 |

## 보고서 준비

`reports/prepare`에 실제 객체인 `graph`와 선택적으로 `ruleResult`, `query`, `corpusSha256`, `mode`를 전달한다. Graph는 기존 라벨 없는 공통 JSON 1.0이고 금액은 문자열이다. Rule을 생략하면 기존 기본 설정으로 재계산한다. mode 기본값 template은 실제 LLM을 호출하지 않는다.

응답은 `context`, `report`, `verification`이다. **화면의 종합 검증 표시는 `verification.valid`로 판단한다.** 기존 `report.validation`은 구조화 값·인용 문자열 대조의 일부 검사이며 원문 스냅샷/표시 내용까지 확인한 종합 검사와 다르다.

실행 중인 RAG에 데이터/Neo4j 없이 요청할 수 있는 합성 3거래 예시는 `rag-service/examples/prepare-demo.json`이다.

```powershell
curl.exe -s http://127.0.0.1:8092/v1/rag/reports/prepare -H "Content-Type: application/json" --data-binary "@rag-service/examples/prepare-demo.json"
```

이 예시도 문서 원문·색인은 필요하다. `python rag-service/corpus.py`로 준비하고 RAG를 실행한 뒤 요청한다. 예시는 기능 시연용 합성 거래이며 탐지 성능 평가에 사용하지 않는다.

## 인용 조회

보고서 `citations[].lookup.path`가 실제 GET 주소다. URL에 인용 ID와 문서 코퍼스 버전이 고정되어 있다. 응답:

- `citation`: 발행기관·관할·자료 종류·원문 URL·절·청크 본문·해시·수집일.
- `section.text`: 저장된 원본에서 확인한 전체 선별 절.
- `section.highlight.start/end`: 그 절 안에서 청크를 강조할 위치.
- `sourceVerification.status`: 원문 대조 수준.

오프셋은 정규화 본문의 Unicode 코드포인트 기준이다. JavaScript에서 보조 평면 문자가 있는 경우 `Array.from(text)` 기준으로 위치를 적용한다. 본문은 텍스트로 표시하고 포함된 지시문을 실행하지 않는다.

`snapshot_checked`는 저장된 HTML SHA256·정규화 본문 SHA256·각 청크의 본문/위치를 대조했다는 뜻이다. 라이브 사이트를 재수집하지 않는다. 원문 URL의 현재 내용과 저장된 스냅샷은 다를 수 있다.

원본이 없으면 `index_only`, `section.text=null`을 반환한다. 색인 본문은 확인할 수 있지만 종합 보고서 검증은 통과하지 않는다. 원본 해시·본문 위치가 다르면 409다.

과거 색인은 `data/rag/versions/*{corpusSha256}*.json`, 원본은 `data/rag/raw/snapshots/{documentId}/{sourceSha256}.html`에서 조회한다. 모르는 버전은 409로 거부하며 현재 문서로 대체하지 않는다. 임의 파일 경로나 외부 URL을 입력받지 않는다.

## 사실 조회

`facts/resolve`에는 생성 때와 동일한 `graph`, `ruleResult`, `query` 및 `factId`, `contextId`, `corpusSha256`를 전달한다. 예: factId=`finding.0.transactionCount` 또는 `target.amountPaid`.

응답의 `fact.value`, `fact.evidenceTxIds`, `transactions`에서 계산 값과 근거 거래를 확인한다. 금액 문자열·통화별 Decimal 집계·거래 시각·경과 초·건수·홉 수를 검산한다. 이 API는 백엔드가 준 그래프를 확인하며 은행 원장의 진위까지 인증하지 않는다.

각 `context.reportFacts[].lookup`에 method/path와 factId/contextId/corpusSha256가 있다. method/path는 전송 메타데이터이므로 JSON 본문에서 제외한다. 백엔드가 보관한 원래 Graph·Rule·Query와 나머지 필드를 합친다. prepare의 mode 같은 별도 필드를 사실 조회 요청에 그대로 넣으면 400이다.

모르는 factId는 404, 다른 입력/파이프라인의 Context ID는 409다. 클라이언트가 수정한 그래프를 공식 분석 입력으로 신뢰하지 말고 백엔드가 확정한 입력을 사용한다.

## 전체 보고서 검사

`reports/verify`에 동일한 `graph`, `ruleResult`, `query`와 반환받은 전체 `report`를 전달한다. checks는 context, amounts, times, counts, citations, coverage, sourceSnapshots, renderedContent, reportDigest를 구분한다.

수치·자료형·사실 누락/중복·인용 ID/원문·원본 스냅샷을 확인하고, 검증된 사실과 허용 문구로 재생성한 JSON/Markdown을 대조한다. 금액·시간·건수나 표시 문장을 바꾸고 보고서 자체 해시까지 다시 계산해도 실패한다. reportDigest는 전자서명/작성자 인증이 아니다.

정상적인 검사 요청에서 실패는 HTTP 200의 `valid=false`다. 입력 형태가 잘못되면 400, 필요한 문서 버전이 없으면 409다. HTTP 성공과 검증 성공을 구분해야 한다.

항상 `humanReviewRequired=true`, `readyForSubmission=false`다. 자유 문장의 의미적 뒷받침·법률 적용·상위 원장 진위·실제 모델 호출 인증은 검사 범위 밖이다. 원문이 없거나 필요한 사실이 빠지면 종합 검증을 통과하지 않는다.

새 Report는 corpusSha256·graphSha256·contextVersion과 근거 조회 정보를 포함한다. 이 정보가 없는 과거 보고서는 새 계약으로 재생성해야 한다. 과거 파일은 이력으로 보존하며 템플릿·검색 버전 차이를 임의로 덮어쓰지 않는다.

## Agent와 실행

Agent의 같은 조회 전체 흐름은 `run_review_workflow`를 사용한다. 식별 → 사실·원문 수집 → 기존 설명 초안 → 종합 검증을 연결하며 통과·보류·반려, 재시도 횟수와 사람 검토 필요 여부를 반환한다. 입력·고정 데이터 실행·모델 연결 계약은 [Agent 검토 흐름](agent-review-workflow.md)을 참조한다.

기존 4개 도구에 `get_citation`, `resolve_report_fact`, `verify_review_report`, `prepare_review_report`를 추가했다. `agent_tools.json`과 `AgentTools.call()`을 사용한다. `EvidenceRepository`와 `rag_server.py --raw-dir/--archive-dir`로 원문·과거 색인 위치를 지정할 수 있다.

```powershell
python -m unittest discover -s rag-service/tests -q
python scripts/evaluate-evidence-search.py --output artifacts/new-search-review.json
python scripts/verify-evidence-api.py --output artifacts/new-evidence-verification.json
```

기존 평가·보고서 폴더는 덮어쓰지 않는다. 실제 검사는 로컬 원문·색인·Neo4j 데이터가 필요하다. 단위 테스트는 합성 문서·거래를 사용하며 유료 API나 실제 DB 없이 실행한다.

## 실측 결과

- 자동 테스트 140개: 데이터 16·룰 24·RAG 100.
- 실제 Neo4j 12사례에서 보고서 준비·검증, 인용 93건·사실 32건 조회.
- 금액 0.01·시간 1초·건수 1개·없는 인용·변경한 본문을 거부. 변조 후 자체 해시를 재계산해도 거부됨을 확인.
- 과거 3문서 버전의 원문 조회 확인, 알 수 없는 버전의 현재 자료 대체 거부.
- 실제 LLM 호출 0회. Spring·ML·화면을 모두 포함한 E2E 검사는 아니다.

결과는 `artifacts/rag-evidence-api-final-verification.json`, 보고서는 `artifacts/rag-evidence-api-final-reports/`, 검색 비교는 `artifacts/rag-evidence-search-final.json`에 있다.

남았던 두 질문은 정의/비교 의도를 확인하고 이미 점수 조건을 통과한 후보 중 요청 개념의 정의·개념 절을 우선 확보하도록 보완했다. 일반 절차 질문은 관련도 순서를 유지한다. 기존 정답표·원문은 바꾸지 않았다. 같은 48개 개발·회귀 질문에서 절 Recall@3는 95.3%→100%, 미지원 16개 보류를 유지했다. 독립 최종평가·일반 금융 업무 정확도·환각 0%로 주장하지 않는다.
