# 그래프 → RAG 근거 → 검토 초안 — 2026-11-16

`http://127.0.0.1:8093`의 로컬 시연 화면에서 실제 Neo4j 조회 결과를 기존 RAG API에 연결했다. 화면 흐름은 **거래·경로 선택 → 관련 근거·초안 준비 → 원문/거래 근거 확인 → 초안 재검사·다운로드**다. Spring 운영 API, ML 결합, 실제 LLM, 영속 분석/검토 이력은 별도다.

## 실행과 사용

준비된 로컬 Neo4j와 공식 문서 색인이 필요하다. 기존 실행 절차를 그대로 사용한다.

```powershell
.\scripts\start-local-services.ps1
.\scripts\start-graph-viewer.ps1
```

1. 거래를 고르고 핵심 경로에서 패턴을 선택한다.
2. `관련 근거·검토 초안 확인` 또는 아래 `근거·초안 준비`를 누른다.
3. 검토 초안·연결 근거/원문·수치/거래 근거·검사 결과 탭을 확인한다.
4. 초안의 인용이나 자료별 `원문·인용 위치 확인`으로 저장된 절과 인용 위치를 연다. 국내 법령·공식 안내·해외 참고를 구분한다.
5. 사실 값의 `거래 근거`에서 재계산 값과 실제 거래를 확인하고 `그래프에서 이 거래 보기`로 이동한다.
6. `초안 내려받기`/`근거 JSON`은 원래 초안을 재검사한 뒤 제공한다. 검증 실패나 만료 시 내려받기를 막는다.

거래·홉·기간·선택 패턴을 변경하면 이전 초안을 지운다. 단순히 핵심/주변 보기만 바꾸는 경우 같은 조회/패턴의 초안은 유지한다. 만료 안내의 `거래 다시 조회`는 현재 조건으로 DB를 새로 읽으며, 이후 새 초안을 준비한다.

## 표시 의미

- **자동 검사 통과:** `verification.valid` 및 거래/문서 식별자·필수 검사들이 일치한다. 금액·시간·건수·인용·저장 원문·제어형 본문/해시를 대조한다. 의미적 충분성·법률 적용·범죄/정상 확정은 포함하지 않는다.
- **근거 부족·추가 검토 필요:** 문서가 검색됐어도 동일 자금·실소유자·거래 목적 등의 확인이 남아 있음을 별도로 표시한다. 자동 검사 통과로 이 상태를 해제하지 않는다.
- **답변 보류:** RAG의 원질문 보류를 유지하며 보조 자료를 원질문의 답으로 표시하지 않는다.
- **생성 방식:** 검증된 사실·허용 문구 기반 `template`만 사용한다. 실제 LLM 호출은 없다.
- **보고서 범위:** 현재 조회 그래프와 전체 룰 결과다. 선택 경로의 원문 역할을 우선 표시하지만 다른 룰을 제거해 재검산 결과를 바꾸지 않는다.
- **원문 확인:** 저장된 코퍼스·스냅샷을 대조한다. 라이브 사이트 최신성 인증은 아니며 과거 거래에 대한 법률 유효 시점도 별도 확인한다.

## 화면용 연결 계약

같은 Graph·Rule을 사용하는 Agent 실행은 [Agent 검토 흐름](agent-review-workflow.md)의 `run_review_workflow`에서 제공한다. Agent의 재시도/상태 반환은 Python 도구 계약이며 아래 화면용 HTTP 계약과 별도다.

브라우저는 DB/코퍼스 자격증명 없이 현재 화면의 ID만 보낸다. GET graph 응답의 `reviewSnapshotId`는 서버가 보관한 Graph·Rule과 연결된다.

| 경로 | 입력 | 결과 |
|---|---|---|
| `POST /api/review/prepare` | `snapshotId`, `findingIndex`(미탐지는 null) | `reviewId`, 선택 경로, 원래 조회 출처, Context·Report·Verification |
| `GET /api/review/citation` | `reviewId`, `chunkId` | 보고서 버전의 원문 절·청크·코드포인트 강조 위치 |
| `POST /api/review/fact` | `reviewId`, `factId` | 동일 입력으로 재계산한 값·근거 거래 |
| `POST /api/review/verify` | `reviewId` | 서버 보관 원본 초안과 최신 재검사 결과 |
| `GET /api/review/download` | `reviewId`, `format`(json/markdown) | 서버에서 재검사한 원본 초안 첨부파일 |

내부 호출은 `http://127.0.0.1:8092`의 prepare/citation/facts/verify 네 연산으로 고정한다. 사용자 URL·질문·모델·그래프·점수·보고서·코퍼스 선택은 허용하지 않는다. POST는 동일 Origin, JSON 2KB, 정확한 필드만 받으며 중복 필드·비유한 수·변조 입력을 거부한다. 동시 RAG 요청은 2개, 상위 대기는 20초, 응답 읽기는 2MB다. 임의 쿼리·DB 쓰기·외부 모델·일반 프록시는 없다.

Graph/Review 임시 메모리는 각각 최대 24개·20분이다. 개수 제한·시간 만료·프로세스 재시작 후에는 409로 재조회를 요구한다. 기존 입력 대신 현재 DB를 몰래 재조회하지 않는다. 이는 장기 분석 이력이나 사람 승인 상태 저장이 아니다.

원문은 HTML로 실행하지 않고 텍스트로 표시하며, JavaScript `Array.from`으로 코드포인트 강조를 적용한다. 출처 링크는 등록된 공식 HTTPS 호스트만 열 수 있다. 키·개인 주소·원본 내부 예외는 브라우저로 전달하지 않는다.

## 검증

```powershell
python -m unittest discover -s graph-viewer/tests -q
node --test graph-viewer/tests/graph.test.mjs graph-viewer/tests/flow.test.mjs graph-viewer/tests/review.test.mjs
python scripts/verify-graph-rag.py --output artifacts/graph-viewer/new-rag-integration.json
```

- 관련 자동 검사 **55개**: Python26, JavaScript29. 합성 자료로 원문·사실·거래 연결, 변조/불일치 거부, 만료/개수 제한, 같은 시각/소수점, 다운로드 판정·보류·오래된 응답 차단을 검사했다.
- 실제 DB 시연 사례 **12건**의 Context·Report·Verification이 기존 RAG 직접 호출과 완전히 일치했다. 사실 **44개**, 인용 **158개**의 원문 위치·문서 버전·근거 거래를 조회했다. 원본 초안의 재검사, JSON/Markdown 첨부파일 **24개**의 본문 일치와 요청 경계 거부 **8종**도 확인했다.
- 결과는 `artifacts/graph-viewer/rag-integration-v2.json`이다. 기존 산출물은 덮어쓰지 않으며 재실행은 새 `--output`을 지정한다.
- DB 쓰기·실제 LLM 호출은 **0회**다. 기존 RAG 검색/룰/고정 평가 엔진은 변경하지 않았다. 이는 연결·정합성 검증이며 새로운 탐지 정확도·일반 금융 답변 정확도 평가는 아니다.

실제 화면에서 원문 모달, 사실→거래→그래프 이동, 재검사, 거래/범위 전환 시 초기화, 만료 후 재조회 복구, JSON 파일 전달과 좁은 화면의 탭/본문 표시를 확인했다. 실제 실행 데이터·원문·다운로드·스크린샷은 Git에서 제외한다.
