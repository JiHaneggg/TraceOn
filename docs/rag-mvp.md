# RAG 기본 실행·API — 1차 구현 기록

현재 코퍼스·보고서 API·검색 범위 검사는 [RAG V2 가이드](rag-v2.md)를 우선한다. 아래는 3문서·8청크로 시작한 1차 구현 기록이다.

현재 역할은 팀장·RAG 담당이다. 구현은 `공식 문서 검색 + 거래 근거 검증 → Agent가 사용할 Context`까지다. LLM 호출·최종 보고서 생성·관제 UI·Spring Graph API는 후속 통합 대상이다.

## 실행

프로젝트 루트에서 Python 3.10 이상으로 실행한다. 표준 라이브러리만 사용하며 유료 키·임베딩·벡터 DB 설치가 필요 없다. 현재 PC에서는 기존 Python 3.14로 검증했다.

```powershell
# 최초 1회: 공개 공식 HTML을 내려받아 색인 생성
python rag-service/corpus.py

# 기존 Neo4j·Rule·RAG를 로컬에서 시작 (Docker 실행 안 함)
.\scripts\start-local-services.ps1

# 또는 RAG 단독 실행
python rag-service/rag_server.py --port 8092

# 새 RAG 테스트 / 작은 개발용 검색 평가
python -m unittest discover -s rag-service/tests -v
python rag-service/evaluate_retrieval.py

# 실제 Neo4j 조회 → Rule API → RAG Context → 구조화 초안 검증
python scripts/verify-rag.py
```

`corpus.py`는 저장된 HTML을 재사용한다. 원문 재수집은 `--refresh`로 명시한다. 새 색인이 준비되면 RAG 프로세스를 다시 시작해 읽게 한다. 수집 중 실패하면 기존 색인은 유지한다. 서비스는 색인이 없거나 체크섬이 틀리면 시작하지 않는다.

| 주소 | 기능 |
|---|---|
| `GET http://127.0.0.1:8092/health` | 코퍼스 해시·문서/청크 수 |
| `GET /openapi.json` | OpenAPI 3.1 명세 JSON |
| `POST /v1/rag/search` | 관련 공식 안내 검색 |
| `POST /v1/rag/context` | 거래·룰 검증 후 보고서용 사실·문서 근거 구성 |
| `POST /v1/rag/validate` | 구조화 사실·정확한 원문 인용 대조 |

로컬 개발 전용 API다. 외부 배포 시 인증·접근제어·트래픽/시간 제한은 백엔드와 별도로 구현해야 한다. Swagger UI는 포함하지 않는다. 2MB 본문 제한·최대 2,000거래·검색 topK 1~8을 적용한다.

## 코퍼스와 출처 추적

| 문서 | 원문 | 초기 선별 범위 |
|---|---|---|
| KoFIU STR | https://kofiu.go.kr/kor/policy/amls03.do | 정의, 보고 대상, 방법·절차 |
| KoFIU CTR | https://www.kofiu.go.kr/kor/policy/amls04.do | 개념과 STR과의 차이 |
| KoFIU CDD | https://www.kofiu.go.kr/kor/policy/amls05.do | 개념, 고객확인 대상 |

원문 3문서, 선별 6절을 최대 800자·중복 100자로 나눠 8청크로 만들었다. 페이지 메뉴·스크립트·주석은 대상 본문에서 제외한다. 안내 자료는 `official_guidance`이며 법령 원문으로 표시하지 않는다. `effectiveDate=null`은 시행일을 확인하지 않았다는 뜻이다. 수집일과 시행일은 다르다.

원본은 `data/rag/raw/`에, 버전별 원본은 `raw/snapshots/{documentId}/{sourceSha256}.html`에 보존한다. `data/rag/index.json`에는 다음 정보가 있다.

- 문서: 발행기관·원문 URL·출처 종류·관할·수집 시각·원문 SHA256.
- 청크: `chunkId`, 절 제목, 원문 그대로 정규화한 본문, 본문 SHA256.
- 위치: HTML 전체의 정규화된 텍스트 기준 `locator.start/end` 문자 오프셋. PDF 페이지 번호가 아니다.
- 코퍼스 전체 체크섬. 원문이 바뀌면 새 버전으로 추적한다.

원본·색인·실제 거래를 포함한 실행 산출물은 Git 제외다. 코드는 출처 목록과 재수집 방법을 제공한다. FATF FAQ는 공식 웹에서 확인했으나 로컬 다운로드가 HTTP 403으로 거절되어 포함하지 않았다. 국법센터 법령 페이지는 이번에 조문 본문을 안정적으로 추출하지 못해 제외했다. 법령 원문·FATF 사례집·은행 내부 규정은 별도 확보 후 추가해야 한다.

## 검색 요청·해석

```json
{"query":"고객확인에서 실제 소유자와 자금의 원천을 확인하는 이유", "topK":3, "jurisdiction":"KR"}
```

단어·한국어 문자 2-gram 기반 BM25를 사용한다. STR/CTR/CDD/KYC 약어는 대응하는 한국어 검색어로 확장한다. 결과에는 순위 점수와 출처·절·본문·해시가 함께 나온다. `sourceKind=law`로 검색하면 현재 법령 코퍼스가 없어 빈 결과를 반환한다.

`status=matched`는 관련 본문 검색 성공이고 `answerability=not_assessed`는 질문에 답할 수 있는지는 평가하지 않았다는 뜻이다. 점수·문자 겹침이 작으면 `no_evidence`로 반환하지만 의미 기반 근거 부족을 전부 판별하지 못한다. 특히 KB 내부 룰 점수 기준 질문에도 STR 일반 설명이 검색될 수 있다. `corpusCoverage`와 실제 인용의 내용을 확인하고 없는 세부 규정은 근거 부족으로 처리한다.

## 거래 Context 계약

```text
POST /v1/rag/context
{"graph": <백엔드 공통 Graph JSON 1.0>, "ruleResult": <룰 API 결과, 생략 가능>}
```

Graph는 `schemaVersion`, `targetTxId`, `asOf`, `windowStart`, `nodes`, `edges`, `meta`를 사용한다. edge의 정확한 필드는 기존 공통 규격의 txId, timestamp, fromAccountKey, toAccountKey, amountPaid, paymentCurrency, amountReceived, receivingCurrency, paymentFormat이다. 금액은 문자열이다. 정답 라벨을 포함하면 거부한다.

RAG는 전달된 룰 설정으로 기존 엔진을 재실행해 점수·발견 내용·근거 거래·집계가 일치하는지 확인한다. 룰 결과를 생략하면 기존 기본 설정으로 계산한다. V2 연구용 설정은 자동 적용하지 않는다.

주요 응답:

- `contextId`, `graphSha256`, `corpusSha256`: 재현·추적 기준.
- `targetTransaction`, `evidenceTransactions`: 원본 금액·통화·시각·거래 ID.
- `reportFacts`: 보고서에 사용할 `factId/value/evidenceTxIds`.
- `findings[].verifiedSummary`: 근거 거래의 건수·계좌 수·최초/최종 시각·경과 초·통화별 지급/수취 총액.
- `highlightTxIds`: UI가 강조할 실제 거래 ID.
- `citations`: 검색된 원문 청크·URL·절·해시·위치.
- `scope`, `limitations`, `generationContract`: 조회 범위와 사람 검토 요구사항.

계좌 사이의 연결은 동일 자금의 추적을 입증하지 않는다. 특히 순환 경로 각 구간의 총 송금액 합계를 고유 원금·순유입으로 해석하지 않는다. IBM 합성 데이터에 한국 안내를 붙이는 것은 설명 흐름 시연이며 실제 한국법 적용 판단이 아니다.

## Agent 연결·초안 검증

`rag-service/agent_tools.json`의 도구 정의와 `agent_tools.py`의 `AgentTools.call()`을 Agent 담당에게 전달한다. 허용한 도구는 `search_regulations`, `build_evidence_context`, `validate_report_draft` 3개다. 보고서 작성 규격은 `rag-service/report_instructions.md`를 사용한다. 이것만으로 실제 LLM Agent가 실행되는 것은 아니며 호출·오케스트레이션은 Agent 담당이 연결한다.

```json
{
  "contextId":"CTX-응답값",
  "facts":[{"factId":"target.amountPaid","value":"원본 금액 문자열"}],
  "citations":[{"chunkId":"응답의 청크 ID","quote":"해당 청크 본문에 정확히 포함된 짧은 인용"}]
}
```

`POST /v1/rag/validate` 요청에는 위 draft와 Context 생성 때와 같은 graph/ruleResult/query를 넣는다. 다른 Context, 변경한 수치·자료형, 없는 factId·chunkId, 원문에 없는 인용은 검증 실패다. 자유 문장 텍스트의 의미·법률 해석·누락 여부는 이 API의 검사 범위 밖이다. 검증 통과를 환각 0%로 주장하면 안 된다.

## 책임 분리와 통합 경로

Spring이 Neo4j에서 제한된 Subgraph를 읽고 룰·ML 결과를 결합한다. RAG는 Graph JSON과 룰 근거를 받아 문서 검색 결과를 묶고 Agent/LLM이 이를 설명한다. RAG 코드에는 Neo4j 자격증명과 Cypher가 없다.

`scripts/verify-rag.py`는 관리자용 읽기 전용 검증이다. 기존 DB 검사 도구로 실제 그래프를 읽어 API 연결을 확인한다. 이 성공이 Spring Graph API·ML 결합·최종 LLM 보고서·관제 UI의 완성을 의미하지 않는다. 결과는 `artifacts/rag-neo4j-smoke.json`과 검토용 `.md`로 저장한다.
